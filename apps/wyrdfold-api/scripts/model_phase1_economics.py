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
(default 1000). That cap changes the shape of the answer: demand above it
is not billed, it is *deferred* — re-offered next cycle, and lost only when
saturation persists. So the failure mode of growth is not a large invoice;
it is triage stopping partway through the day, which reaches a user as
"I'm not getting matches" and reads like a relevance bug.

WHAT IS MEASURED VS WHAT IS PROJECTED — read this before quoting a number.
Every "suppressed" / "days cap" figure is a COUNTERFACTUAL: today's cap
applied to demand recorded before the cap existed. Only the PACKING section
measures current behaviour.

The demand sample is bounded to ``created_at < CAP_DEPLOYED`` so that a
capped observation cannot enter it — a cap that is live while calls are
recorded censors them, and re-capping censored numbers is circular. The
boundary is structural; the script reports what it excludes, so once Phase 1
runs post-cap you see the baseline going stale instead of the two regimes
quietly blending.

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
from datetime import date

# app.services.relevance.title_triage.PHASE1_BATCH_SIZE — inlined so the model
# runs without importing the app package.
CONFIGURED_BATCH = 250

# The commit that introduced ``phase1_daily_cap`` (744af754, 2026-08-31). The
# demand sample is restricted to BEFORE this date, because a cap that is live
# while calls are recorded CENSORS them: llm_costs would hold served work, not
# demand, and re-capping it would be circular.
#
# This is a hard boundary, not a sanity check. Review of #1016 caught the
# earlier design, which asked only "does ANY target-day exceed the cap?" — an
# EXISTENTIAL test. It passed because pre-cap days peak at 3,718, and it would
# have kept passing forever once post-cap days were added alongside them,
# silently mixing censored and uncensored populations. Override with
# --cap-deployed if the cap's effective date changes.
CAP_DEPLOYED = "2026-08-31"

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
    AND created_at < '{cap_deployed}'::date
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

# Is the demand series CENSORED by the very cap we are modelling? If the cap
# was live while these calls were recorded, a capped day records only the
# served calls and the model would be capping already-capped numbers — the
# circularity a reviewer rightly flagged on #1016. The test is empirical: a
# live cap of N truncates the series at N, so demand ABOVE the cap is proof it
# was not enforced then.
Q_CENSORING = """
SET statement_timeout = '120s';
WITH d AS (
  SELECT created_at::date AS day, metadata->>'target_id' AS tgt, count(*) AS calls
  FROM llm_costs
  WHERE purpose = 'relevance.title_triage' AND metadata->>'target_id' IS NOT NULL
    AND created_at < '{cap_deployed}'::date
  GROUP BY 1, 2)
SELECT count(*) FILTER (WHERE calls > {cap})::int,
       COALESCE(max(calls), 0)::int,
       COALESCE(min(day)::text, ''),
       COALESCE(max(day)::text, '')
FROM d;
"""

# Back-test window: the busiest stretch, where the most targets ran at once.
# What the boundary excludes. Silence here would hide the very regime change
# the boundary exists for: once Phase 1 runs again post-cap, this is non-zero
# and the forecast is extrapolating from an increasingly historical baseline.
Q_POST_CAP = """
SET statement_timeout = '120s';
WITH d AS (
  SELECT created_at::date AS day, metadata->>'target_id' AS tgt, count(*) AS calls
  FROM llm_costs
  WHERE purpose = 'relevance.title_triage' AND metadata->>'target_id' IS NOT NULL
    AND created_at >= '{cap_deployed}'::date
  GROUP BY 1, 2)
SELECT count(*)::int, COALESCE(max(calls), 0)::int, COALESCE(max(day)::text, '')
FROM d;
"""

# Backfill rows are distinguishable — ``metadata.trigger = 'activation_backfill'``
# — so how much of its allowance a backfill ACTUALLY spends is measurable, once
# any has run. Until then it is an assumption and must be swept and labelled as
# one. Raised in review of #1016: the allowance is a CEILING
# (``min(cap - used, floor(cap * fraction))``), not demand — a backfill stops
# when its candidate window is exhausted, and rejection-store hits cost nothing.
Q_BACKFILL_OBSERVED = """
SET statement_timeout = '120s';
WITH d AS (
  SELECT created_at::date AS day, metadata->>'target_id' AS tgt, count(*) AS calls
  FROM llm_costs
  WHERE purpose = 'relevance.title_triage'
    AND metadata->>'trigger' = 'activation_backfill'
  GROUP BY 1, 2)
SELECT count(*)::int, COALESCE(round(avg(calls)), 0)::int, COALESCE(max(calls), 0)::int
FROM d;
"""

