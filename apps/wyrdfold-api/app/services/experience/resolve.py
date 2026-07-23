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

import asyncio

from supabase import Client

from app.models.experience import OptimizedPayload
from app.services.experience import derive, optimized, prose
from app.services.llm import cost_log
from app.services.llm.client import LLMClient


async def resolve_current_payload(
    supabase: Client,
    llm: LLMClient,
    *,
    cost_supabase: Client,
    user_id: str | None,
) -> OptimizedPayload | None:
    """An ``OptimizedPayload`` fresh vs. the user's latest master document.

    Returns ``None`` when there is nothing to score against (no prose and no
    optimized doc). ``cost_supabase`` is the service-role client for the cost
    ledger (``llm_costs`` has no ``authenticated`` INSERT policy); it may be the
    same client as ``supabase`` in service-role contexts.
    """
    prose_doc = await asyncio.to_thread(prose.get_latest, supabase, user_id)
    latest = await asyncio.to_thread(optimized.get_latest, supabase, user_id)

    # Nothing to derive from → fall back to whatever optimized doc exists.
    if prose_doc is None:
        return latest.payload if latest is not None else None

    # The common path: the derived doc already reflects the current master
    # document. Cheap cache read, no LLM.
    if latest is not None and latest.prose_doc_id == prose_doc.id:
        return latest.payload

    # Stale (prose advanced past the derived doc) or never derived → freshen
    # transiently from the current prose so scoring reflects the live profile.
    payload, result = await derive.derive_from_prose(llm, prose_text=prose_doc.content)
    await asyncio.to_thread(
        cost_log.record,
        cost_supabase,
        user_id=user_id,
        purpose=derive.DEFAULT_PURPOSE,
        result=result,
        metadata={"reason": "fit_payload_refresh", "prose_doc_id": prose_doc.id},
    )
    return payload
