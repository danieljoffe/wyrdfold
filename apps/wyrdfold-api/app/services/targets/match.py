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
from typing import Any, cast

from supabase import Client

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
    _parse_target,
    get_user_target_ids,
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


def find_matching_target(supabase: Client, label: str) -> JobTarget | None:
    """Find an existing target matching a label, or None.

    Tries exact match first, then fuzzy via pg_trgm similarity.
    """
    normalized = _normalize_label(label)

    # Exact match
    resp = (
        supabase.table(TARGETS_TABLE)
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
        rpc_resp = supabase.rpc(
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


def _match_suggestions(
    supabase: Client,
    suggestions: list[TargetSuggestion],
    existing_ids: set[str],
) -> list[MatchedSuggestion]:
    """Match each suggestion against the shared catalog, dropping any the user
    already follows.

    A suggestion matched to a target the caller already has is skipped (no
    point re-offering it); everything else is returned paired with its matched
    target (``is_new=False``) or flagged as new (``is_new=True``).
    """
    matches: list[MatchedSuggestion] = []
    for suggestion in suggestions:
        matched = find_matching_target(supabase, suggestion.label)

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
    supabase: Client,
    llm: LLMClient,
    *,
    payload: OptimizedPayload,
    user_id: str,
) -> tuple[MatchedSuggestions, LLMResult]:
    """Suggest targets from experience, match against existing, exclude the
    user's current targets.

    Returns (matched_suggestions, llm_result) so callers can log cost.
    """
    existing_ids = get_user_target_ids(supabase, user_id)
    suggestions, result = await suggest_targets(llm, payload=payload)
    matches = _match_suggestions(supabase, suggestions.suggestions, existing_ids)
    return MatchedSuggestions(matches=matches), result


async def suggest_and_match_from_query(
    supabase: Client,
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
    existing_ids = get_user_target_ids(supabase, user_id)
    suggestions, result = await suggest_targets_from_query(llm, query=query, payload=payload)
    matches = _match_suggestions(supabase, suggestions.suggestions, existing_ids)
    return MatchedSuggestions(matches=matches), result
