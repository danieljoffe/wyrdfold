from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from supabase import Client

from app.config import settings
from app.models.experience import OptimizedDoc
from app.models.schemas import PollResult
from app.models.targets import JobTarget
from app.services import notify
from app.services.ashby import fetch_ashby_jobs
from app.services.date_normalize import normalize_posted_at
from app.services.db_write import DB_WRITE_CONCURRENCY, poll_db_read, poll_db_write
from app.services.experience.optimized import get_latest as get_latest_optimized
from app.services.extract import extract_salary_from_html, salary_columns
from app.services.firecrawl import fetch_firecrawl_jobs
from app.services.fit import run_phase2_for_jobs
from app.services.greenhouse import fetch_board_jobs
from app.services.jd_parser import parse_jd
from app.services.jsonld import fetch_jsonld_jobs, fetch_salary_from_posting_page
from app.services.lever import fetch_lever_jobs
from app.services.llm import MissingUserKeyError
from app.services.llm import get_client as get_llm_client
from app.services.llm.client import LLMClient
from app.services.llm.cost_log import enqueue as enqueue_llm_cost
from app.services.llm.cost_log import record as record_llm_cost
from app.services.llm.cost_log import total_spend_all as total_llm_spend_all
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
from app.services.location_parse import parse_location
from app.services.mock_board import fetch_mock_jobs
from app.services.qualification import (
    QUALIFICATION_PURPOSE,
    is_us_location,
    positively_us_location,
    qualification_hash,
    tag_job,
)
from app.services.qualification.family_gate import passes_family_gate
from app.services.recency import refresh_recency_scores_poll
from app.services.relevance.title_triage import (
    PHASE1_PURPOSE,
    TitleVerdict,
    admitted,
    phase1_batch_size,
    triage_titles,
)
from app.services.sanitize import sanitize_html
from app.services.scoring import score_title_against_profile
from app.services.smartrecruiters import fetch_smartrecruiters_jobs
from app.services.standard_job import StandardJob
from app.services.target_scoring import (
    score_and_upsert_poll as target_score_and_upsert,
)
from app.services.target_scoring import (
    score_title_and_upsert_poll as target_title_score_and_upsert,
)
from app.services.targets.crud import get_active as get_active_target
from app.services.targets.crud import is_pipeline_active as target_is_pipeline_active
from app.services.targets.payers import PayerBudgetGate, build_budget_gate
from app.services.validate import liveness_verdict, validate_job_url
from app.services.workday import fetch_workday_jobs

logger = logging.getLogger(__name__)

# Large id lists derived from a source's job feed (re-read of newly-inserted
# rows, Stage 3 score lookups, stale-archive) never go through a request URL:
# they ride in a ``p_ids`` jsonb body on a server-side RPC (#93), so there's
# no PostgREST URL-length limit to chunk around.

Fetcher = Callable[[str], Coroutine[Any, Any, list[StandardJob]]]

FETCHERS: dict[str, Fetcher] = {
    "greenhouse": fetch_board_jobs,
    "lever": fetch_lever_jobs,
    "ashby": fetch_ashby_jobs,
    "workday": fetch_workday_jobs,
    "smartrecruiters": fetch_smartrecruiters_jobs,
    "jsonld": fetch_jsonld_jobs,
    "crawl": fetch_firecrawl_jobs,
    # Local load-testing only — raises unless MOCK_FETCHER_ENABLED (see
    # app/services/mock_board.py), so a mistyped provider on a real source
    # fails its poll instead of fabricating jobs.
    "mock": fetch_mock_jobs,
}

# How many sources poll in parallel. Lowered from 10 (audit #29 / live
# prod broken-pipe storm): combined with the per-source scoring fan-out
# (each row → one supabase write), 10 concurrent sources thundering-herd
# the Supabase pooler. The hard ceiling on concurrent DB writes is
# ``DB_WRITE_CONCURRENCY`` (see ``app.services.db_write``), but a lower
# source fan-out also keeps the per-source detail/JD fetches civil.
POLL_CONCURRENCY = 6
LLM_CONCURRENCY = 3

# Cycle-wide caps for the two fan-outs that otherwise had none. Both
# ``_qualify_jobs`` (LLM ``tag_job`` per row) and ``_validate_rows`` (URL
# validation per row) gather over a whole source's rows, and POLL_CONCURRENCY
# sources run at once — so without a *shared* bound the poll can open hundreds
# of simultaneous OpenRouter calls (429s + cost bursts) or thousands of
# simultaneous URL validations. One semaphore per event loop, keyed by the
# running loop like ``db_write`` so a fresh test/worker loop gets its own.
QUALIFY_LLM_CONCURRENCY = 12
VALIDATE_CONCURRENCY = 20
_qualify_llm_sems: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}
_validate_sems: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}


def _qualify_llm_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _qualify_llm_sems.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(QUALIFY_LLM_CONCURRENCY)
        _qualify_llm_sems[loop] = sem
    return sem


def _validate_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _validate_sems.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(VALIDATE_CONCURRENCY)
        _validate_sems[loop] = sem
    return sem


# Every DB touch in the poll cycle routes through the ``db_write`` seam
# (``poll_db_write`` / ``poll_db_read`` or the ``*_poll`` service variants
# built on them): sync-client-in-a-thread today, the pooled async HTTP/2
# client when ``POLLER_ASYNC_DB`` is on (#57). The handful of remaining
# ``asyncio.to_thread(helper, ...)`` calls below wrap sync helpers from
# modules outside the poll herd (targets CRUD, budget meters, analysis
# persistence) — a few calls per cycle, migrating with the request-handler
# slice, not per-row fan-outs.

# How many qualification-tagger jobs to fan out between global-budget
# re-reads (#60 overspend fix). The tagger bills the instance key, so it is
# invisible to the per-payer ``PayerBudgetGate``; left ungated it ground the
# whole backlog past ``global_llm_daily_budget_usd`` (the June incident).
# ``_qualify_jobs`` re-checks the live day-spend before each chunk and stops
# once the cap is hit, so worst-case overshoot is bounded to ONE chunk's
# spend (~this many Haiku calls) rather than the entire backlog. Smaller =
# tighter cap, more meter reads; this balances the two.
QUALIFICATION_BUDGET_RECHECK_EVERY = 50

# --- Provider-fatal fast-fail breaker (audit PERF-M "402/429 fast-fail") -------
# ``_global_budget_exhausted`` stops at our SELF-IMPOSED daily spend cap. The
# provider-fatal breaker catches the *provider* rejecting every call — OpenRouter
# out of credits (402) or sustained rate-limiting (429) — which can happen while
# we're still UNDER budget. The first such error from the tagger latches it for a
# cooldown so the qualify fan-out stops firing doomed calls; it auto-clears
# (monotonic). It now lives in ``app.services.llm.provider_breaker`` (imported at
# the top as ``_provider_fatal_active`` / ``_trip_provider_fatal``) so the E2 lazy
# fit-score refresh shares the SAME latch — a credits outage caught by either
# backs the other off too.


# US-location detection (hint list + regexes + ``_is_us_location``) moved to
# ``app/services/qualification/heuristics.py`` so the poller's ingestion gate
# and the #60 qualification tagger's L1 share ONE implementation (single source
# of truth). ``_is_us_location`` is re-exported below for back-compat with
# callers/tests that import it from this module.


def _title_matches_any_target(title: str, targets: list[JobTarget]) -> bool:
    """Check if a job title is worth ingesting for at least one target.

    Admission rules per target (any one target admitting → admit):
      1. Excluded by negative keywords → admit anyway, so the scoring
         pipeline records the rejection (excluded=True) for audit.
         Without this, junior-vs-director hits would silently vanish
         instead of being explainable in the UI.
      2. Matched scoring keywords AND (search_keywords overlap matches
         the title, OR the target has no search_keywords). This is the
         AND-semantics fix from the relevance-matcher research doc:
         a title that only hits incidental skill/seniority tokens but
         doesn't look like the *kind* of role the user is hunting for
         (no search-keyword overlap) is rejected at the door rather
         than ingested as low-score noise.
    """
    for target in targets:
        result = score_title_against_profile(
            title,
            target.scoring_profile,
            search_keywords=target.search_keywords,
        )
        if result.excluded:
            return True
        if not result.matched_keywords:
            continue
        # Empty search_keywords means we can't gate on role-title intent;
        # fall back to legacy "any keyword match admits" semantics so a
        # draft / legacy profile doesn't ingestion-block itself.
        if target.search_keywords and not _title_matches_target(title, target.search_keywords):
            continue
        return True
    return False


# Tokens we drop before token-overlap matching — pure connective words
# that contribute no signal. Kept short on purpose; anything role-specific
# (e.g. "engineering", "operations") stays in.
_MATCH_STOPWORDS: frozenset[str] = frozenset(
    {"of", "the", "and", "a", "an", "for", "to", "in", "on", "at"}
)

# Minimum fraction of a keyword's content tokens that must appear in the
# title for a token-overlap match. 0.6 means a 5-token keyword needs 3 of
# those tokens in the title — strict enough that "Director" alone doesn't
# match "Director of CX Operations", lax enough that "Director, Customer
# Experience" matches both "director of customer experience" and
# "head of customer experience".
_MATCH_MIN_OVERLAP_RATIO: float = 0.6


def _content_tokens(text: str) -> list[str]:
    """Lower-case word-boundary split with stopwords removed.

    Used by ``_title_matches_target`` on both sides of the comparison so
    matching is symmetric (token-by-token rather than substring-by-substring).
    """
    raw = text.lower().replace(",", " ").replace("/", " ").split()
    return [t for t in raw if t and t not in _MATCH_STOPWORDS]


# Ubiquitous role / seniority tokens that describe a job's LEVEL or generic
# FUNCTION, not its domain. Excluded from the "distinctive token" requirement
# below: a match that rides only these (with the keyword's domain token absent)
# is a false positive — "Senior Director, Finance Operations" sharing
# director+operations with "director of customer operations" is not a CX role.
# Kept to genuinely cross-domain words; anything domain-bearing (customer, cx,
# frontend, react, ui, success, experience, ...) is deliberately NOT here.
_GENERIC_ROLE_TOKENS: frozenset[str] = frozenset(
    {
        # seniority / org level
        "senior",
        "junior",
        "staff",
        "principal",
        "lead",
        "leader",
        "mid",
        "entry",
        "chief",
        "head",
        "vp",
        "svp",
        "evp",
        "vice",
        "president",
        "director",
        "manager",
        "management",
        "officer",
        "associate",
        "intern",
        "trainee",
        "deputy",
        "assistant",
        "global",
        "regional",
        "sr",
        "jr",
        # ubiquitous cross-domain function / role-type nouns
        "operations",
        "ops",
        "engineer",
        "engineering",
        "developer",
        "development",
        "platform",
        "specialist",
        "coordinator",
        "generalist",
        "professional",
    }
)


def _title_matches_target(title: str, keywords: list[str]) -> bool:
    """Token-overlap match between a job title and any of the target's
    search keywords.

    Previous version used pure substring match — ``"director of cx operations"
    in title_lower`` — which silently dropped almost every real posting
    because companies rarely include filler words verbatim in their titles
    ("Director, Customer Experience" doesn't contain "director of cx
    operations"). The matcher tokenizes both sides on word boundaries, drops
    stopwords, and accepts the keyword when at least ``_MATCH_MIN_OVERLAP_RATIO``
    of its content tokens appear as substrings of the title's tokens. Substring
    (not exact) so plurals and "Customer-Centric" → "Customer" still match.

    Overlap alone over-admits, though: on a 3-token keyword the 0.6 ratio is
    satisfied by any 2 tokens, so generic role words carry the match —
    "Senior Director, Finance Operations" hit "director of customer operations"
    on director+operations with the distinctive "customer" absent. So the match
    ALSO requires ≥1 ``distinctive`` token (keyword tokens minus
    ``_GENERIC_ROLE_TOKENS``) in the title. A keyword that is entirely generic
    keeps the overlap-only behaviour.
    """
    if not keywords:
        return False
    title_tokens = _content_tokens(title)
    if not title_tokens:
        return False
    for keyword in keywords:
        kw_tokens = _content_tokens(keyword)
        if not kw_tokens:
            continue
        # Fast path: a 1-token keyword degenerates to plain substring match.
        if len(kw_tokens) == 1:
            if any(kw_tokens[0] in t for t in title_tokens):
                return True
            continue
        hits = sum(1 for kw in kw_tokens if any(kw in t for t in title_tokens))
        if hits / len(kw_tokens) < _MATCH_MIN_OVERLAP_RATIO:
            continue
        distinctive = [t for t in kw_tokens if t not in _GENERIC_ROLE_TOKENS]
        if distinctive and not any(any(d in t for t in title_tokens) for d in distinctive):
            continue
        return True
    return False


# ``_is_us_location`` now lives in ``qualification.heuristics`` (single source
# of truth shared with the #60 tagger). Re-exported under the original private
# name so existing callers/tests (``tests/test_poller.py``) keep importing it
# from this module unchanged.
_is_us_location = is_us_location


def _passes_free_gates(job: StandardJob, active_targets: list[JobTarget]) -> bool:
    """The zero-cost ingestion gates, conjunction form.

    Same semantics as the per-job loop in ``_poll_one_source`` — title
    prematch (including the excluded-admits-for-audit rule and the
    empty-``search_keywords`` fallback inside ``_title_matches_any_target``)
    AND the US-location pass. With NO active targets there is nothing to
    match, so nothing passes — a poll with zero active targets ingests
    nothing (a target activating re-polls its sources). Used to pre-filter
    the Phase 1 triage candidate set so the LLM only ever sees titles that
    could actually be ingested: a job these gates reject is dropped in the
    per-job loop regardless of its verdict, so classifying it is pure spend.
    """
    if not _title_matches_any_target(job.title, active_targets):
        return False
    return _is_us_location(job.location_name)


def _phase1_promising(
    verdict: TitleVerdict | None,
    *,
    attempted: bool,
    gate_active: bool,
    min_confidence: int,
) -> bool | None:
    """Phase-1 admission decision WITH budget-deferral (#285 follow-on).

    Replaces the blanket fail-open. A missing verdict means one of two very
    different things, and we must not conflate them:

    - ``attempted`` — this target actually SENT this title to the triage LLM
      this cycle. A verdict present → use it; a missing verdict is a genuine
      LLM hiccup (dropped id) and still fail-opens to admit, so a rare model
      glitch can't drop a relevant posting.
    - NOT ``attempted`` — the target never triaged this title because the
      daily LLM budget / payer allowance was exhausted (poller breaks out of
      triage mid-cycle). Returns ``None`` = DEFER: the job is excluded now
      (not shown, not Phase-2 graded) and re-triaged once the budget resets.
      There is NO admit-everything fallback on budget exhaustion — the whole
      point of this fix (prod was ~55% fail-open on budget, all off-target).

    ``gate_active`` False (triage disabled / no targets) keeps the legacy
    admit-all behavior so turning the gate off is unchanged.
    """
    if not gate_active or attempted:
        return admitted(verdict, min_confidence=min_confidence)
    return None


