"""Lazy (on-demand) qualification tagging — the #60 tagger's materializer.

``ensure_job_tags`` is to qualification tags what ``ensure_job_vectors`` is to
job embeddings (Disk IO slim-down, 2026-07-30): the tags for a listing are
bought at the moment something is about to READ them, not for every job the
poller ingests. Ingest is $0 LLM; the only consumer of these tags is grade
time (the Phase-2 runner's US + family gates and the per-target ordering), so
that is where they materialize — for exactly the rows about to be graded.

Stranding is structurally impossible, the same way it is for vectors: a job is
tagged exactly when first needed, and the content hash makes retries free. A
job nobody ever grades simply stays NULL, which every read gate treats
permissively (``is_us IS NOT FALSE``, keep-null family) — the pre-existing
"not yet tagged" state, now the normal one.

The bodies below MOVED out of ``app/services/poller.py`` unchanged (they were
``_qualify_one_job`` / ``_qualify_jobs`` / ``_qualify_rows_with_budget``). They
live here rather than being imported from the poller because the poller imports
``app.services.fit`` — so ``fit.phase2_runner`` importing the poller would close
an import cycle. The one thing that did NOT move is the global-spend predicate
``poller._global_budget_exhausted``: the triage loops and the cycle circuit
breaker still own it, so callers INJECT it as ``budget_exhausted`` instead
(one implementation of the meter, no parallel budget logic).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, cast

from supabase import AsyncClient

from app.config import settings
from app.services.db_write import poll_db_read, poll_db_write
from app.services.llm import get_client_async as get_llm_client_async
from app.services.llm.client import LLMClient
from app.services.llm.cost_log import enqueue as enqueue_llm_cost
from app.services.llm.errors import (
    LLMQuotaExhaustedError,
    LLMRateLimitedError,
    LLMServiceError,
)
from app.services.llm.provider_breaker import (
    provider_fatal_active as _provider_fatal_active,
)
from app.services.llm.provider_breaker import (
    trip_provider_fatal as _trip_provider_fatal,
)
from app.services.qualification.family_gate import passes_family_gate
from app.services.qualification.heuristics import positively_us_location, qualification_hash
from app.services.qualification.skill_dictionary import extract_skills as extract_dictionary_skills
from app.services.qualification.tagger import QUALIFICATION_PURPOSE, tag_job
from app.supabase_pool import get_async_supabase

logger = logging.getLogger(__name__)

# An async "should I stop spending?" predicate the caller injects (the poller
# passes its ``_global_budget_exhausted`` bound to the tagger's reserve). None
# means "no self-imposed-cap check at this call site" — see ``ensure_job_tags``.
BudgetCheck = Callable[[], Awaitable[bool]]

# Cycle-wide cap on the tagger's LLM fan-out. ``ensure_job_tags`` gathers over a
# whole candidate set and several targets/sources can be in flight at once, so
# without a *shared* bound a cycle could open hundreds of simultaneous
# OpenRouter calls (429s + cost bursts). One semaphore per event loop, keyed by
# the running loop like ``db_write`` so a fresh test/worker loop gets its own.
QUALIFY_LLM_CONCURRENCY = 12
_qualify_llm_sems: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}

# How many tagger jobs fan out between budget re-reads (#60 overspend fix). The
# tagger bills the instance key, so it is invisible to the per-payer
# ``PayerBudgetGate``; left ungated it ground the whole backlog past
# ``global_llm_daily_budget_usd`` (the June incident). The loop re-checks the
# injected predicate before each chunk and stops once it says stop, so
# worst-case overshoot is bounded to ONE chunk rather than the whole set.
QUALIFICATION_BUDGET_RECHECK_EVERY = 50

# in_() id-chunk bound for the tag-refresh / reconcile reads (#57 lesson:
# ≤150-200 UUIDs keeps the PostgREST URL under proxy limits).
_IN_CHUNK = 150


def _service_client() -> AsyncClient:
    """Pooled SERVICE-role async client, used ONLY to resolve the instance LLM
    client (moved verbatim from ``poller._async_service_client``).

    Deliberately not the ``supabase`` argument: that can be a user-scoped
    client on the target-activation path, and the instance-key lookup is a
    cross-user read that must bypass RLS. The DB writes below keep using the
    caller's client, exactly as they did in the poller.
    """
    return cast(AsyncClient, get_async_supabase())


def _qualify_llm_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _qualify_llm_sems.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(QUALIFY_LLM_CONCURRENCY)
        _qualify_llm_sems[loop] = sem
    return sem


async def _qualify_one_job(
    llm: LLMClient,
    supabase: AsyncClient,
    row: dict[str, Any],
) -> None:
    """Tag ONE job row and persist its qualification columns (#60).

    Content-hash cached: skips the LLM call when the row's current
    ``qualified_hash`` already matches the freshly-computed hash over
    (title, company, location, description) — so a re-read of an unchanged
    posting costs nothing. Fully best-effort: any error is logged
    and swallowed so the row simply stays NULL (not-yet-tagged) and a later
    read re-attempts it.

    Archived rows never reach the model — see the guard below.
    """
    # ARCHIVED ⇒ SPEND NOTHING. A board that keeps listing a posting we
    # already archived re-upserts it on every cycle; its description drifts,
    # the content-hash below MISSES, and we re-paid for a tag no serving
    # surface can read (measured on prod: roughly a third of all tagger calls
    # were rows archived before the tag was bought — some archived weeks
    # earlier and still re-tagged today). The hash cache was the ONLY skip, and
    # drift defeats it by construction.
    #
    # ``row`` carries ``archived_at`` (it is either an upsert RESULT or a
    # grade-time candidate read) — exactly why the hash skip below can read
    # ``qualified_hash``/``qualified_at`` off the same dict. Placed BEFORE the
    # hash computation so an archived row costs neither the model call nor the
    # hash work, and it sits at the shared per-row chokepoint so EVERY caller
    # inherits it.
    #
    # Not a one-way door. Skipping leaves the row's existing tags AND its stale
    # ``qualified_hash`` untouched, so if ``archived_at`` is ever cleared the
    # very next read misses the content hash and re-tags normally — no
    # backfill, no stranded row. (Nothing in the poller un-archives today:
    # archival is sticky by design, so in practice this guard means "archived
    # rows are done costing us". The clean re-entry path is for an operator
    # revival.)
    if row.get("archived_at"):
        return

    new_hash = qualification_hash(
        title=row.get("title"),
        company=row.get("company_name"),
        location=row.get("location"),
        description=row.get("description_html"),
    )
    if row.get("qualified_hash") == new_hash and row.get("qualified_at"):
        # Unchanged content already tagged — skip the spend.
        return

    # Fast-fail: once the provider has rejected a call this cooldown (402/429),
    # skip the round-trip entirely — it would just fail too. (audit PERF-M)
    if _provider_fatal_active():
        return

    # PERF-H2: bound the qualify LLM fan-out cycle-wide (see _qualify_llm_semaphore).
    try:
        async with _qualify_llm_semaphore():
            # Re-check under the semaphore: the breaker may have latched while
            # we waited for a slot behind an earlier row's 402 — don't fire the
            # doomed call just because we passed the check before queuing.
            if _provider_fatal_active():
                return
            tags, result = await tag_job(
                llm,
                title=row.get("title", ""),
                company=row.get("company_name"),
                location=row.get("location"),
                description=row.get("description_html"),
            )
    except (LLMQuotaExhaustedError, LLMRateLimitedError) as exc:
        # Provider-fatal (402 out-of-credits / sustained 429): latch the breaker
        # so the rest of the cycle stops hammering it. Leave THIS row NULL
        # (best-effort — it re-tags once the cooldown clears). (audit PERF-M)
        _trip_provider_fatal(exc)
        return
    except LLMServiceError:
        # Other provider error (auth/upstream) — transient / config, handled
        # elsewhere; leave the row NULL like a row-level tagger failure.
        return
    if tags is None:
        # Tagger failed on a row-specific error (logged inside tag_job). NULL.
        return

    if result is not None:
        # System-driven spend → async buffered cost-log path, like the rest
        # of the poller's background LLM work.
        with contextlib.suppress(Exception):
            enqueue_llm_cost(None, QUALIFICATION_PURPOSE, result)

    # Free, deterministic, and independent of the LLM verdict above — see
    # ``skill_dictionary``. Scans the FULL description because local regex
    # costs nothing; the LLM path needed a truncated window only because
    # reading was billed per token.
    dict_skills = extract_dictionary_skills(row.get("title"), row.get("description_html"))

    # DEFER TO THE BOARD (#846). ``row`` already carries whatever
    # ``board_columns`` wrote at ingest — Ashby's ``isRemote``, Lever's
    # ``workplaceType``, SmartRecruiters' ``location.remote``, or a board-stated
    # "Remote" in the location string. Those are the employer's own answer; the
    # tagger's are inferences from JD prose. Writing the inference on top is how
    # #795 ended up with 229 prod contradictions, and it silently nullified
    # #847/#848 — the board value was written and then overwritten within the
    # same poll.
    #
    # So these two keys are omitted when the row already holds a value. Every
    # other tag below is a fact NO board publishes (role_family, seniority,
    # is_us, metro, is_genuine_role), so the tagger remains the only source and
    # keeps writing them unconditionally. Board values re-derive on every poll,
    # so a board that changes its mind still propagates.
    payload: dict[str, Any] = {
        "is_us": tags.is_us,
        "role_family": tags.role_family,
        "seniority": tags.seniority,
        "metro": tags.metro,
        "is_genuine_role": tags.is_genuine_role,
        **({} if row.get("employment_type") else {"employment_type": tags.employment_type}),
        **({} if row.get("is_remote") is not None else {"is_remote": tags.is_remote}),
        "qualified_at": datetime.now(UTC).isoformat(),
        "qualified_hash": new_hash,
        # Catalog-wide skill facts (backs /search?skill=react), extracted by
        # DICTIONARY — no LLM, no per-job cost, so it rides this write for free
        # rather than buying a second read of text we already store. Written
        # only when non-empty so a posting that names nothing recognizable
        # never blanks a value the Phase-2 harvest's LLM read already found.
        **({"skills_required": dict_skills} if dict_skills else {}),
    }
    # US-only corpus (#60 workstream B): a high-confidence non-US verdict
    # archives the job in the SAME write. The poller's ingest gate already
    # drops clearly-non-US locations, but its L1 heuristic is permissive
    # (ambiguous / bare-foreign-city rows slip through) — the L2 tagger catches
    # them here, so we close the loop to the ``archived_at`` gate instead of
    # leaving non-US jobs live in a US-only catalog. Conf-gated + reversible;
    # off by default so a global-catalog self-host is unaffected. The
    # ``positively_us_location`` veto hedges a high-confidence tagger
    # FALSE-negative on an unambiguously-US location (a real "New York, NY,
    # United States" was seen tagged non-US at conf 95): never archive when the
    # location plainly says US.
    if (
        settings.qualification_archive_non_us
        and tags.is_us is False
        and tags.us_confidence is not None
        and tags.us_confidence >= settings.qualification_non_us_archive_min_confidence
        and not positively_us_location(row.get("location"))
    ):
        payload["archived_at"] = datetime.now(UTC).isoformat()
    # Non-postings (#60 wire-up): an explicit ``is_genuine_role=false``
    # verdict ("join our talent community", evergreen collectors) archives in
    # the same write — these aren't jobs, so they leave every serving surface
    # via the standard liveness gate. Lenient: ``None`` never archives.
    if settings.qualification_archive_non_genuine and tags.is_genuine_role is False:
        payload["archived_at"] = datetime.now(UTC).isoformat()
    try:
        await poll_db_write(
            supabase,
            lambda c: c.table("jobs").update(payload).eq("id", row["id"]),
            label="qualification tags update",
        )
    except Exception:
        logger.exception("Qualification tag write failed for job %s", row.get("id"))


async def ensure_job_tags(
    supabase: AsyncClient,
    rows: list[dict[str, Any]],
    *,
    budget_exhausted: BudgetCheck | None = None,
) -> None:
    """Materialize the #60 qualification tags for exactly ``rows`` (best-effort).

    The lazy replacement for tag-on-ingest: a listing is tagged when something
    is about to CONSUME the tags (grade time), not when it is ingested. Rows
    that already carry a matching ``qualified_hash`` cost nothing, so calling
    this on a set that is mostly already tagged is cheap.

    **Patches the fresh tag values back into the caller's row dicts in place**
    (``_refresh_job_tags``), so the gates that run immediately after this call
    see this pass's verdicts rather than the pre-tag snapshot. Callers rely on
    that: the Phase-2 runner re-applies its US and family gates to the tagged
    set before spending a grade.

    Target-INDEPENDENT, so it bills the instance key (``get_client_async(..,
    None)``) — never a per-target payer. The whole step is wrapped so a tagger
    or client-resolution failure can never break the caller.

    Budget-gated through the INJECTED ``budget_exhausted`` predicate: the tagger
    bills the instance key, so the per-payer ``PayerBudgetGate`` that protects
    Phase-1/2 work can't see it. Left ungated it ground the backlog clean past
    ``global_llm_daily_budget_usd``. The predicate is re-checked before each
    chunk and the run stops the moment it says stop — bounding overshoot to one
    chunk instead of the whole set. A predicate that RAISES fails CLOSED (refuse
    to spend when we can't see the budget). ``None`` means the call site does
    its own gating (it is the poller that owns the meter — see the module
    docstring); untagged rows simply stay NULL (fail-soft, exactly like a tagger
    outage) and re-attempt on the next read.

    Whatever happens above — full tag pass, budget defer, provider trip, even
    an unavailable LLM client — the ``finally`` step ALWAYS runs the two
    DB-only closers: ``_refresh_job_tags`` (patch fresh tag columns back into
    the caller's row dicts) and ``_reconcile_offfamily_promising`` (retract
    ``promising`` verdicts the now-known family hard-contradicts — Phase 1
    triages titles pre-ingest, before any tag exists, and #517 deliberately
    never demotes on re-poll, so a late-landing tag is the ONLY chance to
    correct an off-family admit; prod 2026-07-30: 55% of promising rows).
    Neither needs the LLM, and both are cheap id-scoped reads/writes.
    """
    if not rows:
        return
    try:
        # Inside the try, so the DB-only closers below STILL run: they need no
        # LLM, they're cheap id-scoped reads/writes, and the reconcile is the
        # only chance to retract an off-family ``promising`` verdict written
        # pre-tag. A skip here must cost tags, never correctness.
        try:
            llm = await get_llm_client_async(_service_client(), None)
        except Exception:
            logger.exception("Qualification tagger: LLM client unavailable; skipping")
            return
        await _qualify_rows_with_budget(supabase, llm, rows, budget_exhausted)
    finally:
        await _refresh_job_tags(supabase, rows)
        await _reconcile_offfamily_promising(
            supabase, [cast(str, r["id"]) for r in rows if r.get("id")]
        )


async def _qualify_rows_with_budget(
    supabase: AsyncClient,
    llm: LLMClient,
    rows: list[dict[str, Any]],
    budget_exhausted: BudgetCheck | None,
) -> None:
    """The budget-gated tagger fan-out (see ``ensure_job_tags`` docstring)."""
    for start in range(0, len(rows), QUALIFICATION_BUDGET_RECHECK_EVERY):
        # Provider fast-fail (audit PERF-M): if a prior chunk already hit a
        # 402/429, the provider is rejecting every call — defer the rest of the
        # backlog this cycle instead of firing hundreds of doomed round-trips.
        if _provider_fatal_active():
            logger.warning(
                "Qualification tagger: LLM provider fast-fail active — deferring "
                "%d remaining job(s) this cycle (they re-tag after the cooldown).",
                len(rows) - start,
            )
            return
        # Re-read the meter between chunks so a long run can't blow past the
        # cap. A meter-read failure fails CLOSED (skip the rest) — refuse to
        # spend when we can't see the budget, matching the cycle gate's posture.
        if budget_exhausted is not None:
            try:
                exhausted = await budget_exhausted()
            except Exception:
                logger.exception(
                    "Qualification tagger: global-budget read failed — "
                    "deferring remaining %d job(s) this cycle",
                    len(rows) - start,
                )
                return
            if exhausted:
                logger.warning(
                    "Qualification tagger: tagger LLM budget reached — deferring "
                    "%d remaining job(s); they re-tag on the next read (rows stay "
                    "NULL). The caller's grading reserve stays available for "
                    "Phase-1/Phase-2 grading.",
                    len(rows) - start,
                )
                return
        chunk = rows[start : start + QUALIFICATION_BUDGET_RECHECK_EVERY]
        await asyncio.gather(
            *(_qualify_one_job(llm, supabase, row) for row in chunk),
            return_exceptions=True,
        )


async def _refresh_job_tags(supabase: AsyncClient, rows: list[dict[str, Any]]) -> None:
    """Patch fresh tag columns back into ``rows`` (in place, best-effort).

    The dicts a caller hands in are snapshots from BEFORE this pass's UPDATEs,
    so on a job's first tagging ``role_family``/``is_us`` would read as NULL
    downstream even though the row is now tagged — and the gates that run
    straight after would fail open on exactly the pass that tags most jobs.
    One chunked read closes that window; a read failure leaves the snapshots as
    they were (the pre-existing behavior).
    """
    ids = [cast(str, r["id"]) for r in rows if r.get("id")]
    if not ids:
        return
    by_id = {cast(str, r["id"]): r for r in rows if r.get("id")}
    try:
        for start in range(0, len(ids), _IN_CHUNK):
            chunk = ids[start : start + _IN_CHUNK]
            resp = await poll_db_read(
                supabase,
                lambda c, _chunk=chunk: (
                    c.table("jobs").select("id, role_family, is_us").in_("id", _chunk)
                ),
                label="qualify tag refresh",
            )
            for raw in cast(list[dict[str, Any]], resp.data or []):
                row = by_id.get(cast(str, raw.get("id")))
                if row is not None:
                    row["role_family"] = raw.get("role_family")
                    row["is_us"] = raw.get("is_us")
    except Exception:
        logger.exception("Qualification tag refresh failed (best-effort; dicts stay stale)")


async def _reconcile_offfamily_promising(supabase: AsyncClient, job_ids: list[str]) -> None:
    """Retract ``promising`` verdicts that the (now-landed) family tag
    hard-contradicts (best-effort).

    Phase 1 triages titles PRE-ingest — no jobs row, no tag — and #517's
    floor deliberately never demotes a persisted verdict on re-poll, so an
    off-family admit would otherwise stand forever (the 2026-07-30 audit:
    55% of all promising rows were hard off-family; the one-off
    ``backfill_offfamily_promising.py`` cleaned the stock, this closes the
    flow). Mismatch test = the shared ``passes_family_gate`` over the
    trigger-synced ``scores.job_role_family`` denorm vs the target's family;
    keep-null on either side, exactly like every read gate. ``excluded`` is
    never touched (user-preference semantics).
    """
    if not job_ids:
        return
    try:
        tresp = await poll_db_read(
            supabase,
            lambda c: c.table("targets").select("id, role_family"),
            label="reconcile target families",
        )
        target_family = {
            cast(str, r["id"]): cast("str | None", r.get("role_family"))
            for r in cast(list[dict[str, Any]], tresp.data or [])
        }
        if not any(target_family.values()):
            return  # no classified targets — nothing can mismatch

        to_retract: list[str] = []
        # Third-size chunks: this read returns up to one row per (job, target)
        # pair, and a full 150-job chunk on a many-target install could cross
        # PostgREST's 1000-row response cap (silent truncation).
        scan_chunk = _IN_CHUNK // 3
        for start in range(0, len(job_ids), scan_chunk):
            chunk = job_ids[start : start + scan_chunk]
            sresp = await poll_db_read(
                supabase,
                lambda c, _chunk=chunk: (
                    c.table("scores")
                    .select("id, target_id, job_role_family")
                    .eq("promising", True)
                    .in_("job_posting_id", _chunk)
                ),
                label="reconcile promising read",
            )
            for s in cast(list[dict[str, Any]], sresp.data or []):
                tfam = target_family.get(cast(str, s.get("target_id")))
                if not passes_family_gate(tfam, cast("str | None", s.get("job_role_family"))):
                    to_retract.append(cast(str, s["id"]))

        for start in range(0, len(to_retract), _IN_CHUNK):
            chunk = to_retract[start : start + _IN_CHUNK]
            await poll_db_write(
                supabase,
                lambda c, _chunk=chunk: (
                    c.table("scores").update({"promising": False}).in_("id", _chunk)
                ),
                label="reconcile promising retract",
            )
        if to_retract:
            logger.info(
                "Family reconcile: retracted %d off-family promising verdict(s) across %d job(s)",
                len(to_retract),
                len(job_ids),
            )
    except Exception:
        logger.exception("Family reconcile failed (best-effort; next cycle retries)")