# Proves the demand series carries NO backfill calls, so it is ingestion-only
# and the two claimants are genuinely additive rather than double-counted.
Q_SAMPLE_PURITY = """
SET statement_timeout = '120s';
SELECT count(*) FILTER (WHERE metadata->>'trigger' = 'activation_backfill')::int,
       count(*)::int
FROM llm_costs
WHERE purpose = 'relevance.title_triage' AND created_at < '{cap_deployed}'::date;
"""

Q_BACKTEST = """
SET statement_timeout = '120s';
WITH d AS (
  SELECT created_at::date AS day, metadata->>'target_id' AS tgt, count(*) AS calls
  FROM llm_costs
  WHERE purpose = 'relevance.title_triage' AND metadata->>'target_id' IS NOT NULL
    AND created_at < '{cap_deployed}'::date
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

    def over_cap_share(self, cap: int) -> float:
        """Share of observed target-days whose demand REACHES ``cap``.

        Takes the cap rather than hardcoding 1000, so this headline can never
        disagree with the scenario being modelled below it (it did: the figure
        was pinned at 1000 while the forecast honoured --cap)."""
        d = self.demand
        return sum(1 for c in d if c >= cap) / len(d)

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


def measure(cap_deployed: str) -> Measured:
    by_day: dict[str, list[int]] = {}
    for day, calls in psql(Q_TARGET_DAY_DEMAND.format(cap_deployed=cap_deployed)):
        by_day.setdefault(day, []).append(int(calls))
    ti, to, n, tc = (int(x) for x in psql(Q_TOKENS_PER_CALL)[0])
    if not by_day:
        sys.exit("no triage history found — nothing to model")
    return Measured(by_day=by_day, tok_in=ti, tok_out=to, calls_sampled=n, tok_cache=tc)


def censoring_report(m: Measured, cap: int, cap_deployed: str) -> None:
    """Establish that the demand sample is UNCENSORED, before anything uses it.

    If a cap is live while calls are recorded, ``llm_costs`` holds served work
    rather than demand: a capped day records only what ran, so applying the cap
    again is circular and the "suppressed" column infers a tail it had already
    destroyed. That objection is decisive when true.

    The guarantee is STRUCTURAL, not observational. Every demand query is
    bounded to ``created_at < cap_deployed``, so no capped observation can enter
    the sample regardless of what production accumulates later.

    An earlier version relied on an EXISTENTIAL check instead — "does any
    target-day exceed the cap?" — which review of #1016 correctly rejected:
    pre-cap days peak at 3,718, so that test would have kept reporting
    "not censored" forever while post-cap capped days were quietly mixed in
    beside them. It is kept below, demoted to corroboration of the boundary.
    """
    raw = psql(Q_CENSORING.format(cap=cap, cap_deployed=cap_deployed))[0]
    over, mx = int(raw[0]), int(raw[1])
    first, last = raw[2], raw[3]
    post = psql(Q_POST_CAP.format(cap_deployed=cap_deployed))[0]
    post_days, post_max, post_last = int(post[0]), int(post[1]), post[2]

    print("\nIS THE DEMAND SAMPLE UNCENSORED? (settled before anything uses it)")
    provenance = "744af754" if cap_deployed == CAP_DEPLOYED else "overridden via --cap-deployed"
    print(f"  cap deployed                      : {cap_deployed}  ({provenance})")
    print(f"  sample window (pre-cap only)      : {first} -> {last}")
    print(f"  GUARANTEE: every demand query is bounded to created_at < {cap_deployed},")
    print("  so a capped observation cannot enter the sample by construction.")
    print(f"  corroboration — target-days above {cap:,}: {over:,} (max {mx:,})")
    if not over:
        print(
            f"  NOTE: nothing in the sample exceeds {cap:,}. Not a problem — the\n"
            "  boundary above is the guarantee — but it means this run cannot\n"
            "  also corroborate it from the data, and a --cap set above observed\n"
            "  demand makes every 'suppressed' figure identically zero."
        )

    print(f"\n  EXCLUDED as post-cap                : {post_days:,} target-days", end="")
    if post_days:
        print(f" (through {post_last}, max {post_max:,})")
        print(
            "  ^^ Phase 1 has run since the cap shipped, so those days ARE\n"
            "  censored and are deliberately kept out of the demand sample. Two\n"
            "  consequences: this forecast is now extrapolating from a\n"
            "  historical baseline, and real suppression is finally measurable —\n"
            "  instrument demand at the decision point BEFORE the cap check and\n"
            "  model from that instead of from this script."
        )
    else:
        print(" — none yet; the cap has never run.")
        print(
            "  So every 'suppressed' / 'days cap' figure below is a COUNTERFACTUAL:\n"
            "  today's cap applied to demand recorded before it existed. Nothing\n"
            "  here observes the cap binding, because it never has."
        )


def backfill_report(cap: int, fraction: float, utilisation: float, cap_deployed: str) -> int:
    """Decide how many calls ONE activation spends — and be honest that this
    cannot currently be measured, only assumed.

    ``phase1_backfill_allowance`` returns ``min(cap - used, floor(cap *
    fraction))`` — a CEILING. The real backfill stops when its candidate window
    is exhausted, and rejection-store hits cost no LLM call, so treating the
    allowance as consumption overstates it (review of #1016).

    WHY THIS DOES NOT SELF-PROMOTE TO A MEASUREMENT. Backfill cost rows are
    tagged ``metadata.trigger='activation_backfill'``, which is tempting: group
    by (day, target) and average. But that denominator only contains activation
    days where the backfill made AT LEAST ONE call. A perfectly real activation
    spends ZERO — empty candidate window, everything already graded, every
    candidate served from the rejection store, or the pass stopping before the
    first call (``no_llm_client``, ``allowance`` 0, budget block). Those write
    no ``llm_costs`` row and vanish from the denominator entirely, so the
    average is conditional on being non-zero and biases HIGH — precisely the
    overstatement the allowance-vs-consumption fix was meant to remove.

    An earlier version of this function did promote itself on exactly that
    basis and labelled the result "MEASURED". It was caught in review of #1016.
    A real measurement needs an activation/attempt denominator that includes
    zero-call passes; nothing persists one today — ``backfill_phase1_for_target``
    reports its counts to the application log and returns, writing no durable
    attempt marker.

    So: the observed rows are reported as CONTEXT, explicitly conditional, and
    the returned figure always comes from the swept ``--backfill-utilisation``.
    """
    allowance = int(cap * fraction)
    days, avg_calls, max_calls = (int(x) for x in psql(Q_BACKFILL_OBSERVED)[0])
    pure = psql(Q_SAMPLE_PURITY.format(cap_deployed=cap_deployed))[0]
    bf_in_sample, total_in_sample = int(pure[0]), int(pure[1])
    used = int(allowance * utilisation)

    print("\nBACKFILL — the cap's second spender")
    print(
        f"  allowance per activation          : {allowance:,} calls "
        f"(floor({cap:,} x {fraction:g})) — a CEILING, not demand"
    )
    print(
        f"  demand series is ingestion-only   : {bf_in_sample:,} backfill rows "
        f"of {total_in_sample:,} — the two claimants are additive, not double-counted"
    )
    if days:
        print(
            f"  context — NON-ZERO backfill days  : {days:,} day(s), {avg_calls:,} calls "
            f"avg, max {max_calls:,}"
        )
        print(
            "  ^^ NOT a per-activation average. llm_costs only records days on\n"
            "  which the backfill made a call, so activations that spent ZERO\n"
            "  (empty window, all already graded, all rejection-store hits, or a\n"
            "  pass that stopped before its first call) are missing from the\n"
            "  denominator. This figure is conditional on being non-zero and so\n"
            "  biases HIGH. It does not replace the assumption below."
        )
    else:
        print("  observed backfill calls           : 0 — the backfill has never run")

    print(f"  ASSUMED consumption               : {utilisation:.0%} of allowance = {used:,} calls")
    if utilisation >= 1.0:
        print(
            "  ^^ 100% = a WORST-CASE BOUND, not a forecast. Intake is reduced by\n"
            "  the full slice AND the full slice is billed; both move together, so\n"
            "  read the rows as a bound on contention rather than expected spend.\n"
            "  Lower it with --backfill-utilisation to model partial consumption."
        )
    print(
        "  TO MEASURE THIS FOR REAL: persist an activation/backfill ATTEMPT row\n"
        "  (including zero-call passes) and average over that denominator.\n"
        "  backfill_phase1_for_target already computes candidates / store_hits /\n"
        "  llm_calls / stopped — it logs them and writes nothing durable."
    )
    return used


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
    print("  (spans ALL history on purpose: how many titles ride a call is")
    print("   unaffected by the cap, so it needs no pre-cap boundary)")
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


def back_test(m: Measured, cap_deployed: str, trials: int = 400) -> None:
    """Before forecasting: can the model reproduce days we have truth for?

    The model's only claim is `total = sum over targets of per-target demand`,
    so the test is whether sampling that empirical distribution reproduces the
    observed daily totals. If it cannot, the forecast is worthless.
    """
    rows = psql(Q_BACKTEST.format(cap_deployed=cap_deployed))
    print(
        f"\nBACK-TEST — predicted vs actual on days with >=5 targets running ({trials:,} sims/day)"
    )
    print(
        f"  {'day':<12}{'targets':>8}{'actual':>9}{'model p50':>11}"
        f"{'p10-p90':>16}{'err':>8}{'in band':>9}"
    )
    errs, inside = [], 0
    for day, targets_s, actual_s in rows:
        targets, actual = int(targets_s), int(actual_s)
        # LEAVE-ONE-OUT: predict this day WITHOUT its own profile in the pool,
        # or the test is circular.
        sims = sorted(sum(m.draw_targets(targets, exclude=day)) for _ in range(trials))
        p50 = sims[trials // 2]
        p10, p90 = sims[int(trials * 0.10)], sims[int(trials * 0.90)]
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
            "\n  WHAT THIS DOES *NOT* VALIDATE (raised in review of #1016): it tests\n"
            "  only that resampling observed demand reproduces observed demand. It\n"
            "  says nothing about the SUPPRESSION column, which is arithmetic\n"
            "  applied on top (demand minus cap), not something back-tested. Do not\n"
            "  let 80% coverage lend confidence to the suppression figure. It also\n"
            "  cannot validate the ASSUMED parameters below, where the real risk\n"
            "  lives. The suppression estimate is only as good as its premise that\n"
            "  demand is uncensored — which the section above tests directly."
        )


def forecast(
    m: Measured,
    users: list[int],
    tpu: list[int],
    cap: int,
    trials: int,
    price_in: float | None,
    price_out: float | None,
    price_cache: float | None,
    activation_rates: list[float],
    backfill_calls: int,
) -> None:
    """Forecast served vs deferred Phase-1 work, with BOTH claimants on the cap.

    ONE COUNTER, TWO SPENDERS — ``services/relevance/daily_cap.py`` says it
    outright: the activation backfill and ordinary poll-cycle ingestion both
    write ``purpose='relevance.title_triage'`` rows against the same
    ``metadata.target_id``, so they draw down the SAME per-target daily count.
    The backfill clamps itself to ``min(cap - used, floor(cap * fraction))``.

    The demand series this model bootstraps predates that design, so it is
    ORDINARY INGESTION DEMAND ONLY. Applying the whole cap to it would model a
    world where intake is the sole claimant — which overstates served intake and
    understates deferred work exactly when it matters most, since #1015 is about
    opening signup and activation is precisely what a signup burst produces.
    Raised in review of #1016.

    So ``activation_rate`` is swept, never estimated: on an activating target's
    day the backfill takes its share FIRST (the pessimistic ordering — running
    it after intake would leave it nothing), and intake is served from what
    remains.
    """
    print(
        f"\nFORECAST — cap {cap:,} calls/target/day, {trials:,} trials, "
        f"demand bootstrapped from {len(m.demand)} observed target-days"
    )
    print(
        f"  backfill shares the cap: an activating target spends {backfill_calls:,} "
        f"calls (an ASSUMPTION — see BACKFILL above)"
    )
    hdr = (
        f"  {'users':>8}{'t/user':>8}{'act%':>6}{'targets':>9}{'demand/day':>13}"
        f"{'intake srv':>12}{'backfill':>11}{'deferred':>12}{'days cap':>10}{'call ceil':>12}"
    )
    if price_in is not None:
        hdr += f"{'rel. cost':>11}"
    print(hdr)
    # What an activation actually consumes — measured if any backfill has run,
    # otherwise the swept assumption. NOT the raw allowance (review of #1016).
    backfill_reserve = backfill_calls
    for n in users:
        for t in tpu:
            targets = n * t
            # Sampling every target is O(users*targets); sample a cohort and
            # scale — the sum of draws scales linearly in expectation.
            cohort = min(targets, 5000)
            scale = targets / cohort
            # Draw the day profiles ONCE and evaluate every activation rate
            # against the SAME draws. Re-drawing per rate confounds the effect
            # being measured with sampler noise: demand/day wandered by ~7%
            # between rates that should share it exactly, which is larger than
            # the activation effect itself.
            all_draws = [m.draw_targets(cohort) for _ in range(trials)]
            for act in activation_rates:
                # Deterministic split of the cohort rather than a coin flip per
                # target: with the rate swept, the question is "what does THIS
                # activation level cost", not "how does that level vary" — and a
                # per-target flip would add variance that is an artefact of the
                # sampler, not of the system.
                n_act = round(cohort * act)
                sims = []
                for draws in all_draws:
                    intake_served = 0.0
                    capped = 0
                    for i, d in enumerate(draws):
                        # Backfill takes its share FIRST — the pessimistic
                        # ordering for intake. Running it after intake would
                        # leave it `cap - used`, i.e. often nothing.
                        room = cap - backfill_reserve if i < n_act else cap
                        intake_served += min(d, room)
                        if d >= room:
                            capped += 1
                    sims.append(
                        (
                            sum(draws) * scale,
                            intake_served * scale,
                            n_act * backfill_reserve * scale,
                            capped / cohort,
                        )
                    )
                # Daily volume is bimodal: a sampled day either bursts (every
                # target caps) or is quiet (none do), so a median row is one of
                # those two worlds and hides the other. Spend and deferral
                # ACCUMULATE over days, so the mean is the honest expectation;
                # cap-binding is reported as the share of DAYS it happens on.
                d50 = statistics.mean(s[0] for s in sims)
                s50 = statistics.mean(s[1] for s in sims)
                b50 = statistics.mean(s[2] for s in sims)
                c50 = sum(1 for s in sims if s[3] > 0.10) / len(sims)
                # Deterministic, needs no simulation — but ONLY over CALLS. The
                # cap bounds calls per target per day; it does not bound tokens,
                # because tokens per call depend on packing. Expressing it in
                # tokens would multiply by today's 11.1 titles/call, and the
                # packing fix this same script argues for would raise
                # tokens/call toward 250 and invalidate it. (Flagged in review
                # of #1016.)
                call_ceiling = targets * cap
                row = (
                    f"  {n:>8,}{t:>8}{act:>6.0%}{targets:>9,}{d50:>13,.0f}"
                    f"{s50:>12,.0f}{b50:>11,.0f}{d50 - s50:>12,.0f}{c50:>10.0%}"
                    f"{call_ceiling / 1e6:>11,.1f}M"
                )
                if price_in is not None and price_out is not None:
                    # Cache reads bill at their own (discounted) rate and are
                    # 56.5% of input here, so ignoring them understates. They
                    # get --price-cache-read; folding them into --price-in was
                    # not "conservative but exact either way" — it priced fresh
                    # input at the cache rate too (review of #1016). Defaults to
                    # the input rate, i.e. an explicit upper bound.
                    cache_rate = price_cache if price_cache is not None else price_in
                    billed = s50 + b50  # the backfill's calls are billed too
                    cost = (
                        billed
                        * (m.tok_in * price_in + m.tok_cache * cache_rate + m.tok_out * price_out)
                        / 1_000_000
                    )
                    row += f"{cost:>11,.0f}"
                print(row)


def _iso_date(raw: str) -> str:
    """argparse type for ``--cap-deployed``: parse, then re-serialise canonically.

    This value is the ONLY string input that reaches SQL — every demand query
    interpolates it as ``created_at < '{cap_deployed}'::date`` — and ``--cap``,
    the only other interpolated value, is already ``type=int``. Left as free
    text it is an injection boundary: ``--cap-deployed "2026-01-01'::date OR
    true--"`` builds

        AND created_at < '2026-01-01'::date OR true--'::date

    which silently defeats the pre-cap boundary the censoring guarantee rests
    on, while still printing a normal-looking table. The script is read-only by
    intent, but intent is not the control — the DATABASE_URL credential is, and
    it points at production.

    Parsing to a ``date`` and interpolating only ``date.isoformat()`` makes the
    value safe by construction rather than by escaping, and rejects impossible
    boundaries before any query runs. Raised in review of #1016.
    """
    try:
        return date.fromisoformat(raw.strip()).isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"must be an ISO date (YYYY-MM-DD), got {raw!r}: {exc}"
        ) from exc


def _validate(a: argparse.Namespace) -> None:
    """Reject impossible scenarios instead of modelling them.

    Every parameter here has a physical meaning, and out-of-range values do not
    fail — they produce plausible-looking tables. Two examples this catches, both
    reproduced before the check existed: ``--backfill-fraction 1.5`` makes the
    reserve exceed the cap, so ``room`` goes negative and the model reports
    NEGATIVE served intake (-50,000 on a 100-target run) with the deficit
    silently added to "deferred"; ``--activation-rates 1.5`` activates more
    targets than exist in the cohort and bills 150 activations against 100
    targets. For a script whose numbers are quoted in a capacity decision,
    printing that is worse than refusing to run (review of #1016).
    """
    errs: list[str] = []
    if a.cap <= 0:
        errs.append(f"--cap must be > 0 (got {a.cap}); a zero/negative cap is not 'unlimited'")
    if a.trials <= 0:
        errs.append(f"--trials must be > 0 (got {a.trials})")
    if a.backtest_trials <= 0:
        errs.append(f"--backtest-trials must be > 0 (got {a.backtest_trials})")
    if not 0.0 <= a.backfill_fraction <= 1.0:
        errs.append(
            f"--backfill-fraction must be in [0,1] (got {a.backfill_fraction}); "
            "above 1 the backfill reserve exceeds the cap and served intake goes negative"
        )
    if not 0.0 <= a.backfill_utilisation <= 1.0:
        errs.append(
            f"--backfill-utilisation must be in [0,1] (got {a.backfill_utilisation}); "
            "it is a SHARE of the allowance, so above 1 spends more than the cap allows"
        )
    for label, raw in (("--users", a.users), ("--targets-per-user", a.targets_per_user)):
        for tok in raw.split(","):
            try:
                if int(tok) <= 0:
                    errs.append(f"{label} entries must be > 0 (got {tok})")
            except ValueError:
                errs.append(f"{label} entries must be integers (got {tok!r})")
    for tok in a.activation_rates.split(","):
        try:
            if not 0.0 <= float(tok) <= 1.0:
                errs.append(
                    f"--activation-rates entries must be in [0,1] (got {tok}); "
                    "above 1 activates more targets than the cohort holds, "
                    "below 0 produces negative backfill volume"
                )
        except ValueError:
            errs.append(f"--activation-rates entries must be numbers (got {tok!r})")
    for label, val in (
        ("--price-in", a.price_in),
        ("--price-out", a.price_out),
        ("--price-cache-read", a.price_cache_read),
    ):
        if val is not None and val < 0:
            errs.append(f"{label} must be >= 0 (got {val})")
    if errs:
        sys.exit("refusing to run — invalid scenario:\n  " + "\n  ".join(errs))


def main() -> None:
    ap = argparse.ArgumentParser(description="#1015 Phase-1 economics model (read-only).")
    ap.add_argument("--users", default="100,1000,10000,100000")
    ap.add_argument("--targets-per-user", default="1,3,5")
    ap.add_argument("--cap", type=int, default=1000, help="phase1_daily_cap (prod default 1000)")
    # Used LITERALLY. It was previously scaled by //20 inside forecast() while
    # the header printed the unscaled figure, so "--trials 2000" reported 2,000
    # and ran 100 (caught in review of #1016). Default lowered to match the
    # simulation count that was actually running, so runtime is unchanged.
    ap.add_argument("--trials", type=int, default=200, help="forecast simulations per row")
    ap.add_argument(
        "--backtest-trials", type=int, default=400, help="simulations per back-tested day"
    )
    ap.add_argument(
        "--cap-deployed",
        type=_iso_date,
        default=CAP_DEPLOYED,
        help=f"date phase1_daily_cap went live; demand is sampled strictly before it "
        f"(default {CAP_DEPLOYED})",
    )
    ap.add_argument("--price-in", type=float, default=None, help="USD per 1M input tokens")
    ap.add_argument("--price-out", type=float, default=None, help="USD per 1M output tokens")
    ap.add_argument(
        "--price-cache-read",
        type=float,
        default=None,
        help="USD per 1M prompt-cache-read tokens (defaults to --price-in, an upper bound)",
    )
    ap.add_argument(
        "--activation-rates",
        default="0,0.05,0.25",
        help="share of targets activating on a given day; each reserves the "
        "backfill's slice of the shared cap (swept, never estimated)",
    )
    ap.add_argument(
        "--backfill-fraction",
        type=float,
        default=0.25,
        help="phase1_backfill_cap_fraction (prod default 0.25)",
    )
    ap.add_argument(
        "--backfill-utilisation",
        type=float,
        default=1.0,
        help="share of the backfill ALLOWANCE consumed per activation, in [0,1]. "
        "1.0 (default) is a worst-case bound, not a forecast. Always governs the "
        "model: observed backfill rows are reported as context but never replace "
        "it, because llm_costs cannot see zero-call activations",
    )
    ap.add_argument("--seed", type=int, default=17)
    a = ap.parse_args()
    _validate(a)
    random.seed(a.seed)

    m = measure(a.cap_deployed)
    print("MEASURED FROM PRODUCTION")
    print(f"  observed days / target-days       : {len(m.by_day):,} / {len(m.demand):,}")
    print(f"  calls/target/day  p50 / max       : {m.p50:,} / {max(m.demand):,}")
    print(
        f"  target-days whose demand >= cap   : {m.over_cap_share(a.cap):.1%}"
        f"   (cap={a.cap:,}) <-- a PROJECTION, see below"
    )
    print(
        f"  tokens per call   in / out        : {m.tok_in:,} / {m.tok_out:,}"
        f"  (n={m.calls_sampled:,})"
    )
    print(f"  + prompt-cache reads per call     : {m.tok_cache:,}  (not in input_tokens)")
    print("\nASSUMED (swept, never point-estimated)")
    print("  users, targets/user — the account sample is n=1, so these are guesses.")

    censoring_report(m, a.cap, a.cap_deployed)
    bf_calls = backfill_report(a.cap, a.backfill_fraction, a.backfill_utilisation, a.cap_deployed)
    packing_report(m)
    back_test(m, a.cap_deployed, a.backtest_trials)
    forecast(
        m,
        [int(x) for x in a.users.split(",")],
        [int(x) for x in a.targets_per_user.split(",")],
        a.cap,
        a.trials,
        a.price_in,
        a.price_out,
        a.price_cache_read,
        [float(x) for x in a.activation_rates.split(",")],
        bf_calls,
    )
    print(
        "\nREADING THIS: columns are MEANS over simulated days; 'days cap' is the\n"
        "share of days on which >10% of targets hit the cap. Mean, not median,\n"
        "because daily volume is bimodal and spend accumulates across days.\n"
        "'intake srv' + 'backfill' is what would be billed — BOTH draw on the\n"
        "same per-target counter. 'deferred' is intake the cap would push to the\n"
        "next cycle, lost only when saturation persists, which reaches a user as\n"
        "missing matches rather than as spend.\n"
        "\nThese are PROJECTIONS, not observations: the cap postdates this demand\n"
        "window entirely (see the censoring section above), so it has never yet\n"
        "run against live demand. Cost is bounded by cap x targets BY\n"
        "CONSTRUCTION; throughput is what degrades. See #1015."
    )


if __name__ == "__main__":
    main()