# Phase-1 negative-verdict cache (#514 residual). A REJECTED candidate never
# ingests, so its title re-enters triage every cycle and re-pays the LLM for
# the same "no" at the source's poll cadence until the posting closes —
# measured as the dominant LLM line item (17,843 title_triage calls / $8.34
# per 7d, 2026-07-29). Remember rejections per (target, profile_version,
# normalized title) for ``settings.phase1_rejection_ttl_hours``; a cache hit
# re-injects a synthetic ``promising=False`` verdict, so every downstream
# mechanism (attempted-set defer semantics, ``_any_target_admits``, Stage-2
# floor writes) behaves exactly as if the LLM had re-said no. Admits are
# never cached: an admitted job INGESTS, so known-ness already stops its
# re-triage. Keyed on profile_version so a profile edit re-judges everything
# under the new profile immediately. In-process only — the poller runs under
# a fleet-wide advisory lock, so one process sees all cycles; a restart just
# costs one extra verdict per title.
_PHASE1_REJECTIONS: dict[tuple[str, int, str], float] = {}
# Hard size bound. ~60k rejections is far beyond a day of fleet-wide triage
# (~2.5k verdicts/day measured); hitting it means something is looping.
_PHASE1_REJECTIONS_CAP = 60_000


def _phase1_rejection_key(target: JobTarget, title: str) -> tuple[str, int, str]:
    return (target.id, target.profile_version, " ".join(title.lower().split()))


def _phase1_cached_rejection(target: JobTarget, title: str) -> bool:
    """True iff this (target, title) was LLM-rejected within the TTL."""
    if settings.phase1_rejection_ttl_hours <= 0:
        return False
    expiry = _PHASE1_REJECTIONS.get(_phase1_rejection_key(target, title))
    return expiry is not None and expiry > time.monotonic()


def _phase1_record_rejection(target: JobTarget, title: str) -> None:
    if settings.phase1_rejection_ttl_hours <= 0:
        return
    if len(_PHASE1_REJECTIONS) >= _PHASE1_REJECTIONS_CAP:
        now = time.monotonic()
        for key in [k for k, exp in _PHASE1_REJECTIONS.items() if exp <= now]:
            del _PHASE1_REJECTIONS[key]
        if len(_PHASE1_REJECTIONS) >= _PHASE1_REJECTIONS_CAP:
            # Still full of LIVE entries — blunt reset. Losing the cache
            # costs extra verdicts, never correctness.
            _PHASE1_REJECTIONS.clear()
    _PHASE1_REJECTIONS[_phase1_rejection_key(target, title)] = (
        time.monotonic() + settings.phase1_rejection_ttl_hours * 3600.0
    )


def _content_dedupe_key(company: str | None, title: str | None) -> tuple[str, str]:
    """Stable lowercase + collapsed-whitespace key for the
    (company, title) dedupe pass. Whitespace differences ("Director"
    vs "Director " vs "Director\\n") are normalized; punctuation and
    casing differences are normalized; everything else is left as-is
    on purpose (e.g. "Director, Customer Ops" vs "Director Customer
    Ops" should still be considered distinct because the comma
    might actually delimit a different role)."""
    co = " ".join((company or "").lower().split())
    ti = " ".join((title or "").lower().split())
    return (co, ti)


def _dedupe_by_content(
    rows: list[dict[str, Any]],
    *,
    existing: list[dict[str, Any]],
    source: str,
) -> list[dict[str, Any]]:
    """Drop rows whose (company, title) collides with another row in
    the batch OR with an existing in-DB row whose external_id differs.

    Greenhouse posts the same role under each office's location as a
    separate listing with a distinct external_id. The upsert's
    on_conflict key only matches by external_id so the duplicates
    sneak through. This helper closes that hole.

    Within-batch dedupe keeps the first row seen (input order is the
    poll cycle's discovery order, so this is reasonably stable). The
    cross-batch dedupe leaves the existing-in-DB row alone — only
    new incoming candidates with a different external_id are
    dropped. An upsert of the SAME external_id (the legitimate
    update path) is unaffected.
    """
    existing_by_key: dict[tuple[str, str], str] = {}
    for row in existing:
        key = _content_dedupe_key(row.get("company_name"), row.get("title"))
        # First-seen wins on the DB side too (existing rows may already
        # contain duplicates from before this dedupe existed — pin to
        # one of them as the canonical entry).
        existing_by_key.setdefault(key, row.get("external_id", ""))

    seen_in_batch: set[tuple[str, str]] = set()
    deduped: list[dict[str, Any]] = []
    skipped_within = 0
    skipped_cross = 0
    for row in rows:
        key = _content_dedupe_key(row.get("company_name"), row.get("title"))

        if key in seen_in_batch:
            skipped_within += 1
            continue

        existing_ext = existing_by_key.get(key)
        if existing_ext and existing_ext != row.get("external_id"):
            skipped_cross += 1
            continue

        seen_in_batch.add(key)
        deduped.append(row)

    if skipped_within or skipped_cross:
        logger.info(
            "dedupe %s: %d within-batch, %d cross-batch (kept %d of %d)",
            source,
            skipped_within,
            skipped_cross,
            len(deduped),
            len(rows),
        )
    return deduped


async def _validate_one_row(row: dict[str, Any]) -> dict[str, Any]:
    """Validate the absolute_url of a single job row."""
    url = row.get("absolute_url")
    if not url:
        return row
    try:
        # PERF-H3: bound the URL-validation fan-out cycle-wide.
        async with _validate_semaphore():
            result = await validate_job_url(url)
        # The validation's OBSERVABLE effects live on absolute_url (null a
        # rejected link, normalize redirects). The old per-row ledger columns
        # (url_validation_status/warnings) were write-only for their whole
        # life — 0 rejected / 35k 'valid' ever — and were dropped in R2.
        if not result.is_valid:
            row["absolute_url"] = None
        elif result.final_url != url:
            row["absolute_url"] = result.final_url
    except Exception:
        logger.exception("URL validation failed for %s", url)
    return row


