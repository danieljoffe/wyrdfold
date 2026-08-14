"""Target matching for the suggestion flow (#553).

When the LLM suggests targets, we match against existing targets before
creating new ones. This avoids duplicates and lets users discover targets
other users have already created.

Matching strategy:
1. Normalize label (lowercase, trim, collapse whitespace)
2. Exact match on normalized_label
3. Fuzzy match via pg_trgm similarity (threshold 0.7)
4. Exclude targets the user already has

Threshold rationale: 0.7 keeps "sr fe eng" → "Senior Frontend Engineer"
matches working while preventing specialization collisions like
"Senior Backend Engineer" → "Senior Frontend Engineer" (~0.59 similarity
because they share "senior" + "engineer" + suffix).
"""

from __future__ import annotations

import logging
import re
from typing import Any, cast

from supabase import AsyncClient

from app.models.experience import OptimizedPayload
from app.models.llm import LLMResult
from app.models.targets import (
    JobTarget,
    MatchedSuggestion,
    MatchedSuggestions,
    TargetSuggestion,
)
from app.services.llm.client import LLMClient
from app.services.targets.crud import (
    TARGETS_TABLE,
    USER_TARGETS_TABLE,
    _parse_target,
    normalize_label,
)
from app.services.targets.suggest import (
    suggest_targets,
    suggest_targets_from_query,
)

logger = logging.getLogger(__name__)

_SIMILARITY_THRESHOLD = 0.7

# The label dedup key is normalized once, in crud, so the write path
# (normalized_label) and this lookup path can never diverge. Kept under the
# original private name for the callers and tests that import it here.
_normalize_label = normalize_label


# ── #57 PR-G2b: this module + its callers (from_input, the targets suggest
# handlers) all run on the pooled async service client now. crud stays SYNC for
# the poller/learner; the two reads this module needs — the shared-catalog
# label lookup and the caller's any-status memberships (crud.get_user_target_ids)
# — are inlined async here (reusing crud._parse_target for shape) rather than
# converting crud. No sync+async twin in the shared layer.


