from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import logging
import time
from collections import Counter
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from supabase import AsyncClient

from app.config import settings
from app.constants import resolve_owner
from app.http_client import BoardFetchError
from app.models.experience import OptimizedDoc
from app.models.schemas import PollResult
from app.models.targets import JobTarget
from app.services import catalog_health, notify
from app.services.ashby import fetch_ashby_jobs
from app.services.board_metadata import board_columns, board_us_verdict
from app.services.date_normalize import normalize_posted_at
from app.services.db_write import (
    DB_WRITE_CONCURRENCY,
    poll_db_read,
    poll_db_upsert,
    poll_db_write,
)
from app.services.experience import optimized
from app.services.extract import extract_salary_from_html, salary_columns
from app.services.firecrawl import fetch_firecrawl_jobs
from app.services.fit import run_phase2_for_jobs
from app.services.greenhouse import fetch_board_jobs
from app.services.jd_parser import parse_jd
from app.services.jsonld import fetch_jsonld_jobs, fetch_salary_from_posting_page
from app.services.lever import fetch_lever_jobs
from app.services.llm import MissingUserKeyError, TrialExpiredError
from app.services.llm import get_client_async as get_llm_client_async
from app.services.llm.client import LLMClient
from app.services.llm.cost_log import record_async as record_llm_cost_async
from app.services.llm.cost_log import total_spend_all_async as total_llm_spend_all_async
from app.services.llm.provider_breaker import (
    provider_fatal_active as _provider_fatal_active,
)
from app.services.location_parse import parse_location
from app.services.mock_board import fetch_mock_jobs
from app.services.qualification import is_us_location, positively_us_location
from app.services.recency import refresh_recency_scores_poll
from app.services.relevance.daily_cap import phase1_cap_reached
from app.services.relevance.phase1_backfill import backfill_phase1_for_target
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
from app.services.sanitize import sanitize_html
from app.services.scoring import score_title_against_profile
from app.services.smartrecruiters import fetch_smartrecruiters_jobs
from app.services.source_redetect import redetect_source
from app.services.standard_job import StandardJob
from app.services.target_scoring import TargetReapedError
from app.services.target_scoring import (
    score_and_upsert as target_score_and_upsert,
)
from app.services.target_scoring import (
    score_title_and_upsert as target_title_score_and_upsert,
)
from app.services.targets import crud
from app.services.targets.payers import (
    BlockReason,
    PayerBudgetGate,
    block_admits_ingestion,
    block_is_persistent,
    build_budget_gate,
)
from app.services.titles import clean_title_display
from app.services.url_health import escalate_source_listings
from app.services.validate import liveness_verdict, validate_job_url
from app.services.workday import KnownPosting, fetch_workday_jobs
from app.supabase_pool import get_async_supabase

logger = logging.getLogger(__name__)

# Every poll-cycle entry point now passes the pooled ``AsyncClient`` (#57): the
# scheduler + force poll (PR-G2d-b) and the target-activation poll
# (``routers.targets`` → ``poll_sources_for_target``). Every DB touch below routes
# through the ``db_write`` seam (client-agnostic — it uses ``get_async_supabase()``
# internally), and the cross-user service-role collaborators (budget meter, payer
# resolver, alert dispatch) await on the pooled async service client via
# ``_async_service_client`` (PR-G2e-1).

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

# How often the target-activation fan-out re-checks that its target is
# still pipeline-active (#638). One cheap read a minute against a
# multi-hour full-catalog grind; deactivation drains the fan-out within
# roughly one interval.
_ACTIVE_RECHECK_S = 60
LLM_CONCURRENCY = 3

# Cycle-wide cap for ``_validate_rows`` (URL validation per row): it gathers
# over a whole source's rows and POLL_CONCURRENCY sources run at once, so
# without a *shared* bound the poll can open thousands of simultaneous URL
# validations. One semaphore per event loop, keyed by the running loop like
# ``db_write`` so a fresh test/worker loop gets its own. (The qualify-tagger
# twin moved to ``qualification.materialize`` with the tagger itself.)
VALIDATE_CONCURRENCY = 20
_validate_sems: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}


@dataclass
class AdmissionBudget:
    """One poll cycle's allowance for the persistent-block admission fallback.

    CYCLE-LOCAL, deliberately, not a module global. Cycles can overlap: the
    scheduler holds a Postgres advisory lock, but ``POST /poll/due`` calls
    ``poll_due_sources`` DIRECTLY without it — it exists for external cron
    callers (pg_cron, GitHub Actions) — so a scheduled tick and an HTTP-driven
    one can run at once. A module-level counter would let the second cycle's
    reset wipe the first cycle's remaining allowance mid-drain, and the
    50-per-cycle guarantee this class exists to provide would quietly stop
    holding. Each cycle carries its own.

    Within one cycle, concurrent source workers are safe: ``take`` has no
    ``await``, so check-and-decrement is atomic on the event loop.

    Only the FALLBACK draws on it. A job a target actually triaged and admitted
    is never rate-limited, so a healthy pipeline never touches this.
    """

    cap: int
    remaining: int | None

    def take(self) -> bool:
        """Consume one slot, or report this cycle's ramp is spent."""
        if self.remaining is None:
            return True  # uncapped
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True

    def report(self) -> str:
        """One-line ramp state for the cycle log."""
        if self.cap <= 0 or self.remaining is None:
            return "admission_ramp=uncapped"
        used = self.cap - self.remaining
        spent = " EXHAUSTED (backlog continues next cycle)" if self.remaining <= 0 else ""
        return f"admission_ramp={used}/{self.cap}{spent}"


def new_admission_budget() -> AdmissionBudget:
    """A fresh allowance for one cycle. Cap 0 means uncapped."""
    cap = settings.persistent_block_admission_cap_per_cycle
    return AdmissionBudget(cap=cap, remaining=cap if cap > 0 else None)


@dataclass
class IntakeBudget:
    """This cycle's slice of the hourly ceiling on AUTOMATED new listings.

    Bounds write pressure, not spend or relevance: ``settings.
    intake_max_new_jobs_per_hour`` caps how many rows the POLLER may INSERT
    into ``jobs`` per rolling hour — the scheduled cycle, ``POST /poll/due``,
    ``poll_all_sources`` and the activation fan-out all share this one budget,
    including the ordinary "a target triaged it and said promising" path that
    :class:`AdmissionBudget` deliberately never touches.

    NOT a ceiling on every row that can reach ``jobs``.
    ``job_ingest.materialize_and_score_job`` (manual add, target-from-URL)
    inserts one row per rate-limited request on explicit human intent and is
    deliberately exempt — refusing a person's "add this job" to protect against
    a poller burst would invert the priority ``grading_budget_reserve_usd``
    already sets, where background yields to live work and not the reverse.
    Those rows still land in ``cataloged_at``, so they shrink the next cycle's
    allowance; the hour can be exceeded, but only by human action.

    Seeded from the DATABASE, not a process counter. ``remaining`` is
    ``cap - (rows cataloged in the last hour)``, re-read once per cycle, for the
    same reason the admission ramp had to become cycle-local: ``POST /poll/due``
    calls ``poll_due_sources`` without the scheduler's advisory lock, so two
    cycles can run at once, and a module-global counter would let one cycle's
    reset wipe the other's. Re-reading truth also survives a restart mid-hour —
    a process counter would silently grant a fresh full allowance on every
    deploy, and this app deploys near-daily.

    Overlap is bounded, not eliminated: two concurrent cycles each read the same
    pre-cycle count, so the pair can overshoot by at most one cycle's intake
    before the next read corrects. At the default cap that overshoot is a
    rounding error, and the alternative (a DB round-trip per admitted row) would
    cost far more than the burst it prevents.

    Within one cycle, concurrent source workers are safe: ``take`` has no
    ``await``, so check-and-decrement is atomic on the event loop.
    """

    cap: int
    remaining: int | None
    prior: int = 0

    def take(self) -> bool:
        """Consume one slot, or report the hourly ceiling is reached."""
        if self.remaining is None:
            return True  # uncapped
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True

    def report(self) -> str:
        """One-line intake state for the cycle log.

        The counts are admission SLOTS CONSUMED, not rows confirmed written: a
        slot is taken before the upsert runs, so a later write failure leaves
        this line reading high. Deliberate — the ceiling must not be able to
        under-count itself — but it means an operator should not diff this
        against a ``COUNT(*)`` and expect equality. ``prior`` is the only
        figure here read back from the database."""
        if self.cap <= 0 or self.remaining is None:
            return "intake=uncapped"
        admitted = max(0, self.cap - self.prior - self.remaining)
        spent = " HOURLY CAP REACHED (deferred to a later poll)" if self.remaining <= 0 else ""
        return f"intake={self.prior + admitted}/{self.cap}per_h (+{admitted} this cycle){spent}"


async def new_intake_budget(supabase: AsyncClient) -> IntakeBudget:
    """Read how much of this hour's intake ceiling is already spent.

    Counts live+archived rows alike: ``cataloged_at`` records when we WROTE the
    row, which is the write pressure being bounded, and a row archived minutes
    after ingest cost exactly as much to insert as one that survived.

    Fails OPEN (uncapped) on a read error. This is a burst ceiling on a path
    that is otherwise unbounded and is nowhere near binding in normal
    operation, so refusing all intake because one count query failed would
    trade a hypothetical write burst for a certain ingestion stall — the
    failure mode that cost 50 hours of zero intake once already.
    """
    cap = settings.intake_max_new_jobs_per_hour
    if cap <= 0:
        return IntakeBudget(cap=cap, remaining=None)
    since = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    try:
        resp = await poll_db_read(
            supabase,
            lambda c: (
                c.table("jobs").select("id", count="exact", head=True).gte("cataloged_at", since)
            ),
            label="poll intake hourly count",
            retry_sync=True,
        )
    except Exception:
        logger.exception("Intake cap: hourly count failed — proceeding uncapped this cycle")
        return IntakeBudget(cap=cap, remaining=None)
    prior = int(resp.count or 0)
    return IntakeBudget(cap=cap, remaining=max(0, cap - prior), prior=prior)


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

# --- Provider-fatal fast-fail breaker (audit PERF-M "402/429 fast-fail") -------
# ``_global_budget_exhausted`` stops at our SELF-IMPOSED daily spend cap. The
# provider-fatal breaker catches the *provider* rejecting every call — OpenRouter
# out of credits (402) or sustained rate-limiting (429) — which can happen while
# we're still UNDER budget. The first such error latches it for a cooldown so
# dependent fan-outs stop firing doomed calls; it auto-clears (monotonic). It
# lives in ``app.services.llm.provider_breaker`` (imported at the top as
# ``_provider_fatal_active``) so the qualification tagger, the Phase-1 triage
# gate below and the E2 lazy fit-score refresh share the SAME latch — a credits
# outage caught by any one backs the others off too.


# US-location detection (hint list + regexes + ``_is_us_location``) moved to
# ``app/services/qualification/heuristics.py`` so the poller's ingestion gate
# and the #60 qualification tagger's L1 share ONE implementation (single source
# of truth). ``_is_us_location`` is re-exported below for back-compat with
# callers/tests that import it from this module.


def _admits_for_catalog(
    title: str,
    active_targets: list[JobTarget],
    admission_targets: list[JobTarget] | None,
) -> bool:
    """Does this title belong in the shared catalog?

    TWO rule sets, because the targets differ in kind:

    * ACTIVE targets get the full rules, including the excluded-so-admit
      audit rule. Someone is looking at these, so "we found it and filtered
      it out, here is why" is worth a row.
    * The WIDER set admits on a POSITIVE match only. Nobody is looking at an
      unfollowed target, so its negative keywords are just a miss — turning
      them into an admit rule is what flooded the catalog with assistants and
      sales reps (see ``_title_matches_any_target``).

    The wider set is a superset of the active one, so the second check is
    strictly narrower and the ``or`` cannot lose an active-target admit.
    """
    if _title_matches_any_target(title, active_targets):
        return True
    if not admission_targets:
        return False
    return _title_matches_any_target(title, admission_targets, exclusion_admits=False)


