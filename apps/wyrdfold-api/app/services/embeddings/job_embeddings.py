"""Per-job embedding write path for the pre-scan (#60, Phase 1).

Embeds a job ONCE (target-INDEPENDENT) and caches the vector in
``job_embeddings`` keyed by (job, model). The relevance spine will later
read these vectors to admit only semantically-close jobs to the expensive
per-target LLM grade — so this is the populate side of a flag-off,
inert pipeline: nothing here runs unless a caller invokes it (the poller
hook is gated behind ``settings.prescan_embed_enabled``, default off).

Mirrors the experience chunk-write path (``experience/chunks.py``): embed,
``cost_log.record_embedding``, then write rows carrying the raw vector. The
content-hash skip mirrors the qualification firewall's ``jobs.qualified_hash``
— a re-poll of an unchanged posting re-derives the same hash and costs nothing.

Fail-soft: every public coroutine swallows its own errors and returns a
status string. An embedding outage (Voyage down, a bad row) must never
break polling — the job simply stays un-embedded and a later cycle retries.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any, Literal, cast

from supabase import Client

from app.models.embeddings import EmbeddingModelId
from app.services.embeddings.client import EmbeddingsClient
from app.services.llm import cost_log
from app.services.qualification.heuristics import clean_description

logger = logging.getLogger(__name__)

TABLE = "job_embeddings"

# Cost-log grouping label — sliceable alongside "qualification.tagger",
# "experience.chunks", etc. on the spend dashboard.
JOB_EMBED_PURPOSE = "prescan.job_embed"

DEFAULT_MODEL: EmbeddingModelId = "voyage-3"

# The validated body window (#60): title + the first 4000 chars of the cleaned
# description. Voyage-3 reads far more, but 4000 chars captured the relevance
# signal in validation while keeping the per-job token cost ~flat — the head of
# a JD (role summary + core responsibilities) carries the discriminating
# content; the tail is boilerplate (benefits, EEO, "about us").
_MAX_DESCRIPTION_CHARS = 4000

UpsertStatus = Literal["embedded", "cache_hit", "skipped_empty", "error"]


def embed_text_for_job(title: str | None, description_html: str | None) -> str:
    """Build the text we embed for a job: title + cleaned description head.

    ``clean_description`` strips HTML/entities and collapses whitespace (the
    same cleaner the qualification firewall uses), so a vendor re-encoding the
    same posting doesn't churn the vector. The description is truncated to
    ``_MAX_DESCRIPTION_CHARS`` AFTER cleaning so the cap is over real text, not
    markup. Returns ``"{title}\\n{description}"`` — either part may be empty.
    """
    clean_title = (title or "").strip()
    clean_body = clean_description(description_html)[:_MAX_DESCRIPTION_CHARS]
    return f"{clean_title}\n{clean_body}"


def content_hash(text: str) -> str:
    """Stable sha256 of the embedded text — the re-embed skip key."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _existing_hash(supabase: Client, *, job_id: str, model: str) -> str | None:
    """Return the stored content_hash for (job, model), or None if no row."""
    resp = await asyncio.to_thread(
        lambda: (
            supabase.table(TABLE)
            .select("content_hash")
            .eq("job_posting_id", job_id)
            .eq("model", model)
            .limit(1)
            .execute()
        )
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        return None
    return cast("str | None", rows[0].get("content_hash"))


async def upsert_job_embedding(
    supabase: Client,
    embeddings_client: EmbeddingsClient,
    *,
    job_id: str,
    title: str | None,
    description_html: str | None,
    model: EmbeddingModelId = DEFAULT_MODEL,
) -> UpsertStatus:
    """Embed one job and upsert its vector into ``job_embeddings`` (best-effort).

    Returns a status: ``"cache_hit"`` (an up-to-date row already exists for
    this content — no embed call), ``"embedded"`` (embedded + written),
    ``"skipped_empty"`` (no title and no description — nothing worth
    embedding), or ``"error"`` (logged and swallowed). Never raises into the
    caller; the poller hook relies on that.
    """
    try:
        text = embed_text_for_job(title, description_html)
        if not text.strip():
            # Neither a title nor a description — nothing meaningful to embed.
            return "skipped_empty"

        new_hash = content_hash(text)

        if await _existing_hash(supabase, job_id=job_id, model=model) == new_hash:
            # Unchanged content already embedded — skip the spend.
            return "cache_hit"

        result = await embeddings_client.embed(
            model=model,
            inputs=[text],
            purpose=JOB_EMBED_PURPOSE,
            input_type="document",
        )
        if not result.embeddings:
            # Defensive: a non-empty input should always yield one vector.
            logger.warning("Job embed returned no vector for job %s", job_id)
            return "error"

        # System-driven spend → instance key (user_id=None), like the rest of
        # the poller's target-INDEPENDENT work.
        cost_log.record_embedding(
            supabase,
            user_id=None,
            purpose=JOB_EMBED_PURPOSE,
            result=result,
            metadata={"job_posting_id": job_id, "model": model},
        )

        row: dict[str, Any] = {
            "job_posting_id": job_id,
            "model": model,
            "content_hash": new_hash,
            "embedding": result.embeddings[0],
        }
        await asyncio.to_thread(
            lambda: supabase.table(TABLE).upsert(row, on_conflict="job_posting_id,model").execute()
        )
        return "embedded"
    except Exception:
        logger.exception("Job embedding failed for job %s", job_id)
        return "error"