async def _user_target_ids(supabase: AsyncClient, user_id: str) -> set[str]:
    """Async inline of ``crud.get_user_target_ids`` — any-status memberships."""
    resp = (
        await supabase.table(USER_TARGETS_TABLE)
        .select("target_id")
        .eq("user_id", user_id)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return {r["target_id"] for r in rows}


async def _user_target_labels(supabase: AsyncClient, target_ids: set[str]) -> set[str]:
    """Normalized labels of the caller's own targets, for the containment
    check below. Single ``in_`` read — user memberships are tens at most,
    far under the ~150-id URL-length ceiling on ``in_()`` chunks (#57)."""
    if not target_ids:
        return set()
    resp = (
        await supabase.table(TARGETS_TABLE)
        .select("normalized_label")
        .in_("id", sorted(target_ids))
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return {r["normalized_label"] for r in rows if r.get("normalized_label")}


# A containment match requires the shorter label to carry at least this many
# words — single-word labels ("engineer") are generic enough to sit inside
# almost any suggestion, and dropping on those would erase whole categories.
_MIN_CONTAINMENT_WORDS = 2


def _near_duplicate_of_existing(normalized: str, existing_labels: set[str]) -> bool:
    """True when the suggestion contains — or is contained by, at word
    boundaries — a label the user already follows.

    Closes the word-extension gap in the trigram gate (sweep 2026-08-14
    A4): "founding engineer / head of engineering" vs an existing
    "founding engineer" scores below the 0.7 similarity threshold (the
    extra words dilute the trigram overlap) yet is the same target for
    offer purposes. Scoped to the USER'S OWN labels only — catalog-wide
    matching keeps the documented 0.7 rationale untouched.
    """
    for existing in existing_labels:
        shorter, longer = sorted((normalized, existing), key=len)
        if len(shorter.split()) < _MIN_CONTAINMENT_WORDS:
            continue
        # Word-boundary containment: "founding engineer" matches inside
        # "founding engineer / head of engineering" (delimited by space /
        # punctuation) but "end engineer" never matches "backend engineer".
        if re.search(rf"(?<!\w){re.escape(shorter)}(?!\w)", longer):
            return True
    return False


async def find_matching_target(supabase: AsyncClient, label: str) -> JobTarget | None:
    """Find an existing target matching a label, or None.

    Tries exact match first, then fuzzy via pg_trgm similarity.
    """
    normalized = _normalize_label(label)

    # Exact match
    resp = (
        await supabase.table(TARGETS_TABLE)
        .select("*")
        .eq("normalized_label", normalized)
        .limit(1)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    if rows:
        return _parse_target(rows[0])

    # Fuzzy match via pg_trgm (requires the extension enabled in Phase 1)
    try:
        rpc_resp = await supabase.rpc(
            "match_target_by_label",
            {"query_label": normalized, "threshold": _SIMILARITY_THRESHOLD},
        ).execute()
        rpc_rows = cast(list[dict[str, Any]], rpc_resp.data or [])
        if rpc_rows:
            return _parse_target(rpc_rows[0])
    except Exception:
        # RPC not yet created — fall back to exact-only matching
        logger.debug("match_target_by_label RPC not available, using exact match only")

    return None


async def _match_suggestions(
    supabase: AsyncClient,
    suggestions: list[TargetSuggestion],
    existing_ids: set[str],
    existing_labels: set[str],
) -> list[MatchedSuggestion]:
    """Match each suggestion against the shared catalog, dropping any the user
    already follows.

    A suggestion matched to a target the caller already has is skipped (no
    point re-offering it), as is a word-boundary near-duplicate of one of
    their labels (``_near_duplicate_of_existing``); everything else is
    returned paired with its matched target (``is_new=False``) or flagged as
    new (``is_new=True``).
    """
    matches: list[MatchedSuggestion] = []
    for suggestion in suggestions:
        # Containment first: it's an in-memory check, so a near-dup of an
        # already-followed target never costs the catalog lookups below.
        if _near_duplicate_of_existing(_normalize_label(suggestion.label), existing_labels):
            continue

        matched = await find_matching_target(supabase, suggestion.label)

        if matched and matched.id in existing_ids:
            # User already has this target — skip
            continue

        matches.append(
            MatchedSuggestion(
                suggestion=suggestion,
                matched_target=matched,
                is_new=matched is None,
            )
        )
    return matches


async def suggest_and_match(
    supabase: AsyncClient,
    llm: LLMClient,
    *,
    payload: OptimizedPayload,
    user_id: str,
) -> tuple[MatchedSuggestions, LLMResult]:
    """Suggest targets from experience, match against existing, exclude the
    user's current targets.

    Returns (matched_suggestions, llm_result) so callers can log cost.
    """
    existing_ids = await _user_target_ids(supabase, user_id)
    existing_labels = await _user_target_labels(supabase, existing_ids)
    suggestions, result = await suggest_targets(llm, payload=payload)
    matches = await _match_suggestions(
        supabase, suggestions.suggestions, existing_ids, existing_labels
    )
    return MatchedSuggestions(matches=matches), result


async def suggest_and_match_from_query(
    supabase: AsyncClient,
    llm: LLMClient,
    *,
    query: str,
    user_id: str,
    payload: OptimizedPayload | None = None,
) -> tuple[MatchedSuggestions, LLMResult]:
    """Suggest targets from a free-text query, match against the shared catalog,
    exclude the user's current targets.

    Backs the catalog-search LLM fallback: when ``GET /targets/search`` finds
    nothing, the LLM canonicalizes the query into a target plus adjacent roles,
    each matched so the user can follow an existing one or create a new one.
    Experience-tailored when ``payload`` is provided, but works without it.

    Returns (matched_suggestions, llm_result) so callers can log cost.
    """
    existing_ids = await _user_target_ids(supabase, user_id)
    existing_labels = await _user_target_labels(supabase, existing_ids)
    suggestions, result = await suggest_targets_from_query(llm, query=query, payload=payload)
    matches = await _match_suggestions(
        supabase, suggestions.suggestions, existing_ids, existing_labels
    )
    return MatchedSuggestions(matches=matches), result