def _title_matches_any_target(
    title: str, targets: list[JobTarget], *, exclusion_admits: bool = True
) -> bool:
    """Check if a job title is worth ingesting for at least one target.

    Admission rules per target (any one target admitting → admit):
      1. Excluded by negative keywords → admit anyway WHEN
         ``exclusion_admits``, so the scoring pipeline records the
         rejection (excluded=True) for audit. Without this, junior-vs-
         director hits would silently vanish instead of being explainable
         in the UI.

         ``exclusion_admits=False`` turns that off, and the caller must
         pass it for any target NOBODY IS LOOKING AT. The audit rule buys
         one thing — a user asking "why isn't this in my list?" gets an
         answer — and an unfollowed target has no such user. Applied to
         every target in the catalog it inverts into a firehose: each
         target's NEGATIVE keywords become an ADMIT rule, so a
         "Director of CX Operations" target with negatives on assistants
         and sales pulled every "Administrative Assistant" and "Sales
         Representative" on the boards into the shared catalog to record
         a rejection no one would ever read. Measured in prod: 3,957 rows
         admitted in 9h, led by specialist (834), assistant (632) and
         associate (610), against 310 engineers and 2 frontend roles.
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
            if exclusion_admits:
                return True
            # No user to explain this rejection to — a negative-keyword hit
            # is just a miss, not a reason to ingest.
            continue
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


# Phase-1 negative-verdict memory (#514) lives in Postgres now
# (``relevance.rejection_store`` / the ``phase1_rejections`` table). The
# in-process TTL dict it replaces re-billed the entire standing rejected
# corpus roughly daily — its 24h TTL guaranteed that by design, and Railway's
# near-daily deploys wiped it anyway (measured 2026-08-12 as ~75-90% of
# Phase-1 volume; see docs/plan-phase1-rejection-persistence.md). A store hit
# re-injects a synthetic ``promising=False`` verdict, so every downstream
# mechanism (attempted-set defer semantics, ``_any_target_admits``, Stage-2
# floor writes) behaves exactly as if the LLM had re-said no. Admits are
# never cached: an admitted job INGESTS, so known-ness already stops its
# re-triage.


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


# The refreshable payload fields the per-cycle content hash covers (#642).
# Derived columns (city/state/country/location_remote from ``location``;
# salary_min/max/currency/period from ``salary_text``) are deliberately
# absent: they are pure functions of hashed inputs, so hashing the inputs
# suffices — and code changes to the derivations are healed by backfills,
# not by rewriting every row every cycle.
_CONTENT_HASH_FIELDS = (
    "title",
    "location",
    "description_html",
    "absolute_url",
    "source_posted_at",
    "salary_text",
)


def _content_hash(row: dict[str, Any]) -> str:
    """sha256 over the poller-refreshable payload of a built jobs row.

    Change-detection for the per-cycle content refresh (#642): the poller
    used to rewrite every KNOWN row's full payload every cycle (~63
    rewrites per row measured; description_html TOAST included) and then
    re-run stage-2 scoring on it — the dominant write load behind the
    2026-08-06/07 disk-IO exhaustion incidents. Rows whose hash matches
    the stored ``jobs.content_hash`` skip both.
    """
    h = hashlib.sha256()
    for field in _CONTENT_HASH_FIELDS:
        value = row.get(field)
        h.update(b"\x1f")
        h.update(("" if value is None else str(value)).encode())
    return h.hexdigest()


def _partition_unchanged(
    rows: list[dict[str, Any]],
    known_hashes: dict[str | None, str | None],
) -> tuple[list[dict[str, Any]], int]:
    """Split built rows into (to_write, skipped_unchanged_count).

    A row skips iff its external_id is KNOWN and the stored hash equals its
    freshly computed hash. Fresh rows and NULL-stored-hash rows (pre-#642
    ingests) always write — the write stamps ``content_hash``, so each
    legacy row pays exactly one more rewrite and then skips forever.
    Every written row carries its hash in the payload.
    """
    to_write: list[dict[str, Any]] = []
    skipped = 0
    for row in rows:
        digest = _content_hash(row)
        ext = row.get("external_id")
        if ext in known_hashes and known_hashes[ext] == digest:
            skipped += 1
            continue
        row["content_hash"] = digest
        to_write.append(row)
    return to_write, skipped


# in_() id-chunk bound for the post-upsert ``is_us`` writes (#57 lesson:
# ≤150-200 UUIDs keeps the PostgREST URL under proxy limits).
_BOARD_US_CHUNK = 150


async def _update_jobs_chunked(
    supabase: AsyncClient,
    rows: list[dict[str, Any]],
    payload: dict[str, Any],
    *,
    null_filters: tuple[str, ...] = (),
    label: str,
) -> int:
    """Write ``payload`` to ``rows`` in id-chunks; return how many rows ACTUALLY
    changed, and patch the payload back into the ones that did.

    Not ``len(rows)``. Every caller re-asserts a precondition in the WHERE
    clause (``null_filters``), so a row another task changed between the upsert
    snapshot and this write matches nothing — counting the candidate would
    report a write that never happened. PostgREST answers an UPDATE with the
    rows it changed (verified against the local stack: 3 ids in, one
    concurrently archived, 2 rows back, and the untouched row keeps its
    original timestamp), so ``.data`` is the outcome rather than the intent.

    The same set drives the patch-back, for the same reason: a row the WHERE
    clause skipped must not be told it was written. Those dicts go on to become
    this cycle's Phase-2 ``cycle_rows``, so a lie here is spent grading.

    Shared by the two post-upsert ``is_us`` passes (board country, location
    string) so neither can drift into counting intent or skipping the
    patch-back — the failure mode that makes an irreversible write look clean.
    """
    written = 0
    for start in range(0, len(rows), _BOARD_US_CHUNK):
        chunk = rows[start : start + _BOARD_US_CHUNK]
        ids = [r["id"] for r in chunk]

        def _build(c: Any, ids: list[str] = ids) -> Any:
            q = c.table("jobs").update(payload).in_("id", ids)
            for column in null_filters:
                q = q.is_(column, "null")
            return q

        resp = await poll_db_write(supabase, _build, label=label)
        changed = {
            r["id"]
            for r in (getattr(resp, "data", None) or [])
            if isinstance(r, dict) and r.get("id")
        }
        written += len(changed)
        for row in chunk:
            if row["id"] in changed:
                row.update(payload)
    return written


async def _apply_board_us_verdicts(
    supabase: AsyncClient,
    jobs: list[StandardJob],
    upserted: list[dict[str, Any]],
) -> tuple[int, int]:
    """Stamp ``jobs.is_us`` from the country the BOARD published, and archive
    the non-US rows when the operator asked for a US-only catalog.

    Returns ``(marked, archived)`` for the cycle's funnel log — rows ACTUALLY
    written, read back off each UPDATE's response, never the candidate count.
    These counters are the only operational evidence for a mechanism that
    archives irreversibly, so they have to report outcomes; a partial write
    that reported its intent would look exactly like a clean one.

    WHY THIS EXISTS. Qualification tagging went lazy (2026-08-26): nothing
    tags at ingest, so a fresh listing carries ``is_us = NULL``, which the read
    gates admit (``is_us IS NOT FALSE``) and which ``QUALIFICATION_ARCHIVE_NON_US``
    never sees. Tag-time archiving used to remove 13.4% of newly tagged rows,
    133 of 134 of them non-US. The L1 ``is_us_location`` gate above cannot take
    that job back: it drops a listing only when the location string carries a
    non-US hint AND no US marker, so every row it ADMITS is by construction one
    the same parser cannot call non-US. Ashby / Lever / SmartRecruiters publish
    the country as a structured field — the employer's own answer, free, and
    deterministic. That is what this writes.

    WHY IT IS A SEPARATE UPDATE rather than an upsert key. A PostgREST bulk
    upsert builds ONE column list for the whole batch: a key present on any row
    is written to every row, and rows that omitted it get NULL (verified
    against the local stack). ``is_us`` is decided per row, so putting it in
    the payload would blank the tagger's verdict on every board-silent sibling
    in the same batch — the exact class of bug #846 was written to prevent.

    Scoped to ``upserted`` — the rows this cycle actually wrote. Rows skipped
    as byte-identical (#642) are deliberately NOT revisited: back-filling the
    whole known corpus is the per-cycle full rewrite that change removed. This
    fixes intake going forward, not history.

    PATCHES THE VERDICT BACK into the upsert-result dicts, like
    ``ensure_job_tags`` does with the tags it buys. Those same dicts become
    ``cycle_rows`` for this cycle's Phase-2 grading, and ``run_phase2_for_jobs``
    gates on ``is_us`` — without the patch it would spend a grade on a row this
    function had just archived, which is precisely the waste being removed.

    BEST-EFFORT: a write failure is logged and swallowed. The verdict re-derives
    from the board on the next poll that touches the row, and the lazy tagger
    remains the backstop, so a transient blip must not fail a poll whose upsert
    already succeeded (a failed poll counts toward the source's auto-disable
    threshold).
    """
    verdicts = {j.external_id: v for j in jobs if (v := board_us_verdict(j)) is not None}
    if not verdicts:
        return 0, 0
    to_us: list[dict[str, Any]] = []
    to_non_us: list[dict[str, Any]] = []
    to_archive: list[dict[str, Any]] = []
    for row in upserted:
        external_id = row.get("external_id")
        verdict = verdicts.get(external_id) if isinstance(external_id, str) else None
        if verdict is None:
            continue
        # The upsert RETURNING carries the STORED ``is_us`` (the payload never
        # sets it), so a row that already agrees costs no write.
        if verdict is True:
            if row.get("is_us") is not True:
                to_us.append(row)
        else:
            if row.get("is_us") is not False:
                to_non_us.append(row)
            if settings.qualification_archive_non_us and row.get("archived_at") is None:
                to_archive.append(row)

    marked = archived = 0
    label = "board country is_us"
    try:
        # The verdict writes carry NO WHERE filter: they are corrections that
        # have to land on archived rows too.
        if to_us:
            marked += await _update_jobs_chunked(supabase, to_us, {"is_us": True}, label=label)
        if to_non_us:
            marked += await _update_jobs_chunked(supabase, to_non_us, {"is_us": False}, label=label)
        if to_archive:
            # Re-assert the precondition in the WHERE clause: a row archived by
            # url_health or the stale sweep between the upsert and this write
            # must not have its timestamp moved forward.
            archived += await _update_jobs_chunked(
                supabase,
                to_archive,
                {"archived_at": datetime.now(UTC).isoformat()},
                null_filters=("archived_at",),
                label=label,
            )
    except Exception:
        logger.exception("Board-country is_us write failed; leaving the rows untagged")
    return marked, archived


async def _apply_location_us_verdicts(
    supabase: AsyncClient,
    jobs: list[StandardJob],
    upserted: list[dict[str, Any]],
) -> int:
    """Stamp ``jobs.is_us = TRUE`` where the LOCATION STRING plainly names the US.

    Returns the rows ACTUALLY written, for the cycle's funnel log.

    WHY THIS EXISTS. ``_apply_board_us_verdicts`` above restored ingest-time
    ``is_us`` for the providers that publish a structured country — Ashby,
    Lever, SmartRecruiters — which is 39.2% of enabled sources (1,904 of 4,857
    the day this shipped). Greenhouse (1,708) and Workday (1,245) publish no
    country at all and never will, so 60.8% of sources are out of that path's
    reach by construction, and measured coverage of newly cataloged rows sat at
    23.5%.

    What those boards DO publish is a location string, and
    ``positively_us_location`` already reads it — the archive veto and
    ``board_us_verdict`` both consult it. Until now its conclusion was computed
    and thrown away: it was used to VETO an archive, never recorded.

    NB it is NOT the admission gate. Admission uses the permissive sibling
    ``is_us_location``, whose True means only "not provably foreign" — a
    distinction this docstring previously blurred, and one worth keeping sharp
    because the two helpers have opposite risk profiles.
    This records it. Measured over the 5 days since tagging went lazy: 69 of
    104 untagged rows (66%), taking deterministic coverage 23.5% → 74.3%.

    ONE-DIRECTIONAL — it writes TRUE and never FALSE. The costs are asymmetric:
    a wrong FALSE hides a real job from every serving surface (they all gate
    ``is_us IS NOT FALSE``) and, with ``QUALIFICATION_ARCHIVE_NON_US`` on, gets
    it archived — irreversibly. A withheld verdict merely leaves the row NULL,
    which is exactly where it sits today and which the lazy tagger still grades
    later. So a location string is trusted to CONFIRM the US and never to deny
    it, the same asymmetry ``board_us_verdict`` and ``board_columns`` apply to
    the board's own fields. ``is_us_location`` — the permissive sibling that
    returns True for "Remote" and for anything ambiguous — is deliberately NOT
    used here: it is an admission filter, and its True means "not provably
    foreign", which is not a fact worth storing.

    NEVER OVERWRITES. A row that already carries a verdict — from the board
    pass that ran first, or from an earlier LLM tagging — is left alone, and
    the write re-asserts ``is_us IS NULL`` in the WHERE clause so a verdict
    that landed between the upsert snapshot and this write survives too. Order
    matters: the board's structured country is the employer's own answer and
    the stronger fact, so it runs first and this pass fills only what it left
    unknown.

    NOT AN UPSERT KEY, for the same reason the board pass isn't one: a
    PostgREST bulk upsert builds ONE column list for the whole batch, so a key
    present on any row is written to every row and the rows that omitted it get
    NULL (#928). ``is_us`` is decided per row, so riding the payload would
    blank the tagger's verdict on every silent sibling in the batch.

    BEST-EFFORT: a write failure is logged and swallowed. The verdict re-derives
    from the same location string on the next poll that touches the row, so a
    transient blip must not fail a poll whose upsert already succeeded (a failed
    poll counts toward the source's auto-disable threshold).
    """
    us_ids = {j.external_id for j in jobs if positively_us_location(j.location_name)}
    if not us_ids:
        return 0
    # The upsert RETURNING carries the STORED ``is_us`` (the payload never sets
    # it) and the board pass has already patched its own verdicts in, so
    # ``is_us is None`` here means "still unknown after the stronger source".
    to_us = [
        row
        for row in upserted
        if isinstance(row.get("external_id"), str)
        and row["external_id"] in us_ids
        and row.get("is_us") is None
    ]
    if not to_us:
        return 0
    try:
        return await _update_jobs_chunked(
            supabase,
            to_us,
            {"is_us": True},
            null_filters=("is_us",),
            label="location is_us",
        )
    except Exception:
        logger.exception("Location is_us write failed; leaving the rows untagged")
        return 0


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
    supabase: AsyncClient, new_rows: list[dict[str, Any]]
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


def _async_service_client() -> AsyncClient:
    """Async service-role client for the poll cycle's now-async collaborators.

    The async twin of the retired sync service-client escape-hatch (#57
    PR-G2e-1): the budget/spend meter (``build_budget_gate`` + the global-spend
    predicates on
    cost_log's async meter twins), the per-payer BYOK resolver, the Phase-1 cost
    write, and the email/SMS alert dispatch now await on the pooled ``AsyncClient``
    instead of running a sync client in a thread.

    Deliberately the SERVICE-role pool, NOT the cycle's ``supabase`` argument:
    on the scheduled/force path ``supabase`` IS this client, but on the
    target-activation path it can be a user-scoped client, and these are
    cross-user reads (spend across all users, every alertable profile, every
    payer link) that must bypass RLS — exactly what the sync helper guaranteed.

    Prod always has it (``init_async_supabase`` runs in the lifespan); the
    ``cast`` covers the unconfigured/test path, where every caller's collaborator
    is stubbed and the client is never dereferenced.

    NOTE: ``run_phase2_for_jobs`` takes this async client too (#57 PR-G2e-3 —
    its scoring/embeddings vertical, incl. the Phase-2 grader, quota counter, and
    the lazy vector reads/writes, migrated). The lifecycle/archival sweeps and the
    qualification tagger's LLM-client construction now take it as well (#57
    PR-G2e-6): the sweeps route their DB through the ``db_write`` seam, so this is
    the last sync-client escape hatch the poll cycle retired.
    """
    return cast(AsyncClient, get_async_supabase())


async def _admission_targets(
    supabase: AsyncClient, *, fallback: list[JobTarget] | None = None
) -> list[JobTarget]:
    """Targets whose keywords may admit a listing into the shared catalog.

    EVERY target when ``settings.admit_on_any_target`` (the default), not just
    the active ones. These are two different questions that one set used to
    answer: "what belongs in the corpus public /search reads" is about the
    CATALOG, while "what do we pay to grade" is about SPEND. Tying them together
    made the public corpus a side effect of who happened to have a target
    activated — it grew and shrank as people came and went.

    Concretely: the catalog held four INACTIVE frontend targets while the five
    active ones covered backend, full-stack, PM, design, data and devops. So
    "Frontend Engineer" matched nothing at the door and /search could never
    surface a recent frontend role. The keywords existed; nothing read them.

    This widens the keyword set, it does NOT open the door — admission stays
    deterministic and keyword-bounded. Spend is untouched: Phase 1 and Phase 2
    still grade against ``_active_targets`` alone, so a row admitted for an
    inactive target sits in the catalog ungraded, which is exactly what
    /search reads (it skips ``scores`` entirely).
    """
    if not settings.admit_on_any_target:
        return fallback if fallback is not None else await _active_targets(supabase)
    try:
        resp = await poll_db_read(
            supabase,
            lambda c: c.table(crud.TARGETS_TABLE).select("*"),
            label="poll admission-targets",
            retry_sync=True,
        )
    except Exception as exc:
        # Fall back to the ACTIVE set rather than failing the poll. This is the
        # conservative direction: admission NARROWS to what it was before this
        # setting existed, so a read blip costs breadth for one cycle instead
        # of stopping ingestion. Warned, not swallowed silently, because a
        # persistent failure quietly reverts the widening.
        #
        # ONE LINE, NO STACK -- this sits in the per-cycle path and a repeating
        # traceback here is how #652 flooded Railway's log replica cap, which
        # is exactly when the logs are needed. Same posture as
        # ``phase1_calls_today``.
        logger.warning(
            "admission targets unreadable (%s: %s) — falling back to active targets",
            type(exc).__name__,
            exc,
        )
        # ``fallback`` when the caller already holds the active set, so a failed
        # read costs no second round trip (and cannot fail twice on the same
        # table).
        return fallback if fallback is not None else await _active_targets(supabase)
    rows = cast(list[dict[str, Any]], resp.data or [])
    out: list[JobTarget] = []
    for r in rows:
        try:
            out.append(crud._parse_target(r))
        except Exception:
            # A malformed target must not stop the catalog from ingesting.
            logger.warning("admission targets: skipping unparseable target %s", r.get("id"))
    return out


async def _active_targets(supabase: AsyncClient) -> list[JobTarget]:
    """Async inline of ``crud.get_active`` (the sync twin stays for its non-poll
    callers): the derived ``app_active OR EXISTS(active membership)`` pipeline
    predicate — two indexed reads deduped in Python (see the crud docstring for
    the dropped-trigger history). Routed through the poll seam so it rides the
    pooled async client; mirror of ``routers.targets._active_targets``.
    """
    floor_resp = await poll_db_read(
        supabase,
        lambda c: c.table(crud.TARGETS_TABLE).select("*").eq("app_active", True),
        label="poll active-targets floor",
        retry_sync=True,
    )
    member_ids_resp = await poll_db_read(
        supabase,
        lambda c: c.table(crud.USER_TARGETS_TABLE).select("target_id").eq("is_active", True),
        label="poll active-targets members",
        retry_sync=True,
    )
    member_ids = {
        cast(str, r["target_id"]) for r in cast(list[dict[str, Any]], member_ids_resp.data or [])
    }
    rows = cast(list[dict[str, Any]], floor_resp.data or [])
    seen = {cast(str, r["id"]) for r in rows}
    missing = sorted(member_ids - seen)
    if missing:
        member_resp = await poll_db_read(
            supabase,
            lambda c, _missing=missing: c.table(crud.TARGETS_TABLE).select("*").in_("id", _missing),
            label="poll active-targets missing",
            retry_sync=True,
        )
        rows.extend(cast(list[dict[str, Any]], member_resp.data or []))
    return [crud._parse_target(r) for r in rows]


async def _is_pipeline_active(supabase: AsyncClient, target_id: str) -> bool:
    """Async inline of ``crud.is_pipeline_active`` (sync twin stays for the bulk
    re-scorer): the instance floor OR any active membership. Seam-routed."""
    t_resp = await poll_db_read(
        supabase,
        lambda c: c.table(crud.TARGETS_TABLE).select("app_active").eq("id", target_id).limit(1),
        label="poll pipeline-active target",
        retry_sync=True,
    )
    t_rows = cast(list[dict[str, Any]], t_resp.data or [])
    if not t_rows:
        return False
    if bool(t_rows[0].get("app_active")):
        return True
    m_resp = await poll_db_read(
        supabase,
        lambda c: (
            c.table(crud.USER_TARGETS_TABLE)
            .select("id", count="exact", head=True)
            .eq("target_id", target_id)
            .eq("is_active", True)
        ),
        label="poll pipeline-active members",
        retry_sync=True,
    )
    return bool(m_resp.count or 0)


async def _latest_optimized(supabase: AsyncClient, user_id: str | None) -> OptimizedDoc | None:
    """Async inline of ``optimized.get_latest`` (sync twin stays for the request
    path's TTL cache + non-poll callers). Seam-routed; skips the module TTL
    cache (the poller reads a user's doc at most once per cycle, and the cache
    exists for the hot request path)."""
    resp = await poll_db_read(
        supabase,
        lambda c: (
            c.table(optimized.TABLE)
            .select("*")
            .eq("user_id", resolve_owner(user_id))
            .order("version", desc=True)
            .limit(1)
        ),
        label="poll optimized-doc read",
        retry_sync=True,
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        return None
    return OptimizedDoc.model_validate(rows[0])


async def _resolve_user_targets_for_stage3(
    supabase: AsyncClient,
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
            doc = await _latest_optimized(supabase, user_id)
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


# Rotating KEYSET cursor for the liveness sweep below — the last
# ``(cataloged_at, id)`` pair it handed out, or None to (re)start at the oldest.
#
# Under LAZY tagging "untagged" is the NORMAL state, so the sweep's
# ``role_family IS NULL`` selection no longer shrinks as it runs the way it did
# when the sweep itself filled ``role_family`` — ordered oldest-first with a
# plain LIMIT it would re-check the SAME oldest batch every cycle forever and
# never reach the row after it. So it has to walk.
#
# It walks by KEYSET, not OFFSET. An offset over this predicate silently SKIPS
# rows: the set is unstable (a row leaves it the moment this sweep archives it),
# so when earlier rows drop out the later ones shift backward underneath a
# cursor that only moves forward, and whatever slid past the offset is never
# checked. ``(cataloged_at, id)`` is a total order — ``cataloged_at`` is NOT
# NULL across the table — so "everything after this pair" is exact regardless of
# what left the set in between.
#
# STILL IN-PROCESS, deliberately: a restart re-starts the walk at the oldest
# row. That costs coverage of the tail on a deploy-heavy day, but it is a
# fairness question, not a correctness one, and the check it repeats is a bounded
# HTTP validate — no LLM spend. ``url_health`` is the mechanism that solves this
# properly, with a real persisted cursor column (``last_url_check_at``) and a
# consecutive-failure threshold; consolidating onto it is the follow-up if tail
# coverage ever proves inadequate. It cannot simply replace this sweep today:
# at ``url_health_batch_size`` 250/24h it needs ~7 weeks for one pass of the
# live corpus, where this sweep covers a batch every cycle.
_QUALIFY_BACKFILL_CURSOR: tuple[str, str] | None = None


async def _backfill_qualify_stale(supabase: AsyncClient, limit: int) -> None:
    """Liveness-check a rotating batch of untagged, unarchived jobs (#285) and
    archive the ones whose listing is gone. NO LLM SPEND.

    This sweep used to TAG the live rows it found. That half is gone: tagging
    is LAZY now (``qualification.materialize.ensure_job_tags`` buys a listing's
    tags at grade time, when something is about to read them), so "untagged" is
    the normal state of the catalog rather than a backlog to drain. Left as it
    was — ``role_family IS NULL``, oldest first, every cycle — this sweep would
    have quietly re-bought the entire catalog's tags a batch at a time and
    re-introduced exactly the spend lazy tagging removes. Tags are no longer
    its job; a row nobody grades simply stays NULL, which every read gate
    treats permissively.

    The liveness half stays, because it is free and nothing else covers it on
    this path: a job that fell off its source's feed without being archived is
    never re-visited, so a dead listing lingers on every serving surface (the
    stale-but-shown postings #285 found). Each selected row gets a cheap check
    (SSRF-safe, via ``validate_job_url``), then:

    * DEAD  → archive: a hard 4xx is a confident "gone".
    * LIVE / UNKNOWN (a 200 that isn't a job, timeout, 5xx) → left untouched
      and re-checked on a later rotation — archival is sticky, so it needs the
      hard 4xx signal.
    """
    global _QUALIFY_BACKFILL_CURSOR
    if limit <= 0:
        return
    cursor = _QUALIFY_BACKFILL_CURSOR

    def _select(c: Any) -> Any:
        q = (
            c.table("jobs")
            .select("id, absolute_url, cataloged_at")
            .is_("role_family", "null")
            .is_("archived_at", "null")
        )
        if cursor is not None:
            seen_at, seen_id = cursor
            # (cataloged_at, id) > (seen_at, seen_id). Values are quoted because
            # a timestamptz carries '+' and ':' which PostgREST would otherwise
            # read as filter syntax.
            q = q.or_(
                f'cataloged_at.gt."{seen_at}",and(cataloged_at.eq."{seen_at}",id.gt."{seen_id}")'
            )
        return q.order("cataloged_at", desc=False).order("id", desc=False).limit(limit)

    try:
        resp = await poll_db_read(supabase, _select, label="poll qualify-backfill select")
    except Exception:
        logger.exception("Qualification backfill: select failed; skipping this cycle")
        return
    rows = cast(list[dict[str, Any]], resp.data or [])
    # Advance to the last pair handed out. A short (or empty) page means we
    # reached the end of the untagged set, so wrap back to the oldest.
    if len(rows) == limit and rows[-1].get("cataloged_at") and rows[-1].get("id"):
        _QUALIFY_BACKFILL_CURSOR = (
            str(rows[-1]["cataloged_at"]),
            str(rows[-1]["id"]),
        )
    else:
        _QUALIFY_BACKFILL_CURSOR = None
    if not rows:
        return

    # A fresh semaphore (sized like the DB-write cap) bounds the HTTP fan-out;
    # each check is timeout-capped inside ``validate_job_url`` — also the ONLY
    # SSRF-safe way to fetch these arbitrary posting URLs.
    sem = asyncio.Semaphore(DB_WRITE_CONCURRENCY)
    dead: list[str] = []

    async def _check(row: dict[str, Any]) -> None:
        url = row.get("absolute_url")
        if not url:
            return  # no URL to verify — leave it alone
        async with sem:
            try:
                verdict = liveness_verdict(await validate_job_url(cast(str, url)))
            except Exception:
                return  # transient — retry on a later rotation
        if verdict == "dead":
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


async def _drop_purged_rows(
    supabase: AsyncClient, source_id: str, rows: list[dict[str, Any]]
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


async def _backfill_grade_stale(supabase: AsyncClient, limit: int) -> None:
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
        active_targets = await _active_targets(supabase)
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
        backfill_reason = gate.user_block_reason(uid)
        if backfill_reason is not None:
            logger.info(
                "Grade backfill deferred for user %s / target %s (%s)",
                uid,
                target.id,
                backfill_reason,
            )
            continue
        llm = await _resolve_payer_client(payer_clients, _async_service_client(), uid)
        if llm is None:
            continue  # BYOK require-mode without a key — defer (logged inside)
        try:
            resp = await poll_db_read(
                supabase,
                lambda c, tid=target.id: (
                    c.table("scores")
                    # The embedded jobs projection must carry every column the
                    # grade-time tagger reads (``TAG_INPUT_COLUMNS``) — the
                    # content-hash inputs, the archived guard and #846's
                    # board-defer keys. A partial projection degrades the
                    # tagger silently (permanent cache miss, board answer
                    # overwritten); ``test_grade_backfill`` pins it.
                    .select(
                        "recency_score, jobs!inner(id, title, company_name, location, "
                        "country, description_html, cataloged_at, archived_at, purged_at, "
                        "is_us, role_family, is_remote, employment_type, "
                        "qualified_hash, qualified_at)"
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
                _async_service_client(),
                llm,
                target=target,
                payload=user_optimized[uid].payload,
                jobs=stale_jobs,
                user_id=uid,
                tag_budget_exhausted=_tagger_budget_exhausted,
            )
        except Exception:
            logger.exception("Grade backfill failed for target %s", target.id)
            continue


async def _resolve_payer_client(
    cache: dict[str | None, LLMClient | None],
    supabase: AsyncClient | None,
    payer_user_id: str | None,
) -> LLMClient | None:
    """Per-payer LLM client for background grading (#5 P3 BYOK).

    Each payer's background LLM work bills the payer's own OpenRouter key
    (via ``llm.get_client_async``), not the instance key. Memoized by payer for
    the duration of one source poll, so the N targets/jobs a payer owns
    reuse a single client — one key decrypt, and that payer's calls stay
    grouped rather than interleaved across keys (interleaving would
    cold-start each key's prompt cache).

    Returns ``None`` when the payer can't be served with their own key
    (``MissingUserKeyError``). TWO independent conditions raise it, and
    conflating them has cost a misdiagnosis (#841): either
    ``BYOK_REQUIRE_USER_KEYS`` is set, **or** the payer's plan is BYOK
    (``entitlements_for(plan).llm_key_source == "byok"`` — the saas free
    tier) — see ``llm.get_client_async``. The plan branch fires on its own,
    so this defer is routine on a hosted deployment even with the flag unset.

    Callers defer that payer's grading — jobs stay promising / score NULL
    and grade on a later cycle once a key is added — exactly like the
    over-allowance defer, never billing the operator key for a stranger.
    A ``None``/unattributable payer resolves to the instance key, unchanged
    from P2.
    """
    if payer_user_id not in cache:
        try:
            cache[payer_user_id] = await get_llm_client_async(supabase, payer_user_id)
        except TrialExpiredError:
            # A lapsed trial stops costing money the moment it lapses: the
            # same defer path as a missing key, so background grading halts
            # without needing a separate sweep (#841).
            logger.info("Background grading deferred for payer %s (trial expired)", payer_user_id)
            cache[payer_user_id] = None
        except MissingUserKeyError:
            logger.info(
                "Background grading deferred for payer %s (no stored BYOK key; %s)",
                payer_user_id,
                "BYOK_REQUIRE_USER_KEYS is set"
                if settings.byok_require_user_keys
                else "their plan requires BYOK",
            )
            cache[payer_user_id] = None
    return cache[payer_user_id]


async def _call_fetcher(
    fetcher: Any,
    board_token: str,
    known_postings: dict[str, KnownPosting],
    admissible: Callable[[str, str | None], bool],
) -> list[StandardJob]:
    """Call a provider fetcher, handing Workday what we already hold.

    Only Workday needs it — every other provider returns content in its list
    call, so there is no per-posting request to skip. Signature-sniffed rather
    than branched on the provider string so a fetcher that grows the parameter
    picks it up without another edit here.
    """
    params = inspect.signature(fetcher).parameters
    extra: dict[str, Any] = {}
    if known_postings and "known" in params:
        extra["known"] = known_postings
    if "admissible" in params:
        extra["admissible"] = admissible
    return cast(list[StandardJob], await fetcher(board_token, **extra))


async def _poll_one_source(
    source: dict[str, Any],
    supabase: AsyncClient,
    budget_gate: PayerBudgetGate | None = None,
    admission_budget: AdmissionBudget | None = None,
    intake_budget: IntakeBudget | None = None,
    *,
    active_targets: list[JobTarget] | None = None,
    admission_targets: list[JobTarget] | None = None,
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
        # Negative-store consult counters (summed across targets); folded
        # into one INFO line per cycle by ``poll_due_sources``.
        "phase1_store_hits": 0,
        "phase1_store_misses": 0,
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

        # What we already hold for this source, read BEFORE the fetch so
        # Workday can skip the per-posting detail request for postings whose
        # list entry is unchanged. Cheap columns only, and a failure here just
        # means every detail is fetched — today's behaviour.
        # Targets are normally resolved once per cycle by the caller; the
        # fallback keeps direct/legacy callers working. Resolved BEFORE the
        # fetch so Workday can apply the free gates to its list entries and
        # skip the detail request for postings we would only drop.
        if active_targets is None:
            active_targets = await _active_targets(supabase)
        if admission_targets is None:
            admission_targets = await _admission_targets(supabase, fallback=active_targets)

        def _admissible(title: str, location: str | None) -> bool:
            """The two FREE gates, judged on a provider's list entry. Mirrors
            the row-build loop below — keep them in step, or a posting gets
            skipped here that the loop would have kept."""
            return _admits_for_catalog(
                title, active_targets or [], admission_targets
            ) and _is_us_location(location)

        known_postings: dict[str, KnownPosting] = {}
        if provider == "workday":
            try:
                pre_resp = await poll_db_read(
                    supabase,
                    lambda c: (
                        c.table("jobs")
                        .select("external_id, title, source_posted_at")
                        .eq("source_id", source_id)
                        .is_("archived_at", "null")
                    ),
                    label=f"poll known postings {company_name}",
                    retry_sync=True,
                )
                known_postings = {
                    str(r["external_id"]): KnownPosting(
                        title=r.get("title"), posted_at_stored=r.get("source_posted_at")
                    )
                    for r in cast(list[dict[str, Any]], pre_resp.data or [])
                    if r.get("external_id")
                }
            except Exception:
                logger.warning(
                    "poll %s: known-postings read failed; fetching every detail",
                    company_name,
                    exc_info=True,
                )

        jobs = await _call_fetcher(fetcher, board_token, known_postings, _admissible)
        summary["polled"] = True

        # Collect ALL external IDs from the API (before title/location filtering)
        # so we don't archive jobs that exist on the board but don't match filters.
        all_external_ids: set[str] = {job.external_id for job in jobs}

        # (Targets were resolved above, before the fetch, so the free gates
        # could be handed to the provider.)

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
            lambda c: (
                c.table("jobs").select("external_id, content_hash").eq("source_id", source_id)
            ),
            label=f"poll known ids {company_name}",
            retry_sync=True,
        )
        known_rows_read = cast(list[dict[str, Any]], known_ids_resp.data or [])
        known_external_ids = {r.get("external_id") for r in known_rows_read}
        # external_id → stored content hash (#642): drives the unchanged-row
        # skip before the upsert. NULL for pre-migration rows (always write
        # once, stamping the hash).
        known_hashes: dict[str | None, str | None] = {
            r.get("external_id"): r.get("content_hash") for r in known_rows_read
        }

        # Payer/allowance snapshot: who pays for each target's LLM work,
        # and which payers are over their monthly allowance. Built once
        # per cycle by the entry points; locally as a fallback.
        gate = budget_gate
        if gate is None:
            try:
                gate = await build_budget_gate(
                    _async_service_client(), [t.id for t in active_targets]
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
        # How many targets abstained because their block is PERSISTENT. Non-zero
        # means an empty ``phase1_verdicts`` is the ramped fallback, not the
        # ordinary "triage off / no targets" admit — see ``_any_target_admits``.
        persistent_skips = 0
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
                    # Blocked target — spend nothing this cycle. Either a
                    # sponsored target whose payer is over allowance /
                    # idle / disabled, or (default) a CATALOG-ONLY target
                    # nobody is pursuing — see ``grade_catalog_targets``.
                    #
                    # A blocked target casts no verdict. Whether it still gets
                    # a VOTE at the admission gate depends on whether the block
                    # can clear on its own.
                    #
                    # TRANSIENT (over allowance): registering empty verdicts +
                    # empty ``attempted`` makes every title take the
                    # NOT-attempted arm of ``_phase1_promising`` → DEFER:
                    # excluded from scores now, re-triaged next cycle when the
                    # rolling window frees up. That is #285's rule and it is
                    # correct here, because "next cycle" genuinely arrives.
                    #
                    # PERSISTENT (idle / catalog-ungraded / disabled / no
                    # snapshot): "next cycle" never arrives, so registering the
                    # target would let it veto ingestion FOREVER. And it does
                    # veto ingestion — the comment that used to sit here
                    # claimed "the JOB row itself is unaffected... already
                    # ingested before scoring", which is false:
                    # ``_any_target_admits`` runs AT the upsert gate, before
                    # the row is written. Prod proved it — one idle account put
                    # every active target into a persistent block and new-job
                    # ingestion stopped dead for 50 hours (~287 free-gate
                    # survivors discarded per log window, `new=0` every cycle)
                    # while `updated=N` kept flowing, because known rows bypass
                    # the gate. This is the ingestion-starvation class
                    # ``payers.target_blocked``'s HISTORY note exists to
                    # prevent, reached by a path that note does not cover:
                    # the gate suppresses only LLM work, but ADMISSION DEPENDS
                    # ON AN LLM VERDICT, so suppressing the LLM suppressed
                    # admission anyway.
                    #
                    # So a persistently-blocked target simply does not
                    # participate. If it was the only one, ``phase1_verdicts``
                    # stays empty and ``_any_target_admits`` falls back to the
                    # deterministic free gates that already passed.
                    reason = gate.target_block_reason(active_target.id)
                    admits = block_admits_ingestion(
                        reason,
                        staged_rollout=settings.persistent_block_admits_ingestion,
                    )
                    if admits:
                        persistent_skips += 1
                    if not admits:
                        phase1_verdicts[active_target.id] = {}
                        phase1_attempted[active_target.id] = set()  # → all defer
                    # Three distinct outcomes, said plainly. "will re-triage"
                    # is only TRUE for a transient block, where the next cycle
                    # genuinely retries — claiming it for a persistent one
                    # repeats the overstatement #917 existed to fix.
                    if admits:
                        outcome = "not vetoing ingestion"
                    elif block_is_persistent(reason):
                        outcome = (
                            "deferring; persistent block and "
                            "persistent_block_admits_ingestion is off, so this will NOT "
                            "re-triage until the block lifts"
                        )
                    else:
                        outcome = "deferring, will re-triage next cycle"
                    logger.info(
                        "Phase 1 deferred for target %s (payer %s blocked: %s; %s)",
                        active_target.id,
                        gate.payer_for(active_target.id),
                        reason,
                        outcome,
                    )
                    continue
                # BYOK (#5 P3): grade on the payer's own key. No key in
                # hosted require-mode → defer like over-allowance above
                # (empty verdicts → fail-open ingest, grade once a key is
                # added).
                payer = gate.payer_for(active_target.id)
                llm = await _resolve_payer_client(payer_clients, _async_service_client(), payer)
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
                # Negative-verdict store (#514, persistent): titles this
                # target's LLM already rejected within the TTL skip the model
                # and re-enter as a synthetic promising=False verdict, marked
                # attempted — the downstream gates treat them exactly like a
                # fresh "no" (rejected, not budget-deferred). Only the
                # remainder is actually sent. One chunked read per
                # (source, target); fail-open to the LLM on store errors.
                cached_rejections = await fetch_rejected_titles(
                    supabase, active_target, [j.title for _, j in triage_candidates]
                )
                send_candidates: list[tuple[int, StandardJob]] = []
                for cand_idx, cand_job in triage_candidates:
                    if normalize_title(cand_job.title) in cached_rejections:
                        global_idx = cand_idx + 1
                        target_verdicts[global_idx] = TitleVerdict(id=global_idx, promising=False)
                        attempted_here.add(global_idx)
                    else:
                        send_candidates.append((cand_idx, cand_job))
                summary["phase1_store_hits"] += len(triage_candidates) - len(send_candidates)
                summary["phase1_store_misses"] += len(send_candidates)
                titles = [job.title for _, job in send_candidates]
                # Raw promising=False verdicts collected here persist in ONE
                # write after the batch loop (budget breaks keep what stands).
                rejected_now: list[tuple[str, int | None]] = []
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
                    # #930: the per-target daily COUNT cap, shared with the
                    # activation backfill. The $-rails above bound the bill;
                    # this bounds the call volume, which is what a runaway
                    # looks like before it is expensive. Same break semantics
                    # as the budget check — collected verdicts stand, the rest
                    # defer to the next cycle.
                    if await phase1_cap_reached(supabase, active_target.id):
                        logger.warning(
                            "Phase 1 triage: per-target daily call cap (%d) reached "
                            "for target %s — deferring remaining titles",
                            settings.phase1_daily_cap,
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
                            await record_llm_cost_async(
                                _async_service_client(),
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
                    # store so future cycles skip the model for this title.
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
                                rejected_now.append(
                                    (send_candidates[subset_pos][1].title, verdict.confidence)
                                )
                if rejected_now:
                    await record_rejections(supabase, active_target, rejected_now)
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
                if persistent_skips and admission_budget is not None:
                    # Every target abstained on a PERSISTENT block, so this
                    # admit is the fallback to the deterministic free gates —
                    # the path that drains the backlog. Ramp it: the untouched
                    # backlog is ~14,800 rows, and admitting it in one tick
                    # drags a burst of score rows, activation tagging and
                    # archival behind it. A job that loses the race is dropped
                    # exactly as before and re-offered next cycle, so nothing is
                    # lost — only slowed.
                    return admission_budget.take()
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
        dropped_intake_cap = 0
        dropped_title_prematch = 0
        dropped_non_us = 0
        for idx, job in enumerate(jobs):
            # Filter by target relevance instead of static keyword list. With
            # NO active targets there is nothing to match against, so drop
            # everything (previously the `active_targets and` guard SKIPPED this
            # gate when empty, ingesting whole boards of untargeted roles).
            # ADMISSION uses the widened set (``_admission_targets``): a
            # listing belongs in the shared catalog if ANY target's keywords
            # want it, active or not. Phase 1 below still grades against
            # ACTIVE targets only -- corpus and spend are separate questions.
            #
            # ``or active_targets`` is a LAST-RESORT floor, not a policy: the
            # active set is a strict SUBSET of all targets, so a healthy wide
            # read can only be empty when the active one is too, and the two
            # branches agree. It diverges in exactly one case -- every row of
            # the wide read failing to parse, which the loader logs per row --
            # and there the floor keeps ingestion at its pre-existing breadth
            # instead of silently admitting nothing. If "zero admission targets
            # means admit nothing" ever needs to be a strict invariant, this is
            # the line to change; it is deliberate today, not truthiness by
            # accident. (Raised in review of #952.)
            if not _admits_for_catalog(job.title, active_targets, admission_targets):
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
            is_new_row = job.external_id not in known_external_ids
            if is_new_row and not _any_target_admits(idx + 1):
                dropped_phase1 += 1
                continue

            # The GLOBAL hourly intake ceiling, applied AFTER the relevance
            # gates so it never changes WHICH listings are worth admitting —
            # only how fast the ones already judged worthy may land. Applies to
            # every admission path, including the ordinary triage-said-yes one
            # that ``_any_target_admits`` waves straight through, because write
            # pressure does not care why a row was admitted.
            #
            # Known rows are exempt: they update a row that already exists, so
            # they add none of the insert pressure this bounds, and blocking
            # them would re-create the #514 content-refresh starvation.
            if is_new_row and intake_budget is not None and not intake_budget.take():
                dropped_intake_cap += 1
                continue

            if job.detail_skipped:
                # Held unchanged, detail deliberately not fetched — ``content``
                # is empty, so building a row here would blank the stored
                # description. It stays in ``all_external_ids`` above, so the
                # stale-archive pass still counts it as seen.
                continue

            salary = job.salary_text or extract_salary_from_html(job.content)
            loc = parse_location(job.location_name)

            phase1_idx_by_external_id[job.external_id] = idx + 1
            rows_to_upsert.append(
                {
                    "external_id": job.external_id,
                    "source_id": source_id,
                    "title": job.title,
                    "title_display": clean_title_display(job.title),
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
                    **board_columns(job, loc),
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
        board_us_marked = 0
        board_us_archived = 0
        location_us_marked = 0
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

        # #642: drop KNOWN rows whose refreshable payload is byte-identical
        # to what's stored — no jobs rewrite (TOAST included), and because
        # the scoring stages below iterate the UPSERT RESULT, no redundant
        # stage-1/2 rescore either. Content or salary changes alter the
        # hash and flow through unchanged-path-free. Profile-version bumps
        # rescore via bulk_score_for_target at bump time, not here.
        unchanged_skipped = 0
        if rows_to_upsert:
            rows_to_upsert, unchanged_skipped = _partition_unchanged(rows_to_upsert, known_hashes)
            if unchanged_skipped:
                summary["unchanged"] = unchanged_skipped

        # Re-check after dedupe: it can remove EVERY row (a source whose
        # postings are all cross-posting dupes of existing rows). Calling
        # ``.upsert([])`` then raises PGRST100 "failed to parse columns
        # parameter ()", which marks the poll failed and — after
        # ``source_failure_disable_threshold`` consecutive empties — would
        # auto-disable a perfectly healthy source. Skip the write instead.
        if rows_to_upsert:
            # #928: grouped by key-set, NOT one bulk upsert. PostgREST builds a
            # single statement from the union of the batch's keys, so a row that
            # omits ``is_remote`` / ``employment_type`` was being sent NULL for
            # it whenever a sibling in the same batch supplied one — blanking
            # exactly the board-silent postings ``board_columns`` promises to
            # leave alone. See ``poll_db_upsert``.
            upserted_rows = await poll_db_upsert(
                supabase,
                table="jobs",
                rows=rows_to_upsert,
                on_conflict="source_id,external_id",
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
            for data in upserted_rows:
                if data.get("external_id") in known_external_ids:
                    summary["updated"] += 1
                    known_upserted_ids.append(data["id"])
                else:
                    summary["new"] += 1
                    new_rows.append(data)

            # The board's own US verdict — free, deterministic, no LLM. This is
            # what restores the pruning lazy tagging took away for the
            # providers that publish a structured country.
            board_us_marked, board_us_archived = await _apply_board_us_verdicts(
                supabase, jobs, upserted_rows
            )
            # ...and then the location string, for the 61% of sources whose
            # board publishes no country at all. Second, never first: the
            # board's structured field is the stronger fact, so this fills only
            # what it left unknown. TRUE only — see _apply_location_us_verdicts.
            location_us_marked = await _apply_location_us_verdicts(supabase, jobs, upserted_rows)

            # Qualification tags are LAZY now, exactly like embeddings below:
            # ingest is $0 LLM. ``ensure_job_tags`` in the Phase-2 runner buys
            # a listing's tags at grade time, for the trimmed candidate set
            # about to consume them. What ingest still writes is FREE and
            # unchanged: ``board_columns`` (the board's own remote /
            # employment-type answer, #846) and the deterministic parses.

            # Job embeddings are LAZY too (Disk IO slim-down, 2026-07-30):
            # no embed-on-ingest — ``ensure_job_vectors`` in the Phase-2
            # runner materializes vectors for exactly the candidate set
            # about to be read ("only a few will ever be read").

            # Targets whose scores upsert hit the target-FK 23503 this cycle:
            # the target was reaped (account erasure / last-follower unlink)
            # after this cycle snapshotted active_targets (#869). One typed
            # error marks it here; every later stage skips it instead of
            # paying per-row for work the write must discard.
            reaped_targets: set[str] = set()

            # ---- Stage 1: Title scoring per target ----
            for active_target in active_targets:

                async def _title_score_one(
                    row_data: dict[str, Any], target: JobTarget = active_target
                ) -> None:
                    if target.id in reaped_targets:
                        return
                    try:
                        await target_title_score_and_upsert(
                            supabase,
                            job_posting_id=row_data["id"],
                            title=row_data.get("title", ""),
                            target=target,
                        )
                    except TargetReapedError:
                        if target.id not in reaped_targets:
                            reaped_targets.add(target.id)
                            logger.warning(
                                "Target %s was reaped mid-cycle; dropping its remaining "
                                "scoring for this cycle",
                                target.id,
                            )
                    except Exception:
                        logger.exception("Stage 1 scoring failed for job %s", row_data.get("id"))

                await asyncio.gather(*(_title_score_one(r) for r in upserted_rows))

            # ---- Stage 2: Full JD scoring per target (async) ----
            # Pre-parse each JD once, reuse across all targets
            jd_cache: dict[str, Any] = {}
            for rd in upserted_rows:
                jd_cache[rd["id"]] = parse_jd(rd.get("description_html") or "")

            for active_target in active_targets:
                if active_target.id in reaped_targets:
                    continue
                # Per-target Phase 1 verdicts (None when flag off): keyed by
                # the 1-based job idx assigned during the candidate-build
                # loop above. Each upserted row carries an
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
                    if target.id in reaped_targets:
                        return
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
                    except TargetReapedError:
                        if target.id not in reaped_targets:
                            reaped_targets.add(target.id)
                            logger.warning(
                                "Target %s was reaped mid-cycle; dropping its remaining "
                                "scoring for this cycle",
                                target.id,
                            )
                    except Exception:
                        logger.exception("Stage 2 scoring failed for job %s", row_data.get("id"))

                await asyncio.gather(*(_full_score_one(r) for r in upserted_rows))

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
                cycle_rows = list(upserted_rows)
                for uid, p2_target in primary_by_user.items():
                    if p2_target.id in reaped_targets:
                        # #869: the whole point of marking the reap in stage
                        # 1/2 — Phase 2 is the expensive LLM stage, and every
                        # grade for a reaped target is paid and then discarded
                        # at the write.
                        logger.info(
                            "Phase 2 skipped for user %s / target %s (target reaped mid-cycle)",
                            uid,
                            p2_target.id,
                        )
                        continue
                    p2_reason = gate.user_block_reason(uid)
                    if p2_reason is not None:
                        # Defer. Jobs keep promising=True/score=NULL and get
                        # graded once the payer unblocks — which is NOT always
                        # a budget window: an idle or operator-disabled payer
                        # lands here too, so the reason is logged, not assumed.
                        logger.info(
                            "Phase 2 deferred for user %s / target %s (%s)",
                            uid,
                            p2_target.id,
                            p2_reason,
                        )
                        continue
                    # BYOK (#5 P3): grade on this user's own key; no key in
                    # hosted require-mode defers like over-allowance.
                    llm = await _resolve_payer_client(payer_clients, _async_service_client(), uid)
                    if llm is None:
                        logger.info(
                            "Phase 2 deferred for user %s / target %s (no BYOK key)",
                            uid,
                            p2_target.id,
                        )
                        continue
                    try:
                        await run_phase2_for_jobs(
                            _async_service_client(),
                            llm,
                            target=p2_target,
                            payload=user_optimized[uid].payload,
                            jobs=cycle_rows,
                            user_id=uid,
                            tag_budget_exhausted=_tagger_budget_exhausted,
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
            stage2_ids = [r["id"] for r in upserted_rows]

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
            # Mass-archive guard. The five ATS list fetchers now raise
            # ``BoardFetchError`` instead of swallowing an API error into [],
            # so a failed fetch never reaches this line — but the guard stays
            # as defence in depth: the jsonld / crawl / mock fetchers still
            # return [] on failure, and any future fetcher might. Archiving
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
                await notify.send_alerts_for_new_jobs(_async_service_client(), alert_rows)
            except Exception:
                logger.exception("Email alert dispatch raised for %s", company_name)
            try:
                await notify.send_sms_alerts_for_new_jobs(_async_service_client(), alert_rows)
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
            "dropped_intake_cap=%d "
            "dropped_title_prematch=%d dropped_non_us=%d candidates=%d "
            "upserted_new=%d upserted_updated=%d archived=%d "
            "board_us_marked=%d board_us_archived=%d location_us_marked=%d "
            "phase1_no_by_target=%s",
            company_name,
            len(jobs),
            dropped_phase1,
            dropped_intake_cap,
            dropped_title_prematch,
            dropped_non_us,
            len(rows_to_upsert),
            summary["new"],
            summary["updated"],
            summary["archived"],
            board_us_marked,
            board_us_archived,
            location_us_marked,
            per_target_phase1_no or "{}",
        )

    except BoardFetchError as exc:
        # The board did not answer with a listing (404/410/422, a 5xx that
        # outlived its retries, a WAF page served as 200). Routine upstream
        # weather rather than a code bug, so it gets one WARNING line instead
        # of a traceback — but it is still a FAILED poll, so it counts toward
        # ``source_failure_disable_threshold``. Before this the fetchers
        # returned [], the poll looked successful, and the counter below was
        # reset to 0 every cycle — the threshold could never fire and a dead
        # board was re-polled forever.
        logger.warning("Poll failed for %s: %s", company_name, exc)
        summary["error"] = f"{company_name}: board fetch failed"
        await _record_source_failure(supabase, source, error=str(exc))
    except Exception as exc:
        logger.exception("Poll failed for %s", company_name)
        summary["error"] = f"{company_name}: poll failed"
        await _record_source_failure(supabase, source, error=repr(exc))

    return summary


# Truncate stored failure text so a giant traceback/HTML body can't bloat
# the row (the column is queryable signal, not a log store).
_SOURCE_LAST_ERROR_MAX_LEN = 500

# #962: re-detection outcomes accumulated across a cycle. Per-event log lines
# exist (RE-POINTED / still-live / collision), but spotting drift -- a new ATS
# migration wave, a cohort of boards dying at once -- from individual lines is
# log archaeology. Each cycle end logs-and-clears the aggregate. Keys are the
# _redetect_before_disabling verdicts plus listings_escalated.
_redetect_cycle_counts: Counter[str] = Counter()


def _log_redetect_cycle_summary() -> None:
    """Emit and reset the cycle's re-detection outcome counters (#962).

    Called from both cycle ends (full + due-source). Silent when the cycle
    had no threshold-crossing sources -- most cycles -- so the line only
    appears when there is something to aggregate."""
    if _redetect_cycle_counts:
        logger.info("redetect outcomes this cycle: %s", dict(_redetect_cycle_counts))
        _redetect_cycle_counts.clear()


async def _record_source_failure(
    supabase: AsyncClient, source: dict[str, Any], *, error: str | None = None
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

    ``last_polled_at`` is stamped here too, same as the per-source
    budget-timeout path: the cycle takes the ``poll_max_sources_per_cycle``
    MOST OVERDUE sources, ordered by ``last_polled_at``, so a source that
    never gets stamped pins itself to the FRONT of that queue and re-hogs a
    slot every tick — crowding out healthy sources. A failing source has to
    rotate to the back like any other. The stamp deliberately touches nothing
    else: no ``job_count``, and emphatically no counter reset — this was not a
    clean poll.

    At the threshold — and only there — :func:`_redetect_before_disabling`
    gets a say first (#912): a board that stopped answering may have moved
    rather than died, and it may not even be dead. That can re-point this row
    onto the company's live board (in which case nothing else is written) or
    veto the disable, but it can never manufacture one.
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
            # Rotate to the back of the most-overdue-first queue (see docstring).
            "last_polled_at": now_iso,
        }
        if error:
            updates["last_error"] = error[:_SOURCE_LAST_ERROR_MAX_LEN]
        disabling = failures >= threshold
        if disabling and settings.source_redetect_on_disable_enabled:
            # #912: the threshold says "this board stopped answering", which is
            # not the same claim as "this company stopped hiring". Ask the ATSs
            # before acting on the identifier. Gated on the threshold, not on
            # every failure, so the probing is bounded to the handful of
            # sources that would otherwise be disabled this cycle.
            verdict = await _redetect_before_disabling(supabase, source, failures=failures)
            _redetect_cycle_counts[verdict] += 1
            if verdict == "repointed":
                # The row now points at the live board and the failure is
                # resolved — the failure/disable write below would undo it.
                return
            disabling = verdict == "disable"
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
        if disabling:
            # #962: the disabled board's listings are the likeliest dead URLs
            # in the corpus — get them per-listing verdicts promptly instead
            # of on the background rotation. Internally best-effort.
            escalated = await escalate_source_listings(supabase, source_id)
            if escalated:
                _redetect_cycle_counts["listings_escalated"] += escalated
                logger.info(
                    "Escalated %d live listing(s) of disabled source %s to the "
                    "front of the url_health queue",
                    escalated,
                    company,
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


async def _redetect_before_disabling(
    supabase: AsyncClient, source: dict[str, Any], *, failures: int
) -> Literal["repointed", "suppress", "disable"]:
    """Re-detect a source the backoff is about to disable (#912).

    Returns what the caller should do:

    ``repointed``
        The row has already been rewritten to the company's live board. The
        caller must NOT also write the failure payload — that would put the
        failure counter and ``last_error`` back on a source that is now
        healthy.
    ``suppress``
        The board we hold is still live, so this was a transient failure and
        disabling would be wrong. The caller still records the failure (the
        counter keeps climbing and stays queryable) but leaves ``enabled``
        alone.
    ``disable``
        Nothing found, or the live board is already owned by another source.
        Today's behaviour, unchanged.

    Best-effort throughout: any unexpected failure degrades to ``disable``.
    """
    company = source.get("company_name", source.get("id"))
    source_id = source.get("id")
    try:
        outcome = await redetect_source(supabase, source)
    except Exception:
        logger.exception("re-detect failed for %s; disabling as usual", company)
        return "disable"

    if outcome.action == "still_live":
        # Say what was observed, not what it implies. A probe establishes that
        # the board answered ONE lightweight request just now; the production
        # fetch path uses different shapes (pagination, per-posting detail) and
        # is demonstrably still failing — that is why we are here.
        logger.warning(
            "Source %s hit %d consecutive failures but a probe of its board "
            "(%s/%s) %s — NOT disabling; the normal poll is still failing",
            company,
            failures,
            source.get("provider"),
            source.get("board_token"),
            (
                f"answered within the last "
                f"{settings.source_redetect_still_live_cooldown_hours}h "
                f"(not re-probed this cycle)"
                if outcome.from_cooldown
                else "answered"
            ),
        )
        return "suppress"

    if outcome.action == "collision":
        logger.warning(
            "Source %s moved to %s/%s but source %s already owns that "
            "board_token — skipping the re-point (duplicate company); "
            "disabling after %d consecutive failures",
            company,
            outcome.provider,
            outcome.board_token,
            outcome.blocked_by,
            failures,
        )
        return "disable"

    if outcome.action != "repoint":
        return "disable"

    # Re-point the EXISTING row rather than registering a new source: jobs
    # carry ``source_id`` as an FK, so a new row would orphan every listing we
    # already hold for this company. ``company_name`` is deliberately left
    # alone — it is user-visible and feeds the dedup key, so a probe is not
    # licence to rewrite it.
    repoint: dict[str, Any] = {
        "provider": outcome.provider,
        "board_token": outcome.board_token,
        # The failure is resolved, so clear the whole failure state — same
        # shape a clean poll writes.
        "consecutive_failures": 0,
        "last_error": None,
        "last_error_at": None,
        # Rotate to the back of the most-overdue-first queue: this source just
        # consumed a poll slot (see _record_source_failure's docstring).
        "last_polled_at": datetime.now(UTC).isoformat(),
    }
    try:
        await poll_db_write(
            supabase,
            lambda c: c.table("sources").update(repoint).eq("id", source_id),
            label="poll source re-point",
        )
    except Exception:
        # A losing race on UNIQUE(board_token) lands here. Fall through to the
        # disable rather than leaving the source in limbo.
        logger.exception(
            "re-point write failed for %s (%s/%s); disabling as usual",
            company,
            outcome.provider,
            outcome.board_token,
        )
        return "disable"

    # Loud on purpose. This mutates a SHARED catalogue row's identity on the
    # strength of a heuristic probe, and every one of them should be greppable
    # in the Railway logs (and reversible by hand from this line alone).
    logger.warning(
        "Source %s RE-POINTED after %d consecutive failures: %s/%s -> %s/%s (%d live postings)",
        company,
        failures,
        source.get("provider"),
        source.get("board_token"),
        outcome.provider,
        outcome.board_token,
        outcome.job_count,
    )
    return "repointed"


async def recover_stale_sources(supabase: AsyncClient, *, now: datetime | None = None) -> int:
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


async def _global_budget_exhausted(supabase: AsyncClient, *, reserve_usd: float = 0.0) -> bool:
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
    in long per-job LLM loops (the Phase-1 triage batch loops, and the
    qualification tagger via :func:`_tagger_budget_exhausted` — the tagger
    lives outside this module now and takes the check as an injected
    callable). One implementation so every LLM-spending path reads the
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
    return await _memoized_total_spend(supabase, midnight) >= effective_cap


# #642: TTL memo for the day-spend aggregate. The mid-loop budget re-checks
# (per qualify chunk, per triage batch) each re-aggregated today's llm_costs
# — pg_stat showed the spend read among the top time consumers (236k calls).
# One chunk of LLM work takes minutes, so a 60s-stale meter changes nothing
# operationally: worst case one extra ~15-job chunk (~cents) before the next
# fresh read trips the breaker. Cache keys on the midnight boundary so the
# UTC-day rollover naturally invalidates.
_SPEND_MEMO_TTL_S = 60.0
_spend_memo: dict[str, Any] = {"at": 0.0, "midnight": None, "value": 0.0}


async def _memoized_total_spend(supabase: AsyncClient, midnight: datetime) -> float:
    now = time.monotonic()
    if _spend_memo["midnight"] == midnight and now - _spend_memo["at"] < _SPEND_MEMO_TTL_S:
        return cast(float, _spend_memo["value"])
    value = await total_llm_spend_all_async(supabase, since=midnight)
    _spend_memo.update(at=now, midnight=midnight, value=value)
    return value


async def _tagger_budget_exhausted() -> bool:
    """The grade-time qualification tagger's self-imposed-cap check.

    ``qualification.materialize.ensure_job_tags`` lives outside this module (the
    poller imports ``app.services.fit``, so the Phase-2 runner cannot import the
    poller back), and the spend meter stays HERE with the triage loops and the
    cycle breaker — so the poller injects this bound predicate into
    ``run_phase2_for_jobs`` instead of the tagger re-deriving one. Same meter,
    same cap, same grading reserve the tagger has always yielded: it stops at
    ``cap - grading_budget_reserve_usd`` so the top slice stays available for
    the Phase-1/Phase-2 grades themselves. Raising is meaningful — the tagger
    fails CLOSED on a meter-read error.
    """
    return await _global_budget_exhausted(
        _async_service_client(), reserve_usd=settings.grading_budget_reserve_usd
    )


async def _triage_budget_blocks(supabase: AsyncClient) -> bool:
    """Async, fail-OPEN global-budget check for the Phase-1 triage loops.

    Reads the meter off-thread (it's a sync DB call). Returns True only when
    the global daily cap is provably reached. A meter-read error fails OPEN
    (returns False → keep triaging) on purpose: triage is already protected
    by the once-per-cycle ``gate.target_blocked`` breaker, so this per-batch
    check is a best-effort tightening — a transient read blip must not break
    a poll or silently drop the precision filter. (Qualification, which has
    NO other gate, instead fails CLOSED — see
    ``qualification.materialize.ensure_job_tags``.)

    Also honors the provider fast-fail breaker (audit PERF-M): if the tagger
    already tripped it on a 402/429 this cooldown, triage stops too — the same
    provider will reject its Phase-1 calls.
    """
    if _provider_fatal_active():
        return True
    try:
        return await _global_budget_exhausted(_async_service_client())
    except Exception:
        logger.exception(
            "Phase 1 triage: global-budget read failed — continuing "
            "(fail-open; the per-cycle breaker still applies)"
        )
        return False


async def _global_circuit_breaker_tripped(supabase: AsyncClient) -> bool:
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
    spent = await total_llm_spend_all_async(supabase, since=midnight)
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


async def resume_phase1_backfills(supabase: AsyncClient) -> dict[str, int]:
    """Resume Phase-1 backfills for active targets that stopped early.

    The activation pass is allowed to stop short — its own fractional
    allowance, the shared daily cap, the global budget, a provider outage. The
    rows it did not reach keep ``scores.promising IS NULL``, and nothing else
    in the system will ever judge them: ordinary polling triages only
    externally-NEW listings (#514), which is the whole reason the backfill
    exists. So without this sweep an early stop was permanent unless a user
    happened to toggle the target off and on. Caught in review.

    Lives HERE rather than in ``relevance`` because it needs the poller's
    active-target read, per-payer client cache and global spend meter, and
    ``relevance`` cannot import the poller without a cycle — the same reason
    ``backfill_phase1_for_target`` takes its budget predicate injected.

    Idempotent and self-limiting: the backfill only touches ungraded in-window
    rows and re-reads its own allowance, so a target with nothing left is a
    cheap no-op and a target still behind picks up exactly one more day's
    share. Per-target failures are isolated — one bad target must not stop the
    others, exactly like the poll cycle's per-source isolation.
    """
    out = {"targets": 0, "resumed": 0, "written": 0, "skipped": 0, "errors": 0}
    if not settings.phase1_backfill_enabled:
        return out

    targets = await _active_targets(supabase)
    if not targets:
        return out
    out["targets"] = len(targets)

    # Spend only where the poller itself would spend. The sweep is pure
    # DISCRETIONARY catch-up, so any reason the gate gives to skip a target is
    # a reason not to buy grades for it — there is no admission decision here
    # for the transient/persistent split to matter to, only a spend one.
    #
    # Without this the sweep re-opened the exact hole this codebase was built
    # to close. ``_active_targets`` is ``app_active OR any active membership``,
    # so it deliberately includes APP-OWNED CATALOG targets; those resolve to
    # ``payer=None``; and ``_resolve_payer_client(None)`` hands back the
    # INSTANCE-KEY client rather than nothing. So a daily sweep would have
    # bought Phase-1 grades for targets nobody is pursuing, billed to the
    # instance, in flat contradiction of ``grade_catalog_targets=false`` — the
    # setting whose entire purpose is refusing that spend. It is also the same
    # shape as the passive burn this codebase already paid for once:
    # target-independent work bills the instance key and so is invisible to a
    # PER-PAYER gate unless something explicitly consults it.
    # ONE snapshot decides both "may we spend on this target" and "who pays".
    # ``build_budget_gate`` already resolves the payers it gates on, so a
    # second ``resolve_target_payers`` here would be a separate read of
    # ``user_targets`` that can disagree with it: a membership deactivated
    # between the two reads leaves the gate saying "sponsored, unblocked"
    # while the payer comes back ``None``, and ``_resolve_payer_client(None)``
    # then bills the INSTANCE KEY for a target that is no longer sponsored —
    # re-creating, through a race, exactly the unsponsored-catalog spend the
    # gate check above exists to prevent. Taking the payer from the gate makes
    # authorization and billing attribution atomic with each other, and drops
    # a query.
    gate = await build_budget_gate(supabase, [t.id for t in targets])
    cache: dict[str | None, LLMClient | None] = {}
    for target in targets:
        payer = gate.payer_for(target.id)
        block = gate.target_block_reason(target.id)
        if block is not None:
            out["skipped"] += 1
            logger.info("phase1 backfill resume: skipping target %s (%s)", target.id, block)
            continue
        try:
            llm = await _resolve_payer_client(cache, supabase, payer)
            if llm is None:
                # No key for this payer (BYOK-required, disabled, no client).
                # Not an error — the next sweep retries.
                continue
            result = await backfill_phase1_for_target(
                supabase,
                llm,
                target,
                payer_user_id=payer,
                budget_blocks=lambda: _global_budget_exhausted(supabase),
            )
            if result.verdicts_written:
                out["resumed"] += 1
                out["written"] += result.verdicts_written
        except Exception:
            out["errors"] += 1
            logger.exception("Phase-1 backfill resume failed for target %s", target.id)
    logger.info(
        "phase1 backfill resume sweep: %d target(s), %d resumed, %d verdict(s), "
        "%d skipped, %d error(s)",
        out["targets"],
        out["resumed"],
        out["written"],
        out["skipped"],
        out["errors"],
    )
    return out


async def _cycle_budget_gate(supabase: AsyncClient) -> tuple[PayerBudgetGate, bool]:
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
        active = await _active_targets(supabase)
        if await _global_circuit_breaker_tripped(_async_service_client()):
            return PayerBudgetGate(), bool(active)
        gate = await build_budget_gate(_async_service_client(), [t.id for t in active])
        return gate, bool(active)
    except Exception:
        logger.exception("Budget gate build failed — deferring all LLM work this cycle")
        return PayerBudgetGate(), True


# Last lifecycle sweep (time.monotonic). In-process is fine on the
# single-replica deploy: a restart just causes one harmless early re-run
# (the sweep is idempotent).
_LIFECYCLE_LAST_RUN: float = 0.0
LIFECYCLE_SWEEP_INTERVAL_S = 6 * 3600.0


async def _maybe_run_lifecycle_sweep(supabase: AsyncClient) -> None:
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


async def _maybe_run_archival_sweep(supabase: AsyncClient) -> None:
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


# Page size for the enabled-sources read. Deliberately UNDER PostgREST's
# max-rows clamp (hosted default 1,000): a full page unambiguously means
# "there may be more", a short page means "done" — if the page size equaled
# the server clamp, a server-truncated page would be indistinguishable from
# the final page and the loop would silently stop early (the exact bug this
# helper exists to fix).
_SOURCES_PAGE_SIZE = 500


async def _read_enabled_sources(supabase: AsyncClient) -> list[dict[str, Any]]:
    """Every enabled source row, paginated past PostgREST's max-rows clamp.

    The hosted PostgREST silently truncates ANY un-ranged select at
    ``db-max-rows`` (~1,000). Found live 2026-08-05: 3,676 enabled sources,
    the cycle's read returned exactly 1,000 — the 1,144 never-polled catalog
    rows (physically newest, outside the drifting heap-order window) never
    entered a cycle, so the backlog froze while the visible cohort re-polled
    on cadence. Pages are ordered by primary key: heap-order pagination can
    skip or duplicate rows across pages when concurrent updates relocate
    tuples mid-read.
    """
    out: list[dict[str, Any]] = []
    offset = 0
    while True:
        resp = await poll_db_read(
            supabase,
            lambda c, _o=offset: (
                c.table("sources")
                .select("*")
                .eq("enabled", True)
                .order("id")
                .range(_o, _o + _SOURCES_PAGE_SIZE - 1)
            ),
            label=f"poll sources read (offset {offset})",
        )
        page = cast(list[dict[str, Any]], resp.data or [])
        out.extend(page)
        if len(page) < _SOURCES_PAGE_SIZE:
            logger.debug(
                "enabled-sources read: %d rows over %d page(s)",
                len(out),
                offset // _SOURCES_PAGE_SIZE + 1,
            )
            return out
        offset += _SOURCES_PAGE_SIZE


async def _poll_one_source_budgeted(
    source: dict[str, Any],
    supabase: AsyncClient,
    budget_gate: PayerBudgetGate | None,
    *,
    active_targets: list[JobTarget] | None,
    admission_targets: list[JobTarget] | None = None,
    stage3_users: tuple[dict[str, JobTarget], dict[str, OptimizedDoc]] | None,
    admission_budget: AdmissionBudget | None = None,
    intake_budget: IntakeBudget | None = None,
) -> dict[str, Any]:
    """``_poll_one_source`` bounded by the per-source wall-time budget.

    One giant board (a workday tenant with hundreds of 429-throttled detail
    fetches) must not occupy a concurrency slot until the CYCLE watchdog
    kills everything. On expiry the source's coroutine is cancelled — safe
    by construction: the stale-archive pass sits after the full fetch, so a
    cancel can never archive against a partial board list, and completed
    ingest stages persist (idempotent upserts). The board's row is stamped
    ``last_polled_at`` so it rotates to the BACK of the most-overdue-first
    queue instead of re-hogging a slot next tick; the stamp deliberately
    touches nothing else (no ``job_count``/failure-counter reset — this was
    not a clean poll). Returns a not-polled summary with no error string so
    ingestion-health error alarms don't fire on a bounded, expected event.
    """
    budget = settings.poll_source_budget_seconds
    if not budget:
        return await _poll_one_source(
            source,
            supabase,
            budget_gate,
            active_targets=active_targets,
            admission_targets=admission_targets,
            stage3_users=stage3_users,
            admission_budget=admission_budget,
            intake_budget=intake_budget,
        )
    try:
        return await asyncio.wait_for(
            _poll_one_source(
                source,
                supabase,
                budget_gate,
                active_targets=active_targets,
                admission_targets=admission_targets,
                stage3_users=stage3_users,
                admission_budget=admission_budget,
                intake_budget=intake_budget,
            ),
            timeout=budget,
        )
    except TimeoutError:
        company_name = source.get("company_name", "?")
        logger.warning(
            "poll %s exceeded the %ds per-source budget and was cancelled — "
            "stamped last_polled_at so it rotates to the back of the due queue; "
            "nothing archived. A chronically over-budget board is a "
            "catalog-hygiene candidate.",
            company_name,
            budget,
        )
        try:
            source_id = source.get("id")
            if source_id:
                await poll_db_write(
                    supabase,
                    lambda c: (
                        c.table("sources")
                        .update({"last_polled_at": datetime.now(UTC).isoformat()})
                        .eq("id", source_id)
                    ),
                    label=f"poll budget stamp {company_name}",
                )
        except Exception:
            # Non-fatal: an unstamped board just stays at the queue front —
            # the next cycle re-attempts it (today's behavior).
            logger.exception("budget stamp failed for %s", company_name)
        return {
            "polled": False,
            "new": 0,
            "updated": 0,
            "archived": 0,
            "error": None,
            "budget_exhausted": True,
        }


def _accumulate_poll_summary(result: PollResult, summary: dict[str, Any]) -> None:
    """Fold one source's poll summary into the cycle result.

    Runs inside each worker as it finishes (single-threaded on the event
    loop, so no synchronization needed) rather than after the gather — a
    watchdog-cancelled cycle then still exposes the completed sources'
    counts through the caller-owned ``progress`` accumulator instead of
    losing them with the cancelled gather.
    """
    if summary["polled"]:
        result.sources_polled += 1
    result.new_jobs += summary["new"]
    result.updated_jobs += summary["updated"]
    # #642 visibility: unchanged-row skips ride the log, not PollResult
    # (API model stability). Grep 'poll cycle unchanged' for the cycle sum.
    if summary.get("unchanged"):
        logger.debug("poll unchanged-skip: %d rows kept their content_hash", summary["unchanged"])
    result.archived_jobs += summary["archived"]
    if summary["error"]:
        result.errors.append(summary["error"])


async def poll_all_sources(
    supabase: AsyncClient, *, progress: PollResult | None = None
) -> PollResult:
    """Force-poll every enabled source (ignores ``poll_interval_minutes``).

    ``progress``: optional caller-owned accumulator. Per-source counts fold
    into it as each source finishes, so a cycle the caller cancels (the
    scheduler's watchdog) still exposes partial progress. When provided, the
    return value is that same object.
    """
    admission_budget = new_admission_budget()
    intake_budget = await new_intake_budget(supabase)
    result = (
        progress
        if progress is not None
        else PollResult(sources_polled=0, new_jobs=0, updated_jobs=0, archived_jobs=0, errors=[])
    )
    all_sources = await _read_enabled_sources(supabase)

    # Cycle-wide constants resolved once instead of once per source:
    # active targets and the stage-3 (user → target/optimized-doc) maps.
    active_targets = await _active_targets(supabase)
    admission_targets = await _admission_targets(supabase, fallback=active_targets)
    stage3_users = await _resolve_user_targets_for_stage3(
        supabase, active_targets, "(cycle prefetch)"
    )

    budget_gate, has_active = await _cycle_budget_gate(supabase)
    sources = _drop_paid_sources_if_unconsumed(all_sources, has_active_targets=has_active)
    semaphore = asyncio.Semaphore(POLL_CONCURRENCY)

    async def _worker(raw_source: Any) -> None:
        async with semaphore:
            summary = await _poll_one_source_budgeted(
                cast(dict[str, Any], raw_source),
                supabase,
                budget_gate,
                active_targets=active_targets,
                admission_targets=admission_targets,
                stage3_users=stage3_users,
                admission_budget=admission_budget,
                intake_budget=intake_budget,
            )
        # No await between the source completing and the fold, so a
        # cancellation can never drop a finished source's counts.
        _accumulate_poll_summary(result, summary)

    await asyncio.gather(*(_worker(s) for s in sources))
    logger.info("poll cycle finished: %s %s", admission_budget.report(), intake_budget.report())
    _log_redetect_cycle_summary()
    # Product-level catalog health (#958). Telemetry only: record_cycle_health
    # swallows its own failures, and the try/except here is the belt to that
    # braces — a health bug must never cost a poll cycle.
    try:
        await catalog_health.record_cycle_health(supabase)
    except Exception:
        logger.exception("catalog_health hook failed (cycle unaffected)")
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


async def poll_due_sources(
    supabase: AsyncClient, *, progress: PollResult | None = None
) -> PollResult:
    """Poll only the sources whose interval has elapsed.

    Same shape as ``poll_all_sources`` but skips sources that were
    polled recently. Designed to be called from a frequent cron tick
    (e.g. every 30 min) without re-hammering boards that have a longer
    configured cadence.

    ``progress``: optional caller-owned accumulator — per-source counts fold
    in as each source finishes, so the scheduler's watchdog abort still sees
    partial progress. When provided, the return value is that same object.
    """
    result = (
        progress
        if progress is not None
        else PollResult(sources_polled=0, new_jobs=0, updated_jobs=0, archived_jobs=0, errors=[])
    )
    # Auto-recovery first, so a source whose cooldown just elapsed is
    # re-enabled and picked up in THIS cycle rather than waiting a tick.
    # A transient ATS-wide outage that tripped every source can't keep
    # ingestion down forever (the Sept-2026 failure mode).
    await recover_stale_sources(supabase)

    all_enabled = await _read_enabled_sources(supabase)

    # Idle-account housekeeping piggybacks the cron tick (throttled to
    # ~6h inside; never blocks or fails the poll). Runs on the pooled async
    # service client — the sweeps' DB routes through the ``db_write`` seam now
    # (#57 PR-G2e-6), so it awaits natively in prod.
    await _maybe_run_lifecycle_sweep(_async_service_client())

    # Archival lifecycle (UX/IA §5): 30d soft-archive + 60d purge, same
    # piggyback/throttle shape, flag-gated.
    await _maybe_run_archival_sweep(_async_service_client())

    admission_budget = new_admission_budget()
    intake_budget = await new_intake_budget(supabase)
    due = filter_due_sources(all_enabled)

    # Liveness-check a rotating slice of the untagged catalog (#285) EVERY
    # cycle — a job orphaned when its source stopped listing it is never
    # re-polled, so a dead listing lingers on every serving surface. Runs
    # independent of whether any source is due (a quiet cycle still rotates
    # it), and BEFORE the ``not due`` early-exit so it isn't skipped. Costs NO
    # LLM (its tagging half is gone — tags are lazy now); best-effort.
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
        return result

    # Cycle-wide constants resolved once instead of once per source:
    # active targets and the stage-3 (user → target/optimized-doc) maps.
    active_targets = await _active_targets(supabase)
    admission_targets = await _admission_targets(supabase, fallback=active_targets)
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

    # Negative-store effectiveness, cycle-wide. One INFO line per cycle (not
    # per source — the #702 log-storm lesson) makes the store's effect
    # observable in prod: hits should dwarf misses once the corpus warms, and
    # the count must PERSIST across a deploy — the in-process dict this
    # replaced silently reset to all-misses on every release.
    phase1_store_stats = {"hits": 0, "misses": 0}

    async def _worker(source: dict[str, Any]) -> None:
        async with semaphore:
            summary = await _poll_one_source_budgeted(
                source,
                supabase,
                budget_gate,
                active_targets=active_targets,
                admission_targets=admission_targets,
                stage3_users=stage3_users,
                admission_budget=admission_budget,
                intake_budget=intake_budget,
            )
        # No await between the source completing and the fold, so a
        # cancellation can never drop a finished source's counts.
        _accumulate_poll_summary(result, summary)
        phase1_store_stats["hits"] += summary.get("phase1_store_hits", 0)
        phase1_store_stats["misses"] += summary.get("phase1_store_misses", 0)

    await asyncio.gather(*(_worker(s) for s in due))
    logger.info("poll cycle finished: %s %s", admission_budget.report(), intake_budget.report())
    _log_redetect_cycle_summary()
    if phase1_store_stats["hits"] or phase1_store_stats["misses"]:
        logger.info(
            "phase1 rejection store: %d LLM verdict(s) avoided, %d sent to the model this cycle",
            phase1_store_stats["hits"],
            phase1_store_stats["misses"],
        )
    # Product-level catalog health (#958) — same belt-and-braces as the full
    # cycle path; the recorder's own min-interval throttle keeps the frequent
    # due-source ticks from writing duplicate rows.
    try:
        await catalog_health.record_cycle_health(supabase)
    except Exception:
        logger.exception("catalog_health hook failed (cycle unaffected)")
    return result


# ---- Target-specific polling ------------------------------------------------


async def _poll_one_source_for_target(
    source: dict[str, Any],
    supabase: AsyncClient,
    target: JobTarget,
    payer_user_id: str | None = None,
    payer_block_reason: BlockReason | None = None,
    intake_budget: IntakeBudget | None = None,
) -> dict[str, Any]:
    """Poll a single source for a specific target. Three-stage pipeline.

    ``payer_user_id`` is the user charged for this target's LLM work
    (the activator); ``payer_block_reason`` is WHY that payer's LLM work is
    skipped this cycle (``None`` = not skipped) and suppresses Phase 1 spend
    while still ingesting fail-open — both resolved once by
    ``poll_sources_for_target``. It carries the reason rather than a bare
    bool so the defer log can name it: this path is reached by an idle or
    operator-disabled payer too, not only one over allowance.

    (The ``budget_gate`` snapshot this used to take was only ever read by the
    ingest-time qualification tagger, which is gone: tagging happens at grade
    time now, where a live consumer is a precondition of being called at all.)
    """
    summary: dict[str, Any] = {
        "polled": False,
        "new": 0,
        "updated": 0,
        "error": None,
        "phase1_store_hits": 0,
        "phase1_store_misses": 0,
    }
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

        # What we already hold for this source, read BEFORE the fetch so
        # Workday can skip the per-posting detail request for postings whose
        # list entry is unchanged. Cheap columns only, and a failure here just
        # means every detail is fetched — today's behaviour.
        # This path polls for ONE target, so the free gates judge against it
        # alone. Applied to the provider's LIST entries so Workday can skip
        # the detail request for postings we would only drop.
        def _admissible(title: str, location: str | None) -> bool:
            """Mirrors the row-build loop below — keep them in step, or a
            posting gets skipped here that the loop would have kept."""
            return _title_matches_any_target(title, [target]) and _is_us_location(location)

        known_postings: dict[str, KnownPosting] = {}
        if provider == "workday":
            try:
                pre_resp = await poll_db_read(
                    supabase,
                    lambda c: (
                        c.table("jobs")
                        .select("external_id, title, source_posted_at")
                        .eq("source_id", source_id)
                        .is_("archived_at", "null")
                    ),
                    label=f"poll known postings {company_name}",
                    retry_sync=True,
                )
                known_postings = {
                    str(r["external_id"]): KnownPosting(
                        title=r.get("title"), posted_at_stored=r.get("source_posted_at")
                    )
                    for r in cast(list[dict[str, Any]], pre_resp.data or [])
                    if r.get("external_id")
                }
            except Exception:
                logger.warning(
                    "poll %s: known-postings read failed; fetching every detail",
                    company_name,
                    exc_info=True,
                )

        jobs = await _call_fetcher(fetcher, board_token, known_postings, _admissible)
        summary["polled"] = True

        # #514: this path had no known-row read at all, so already-ingested
        # external_ids were re-triaged on every cycle (the exact LLM waste
        # the shared path's candidate comment calls out) and a flipped
        # verdict — or the fail-closed gate below — starved their content
        # refresh. Same full-set admission scoping as ``_poll_one_source``.
        known_ids_resp = await poll_db_read(
            supabase,
            lambda c: (
                c.table("jobs").select("external_id, content_hash").eq("source_id", source_id)
            ),
            label=f"poll known ids {company_name}",
            retry_sync=True,
        )
        known_rows_read = cast(list[dict[str, Any]], known_ids_resp.data or [])
        known_external_ids = {r.get("external_id") for r in known_rows_read}
        # external_id → stored content hash (#642): drives the unchanged-row
        # skip before the upsert. NULL for pre-migration rows (always write
        # once, stamping the hash).
        known_hashes: dict[str | None, str | None] = {
            r.get("external_id"): r.get("content_hash") for r in known_rows_read
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
            await _resolve_payer_client(payer_clients, _async_service_client(), payer_user_id)
            if settings.phase1_triage_enabled and triage_candidates and payer_block_reason is None
            else None
        )
        if llm is not None:
            # Negative-verdict store (#514, persistent): titles this target's
            # LLM already rejected within the TTL skip the model — synthetic
            # promising=False verdict, marked attempted (rejected, not
            # deferred). Same semantics as the scheduled path.
            cached_rejections = await fetch_rejected_titles(
                supabase, target, [j.title for _, j in triage_candidates]
            )
            send_candidates: list[tuple[int, StandardJob]] = []
            for cand_idx, cand_job in triage_candidates:
                if normalize_title(cand_job.title) in cached_rejections:
                    global_idx = cand_idx + 1
                    target_verdicts[global_idx] = TitleVerdict(id=global_idx, promising=False)
                    phase1_attempted.add(global_idx)
                else:
                    send_candidates.append((cand_idx, cand_job))
            summary["phase1_store_hits"] += len(triage_candidates) - len(send_candidates)
            summary["phase1_store_misses"] += len(send_candidates)
            titles = [job.title for _, job in send_candidates]
            rejected_now: list[tuple[str, int | None]] = []
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
                # #930: the per-target daily COUNT cap, shared with the
                # activation backfill (see the scheduled path for why).
                if await phase1_cap_reached(supabase, target.id):
                    logger.warning(
                        "Phase 1 triage: per-target daily call cap (%d) reached "
                        "for target %s — deferring remaining titles",
                        settings.phase1_daily_cap,
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
                        await record_llm_cost_async(
                            _async_service_client(),
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
                # mapping; raw rejections feed the negative store (#514).
                for batch_idx, verdict in verdicts.items():
                    subset_pos = start + batch_idx - 1  # 0-based
                    if 0 <= subset_pos < len(send_candidates):
                        global_idx = send_candidates[subset_pos][0] + 1
                        target_verdicts[global_idx] = verdict
                        if not verdict.promising:
                            rejected_now.append(
                                (send_candidates[subset_pos][1].title, verdict.confidence)
                            )
            if rejected_now:
                await record_rejections(supabase, target, rejected_now)

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

            # The GLOBAL hourly intake ceiling, same rule as the main poll
            # path. This route exists for target ACTIVATION, which fans out
            # across every source at once — so it is the burstiest inserter we
            # have, and leaving it uncapped would have left the ceiling
            # trivially bypassable. Known rows stay exempt: they update a row
            # that already exists and add none of the insert pressure bounded
            # here.
            if (
                job.external_id not in known_external_ids
                and intake_budget is not None
                and not intake_budget.take()
            ):
                continue

            if job.detail_skipped:
                # Held unchanged, detail deliberately not fetched — ``content``
                # is empty, so building a row here would blank the stored
                # description. It stays in ``all_external_ids`` above, so the
                # stale-archive pass still counts it as seen.
                continue

            salary = job.salary_text or extract_salary_from_html(job.content)
            loc = parse_location(job.location_name)

            phase1_idx_by_external_id[job.external_id] = idx + 1
            rows_to_upsert.append(
                {
                    "external_id": job.external_id,
                    "source_id": source_id,
                    "title": job.title,
                    "title_display": clean_title_display(job.title),
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
                    **board_columns(job, loc),
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

        # #642: drop KNOWN rows whose refreshable payload is byte-identical
        # to what's stored — no jobs rewrite (TOAST included), and because
        # the scoring stages below iterate the UPSERT RESULT, no redundant
        # stage-1/2 rescore either. Content or salary changes alter the
        # hash and flow through unchanged-path-free. Profile-version bumps
        # rescore via bulk_score_for_target at bump time, not here.
        unchanged_skipped = 0
        if rows_to_upsert:
            rows_to_upsert, unchanged_skipped = _partition_unchanged(rows_to_upsert, known_hashes)
            if unchanged_skipped:
                summary["unchanged"] = unchanged_skipped

        if rows_to_upsert:
            # Routing through the seam also gives this upsert the transient-
            # blip retry the all-sources path already had (idempotent upsert,
            # so a re-issue after a dropped stream is safe).
            # Grouped by key-set (#928) — same reason as _poll_one_source: a
            # single bulk upsert writes the union of the batch's keys to every
            # row, blanking the board-silent postings' is_remote /
            # employment_type. See ``poll_db_upsert``.
            upserted_rows = await poll_db_upsert(
                supabase,
                table="jobs",
                rows=rows_to_upsert,
                on_conflict="source_id,external_id",
                label=f"poll upsert {company_name}",
            )
            # #514: a row is a REFRESH iff its external_id was known before
            # this cycle's upsert — NOT created==updated (a conflict-update
            # never bumps jobs.updated_at, see _poll_one_source). The
            # Stage-2 pass preserves the persisted Phase-1 verdict for
            # refreshed rows instead of re-litigating admission.
            known_upserted_ids: list[str] = []
            for data in upserted_rows:
                if data.get("external_id") in known_external_ids:
                    summary["updated"] += 1
                    known_upserted_ids.append(data["id"])
                else:
                    summary["new"] += 1

            # The board's own US verdict (see _poll_one_source) — free,
            # deterministic, and the half of the tag-time pruning that lazy
            # tagging can give straight back. This path has no ``poll_funnel``
            # line, so the counters get their own: an operator has to be able
            # to see the prune on BOTH ingest paths, not just the shared cycle.
            board_us_marked, board_us_archived = await _apply_board_us_verdicts(
                supabase, jobs, upserted_rows
            )
            location_us_marked = await _apply_location_us_verdicts(supabase, jobs, upserted_rows)
            if board_us_marked or board_us_archived or location_us_marked:
                logger.info(
                    "poll_funnel_target source=%s target=%s "
                    "board_us_marked=%d board_us_archived=%d location_us_marked=%d",
                    company_name,
                    target.id,
                    board_us_marked,
                    board_us_archived,
                    location_us_marked,
                )

            # Qualification tags are LAZY (see _poll_one_source): the Phase-2
            # runner's ensure_job_tags buys them for the trimmed grade set.
            # Job embeddings likewise: ensure_job_vectors materializes exactly
            # the read set.

            # Single-target twin of the shared path's reap guard (#869): the
            # first target-FK 23503 marks the target reaped; every later item
            # and stage drops instead of paying per-row for a discarded write.
            reaped_targets: set[str] = set()

            # Stage 1: Title scoring
            async def _title_score_one(row_data: dict[str, Any]) -> None:
                if target.id in reaped_targets:
                    return
                try:
                    await target_title_score_and_upsert(
                        supabase,
                        job_posting_id=row_data["id"],
                        title=row_data.get("title", ""),
                        target=target,
                    )
                except TargetReapedError:
                    if target.id not in reaped_targets:
                        reaped_targets.add(target.id)
                        logger.warning(
                            "Target %s was reaped mid-cycle; dropping its remaining "
                            "scoring for this cycle",
                            target.id,
                        )
                except Exception:
                    logger.exception("Stage 1 scoring failed for job %s", row_data.get("id"))

            await asyncio.gather(*(_title_score_one(r) for r in upserted_rows))

            # Stage 2: Full JD scoring (pre-parse JDs once)
            jd_cache: dict[str, Any] = {}
            for rd in upserted_rows:
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
                if target.id in reaped_targets:
                    return
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
                except TargetReapedError:
                    if target.id not in reaped_targets:
                        reaped_targets.add(target.id)
                        logger.warning(
                            "Target %s was reaped mid-cycle; dropping its remaining "
                            "scoring for this cycle",
                            target.id,
                        )
                except Exception:
                    logger.exception("Stage 2 scoring failed for job %s", row_data.get("id"))

            await asyncio.gather(*(_full_score_one(r) for r in upserted_rows))

            if target.id in reaped_targets:
                # #869: the target is gone — Stage 3 is the paid LLM stage,
                # and every grade for it would be discarded at the write.
                logger.info("Stage 3 skipped for target %s (target reaped mid-cycle)", target.id)
                primary_by_user: dict[str, JobTarget] = {}
                user_optimized: dict[str, OptimizedDoc] = {}
            else:
                # Stage 3: LLM scoring for qualified jobs (concurrent).
                # JobTarget is a global row with no user_id — resolve owning
                # users via the user_targets junction, then fetch each user's
                # optimized doc. The pre-fix ``get_latest(None)`` returned
                # nothing since no system-wide doc exists in the multi-user
                # schema.
                primary_by_user, user_optimized = await _resolve_user_targets_for_stage3(
                    supabase, [target], company_name
                )
            if primary_by_user and payer_block_reason is not None:
                logger.info(
                    "Stage 3 deferred for target %s (payer %s blocked: %s)",
                    target.id,
                    payer_user_id,
                    payer_block_reason,
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
                llm = await _resolve_payer_client(
                    payer_clients, _async_service_client(), payer_user_id
                )
                if llm is None:
                    logger.info(
                        "Phase 2 deferred for target %s (payer %s has no BYOK key)",
                        target.id,
                        payer_user_id,
                    )
                else:
                    cycle_rows = list(upserted_rows)
                    for uid in primary_by_user:
                        try:
                            await run_phase2_for_jobs(
                                _async_service_client(),
                                llm,
                                target=target,
                                payload=user_optimized[uid].payload,
                                jobs=cycle_rows,
                                user_id=payer_user_id,
                                tag_budget_exhausted=_tagger_budget_exhausted,
                            )
                        except Exception:
                            logger.exception(
                                "Phase 2 grading failed for user %s / target %s",
                                uid,
                                target.id,
                            )

    except BoardFetchError as exc:
        # Same quiet treatment as the shared path: an upstream board that
        # didn't answer with a listing is not a code bug. No failure counting
        # here — this path is the per-target fan-out and never touched
        # ``consecutive_failures`` in either direction, so the shared poll
        # cycle stays the single writer of the backoff.
        logger.warning("Poll failed for %s (target %s): %s", company_name, target.label, exc)
        summary["error"] = f"{company_name}: board fetch failed"
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


async def poll_sources_for_target(supabase: AsyncClient, target: JobTarget) -> PollResult:
    """Poll all enabled sources, filtering for jobs matching a target's search keywords.

    Skips non-pipeline-active targets entirely (returns an empty
    ``PollResult``). Pipeline-active = ``app_active`` (the instance
    floor) OR any active membership — the derived predicate from
    ``crud.is_pipeline_active`` (the trigger-cached flag is gone, schema
    audit P0). The /activate endpoint activates the caller's membership
    before invoking this, which satisfies the membership arm.
    """
    if not await _is_pipeline_active(supabase, target.id):
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

    # Share the hourly ceiling with the scheduled cycle: it is one budget over
    # one rolling hour across the whole instance, re-read from the DB here, so
    # an activation that runs alongside a poll tick sees what that tick has
    # already spent instead of getting its own fresh allowance.
    intake_budget = await new_intake_budget(supabase)

    sources: list[dict[str, Any]] = await _read_enabled_sources(supabase)

    # Optimized doc is fetched per-user inside
    # ``_poll_one_source_for_target`` now — the previous shared-doc fetch
    # (``user_id=None``) never returned a row in the multi-user schema.

    # Resolve the payer (activator) once for the whole fan-out; their
    # monthly allowance decides whether Phase 1 spends anything. On
    # failure: refuse to spend (defer LLM work), keep ingesting.
    try:
        gate = await build_budget_gate(_async_service_client(), [target.id])
    except Exception:
        logger.exception(
            "Budget gate build failed for target %s — deferring LLM work",
            target.id,
        )
        gate = PayerBudgetGate()
    payer = gate.payer_for(target.id)
    block_reason = gate.target_block_reason(target.id)
    over = block_reason is not None
    if over:
        logger.info(
            "poll_sources_for_target: Phase 1 deferred for target %s (payer %s blocked: %s)",
            target.id,
            payer,
            block_reason,
        )

    semaphore = asyncio.Semaphore(POLL_CONCURRENCY)

    # Cooperative cancellation (#638): the active check above runs ONCE at
    # entry, but this fan-out grinds the full catalog for HOURS on a big
    # source set — and deactivating the target used to change nothing (the
    # 2026-08-06 incident: a sweep-activated target was deactivated 26 min
    # in; the fan-out ran 3+ more hours, saturated Supabase into gateway
    # 504s, and 500'd the owner's /search). A watcher re-checks
    # pipeline-active every ``_ACTIVE_RECHECK_S``; on deactivation the
    # remaining workers drain as no-ops within one interval.
    abort = asyncio.Event()
    deactivated_mid_run = False

    async def _watch_active() -> None:
        nonlocal deactivated_mid_run
        while not abort.is_set():
            try:
                await asyncio.wait_for(abort.wait(), timeout=_ACTIVE_RECHECK_S)
                return
            except TimeoutError:
                pass
            try:
                still_active = await _is_pipeline_active(supabase, target.id)
            except Exception:
                # Transient read failure must not kill the fan-out — keep
                # polling on the next interval; the entry check already
                # proved the target active once.
                logger.debug("activation watcher re-check failed; retrying", exc_info=True)
                continue
            if not still_active:
                logger.warning(
                    "poll_sources_for_target: target %s (%s) deactivated "
                    "mid-fan-out — aborting remaining sources",
                    target.id,
                    target.label,
                )
                deactivated_mid_run = True
                abort.set()
                return

    watcher = asyncio.create_task(_watch_active())

    async def _worker(raw_source: Any) -> dict[str, Any]:
        if abort.is_set():
            return {"polled": False, "new": 0, "updated": 0, "error": None}
        async with semaphore:
            if abort.is_set():
                return {"polled": False, "new": 0, "updated": 0, "error": None}
            return await _poll_one_source_for_target(
                cast(dict[str, Any], raw_source),
                supabase,
                target,
                payer_user_id=payer,
                payer_block_reason=block_reason,
                intake_budget=intake_budget,
            )

    try:
        summaries = await asyncio.gather(*(_worker(s) for s in sources))
    finally:
        abort.set()
        watcher.cancel()

    result = PollResult(sources_polled=0, new_jobs=0, updated_jobs=0, archived_jobs=0, errors=[])
    for s in summaries:
        if s["polled"]:
            result.sources_polled += 1
        result.new_jobs += s["new"]
        result.updated_jobs += s["updated"]
        if s.get("error"):
            result.errors.append(s["error"])
    if deactivated_mid_run:
        result.errors.append("activation fan-out aborted: target deactivated mid-run")

    logger.info("activation poll finished for target %s: %s", target.id, intake_budget.report())
    return result
