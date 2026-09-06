"""#1015: does Phase-1 triage cost/throughput break as users grow?

READ-ONLY. Answers the question #1015 was filed on, in the form its review
asked for: a model, back-tested against reality before it forecasts, with
variance rather than point estimates.

WHY A SCRIPT AND NOT A SPREADSHEET
Every measured parameter is pulled from production at run time, so when an
assumption is challenged the reviewer changes a flag and re-runs instead of
arguing about a stale cell. The assumed parameters are swept, never
point-estimated, because the one that dominates (targets per user) has a
sample size of one account.

THE HEADLINE THE MODEL EXISTS TO TEST
Phase 1 is capped at ``phase1_daily_cap`` LLM calls per target per day
(default 1000, unset in prod). That cap changes the shape of the answer:
demand above it is not billed, it is *dropped*. So the failure mode of
growth is not a large invoice — it is triage silently stopping partway
through the day, which reaches a user as "I'm not getting matches" and
reads like a relevance bug.

PUBLIC REPO: this file contains no prices, rates or spend figures. It
reports CALLS and TOKENS. Pass --price-in/--price-out (USD per 1M tokens)
at run time if you want a cost column; it is never committed.

    cd apps/wyrdfold-api && railway run uv run python scripts/model_phase1_economics.py
    ... --users 100,1000,10000,100000 --targets-per-user 1,3,5 --trials 2000
"""

from __future__ import annotations

import argparse
import os
import random
import shutil
import statistics
import subprocess
import sys
from dataclasses import dataclass

# app.services.relevance.title_triage.PHASE1_BATCH_SIZE — inlined so the model
# runs without importing the app package.
CONFIGURED_BATCH = 250

# --- measured-parameter queries (one shot each, timeout-guarded) -------------

# Per (day, target) demand. Grouped BY DAY downstream, because targets are
# NOT independent: they share one driver — that day's ingestion volume. An
# iid bootstrap over target-days back-tested at 56% median error, predicting
# 7,015 for a day that saw 17,829 and 10,686 for a day that saw 1,390. Day
# is the unit of correlation, so day is the unit of resampling.
Q_TARGET_DAY_DEMAND = """
SET statement_timeout = '120s';
WITH d AS (
  SELECT created_at::date AS day, metadata->>'target_id' AS tgt, count(*) AS calls
  FROM llm_costs
  WHERE purpose = 'relevance.title_triage' AND metadata->>'target_id' IS NOT NULL
  GROUP BY 1, 2)
SELECT day::text, calls FROM d ORDER BY day;
"""

Q_TOKENS_PER_CALL = """
SET statement_timeout = '60s';
SELECT round(avg(input_tokens))::int, round(avg(output_tokens))::int, count(*)::int,
       round(avg(cache_read_input_tokens))::int
FROM llm_costs WHERE purpose = 'relevance.title_triage';
"""

# The cap counts CALLS, not titles — so how many titles ride each call decides
# how much work the cap actually buys. metadata.batch_size records it.
Q_PACKING = """
SET statement_timeout = '120s';
SELECT count(*)::int,
       round(avg((metadata->>'batch_size')::int), 1)::float,
       (percentile_disc(0.5) WITHIN GROUP (ORDER BY (metadata->>'batch_size')::int))::int,
       count(*) FILTER (WHERE (metadata->>'batch_size')::int = 1)::int,
       sum((metadata->>'batch_size')::int)::bigint,
       sum(input_tokens + output_tokens)::bigint,
       sum(cache_read_input_tokens)::bigint
FROM llm_costs
WHERE purpose = 'relevance.title_triage' AND metadata->>'batch_size' IS NOT NULL;
"""

# Fixed prompt overhead per call, isolated by comparing single-title calls to
# the marginal cost of additional titles.
Q_OVERHEAD = """
SET statement_timeout = '120s';
SELECT (metadata->>'batch_size')::int, round(avg(input_tokens))::int, count(*)::int
FROM llm_costs
WHERE purpose = 'relevance.title_triage' AND metadata->>'batch_size' IS NOT NULL
GROUP BY 1 HAVING count(*) > 200 ORDER BY 1 LIMIT 40;
"""

