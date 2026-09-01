"""Phase-1 backfill at target activation (#930).

The hole this closes
--------------------
Two mechanisms each look correct alone and leave a gap between them:

1. ``poller`` only ever sends titles whose ``external_id`` is NOT already in
   the catalog to Phase-1 triage. Correct on its own — a known row was
   judged on the cycle that ingested it, and re-judging it is pure spend
   (#514).
2. ``target_scoring.bulk_title_score_for_target`` — the activation fan-out
   that exists "so postings that pre-date the target still appear under it"
   — writes keyword-only ``scoring_status='stage1'`` rows and never passes
   ``promising``. It makes no LLM call at all.

So a listing that entered the catalog before a target existed gets a score
row from (2) and is never a candidate for (1): **it never receives a
Phase-1 verdict, at activation or ever after.** ``promising`` stays NULL
forever. ``_reconcile_offfamily_promising`` can RETRACT a verdict once a
role-family tag lands, but nothing in the system can CREATE one for an
already-catalogued row. A user activates a target expecting its listings to
be judged, and they are not.

What this does
--------------
Grades the target's ungraded rows — ``scores.promising IS NULL`` — with the
same ``triage_titles`` call, the same negative-verdict store, the same
confidence gate and the same cost ledger the poller uses. Three bounds:

**Newest-first.** Pages ``jobs`` by ``cataloged_at DESC``. Whatever a bound
truncates is then the oldest tail, which is the part closest to
disappearing anyway.

**An age bound.** ``phase1_backfill_max_age_days`` (default 30) on
``cataloged_at`` — deliberately the same column AND default as
``archival_archive_after_days``, because archival soft-archives on that
clock. Grading past it buys verdicts for listings that vanish from every
view before anyone reads them. Measured on prod 2026-08-31: ~1,540 of a
target's ~8,200 ungraded rows are inside the window, so the bound removes
~81% of the nominal work for no user-visible loss.

**A shared daily count cap.** Every call here writes the same
``purpose='relevance.title_triage'`` / ``metadata.target_id`` cost row the
poller writes, so backfill and fresh ingestion draw on ONE counter
(``relevance.daily_cap``). The backfill clamps itself to
``phase1_backfill_cap_fraction`` of that cap, so a large activation cannot
consume the day and starve new intake.

What this deliberately is NOT
-----------------------------
A per-user equal-share scheduler. ``scores`` is keyed
``(job_posting_id, target_id)`` with no ``user_id``: a verdict belongs to
the TARGET, not to whoever paid for it, so "equalising" spend across users
would mean deliberately computing the same shared good more than once.
And it is not scarce — re-measured on prod 2026-08-31, a full age-bounded
backfill of ALL five active targets is ~40 batched calls (~8 per target),
because the rejection store collapses repeat titles and 150 titles ride per
call. A quota mechanism would ration something that is not scarce. Revisit
when distinct in-window titles x targets grows materially; re-run the
measurement rather than guessing (see the PR for #930).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from supabase import AsyncClient

from app.config import settings
from app.models.targets import JobTarget
from app.services.llm.client import LLMClient
from app.services.llm.cost_log import record_async as record_llm_cost_async
from app.services.relevance.daily_cap import phase1_backfill_allowance, phase1_cap_reached
from app.services.relevance.rejection_store import (
    fetch_rejected_titles,
    normalize_title,
    record_rejections,
)
from app.services.relevance.title_triage import (
    PHASE1_PURPOSE,
    TitleVerdict,
    admitted,
    phase1_batch_size,
    triage_titles,
)

logger = logging.getLogger(__name__)

SCORES_TABLE = "scores"

# Rows per ``jobs`` page. Same 500 as the re-score / retro-title pagers.
_JOBS_PAGE_SIZE = 500

# Chunk bound for ``.in_()`` reads — ids travel in the URL and PostgREST
# starts 414-ing around 150-200 UUIDs (#57). Same 100 ``target_scoring`` uses.
_IN_CHUNK_SIZE = 100

# Rows per ``scores`` upsert payload (request-body-bound, not URL-bound).
_UPSERT_CHUNK_SIZE = 500


@dataclass(frozen=True)
class Phase1BackfillResult:
    """What one activation backfill did. Returned for logging + tests."""

    #: Ungraded (job, target) pairs the pass actually looked at.
    candidates: int = 0
    #: Verdicts served by the negative store — no LLM call, no cost row.
    store_hits: int = 0
    #: LLM calls issued (each one batch of up to ``phase1_batch_size()``).
    llm_calls: int = 0
    #: ``scores`` rows given a real Phase-1 verdict by this pass.
    verdicts_written: int = 0
    promising: int = 0
    rejected: int = 0
    #: Calls this pass was allowed to make; ``None`` = the cap is disabled.
    allowance: int | None = None
    #: Why the pass stopped early, or ``None`` if it finished the window.
    stopped: str | None = None


def _cutoff_iso(max_age_days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=max_age_days)).isoformat()


def _verdict_row(
    *,
    job_posting_id: str,
    target_id: str,
    promising: bool,
    confidence: int | None,
    was_excluded: bool,
) -> dict[str, Any]:
    """One ``scores`` upsert row carrying a Phase-1 verdict.

    EVERY row this module writes carries the SAME six keys, on purpose. A
    PostgREST bulk upsert writes the union of the batch's keys to every row
    (#928), so a payload where some rows omit ``phase1_confidence`` would
    NULL it on the rows that do carry it. A uniform key set makes that
    impossible rather than merely unlikely — an explicit ``None`` here is a
    truthful "no confidence for this verdict" (a store hit has none), not an
    accidental erasure.

    ``excluded`` is OR-ed, never overwritten: a Phase-1 rejection excludes
    the row (this is ``excluded_by_prefilter`` in the poller's Stage-2 path),
    but a row already excluded by a negative KEYWORD must stay excluded even
    when Phase 1 says promising. Un-excluding on a promising verdict would
    walk the deterministic noise floor back up.
    """
    return {
        "job_posting_id": job_posting_id,
        "target_id": target_id,
        "promising": promising,
        "phase1_confidence": confidence,
        "excluded": bool(was_excluded or not promising),
        "updated_at": datetime.now(UTC).isoformat(),
    }


async def _write_verdicts(supabase: AsyncClient, rows: list[dict[str, Any]]) -> None:
    """Upsert verdict rows in chunks. Best-effort: a failed chunk is logged
    and the pass continues, exactly like the rejection store's write posture —
    a lost verdict is re-derived on the next activation, and raising here
    would fail the whole activation over a partial write."""
    for i in range(0, len(rows), _UPSERT_CHUNK_SIZE):
        chunk = rows[i : i + _UPSERT_CHUNK_SIZE]
        try:
            await (
                supabase.table(SCORES_TABLE)
                .upsert(chunk, on_conflict="job_posting_id,target_id")
                .execute()
            )
        except Exception:
            logger.warning(
                "phase1 backfill: verdict write failed for %d row(s) — "
                "they stay ungraded and re-enter the next backfill",
                len(chunk),
                exc_info=True,
            )


async def _ungraded_page(
    supabase: AsyncClient, target: JobTarget, *, cutoff: str, offset: int
) -> tuple[list[tuple[str, str, bool]], bool]:
    """One newest-first page of ``(job_id, title, already_excluded)`` for rows
    this target holds a score row for but no Phase-1 verdict.

    Returns ``(candidates, more_pages)``.

    OFFSET, not keyset — and that is safe HERE specifically because of the
    sort direction. Rows are ordered ``cataloged_at DESC``, so a listing
    ingested mid-pass lands at the FRONT of the ordering and pushes the
    window DOWN: the worst a concurrent insert can do is make a later page
    re-visit a row this pass already graded, which is idempotent. The
    opposite hazard (a row shifting UP and being skipped) needs a row to
    LEAVE the window, which means either a purge — hard-deletes only touch
    long-archived rows, excluded by the filter — or an archival stamp, which
    only fires past ``archival_archive_after_days``, i.e. outside this
    window by construction. (``bulk_title_score_for_target`` pages by
    ``id ASC`` where that argument does not hold, so it must keyset; this
    one must sort by date and cannot.)
    """
    resp = await (
        supabase.table("jobs")
        .select("id, title")
        .is_("archived_at", "null")
        .gte("cataloged_at", cutoff)
        .order("cataloged_at", desc=True)
        .order("id", desc=True)
        .range(offset, offset + _JOBS_PAGE_SIZE - 1)
        .execute()
    )
    job_rows = cast(list[dict[str, Any]], resp.data or [])
    more = len(job_rows) >= _JOBS_PAGE_SIZE
    if not job_rows:
        return [], more

    title_by_id: dict[str, str] = {r["id"]: r.get("title") or "" for r in job_rows}
    ids = list(title_by_id.keys())

    excluded_by_id: dict[str, bool] = {}
    for i in range(0, len(ids), _IN_CHUNK_SIZE):
        chunk = ids[i : i + _IN_CHUNK_SIZE]
        s_resp = await (
            supabase.table(SCORES_TABLE)
            .select("job_posting_id, excluded")
            .eq("target_id", target.id)
            .is_("promising", "null")
            .in_("job_posting_id", chunk)
            .execute()
        )
        for row in cast(list[dict[str, Any]], s_resp.data or []):
            excluded_by_id[cast(str, row["job_posting_id"])] = bool(row.get("excluded"))

    # Preserve the newest-first page order; only rows with a title are
    # gradable (Phase 1 grades titles).
    return (
        [
            (jid, title_by_id[jid], excluded_by_id[jid])
            for jid in ids
            if jid in excluded_by_id and title_by_id[jid]
        ],
        more,
    )


async def backfill_phase1_for_target(
    supabase: AsyncClient,
    llm: LLMClient | None,
    target: JobTarget,
    *,
    payer_user_id: str | None,
    budget_blocks: Callable[[], Awaitable[bool]] | None = None,
) -> Phase1BackfillResult:
    """Give this target's ungraded, in-window score rows a Phase-1 verdict.

    Runs at target activation, after ``bulk_title_score_for_target`` has
    written the keyword ``stage1`` rows this pass then judges. Requires BOTH
    ``phase1_triage_enabled`` (the gate itself) and
    ``phase1_backfill_enabled`` (this pass) — writing ``promising`` while
    the gate is off would arm a filter the deployment has switched off.

    ``budget_blocks`` is the injected global daily-spend meter. The poller
    owns that meter (``_global_budget_exhausted``) and this module cannot
    import the poller without a cycle, so the caller passes it bound — the
    same shape ``ensure_job_tags`` uses. ``None`` means unmetered (tests).
    """
    if not (settings.phase1_triage_enabled and settings.phase1_backfill_enabled):
        return Phase1BackfillResult(stopped="disabled")
    if llm is None:
        # No client for the payer (no BYOK key / expired trial). Same defer
        # the poller takes: nothing is written, everything re-enters the next
        # activation. Never a fail-open write of unjudged verdicts.
        return Phase1BackfillResult(stopped="no_llm_client")

    allowance = await phase1_backfill_allowance(supabase, target.id)
    if allowance is not None and allowance <= 0:
        logger.info(
            "phase1 backfill: target %s has no Phase-1 call allowance left today "
            "(cap %d, backfill share %.2f)",
            target.id,
            settings.phase1_daily_cap,
            settings.phase1_backfill_cap_fraction,
        )
        return Phase1BackfillResult(allowance=0, stopped="allowance")

    cutoff = _cutoff_iso(settings.phase1_backfill_max_age_days)
    batch_cap = phase1_batch_size()

    candidates = store_hits = llm_calls = written = promising_n = rejected_n = 0
    stopped: str | None = None
    offset = 0

    while stopped is None:
        page, more = await _ungraded_page(supabase, target, cutoff=cutoff, offset=offset)
        offset += _JOBS_PAGE_SIZE
        if page:
            candidates += len(page)

            # Negative-verdict store first (#514): a title this target's LLM
            # already rejected inside the TTL is re-served as a synthetic
            # promising=False WITHOUT an LLM call. This is the single biggest
            # reason the backfill is cheap, and it is checked before anything
            # is batched so a repeat title never reaches the model.
            cached = await fetch_rejected_titles(supabase, target, [t for _, t, _ in page])
            hits = [c for c in page if normalize_title(c[1]) in cached]
            todo = [c for c in page if normalize_title(c[1]) not in cached]
            if hits:
                store_hits += len(hits)
                await _write_verdicts(
                    supabase,
                    [
                        _verdict_row(
                            job_posting_id=jid,
                            target_id=target.id,
                            promising=False,
                            confidence=None,
                            was_excluded=was_excluded,
                        )
                        for jid, _title, was_excluded in hits
                    ],
                )
                written += len(hits)
                rejected_n += len(hits)

            for start in range(0, len(todo), batch_cap):
                if allowance is not None and llm_calls >= allowance:
                    stopped = "allowance"
                    break
                if await phase1_cap_reached(supabase, target.id):
                    stopped = "daily_cap"
                    break
                if budget_blocks is not None and await budget_blocks():
                    stopped = "global_budget"
                    break

                batch = todo[start : start + batch_cap]
                verdicts, result = await triage_titles(
                    llm, target=target, titles=[t for _, t, _ in batch]
                )
                if result is None:
                    # A FAILED call (dead key, spent limit, provider error).
                    # These titles stay un-attempted and keep NULL: they DEFER
                    # to a later activation rather than fail-open into
                    # unjudged admits. A dead key does not heal mid-pass, so
                    # stop rather than re-pay input tokens for every remaining
                    # batch.
                    stopped = "llm_unavailable"
                    break

                llm_calls += 1
                await _record_cost(supabase, target, payer_user_id, result, len(batch))

                rows: list[dict[str, Any]] = []
                rejections: list[tuple[str, int | None]] = []
                for pos, (jid, title, was_excluded) in enumerate(batch, start=1):
                    verdict: TitleVerdict | None = verdicts.get(pos)
                    # Same admission rule as the poller: the confidence gate
                    # applies (#47), and a DROPPED verdict (the model omitted
                    # the id, or the title_prefix cross-check rejected it)
                    # fail-opens to promising — false positives are cheap,
                    # false negatives are lost forever.
                    is_promising = admitted(
                        verdict, min_confidence=settings.phase1_min_confidence
                    )
                    rows.append(
                        _verdict_row(
                            job_posting_id=jid,
                            target_id=target.id,
                            promising=is_promising,
                            confidence=verdict.confidence if verdict is not None else None,
                            was_excluded=was_excluded,
                        )
                    )
                    if is_promising:
                        promising_n += 1
                    else:
                        rejected_n += 1
                    # Only a RAW "no" is cached. A promising verdict the
                    # confidence gate dropped is not a rejection of the title
                    # — the threshold is a live setting.
                    if verdict is not None and not verdict.promising:
                        rejections.append((title, verdict.confidence))

                await _write_verdicts(supabase, rows)
                written += len(rows)
                if rejections:
                    await record_rejections(supabase, target, rejections)

        if stopped is None and not more:
            break

    logger.info(
        "phase1 backfill for target %s: %d candidate(s), %d store hit(s), "
        "%d LLM call(s), %d verdict(s) written (%d promising / %d rejected), "
        "allowance=%s, stopped=%s",
        target.id,
        candidates,
        store_hits,
        llm_calls,
        written,
        promising_n,
        rejected_n,
        allowance,
        stopped,
    )
    return Phase1BackfillResult(
        candidates=candidates,
        store_hits=store_hits,
        llm_calls=llm_calls,
        verdicts_written=written,
        promising=promising_n,
        rejected=rejected_n,
        allowance=allowance,
        stopped=stopped,
    )


async def _record_cost(
    supabase: AsyncClient,
    target: JobTarget,
    payer_user_id: str | None,
    result: Any,
    batch_size: int,
) -> None:
    """Write the cost row that makes this call count against the SHARED daily
    cap. Best-effort on failure — but note the consequence, because it is not
    symmetric with the poller's: a lost row here also means the call did not
    count, so the cap read is an under-count until the day rolls over. The
    backfill's own in-process ``llm_calls`` counter still bounds this pass;
    only the cross-pass view is affected."""
    try:
        await record_llm_cost_async(
            supabase,
            user_id=payer_user_id,
            purpose=PHASE1_PURPOSE,
            result=result,
            metadata={
                "target_id": target.id,
                "trigger": "activation_backfill",
                "batch_size": batch_size,
            },
        )
    except Exception:
        logger.exception(
            "phase1 backfill: failed to record cost for target %s "
            "(this call will not count against the daily cap)",
            target.id,
        )
