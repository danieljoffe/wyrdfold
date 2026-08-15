"""Eval: reference-JD extraction quality (``derive_profile_from_jd``).

Why this exists
---------------
``app/services/targets/derive_profile.py`` had **no eval at all**. The only
``derive_profile*`` eval is ``eval_derive_target.py``, which exercises
``derive_profile_from_label.py`` — a different prompt on a different input
(a role label, not a JD). So CONTRIBUTING's "pick the eval closest to the
prompt you touched" had no match for the JD-extraction path, and the
prompt-regression guard could only pin the prompt TEXT, never its quality.

That gap is how two defects reached prod unnoticed. Both were found by
reading an assembled scoring profile after adding one reference JD
(``docs/ux-resweep-targets-2026-08-14.md`` C2):

1. **Cross-section leakage.** ``ACH``, ``SEPA`` and ``PCI-DSS`` were emitted
   into BOTH a skills category AND ``domain.signals``. The two sections feed
   different scoring axes (skills fit / domain fit), so a leaked term is
   counted twice against the same job. Note this is NOT covered by the
   case-insensitive dedup in ``services/targets/merge.py`` — that is
   deliberately scoped per-category
   (``test_merge_case_variants_do_not_cross_categories``).

2. **Seniority-signal pollution.** ``design``, ``ship`` and ``own services``
   were emitted as ``seniority.signals``, sitting beside curated evidence like
   ``5+ years`` and ``project leadership``. They are generic responsibility
   verbs lifted from prose and carry no seniority information. Same defect
   family as #749, which fixed it for ``normalize_posting_title.py`` and not
   here.

What it measures
----------------
Per JD, against the LIVE ``SYSTEM_PROMPT`` imported from the service module
(so the eval cannot drift from the shipped prompt):

- ``leaked_terms``      — terms in both a skills category and domain signals.
- ``bare_signals``      — seniority signals carrying no seniority evidence.
- ``case_dupes``        — case-variant duplicates within one category.
- ``schema_ok``         — the payload round-trips through ``DerivedTarget``.
- ``skills`` / ``domain`` / ``signals`` counts — so a "fix" that simply
  empties a section is visible as a regression rather than a win.

The last point is why ``tests/fixtures/derive_jd_eval_set.json`` carries two
control cases. Driving ``leaked_terms`` to zero by emitting no domain signals
at all would score perfectly on the defect metrics and be strictly worse.

Fixtures are SYNTHETIC and committed. The prod snapshot
(``tests/fixtures/eval_set.json``) is real JD text — PII, gitignored, and
therefore unusable as a shared baseline anyone can re-run.

Cost: ~8 JDs x 1 call ~= $0.10 per run at Sonnet 4.6 rates.

Usage::

    cd apps/wyrdfold-api
    # baseline, before touching the prompt
    zsh -c 'source ~/.zshrc && uv run python scripts/eval_derive_profile_from_jd.py --label before'
    # ...edit SYSTEM_PROMPT, bump PROMPT_VERSION...
    zsh -c 'source ~/.zshrc && uv run python scripts/eval_derive_profile_from_jd.py --label after'
    uv run python scripts/eval_derive_profile_from_jd.py --compare before after
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

# Make scripts._openrouter importable.
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.models.targets import DerivedTarget
from app.services.llm.untrusted import wrap_untrusted
from app.services.targets.derive_profile import PROMPT_VERSION, SYSTEM_PROMPT
from scripts._openrouter import MODELS, call_model, get_api_key

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("eval_derive_profile_from_jd")

_FIXTURE_PATH = Path(__file__).parent.parent / "tests" / "fixtures" / "derive_jd_eval_set.json"
_RESULTS_DIR = Path(__file__).parent / "eval_results"
_MODEL_SHORT = "sonnet-4.6"  # production baseline for this call site

# A seniority signal must carry SENIORITY evidence. Accepted forms:
#   * a years-of-experience phrase ("5+ years", "8 years")
#   * a scope / leadership / autonomy / authority marker
#
# PRECISION NOTE. The first baseline run flagged 14 signals, but several were
# real seniority evidence the vocabulary simply didn't know — "technical
# authority", "across three teams", "set technical direction". Over-flagging
# those would make any before/after unreadable: the prompt could improve and
# the number stay flat, or vice versa. The vocabulary below is therefore
# deliberately GENEROUS about what counts as evidence, so what remains flagged
# is only the indefensible: bare responsibility verbs ("design", "ship",
# "drive improvements") and perk/boilerplate ("no on-call rotation").
#
# Matched on whole words, so "own services" does not satisfy "ownership".
# That asymmetry is intentional and tested: owning a named system is scope,
# a bare "own services" lifted from a duties list is not.
_YEARS_RE = re.compile(r"\d+\s*\+?\s*year", re.IGNORECASE)
_SENIORITY_MARKERS: frozenset[str] = frozenset(
    {
        # leadership / people
        "lead",
        "leads",
        "leading",
        "leadership",
        "mentor",
        "mentoring",
        "mentorship",
        "manage",
        "manager",
        "management",
        "supervise",
        "supervision",
        "oversight",
        "coach",
        "coaching",
        # Third-person inflections. Extractions phrase these as "mentors two
        # engineers" as often as "mentor", and the missing -s forms inflated
        # the first after-run's bare count with ~7 pure false positives.
        "mentors",
        "manages",
        "supervises",
        "oversees",
        "coaches",
        "guides",
        # level words
        "principal",
        "staff",
        "senior",
        "junior",
        "mid",
        "director",
        "head",
        "vp",
        "chief",
        "expert",
        "advanced",
        "seasoned",
        "veteran",
        "experienced",
        # scope / breadth
        "scope",
        "teams",
        "org",
        "organisation",
        "organization",
        "company-wide",
        "multi-team",
        "cross-functional",
        "stakeholder",
        "stakeholders",
        "roadmap",
        "strategic",
        "strategy",
        "direction",
        "vision",
        # authority / autonomy
        "authority",
        "autonomy",
        "autonomous",
        "independent",
        "independently",
        "ownership",
        "accountable",
        "accountability",
        "initiative",
        "decision",
        "decisions",
        "decision-making",
        "influence",
        "architect",
        "architecture",
        "architectural",
    }
)

# Perks, benefits and culture boilerplate. The prompt ALREADY tells the model
# to ignore these ("IGNORE company-specific noise: perks, benefits,
# compensation, culture/values statements..."), so one appearing as a
# seniority signal is an unambiguous rule violation, not a judgement call.
_PERK_MARKERS: frozenset[str] = frozenset(
    {
        "on-call",
        "oncall",
        "salary",
        "compensation",
        "bonus",
        "equity",
        "benefits",
        "insurance",
        "pto",
        "vacation",
        "holiday",
        "parental",
        "wellness",
        "snacks",
        "office",
        "hybrid",
        "remote",
        "relocation",
        "culture",
        "values",
        "mission",
        "perks",
        "funding",
        "series",
        "asynchronously",
        "async",
    }
)


def _words(s: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[a-zA-Z][a-zA-Z\-]*", s or "")}


def _is_seniority_evidence(signal: str) -> bool:
    """True when the signal actually says something about seniority."""
    if _YEARS_RE.search(signal):
        return True
    return bool(_words(signal) & _SENIORITY_MARKERS)


def _is_perk_noise(signal: str) -> bool:
    """True for perk/benefit/culture boilerplate the prompt already bans."""
    return bool(_words(signal) & _PERK_MARKERS)


def _as_dict(v: Any) -> dict[str, Any]:
    """Malformed-output guard.

    The model does not always honour the schema — one run returned a whole
    category as a float, which crashed the eval mid-flight and lost 23 other
    trials. An eval that dies on bad output cannot measure bad output, so
    every nested access is coerced and the damage is reported as a schema
    failure instead.
    """
    return v if isinstance(v, dict) else {}


def _as_list(v: Any) -> list[Any]:
    return v if isinstance(v, list) else []


def _skill_terms(profile: dict[str, Any]) -> dict[str, list[str]]:
    """{lowercased term -> [category names it appears in]}."""
    out: dict[str, list[str]] = {}
    for cat_name, cat in _as_dict(profile.get("categories")).items():
        for kw in _as_dict(_as_dict(cat).get("keywords")):
            if isinstance(kw, str):
                out.setdefault(kw.strip().lower(), []).append(cat_name)
    return out


def _analyse(payload: dict[str, Any]) -> dict[str, Any]:
    """Score one extraction. Pure — no network, unit-testable."""
    profile = _as_dict(_as_dict(payload).get("scoring_profile"))
    skills = _skill_terms(profile)
    domain_signals = [
        s for s in _as_list(_as_dict(profile.get("domain")).get("signals")) if isinstance(s, str)
    ]
    domain_lower = {s.strip().lower() for s in domain_signals}

    leaked = sorted(term for term in skills if term in domain_lower)

    seniority_signals = [
        s for s in _as_list(_as_dict(profile.get("seniority")).get("signals")) if isinstance(s, str)
    ]
    bare = [s for s in seniority_signals if not _is_seniority_evidence(s)]
    # Reported separately: a perk in the seniority section breaks an explicit
    # prompt rule, so it is the least arguable of the metrics.
    perks = [s for s in seniority_signals if _is_perk_noise(s)]

    # Case-variant duplicates WITHIN one category (the merge layer dedups
    # these downstream, but a clean prompt should not emit them at all).
    case_dupes: list[str] = []
    for cat_name, cat in _as_dict(profile.get("categories")).items():
        seen: dict[str, str] = {}
        for kw in _as_dict(_as_dict(cat).get("keywords")):
            if not isinstance(kw, str):
                continue
            k = kw.strip().lower()
            if k in seen:
                case_dupes.append(f"{cat_name}:{seen[k]}|{kw}")
            else:
                seen[k] = kw

    try:
        DerivedTarget.model_validate(payload)
        schema_ok = True
        schema_error = ""
    except Exception as exc:  # eval reports schema failure, never raises
        schema_ok = False
        schema_error = str(exc)[:200]

    return {
        "leaked_terms": leaked,
        "bare_signals": bare,
        "perk_signals": perks,
        "case_dupes": case_dupes,
        "schema_ok": schema_ok,
        "schema_error": schema_error,
        # Volume counters: a "fix" that empties a section must not read as a win.
        "n_skill_terms": len(skills),
        "n_domain_signals": len(domain_signals),
        "n_seniority_signals": len(seniority_signals),
        "domain_signals": domain_signals,
        "seniority_signals": seniority_signals,
    }


async def _run_one(case: dict[str, Any], api_key: str, trial: int = 0) -> dict[str, Any]:
    # Mirror the production input shape exactly — the real call site fences the
    # JD with wrap_untrusted, and the fence is part of what the model sees.
    user = "Extract the scoring profile from the job description below.\n\n" + wrap_untrusted(
        case["jd_text"], name="job_posting"
    )
    res = await call_model(
        model_slug=MODELS[_MODEL_SHORT],
        system=SYSTEM_PROMPT,
        user=user,
        api_key=api_key,
        max_tokens=2048,
    )
    if res.error or res.parsed is None:
        return {
            "id": case["id"],
            "trial": trial,
            "error": res.error or "unparseable JSON",
            "cost_usd": res.cost_usd,
        }
    return {
        "id": case["id"],
        "trial": trial,
        "cost_usd": res.cost_usd,
        "latency_ms": res.latency_ms,
        # Persist the raw extraction so the metrics can be recomputed offline.
        # The first baseline had to be re-run purely because a detector was
        # refined; with this, that iteration is free.
        "raw": res.parsed,
        **_analyse(res.parsed),
    }


def _summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in rows if "error" not in r]
    # Extraction is nondeterministic (no temperature is pinned anywhere in
    # app/ or in this helper, so both run at the provider default). Two runs of
    # the SAME prompt gave 2 leaks and then 0. So the headline numbers are
    # rates over trials, and `*_ever` counts how many distinct JDs tripped a
    # defect at least once — the "can this happen at all" signal.
    ids = {r["id"] for r in ok}
    ever_leak = {r["id"] for r in ok if r["leaked_terms"]}
    ever_bare = {r["id"] for r in ok if r["bare_signals"]}
    return {
        "trials": len(rows),
        "distinct_jds": len(ids),
        "errors": len(rows) - len(ok),
        "jds_ever_leaking": len(ever_leak),
        "jds_ever_bare": len(ever_bare),
        "cases_with_leak": sum(1 for r in ok if r["leaked_terms"]),
        "total_leaked_terms": sum(len(r["leaked_terms"]) for r in ok),
        "cases_with_bare_signal": sum(1 for r in ok if r["bare_signals"]),
        "total_bare_signals": sum(len(r["bare_signals"]) for r in ok),
        "total_perk_signals": sum(len(r["perk_signals"]) for r in ok),
        "total_case_dupes": sum(len(r["case_dupes"]) for r in ok),
        "schema_failures": sum(1 for r in ok if not r["schema_ok"]),
        "total_skill_terms": sum(r["n_skill_terms"] for r in ok),
        "total_domain_signals": sum(r["n_domain_signals"] for r in ok),
        "total_seniority_signals": sum(r["n_seniority_signals"] for r in ok),
        "cost_usd": round(sum(r.get("cost_usd", 0.0) for r in rows), 4),
    }


def _print_report(label: str, rows: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    logger.info("\n=== derive_profile_from_jd — %s (prompt %s) ===", label, PROMPT_VERSION)
    for r in rows:
        if "error" in r:
            logger.info("  %-22s t%-2s ERROR %s", r["id"], r.get("trial", 0), r["error"])
            continue
        flags = []
        if r["leaked_terms"]:
            flags.append(f"leak={r['leaked_terms']}")
        if r["bare_signals"]:
            flags.append(f"bare={r['bare_signals']}")
        if r["perk_signals"]:
            flags.append(f"PERK={r['perk_signals']}")
        if r["case_dupes"]:
            flags.append(f"dupes={r['case_dupes']}")
        if not r["schema_ok"]:
            flags.append("SCHEMA-FAIL")
        logger.info(
            "  %-22s t%-2s skills=%-3d domain=%-2d sen=%-2d %s",
            r["id"],
            r.get("trial", 0),
            r["n_skill_terms"],
            r["n_domain_signals"],
            r["n_seniority_signals"],
            "  ".join(flags) or "clean",
        )
    logger.info("  ---")
    for k, v in summary.items():
        logger.info("  %-24s %s", k, v)


def _compare(before: str, after: str) -> None:
    a = json.loads((_RESULTS_DIR / f"derive_jd_{before}.json").read_text())
    b = json.loads((_RESULTS_DIR / f"derive_jd_{after}.json").read_text())
    logger.info("\n=== %s -> %s ===", before, after)
    keys = [
        "jds_ever_leaking",
        "jds_ever_bare",
        "cases_with_leak",
        "total_leaked_terms",
        "cases_with_bare_signal",
        "total_bare_signals",
        "total_perk_signals",
        "total_case_dupes",
        "schema_failures",
        "total_skill_terms",
        "total_domain_signals",
        "total_seniority_signals",
    ]
    for k in keys:
        av, bv = a["summary"][k], b["summary"][k]
        arrow = "->" if av == bv else ("DOWN" if bv < av else "UP")
        logger.info("  %-26s %5s %5s  %s", k, av, bv, arrow)
    logger.info(
        "\n  Defect metrics should go DOWN. The three volume counters "
        "(skill_terms / domain_signals / seniority_signals) should hold roughly "
        "steady — a large drop means the prompt stopped extracting, not that it "
        "got cleaner."
    )


async def _main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--label", help="Run the eval and save under this label (e.g. before/after)")
    ap.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"))
    ap.add_argument(
        "--repeats",
        type=int,
        default=3,
        help="Trials per JD (default 3). Extraction is nondeterministic, so a "
        "single trial cannot separate a prompt change from sampling noise.",
    )
    ap.add_argument(
        "--reanalyse",
        metavar="LABEL",
        help="Recompute metrics from a saved run's raw payloads. Spend-free — "
        "use after refining a detector so the baseline stays comparable.",
    )
    args = ap.parse_args()

    if args.compare:
        _compare(*args.compare)
        return 0

    if args.reanalyse:
        path = _RESULTS_DIR / f"derive_jd_{args.reanalyse}.json"
        saved = json.loads(path.read_text())
        rows = []
        for r in saved["rows"]:
            if "raw" not in r:
                logger.info("  %s has no raw payload — re-run it", r.get("id"))
                return 1
            rows.append(
                {k: r[k] for k in ("id", "trial", "cost_usd", "latency_ms", "raw") if k in r}
                | _analyse(r["raw"])
            )
        summary = _summarise(rows)
        _print_report(f"{args.reanalyse} (re-analysed)", rows, summary)
        saved["summary"] = summary
        saved["rows"] = rows
        path.write_text(json.dumps(saved, indent=2))
        logger.info("\n  rewrote %s", path)
        return 0
    if not args.label:
        ap.error("one of --label or --compare is required")

    cases = json.loads(_FIXTURE_PATH.read_text())["cases"]
    api_key = get_api_key()
    rows = list(
        await asyncio.gather(
            *(_run_one(c, api_key, trial) for trial in range(args.repeats) for c in cases)
        )
    )
    summary = _summarise(rows)
    _print_report(args.label, rows, summary)

    _RESULTS_DIR.mkdir(exist_ok=True)
    out = _RESULTS_DIR / f"derive_jd_{args.label}.json"
    out.write_text(
        json.dumps(
            {
                "label": args.label,
                "prompt_version": PROMPT_VERSION,
                "model": MODELS[_MODEL_SHORT],
                "summary": summary,
                "rows": rows,
            },
            indent=2,
        )
    )
    logger.info("\n  wrote %s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