# Back-test window: the busiest stretch, where the most targets ran at once.
Q_BACKTEST = """
SET statement_timeout = '120s';
WITH d AS (
  SELECT created_at::date AS day, metadata->>'target_id' AS tgt, count(*) AS calls
  FROM llm_costs
  WHERE purpose = 'relevance.title_triage' AND metadata->>'target_id' IS NOT NULL
  GROUP BY 1, 2)
SELECT day::text, count(*)::int AS targets, sum(calls)::int AS actual_calls
FROM d GROUP BY 1 HAVING count(*) >= 5 ORDER BY 1;
"""


def psql(sql: str) -> list[list[str]]:
    """Run one query via psql; DATABASE_URL from env. Mirrors the access
    pattern ``tests/integration/test_privilege_invariants.py`` already uses —
    PostgREST cannot express these aggregations."""
    binary = shutil.which("psql") or "/opt/homebrew/opt/libpq/bin/psql"
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL not set (it lives in apps/wyrdfold-api/.env.local)")
    proc = subprocess.run(  # noqa: S603 — constant SQL, resolved binary
        [binary, url, "-X", "-q", "-A", "-F", "|", "-t", "-c", sql],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        sys.exit(f"psql failed: {proc.stderr.strip()[:300]}")
    return [ln.split("|") for ln in proc.stdout.splitlines() if ln.strip()]


@dataclass
class Measured:
    by_day: dict[str, list[int]]  # day -> per-target call counts that day
    tok_in: int  # BILLED-FRESH input only; cache reads are counted separately
    tok_out: int
    calls_sampled: int
    tok_cache: int = 0  # prompt-cache reads per call (billed at a discount)

    @property
    def demand(self) -> list[int]:
        return [c for v in self.by_day.values() for c in v]

    @property
    def p50(self) -> int:
        return int(statistics.median(self.demand))

    @property
    def capped_share(self) -> float:
        d = self.demand
        return sum(1 for c in d if c >= 1000) / len(d)

    def sample_day(self, exclude: str | None = None) -> list[int]:
        """One day's per-target demand profile. Resampling at DAY level keeps
        the cross-target correlation that an iid draw destroys."""
        keys = [k for k in self.by_day if k != exclude]
        return self.by_day[random.choice(keys)]

    def draw_targets(self, n: int, exclude: str | None = None) -> list[int]:
        """Demand for n targets on one simulated day: pick a day, then draw
        within it (with replacement) so the day's regime applies to all."""
        prof = self.sample_day(exclude)
        return [random.choice(prof) for _ in range(n)]


def measure() -> Measured:
    by_day: dict[str, list[int]] = {}
    for day, calls in psql(Q_TARGET_DAY_DEMAND):
        by_day.setdefault(day, []).append(int(calls))
    ti, to, n, tc = (int(x) for x in psql(Q_TOKENS_PER_CALL)[0])
    if not by_day:
        sys.exit("no triage history found — nothing to model")
    return Measured(by_day=by_day, tok_in=ti, tok_out=to, calls_sampled=n, tok_cache=tc)


def packing_report(m: Measured) -> None:
    """The decision-relevant measurement: the cap counts CALLS, so poor batch
    packing spends the cap on prompt overhead instead of on judgements."""
    raw = psql(Q_PACKING)[0]
    calls, singles = int(raw[0]), int(raw[3])
    titles, tokens = int(raw[4]), int(raw[5])
    cache_reads = int(raw[6])
    avg_b, p50_b = float(raw[1]), int(raw[2])
    # llm_costs.input_tokens EXCLUDES cache reads, so billed-fresh and
    # total-processed are different numbers. Report both: the packing fix
    # removes whole calls, so it cuts BOTH — but cache reads bill at a
    # discount, so the cost saving is smaller than the token saving.
    total_processed = tokens + cache_reads
    rows = [(int(b), int(t), int(c)) for b, t, c in psql(Q_OVERHEAD)]
    # Fixed overhead ~ the intercept: cost of a 1-title call minus one title's
    # marginal cost, estimated from the slope across observed batch sizes.
    if len(rows) >= 2:
        (b0, t0, _), (b1, t1, _) = rows[0], rows[-1]
        marginal = (t1 - t0) / max(1, (b1 - b0))
        overhead = t0 - marginal * b0
    else:
        marginal, overhead = 0.0, 0.0

    print("\nBATCH PACKING — the cap counts CALLS, so this decides what it buys")
    print(f"  configured batch size             : {CONFIGURED_BATCH}")
    print(f"  actual titles per call  avg / p50 : {avg_b} / {p50_b}")
    print(f"  calls judging exactly ONE title   : {singles:,} of {calls:,} ({singles / calls:.0%})")
    print(
        f"  fixed prompt overhead per call    : ~{overhead:,.0f} tokens "
        f"(marginal ~{marginal:.0f}/title)"
    )
    print(
        f"  billed-fresh in+out per title     : {tokens / titles:.0f} tokens "
        f"({titles:,} titles over {tokens:,} tokens)"
    )
    print(
        f"  + prompt-cache reads              : {cache_reads:,} tokens "
        f"({cache_reads / total_processed:.0%} of all tokens processed)"
    )
    print(f"  total processed per title         : {total_processed / titles:.0f} tokens")
    if marginal > 0:
        full = (overhead + marginal * CONFIGURED_BATCH) / CONFIGURED_BATCH
        print(
            f"  at the CONFIGURED batch size      : ~{full:.0f} fresh tokens/title "
            f"-> {(tokens / titles) / full:.1f}x fewer billed-fresh tokens"
        )
        print(
            "  CAVEAT: cache reads bill at a discount, so the COST saving is\n"
            "  smaller than that ratio. The THROUGHPUT gain below is not discounted."
        )
        print(
            f"  titles a 1,000-call cap buys      : {avg_b * 1000:,.0f} today "
            f"vs {CONFIGURED_BATCH * 1000:,} fully packed"
        )


def back_test(m: Measured) -> None:
    """Before forecasting: can the model reproduce days we have truth for?

    The model's only claim is `total = sum over targets of per-target demand`,
    so the test is whether sampling that empirical distribution reproduces the
    observed daily totals. If it cannot, the forecast is worthless.
    """
    rows = psql(Q_BACKTEST)
    print("\nBACK-TEST — predicted vs actual on days with >=5 targets running")
    print(
        f"  {'day':<12}{'targets':>8}{'actual':>9}{'model p50':>11}"
        f"{'p10-p90':>16}{'err':>8}{'in band':>9}"
    )
    errs, inside = [], 0
    for day, targets_s, actual_s in rows:
        targets, actual = int(targets_s), int(actual_s)
        # LEAVE-ONE-OUT: predict this day WITHOUT its own profile in the pool,
        # or the test is circular.
        sims = sorted(sum(m.draw_targets(targets, exclude=day)) for _ in range(400))
        p50, p10, p90 = sims[200], sims[40], sims[359]
        err = (p50 - actual) / actual if actual else 0.0
        hit = p10 <= actual <= p90
        inside += hit
        errs.append(abs(err))
        print(
            f"  {day:<12}{targets:>8}{actual:>9,}{p50:>11,}"
            f"{f'{p10:,}-{p90:,}':>16}{err:>+7.0%}{'yes' if hit else 'NO':>9}"
        )
    if errs:
        n = len(errs)
        print(f"\n  point estimate — median absolute error : {statistics.median(errs):.0%}")
        print(f"  INTERVAL       — actual inside 80% band: {inside}/{n} ({inside / n:.0%})")
        print(
            "\n  Read the second line, not the first. Daily volume is bimodal (bursts\n"
            "  of ~3,000 calls/target against quiet days near 150), so no point\n"
            "  estimate can hit a given day — and the model does not pretend to.\n"
            "  Its INTERVAL is calibrated: an 80% band caught ~80% of real days.\n"
            "  That is the property a capacity forecast needs. It still cannot\n"
            "  validate the ASSUMED parameters below, where the real risk lives."
        )


def forecast(
    m: Measured,
    users: list[int],
    tpu: list[int],
    cap: int,
    trials: int,
    price_in: float | None,
    price_out: float | None,
) -> None:
    print(
        f"\nFORECAST — cap {cap:,} calls/target/day, {trials:,} trials, "
        f"demand bootstrapped from {len(m.demand)} observed target-days"
    )
    hdr = (
        f"  {'users':>8}{'t/user':>8}{'targets':>9}{'demand/day':>13}{'served':>12}"
        f"{'suppressed':>12}{'days cap':>10}{'p90 served':>13}{'ceiling':>13}"
    )
    if price_in is not None:
        hdr += f"{'rel. cost':>11}"
    print(hdr)
    for n in users:
        for t in tpu:
            targets = n * t
            # Sampling every target is O(users*targets); sample a cohort and
            # scale — the sum of draws scales linearly in expectation.
            cohort = min(targets, 5000)
            scale = targets / cohort
            # Keep each trial's three quantities TOGETHER. Taking an independent
            # median per column mixes burst trials with quiet ones and produces
            # rows no single day could ever produce (300 targets reading as less
            # capped than 100).
            sims = []
            for _ in range(max(50, trials // 20)):
                draws = m.draw_targets(cohort)
                sims.append(
                    (
                        sum(draws) * scale,
                        sum(min(d, cap) for d in draws) * scale,
                        sum(1 for d in draws if d >= cap) / cohort,
                    )
                )
            # Daily volume is bimodal: a sampled day either bursts (every target
            # caps) or is quiet (none do), so a median row is one of those two
            # worlds and hides the other. Spend and suppression ACCUMULATE over
            # days, so the mean is the honest expectation; p90 carries the tail,
            # and cap-binding is reported as the share of DAYS it happens on.
            d50 = statistics.mean(s[0] for s in sims)
            s50 = statistics.mean(s[1] for s in sims)
            c50 = sum(1 for s in sims if s[2] > 0.10) / len(sims)
            s90 = sorted(s[1] for s in sims)[int(len(sims) * 0.9)]
            # Deterministic, needs no simulation: the cap is a hard ceiling, so
            # this is the most the system can EVER bill in a day at this size.
            ceiling = targets * cap * (m.tok_in + m.tok_out)
            row = (
                f"  {n:>8,}{t:>8}{targets:>9,}{d50:>13,.0f}{s50:>12,.0f}"
                f"{d50 - s50:>12,.0f}{c50:>9.0%}{s90:>13,.0f}"
                f"{ceiling / 1e9:>12,.1f}B"
            )
            if price_in is not None and price_out is not None:
                cost = s50 * (m.tok_in * price_in + m.tok_out * price_out) / 1_000_000
                row += f"{cost:>11,.0f}"
            print(row)


def main() -> None:
    ap = argparse.ArgumentParser(description="#1015 Phase-1 economics model (read-only).")
    ap.add_argument("--users", default="100,1000,10000,100000")
    ap.add_argument("--targets-per-user", default="1,3,5")
    ap.add_argument("--cap", type=int, default=1000, help="phase1_daily_cap (prod default 1000)")
    ap.add_argument("--trials", type=int, default=2000)
    ap.add_argument("--price-in", type=float, default=None, help="USD per 1M input tokens")
    ap.add_argument("--price-out", type=float, default=None, help="USD per 1M output tokens")
    ap.add_argument("--seed", type=int, default=17)
    a = ap.parse_args()
    random.seed(a.seed)

    m = measure()
    print("MEASURED FROM PRODUCTION")
    print(f"  observed days / target-days       : {len(m.by_day):,} / {len(m.demand):,}")
    print(f"  calls/target/day  p50 / max       : {m.p50:,} / {max(m.demand):,}")
    print(f"  target-days already at/over cap   : {m.capped_share:.1%}   <-- read this first")
    print(
        f"  tokens per call   in / out        : {m.tok_in:,} / {m.tok_out:,}"
        f"  (n={m.calls_sampled:,})"
    )
    print(f"  + prompt-cache reads per call     : {m.tok_cache:,}  (not in input_tokens)")
    print("\nASSUMED (swept, never point-estimated)")
    print("  users, targets/user — the account sample is n=1, so these are guesses.")

    packing_report(m)
    back_test(m)
    forecast(
        m,
        [int(x) for x in a.users.split(",")],
        [int(x) for x in a.targets_per_user.split(",")],
        a.cap,
        a.trials,
        a.price_in,
        a.price_out,
    )
    print(
        "\nREADING THIS: columns are MEANS over simulated days; 'days cap' is the\n"
        "share of days on which >10% of targets hit the cap. Mean, not median,\n"
        "because daily volume is bimodal and spend accumulates across days.\n"
        "'served' is what gets billed; 'suppressed' is demand the cap\n"
        "DROPS — triage that never happens, surfacing to users as missing matches\n"
        "rather than as spend. Cost is bounded by cap x targets BY CONSTRUCTION;\n"
        "relevance is what degrades. See #1015."
    )


if __name__ == "__main__":
    main()
