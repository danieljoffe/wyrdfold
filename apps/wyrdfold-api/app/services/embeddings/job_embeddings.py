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

# voyage-3 was retired by Voyage (2026-07, rolling InvalidRequestError) —
# voyage-3.5 is the drop-in successor: same 1024-dim default (matches the
# vector(1024) columns) and the same $0.06/M price. NB the two vector spaces
# are NOT comparable: never cosine a voyage-3 job vector against a voyage-3.5
# target vector — the model-keyed reads below + the model-aware backfill
# anti-join keep the spaces separate, and thresholds must be re-derived in
# 3.5 space (see #21).
DEFAULT_MODEL: EmbeddingModelId = "voyage-3.5"

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


# Batched-path chunk sizes. The Voyage client sub-batches API calls at 128
# internally; 96 keeps one call per batch with headroom. Vector writes go in
# 8-row statements (~160KB of 1024-dim vectors): 24-row (~500KB) chunks still
# hit the statement timeout on the small prod instance under load — the
# 2026-07-12 audit found ~80% of sweep writes failing 57014 and the same jobs
# being re-embedded every cycle.
_BATCH_EMBED_SIZE = 96
_BATCH_WRITE_CHUNK = 8

# Circuit breaker: consecutive failed write chunks before the WHOLE run
# aborts. Chunk failures here mean the DB is IO-throttled — grinding on
# (and worse, sending MORE Voyage batches whose writes will also fail) is
# exactly the hammer-loop the incident memory says to break: abort on the
# signal, let the next cycle retry when the disk has recovered.
_WRITE_BREAKER_LIMIT = 2


async def embed_jobs_batch(
    supabase: Client,
    embeddings_client: EmbeddingsClient,
    rows: list[dict[str, Any]],
    *,
    model: EmbeddingModelId = DEFAULT_MODEL,
) -> dict[str, int]:
    """Embed many KNOWN-VECTORLESS jobs with batched IO (best-effort).

    The per-job path above costs ~4 round trips per job (hash probe, embed
    call, cost row, single-row upsert) — fine for the on-ingest trickle,
    ruinous for the sweep (200 jobs/cycle ≈ 800 operations; the 2026-07-10
    disk-budget incident). This path assumes the caller ALREADY knows the
    rows have no current-model vector (the sweep's anti-join guarantees it),
    so it skips the hash probes and does one Voyage call per
    ``_BATCH_EMBED_SIZE`` texts, one cost row per call, and chunked bulk
    upserts.

    Write failures trip a circuit breaker: after ``_WRITE_BREAKER_LIMIT``
    consecutive failed chunks the ENTIRE run aborts — no further writes and,
    critically, no further Voyage calls. Without it, a throttled disk turns
    the sweep into a paid retry loop: embeds succeed (spend), writes fail,
    the anti-join re-selects the same jobs next cycle, repeat (observed
    2026-07-12: 3,550 embeds bought for ~680 landed vectors).

    Returns status counts (``embedded`` / ``skipped_empty`` / ``error`` /
    ``aborted`` — rows never attempted after the breaker tripped). Never
    raises.
    """
    counts = {"embedded": 0, "skipped_empty": 0, "error": 0, "aborted": 0}
    consecutive_write_failures = 0
    for i in range(0, len(rows), _BATCH_EMBED_SIZE):
        batch = rows[i : i + _BATCH_EMBED_SIZE]
        texts: list[tuple[dict[str, Any], str]] = []
        for row in batch:
            text = embed_text_for_job(row.get("title"), row.get("description_html"))
            if text.strip():
                texts.append((row, text))
            else:
                counts["skipped_empty"] += 1
        if not texts:
            continue
        try:
            result = await embeddings_client.embed(
                model=model,
                inputs=[t for _, t in texts],
                purpose=JOB_EMBED_PURPOSE,
                input_type="document",
            )
            cost_log.record_embedding(
                supabase,
                user_id=None,
                purpose=JOB_EMBED_PURPOSE,
                result=result,
                metadata={"batched": len(texts), "model": model},
            )
            to_write = [
                {
                    "job_posting_id": row["id"],
                    "model": model,
                    "content_hash": content_hash(text),
                    "embedding": vec,
                }
                for (row, text), vec in zip(texts, result.embeddings, strict=True)
            ]

            def _write(c: list[dict[str, Any]]) -> Any:
                return (
                    supabase.table(TABLE)
                    .upsert(c, on_conflict="job_posting_id,model")
                    .execute()
                )

            for w in range(0, len(to_write), _BATCH_WRITE_CHUNK):
                chunk = to_write[w : w + _BATCH_WRITE_CHUNK]
                try:
                    await asyncio.to_thread(_write, chunk)
                    counts["embedded"] += len(chunk)
                    consecutive_write_failures = 0
                except Exception:
                    consecutive_write_failures += 1
                    logger.exception(
                        "Batched embed write failed for %d row(s) "
                        "(consecutive failure %d/%d)",
                        len(chunk),
                        consecutive_write_failures,
                        _WRITE_BREAKER_LIMIT,
                    )
                    counts["error"] += len(chunk)
                    if consecutive_write_failures >= _WRITE_BREAKER_LIMIT:
                        remaining = len(to_write) - (w + len(chunk))
                        counts["aborted"] = remaining + max(
                            0, len(rows) - (i + len(batch))
                        )
                        logger.warning(
                            "Batched embed: write circuit breaker tripped "
                            "(%d consecutive chunk failures) — aborting the "
                            "run, %d row(s) deferred to the next cycle. The "
                            "DB is IO-throttled; retrying now would re-buy "
                            "embeds whose writes keep failing.",
                            consecutive_write_failures,
                            counts["aborted"],
                        )
                        return counts
        except Exception:
            logger.exception("Batched embed failed for %d job(s)", len(texts))
            counts["error"] += len(texts)
    return counts
