"""Eval: posting-title canonicalization (``normalize_posting_title``).

Why this exists
---------------
#745 put an LLM call on the create-from-URL path — every URL-created target
now normalizes the posting's own title into a reusable role label — and
shipped WITHOUT a spend-bearing eval. The release note flagged that as an
owner call and it stayed open. #760 built an eval for
``derive_profile.py``; this is the one for the OTHER prompt #745 added.

What makes this prompt worth measuring is that its output is not cosmetic:
the label becomes ``targets.normalized_label``, the dedup key behind the
``targets_normalized_label_key`` UNIQUE constraint. A label that fails to
canonicalize does not merely read badly — it mints a new catalog row, which
is the defect #745 exists to prevent.

What it measures
----------------
Against the LIVE ``SYSTEM_PROMPT`` imported from the service module, at
production temperature (see below):

- **convergence** — the headline. Every posting in a fixture group is the same
  role wearing different company/team/product noise, so all of them must
  collapse to ONE ``crud.normalize_label`` value. Groups, not individual
  strings, because "reads nicely" is not the property that matters; agreeing
  with the other postings of the same role is.
- **seniority_leak** — a level word in the output that is NOT in the source
  title. The prompt's central rule is that the TITLE is the only source of
  seniority; #749 exists because an earlier version inferred it from JD prose,
  which makes the dedup key depend on how a posting happens to read. Groups
  flagged ``expect_no_seniority`` pair a bare title with a body shouting
  "10+ years, staff-level".
- **noise_survivors** — fixture-declared company / team / product / req-id /
  location / marketing tokens that should have been stripped.
- **over_length** — longer than ``MAX_LABEL_CHARS``.
- **emptied** — a label that lost the function word entirely. Stripping noise
  down to nothing would score perfectly on every other metric and be useless,
  so it is counted.

TEMPERATURE — 0.0, matching production (``complete_json`` defaults it to 0.0
and forwards it). An eval that leaves it unset samples at the provider default
and measures a distribution the app never produces.

TRANSPORT CAVEAT — this harness uses OpenRouter JSON mode; production uses
``complete_json``'s forced tool use, where the API validates the shape before
returning. Shape failures here mean the prompt alone did not pin the schema,
not that prod would emit them.

Fixtures are SYNTHETIC and committed; the prod snapshot is PII and gitignored.

Cost: 11 postings x repeats, ~$0.02/call — about $0.25 for a 3-trial run.

Usage::

    cd apps/wyrdfold-api
    railway run sh -c '\
      uv run python scripts/eval_normalize_posting_title.py --label baseline'
    uv run python scripts/eval_normalize_posting_title.py --compare before after
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services.targets.crud import normalize_label
from app.services.targets.normalize_posting_title import (
    MAX_LABEL_CHARS,
    SYSTEM_PROMPT,
    _build_user_message,
)
from scripts._openrouter import MODELS, call_model, get_api_key

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("eval_normalize_posting_title")

_FIXTURE_PATH = (
    Path(__file__).parent.parent / "tests" / "fixtures" / "normalize_title_eval_set.json"
)
_RESULTS_DIR = Path(__file__).parent / "eval_results"
_MODEL_SHORT = "sonnet-4.6"

# The level words the prompt says to KEEP when the title has one — and
# therefore the ones that must never appear when the title does not.
_SENIORITY_WORDS: frozenset[str] = frozenset(
    {
        "junior",
        "jr",
        "entry",
        "associate",
        "mid",
        "intermediate",
        "senior",
        "sr",
        "staff",
        "principal",
        "lead",
        "director",
        "vp",
        "head",
        "chief",
        "distinguished",
        "fellow",
    }
)


# "Sr." -> "Senior" is the canonicalization working, not an invented level.
_ABBREV: dict[str, str] = {"sr": "senior", "jr": "junior"}


def _words(s: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[a-zA-Z]+", s or "")}


def _seniority_in(s: str) -> set[str]:
    return _words(s) & _SENIORITY_WORDS


def analyse_posting(
    *, label: str, title: str, noise_tokens: list[str], expect_no_seniority: bool
) -> dict[str, Any]:
    """Score one normalization. Pure — no network, unit-testable."""
    # A level word the TITLE did not have. When the fixture says the title is
    # bare, any level word at all is a leak; otherwise only ones absent from
    # the title count (so "Sr." -> "Senior" is not punished as invented).
    out_sen = _seniority_in(label)
    title_sen = _seniority_in(title)
    title_sen = {_ABBREV.get(w, w) for w in title_sen}
    leaked = sorted(w for w in out_sen if _ABBREV.get(w, w) not in title_sen)
    if not expect_no_seniority and not leaked:
        leaked = []

    survivors = sorted(t for t in noise_tokens if t and t.lower() in (label or "").lower())

    return {
        "label": label,
        "normalized": normalize_label(label or ""),
        "seniority_leak": leaked,
        "noise_survivors": survivors,
        "over_length": len(label or "") > MAX_LABEL_CHARS,
        # Stripping everything would ace every other metric. A label needs at
        # least two words to plausibly still name a role ("Product Manager").
        "emptied": len((label or "").split()) < 2,
    }


def summarise_group(group: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in rows if "error" not in r]
    distinct = sorted({r["normalized"] for r in ok})
    return {
        "id": group["id"],
        "postings": len(ok),
        # 1 = every posting of this role collapsed to the same dedup key.
        "distinct_labels": len(distinct),
        "labels": distinct,
        "seniority_leaks": sum(len(r["seniority_leak"]) for r in ok),
        "noise_survivors": sum(len(r["noise_survivors"]) for r in ok),
        "over_length": sum(1 for r in ok if r["over_length"]),
        "emptied": sum(1 for r in ok if r["emptied"]),
        "errors": len(rows) - len(ok),
    }


async def _run_one(
    group: dict[str, Any], posting: dict[str, Any], api_key: str, trial: int
) -> dict[str, Any]:
    res = await call_model(
        model_slug=MODELS[_MODEL_SHORT],
        system=SYSTEM_PROMPT,
        # The REAL user-message builder, so the eval cannot drift from the
        # excerpt length / framing the service actually sends.
        user=_build_user_message(posting["title"], posting.get("jd_text", "")),
        api_key=api_key,
        max_tokens=256,
        temperature=0.0,
    )
    base = {"group": group["id"], "title": posting["title"], "trial": trial}
    if res.error or res.parsed is None:
        return {**base, "error": res.error or "unparseable JSON", "cost_usd": res.cost_usd}
    label = res.parsed.get("label")
    if not isinstance(label, str):
        return {**base, "error": f"no string label: {res.parsed!r}"[:160], "cost_usd": res.cost_usd}
    return {
        **base,
        "cost_usd": res.cost_usd,
        "raw": res.parsed,
        **analyse_posting(
            label=label,
            title=posting["title"],
            noise_tokens=group.get("noise_tokens", []),
            expect_no_seniority=bool(group.get("expect_no_seniority")),
        ),
    }


def _report(label: str, groups: list[dict[str, Any]], rows: list[dict[str, Any]]) -> dict[str, Any]:
    logger.info("\n=== normalize_posting_title — %s ===", label)
    totals = {"leaks": 0, "noise": 0, "over": 0, "emptied": 0, "unconverged": 0, "errors": 0}
    per_group = []
    for g in groups:
        grows = [r for r in rows if r["group"] == g["id"]]
        s = summarise_group(g, grows)
        per_group.append(s)
        converged = s["distinct_labels"] <= 1
        if not converged:
            totals["unconverged"] += 1
        totals["leaks"] += s["seniority_leaks"]
        totals["noise"] += s["noise_survivors"]
        totals["over"] += s["over_length"]
        totals["emptied"] += s["emptied"]
        totals["errors"] += s["errors"]
        logger.info(
            "  %-28s %s  %d label(s): %s",
            s["id"],
            "CONVERGED" if converged else "SPLIT    ",
            s["distinct_labels"],
            s["labels"],
        )
        for r in grows:
            flags = []
            if r.get("seniority_leak"):
                flags.append(f"SENIORITY-LEAK={r['seniority_leak']}")
            if r.get("noise_survivors"):
                flags.append(f"noise={r['noise_survivors']}")
            if r.get("over_length"):
                flags.append("OVER-LENGTH")
            if r.get("emptied"):
                flags.append("EMPTIED")
            if flags:
                logger.info("      ↳ %r  %s", r.get("label"), "  ".join(flags))
    logger.info("  ---")
    for k, v in totals.items():
        logger.info("  %-14s %s", k, v)
    logger.info("  %-14s $%.4f", "cost_usd", sum(r.get("cost_usd", 0.0) for r in rows))
    return {"totals": totals, "groups": per_group}


def _compare(before: str, after: str) -> None:
    a = json.loads((_RESULTS_DIR / f"normalize_title_{before}.json").read_text())
    b = json.loads((_RESULTS_DIR / f"normalize_title_{after}.json").read_text())
    logger.info("\n=== %s -> %s ===", before, after)
    for k in ("unconverged", "leaks", "noise", "over", "emptied", "errors"):
        av, bv = a["summary"]["totals"][k], b["summary"]["totals"][k]
        logger.info(
            "  %-14s %4s %4s  %s", k, av, bv, "->" if av == bv else ("DOWN" if bv < av else "UP")
        )
    logger.info("\n  All six should go DOWN or hold at 0. `unconverged` is the one that")
    logger.info("  matters most — it is the dedup key failing, not a wording preference.")


async def _main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--label", help="Run and save under this label")
    ap.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"))
    ap.add_argument("--repeats", type=int, default=3)
    args = ap.parse_args()

    if args.compare:
        _compare(*args.compare)
        return 0
    if not args.label:
        ap.error("one of --label or --compare is required")

    groups = json.loads(_FIXTURE_PATH.read_text())["groups"]
    api_key = get_api_key()
    rows = list(
        await asyncio.gather(
            *(
                _run_one(g, p, api_key, trial)
                for trial in range(args.repeats)
                for g in groups
                for p in g["postings"]
            )
        )
    )
    summary = _report(args.label, groups, rows)

    _RESULTS_DIR.mkdir(exist_ok=True)
    out = _RESULTS_DIR / f"normalize_title_{args.label}.json"
    out.write_text(json.dumps({"label": args.label, "summary": summary, "rows": rows}, indent=2))
    logger.info("\n  wrote %s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
