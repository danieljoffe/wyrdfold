"""Per-target embedding write path for the pre-scan (#60, Phase 2).

The query side of the relevance spine. Phase 1 (``job_embeddings.py``) caches
one vector per JOB; this caches one vector per TARGET, embedded as a Voyage
*query* (``input_type="query"``) — targets are the thing we search WITH, jobs
are the documents we search OVER, and Voyage's asymmetric embeddings reward
matching the right side to each. Phase 3 (#68) reads both: it admits a job to
the expensive per-target LLM grade iff
``cosine(job_vec, target_vec) >= target.prescan_cosine_threshold``.

The embedded text is ``label`` + ``search_keywords`` (NOT ``description``): #60
validation showed the description hurt separation (it pulls in boilerplate /
domain prose that blurs the role signal), so the validated text is the label
plus the ATS query keywords that define the role.

Mirrors ``job_embeddings.py`` end to end: ``content_hash`` skip (re-embed only
on text change), ``cost_log.record_embedding``, then write the vector onto the
``targets`` row. Fail-soft — the one public coroutine swallows its own errors
and returns a status string so a backfill / future on-change hook never breaks
on a Voyage outage; the target simply stays un-embedded and a later run retries.

Inert until activated: nothing imports this on the request/poll path. The
Phase-2 backfill (``scripts/backfill_target_embeddings.py``) is the only caller,
and it must be run deliberately against the Voyage provider.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any, Literal, cast

from supabase import Client

from app.models.embeddings import EmbeddingModelId
from app.models.targets import JobTarget
from app.services.embeddings.client import EmbeddingsClient
from app.services.llm import cost_log

logger = logging.getLogger(__name__)

TABLE = "targets"

# Cost-log grouping label — sliceable alongside "prescan.job_embed",
# "qualification.tagger", etc. on the spend dashboard.
TARGET_EMBED_PURPOSE = "prescan.target_embed"

DEFAULT_MODEL: EmbeddingModelId = "voyage-3"

UpsertStatus = Literal["embedded", "cache_hit", "skipped_empty", "error"]


def embed_text_for_target(target: JobTarget) -> str:
    """Build the text we embed for a target: label + its search keywords.

    The validated #60 shape — ``label`` plus the ATS query keywords, joined as
    ``"{label}. Related roles and skills: {kw1, kw2, ...}"``. Deliberately omits
    ``target.description``: validation showed the description blurs role
    separation (boilerplate / domain prose), so it is NOT part of the embedded
    text. With no keywords the suffix is dropped and we embed the bare label.
    """
    label = (target.label or "").strip()
    keywords = [kw.strip() for kw in (target.search_keywords or []) if kw and kw.strip()]
    if keywords:
        return f"{label}. Related roles and skills: {', '.join(keywords)}"
    return label


def content_hash(text: str) -> str:
    """Stable sha256 of the embedded text — the re-embed skip key."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _existing_hash(supabase: Client, *, target_id: str) -> str | None:
    """Return the stored ``embedding_text_hash`` for a target, or None."""
    resp = await asyncio.to_thread(
        lambda: (
            supabase.table(TABLE)
            .select("embedding_text_hash")
            .eq("id", target_id)
            .limit(1)
            .execute()
        )
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        return None
    return cast("str | None", rows[0].get("embedding_text_hash"))


async def upsert_target_embedding(
    supabase: Client,
    embeddings_client: EmbeddingsClient,
    target: JobTarget,
    *,
    model: EmbeddingModelId = DEFAULT_MODEL,
) -> UpsertStatus:
    """Embed one target and write its vector onto the ``targets`` row (best-effort).

    Returns a status: ``"cache_hit"`` (the stored ``embedding_text_hash`` already
    matches this text — no embed call), ``"embedded"`` (embedded + written),
    ``"skipped_empty"`` (no label and no keywords — nothing to embed), or
    ``"error"`` (logged and swallowed). Never raises into the caller.

    Embeds with ``input_type="query"`` — the target is the query side of the
    asymmetric job↔target cosine. Cost is logged under ``TARGET_EMBED_PURPOSE``
    with the instance key (``user_id=None``), like the rest of the poller's
    target-INDEPENDENT work.
    """
    try:
        text = embed_text_for_target(target)
        if not text.strip():
            # No label and no keywords — nothing meaningful to embed.
            return "skipped_empty"

        new_hash = content_hash(text)

        if await _existing_hash(supabase, target_id=target.id) == new_hash:
            # Unchanged target text already embedded — skip the spend.
            return "cache_hit"

        result = await embeddings_client.embed(
            model=model,
            inputs=[text],
            purpose=TARGET_EMBED_PURPOSE,
            input_type="query",
        )
        if not result.embeddings:
            # Defensive: a non-empty input should always yield one vector.
            logger.warning("Target embed returned no vector for target %s", target.id)
            return "error"

        cost_log.record_embedding(
            supabase,
            user_id=None,
            purpose=TARGET_EMBED_PURPOSE,
            result=result,
            metadata={"target_id": target.id, "model": model},
        )

        updates: dict[str, Any] = {
            "embedding": result.embeddings[0],
            "embedding_text_hash": new_hash,
        }
        await asyncio.to_thread(
            lambda: supabase.table(TABLE).update(updates).eq("id", target.id).execute()
        )
        return "embedded"
    except Exception:
        logger.exception("Target embedding failed for target %s", target.id)
        return "error"
