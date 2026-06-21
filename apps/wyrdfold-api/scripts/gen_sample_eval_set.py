"""Generate a PII-free synthetic ``eval_set.json`` for the recurring eval cadence.

The real eval fixture is a snapshot of production résumé/job data (PII) built by
``eval_grading_prompts.py --snapshot`` and is gitignored. This builds a
fabricated, schema-valid equivalent so the schema / cross-model evals
(``eval_phase1_triage``, ``eval_derive_target``) can run in CI / on-demand
WITHOUT touching real user data. It writes to the same gitignored path the eval
scripts read, so nothing PII-bearing is ever committed.

The data here is invented — no real person, employer, or posting.

Usage::

    uv run python scripts/gen_sample_eval_set.py            # -> tests/fixtures/eval_set.json
    uv run python scripts/gen_sample_eval_set.py --out PATH
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Make ``app`` importable when run as a file.
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.models.experience import OptimizedPayload, Role, Skill
from app.models.targets import (
    CategoryProfile,
    DomainProfile,
    JobTarget,
    NegativeProfile,
    ScoringProfile,
    SeniorityProfile,
)

_DEFAULT_OUT = Path(__file__).parent.parent / "tests" / "fixtures" / "eval_set.json"
_TS = datetime(2026, 1, 1, tzinfo=UTC)


def _payload() -> OptimizedPayload:
    """A fabricated frontend-leaning profile — used as the single user context."""
    return OptimizedPayload(
        summary=(
            "Frontend engineer, ~8 years, consumer SaaS. Strengths in web "
            "performance and design systems; some team-lead experience."
        ),
        roles=[
            Role(
                id="r1",
                company="Northwind Apps",
                title="Senior Frontend Engineer",
                start="2021",
                end=None,
                skills=["React", "TypeScript", "Accessibility"],
            ),
            Role(
                id="r2",
                company="Globex Web",
                title="Frontend Engineer",
                start="2018",
                end="2021",
                skills=["JavaScript", "CSS", "Webpack"],
            ),
        ],
        skills=[
            Skill(name="React", years=8),
            Skill(name="TypeScript", years=6),
            Skill(name="Accessibility", years=4),
        ],
        outcomes=[],
    )


def _target(
    tid: str,
    label: str,
    *,
    seniority_hint: str,
    keywords: dict[str, int],
    domain: list[str],
    promising: list[str],
    unpromising: list[str],
) -> JobTarget:
    return JobTarget(
        id=tid,
        label=label,
        scoring_profile=ScoringProfile(
            categories={"core_skills": CategoryProfile(keywords=keywords, weight=2.0)},
            seniority=SeniorityProfile(level=seniority_hint, signals=[]),
            domain=DomainProfile(signals=domain, weight=0.5),
            negative=NegativeProfile(keywords=["intern", "junior"], weight=-10.0),
        ),
        is_active=True,
        created_at=_TS,
        updated_at=_TS,
        seniority_hint=seniority_hint,  # type: ignore[arg-type]
        domain_hints=domain,
        example_promising_titles=promising,
        example_unpromising_titles=unpromising,
    )


# Two targets spanning IC tech + ops leadership. Titles are a clear-cut mix of
# on-target and off-target so phase-1 triage produces a meaningful agreement
# signal across models.
_TARGETS: dict[str, JobTarget] = {
    "t-fe": _target(
        "t-fe",
        "Staff Frontend Engineer",
        seniority_hint="staff",
        keywords={"React": 3, "TypeScript": 3, "Accessibility": 2},
        domain=["SaaS", "DTC"],
        promising=["Senior Frontend Engineer", "Staff Web Engineer"],
        unpromising=["Sales Manager", "Recruiter"],
    ),
    "t-cx": _target(
        "t-cx",
        "Director of CX Operations",
        seniority_hint="director",
        keywords={"Customer Experience": 3, "Operations": 3, "Support": 2},
        domain=["SaaS", "support"],
        promising=["Head of Customer Experience", "Director of Customer Success"],
        unpromising=["Frontend Engineer", "Warehouse Associate"],
    ),
}

_TITLES: dict[str, list[str]] = {
    "t-fe": [
        "Staff Frontend Engineer",
        "Senior React Engineer",
        "Frontend Engineer, Design Systems",
        "Senior Software Engineer (Frontend)",
        "Lead UI Engineer",
        "Account Executive",
        "Sales Manager",
        "Data Scientist",
    ],
    "t-cx": [
        "Director of CX Operations",
        "Head of Customer Experience",
        "Senior Manager, Support Operations",
        "Director of Customer Success",
        "Frontend Engineer",
        "Graphic Designer",
        "Warehouse Associate",
        "Sales Development Representative",
    ],
}


def build_eval_set() -> dict[str, Any]:
    payload = _payload().model_dump(mode="json")
    targets = {
        tid: {
            "label": t.label,
            "target": t.model_dump(mode="json"),
            "payload": payload,
        }
        for tid, t in _TARGETS.items()
    }
    cases = [
        {"target_id": tid, "title": title} for tid, titles in _TITLES.items() for title in titles
    ]
    return {"targets": targets, "cases": cases, "synthetic": True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    args = parser.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(build_eval_set(), indent=2), encoding="utf-8")
    print(f"Wrote synthetic eval set: {args.out} (PII-free)")


if __name__ == "__main__":
    main()
