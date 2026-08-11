"""Resolve a CURRENT experience payload — one that reflects the latest master
document — for compute-side consumers (fit scoring).

The persisted optimized doc is a cached projection of the prose master document.
Editing or consolidating the master doc advances the *prose* WITHOUT regenerating
the optimized doc — that only happens on an explicit ``/experience/derive`` (the
autosave and Consolidate endpoints call ``prose.create_version`` only). So a bare
``optimized.get_latest()`` can silently trail the current master document, and
anything scored against it (``derive_fit_score``) is computed against stale
experience. That is the stale-payload seam (2026-07-21 hardening follow-up,
BUG 2): a target created right after a profile edit fit-scores against the OLD
profile, and the user sees their edits not affect scoring.

``resolve_current_payload`` closes it: it returns the persisted payload when that
payload already points at the latest prose version (the common path — a cache
read, no LLM), and otherwise derives a fresh payload from the current prose.

It deliberately does NOT persist a new optimized version. Persisting would also
have to re-embed the doc's search chunks to stay consistent with the ``/derive``
short-circuit (``prose_doc_id`` match ⇒ "complete"); leaving a chunk-less version
behind would durably suppress that repair. Version/chunk lifecycle stays owned by
the ``/derive`` endpoint. This is a transient, read-only freshening for scoring.
"""

from __future__ import annotations

from typing import Any, cast

from supabase import AsyncClient

from app.constants import resolve_owner
from app.models.experience import OptimizedDoc, OptimizedPayload, ProseDoc
from app.services.experience import derive, optimized, prose
from app.services.llm import cost_log
from app.services.llm.client import LLMClient


async def _prose_latest(supabase: AsyncClient, user_id: str | None) -> ProseDoc | None:
    """Async inline of ``prose.get_latest`` (#57 PR-G2e-5). The sync twin stays for
    the poller/tailor/orchestrator callers; this module now rides the pooled async
    service client (its callers — link/fit-score/lazy-refresh — are all async)."""
    resp = await (
        supabase.table(prose.TABLE)
        .select("*")
        .order("version", desc=True)
        .limit(1)
        .eq("user_id", resolve_owner(user_id))
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return ProseDoc.model_validate(rows[0]) if rows else None


async def _optimized_latest(supabase: AsyncClient, user_id: str | None) -> OptimizedDoc | None:
    """Async inline of ``optimized.get_latest`` (#57 PR-G2e-5). Reads fresh — the
    module TTL cache is populated only by the sync path and every write invalidates
    it, so a fresh read can't return stale (mirrors ``targets._optimized_latest``)."""
    resp = await (
        supabase.table(optimized.TABLE)
        .select("*")
        .order("version", desc=True)
        .limit(1)
        .eq("user_id", resolve_owner(user_id))
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return OptimizedDoc.model_validate(rows[0]) if rows else None


async def resolve_current_payload(
    supabase: AsyncClient,
    llm: LLMClient,
    *,
    cost_supabase: AsyncClient,
    user_id: str | None,
) -> tuple[OptimizedPayload | None, str | None]:
    """An ``OptimizedPayload`` fresh vs. the user's latest master document, paired
    with the id of the prose master doc it reflects.

    The prose doc id is the *version marker* a caller stamps onto a cached score
    (``user_targets.fit_score_prose_doc_id``, E2) so a later profile edit makes
    the score detectably stale. It is ``None`` only when there is no prose master
    (the fallback-to-optimized path) — an unversioned score that stays stale.

    Returns ``(None, None)`` when there is nothing to score against (no prose and
    no optimized doc). ``cost_supabase`` is the service-role client for the cost
    ledger (``llm_costs`` has no ``authenticated`` INSERT policy); it may be the
    same client as ``supabase`` in service-role contexts.
    """
    prose_doc = await _prose_latest(supabase, user_id)
    latest = await _optimized_latest(supabase, user_id)

    # Nothing to derive from → fall back to whatever optimized doc exists. No
    # prose master ⇒ no version marker.
    if prose_doc is None:
        return (latest.payload if latest is not None else None), None

    # The common path: the derived doc already reflects the current master
    # document. Cheap cache read, no LLM.
    if latest is not None and latest.prose_doc_id == prose_doc.id:
        return latest.payload, prose_doc.id

    # Stale (prose advanced past the derived doc) or never derived → freshen
    # transiently from the current prose so scoring reflects the live profile.
    payload, result = await derive.derive_from_prose(llm, prose_text=prose_doc.content)
    await cost_log.record_async(
        cost_supabase,
        user_id=user_id,
        purpose=derive.DEFAULT_PURPOSE,
        result=result,
        metadata={"reason": "fit_payload_refresh", "prose_doc_id": prose_doc.id},
    )
    return payload, prose_doc.id