async def _validate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate URLs for all rows concurrently."""
    return list(await asyncio.gather(*(_validate_one_row(r) for r in rows)))


async def _fill_jsonld_salaries(
    rows: list[dict[str, Any]], known_external_ids: set[str | None]
) -> None:
    """#503: bounded JSON-LD ``baseSalary`` fallback for NEW salary-less rows.

    Board APIs often omit the structured pay their hosted posting pages carry
    as schema.org markup (Lever/Ashby especially). For up to
    ``jsonld_salary_max_fetches`` NEW rows per source per cycle whose JD text
    yielded no salary, fetch the posting page and read ``baseSalary``.
    Flag-gated (ships dark), bounded by the same cycle-wide fetch semaphore
    as URL validation, and best-effort — failures leave the row's salary
    null, exactly as before. Known rows are skipped: their salary re-derives
    from JD content every cycle (#514), and re-fetching every known row's
    page per cycle would be the same fan-out storm URL validation avoids.
    """
    if not settings.jsonld_salary_enabled:
        return
    candidates = [
        r
        for r in rows
        if r.get("external_id") not in known_external_ids
        and not r.get("salary_text")
        and r.get("absolute_url")
    ][: settings.jsonld_salary_max_fetches]
    if not candidates:
        return

    async def _one(row: dict[str, Any]) -> None:
        try:
            async with _validate_semaphore():
                salary = await fetch_salary_from_posting_page(str(row["absolute_url"]))
            if salary:
                row["salary_text"] = salary
                row.update(salary_columns(salary))
        except Exception:
            logger.exception("jsonld salary fill failed for %s", row.get("absolute_url"))

    await asyncio.gather(*(_one(r) for r in candidates))


async def _load_alert_rows(
    supabase: Client, new_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Re-read newly-inserted job rows with their post-scoring state.

    The upsert response rows carry ``score = 0`` (the column default) —
    the scoring stages write final scores to the DB afterwards without
    mutating those in-memory dicts. Alert thresholds compare against
    ``score``, so dispatching with the stale rows means no alert can
    ever clear the bar. Falls back to the stale rows on a read failure
    (alerts then skip this cycle, matching the old behavior, but the
    failure is logged instead of silent).
    """
    new_ids = [r["id"] for r in new_rows if r.get("id")]
    if not new_ids:
        return new_rows
    # The upsert's RETURNING rows can't be threaded through here: they carry
    # the upsert-time ``score`` (column default 0) — the scoring stages write
    # final scores to the DB afterwards without mutating those dicts — which
    # is the exact staleness this re-read exists to fix. So we re-read the
    # post-scoring state, but via the ``get_jobs_by_ids`` RPC: ``new_ids``
    # scales with a source's feed and rides in the ``p_ids`` jsonb body
    # instead of the request URL (no URL-length limit, one round-trip, #93).
    # The RPC returns SETOF jobs — the same column shape the old
    # ``select("*")`` returned, so alert dispatch reads identical rows.
    try:
        resp = await poll_db_read(
            supabase,
            lambda c: c.rpc("get_jobs_by_ids", {"p_ids": new_ids}),
            label="poll alert-rows refresh",
        )
        refreshed = cast(list[dict[str, Any]], resp.data or [])
        if refreshed:
            return refreshed
    except Exception:
        logger.exception("Alert-row refresh failed — dispatching stale rows")
    return new_rows


async def _resolve_user_targets_for_stage3(
    supabase: Client,
    active_targets: list[JobTarget],
    company_name: str,
) -> tuple[dict[str, JobTarget], dict[str, OptimizedDoc]]:
    """Pair each user with a primary active target + their optimized doc.

    Targets are global; ``user_targets`` is the junction. We fetch the
    junction once, build (user_id → first active target) and (user_id →
    optimized doc) maps, and skip users whose optimized doc hasn't been
    generated yet (onboarding incomplete) — keyword scoring still runs
    for them via stage 1 + stage 2.

    Returns ``(primary_by_user, user_optimized)``. ``company_name`` is
    used only for the skip-log message.
    """
    if not active_targets:
        return {}, {}

    target_ids = [t.id for t in active_targets]
    junction_resp = await poll_db_read(
        supabase,
        lambda c: (
            c.table("user_targets")
            .select("target_id, user_id")
            .eq("is_active", True)
            .in_("target_id", target_ids)
        ),
        label="poll stage3 user-targets read",
    )
    junction_rows = cast(list[dict[str, Any]], junction_resp.data or [])
    users_by_target: dict[str, list[str]] = {}
    for row in junction_rows:
        users_by_target.setdefault(row["target_id"], []).append(row["user_id"])

    primary_by_user: dict[str, JobTarget] = {}
    user_optimized: dict[str, OptimizedDoc] = {}
    for t in active_targets:
        for user_id in users_by_target.get(t.id, []):
            if user_id in primary_by_user:
                continue
            doc = await asyncio.to_thread(get_latest_optimized, supabase, user_id)
            if doc is None:
                logger.info(
                    "No optimized doc for user %s; skipping stage 3 for %s",
                    user_id,
                    company_name,
                )
                continue
            primary_by_user[user_id] = t
            user_optimized[user_id] = doc

    return primary_by_user, user_optimized


async def _qualify_one_job(
    llm: LLMClient,
    supabase: Client,
    row: dict[str, Any],
) -> None:
    """Tag ONE job row and persist its qualification columns (#60).

    Content-hash cached: skips the LLM call when the row's current
    ``qualified_hash`` already matches the freshly-computed hash over
    (title, company, location, description) — so a re-poll that returns an
    unchanged posting costs nothing. Fully best-effort: any error is logged
    and swallowed so the row simply stays NULL (not-yet-tagged) and a later
    cycle re-attempts it.
    """
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

    payload: dict[str, Any] = {
        "is_us": tags.is_us,
        "role_family": tags.role_family,
        "seniority": tags.seniority,
        "employment_type": tags.employment_type,
        "metro": tags.metro,
        "is_remote": tags.is_remote,
        "is_genuine_role": tags.is_genuine_role,
        "qualified_at": datetime.now(UTC).isoformat(),
        "qualified_hash": new_hash,
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
    try:
        await poll_db_write(
            supabase,
            lambda c: c.table("jobs").update(payload).eq("id", row["id"]),
            label="qualification tags update",
        )
    except Exception:
        logger.exception("Qualification tag write failed for job %s", row.get("id"))


async def _qualify_jobs(
    supabase: Client,
    rows: list[dict[str, Any]],
) -> None:
    """Run the #60 qualification tagger over ``rows`` (best-effort).

    Target-INDEPENDENT, so it bills the instance key (``get_client(..,
    None)``) — never a per-target payer. Concurrency is bounded by the
    shared DB-write semaphore inside ``_qualify_one_job``; the tagger calls
    themselves fan out together. The whole step is wrapped so a tagger or
    client-resolution failure can never break the poll.

    Global-budget gated (#60 overspend fix): the tagger bills the instance
    key, so the per-payer ``PayerBudgetGate`` that protects Phase-1/2 work
    can't see it. Left ungated it ground the backlog clean past
    ``global_llm_daily_budget_usd``. We re-read the live day-spend before
    each chunk and stop the moment the cap is reached — bounding overshoot
    to one chunk instead of the whole backlog. Untagged rows simply stay
    NULL (fail-soft, exactly like a tagger outage) and re-attempt next cycle
    once the UTC day rolls over and the meter resets.

    Whatever happens above — full tag pass, budget defer, provider trip, even
    an unavailable LLM client — the ``finally`` step ALWAYS runs the two
    DB-only closers: ``_refresh_job_tags`` (patch fresh tag columns back into
    the caller's row dicts, which are upsert-time snapshots that predate this
    cycle's tag writes) and ``_reconcile_offfamily_promising`` (retract
    ``promising`` verdicts the now-known family hard-contradicts — Phase 1
    triages titles pre-ingest, before any tag exists, and #517 deliberately
    never demotes on re-poll, so a late-landing tag is the ONLY chance to
    correct an off-family admit; prod 2026-07-30: 55% of promising rows).
    Neither needs the LLM, and both are cheap id-scoped reads/writes.
    """
    if not rows:
        return
    try:
        try:
            llm = get_llm_client(supabase, None)
        except Exception:
            logger.exception("Qualification tagger: LLM client unavailable; skipping")
            return
        await _qualify_rows_with_budget(supabase, llm, rows)
    finally:
        await _refresh_job_tags(supabase, rows)
        await _reconcile_offfamily_promising(
            supabase, [cast(str, r["id"]) for r in rows if r.get("id")]
        )


async def _qualify_rows_with_budget(
    supabase: Client,
    llm: LLMClient,
    rows: list[dict[str, Any]],
) -> None:
    """The budget-gated tagger fan-out (see ``_qualify_jobs`` docstring)."""
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
        # Re-read the meter between chunks so a long backlog can't blow past
        # the cap. A meter-read failure fails CLOSED (skip the rest) —
        # refuse to spend when we can't see the budget, matching the cycle
        # gate's posture.
        try:
            exhausted = await asyncio.to_thread(
                _global_budget_exhausted,
                supabase,
                reserve_usd=settings.grading_budget_reserve_usd,
            )
        except Exception:
            logger.exception(
                "Qualification tagger: global-budget read failed — "
                "deferring remaining %d job(s) this cycle",
                len(rows) - start,
            )
            return
        if exhausted:
            logger.warning(
                "Qualification tagger: tagger LLM budget reached "
                "($%.2f cap − $%.2f grading reserve) — deferring %d remaining "
                "job(s); they re-tag next cycle (rows stay NULL). The reserve "
                "stays available for Phase-1/Phase-2 grading.",
                settings.global_llm_daily_budget_usd,
                settings.grading_budget_reserve_usd,
                len(rows) - start,
            )
            return
        chunk = rows[start : start + QUALIFICATION_BUDGET_RECHECK_EVERY]
        await asyncio.gather(
            *(_qualify_one_job(llm, supabase, row) for row in chunk),
            return_exceptions=True,
        )


# in_() id-chunk bound for the tag-refresh / reconcile reads (#57 lesson:
# ≤150-200 UUIDs keeps the PostgREST URL under proxy limits).
_IN_CHUNK = 150


async def _refresh_job_tags(supabase: Client, rows: list[dict[str, Any]]) -> None:
    """Patch fresh tag columns back into ``rows`` (in place, best-effort).

    The dicts the poll cycle threads into Phase 2 (``upsert_resp.data``) are
    snapshots from BEFORE the tagger's UPDATE, so on a job's first cycle
    ``role_family``/``is_us`` read as NULL downstream even though the row is
    tagged — the Phase-2 family and US gates then fail open on exactly the
    cycle that grades most jobs. One chunked read closes that window; a read
    failure leaves the snapshots as they were (the pre-existing behavior).
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


async def _reconcile_offfamily_promising(supabase: Client, job_ids: list[str]) -> None:
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
                "Family reconcile: retracted %d off-family promising verdict(s) "
                "across %d job(s)",
                len(to_retract),
                len(job_ids),
            )
    except Exception:
        logger.exception("Family reconcile failed (best-effort; next cycle retries)")


async def _backfill_qualify_stale(supabase: Client, limit: int) -> None:
    """Liveness-check + tag a bounded batch of the OLDEST untagged, unarchived
    jobs (#285); archive the ones whose listing is gone.

    ``_qualify_jobs`` only sees jobs re-upserted THIS cycle. A job that fell
    off its source's feed without being archived is never re-visited, so it
    stays untagged forever and slips through the is_us (#257) / role_family
    (#278) read gates on the NULL benefit-of-the-doubt (~half the unarchived
    catalog on prod when this landed). This sweep re-selects the oldest such
    rows, cheaply checks each listing is still live (SSRF-safe, via
    ``validate_job_url``), then:

    * LIVE  → tag through the SAME budget-gated ``_qualify_jobs`` (it stops the
      instant the global LLM meter minus the grading reserve is reached, so the
      backlog drains over cycles without overspending or starving grading).
    * DEAD  → archive: a 4xx (a confident "gone") shouldn't linger in the gates
      OR cost a tag (also clears the stale-but-shown postings #285 found).
    * UNKNOWN (a 200 that isn't a job, timeout, 5xx) → left untouched, retried
      next cycle — archival is sticky, so it needs the hard 4xx signal.

    Oldest-first, and every selected row is genuinely never-tagged
    (``role_family`` NULL ⇒ no ``qualified_hash``/``qualified_at`` on prod), so
    the content-hash skip inside ``_qualify_one_job`` can't turn the batch into
    a no-op. A row the tagger can't parse stays NULL and is re-selected next
    cycle — wasting at most a couple of slots, never blocking the rest.
    """
    if limit <= 0:
        return
    try:
        resp = await poll_db_read(
            supabase,
            lambda c: (
                c.table("jobs")
                .select(
                    "id, absolute_url, title, company_name, location, "
                    "description_html, qualified_hash, qualified_at"
                )
                .is_("role_family", "null")
                .is_("archived_at", "null")
                .order("cataloged_at", desc=False)
                .limit(limit)
            ),
            label="poll qualify-backfill select",
        )
    except Exception:
        logger.exception("Qualification backfill: select failed; skipping this cycle")
        return
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        return

    # Liveness gate before spending a tag. A fresh semaphore (sized like the
    # DB-write cap) bounds the HTTP fan-out; each check is timeout-capped inside
    # ``validate_job_url`` — also the ONLY SSRF-safe way to fetch these
    # arbitrary posting URLs.
    sem = asyncio.Semaphore(DB_WRITE_CONCURRENCY)
    live: list[dict[str, Any]] = []
    dead: list[str] = []

    async def _check(row: dict[str, Any]) -> None:
        url = row.get("absolute_url")
        if not url:
            return  # no URL to verify — leave untagged, spend nothing
        async with sem:
            try:
                verdict = liveness_verdict(await validate_job_url(cast(str, url)))
            except Exception:
                return  # transient — retry next cycle
        if verdict == "live":
            live.append(row)
        elif verdict == "dead":
            dead.append(cast(str, row["id"]))

    await asyncio.gather(*(_check(r) for r in rows), return_exceptions=True)

    if dead:
        logger.info("Qualification backfill: archiving %d dead listing(s)", len(dead))
        with contextlib.suppress(Exception):
            await poll_db_write(
                supabase,
                lambda c: (
                    c.table("jobs")
                    .update({"archived_at": datetime.now(UTC).isoformat()})
                    .in_("id", dead)
                ),
                label="poll qualify-backfill archive",
            )
    if live:
        logger.info("Qualification backfill: tagging %d live job(s)", len(live))
        await _qualify_jobs(supabase, live)


async def _drop_purged_rows(
    supabase: Client, source_id: str, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Filter out rows whose (source, external_id) was TOMBSTONED (archival
    Stage 2, option B).

    The jobs upsert conflicts on (source_id, external_id) — without this
    guard, a re-poll of a purged posting would UPDATE the tombstone back to
    life (fresh description, re-triage, re-grade), defeating the purge. One
    indexed id-level read per source poll; fail-soft (on error, upsert
    everything — a resurrected tombstone is recoverable, a broken poll isn't).
    """
    if not rows:
        return rows
    try:
        ext_ids = [r["external_id"] for r in rows if r.get("external_id")]
        purged: set[str] = set()
        for i in range(0, len(ext_ids), 200):
            chunk = ext_ids[i : i + 200]
            resp = await poll_db_read(
                supabase,
                lambda c, ids=chunk: (
                    c.table("jobs")
                    .select("external_id")
                    .eq("source_id", source_id)
                    .not_.is_("purged_at", "null")
                    .in_("external_id", ids)
                ),
                label="poll purge-guard read",
            )
            purged.update(
                cast(str, r["external_id"]) for r in cast(list[dict[str, Any]], resp.data or [])
            )
        if not purged:
            return rows
        kept = [r for r in rows if r.get("external_id") not in purged]
        logger.info(
            "Purge guard: refused re-insert of %d tombstoned job(s) for source %s",
            len(rows) - len(kept),
            source_id,
        )
        return kept
    except Exception:
        logger.exception("Purge guard failed for source %s; upserting all", source_id)
        return rows


async def _backfill_grade_stale(supabase: Client, limit: int) -> None:
    """Grade a bounded, view-ordered batch of the stale ``promising`` backlog.

    ``run_phase2_for_jobs`` only ever sees jobs a poll cycle re-touched, so a
    ``promising=true`` row whose grading was deferred (budget/BYOK/cap) — or
    that a profile-version bump reset back to ``stage2`` via
    ``bulk_score_for_target`` — re-grades only if its source happens to
    re-list the job. A profile edit therefore wipes a target's graded shelf
    and the /jobs list falls back to the *Pending* tier, which carries
    keyword placeholders and fossilized fail-open triage admits — the
    unrelated-roles pollution found on 2026-07-15 (18.5k stale promising rows
    on one target, 76%% of its visible pending tier off-target). Same
    stranding class ``_backfill_qualify_stale`` fixes for tags; this is its
    grading twin.

    Selection is LIVE-only (archived/purged/non-US rows excluded — 78%% of
    that target's stale backlog was attached to dead jobs; grading those is
    pure spend) and ordered by ``recency_score`` DESC so the rows the user
    is actually looking at grade first.

    Spend safety: every existing gate still applies — the once-per-cycle
    global breaker + payer allowances (via ``_cycle_budget_gate``), BYOK
    require-mode (no key → defer), and ``run_phase2_for_jobs``'s own
    per-target DAILY quota, which this sweep shares with cycle grading (it
    fills unused quota, it cannot exceed it). ``limit`` bounds the batch per
    (user, target) per cycle on top. Best-effort throughout — a sweep
    failure never breaks a poll.
    """
    if limit <= 0:
        return
    try:
        gate, _has_active = await _cycle_budget_gate(supabase)
        active_targets = await asyncio.to_thread(get_active_target, supabase)
        primary_by_user, user_optimized = await _resolve_user_targets_for_stage3(
            supabase, active_targets, "(grade backfill)"
        )
    except Exception:
        logger.exception("Grade backfill: cycle context resolution failed; skipping")
        return
    if not primary_by_user:
        return

    payer_clients: dict[str | None, LLMClient | None] = {}
    for uid, target in primary_by_user.items():
        if gate.user_blocked(uid):
            logger.info(
                "Grade backfill deferred for user %s / target %s (over allowance)",
                uid,
                target.id,
            )
            continue
        llm = _resolve_payer_client(payer_clients, supabase, uid)
        if llm is None:
            continue  # BYOK require-mode without a key — defer (logged inside)
        try:
            resp = await poll_db_read(
                supabase,
                lambda c, tid=target.id: (
                    c.table("scores")
                    .select(
                        "recency_score, jobs!inner(id, title, description_html, "
                        "cataloged_at, archived_at, purged_at, is_us, role_family)"
                    )
                    .eq("target_id", tid)
                    .eq("promising", True)
                    .eq("excluded", False)
                    .eq("scoring_status", "stage2")
                    .is_("jobs.archived_at", "null")
                    .is_("jobs.purged_at", "null")
                    .not_.is_("jobs.is_us", "false")
                    .order("recency_score", desc=True, nullsfirst=False)
                    .limit(limit)
                ),
                label="poll grade-backfill select",
            )
        except Exception:
            logger.exception("Grade backfill: select failed for target %s; skipping", target.id)
            continue
        stale_jobs = [
            cast(dict[str, Any], r["jobs"]) for r in cast(list[dict[str, Any]], resp.data or [])
        ]
        if not stale_jobs:
            continue
        logger.info(
            "Grade backfill: grading up to %d stale promising job(s) for target %s",
            len(stale_jobs),
            target.id,
        )
        try:
            await run_phase2_for_jobs(
                supabase,
                llm,
                target=target,
                payload=user_optimized[uid].payload,
                jobs=stale_jobs,
                user_id=uid,
            )
        except Exception:
            logger.exception("Grade backfill failed for target %s", target.id)
            continue


def _resolve_payer_client(
    cache: dict[str | None, LLMClient | None],
    supabase: Client,
    payer_user_id: str | None,
) -> LLMClient | None:
    """Per-payer LLM client for background grading (#5 P3 BYOK).

    Each payer's background LLM work bills the payer's own OpenRouter key
    (via ``llm.get_client``), not the instance key. Memoized by payer for
    the duration of one source poll, so the N targets/jobs a payer owns
    reuse a single client — one key decrypt, and that payer's calls stay
    grouped rather than interleaved across keys (interleaving would
    cold-start each key's prompt cache).

    Returns ``None`` when the payer can't be served: hosted
    ``BYOK_REQUIRE_USER_KEYS`` with no stored key (``MissingUserKeyError``).
    Callers defer that payer's grading — jobs stay promising / score NULL
    and grade on a later cycle once a key is added — exactly like the
    over-allowance defer, never billing the operator key for a stranger.
    A ``None``/unattributable payer resolves to the instance key, unchanged
    from P2.
    """
    if payer_user_id not in cache:
        try:
            cache[payer_user_id] = get_llm_client(supabase, payer_user_id)
        except MissingUserKeyError:
            logger.info(
                "Background grading deferred for payer %s "
                "(no BYOK key; BYOK_REQUIRE_USER_KEYS set)",
                payer_user_id,
            )
            cache[payer_user_id] = None
    return cache[payer_user_id]


async def _poll_one_source(
    source: dict[str, Any],
    supabase: Client,
    budget_gate: PayerBudgetGate | None = None,
    *,
    active_targets: list[JobTarget] | None = None,
    stage3_users: tuple[dict[str, JobTarget], dict[str, OptimizedDoc]] | None = None,
) -> dict[str, Any]:
    """Poll a single job source. Returns a per-source summary dict.

    Three-stage scoring pipeline:
      1. Title-only match against each active target (inline, fast)
      2. Full JD match for stage-1 matches (async, after upsert)
      3. LLM analysis for top stage-2 scores (async)

    ``budget_gate`` is the per-cycle payer/allowance snapshot (built once
    by the cycle entry points); when None it's computed locally so direct
    callers and existing tests keep working.

    ``active_targets`` and ``stage3_users`` are likewise cycle-wide
    constants — callers polling many sources should resolve them once and
    pass them in rather than paying one targets query plus one user/doc
    resolution per source. Both fall back to a per-source fetch when
    omitted.
    """
    summary: dict[str, Any] = {
        "polled": False,
        "new": 0,
        "updated": 0,
        "archived": 0,
        "error": None,
    }
    company_name: str = source.get("company_name", "?")

    try:
        board_token: str = source["board_token"]
        source_id: str = source["id"]
        provider: str = source.get("provider", "greenhouse")

        fetcher = FETCHERS.get(provider)
        if not fetcher:
            summary["error"] = f"{company_name}: unknown provider '{provider}'"
            return summary

        jobs = await fetcher(board_token)
        summary["polled"] = True

        # Collect ALL external IDs from the API (before title/location filtering)
        # so we don't archive jobs that exist on the board but don't match filters.
        all_external_ids: set[str] = {job.external_id for job in jobs}

        # Targets are normally resolved once per cycle by the caller; the
        # fallback keeps direct/legacy callers working.
        if active_targets is None:
            active_targets = await asyncio.to_thread(get_active_target, supabase)

        # Existing rows are needed in three places: skipping Phase 1
        # triage for already-known jobs, the (company, title) dedupe, and
        # stale-row archiving. Fetch once, up front.
        #
        # Per-user translation of the old saved/applied/archived skip (#75
        # C4: jobs.status was dropped). The old filter excluded user-engaged
        # ('saved'/'applied') and already-dead ('archived') jobs from the
        # stale-archive pass. Now: 'archived' is the global archived_at gate,
        # and 'saved'/'applied' become "any user engaged with it" — a
        # user_jobs row with status != 'new" — mirroring url_health.
        #
        # The engaged-id exclusion is a server-side NOT EXISTS anti-join (#93):
        # we no longer pull the full engaged-id set into Python and exclude it
        # via `.not_.in_`, which built an ever-growing request URL that
        # PostgREST silently truncates as user_jobs fills. The engaged set now
        # never leaves Postgres. `source_live_unengaged_jobs` returns exactly
        # the live (archived_at IS NULL), unengaged jobs for this source with
        # the same columns existing_rows is read for below.
        existing_resp = await poll_db_read(
            supabase,
            lambda c: c.rpc("source_live_unengaged_jobs", {"p_source_id": source_id}),
            label=f"poll existing {company_name}",
            retry_sync=True,
        )
        existing_rows = cast(list[dict[str, Any]], existing_resp.data or [])
        # #514: admission scoping keys on EVERY external_id this source
        # already has, not the live-unengaged view above. Engaged
        # (saved/applied) and archived rows are still KNOWN rows — they were
        # admitted on a prior cycle, so they must neither re-enter Phase-1
        # triage as candidates nor be judged by its budget-defer rule below.
        # The RPC view keeps its narrower scope for the (company, title)
        # dedupe and the stale-archive pass, where engaged/archived rows are
        # deliberately out of bounds.
        known_ids_resp = await poll_db_read(
            supabase,
            lambda c: c.table("jobs").select("external_id").eq("source_id", source_id),
            label=f"poll known ids {company_name}",
            retry_sync=True,
        )
        known_external_ids = {
            r.get("external_id") for r in cast(list[dict[str, Any]], known_ids_resp.data or [])
        }

        # Payer/allowance snapshot: who pays for each target's LLM work,
        # and which payers are over their monthly allowance. Built once
        # per cycle by the entry points; locally as a fallback.
        gate = budget_gate
        if gate is None:
            try:
                gate = await asyncio.to_thread(
                    build_budget_gate, supabase, [t.id for t in active_targets]
                )
            except Exception:
                logger.exception(
                    "Budget gate build failed for %s — deferring LLM work",
                    company_name,
                )
                gate = PayerBudgetGate()

        # BYOK (#5 P3): each payer's background grading bills the payer's
        # own OpenRouter key. Resolved + memoized per payer across this
        # source's Phase 1/2/3 work via ``_resolve_payer_client``.
        payer_clients: dict[str | None, LLMClient | None] = {}

        # Phase 1: per-target LLM binary title triage (replaces cosine
        # prefilter). See ``app/services/relevance/title_triage.py``.
        # Verdicts: phase1_verdicts[target.id][1-based job idx] -> bool.
        # Missing entries treated as fail-open admit. Behind a feature
        # flag so the PR can ship dark; when flag is False the gate
        # admits everything (pass-through) and we rely on downstream
        # keyword scoring for filtering.
        #
        # Only NEW external_ids are triaged. Jobs already in the DB were
        # admitted on a previous cycle; re-triaging them re-paid the LLM
        # cost for the same titles on every poll. Known jobs simply have
        # no verdict entry, which the fail-open gate treats as admit.
        #
        # Only FREE-GATE SURVIVORS are triaged. The per-job loop below
        # drops title-prematch misses and non-US locations regardless of
        # their Phase 1 verdict, so paying Haiku to classify them was
        # pure waste (the bulk of the June 5-7 triage bill). Non-survivors
        # simply have no verdict entry — fail-open admit at the Phase 1
        # gate, then dropped at the free gates exactly as before.
        phase1_verdicts: dict[str, dict[int, TitleVerdict]] = {}
        # Per target, the set of global job idxs actually SENT to triage this
        # cycle. Drives the budget-defer decision in _phase1_promising: a
        # missing verdict for an attempted job fail-opens (LLM hiccup); a
        # missing verdict for a NON-attempted job was budget-deferred → defer.
        phase1_attempted: dict[str, set[int]] = {}
        triage_candidates = [
            (idx, job)
            for idx, job in enumerate(jobs)
            if job.external_id not in known_external_ids and _passes_free_gates(job, active_targets)
        ]
        # Grade the most RECENT first (#285 f/u): if the daily budget truncates
        # triage mid-cycle, the deferred tail should be the OLDEST postings, not
        # the newest. Global idx travels with each row, so verdict mapping is
        # unaffected. Undated rows sort last.
        triage_candidates.sort(
            key=lambda c: normalize_posted_at(c[1].posted_at) or "", reverse=True
        )
        if settings.phase1_triage_enabled and active_targets and triage_candidates:
            for active_target in active_targets:
                if gate.target_blocked(active_target.id):
                    # Sponsored target whose payer is blocked (over
                    # allowance / idle / disabled) — spend nothing. Empty
                    # verdicts → fail-open admit, so jobs still ingest
                    # (promising, score=NULL) and get graded once the
                    # payer's window frees up. Same defer semantics as the
                    # Phase 2 daily cap. Catalog targets (no active user
                    # link) are never blocked here — their triage bills
                    # the instance key via ``_resolve_payer_client(None)``.
                    phase1_verdicts[active_target.id] = {}
                    phase1_attempted[active_target.id] = set()  # nothing triaged → all defer
                    logger.info(
                        "Phase 1 deferred for target %s (payer %s blocked: "
                        "over allowance / idle / disabled)",
                        active_target.id,
                        gate.payer_for(active_target.id),
                    )
                    continue
                # BYOK (#5 P3): grade on the payer's own key. No key in
                # hosted require-mode → defer like over-allowance above
                # (empty verdicts → fail-open ingest, grade once a key is
                # added).
                payer = gate.payer_for(active_target.id)
                llm = _resolve_payer_client(payer_clients, supabase, payer)
                if llm is None:
                    phase1_verdicts[active_target.id] = {}
                    phase1_attempted[active_target.id] = set()  # nothing triaged → all defer
                    logger.info(
                        "Phase 1 deferred for target %s (payer %s has no BYOK key)",
                        active_target.id,
                        payer,
                    )
                    continue
                # Chunk to the model-aware batch cap per call (250 on Haiku,
                # tighter on deepseek whose ~8K output ceiling a full batch's
                # verdict JSON would overflow). Sources usually return well
                # under one batch (10-200 jobs); larger sources spread cost
                # across multiple calls.
                batch_cap = phase1_batch_size()
                target_verdicts: dict[int, TitleVerdict] = {}
                attempted_here: set[int] = set()
                # Negative-verdict cache (#514): titles this target's LLM
                # rejected within the TTL skip the model and re-enter as a
                # synthetic promising=False verdict, marked attempted — the
                # downstream gates treat them exactly like a fresh "no"
                # (rejected, not budget-deferred). Only the remainder is
                # actually sent.
                send_candidates: list[tuple[int, StandardJob]] = []
                for cand_idx, cand_job in triage_candidates:
                    if _phase1_cached_rejection(active_target, cand_job.title):
                        global_idx = cand_idx + 1
                        target_verdicts[global_idx] = TitleVerdict(id=global_idx, promising=False)
                        attempted_here.add(global_idx)
                    else:
                        send_candidates.append((cand_idx, cand_job))
                titles = [job.title for _, job in send_candidates]
                for start in range(0, len(titles), batch_cap):
                    # Re-check the global daily cap before each batch (#60
                    # overspend fix). The per-cycle gate above trips the
                    # breaker once at cycle start, but a long backlog cycle
                    # can cross the cap mid-run; re-reading here bounds
                    # overshoot to one batch. Stop spending for this target;
                    # verdicts already collected stand, the rest fail-open
                    # admit (same defer semantics as ``target_blocked``).
                    if await _triage_budget_blocks(supabase):
                        logger.warning(
                            "Phase 1 triage: global daily LLM cap reached "
                            "($%.2f) mid-cycle — deferring remaining titles "
                            "for target %s",
                            settings.global_llm_daily_budget_usd,
                            active_target.id,
                        )
                        break
                    batch = titles[start : start + batch_cap]
                    verdicts, result = await triage_titles(llm, target=active_target, titles=batch)
                    if result is not None:
                        # A REAL LLM response → mark these titles attempted (a dropped
                        # verdict is a hiccup, so it still fail-opens). A FAILED call
                        # (result None: OpenRouter 401 / spent credit-limit / error)
                        # leaves them UN-attempted → they DEFER, not fail-open admit,
                        # and re-triage once the LLM is healthy. A dead key / spent cap
                        # must PAUSE the pipeline, never flood 100% admit.
                        for sp in range(start, min(start + len(batch), len(send_candidates))):
                            attempted_here.add(send_candidates[sp][0] + 1)
                        try:
                            record_llm_cost(
                                supabase,
                                user_id=payer,
                                purpose=PHASE1_PURPOSE,
                                result=result,
                                metadata={
                                    "target_id": active_target.id,
                                    "source": company_name,
                                    "batch_size": len(batch),
                                },
                            )
                        except Exception:
                            logger.exception(
                                "Failed to record Phase 1 cost for target %s",
                                active_target.id,
                            )
                    # Shift batch-local ids (1-based within the SENT subset)
                    # to global 1-based job indices via the send-candidate
                    # mapping. A raw promising=False lands in the negative
                    # cache so the next cycle skips the model for this title.
                    # Only outright rejections are cached — a low-confidence
                    # promising verdict may be gated out by ``admitted()``
                    # today, but the confidence threshold is a live setting
                    # and re-judging borderline titles is the cheap side of
                    # that trade.
                    for batch_idx, verdict in verdicts.items():
                        subset_pos = start + batch_idx - 1  # 0-based
                        if 0 <= subset_pos < len(send_candidates):
                            global_idx = send_candidates[subset_pos][0] + 1
                            target_verdicts[global_idx] = verdict
                            if not verdict.promising:
                                _phase1_record_rejection(
                                    active_target, send_candidates[subset_pos][1].title
                                )
                phase1_verdicts[active_target.id] = target_verdicts
                phase1_attempted[active_target.id] = attempted_here

        def _any_target_admits(global_job_idx: int) -> bool:
            """``global_job_idx`` is 1-based (matches Phase 1's id contract).

            Admit iff a target that ACTUALLY TRIAGED this job admits it — a
            real promising verdict, or a missing verdict for a job we DID send
            to triage (an LLM hiccup, still fail-open). A target that
            budget-DEFERRED the job (never triaged it) does NOT contribute an
            admit, so a job deferred by every target is dropped from the upsert
            and re-triaged next cycle rather than admitted blind (#285 f/u).
            """
            if not phase1_verdicts:
                return True  # gate disabled or no targets — admit
            for tid, target_verdicts in phase1_verdicts.items():
                if global_job_idx not in phase1_attempted.get(tid, set()):
                    continue  # budget-deferred for this target → no admit
                v = target_verdicts.get(global_job_idx)
                if v is None or v.promising:
                    return True
            return False

        rows_to_upsert: list[dict[str, Any]] = []
        # Parallel map: external_id → 1-based Phase 1 idx. Stage 2 uses
        # this to look up per-(job, target) verdicts after the upsert
        # has resolved DB ids. Kept out of the upsert payload because
        # the jobs table doesn't have a column for it.
        phase1_idx_by_external_id: dict[str, int] = {}
        # Pre-DB drop counters for #845 funnel diagnostics. Order
        # matches the gate order below — first miss wins, mutually
        # exclusive. The FREE gates run before the Phase 1 check now
        # (mirroring the triage-candidate pre-filter above), so
        # ``dropped_phase1`` counts only free-gate survivors the LLM
        # actually rejected; free-gate misses land in their own
        # counters whether or not Phase 1 ever saw them. Emitted as a
        # single `poll_funnel` log line at end of cycle so an operator
        # can grep one source's funnel without a DB pass.
        dropped_phase1 = 0
        dropped_title_prematch = 0
        dropped_non_us = 0
        for idx, job in enumerate(jobs):
            # Filter by target relevance instead of static keyword list. With
            # NO active targets there is nothing to match against, so drop
            # everything (previously the `active_targets and` guard SKIPPED this
            # gate when empty, ingesting whole boards of untargeted roles).
            if not _title_matches_any_target(job.title, active_targets):
                dropped_title_prematch += 1
                continue
            if not _is_us_location(job.location_name):
                dropped_non_us += 1
                continue
            # Phase 1 IDs are 1-based; the per-target verdict-check
            # below uses the same idx + 1 convention. Non-survivors
            # never reach this line, so their missing verdicts can't
            # fail-open anything into the upsert.
            #
            # KNOWN rows bypass the gate (#514): they were admitted on the
            # cycle that ingested them and are never triage candidates, so
            # to ``_any_target_admits`` they are indistinguishable from
            # budget-deferred candidates — judging them dropped every known
            # row from the conflict-update whenever a cycle produced any
            # verdicts, starving busy boards of content refreshes entirely
            # (JD edits, the escaped-HTML heal, salary re-extraction).
            if job.external_id not in known_external_ids and not _any_target_admits(idx + 1):
                dropped_phase1 += 1
                continue

            salary = job.salary_text or extract_salary_from_html(job.content)
            loc = parse_location(job.location_name)

            phase1_idx_by_external_id[job.external_id] = idx + 1
            rows_to_upsert.append(
                {
                    "external_id": job.external_id,
                    "source_id": source_id,
                    "title": job.title,
                    "company_name": company_name,
                    "location": job.location_name,
                    "city": loc.city,
                    "state": loc.state,
                    "country": loc.country,
                    "location_remote": loc.remote,
                    "description_html": sanitize_html(job.content),
                    "absolute_url": job.absolute_url,
                    "source_posted_at": normalize_posted_at(job.posted_at),
                    "salary_text": salary,
                    **salary_columns(salary),
                }
            )

        # Optional: validate job URLs before upserting (#496). NEW rows only
        # (#514): a known row's URL was validated at ingest and url_health
        # monitors it from then on — re-validating every known row per cycle
        # is a fleet-wide HEAD storm, and a transient upstream blip would
        # null a working absolute_url on refresh.
        if settings.validate_poll_urls and rows_to_upsert:
            fresh = [r for r in rows_to_upsert if r.get("external_id") not in known_external_ids]
            kept = [r for r in rows_to_upsert if r.get("external_id") in known_external_ids]
            rows_to_upsert = (await _validate_rows(fresh)) + kept

        # #503: JSON-LD baseSalary fallback for new salary-less rows
        # (flag-gated, bounded; no-op by default).
        if rows_to_upsert:
            await _fill_jsonld_salaries(rows_to_upsert, known_external_ids)

        new_rows: list[dict[str, Any]] = []
        if rows_to_upsert:
            # Dedupe rows_to_upsert by (company, title). Both within
            # the current batch and against existing rows that have a
            # different external_id. This catches the case Greenhouse
            # surfaces the same role under multiple location offices as
            # separate listings (e.g. Smartsheet's "Professional Services
            # Business Development Director" at "-REMOTE, USA-" +
            # "Bellevue, WA, USA").
            rows_to_upsert = _dedupe_by_content(
                rows_to_upsert,
                existing=existing_rows,
                source=company_name,
            )

        # Tombstoned (purged) postings must not be resurrected by the
        # conflict-update (archival Stage 2).
        if rows_to_upsert:
            rows_to_upsert = await _drop_purged_rows(supabase, source_id, rows_to_upsert)

        # Re-check after dedupe: it can remove EVERY row (a source whose
        # postings are all cross-posting dupes of existing rows). Calling
        # ``.upsert([])`` then raises PGRST100 "failed to parse columns
        # parameter ()", which marks the poll failed and — after
        # ``source_failure_disable_threshold`` consecutive empties — would
        # auto-disable a perfectly healthy source. Skip the write instead.
        if rows_to_upsert:
            upsert_resp = await poll_db_write(
                supabase,
                lambda c: c.table("jobs").upsert(
                    rows_to_upsert, on_conflict="source_id,external_id"
                ),
                label=f"poll upsert {company_name}",
            )
            # #514: a row is a REFRESH iff its external_id was known BEFORE
            # this cycle's upsert. NOT derivable from created_at ==
            # updated_at: nothing bumps jobs.updated_at on a conflict-update
            # (no moddatetime trigger; the payload doesn't set it), so a
            # refreshed row keeps created == updated forever and the old
            # timestamp split misclassified every refresh as "new" — which
            # would re-fire the new-job email/SMS alerts below on EVERY
            # cycle. The Stage-2 pass preserves the persisted Phase-1
            # verdict for refreshed rows instead of re-litigating admission
            # with this cycle's (absent) verdict.
            known_upserted_ids: list[str] = []
            for raw_row in upsert_resp.data or []:
                data = cast(dict[str, Any], raw_row)
                if data.get("external_id") in known_external_ids:
                    summary["updated"] += 1
                    known_upserted_ids.append(data["id"])
                else:
                    summary["new"] += 1
                    new_rows.append(data)

            # ---- Qualification firewall (#60) ----
            # Target-INDEPENDENT tagging: classify each upserted job ONCE and
            # write the intrinsic tags onto its row, so per-target scoring
            # below can pre-filter cheaply. Runs AFTER the US filter (every
            # row here is a free-gate survivor) and BEFORE per-target scoring.
            # Best-effort and flag-gated: failures are swallowed so a tagger
            # outage never breaks polling, and nothing runs unless
            # ``qualification_enabled`` is set (no LLM spend by default).
            if settings.qualification_enabled and upsert_resp.data:
                await _qualify_jobs(
                    supabase,
                    [cast(dict[str, Any], r) for r in upsert_resp.data],
                )

            # Job embeddings are LAZY now (Disk IO slim-down, 2026-07-30):
            # no embed-on-ingest — ``ensure_job_vectors`` in the Phase-2
            # runner materializes vectors for exactly the candidate set
            # about to be read ("only a few will ever be read").

            # ---- Stage 1: Title scoring per target ----
            for active_target in active_targets:

                async def _title_score_one(
                    row_data: dict[str, Any], target: JobTarget = active_target
                ) -> None:
                    try:
                        await target_title_score_and_upsert(
                            supabase,
                            job_posting_id=row_data["id"],
                            title=row_data.get("title", ""),
                            target=target,
                        )
                    except Exception:
                        logger.exception("Stage 1 scoring failed for job %s", row_data.get("id"))

                await asyncio.gather(
                    *(_title_score_one(cast(dict[str, Any], r)) for r in upsert_resp.data or [])
                )

            # ---- Stage 2: Full JD scoring per target (async) ----
            # Pre-parse each JD once, reuse across all targets
            jd_cache: dict[str, Any] = {}
            for raw_row in upsert_resp.data or []:
                rd = cast(dict[str, Any], raw_row)
                jd_cache[rd["id"]] = parse_jd(rd.get("description_html") or "")

            for active_target in active_targets:
                # Per-target Phase 1 verdicts (None when flag off): keyed by
                # the 1-based job idx assigned during the candidate-build
                # loop above. Each row in upsert_resp carries an
                # ``external_id`` we look up to get the idx, then to get the
                # verdict. Missing entries are fail-open (admit).
                target_verdicts = phase1_verdicts.get(active_target.id, {})

                # #514: persisted Phase-1 verdicts for the refreshed rows.
                # ``promising=False`` is the exclusion floor on re-score
                # (``bulk_score_for_target``'s contract — a refresh must not
                # walk back a Phase-1 rejection); True/NULL rely on the
                # keyword scorer. Chunked ≤150 ids to stay inside
                # PostgREST's URL-safe ``in_`` bound.
                promising_floor: dict[str, bool | None] = {}
                for start in range(0, len(known_upserted_ids), 150):
                    chunk = known_upserted_ids[start : start + 150]
                    floor_resp = await poll_db_read(
                        supabase,
                        lambda c, _chunk=chunk, _tid=active_target.id: (
                            c.table("scores")
                            .select("job_posting_id, promising")
                            .eq("target_id", _tid)
                            .in_("job_posting_id", _chunk)
                        ),
                        label=f"poll stage2 floor {company_name}",
                        retry_sync=True,
                    )
                    for floor_row in cast(list[dict[str, Any]], floor_resp.data or []):
                        promising_floor[floor_row["job_posting_id"]] = floor_row.get("promising")

                async def _full_score_one(
                    row_data: dict[str, Any],
                    target: JobTarget = active_target,
                    verdicts: dict[int, TitleVerdict] = target_verdicts,
                    floor: dict[str, bool | None] = promising_floor,
                ) -> None:
                    try:
                        ext_id = row_data.get("external_id", "")
                        promising: bool | None
                        if ext_id in known_external_ids:
                            # #514: refresh of an already-ingested row — Phase 1
                            # does not re-litigate admission. ``promising=False``
                            # is the persisted exclusion floor; the stored
                            # verdict/confidence columns stay untouched (None
                            # args = leave-unchanged upsert semantics).
                            promising = floor.get(row_data["id"]) is not False
                            promising_arg: bool | None = None
                            phase1_confidence: int | None = None
                        else:
                            phase1_idx = phase1_idx_by_external_id.get(ext_id)
                            verdict = verdicts.get(phase1_idx) if phase1_idx is not None else None
                            # Gate admission on confidence (#47) AND budget-deferral
                            # (#285 f/u): a missing verdict fail-opens ONLY if this
                            # target actually triaged the job; a job the budget
                            # deferred (never triaged) → promising=None = defer
                            # (excluded now, re-triaged after the budget resets),
                            # never admit-blind.
                            attempted_here = (
                                phase1_idx is not None
                                and phase1_idx in phase1_attempted.get(target.id, set())
                            )
                            promising = _phase1_promising(
                                verdict,
                                attempted=attempted_here,
                                gate_active=bool(phase1_verdicts),
                                min_confidence=settings.phase1_min_confidence,
                            )
                            promising_arg = promising if phase1_verdicts else None
                            phase1_confidence = verdict.confidence if verdict is not None else None
                        await target_score_and_upsert(
                            supabase,
                            job_posting_id=row_data["id"],
                            title=row_data.get("title", ""),
                            description_html=row_data.get("description_html", ""),
                            target=target,
                            parsed_jd=jd_cache.get(row_data["id"]),
                            excluded_by_prefilter=not promising,
                            promising=promising_arg,
                            phase1_confidence=phase1_confidence,
                        )
                    except Exception:
                        logger.exception("Stage 2 scoring failed for job %s", row_data.get("id"))

                await asyncio.gather(
                    *(_full_score_one(cast(dict[str, Any], r)) for r in upsert_resp.data or [])
                )

            # ---- Stage 3: LLM scoring for qualified jobs (concurrent) ----
            # Each user with an active target gets one LLM analysis per
            # job, using their personal optimized doc. Previously this
            # fetched a single ``user_id IS NULL`` optimized doc which
            # has never existed in production — so stage 3 silently
            # no-op'd since the multi-user migration.
            #
            # Targets are global rows (no user_id column); the user link
            # lives on ``user_targets``. One query maps active target IDs
            # to their owning users, then we group: per user pick the
            # first active target and that user's latest optimized doc.
            if stage3_users is not None:
                primary_by_user, user_optimized = stage3_users
            else:
                (
                    primary_by_user,
                    user_optimized,
                ) = await _resolve_user_targets_for_stage3(supabase, active_targets, company_name)

            if settings.phase2_enabled and primary_by_user:
                # ---- Phase 2: LLM job-fit grading (#6) ----
                # Replaces the legacy Stage 3 keyword+LLM blend with the
                # Sonnet scorecard. ``run_phase2_for_jobs`` gates on the
                # Phase 1 ``promising`` verdict, honours the re-grade
                # contract, enforces the per-target daily cap, and applies
                # progressive batching. We re-aggregate the global
                # ``jobs.score`` afterwards because Phase 2 rewrites
                # ``scores.score`` (Stage 2's keyword value was a
                # placeholder until graded).
                cycle_rows = [cast(dict[str, Any], r) for r in upsert_resp.data or []]
                for uid, p2_target in primary_by_user.items():
                    if gate.user_blocked(uid):
                        # Over monthly allowance — defer. Jobs keep
                        # promising=True/score=NULL and get graded when
                        # the rolling window frees up.
                        logger.info(
                            "Phase 2 deferred for user %s / target %s (over monthly allowance)",
                            uid,
                            p2_target.id,
                        )
                        continue
                    # BYOK (#5 P3): grade on this user's own key; no key in
                    # hosted require-mode defers like over-allowance.
                    llm = _resolve_payer_client(payer_clients, supabase, uid)
                    if llm is None:
                        logger.info(
                            "Phase 2 deferred for user %s / target %s (no BYOK key)",
                            uid,
                            p2_target.id,
                        )
                        continue
                    try:
                        await run_phase2_for_jobs(
                            supabase,
                            llm,
                            target=p2_target,
                            payload=user_optimized[uid].payload,
                            jobs=cycle_rows,
                            user_id=uid,
                        )
                    except Exception:
                        logger.exception(
                            "Phase 2 grading failed for user %s / target %s",
                            uid,
                            p2_target.id,
                        )
            # Ids of every row touched this cycle — feeds the recency
            # refresh (the legacy global-score recompute that shared this
            # list was retired in R2, schema audit Group A).
            stage2_ids = [cast(dict[str, Any], r)["id"] for r in upsert_resp.data or []]

            # ---- Recency decay refresh (#5) ----
            # Re-derive ``scores.recency_score`` for every row touched
            # this cycle from the job's age, now that the fit scores are
            # settled. Gated on the flag so a disabled rollout skips the
            # extra writes — recency_score already mirrors score from the
            # upsert in that case, so the list sort is unaffected.
            if settings.recency_decay_enabled and stage2_ids:
                try:
                    await refresh_recency_scores_poll(supabase, stage2_ids)
                except Exception:
                    logger.exception("Recency refresh failed for %s", company_name)

        # Identify stale jobs no longer on the board
        stale_ids: list[str] = []
        if not jobs and existing_rows:
            # Mass-archive guard: several fetchers (workday in particular)
            # swallow API errors and return [] instead of raising, which is
            # indistinguishable from "the board emptied out". Archiving
            # everything on a zero-job fetch turns a transient upstream
            # hiccup into a wiped source, so we skip the stale pass and
            # leave the rows for a cycle where the fetch returns data.
            # Genuinely emptied boards stop producing new rows immediately;
            # their leftover rows age out via recency scoring instead.
            logger.warning(
                "poll %s returned 0 jobs but %d active rows exist — "
                "skipping stale-archive pass (suspected fetch failure)",
                company_name,
                len(existing_rows),
            )
        else:
            for existing_job in existing_rows:
                if existing_job["external_id"] not in all_external_ids:
                    stale_ids.append(existing_job["id"])

        # Archive stale jobs AND update last_polled_at in parallel.
        # A successful poll also resets the failure-backoff counter.
        mark_polled_payload: dict[str, Any] = {
            "last_polled_at": datetime.now(UTC).isoformat(),
            "job_count": len(jobs),
            "consecutive_failures": 0,
            # A clean poll clears the stored failure cause so last_error
            # reflects only live problems (queryable signal, not history).
            "last_error": None,
            "last_error_at": None,
        }
        # Adaptive cadence: a non-empty upsert batch means this source
        # produced at least one ingestible candidate this cycle. The
        # lifecycle sweep stretches sources whose stamp goes cold to a
        # daily interval and restores them once they produce again.
        if rows_to_upsert:
            mark_polled_payload["last_candidate_at"] = datetime.now(UTC).isoformat()

        def _mark_polled_query(c: Any) -> Any:
            return c.table("sources").update(mark_polled_payload).eq("id", source_id)

        if stale_ids:
            # Flag stale/delisted jobs globally-dead via archived_at (#75 C3
            # — global liveness, distinct from per-user jobs.status).
            # ``stale_ids`` scales with a source's active-row count, so they
            # ride in the ``archive_jobs_by_ids`` RPC's ``p_ids`` jsonb body
            # rather than an ``id=in.(...)`` URL filter — no URL-length limit,
            # one set-based UPDATE instead of N chunks (#93). The RPC stamps a
            # single ``now()`` across every id (matching the single big-UPDATE
            # semantics: one shared timestamp for all archived rows) and writes
            # the same ``archived_at`` + ``updated_at`` the chunked path wrote.
            #
            # Both writes are idempotent (UPDATE with stable WHERE), so a
            # retry after a stream drop is safe.
            await asyncio.gather(
                poll_db_write(
                    supabase,
                    lambda c: c.rpc("archive_jobs_by_ids", {"p_ids": stale_ids}),
                    label=f"poll archive {company_name}",
                ),
                poll_db_write(
                    supabase,
                    _mark_polled_query,
                    label=f"poll mark-polled {company_name}",
                ),
            )
            summary["archived"] = len(stale_ids)
        else:
            await poll_db_write(
                supabase,
                _mark_polled_query,
                label=f"poll mark-polled {company_name}",
            )

        # Fire email + SMS alerts for newly-inserted high-scoring jobs.
        if new_rows:
            # ``new_rows`` was captured from the upsert response BEFORE any
            # scoring ran, so ``score`` there is the column default 0 — which
            # failed every alert threshold and meant no alert ever fired.
            # Re-read the rows now that the stages have written final scores.
            alert_rows = await _load_alert_rows(supabase, new_rows)
            try:
                await notify.send_alerts_for_new_jobs(supabase, alert_rows)
            except Exception:
                logger.exception("Email alert dispatch raised for %s", company_name)
            try:
                await notify.send_sms_alerts_for_new_jobs(supabase, alert_rows)
            except Exception:
                logger.exception("SMS alert dispatch raised for %s", company_name)

        # Funnel diagnostics for #845. One structured line per source per
        # cycle so an operator can `grep poll_funnel | grep <Company>` in
        # Railway and read where jobs are dropping pre-DB. The counts
        # are mutually exclusive — first gate to fire wins per job.
        per_target_phase1_no = {
            tid: sum(1 for v in verdicts.values() if not v.promising)
            for tid, verdicts in phase1_verdicts.items()
        }
        logger.info(
            "poll_funnel source=%s fetched=%d dropped_phase1=%d "
            "dropped_title_prematch=%d dropped_non_us=%d candidates=%d "
            "upserted_new=%d upserted_updated=%d archived=%d "
            "phase1_no_by_target=%s",
            company_name,
            len(jobs),
            dropped_phase1,
            dropped_title_prematch,
            dropped_non_us,
            len(rows_to_upsert),
            summary["new"],
            summary["updated"],
            summary["archived"],
            per_target_phase1_no or "{}",
        )

    except Exception as exc:
        logger.exception("Poll failed for %s", company_name)
        summary["error"] = f"{company_name}: poll failed"
        await _record_source_failure(supabase, source, error=repr(exc))

    return summary


# Truncate stored failure text so a giant traceback/HTML body can't bloat
# the row (the column is queryable signal, not a log store).
_SOURCE_LAST_ERROR_MAX_LEN = 500


async def _record_source_failure(
    supabase: Client, source: dict[str, Any], *, error: str | None = None
) -> None:
    """Failure backoff: count consecutive fetch failures per source, persist
    the failure cause, and auto-disable at the threshold (a dead board
    otherwise gets re-fetched every cycle forever).

    Persists ``last_error`` + ``last_error_at`` on every failure so the cause
    is queryable in SQL (it was previously only logged — which is why the
    outage was invisible). When the backoff flips ``enabled=false`` it also
    stamps ``disabled_at`` (drives auto-recovery) and fires a Sentry alert.
    Successful polls clear ``last_error``/``last_error_at`` and reset the
    counter via the ``last_polled_at`` update. Best-effort — never raises.
    """
    threshold = settings.source_failure_disable_threshold
    if threshold <= 0:
        return
    source_id = source.get("id")
    if not source_id:
        return
    company = source.get("company_name", source_id)
    try:
        failures = int(source.get("consecutive_failures") or 0) + 1
        now_iso = datetime.now(UTC).isoformat()
        updates: dict[str, Any] = {
            "consecutive_failures": failures,
            "last_error_at": now_iso,
        }
        if error:
            updates["last_error"] = error[:_SOURCE_LAST_ERROR_MAX_LEN]
        disabling = failures >= threshold
        if disabling:
            updates["enabled"] = False
            updates["disabled_at"] = now_iso
            logger.warning(
                "Source %s disabled after %d consecutive failures",
                company,
                failures,
            )
        await poll_db_write(
            supabase,
            lambda c: c.table("sources").update(updates).eq("id", source_id),
            label="poll source-failure update",
        )
        if disabling and settings.sentry_dsn:
            try:
                import sentry_sdk

                sentry_sdk.capture_message(
                    f"source auto-disabled after {failures} consecutive "
                    f"failures: {company} ({source_id}). "
                    f"last_error={updates.get('last_error') or 'n/a'}",
                    level="error",
                )
            except Exception:
                logger.exception("Failed to report source auto-disable to Sentry")
    except Exception:
        logger.exception("Failed to record source failure for %s", source_id)


async def recover_stale_sources(supabase: Client, *, now: datetime | None = None) -> int:
    """Auto-recovery: re-enable sources the backoff auto-disabled longer ago
    than ``source_recovery_after_hours``, resetting their failure counter so
    they get polled again.

    Without this, a transient ATS-wide outage that trips every source at once
    (exactly the Sept-2026 failure) keeps ingestion down forever — nobody
    flips ``enabled`` back. Runs from the poll cycle. Best-effort, never
    raises; returns the number of sources re-enabled.

    Only touches rows we auto-disabled (``disabled_at IS NOT NULL``), so an
    operator who manually disables a source (leaving ``disabled_at`` NULL) is
    never overridden.
    """
    cooldown_hours = settings.source_recovery_after_hours
    if cooldown_hours <= 0:
        return 0
    moment = now or datetime.now(UTC)
    cutoff = (moment - timedelta(hours=cooldown_hours)).isoformat()
    try:
        resp = await poll_db_write(
            supabase,
            lambda c: (
                c.table("sources")
                .update(
                    {
                        "enabled": True,
                        "consecutive_failures": 0,
                        "disabled_at": None,
                    }
                )
                .eq("enabled", False)
                .not_.is_("disabled_at", "null")
                .lt("disabled_at", cutoff)
            ),
            label="poll source auto-recovery",
        )
    except Exception:
        logger.exception("source auto-recovery sweep failed")
        return 0
    recovered = cast(list[dict[str, Any]], resp.data or [])
    if recovered:
        names = ", ".join(str(r.get("company_name") or r.get("id")) for r in recovered[:10])
        logger.info(
            "source auto-recovery: re-enabled %d source(s) disabled > %dh ago: %s%s",
            len(recovered),
            cooldown_hours,
            names,
            "..." if len(recovered) > 10 else "",
        )
    return len(recovered)


# Per-process dedup so the "approaching cap" warning fires once per UTC
# day rather than once per cycle (#26 F3). Keyed on the UTC date so a
# day rollover re-arms it; restart re-arms it (acceptable — one extra
# warning per restart per day is fine).
_GLOBAL_APPROACHING_DAY: str | None = None


def _global_budget_exhausted(supabase: Client, *, reserve_usd: float = 0.0) -> bool:
    """True when today's total LLM spend (ALL users, since UTC midnight)
    has reached ``global_llm_daily_budget_usd``. 0 disables (never True).

    ``reserve_usd`` fences off the top slice of the cap for higher-priority
    spenders: a caller that passes a reserve (the background qualification
    tagger) is "exhausted" once spend reaches ``cap - reserve_usd``, leaving
    that reserve in the daily meter for grading (Phase-1 triage + Phase-2 fit),
    which read the full cap. This is why the heavy background tagger can no
    longer drain the budget that live grading needs — the recurring starvation
    that left new jobs stuck at ``stage2`` (#60). It stays one meter / one cap;
    the reserve is just a lower effective ceiling for the low-priority spender.

    The lean predicate behind both the once-per-cycle circuit breaker
    (:func:`_global_circuit_breaker_tripped`) and the mid-run re-checks
    in long per-job LLM loops (:func:`_qualify_jobs`, the Phase-1 triage
    batch loops). One implementation so every LLM-spending path reads the
    SAME meter against the SAME cap — no parallel budget logic. Carries no
    side effects (warnings/Sentry live in the breaker) so it's safe to
    call repeatedly inside a loop.
    """
    cap = settings.global_llm_daily_budget_usd
    if cap <= 0:
        return False
    effective_cap = cap - reserve_usd
    if effective_cap <= 0:
        # The grading reserve consumes the whole cap → this low-priority
        # spender yields entirely, leaving the budget for grading.
        return True
    midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return total_llm_spend_all(supabase, since=midnight) >= effective_cap


async def _triage_budget_blocks(supabase: Client) -> bool:
    """Async, fail-OPEN global-budget check for the Phase-1 triage loops.

    Reads the meter off-thread (it's a sync DB call). Returns True only when
    the global daily cap is provably reached. A meter-read error fails OPEN
    (returns False → keep triaging) on purpose: triage is already protected
    by the once-per-cycle ``gate.target_blocked`` breaker, so this per-batch
    check is a best-effort tightening — a transient read blip must not break
    a poll or silently drop the precision filter. (Qualification, which has
    NO other gate, instead fails CLOSED in :func:`_qualify_jobs`.)

    Also honors the provider fast-fail breaker (audit PERF-M): if the tagger
    already tripped it on a 402/429 this cooldown, triage stops too — the same
    provider will reject its Phase-1 calls.
    """
    if _provider_fatal_active():
        return True
    try:
        return await asyncio.to_thread(_global_budget_exhausted, supabase)
    except Exception:
        logger.exception(
            "Phase 1 triage: global-budget read failed — continuing "
            "(fail-open; the per-cycle breaker still applies)"
        )
        return False


def _global_circuit_breaker_tripped(supabase: Client) -> bool:
    """True when today's total LLM spend (ALL users, since UTC midnight)
    has reached ``global_llm_daily_budget_usd``.

    Defense-in-depth above the per-payer monthly gates: a runaway cycle
    (bad prompt, bad batch math, many users at once) stops bleeding
    within one poll tick instead of within one user-month. 0 disables.

    Also emits an "approaching cap" Sentry warning at 80% so the operator
    sees the run-up before the breaker actually trips — by the time the
    trip event fires, the cycle has already deferred all LLM work (#26
    F3).
    """
    cap = settings.global_llm_daily_budget_usd
    if cap <= 0:
        return False
    now = datetime.now(UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    spent = total_llm_spend_all(supabase, since=midnight)
    if spent < cap:
        # Approaching-cap warning (#26 F3) — once per UTC day.
        if spent >= cap * 0.8:
            global _GLOBAL_APPROACHING_DAY
            day_key = now.date().isoformat()
            if day_key != _GLOBAL_APPROACHING_DAY:
                _GLOBAL_APPROACHING_DAY = day_key
                logger.warning(
                    "global LLM spend approaching cap: $%.4f / $%.2f (%.0f%%)",
                    spent,
                    cap,
                    spent / cap * 100,
                )
                if settings.sentry_dsn:
                    try:
                        import sentry_sdk

                        sentry_sdk.capture_message(
                            f"global LLM spend approaching daily cap: "
                            f"${spent:.4f} / ${cap:.2f} "
                            f"({spent / cap * 100:.0f}%)",
                            level="warning",
                        )
                    except Exception:
                        logger.exception("Failed to report approaching-cap warning to Sentry")
        return False
    logger.error(
        "global LLM circuit breaker tripped: $%.4f spent today >= $%.2f cap — "
        "deferring ALL LLM work this cycle (jobs still ingest)",
        spent,
        cap,
    )
    if settings.sentry_dsn:
        try:
            import sentry_sdk

            sentry_sdk.capture_message(
                f"global LLM circuit breaker tripped: ${spent:.4f} spent today "
                f">= ${cap:.2f} daily cap",
                level="error",
            )
        except Exception:
            logger.exception("Failed to report circuit breaker trip to Sentry")
    return True


async def _cycle_budget_gate(supabase: Client) -> tuple[PayerBudgetGate, bool]:
    """Build the payer/allowance snapshot once per poll cycle.

    Returns ``(gate, has_active_targets)`` — the active-target fetch is
    shared with the paid-provider skip (Firecrawl sources are pointless
    with no consumer). On any error returns an EMPTY gate, which blocks
    all targets' LLM work for the cycle (``target_blocked`` is True for
    unknown targets) — refuse to spend unattributed money rather than
    crash or fail open. Jobs still ingest; grading defers a cycle.
    ``has_active_targets`` fails True so a gate error never silently
    stops paid-source polling that a healthy cycle would run.

    The global circuit breaker check runs first: when today's spend
    across all users hits ``global_llm_daily_budget_usd`` the cycle gets
    the same EMPTY gate (defer everything, keep ingesting). A breaker
    *query* failure falls into the same except arm — refuse to spend
    when we can't see the meter.
    """
    try:
        active = await asyncio.to_thread(get_active_target, supabase)
        if await asyncio.to_thread(_global_circuit_breaker_tripped, supabase):
            return PayerBudgetGate(), bool(active)
        gate = await asyncio.to_thread(build_budget_gate, supabase, [t.id for t in active])
        return gate, bool(active)
    except Exception:
        logger.exception("Budget gate build failed — deferring all LLM work this cycle")
        return PayerBudgetGate(), True


# Last lifecycle sweep (time.monotonic). In-process is fine on the
# single-replica deploy: a restart just causes one harmless early re-run
# (the sweep is idempotent).
_LIFECYCLE_LAST_RUN: float = 0.0
LIFECYCLE_SWEEP_INTERVAL_S = 6 * 3600.0


async def _maybe_run_lifecycle_sweep(supabase: Client) -> None:
    """Run the idle-account sweep at most every 6h, never blocking polls."""
    global _LIFECYCLE_LAST_RUN

    from app.services.lifecycle import run_lifecycle_sweep

    now = time.monotonic()
    if _LIFECYCLE_LAST_RUN and now - _LIFECYCLE_LAST_RUN < LIFECYCLE_SWEEP_INTERVAL_S:
        return
    _LIFECYCLE_LAST_RUN = now
    try:
        await run_lifecycle_sweep(supabase)
    except Exception:
        logger.exception("Lifecycle sweep failed — continuing with poll cycle")


_ARCHIVAL_LAST_RUN: float = 0.0


async def _maybe_run_archival_sweep(supabase: Client) -> None:
    """Run the archival lifecycle sweep at most every 6h (UX/IA §5).

    Same throttle shape as the idle-account sweep above: piggybacks the
    scheduler tick, never blocks or fails a poll, flag-gated so it ships
    inert until the operator flips ``ARCHIVAL_SWEEP_ENABLED``.
    """
    global _ARCHIVAL_LAST_RUN

    from app.services.archival import run_archival_sweep

    if not settings.archival_sweep_enabled:
        return
    now = time.monotonic()
    if _ARCHIVAL_LAST_RUN and now - _ARCHIVAL_LAST_RUN < LIFECYCLE_SWEEP_INTERVAL_S:
        return
    _ARCHIVAL_LAST_RUN = now
    # ``run_archival_sweep`` is fail-soft internally; this wrapper only
    # exists for the throttle + flag gate.
    await run_archival_sweep(supabase)


def _drop_paid_sources_if_unconsumed(
    sources: list[dict[str, Any]], *, has_active_targets: bool
) -> list[dict[str, Any]]:
    """Skip paid 'crawl' (Firecrawl) sources when no targets are active.

    Free ATS fetchers keep the supply warm regardless; the paid provider
    only runs when at least one active target can consume the results.
    """
    if has_active_targets:
        return sources
    kept = [s for s in sources if s.get("provider") != "crawl"]
    skipped = len(sources) - len(kept)
    if skipped:
        logger.info("Skipping %d paid crawl source(s): no active targets", skipped)
    return kept


async def poll_all_sources(supabase: Client) -> PollResult:
    sources_resp = await poll_db_read(
        supabase,
        lambda c: c.table("sources").select("*").eq("enabled", True),
        label="poll sources read",
    )
    all_sources = cast(list[dict[str, Any]], sources_resp.data or [])

    # Cycle-wide constants resolved once instead of once per source:
    # active targets and the stage-3 (user → target/optimized-doc) maps.
    active_targets = await asyncio.to_thread(get_active_target, supabase)
    stage3_users = await _resolve_user_targets_for_stage3(
        supabase, active_targets, "(cycle prefetch)"
    )

    budget_gate, has_active = await _cycle_budget_gate(supabase)
    sources = _drop_paid_sources_if_unconsumed(all_sources, has_active_targets=has_active)
    semaphore = asyncio.Semaphore(POLL_CONCURRENCY)

    async def _worker(raw_source: Any) -> dict[str, Any]:
        async with semaphore:
            return await _poll_one_source(
                cast(dict[str, Any], raw_source),
                supabase,
                budget_gate,
                active_targets=active_targets,
                stage3_users=stage3_users,
            )

    summaries = await asyncio.gather(*(_worker(s) for s in sources))

    result = PollResult(sources_polled=0, new_jobs=0, updated_jobs=0, archived_jobs=0, errors=[])
    for s in summaries:
        if s["polled"]:
            result.sources_polled += 1
        result.new_jobs += s["new"]
        result.updated_jobs += s["updated"]
        result.archived_jobs += s["archived"]
        if s["error"]:
            result.errors.append(s["error"])

    return result


# ---- Due-source polling (cron entry point) ---------------------------------


# Fallback interval used when a source row predates the
# `poll_interval_minutes` column or has it set to NULL for any reason.
DEFAULT_POLL_INTERVAL_MINUTES = 240


def _is_due(source: dict[str, Any], now: datetime) -> bool:
    """Return True if the source should be polled this tick.

    A source is due when it has never been polled or when its
    ``last_polled_at + poll_interval_minutes`` is in the past.
    """
    last = source.get("last_polled_at")
    if not last:
        return True

    interval_min = source.get("poll_interval_minutes") or DEFAULT_POLL_INTERVAL_MINUTES
    try:
        last_dt = (
            datetime.fromisoformat(last.replace("Z", "+00:00")) if isinstance(last, str) else last
        )
    except (TypeError, ValueError):
        # Unparseable timestamp — treat as never-polled rather than
        # silently skipping the row forever.
        return True

    return last_dt + timedelta(minutes=int(interval_min)) <= now


def filter_due_sources(
    sources: list[dict[str, Any]], *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Pure helper for the due-filter — extracted so tests don't need Supabase."""
    moment = now or datetime.now(UTC)
    return [s for s in sources if _is_due(s, moment)]


async def poll_due_sources(supabase: Client) -> PollResult:
    """Poll only the sources whose interval has elapsed.

    Same shape as ``poll_all_sources`` but skips sources that were
    polled recently. Designed to be called from a frequent cron tick
    (e.g. every 30 min) without re-hammering boards that have a longer
    configured cadence.
    """
    # Auto-recovery first, so a source whose cooldown just elapsed is
    # re-enabled and picked up in THIS cycle rather than waiting a tick.
    # A transient ATS-wide outage that tripped every source can't keep
    # ingestion down forever (the Sept-2026 failure mode).
    await recover_stale_sources(supabase)

    sources_resp = await poll_db_read(
        supabase,
        lambda c: c.table("sources").select("*").eq("enabled", True),
        label="poll sources read",
    )
    all_enabled = cast(list[dict[str, Any]], sources_resp.data or [])

    # Idle-account housekeeping piggybacks the cron tick (throttled to
    # ~6h inside; never blocks or fails the poll).
    await _maybe_run_lifecycle_sweep(supabase)

    # Archival lifecycle (UX/IA §5): 30d soft-archive + 60d purge, same
    # piggyback/throttle shape, flag-gated.
    await _maybe_run_archival_sweep(supabase)

    due = filter_due_sources(all_enabled)

    # Drain a slice of the untagged backlog (#285) EVERY cycle — jobs orphaned
    # when a source stopped re-appearing in its feed never re-enter
    # ``_qualify_jobs`` below, so they'd bypass the read gates forever. Runs
    # independent of whether any source is due (a quiet cycle still drains it),
    # and BEFORE the ``not due`` early-exit so it isn't skipped. Self-budget-
    # gated (respects the grading reserve); best-effort.
    if settings.qualification_enabled and settings.qualification_backfill_batch > 0:
        await _backfill_qualify_stale(supabase, settings.qualification_backfill_batch)

    # The embedding drain is GONE (Disk IO slim-down, 2026-07-30): vectors
    # materialize lazily at grade time (``ensure_job_vectors``), so #21-class
    # stranding is structurally impossible — a job is embedded exactly when
    # first needed and the content-hash makes retries free.

    # Grade a slice of the stale promising backlog (the qualify sweep's
    # grading twin) — also before the ``not due`` early-exit, so quiet
    # cycles still drain it. Flag-gated + quota-bounded inside.
    if settings.phase2_enabled and settings.phase2_backfill_batch > 0:
        await _backfill_grade_stale(supabase, settings.phase2_backfill_batch)

    if not due:
        return PollResult(sources_polled=0, new_jobs=0, updated_jobs=0, archived_jobs=0, errors=[])

    # Cycle-wide constants resolved once instead of once per source:
    # active targets and the stage-3 (user → target/optimized-doc) maps.
    active_targets = await asyncio.to_thread(get_active_target, supabase)
    stage3_users = await _resolve_user_targets_for_stage3(
        supabase, active_targets, "(cycle prefetch)"
    )

    budget_gate, has_active = await _cycle_budget_gate(supabase)
    due = _drop_paid_sources_if_unconsumed(due, has_active_targets=has_active)

    # Bound the cycle (#514 residual): an UNBOUNDED due batch is how the
    # fleet starved — with ~3,200 enabled sources, one slow tail (Workday
    # 429 retry storms) dragged the gather past the 1200s watchdog, the
    # abort killed every unfinished source, and the un-stamped tail stayed
    # due for the next identical over-long cycle. Measured 2026-07-29: 1,110
    # of 3,231 enabled sources >2x overdue, 1,077 unpolled for 24h+. Cap
    # the batch and take the MOST OVERDUE first (never-polled at the very
    # front) so every source rotates through within a few ticks and each
    # cycle finishes well inside the watchdog. 0 = legacy unbounded.
    cap = settings.poll_max_sources_per_cycle
    total_due = len(due)
    if cap > 0 and total_due > cap:
        due = sorted(due, key=lambda s: s.get("last_polled_at") or "")[:cap]
        logger.info(
            "poll cycle capped: %d due, polling the %d most-overdue this tick",
            total_due,
            cap,
        )
    semaphore = asyncio.Semaphore(POLL_CONCURRENCY)

    async def _worker(source: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            return await _poll_one_source(
                source,
                supabase,
                budget_gate,
                active_targets=active_targets,
                stage3_users=stage3_users,
            )

    summaries = await asyncio.gather(*(_worker(s) for s in due))

    result = PollResult(sources_polled=0, new_jobs=0, updated_jobs=0, archived_jobs=0, errors=[])
    for s in summaries:
        if s["polled"]:
            result.sources_polled += 1
        result.new_jobs += s["new"]
        result.updated_jobs += s["updated"]
        result.archived_jobs += s["archived"]
        if s["error"]:
            result.errors.append(s["error"])

    return result


# ---- Target-specific polling ------------------------------------------------


async def _poll_one_source_for_target(
    source: dict[str, Any],
    supabase: Client,
    target: JobTarget,
    payer_user_id: str | None = None,
    payer_over_budget: bool = False,
) -> dict[str, Any]:
    """Poll a single source for a specific target. Three-stage pipeline.

    ``payer_user_id`` is the user charged for this target's LLM work
    (the activator); ``payer_over_budget`` skips Phase 1 spend while
    still ingesting fail-open — both resolved once by
    ``poll_sources_for_target``.
    """
    summary: dict[str, Any] = {"polled": False, "new": 0, "updated": 0, "error": None}
    company_name: str = source.get("company_name", "?")
    # BYOK (#5 P3): grade on the payer's own key. Single payer here, but
    # memoized so Phase 1 and Phase 2/3 reuse one resolved client.
    payer_clients: dict[str | None, LLMClient | None] = {}

    try:
        board_token: str = source["board_token"]
        source_id: str = source["id"]
        provider: str = source.get("provider", "greenhouse")

        fetcher = FETCHERS.get(provider)
        if not fetcher:
            summary["error"] = f"{company_name}: unknown provider '{provider}'"
            return summary

        jobs = await fetcher(board_token)
        summary["polled"] = True

        # #514: this path had no known-row read at all, so already-ingested
        # external_ids were re-triaged on every cycle (the exact LLM waste
        # the shared path's candidate comment calls out) and a flipped
        # verdict — or the fail-closed gate below — starved their content
        # refresh. Same full-set admission scoping as ``_poll_one_source``.
        known_ids_resp = await poll_db_read(
            supabase,
            lambda c: c.table("jobs").select("external_id").eq("source_id", source_id),
            label=f"poll known ids {company_name}",
            retry_sync=True,
        )
        known_external_ids = {
            r.get("external_id") for r in cast(list[dict[str, Any]], known_ids_resp.data or [])
        }

        # Phase 1 per-target triage (single target). Same semantics as
        # ``_poll_one_source`` but the candidate set is one target, so
        # ``phase1_verdicts`` collapses to a single dict. Skipped when a
        # SPONSORED payer is blocked (over allowance / idle / disabled):
        # empty verdicts → fail-open ingest, grading defers. A catalog
        # target (no active user link) triages on the instance key.
        #
        # Only NEW external_ids that are FREE-GATE SURVIVORS are triaged
        # (mirrors ``_poll_one_source``): the per-job loop below drops
        # keyword misses and non-US locations regardless of verdict, and
        # known rows were admitted on a prior cycle (#514) — paying the
        # LLM to classify either is pure waste. Verdicts stay keyed by
        # ORIGINAL 1-based job index via the candidate mapping;
        # non-survivors have no entry (fail-open, then free-gate drop).
        target_verdicts: dict[int, TitleVerdict] = {}
        # Global idxs actually sent to triage (before any budget defer). Missing
        # verdict for these = LLM hiccup (fail-open); missing for others = budget
        # defer → defer, don't admit (#285 f/u). See _phase1_promising.
        phase1_attempted: set[int] = set()
        triage_candidates = [
            (idx, job)
            for idx, job in enumerate(jobs)
            if job.external_id not in known_external_ids
            and _title_matches_target(job.title, target.search_keywords)
            and _is_us_location(job.location_name)
        ]
        # Grade the most RECENT first (#285 f/u): budget truncation defers the
        # oldest tail, not the newest. Undated rows sort last.
        triage_candidates.sort(
            key=lambda c: normalize_posted_at(c[1].posted_at) or "", reverse=True
        )
        # BYOK (#5 P3): triage on the payer's own key. ``None`` means
        # triage is off / over-budget / no BYOK key in require-mode — all
        # leave verdicts empty → fail-open ingest, grade on a later cycle.
        llm = (
            _resolve_payer_client(payer_clients, supabase, payer_user_id)
            if settings.phase1_triage_enabled and triage_candidates and not payer_over_budget
            else None
        )
        if llm is not None:
            # Negative-verdict cache (#514): titles this target's LLM already
            # rejected within the TTL skip the model — synthetic
            # promising=False verdict, marked attempted (rejected, not
            # deferred). Same semantics as the scheduled path.
            send_candidates: list[tuple[int, StandardJob]] = []
            for cand_idx, cand_job in triage_candidates:
                if _phase1_cached_rejection(target, cand_job.title):
                    global_idx = cand_idx + 1
                    target_verdicts[global_idx] = TitleVerdict(id=global_idx, promising=False)
                    phase1_attempted.add(global_idx)
                else:
                    send_candidates.append((cand_idx, cand_job))
            titles = [job.title for _, job in send_candidates]
            batch_cap = phase1_batch_size()
            for start in range(0, len(titles), batch_cap):
                # Re-check the global daily cap before each batch (#60
                # overspend fix) — bounds overshoot to one batch when a long
                # cycle crosses the cap mid-run. Collected verdicts stand;
                # the rest fail-open admit (same defer semantics as an
                # over-budget payer).
                if await _triage_budget_blocks(supabase):
                    logger.warning(
                        "Phase 1 triage: global daily LLM cap reached "
                        "($%.2f) mid-cycle — deferring remaining titles for "
                        "target %s",
                        settings.global_llm_daily_budget_usd,
                        target.id,
                    )
                    break
                batch = titles[start : start + batch_cap]
                verdicts, result = await triage_titles(llm, target=target, titles=batch)
                if result is not None:
                    # A REAL LLM response → attempted. A FAILED call (result None:
                    # OpenRouter 401 / spent limit / error) leaves these un-attempted
                    # → they DEFER, not fail-open admit (a dead key must PAUSE the
                    # pipeline, never flood 100% admit).
                    for sp in range(start, min(start + len(batch), len(send_candidates))):
                        phase1_attempted.add(send_candidates[sp][0] + 1)
                    try:
                        record_llm_cost(
                            supabase,
                            user_id=payer_user_id,
                            purpose=PHASE1_PURPOSE,
                            result=result,
                            metadata={
                                "target_id": target.id,
                                "source": company_name,
                                "batch_size": len(batch),
                            },
                        )
                    except Exception:
                        logger.exception(
                            "Failed to record Phase 1 cost for target %s",
                            target.id,
                        )
                # Shift batch-local ids (1-based within the SENT subset)
                # back to global 1-based job indices via the send-candidate
                # mapping; raw rejections feed the negative cache (#514).
                for batch_idx, verdict in verdicts.items():
                    subset_pos = start + batch_idx - 1  # 0-based
                    if 0 <= subset_pos < len(send_candidates):
                        global_idx = send_candidates[subset_pos][0] + 1
                        target_verdicts[global_idx] = verdict
                        if not verdict.promising:
                            _phase1_record_rejection(target, send_candidates[subset_pos][1].title)

        rows_to_upsert: list[dict[str, Any]] = []
        phase1_idx_by_external_id: dict[str, int] = {}
        for idx, job in enumerate(jobs):
            # Free gates first — Phase 1 only ever saw their survivors,
            # so a non-survivor's missing verdict can't fail-open here.
            if not _title_matches_target(job.title, target.search_keywords):
                continue
            if not _is_us_location(job.location_name):
                continue
            # Phase 1 ids are 1-based. A missing verdict fail-opens ONLY for a
            # job we actually triaged (LLM dropped an id). A job the budget
            # DEFERRED (never triaged) is dropped from the upsert here — deferred,
            # re-triaged next cycle, not admitted blind (#285 f/u).
            #
            # The gate judges CANDIDATES only (#514): known rows were admitted
            # on a prior cycle, and when triage was deliberately skipped this
            # cycle (``llm is None`` — over-budget / BYOK require-mode without
            # a key / zero candidates) ingest fails OPEN as documented above,
            # with grading deferred. The old form dropped EVERY job in both
            # states — fail-closed, the opposite of the documented contract.
            # ``llm is not None`` with FAILED calls keeps the dead-key defer:
            # those candidates stay un-attempted and drop below.
            if (
                settings.phase1_triage_enabled
                and llm is not None
                and job.external_id not in known_external_ids
            ):
                gj = idx + 1
                v = target_verdicts.get(gj)
                if gj not in phase1_attempted or (v is not None and not v.promising):
                    continue

            salary = job.salary_text or extract_salary_from_html(job.content)
            loc = parse_location(job.location_name)

            phase1_idx_by_external_id[job.external_id] = idx + 1
            rows_to_upsert.append(
                {
                    "external_id": job.external_id,
                    "source_id": source_id,
                    "title": job.title,
                    "company_name": company_name,
                    "location": job.location_name,
                    "city": loc.city,
                    "state": loc.state,
                    "country": loc.country,
                    "location_remote": loc.remote,
                    "description_html": sanitize_html(job.content),
                    "absolute_url": job.absolute_url,
                    "source_posted_at": normalize_posted_at(job.posted_at),
                    "salary_text": salary,
                    **salary_columns(salary),
                }
            )

        # NEW rows only (#514) — same scoping as _poll_one_source: known
        # rows' URLs were validated at ingest and are url_health-monitored.
        if settings.validate_poll_urls and rows_to_upsert:
            fresh = [r for r in rows_to_upsert if r.get("external_id") not in known_external_ids]
            kept = [r for r in rows_to_upsert if r.get("external_id") in known_external_ids]
            rows_to_upsert = (await _validate_rows(fresh)) + kept

        # #503: JSON-LD baseSalary fallback (flag-gated, bounded; no-op by default).
        if rows_to_upsert:
            await _fill_jsonld_salaries(rows_to_upsert, known_external_ids)

        # Tombstoned (purged) postings must not be resurrected by the
        # conflict-update (archival Stage 2) — same guard as _poll_one_source.
        if rows_to_upsert:
            rows_to_upsert = await _drop_purged_rows(supabase, source_id, rows_to_upsert)

        if rows_to_upsert:
            # Routing through the seam also gives this upsert the transient-
            # blip retry the all-sources path already had (idempotent upsert,
            # so a re-issue after a dropped stream is safe).
            upsert_resp = await poll_db_write(
                supabase,
                lambda c: c.table("jobs").upsert(
                    rows_to_upsert, on_conflict="source_id,external_id"
                ),
                label=f"poll upsert {company_name}",
            )
            # #514: a row is a REFRESH iff its external_id was known before
            # this cycle's upsert — NOT created==updated (a conflict-update
            # never bumps jobs.updated_at, see _poll_one_source). The
            # Stage-2 pass preserves the persisted Phase-1 verdict for
            # refreshed rows instead of re-litigating admission.
            known_upserted_ids: list[str] = []
            for raw_row in upsert_resp.data or []:
                data = cast(dict[str, Any], raw_row)
                if data.get("external_id") in known_external_ids:
                    summary["updated"] += 1
                    known_upserted_ids.append(data["id"])
                else:
                    summary["new"] += 1

            # Qualification firewall (#60): same target-INDEPENDENT tagging
            # as ``_poll_one_source`` — AFTER the US filter, BEFORE per-target
            # scoring, flag-gated, best-effort.
            if settings.qualification_enabled and upsert_resp.data:
                await _qualify_jobs(
                    supabase,
                    [cast(dict[str, Any], r) for r in upsert_resp.data],
                )

            # Job embeddings are LAZY (see _poll_one_source): the Phase-2
            # runner's ensure_job_vectors materializes exactly the read set.

            # Stage 1: Title scoring
            async def _title_score_one(row_data: dict[str, Any]) -> None:
                try:
                    await target_title_score_and_upsert(
                        supabase,
                        job_posting_id=row_data["id"],
                        title=row_data.get("title", ""),
                        target=target,
                    )
                except Exception:
                    logger.exception("Stage 1 scoring failed for job %s", row_data.get("id"))

            await asyncio.gather(
                *(_title_score_one(cast(dict[str, Any], r)) for r in upsert_resp.data or [])
            )

            # Stage 2: Full JD scoring (pre-parse JDs once)
            jd_cache: dict[str, Any] = {}
            for raw_row in upsert_resp.data or []:
                rd = cast(dict[str, Any], raw_row)
                jd_cache[rd["id"]] = parse_jd(rd.get("description_html") or "")

            # #514: persisted Phase-1 verdicts for the refreshed rows —
            # ``promising=False`` is the exclusion floor on re-score
            # (``bulk_score_for_target``'s contract); True/NULL rely on the
            # keyword scorer. Chunked ≤150 ids for PostgREST's URL bound.
            promising_floor: dict[str, bool | None] = {}
            for start in range(0, len(known_upserted_ids), 150):
                chunk = known_upserted_ids[start : start + 150]
                floor_resp = await poll_db_read(
                    supabase,
                    lambda c, _chunk=chunk: (
                        c.table("scores")
                        .select("job_posting_id, promising")
                        .eq("target_id", target.id)
                        .in_("job_posting_id", _chunk)
                    ),
                    label=f"poll stage2 floor {company_name}",
                    retry_sync=True,
                )
                for floor_row in cast(list[dict[str, Any]], floor_resp.data or []):
                    promising_floor[floor_row["job_posting_id"]] = floor_row.get("promising")

            async def _full_score_one(row_data: dict[str, Any]) -> None:
                try:
                    # Phase 1 verdict for this (job, this-target) pair.
                    # The gate already filtered out non-promising jobs
                    # above, so every row here is promising — but we
                    # still want ``scores.promising=True`` persisted so
                    # Phase 2 candidate selection can rely on it.
                    ext_id = row_data.get("external_id", "")
                    promising: bool | None
                    if ext_id in known_external_ids:
                        # #514: refresh of an already-ingested row — Phase 1
                        # does not re-litigate admission; the persisted
                        # verdict is the floor and its columns stay untouched.
                        promising = promising_floor.get(row_data["id"]) is not False
                        promising_arg: bool | None = None
                        phase1_confidence: int | None = None
                    else:
                        phase1_idx = phase1_idx_by_external_id.get(ext_id)
                        verdict = (
                            target_verdicts.get(phase1_idx) if phase1_idx is not None else None
                        )
                        # Confidence gate (#47) + budget-deferral (#285 f/u): defer a
                        # never-triaged job instead of admitting it blind.
                        attempted_here = phase1_idx is not None and phase1_idx in phase1_attempted
                        promising = _phase1_promising(
                            verdict,
                            attempted=attempted_here,
                            gate_active=settings.phase1_triage_enabled,
                            min_confidence=settings.phase1_min_confidence,
                        )
                        promising_arg = promising if target_verdicts else None
                        phase1_confidence = verdict.confidence if verdict is not None else None
                    await target_score_and_upsert(
                        supabase,
                        job_posting_id=row_data["id"],
                        title=row_data.get("title", ""),
                        description_html=row_data.get("description_html", ""),
                        target=target,
                        parsed_jd=jd_cache.get(row_data["id"]),
                        excluded_by_prefilter=not promising,
                        promising=promising_arg,
                        phase1_confidence=phase1_confidence,
                    )
                except Exception:
                    logger.exception("Stage 2 scoring failed for job %s", row_data.get("id"))

            await asyncio.gather(
                *(_full_score_one(cast(dict[str, Any], r)) for r in upsert_resp.data or [])
            )

            # Stage 3: LLM scoring for qualified jobs (concurrent).
            # JobTarget is a global row with no user_id — resolve owning
            # users via the user_targets junction, then fetch each user's
            # optimized doc. The pre-fix ``get_latest(None)`` returned
            # nothing since no system-wide doc exists in the multi-user
            # schema.
            primary_by_user, user_optimized = await _resolve_user_targets_for_stage3(
                supabase, [target], company_name
            )
            if primary_by_user and payer_over_budget:
                logger.info(
                    "Stage 3 deferred for target %s (payer %s over monthly allowance)",
                    target.id,
                    payer_user_id,
                )
            elif settings.phase2_enabled and primary_by_user:
                # ---- Phase 2: LLM job-fit grading (#6) ----
                # Mirrors ``_poll_one_source``: the Haiku-batched
                # scorecard with the promising gate, re-grade contract
                # and per-target daily cap — NOT the legacy full-JD
                # Sonnet call ($0.038/job) below, which previously ran
                # unconditionally on this activation path. Legacy stays
                # only as the flag-off fallback.
                # BYOK (#5 P3): grade on the payer's own key; no key in
                # hosted require-mode defers (jobs stay promising/NULL,
                # grade once a key is added).
                llm = _resolve_payer_client(payer_clients, supabase, payer_user_id)
                if llm is None:
                    logger.info(
                        "Phase 2 deferred for target %s (payer %s has no BYOK key)",
                        target.id,
                        payer_user_id,
                    )
                else:
                    cycle_rows = [cast(dict[str, Any], r) for r in upsert_resp.data or []]
                    for uid in primary_by_user:
                        try:
                            await run_phase2_for_jobs(
                                supabase,
                                llm,
                                target=target,
                                payload=user_optimized[uid].payload,
                                jobs=cycle_rows,
                                user_id=payer_user_id,
                            )
                        except Exception:
                            logger.exception(
                                "Phase 2 grading failed for user %s / target %s",
                                uid,
                                target.id,
                            )

    except Exception:
        logger.exception("Poll failed for %s (target %s)", company_name, target.label)
        summary["error"] = f"{company_name}: poll failed"

    # Stamp ``last_polled_at`` on the source row whenever we made it past
    # the fetcher dispatch — including the "polled but zero matches against
    # this target" case, which previously left the column null and gave
    # operators no signal that the source was actually being touched. We
    # explicitly skip on the "unknown provider" branch above (that path
    # returns early before reaching here) so a misconfigured row doesn't
    # silently look healthy.
    if summary.get("polled"):
        try:
            source_id_for_stamp = source.get("id")
            if source_id_for_stamp:
                await poll_db_write(
                    supabase,
                    lambda c: (
                        c.table("sources")
                        .update({"last_polled_at": datetime.now(UTC).isoformat()})
                        .eq("id", source_id_for_stamp)
                    ),
                    label=f"poll target mark-polled {company_name}",
                )
        except Exception:
            # Non-fatal — the actual poll already happened, this is just
            # the operator-visibility stamp.
            logger.exception("Failed to update last_polled_at for source %s", company_name)

    return summary


async def poll_sources_for_target(supabase: Client, target: JobTarget) -> PollResult:
    """Poll all enabled sources, filtering for jobs matching a target's search keywords.

    Skips non-pipeline-active targets entirely (returns an empty
    ``PollResult``). Pipeline-active = ``app_active`` (the instance
    floor) OR any active membership — the derived predicate from
    ``crud.is_pipeline_active`` (the trigger-cached flag is gone, schema
    audit P0). The /activate endpoint activates the caller's membership
    before invoking this, which satisfies the membership arm.
    """
    if not await asyncio.to_thread(target_is_pipeline_active, supabase, target.id):
        logger.info(
            "poll_sources_for_target: skipping non-pipeline-active target %s (%s)",
            target.id,
            target.label,
        )
        return PollResult(sources_polled=0, new_jobs=0, updated_jobs=0, archived_jobs=0, errors=[])

    if not target.search_keywords:
        return PollResult(
            sources_polled=0,
            new_jobs=0,
            updated_jobs=0,
            archived_jobs=0,
            errors=["Target has no search keywords"],
        )

    sources_resp = await poll_db_read(
        supabase,
        lambda c: c.table("sources").select("*").eq("enabled", True),
        label="poll sources read",
    )
    sources = sources_resp.data or []

    # Optimized doc is fetched per-user inside
    # ``_poll_one_source_for_target`` now — the previous shared-doc fetch
    # (``user_id=None``) never returned a row in the multi-user schema.

    # Resolve the payer (activator) once for the whole fan-out; their
    # monthly allowance decides whether Phase 1 spends anything. On
    # failure: refuse to spend (defer LLM work), keep ingesting.
    try:
        gate = await asyncio.to_thread(build_budget_gate, supabase, [target.id])
    except Exception:
        logger.exception(
            "Budget gate build failed for target %s — deferring LLM work",
            target.id,
        )
        gate = PayerBudgetGate()
    payer = gate.payer_for(target.id)
    over = gate.target_blocked(target.id)
    if over:
        logger.info(
            "poll_sources_for_target: Phase 1 deferred for target %s "
            "(payer %s blocked: over allowance / idle / disabled)",
            target.id,
            payer,
        )

    semaphore = asyncio.Semaphore(POLL_CONCURRENCY)

    async def _worker(raw_source: Any) -> dict[str, Any]:
        async with semaphore:
            return await _poll_one_source_for_target(
                cast(dict[str, Any], raw_source),
                supabase,
                target,
                payer_user_id=payer,
                payer_over_budget=over,
            )

    summaries = await asyncio.gather(*(_worker(s) for s in sources))

    result = PollResult(sources_polled=0, new_jobs=0, updated_jobs=0, archived_jobs=0, errors=[])
    for s in summaries:
        if s["polled"]:
            result.sources_polled += 1
        result.new_jobs += s["new"]
        result.updated_jobs += s["updated"]
        if s.get("error"):
            result.errors.append(s["error"])

    return result
