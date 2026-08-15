"""The family gate: unknown-tolerant + adjacency-aware (2026-08-15).

The RULE, in Python: ``other`` counts as UNKNOWN (it is what the tagger emits
when it cannot classify, and what the malformed-field coercion substitutes),
and a short, explicit list of family pairs is mutually admissible. Everything
else stays excluded — the gate is what keeps off-discipline postings out of a
user's matches, so each widening must be deliberate and named.

The SQL twin (``public.family_gate_passes``, migration 20260815200000) backs
both /jobs RPCs; ``tests/integration/test_family_gate_parity.py`` compares the
two implementations case-for-case against a live database, because drift
between them is a failure this repo has already shipped twice.
"""

from __future__ import annotations

import itertools

import pytest

from app.services.qualification.family_gate import passes_family_gate
from app.services.qualification.tagger import RoleFamily

_FAMILIES: tuple[str, ...] = tuple(RoleFamily.__args__)  # type: ignore[attr-defined]


# ---- the rule ---------------------------------------------------------------


def test_unclassified_target_is_ungated() -> None:
    for job in (*_FAMILIES, None):
        assert passes_family_gate(None, job) is True


@pytest.mark.parametrize("unknown", [None, "other"])
def test_unknown_job_family_passes_any_target(unknown: str | None) -> None:
    """``other`` means "couldn't classify" exactly like NULL. Treating them
    differently hid 1,852 live postings (11.1% of the catalog) from every
    classified target — see the migration header."""
    for target in _FAMILIES:
        assert passes_family_gate(target, unknown) is True


def test_exact_match_passes() -> None:
    for fam in _FAMILIES:
        assert passes_family_gate(fam, fam) is True


@pytest.mark.parametrize(
    ("target", "job"),
    [
        # Boundaries graders genuinely disagree on — still EXCLUDED, because
        # adjacency was deliberately not shipped (see the module docstring):
        # it was mitigation for a regression that no longer happens.
        ("engineering", "data_ml"),
        ("data_ml", "engineering"),
        ("sales", "customer_experience"),
        # Distinct disciplines: a PM target must not see designer postings.
        ("product", "design"),
        ("design", "product"),
        ("engineering", "design"),
        ("finance", "engineering"),
        ("marketing", "sales"),
    ],
)
def test_different_known_families_are_excluded(target: str, job: str) -> None:
    """The gate still does its job. If this starts failing, the catalog is
    leaking off-discipline postings into every target's matches."""
    assert passes_family_gate(target, job) is False


def test_only_unknown_widens_the_gate() -> None:
    """The ONLY thing that passes besides an exact match is an unknown job
    family. A count, so any future widening fails here rather than in
    production — adjacency, if ever added, must update this deliberately."""
    widened = [
        (t, j)
        for t, j in itertools.product(_FAMILIES, _FAMILIES)
        if t != j and passes_family_gate(t, j)
    ]
    assert {j for _t, j in widened} == {"other"}
