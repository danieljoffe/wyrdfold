"""Cheap title-seniority pre-gate for Phase 2 (#902).

Phase 1's binary ``promising`` verdict is domain-oriented and deliberately
permissive ("lean-promising"), so it forwards many roles whose *seniority* is
well below the target's — a "Customer Success Manager" for a *Director*-level
target. Phase 2 (Sonnet) then spends a real grade discovering the obvious
mismatch. This module is a near-free string gate that drops clearly-below-level
titles *before* Phase 2, complementing — not replacing — the Phase 1 verdict.

Pure + dependency-free so it's trivially testable and safe to call in the hot
path. Conservative by design: it only rejects titles whose seniority is
*explicitly* below the bar, and passes anything ambiguous (no level token) so a
real role with an unusual title is never silently dropped.
"""

from __future__ import annotations

import re

from app.models.targets import SeniorityHint

# The canonical seniority ladder (mirrors models.targets.SeniorityHint).
_RANK: dict[str, int] = {
    "ic": 0,
    "senior": 1,
    "staff": 2,
    "manager": 3,
    "director": 4,
    "vp": 5,
    "c_level": 6,
}

# Title tokens → the rank they imply. Ranks are pulled from ``_RANK`` by name so
# this mapping can never drift from the canonical ladder (#47: the old table
# hard-coded staff/principal/lead to 1, contradicting ``_RANK["staff"] == 2`` and
# derive_profile_from_label's "Lead/Principal -> staff"). Checked high-to-low;
# first match wins, so "Senior Director" reads as director, and "Senior Staff
# Engineer" reads as staff (the higher rung), not senior. Word-boundaried so a
# substring (e.g. "management") can't trip a token (e.g. "manager").
#
# NB ``scoring._TITLE_TIERS`` is a deliberately SEPARATE, finer-grained 0-8
# ladder over title tokens used for keyword-score *penalties* (it distinguishes
# role-type tiers like analyst/engineer that aren't seniority levels). It is not
# this gate's enum-aligned ladder; the two are not merged on purpose.
_TITLE_PATTERNS: tuple[tuple[int, re.Pattern[str]], ...] = (
    (_RANK["c_level"], re.compile(r"\b(chief|c[xeofmt]o|c-level|chief officer)\b", re.I)),
    (_RANK["vp"], re.compile(r"\b(vp|svp|evp|vice[\s-]?president)\b", re.I)),
    (_RANK["director"], re.compile(r"\b(director|head\s+of|global\s+head|group\s+head)\b", re.I)),
    (_RANK["manager"], re.compile(r"\b(manager|mgr)\b", re.I)),
    # Staff/principal/lead are one rung above plain senior. This pattern must
    # precede the senior pattern so a title carrying both reads as the higher.
    (_RANK["staff"], re.compile(r"\b(staff|principal|lead)\b", re.I)),
    (_RANK["senior"], re.compile(r"\b(senior|sr\.?)\b", re.I)),
    (
        _RANK["ic"],
        re.compile(
            r"\b(coordinator|specialist|associate|analyst|representative|"
            r"rep|agent|intern|assistant|clerk|trainee|apprentice)\b",
            re.I,
        ),
    ),
)


def detect_title_rank(title: str) -> int | None:
    """Best-guess seniority rank from a job title, or None if no level token.

    None means "no explicit seniority signal" — the gate treats that as a pass
    so unusual titles are never dropped on a guess.
    """
    for rank, pattern in _TITLE_PATTERNS:
        if pattern.search(title):
            return rank
    return None


def passes_seniority_gate(
    title: str,
    seniority_hint: SeniorityHint | None,
    *,
    tolerance: int = 1,
) -> bool:
    """True if ``title`` is senior enough to be worth a Phase 2 grade.

    Only gates targets whose hint is *director or above* — below that the bar is
    low enough that pre-filtering would mostly cause false drops, so it's a
    pass-through. ``tolerance`` allows roles up to N rungs below the hint (1 by
    default, so a *Manager* still grades for a *Director* target — the stretch
    case worth a look — but a *Coordinator* does not). Ambiguous titles (no
    level token) always pass.
    """
    if seniority_hint is None:
        return True
    hint_rank = _RANK[seniority_hint]
    if hint_rank < _RANK["director"]:
        return True
    title_rank = detect_title_rank(title)
    if title_rank is None:
        return True
    return title_rank >= hint_rank - tolerance


__all__ = ["detect_title_rank", "passes_seniority_gate"]
