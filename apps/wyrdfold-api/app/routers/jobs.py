import asyncio
import base64
import binascii
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from postgrest.exceptions import APIError
from postgrest.types import CountMethod
from supabase import Client

from app.cache import job_list_cache, jobs_cache_prefix, make_cache_key
from app.config import settings
from app.dependencies import (
    get_current_user_id,
    get_current_user_id_optional,
    get_supabase,
    get_supabase_for_caller,
    get_user_supabase,
    verify_api_key,
    verify_api_key_or_jwt,
)
from app.http_client import (
    ResponseTooLargeError,
    UnsafeURLError,
    get_with_size_cap,
)
from app.models.schemas import (
    AddToTargetRequest,
    AddToTargetResponse,
    ManualJobRequest,
    ManualJobResponse,
    UrlValidateRequest,
    UrlValidateResponse,
)
from app.models.targets import SENIORITY_ORDER, AxisWeights, TargetPreferences
from app.rate_limit import limiter
from app.services.extract import (
    ExtractionResult,
    _extract_from_firecrawl,
    extract_job_from_html,
    extract_salary_from_html,
    salary_columns,
)
from app.services.fit.axis_weights import display_score_or_passthrough
from app.services.job_ingest import materialize_and_score_job
from app.services.qualification.family_gate import passes_family_gate
from app.services.recency import display_recency_score
from app.services.tailor import persistence
from app.services.target_scoring import (
    bulk_score_for_target,
    score_and_upsert,
)
from app.services.targets.crud import get as get_target
from app.services.targets.crud import get_active as get_active_target
from app.services.targets.crud import (
    get_active_for_user,
    get_active_target_ids,
    get_user_target,
    list_user_targets,
    preferences_from_user_target,
)
from app.services.validate import (
    assert_safe_host,
    is_banned_domain,
    registrable_domain,
    validate_format,
    validate_job_url,
)

logger = logging.getLogger(__name__)


class _RpcIneligibleError(RuntimeError):
    """Raised by a list-helper when the RPC provably *can't express* the
    requested query shape (multi-word OR-search, post-fetch location filter,
    Pending-aware score bucketing, floor-exemption) — an **expected** signal to
    use the two-query path, not a failure.

    The dispatchers below catch this separately from a genuine RPC failure so
    silent degradation of the hottest endpoints is observable on prod (audit
    2026-07-18 PERF-M): an ineligible *shape* falls back quietly at DEBUG, while
    a real RPC failure (function drift, timeout, non-list response, permission
    change) falls back **loudly at WARNING with the traceback**. Before this the
    whole fallback logged at DEBUG — invisible under prod's INFO root
    (`logging_config.py`) — so an RPC that broke would keep "working" on the
    slower path with no signal until it became a latency/cost incident (cf.
    #365). Subclasses ``RuntimeError`` so any pre-existing broad
    ``except RuntimeError`` still catches it."""


# Operator location-filter path fetches pre-filter rows into Python (location
# can't be filtered server-side), so cap the scan to keep it bounded as `jobs`
# grows (#113). A hit is logged, never silently truncated.
_OPERATOR_LOCATION_SCAN_CAP = 10_000

router = APIRouter(
    prefix="/jobs",
    tags=["jobs"],
    dependencies=[Depends(verify_api_key_or_jwt)],
)

_JP_SELECT_COLS = (
    "id, external_id, source_id, title, company_name, location, "
    "city, state, country, location_remote, department, "
    "absolute_url, salary_text, "
    "salary_min, salary_max, salary_currency, salary_period, "
    # Firewall tag columns (#524 tagger): serving them is what makes the
    # per-target preference filters real — they were written-but-never-
    # selected for a year ("starved tags", schema audit Group B addendum).
    "employment_type, seniority, metro, is_remote, "
    "source_posted_at, cataloged_at"
)

# Detail-view projection — adds ``description_html`` (and anything else
# that's too heavy for list pages). Used by the per-posting GET so the
# UI can render the JD body in the detail panel; analysis/tailor flows
# already read ``description_html`` directly off ``jobs``.
_JP_DETAIL_SELECT_COLS = _JP_SELECT_COLS + ", description_html"


def _tokenize_search(raw: str | None) -> list[str]:
    """Split a search query into individual tokens. ``"customer director"``
    → ``["customer", "director"]``. Empty/None → ``[]``. Dedupes case-
    insensitively (keeps first-seen casing) so a redundant typo doesn't
    inflate the OR chain."""
    if not raw:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for tok in raw.split():
        t = tok.strip()
        if not t:
            continue
        lo = t.lower()
        if lo in seen:
            continue
        seen.add(lo)
        out.append(t)
    return out


# PostgREST `id=in.(uuid,uuid,...)` is URL-encoded into the query string.
# 200 UUIDs (36 chars each, plus commas and the `id.in.()` wrapper) lands
# around 7.5 KB — well under the proxy + nginx + supabase defaults of
# 8-16 KB. Above ~250 the URL silently truncates and the upstream
# returns plain ``Bad Request`` (not JSON), which then crashes the
# postgrest-py error decoder. ``has_location_filter`` and ``search``
# both force ``page_ids = list(score_lookup.keys())`` — a few thousand
# UUIDs after the May poll-cycle ingest. Chunked.
_IN_CHUNK_SIZE = 200


def _default_min_score_for_user(supabase: Client, user_id: str) -> int | None:
    """Return the effective default list floor when no ``min_score`` chip is set.

    Resolution:
    - a stored positive ``user_profiles.list_min_score`` → that value;
    - a stored **0** → ``None`` (the user explicitly opted out — no floor);
    - **NULL / no profile row** (user never set one) → the instance default
      ``settings.default_list_min_score`` (40 by default; #60 workstream D),
      so the list surfaces solid matches rather than the full keyword tail.

    Returns ``None`` when the effective floor is 0 (default disabled instance-
    wide, or user opted out). Not-yet-graded rows are exempt downstream, and
    the per-view ``min_score`` chip overrides this entirely.

    Decoupled from ``job_score_threshold`` (email notifications) and
    ``sms_score_threshold`` (SMS) because those notification UIs are
    disabled until SMTP / Twilio are configured, leaving users no way
    to tune the list view if it were piggybacked on those fields.
    """
    system_default = settings.default_list_min_score or None

    resp = (
        supabase.table("user_profiles")
        .select("list_min_score")
        .eq("user_id", user_id)
        .maybe_single()
        .execute()
    )
    if resp is None:
        return system_default
    row = cast(dict[str, Any] | None, resp.data)
    if row is None:
        return system_default
    value = row.get("list_min_score")
    if value is None:
        # Never configured → apply the instance default floor.
        return system_default
    if not isinstance(value, int) or value <= 0:
        # Explicit 0 (or a bad value) → the user opted out; no floor.
        return None
    return value


def _gate_live_us(query: Any) -> Any:
    """The display/match liveness gate — both conditions defense-in-depth:

    - ``archived_at IS NULL`` — globally-live (url-health / poller archive dead
      or high-confidence non-US jobs — #75 C3 / #246).
    - ``is_us IS NOT false`` — drop CONFIRMED non-US jobs while keeping US
      (``true``) and not-yet-tagged (``null``). This stops trusting the #246
      archive to be complete: a non-US job the tagger flagged but the archive
      missed still can't leak into a user's list/matches (#60). ``IS NOT false``
      (not ``!= false``) deliberately keeps NULLs.
    """
    return query.is_("archived_at", "null").not_.is_("is_us", "false")


def _fetch_jobs_chunked(
    supabase: Client,
    page_ids: list[str],
    *,
    user_id: str | None,
    status: str | None,
    company: str | None,
    search: str | None,
) -> list[dict[str, Any]]:
    """Fetch ``jobs`` rows for many IDs in chunks, resolving each row's
    status from the caller's ``user_jobs`` row (absent → ``'new'``) and
    applying the status/company/title filters per request. Caller is
    responsible for re-sorting by score after the merge (chunk order is
    not preserved).

    **Per-user status (#75 C2):** the displayed ``status`` is the
    caller's per-user status, not the global ``jobs.status``. For each
    chunk we fetch the ``jobs`` rows WITHOUT a status filter, then look
    up the caller's ``user_jobs`` statuses for the same ids and overlay
    them (``'new'`` for any job the user hasn't touched, and for every
    job when ``user_id is None``). The status filter is then applied on
    that per-user value, mirroring the old semantics.

    **Archived exclusion:** when ``status`` is not explicitly supplied
    (i.e. the user is browsing the default mixed view), rows whose
    per-user status is ``'archived'`` are filtered out. URL-health-check
    archived jobs would otherwise float to the top of the score-sorted
    list even though the user can no longer apply to them. Users who want
    to see archived rows can pass ``status='archived'`` explicitly.
    """
    if not page_ids:
        return []
    # The archived VIEW (status='archived') must show globally-archived rows —
    # the 30d sweep's output (UX/IA §5 Stage 1: "archived but still
    # reachable") — alongside per-user-archived ones, so it swaps the
    # liveness gate for a purge gate (tombstones stay hidden: their payload
    # is stripped) and carries ``archived_at`` through for the overlay below.
    archived_view = status == "archived"
    for_view_cols = _JP_SELECT_COLS + ", archived_at" if archived_view else _JP_SELECT_COLS
    out: list[dict[str, Any]] = []
    for i in range(0, len(page_ids), _IN_CHUNK_SIZE):
        chunk = page_ids[i : i + _IN_CHUNK_SIZE]
        # Liveness gate (#75 C3) + non-US gate (#60): exclude globally-archived
        # jobs AND confirmed non-US ones, regardless of per-user status.
        q = supabase.table("jobs").select(for_view_cols).in_("id", chunk)
        if archived_view:
            q = q.is_("purged_at", "null").not_.is_("is_us", "false")
        else:
            q = _gate_live_us(q)
        if company:
            q = q.eq("company_name", company)
        q = _apply_title_search(q, search)
        resp = q.execute()
        rows = cast(list[dict[str, Any]], resp.data or [])

        # Resolve per-user status: jobs the user hasn't touched (no
        # user_jobs row) — and every job when there's no user identity —
        # read as 'new' (#75 "absent = new" rule).
        status_map: dict[str, str] = {}
        if user_id is not None and rows:
            uj_resp = (
                supabase.table("user_jobs")
                .select("job_posting_id,status")
                .eq("user_id", user_id)
                .in_("job_posting_id", chunk)
                .execute()
            )
            status_map = {
                cast(str, r["job_posting_id"]): cast(str, r["status"])
                for r in cast(list[dict[str, Any]], uj_resp.data or [])
            }
        for row in rows:
            row["status"] = status_map.get(cast(str, row["id"]), "new")

        # Apply the status filter on the per-user value, mirroring the old
        # global-status semantics: explicit status keeps only matches;
        # default view drops archived. The archived view additionally
        # admits globally-archived rows (the 30d sweep's output) whatever
        # their per-user status, displaying them as 'archived'.
        if archived_view:
            kept: list[dict[str, Any]] = []
            for r in rows:
                globally_archived = r.get("archived_at") is not None
                if r["status"] == "archived" or globally_archived:
                    r["status"] = "archived"
                    r.pop("archived_at", None)
                    kept.append(r)
            rows = kept
        elif status:
            rows = [r for r in rows if r["status"] == status]
        else:
            rows = [r for r in rows if r["status"] != "archived"]
        out.extend(rows)
    return out


def _apply_title_search(query: Any, search: str | None) -> Any:
    """Apply a search filter to a query against the jobs.title column.

    - 0 tokens → no filter
    - 1 token → single ``ilike`` (unchanged behaviour, fastest path)
    - 2+ tokens → OR chain so ``"customer director"`` matches titles
      containing EITHER word ("Director of Customer Success" or
      "Customer Experience Lead"). Matches the user's mental model of a
      filter, not a phrase search.

    Each token is escaped for PostgREST's OR-list syntax: commas and
    parens would otherwise terminate the list / change grouping."""
    tokens = _tokenize_search(search)
    if not tokens:
        return query
    if len(tokens) == 1:
        return query.ilike("title", f"%{tokens[0]}%")
    # PostgREST or() takes a comma-separated list. Each token gets ``*``
    # wildcards (PostgREST's ilike uses ``*`` not ``%`` inside or_).
    parts = [f"title.ilike.*{_escape_or_token(t)}*" for t in tokens]
    return query.or_(",".join(parts))


def _escape_or_token(t: str) -> str:
    """PostgREST's or-list grammar uses commas and parens as separators.
    A token with either would be parsed as multiple filters or a group.
    Strip them — they have no semantic value in a search query."""
    return t.replace(",", "").replace("(", "").replace(")", "")


# ── Cursor (keyset / offset) pagination helpers ─────────────────────────────
# The jobs list pages with an OPAQUE cursor (load-more), not page numbers.
# The RPC path uses a keyset cursor ``{"v": <sort_value>, "id": <job_id>}``; the
# Python fallback/union/operator paths (which already materialise + sort the
# full candidate set) use an offset cursor ``{"o": <next_offset>}``. Both are
# base64url-encoded JSON so the frontend never inspects them, and a given
# (filters, sort) query routes deterministically to one path, so a cursor is
# always consumed by the path that produced it.


def _encode_cursor(payload: dict[str, Any] | None) -> str | None:
    if not payload:
        return None
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode()


def _decode_cursor(cursor: str | None) -> dict[str, Any]:
    """Opaque cursor → dict. Malformed/None → empty (first page)."""
    if not cursor:
        return {}
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode()))
    except (ValueError, binascii.Error):
        return {}
    return data if isinstance(data, dict) else {}


def _keyset_cursor_from_row(row: dict[str, Any], sort: str) -> dict[str, Any]:
    """Keyset cursor for the next page: the last row's sort value + id."""
    return {"v": row.get(sort), "id": row["id"]}


def _offset_from_cursor(cursor: dict[str, Any]) -> int:
    """Offset for the Python-paginated paths. Non-int/negative → 0."""
    raw = cursor.get("o", 0)
    return raw if isinstance(raw, int) and raw >= 0 else 0


def _offset_next_cursor(offset: int, page_size: int, total: int) -> str | None:
    """Encode the next offset cursor, or None when the page was the last."""
    nxt = offset + page_size
    return _encode_cursor({"o": nxt}) if nxt < total else None


def _list_jobs_for_target_rpc(
    supabase: Client,
    *,
    target_id: str,
    page_size: int,
    sort: str,
    ascending: bool,
    min_score: int | None,
    status: str | None,
    company: str | None,
    search: str | None,
    exclude_terms: list[str],
    only_terms: list[str],
    cursor: dict[str, Any],
    user_id: str | None = None,
) -> dict[str, Any]:
    # The RPC can't apply the post-fetch location filter, so its keyset would
    # walk pre-filter rows and pages would render half-empty. Force the
    # two-query fallback, which filters the full set then paginates.
    if exclude_terms or only_terms:
        raise _RpcIneligibleError(
            "RPC path skipped: location filter requires post-fetch pagination"
        )
    # Multi-word search ("customer director") should OR each token across
    # the title — the RPC's ``p_search`` is a single ilike, so bypass it
    # whenever the user typed more than one word.
    if search and len(_tokenize_search(search)) > 1:
        raise _RpcIneligibleError("RPC path skipped: multi-word search uses OR semantics")
    # Score sort needs Pending-below-graded bucketing (and, with decay on, the
    # ``recency_score`` column the RPC doesn't order by) — both handled in the
    # two-query path. The RPC's single-column keyset can't bucket. (#47/#118)
    if sort == "score":
        raise _RpcIneligibleError(
            "RPC path skipped: Pending-aware score sort handled in two-query path"
        )
    # A min_score floor must exempt not-yet-graded rows; the RPC's flat
    # ``score >= p_min_score`` can't. Route floored queries to the two-query
    # path, which exempts Pending. Unfloored queries keep the keyset fast path
    # (Pending rows pass anyway). (#47)
    if min_score and min_score > 0:
        raise _RpcIneligibleError(
            "RPC path skipped: Pending floor-exemption handled in two-query path"
        )
    """List jobs via server-side keyset RPC (single round-trip)."""
    after_value = cursor.get("v")
    resp = supabase.rpc(
        "get_target_jobs",
        {
            "p_target_id": target_id,
            "p_min_score": min_score or 0,
            "p_status": status,
            "p_company": company,
            "p_search": search,
            "p_sort": sort,
            "p_ascending": ascending,
            # Fetch one extra row to detect "has more" without a COUNT.
            "p_limit": page_size + 1,
            "p_after_value": None if after_value is None else str(after_value),
            "p_after_id": cursor.get("id"),
            "p_user_id": user_id,
        },
    ).execute()
    if not isinstance(resp.data, list):
        raise TypeError("RPC get_target_jobs returned non-list response")
    rows = cast(list[dict[str, Any]], resp.data)
    has_more = len(rows) > page_size
    postings = rows[:page_size]
    # Mark not-yet-graded rows so the UI badges them Pending, same as the
    # two-query paths (#47). The RPC returns ``scoring_status`` per row.
    for p in postings:
        p["pending"] = _is_pending(p)
    next_cursor = (
        _encode_cursor(_keyset_cursor_from_row(postings[-1], sort))
        if has_more and postings
        else None
    )
    # total is not computed on the keyset path (no COUNT) — None is best-effort.
    return {"postings": postings, "next_cursor": next_cursor, "total": None}


def _posted_at_lookup(
    supabase: Client, job_ids: list[str], *, force: bool = False
) -> dict[str, Any]:
    """``job_posting_id`` → posted date (``source_posted_at`` falling back to
    ``cataloged_at``), for recency-aware ordering.

    Two callers need it: the recency-decay display value (when decay is on), and
    the Pending-tier recency sort (``force=True``, used on the score sort so fresh
    ungraded rows surface even with decay off — #47 f/u). Skipped otherwise; the
    round-trip isn't free. Chunked to respect the IN-list cap, like
    ``_fetch_jobs_chunked``.
    """
    lookup: dict[str, Any] = {}
    if (not settings.recency_decay_enabled and not force) or not job_ids:
        return lookup
    for i in range(0, len(job_ids), _IN_CHUNK_SIZE):
        chunk = job_ids[i : i + _IN_CHUNK_SIZE]
        resp = (
            supabase.table("jobs")
            .select("id, source_posted_at, cataloged_at")
            .in_("id", chunk)
            .execute()
        )
        for r in cast(list[dict[str, Any]], resp.data or []):
            lookup[r["id"]] = r.get("source_posted_at") or r.get("cataloged_at")
    return lookup


def _display_sort_value(
    ts: dict[str, Any],
    *,
    weights: AxisWeights | None,
    posted_at: Any,
    now: datetime,
) -> int:
    """The score a row will actually DISPLAY: the axis-weighted blend, then
    read-time recency decay. Sorting the list on this — rather than the stored,
    un-weighted ``recency_score`` (which also freezes for jobs the poller stops
    re-touching) — keeps the dashboard order matching the visible numbers (#47).
    ``_apply_display_recency`` later sets each shown ``score`` to exactly this.
    """
    weighted = display_score_or_passthrough(
        ts.get("axis_scores"), int(ts.get("score") or 0), weights
    )
    if not settings.recency_decay_enabled:
        return weighted
    return display_recency_score(weighted, posted_at, now)


# ---------------------------------------------------------------------------
# Pending vs graded (#47 finding #4)
#
# ``scores.score`` holds TWO things on different scales: a cheap keyword
# placeholder while a row is ``stage1``/``stage2``, and the real Sonnet
# fit score once it's ``complete``. Treating them as one number let the
# ``min_score`` floor admit/exclude a job based only on whether the daily
# grading cap happened to reach it. We split on ``scoring_status``: only
# graded rows carry a fit score, so only they are judged by the floor;
# not-yet-graded ("Pending") rows are always shown, exempt from the floor,
# and sorted below the graded ones.
# ---------------------------------------------------------------------------


def _is_pending(row: dict[str, Any]) -> bool:
    """True when a row is not yet LLM-graded — its ``score`` is still a
    keyword placeholder, not a real fit score.

    The genuine-grade signals are the fields ONLY Phase 2's persist writes,
    atomically in one UPDATE: ``axis_scores`` and ``fit_reasoning``
    (prod-verified equivalent — zero rows carry one without the other). A row
    is graded when EITHER is present, so callers fetching either column
    classify correctly. ``scoring_status`` is NOT reliable here: 'complete'
    is set on rows that were never actually graded (deferred / reset), which
    once surfaced ~2,300 keyword placeholders as if they were fit scores.

    THE SELECT-SHAPE RULE (the 2026-07-16 regression): this classifier may
    only rely on columns the list paths actually fetch. The original
    fit_reasoning-only version silently classified EVERY list row as Pending
    — the list selects don't fetch ``fit_reasoning`` (it's per-row prose;
    fetching it for ~2k candidate rows per request is pure payload) — which
    collapsed the graded/Pending tiers into one recency-ordered lump.
    ``tests/test_jobs_pending_floor.py`` pins signal-columns ⊆ both list
    selects; keep it true when touching either side."""
    flagged = row.get("pending")
    if isinstance(flagged, bool):
        # Posting rows: the overlay precomputes this flag FROM the scores row
        # (which carries the real signal columns) before any ranking runs.
        # The post-fetch-filter branch ranks POSTING rows — they carry neither
        # axis_scores nor fit_reasoning, so without this rung every filtered
        # list collapsed back into the Pending tier (recency-ordered "out of
        # order" scores the moment a location/preference filter was active —
        # the 2026-07-16 follow-up regression).
        return flagged
    axes = row.get("axis_scores")
    if isinstance(axes, dict) and axes:
        return False
    reasoning = row.get("fit_reasoning")
    return not (isinstance(reasoning, str) and reasoning.strip())


# Candidate score-row columns for the two list paths. ``axis_scores`` must
# stay in this list: it is ``_is_pending``'s graded-signal column (the
# 2026-07-16 regression was a classifier keyed on a column these selects
# don't fetch — every row classified Pending and the tiers collapsed).
# ``tests/test_jobs_pending_floor.py`` pins this invariant.
_SCORE_ROW_COLS = (
    "job_posting_id, score, recency_score, score_breakdown, "
    "scoring_status, axis_scores, logistics_filters"
)


# The liveness inner-join doubles as a column ride-along: role_family feeds
# the off-family gate and the posted date the Pending-tier recency sort —
# columns these paths used to re-fetch in up to ~20 sequential chunked reads
# per request (the 4-8s /jobs + dashboard latencies, 2026-07-16). One join,
# zero extra round-trips.
_JOBS_EMBED = ", jobs!inner(id, role_family, source_posted_at, cataloged_at)"


def _embedded_jobs_field(row: dict[str, Any], field: str) -> Any:
    """The embedded ``jobs`` relation's ``field`` for a scores row, or None
    when the row was fetched without the embed (archived view / older
    stubs)."""
    embedded = row.get("jobs")
    if isinstance(embedded, dict):
        return embedded.get(field)
    return None


def _scores_live_join(query: Any, *, archived_view: bool) -> Any:
    """Restrict candidate score rows to LIVE jobs at the scores layer.

    Dead rows (archived/purged/confirmed-non-US jobs) used to survive into
    ranking + pagination and only drop at the jobs re-fetch — silently eating
    page slots (chronically short pages; an ascending score sort whose whole
    first window was dead rows rendered a fully EMPTY page, 2026-07-16). The
    ``jobs!inner`` embed mirrors ``_gate_live_us`` + the purge filter at the
    query layer so pagination only slices rows that can actually render.
    Callers must include ``jobs!inner(id)`` in the select when not the
    archived view (which keeps archived jobs and skips this).
    """
    if archived_view:
        return query
    return (
        query.is_("jobs.archived_at", "null")
        .is_("jobs.purged_at", "null")
        .not_.is_("jobs.is_us", "false")
    )


def _apply_score_floor(query: Any, min_score: int | None) -> Any:
    """Apply the fit-score floor, exempting Pending rows (#47).

    The floor is a fit-quality bar, so it only applies to rows that actually
    have a fit score (``scoring_status = 'complete'``). Pending rows hold only a
    keyword placeholder, so flooring them would hide promising jobs purely
    because the grading cap hasn't reached them yet — instead they always pass
    and the list marks them Pending. ``min_score`` is a validated int, so the
    interpolation below carries no injection surface."""
    if not min_score or min_score <= 0:
        return query
    return query.or_(f"scoring_status.is.null,scoring_status.neq.complete,score.gte.{min_score}")


def _rank_graded_first(
    rows: list[dict[str, Any]],
    *,
    value: Callable[[dict[str, Any]], Any],
    ascending: bool,
    pending_value: Callable[[dict[str, Any]], Any] | None = None,
) -> list[dict[str, Any]]:
    """Order ``rows`` for the score sort so graded rows always precede Pending
    ones. Graded rows sort by ``value`` (the display fit score); Pending rows
    sort by ``pending_value`` when supplied — recency (posted date), so fresh
    ungraded postings surface at the top of the Pending tier instead of being
    ranked by the hidden keyword placeholder ``value`` returns for them. Falls
    back to ``value`` for Pending when ``pending_value`` is None (#47 f/u).

    Graded-first holds regardless of sort direction: a Pending row carries only
    a keyword placeholder, so interleaving by raw value would let an ungraded 80
    outrank a graded 75. Bucketing keeps the real grades on top and the
    not-yet-judged queue beneath them."""
    graded = sorted((r for r in rows if not _is_pending(r)), key=value, reverse=not ascending)
    pkey = pending_value if pending_value is not None else value
    pending = sorted((r for r in rows if _is_pending(r)), key=pkey, reverse=not ascending)
    return graded + pending


def _prefer_score_row(candidate: dict[str, Any], current: dict[str, Any]) -> bool:
    """Whether ``candidate`` is a better per-job representative than ``current``
    in the untargeted (cross-target) view. A graded row beats a Pending one — a
    real fit score over a keyword placeholder (#47); among rows of the same
    gradedness, the higher raw score wins."""
    cand_graded = not _is_pending(candidate)
    cur_graded = not _is_pending(current)
    if cand_graded != cur_graded:
        return cand_graded
    return int(candidate.get("score") or 0) > int(current.get("score") or 0)


@dataclass(frozen=True)
class _LogisticsFilter:
    """The three /jobs logistics filter params (#86), bundled so they thread as
    one argument through the list paths rather than three."""

    remote_only: bool = False
    min_salary: int | None = None
    country: str | None = None

    @property
    def active(self) -> bool:
        return self.remote_only or self.min_salary is not None or bool(self.country)


def _gate_off_family(
    supabase: Client, by_id: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Drop matches whose job ``role_family`` mismatches its target's family.

    The write side of the ``get_target_jobs`` family gate (#277), for the
    two-query list paths (the untargeted dashboard + the per-target two-query
    fallback) — ``get_target_jobs`` gates in SQL, but these paths assemble in
    Python and the check is RELATIONAL (job's family vs the family of the target
    its score belongs to), so it can't be a column filter like ``_gate_live_us``.

    Semantics live in ``services.qualification.family_gate`` (strict,
    keep-null — the #277 rule, shared with the target-membership badge).
    ``by_id`` maps job_posting_id -> the winning ``scores`` row (carrying
    ``target_id``).

    Cheap: one ``targets`` read + chunked ``jobs.role_family`` reads, then an
    in-memory filter — no per-row round-trips. #60 / #278.
    """
    if not by_id:
        return by_id
    target_ids = {r["target_id"] for r in by_id.values() if r.get("target_id")}
    if not target_ids:
        return by_id
    tf = supabase.table("targets").select("id, role_family").in_("id", list(target_ids)).execute()
    target_family: dict[str, str | None] = {
        r["id"]: r.get("role_family") for r in cast(list[dict[str, Any]], tf.data or [])
    }
    # Nothing to enforce when none of these targets is classified.
    if not any(target_family.values()):
        return by_id

    # The scores fetch embeds jobs(role_family) on the live paths, so this is
    # usually a pure in-memory harvest; only rows fetched WITHOUT the embed
    # (archived view) fall back to the chunked reads — which used to run for
    # every request (~20 sequential round-trips on a 2k-row candidate set,
    # the dominant term in the 4-8s /jobs latencies).
    job_family: dict[str, str | None] = {}
    missing: list[str] = []
    for pid, s in by_id.items():
        embedded = s.get("jobs")
        if isinstance(embedded, dict) and "role_family" in embedded:
            job_family[pid] = embedded.get("role_family")
        else:
            missing.append(pid)
    for i in range(0, len(missing), _IN_CHUNK_SIZE):
        chunk = missing[i : i + _IN_CHUNK_SIZE]
        jf = supabase.table("jobs").select("id, role_family").in_("id", chunk).execute()
        for r in cast(list[dict[str, Any]], jf.data or []):
            job_family[r["id"]] = r.get("role_family")

    gated: dict[str, dict[str, Any]] = {}
    for pid, s in by_id.items():
        tid = s.get("target_id")
        tfam = target_family.get(tid) if tid is not None else None
        jfam = job_family.get(pid)
        if passes_family_gate(tfam, jfam):
            gated[pid] = s
    return gated


def _assemble_jobs_page(
    supabase: Client,
    *,
    by_id: dict[str, dict[str, Any]],
    weights_for_row: Callable[[dict[str, Any]], AxisWeights | None],
    offset: int,
    page_size: int,
    sort: str,
    sort_col: str,
    ascending: bool,
    status: str | None,
    company: str | None,
    search: str | None,
    exclude_terms: list[str],
    only_terms: list[str],
    preferences: TargetPreferences | None,
    user_id: str | None,
    logistics: _LogisticsFilter | None = None,
) -> dict[str, Any]:
    """Shared tail of both two-query list paths (per-target + cross-target).

    Given ``by_id`` (the winning ``scores`` row per job) and ``weights_for_row``
    (the per-row axis weights — a constant for the single-target path, a
    per-target lookup for the untargeted one), this ranks by the score each row
    will DISPLAY (axis-weighted blend + read-time decay, Pending below graded,
    #47), fetches + overlays the postings, applies the post-fetch location /
    preference filters, and paginates. The two callers differ ONLY in how they
    build ``by_id`` + ``weights_for_row``; everything from here down was
    duplicated verbatim between them until this extraction.
    """
    # Off-role-family gate (#60/#278): drop matches whose job family mismatches
    # its target's family BEFORE ranking/paginating — a post-fetch drop would
    # render short pages. Mirrors the get_target_jobs SQL gate for these paths.
    by_id = _gate_off_family(supabase, by_id)

    has_location_filter = bool(exclude_terms or only_terms)
    # Per-user preference filters (employment-type / seniority / location) are
    # post-fetch, so — like the location chip — when active we must materialise
    # + filter the full candidate set before paginating, or pages carry the
    # pre-filter total and render short. The score cutoff is NOT here — it's
    # already folded into min_score at the query layer.
    has_pref_filter = _preferences_have_post_fetch_filter(preferences)
    has_logistics_filter = logistics is not None and logistics.active
    has_post_fetch_filter = has_location_filter or has_pref_filter or has_logistics_filter

    now = datetime.now(UTC)
    # Force the posted-date fetch on the score sort even with decay off — the
    # Pending-tier recency sort keys on it (#47 f/u). The live-path scores
    # fetch embeds jobs(source_posted_at, cataloged_at), so this is normally a
    # pure harvest; only embed-less rows (archived view) still pay the reads.
    fs_lookup: dict[str, Any] = {}
    fs_missing: list[str] = []
    for pid, s in by_id.items():
        embedded = s.get("jobs")
        if isinstance(embedded, dict) and "cataloged_at" in embedded:
            fs_lookup[pid] = embedded.get("source_posted_at") or embedded.get("cataloged_at")
        else:
            fs_missing.append(pid)
    if fs_missing:
        fs_lookup.update(
            _posted_at_lookup(supabase, fs_missing, force=(sort == "score" or sort_col == "score"))
        )

    def _display(row: dict[str, Any]) -> int:
        return _display_sort_value(
            row,
            weights=weights_for_row(row),
            posted_at=fs_lookup.get(row["job_posting_id"]),
            now=now,
        )

    # Paginate at the scores layer only when no post-fetch filter can drop rows
    # AFTER pagination (location/pref are post-fetch), and only for the score
    # sort (other sorts key on a posting column fetched below). Sort by the
    # DISPLAY value so the page order matches the numbers the user sees (#47).
    if (
        sort_col == "score"
        and not status
        and not company
        and not search
        and not has_post_fetch_filter
    ):
        ranked = _rank_graded_first(
            list(by_id.values()),
            value=lambda r: (_display(r), r["job_posting_id"]),
            ascending=ascending,
            # Pending rows have no real fit score; order them by recency
            # (posted date) so fresh ungraded jobs surface, not by the hidden
            # keyword placeholder _display returns for them (#47 f/u).
            pending_value=lambda r: (
                fs_lookup.get(r["job_posting_id"]) or "",
                r["job_posting_id"],
            ),
        )
        page_ids = [r["job_posting_id"] for r in ranked[offset : offset + page_size]]
        total: int | None = len(ranked)
    else:
        page_ids = list(by_id.keys())
        total = None  # recomputed after posting-level filters

    if not page_ids:
        return {"postings": [], "next_cursor": None, "total": total or 0}

    postings: list[dict[str, Any]] = _fetch_jobs_chunked(
        supabase,
        page_ids,
        user_id=user_id,
        status=status,
        company=company,
        search=search,
    )

    # Overlay the displayed score (axis-weighted when weights are set, else the
    # raw Sonnet score), keeping ``raw_score`` alongside and flagging Pending.
    for p in postings:
        ts = by_id.get(p["id"])
        if ts:
            raw_score = int(ts["score"])
            p["score"] = display_score_or_passthrough(
                ts.get("axis_scores"), raw_score, weights_for_row(ts)
            )
            p["raw_score"] = raw_score
            p["score_breakdown"] = ts.get("score_breakdown")
            p["scoring_status"] = ts.get("scoring_status", "stage1")
            p["pending"] = _is_pending(ts)
            # Filter-only logistics (remote/salary/location) — never affects
            # score or sort; powers the /jobs chips + filter params (#86).
            p["logistics_filters"] = ts.get("logistics_filters")

    if total is None or sort_col != "score":
        if has_location_filter:
            postings = _apply_location_filter(
                postings, exclude_terms=exclude_terms, only_terms=only_terms
            )
        if has_pref_filter:
            postings = _apply_preferences_filter(postings, preferences)
        if logistics is not None and logistics.active:
            postings = _apply_logistics_filter(postings, logistics)

        def _sort_key(p: dict[str, Any]) -> Any:
            if sort == "score":
                # Same DISPLAY value the scores-layer sort uses (#47).
                ts = by_id.get(p["id"])
                return _display(ts) if ts else 0
            # 'created_at' is the stable WIRE token; the row key is the
            # renamed cataloged_at (R2 two-timestamp model).
            row_key = "cataloged_at" if sort == "created_at" else sort
            val = p.get(row_key)
            return "" if val is None else val

        if sort == "score":
            # Pending below graded (#47); graded by display value, Pending by
            # recency (posted date) so fresh ungraded jobs surface (#47 f/u).
            postings = _rank_graded_first(
                postings,
                value=_sort_key,
                ascending=ascending,
                pending_value=lambda p: (fs_lookup.get(p["id"]) or "", p["id"]),
            )
        else:
            postings.sort(key=_sort_key, reverse=not ascending)
        if total is None:
            total = len(postings)
            postings = postings[offset : offset + page_size]
    else:
        # Restore page_ids order — Supabase's in_() filter doesn't preserve list
        # order, so the postings come back in storage order despite page_ids
        # already being score-sorted.
        order_index = {jid: i for i, jid in enumerate(page_ids)}
        postings.sort(key=lambda p: order_index.get(p["id"], len(page_ids)))

    next_cursor = _offset_next_cursor(offset, page_size, total or 0)
    return {"postings": postings, "next_cursor": next_cursor, "total": total}


def _list_jobs_for_target_two_query(
    supabase: Client,
    *,
    target_id: str,
    page_size: int,
    sort: str,
    ascending: bool,
    min_score: int | None,
    status: str | None,
    company: str | None,
    search: str | None,
    exclude_terms: list[str],
    only_terms: list[str],
    cursor: dict[str, Any],
    axis_weights: AxisWeights | None = None,
    preferences: TargetPreferences | None = None,
    user_id: str | None = None,
    logistics: _LogisticsFilter | None = None,
) -> dict[str, Any]:
    """Fallback: two-query pattern with pagination pushed to the scores layer.

    ``axis_weights`` is the per-(user, target) read-time multiplier on
    Phase 2's axis scores. When non-None, the response's per-row ``score``
    field is replaced with the weighted display score computed from
    ``axis_scores``. Ranking, Pending-below-graded bucketing, and pagination
    all happen in ``_assemble_jobs_page`` keyed on that display value — this
    function only builds the candidate ``scores`` rows for the target.
    """
    offset = _offset_from_cursor(cursor)
    sort_col = "score" if sort == "score" else sort
    # Fetch every candidate score row for the target (score floor applied at the
    # DB). No server-side ORDER BY or exact count: _assemble_jobs_page re-ranks
    # by the DISPLAY value and derives the total from the row set, so both would
    # be pure waste (an exact count on every list request that nothing reads).
    archived_view = status == "archived"
    ts_query = (
        supabase.table("scores")
        .select(_SCORE_ROW_COLS + ("" if archived_view else _JOBS_EMBED))
        .eq("target_id", target_id)
        .eq("excluded", False)
    )
    ts_query = _scores_live_join(ts_query, archived_view=archived_view)
    ts_query = _apply_score_floor(ts_query, min_score)
    ts_rows = cast(list[dict[str, Any]], ts_query.execute().data or [])

    if not ts_rows:
        return {"postings": [], "next_cursor": None, "total": 0}

    score_lookup = {r["job_posting_id"]: r for r in ts_rows}

    # Single-target: the same axis weights apply to every row.
    return _assemble_jobs_page(
        supabase,
        by_id=score_lookup,
        weights_for_row=lambda _row: axis_weights,
        offset=offset,
        page_size=page_size,
        sort=sort,
        sort_col=sort_col,
        ascending=ascending,
        status=status,
        company=company,
        search=search,
        exclude_terms=exclude_terms,
        only_terms=only_terms,
        preferences=preferences,
        user_id=user_id,
        logistics=logistics,
    )


def _list_jobs_for_target(
    supabase: Client,
    *,
    target_id: str,
    page_size: int,
    sort: str,
    ascending: bool,
    min_score: int | None,
    status: str | None,
    company: str | None,
    search: str | None,
    exclude_terms: list[str],
    only_terms: list[str],
    cursor: dict[str, Any],
    axis_weights: AxisWeights | None = None,
    preferences: TargetPreferences | None = None,
    user_id: str | None = None,
    logistics: _LogisticsFilter | None = None,
) -> dict[str, Any]:
    """List jobs for a target view, sorted/paginated by target-specific scores.

    Tries the server-side RPC join first (single round-trip). Falls back to the
    optimized two-query pattern if the RPC function hasn't been deployed yet.
    The two-query path also takes over when location filters are active, since
    the RPC paginates server-side with no knowledge of the location filter.

    When ``axis_weights`` is set, the SCORE sort routes through the cross-target
    RPC (restricted to this target), which now computes the weighted blend
    DB-side (#457) — so a custom-weight target no longer scans everything into
    Python to rank. NON-score sorts with weights still take the two-query path:
    their keyset RPC (``get_target_jobs``) returns the raw score, so only the
    Python overlay shows the weighted number.

    ``preferences`` is the caller's per-(user, target) read-time filter (#60).
    The score cutoff is folded into ``min_score`` by the caller (server-side);
    the remaining post-fetch filters (employment-type / seniority / location)
    force the two-query path — the RPC paginates server-side with no knowledge
    of them, so its keyset would walk pre-filter rows.
    """
    kwargs: dict[str, Any] = {
        "target_id": target_id,
        "page_size": page_size,
        "sort": sort,
        "ascending": ascending,
        "min_score": min_score,
        "status": status,
        "company": company,
        "search": search,
        "exclude_terms": exclude_terms,
        "only_terms": only_terms,
        "cursor": cursor,
        "user_id": user_id,
    }
    # Logistics filters (#86) are post-fetch like location/preferences, so an
    # active one also forces the two-query path — the RPC keyset can't see it.
    # The archived view does too: ``get_target_jobs`` gates to live rows in
    # SQL, so only the two-query path can surface globally-archived rows
    # (UX/IA §5 Stage 1 "still reachable").
    has_logistics = logistics is not None and logistics.active
    # A weighted SCORE sort no longer forces the scan — it routes through the
    # cross-target RPC below, which blends DB-side (#457). Weighted NON-score
    # sorts still need the two-query overlay (their keyset RPC returns raw score).
    if (
        (axis_weights is not None and sort != "score")
        or _preferences_have_post_fetch_filter(preferences)
        or has_logistics
        or status == "archived"
    ):
        return _list_jobs_for_target_two_query(
            supabase,
            axis_weights=axis_weights,
            preferences=preferences,
            logistics=logistics,
            **kwargs,
        )
    # Score sort is Pending-below-graded + decay-aware, which the per-target
    # keyset RPC (get_target_jobs) can't express — so it used to fall to the
    # scan-everything two-query path (~8.6s cold on a heavy target, #2). Route it
    # instead through the cross-target RPC restricted to this one target: the
    # SAME gated + graded-first + decay + Pending-floor ranking, now index-only
    # via the denormalized scores columns (20260717040000). This also applies the
    # off-family gate to per-target score sort — aligning it with the per-target
    # non-score sorts and the cross-target dashboard (previously the score sort
    # alone showed off-family jobs). Location / multi-word search still need the
    # two-query path (the RPC's single ILIKE + server-side page can't express
    # them); those fall through to the keyset RPC's own score-sort bail below.
    has_location_filter = bool(exclude_terms or only_terms)
    is_multiword_search = bool(search and len(_tokenize_search(search)) > 1)
    if sort == "score" and not has_location_filter and not is_multiword_search:
        try:
            return _list_jobs_across_user_targets_rpc(
                supabase,
                user_target_ids={target_id},
                page_size=page_size,
                sort=sort,
                ascending=ascending,
                min_score=min_score,
                status=status,
                company=company,
                search=search,
                cursor=cursor,
                # #457: the RPC blends this target's custom weights DB-side.
                weights_by_target=({target_id: axis_weights} if axis_weights is not None else None),
                user_id=user_id,
            )
        except _RpcIneligibleError as exc:
            logger.debug(
                "cross-target RPC ineligible for per-target score sort (%s); using two-query path",
                exc,
            )
        except Exception:
            logger.warning(
                "cross-target RPC FAILED for per-target score sort; degrading to "
                "the slower two-query path",
                exc_info=True,
            )
        # Reached for a weighted score sort with a location / multi-word filter
        # (the RPC can't express those) — keep axis_weights so the overlay still
        # shows the blend, not the raw score.
        return _list_jobs_for_target_two_query(
            supabase,
            axis_weights=axis_weights,
            preferences=preferences,
            logistics=logistics,
            **kwargs,
        )
    try:
        return _list_jobs_for_target_rpc(supabase, **kwargs)
    except _RpcIneligibleError as exc:
        logger.debug("get_target_jobs ineligible (%s); using two-query path", exc)
    except Exception:
        logger.warning(
            "get_target_jobs RPC FAILED; degrading to the slower two-query path",
            exc_info=True,
        )
    # Non-score keyset fallback: axis_weights is None here (weighted non-score
    # sorts bail to two-query above), passed for consistency / future-proofing.
    return _list_jobs_for_target_two_query(
        supabase,
        axis_weights=axis_weights,
        preferences=preferences,
        logistics=logistics,
        **kwargs,
    )


def _list_jobs_across_user_targets_rpc(
    supabase: Client,
    *,
    user_target_ids: set[str],
    page_size: int,
    sort: str,
    ascending: bool,
    min_score: int | None,
    status: str | None,
    company: str | None,
    search: str | None,
    cursor: dict[str, Any],
    weights_by_target: dict[str, AxisWeights] | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Untargeted list via the server-side dedup+sort+paginate RPC (#365).

    The cross-target twin of ``_list_jobs_for_target_rpc``: pushes the status
    filter, best-representative dedup, off-family gate, Pending-exempt floor,
    graded-first sort, and pagination into ``get_cross_target_jobs`` — one
    round-trip returning just the page, instead of pulling every live scores
    row (JSONB and all) into Python to resolve status + rank (the 57014
    statement-timeout path, #365). Offset-paginated to match the two-query
    cursor contract. Raises to fall back to the two-query path for the shapes
    the RPC can't express DB-side (the dispatcher gates most; multi-word search
    is caught here since it uses OR semantics the RPC's single ILIKE can't).

    ``weights_by_target`` (#457): per-target custom axis weights. When present
    the RPC computes the weighted display blend DB-side (``p_weights``), so a
    custom-weight view uses this fast path instead of the scan-everything
    two-query fallback. Empty/None ⇒ the RPC ranks + returns the raw score
    exactly as before (index-only)."""
    if search and len(_tokenize_search(search)) > 1:
        raise _RpcIneligibleError("RPC path skipped: multi-word search uses OR semantics")
    offset = _offset_from_cursor(cursor)
    # target_id -> axis-weight object, the JSONB map the RPC blends by. Only
    # genuinely-customized weights reach here (defaults persist as NULL), so this
    # is empty for the common case and the RPC stays on its index-only plan.
    p_weights = {tid: w.model_dump() for tid, w in (weights_by_target or {}).items()}
    resp = supabase.rpc(
        "get_cross_target_jobs",
        {
            "p_target_ids": list(user_target_ids),
            "p_min_score": min_score or 0,
            "p_status": status,
            "p_company": company,
            "p_search": search,
            "p_sort": sort,
            "p_ascending": ascending,
            # One extra row to detect "has more" without a COUNT (the two-query
            # path's ``total`` is unavailable here; None is best-effort, same as
            # the per-target keyset RPC).
            "p_limit": page_size + 1,
            "p_offset": offset,
            "p_user_id": user_id,
            # Decay-aware graded sort key when read-time recency decay is on
            # (prod). The shared _apply_display_recency post-step still sets the
            # shown number; the RPC only needs the ORDER to match.
            "p_recency_decay": settings.recency_decay_enabled,
            "p_weights": p_weights,
        },
    ).execute()
    if not isinstance(resp.data, list):
        raise TypeError("RPC get_cross_target_jobs returned non-list response")
    rows = cast(list[dict[str, Any]], resp.data)
    has_more = len(rows) > page_size
    postings = rows[:page_size]
    # The RPC computes ``pending`` per row (its graded signal is ``axis_scores``,
    # which the list selects otherwise avoid) — the UI badges from it directly.
    next_cursor = _encode_cursor({"o": offset + page_size}) if has_more else None
    return {"postings": postings, "next_cursor": next_cursor, "total": None}


def _list_jobs_across_user_targets(
    supabase: Client,
    *,
    user_target_ids: set[str],
    page_size: int,
    sort: str,
    ascending: bool,
    min_score: int | None,
    status: str | None,
    company: str | None,
    search: str | None,
    exclude_terms: list[str],
    only_terms: list[str],
    cursor: dict[str, Any],
    weights_by_target: dict[str, AxisWeights] | None = None,
    user_id: str | None = None,
    logistics: _LogisticsFilter | None = None,
) -> dict[str, Any]:
    """Untargeted list — the union of jobs scored against any of the user's
    active targets, deduplicated by job id.

    Tries the server-side ``get_cross_target_jobs`` RPC first (single
    round-trip, DB-side status filter + dedup + rank + paginate — #365), then
    falls back to the two-query Python path. The RPC is skipped for shapes it
    can't express: post-fetch location / logistics filters (the RPC paginates
    with no knowledge of them, so its page would carry pre-filter rows), and the
    archived view (the RPC gates to live rows). Custom axis weights are NO LONGER
    a skip (#457) — the RPC computes the per-target weighted blend DB-side
    (``p_weights``), so custom-weight views take this fast path instead of the
    scan-everything two-query fallback. Read-time recency decay is likewise not a
    skip — the RPC ranks by the decayed score DB-side (``p_recency_decay``).
    Those exceptions keep the two-query path."""
    has_location_filter = bool(exclude_terms or only_terms)
    has_logistics = logistics is not None and logistics.active
    if has_location_filter or has_logistics or status == "archived":
        return _list_jobs_across_user_targets_two_query(
            supabase,
            user_target_ids=user_target_ids,
            page_size=page_size,
            sort=sort,
            ascending=ascending,
            min_score=min_score,
            status=status,
            company=company,
            search=search,
            exclude_terms=exclude_terms,
            only_terms=only_terms,
            cursor=cursor,
            weights_by_target=weights_by_target,
            user_id=user_id,
            logistics=logistics,
        )
    try:
        return _list_jobs_across_user_targets_rpc(
            supabase,
            user_target_ids=user_target_ids,
            page_size=page_size,
            sort=sort,
            ascending=ascending,
            min_score=min_score,
            status=status,
            company=company,
            search=search,
            cursor=cursor,
            weights_by_target=weights_by_target,
            user_id=user_id,
        )
    except _RpcIneligibleError as exc:
        logger.debug("get_cross_target_jobs ineligible (%s); using two-query path", exc)
    except Exception:
        logger.warning(
            "get_cross_target_jobs RPC FAILED; degrading to the slower two-query path",
            exc_info=True,
        )
    return _list_jobs_across_user_targets_two_query(
        supabase,
        user_target_ids=user_target_ids,
        page_size=page_size,
        sort=sort,
        ascending=ascending,
        min_score=min_score,
        status=status,
        company=company,
        search=search,
        exclude_terms=exclude_terms,
        only_terms=only_terms,
        cursor=cursor,
        weights_by_target=weights_by_target,
        user_id=user_id,
        logistics=logistics,
    )


def _list_jobs_across_user_targets_two_query(
    supabase: Client,
    *,
    user_target_ids: set[str],
    page_size: int,
    sort: str,
    ascending: bool,
    min_score: int | None,
    status: str | None,
    company: str | None,
    search: str | None,
    exclude_terms: list[str],
    only_terms: list[str],
    cursor: dict[str, Any],
    weights_by_target: dict[str, AxisWeights] | None = None,
    user_id: str | None = None,
    logistics: _LogisticsFilter | None = None,
) -> dict[str, Any]:
    """Untargeted list — returns the union of jobs scored against any of the
    user's active targets, deduplicated by job id.

    Two-query pattern, mirroring ``_list_jobs_for_target_two_query`` but
    aggregating by ``max(score)`` across the user's targets so each job
    appears once.

    ``weights_by_target`` maps target_id → AxisWeights for any user-target
    pairing with custom weights set; absent / None means use raw score.
    The displayed ``score`` per row applies the weights for that row's
    target. The deduplication still keys on raw ``max(score)`` so the
    "best representative target" picked per job is stable across users
    with different weights (only the displayed number differs).
    """
    offset = _offset_from_cursor(cursor)
    sort_col = "score" if sort == "score" else sort
    # The "score" sort orders by the score each row DISPLAYS (per-target
    # axis-weighted blend + read-time decay), computed below — not a stored
    # column. ``min_score`` still filters on the raw fit score.

    archived_view = status == "archived"
    score_query = (
        supabase.table("scores")
        .select(_SCORE_ROW_COLS + ", target_id" + ("" if archived_view else _JOBS_EMBED))
        .in_("target_id", list(user_target_ids))
        .eq("excluded", False)
    )
    score_query = _scores_live_join(score_query, archived_view=archived_view)
    score_query = _apply_score_floor(score_query, min_score)
    score_resp = score_query.execute()
    score_rows = cast(list[dict[str, Any]], score_resp.data or [])

    if not score_rows:
        return {"postings": [], "next_cursor": None, "total": 0}

    # Per-job: pick the best representative target. A graded row always beats a
    # Pending one (a real fit score over a keyword placeholder, #47); among rows
    # of the same gradedness, the higher score wins.
    best: dict[str, dict[str, Any]] = {}
    for row in score_rows:
        jid = row["job_posting_id"]
        existing = best.get(jid)
        if existing is None or _prefer_score_row(row, existing):
            best[jid] = row

    # Untargeted: each job's displayed score uses the weights of ITS best
    # target (jobs may resolve to different targets), so the weight resolver is
    # per-row rather than a constant.
    weights_by_target = weights_by_target or {}

    def _weights_for(row: dict[str, Any]) -> AxisWeights | None:
        tid = cast(str | None, row.get("target_id"))
        return weights_by_target.get(tid) if tid else None

    return _assemble_jobs_page(
        supabase,
        by_id=best,
        weights_for_row=_weights_for,
        offset=offset,
        page_size=page_size,
        sort=sort,
        sort_col=sort_col,
        ascending=ascending,
        status=status,
        company=company,
        search=search,
        exclude_terms=exclude_terms,
        only_terms=only_terms,
        preferences=None,  # untargeted view has no per-target preference filter
        user_id=user_id,
        logistics=logistics,
    )


# Sync `def` so FastAPI runs each request in a threadpool worker. The body
# makes multiple blocking supabase `.execute()` calls; `async def` would block
# the event loop and serialize concurrent /jobs reads.


def _parse_location_list(raw: str | None) -> list[str]:
    """Split a comma-separated filter (e.g. ``"India, Brazil, Berlin"``) into
    individual trimmed terms. Empty/None → empty list. Terms are lowercased
    here because the Python-side post-filter does case-insensitive substring
    matching against ``job.location`` (which is stored mixed-case)."""
    if not raw:
        return []
    return [t.strip().lower() for t in raw.split(",") if t.strip()]


# Curated synonyms for short, ambiguous location-filter tokens.
# The previous naive ``term in loc`` matched "us" against "A-us-tin"
# (Austin), "u-s-er" patterns, etc. — any 2-letter code collides with
# fragments of longer words. For these short codes we match at word
# boundaries and expand to common synonyms. Longer terms (≥4 chars)
# fall through to substring matching, which is forgiving for partial
# matches like "California" in "Northern California".
_LOCATION_SYNONYMS: dict[str, frozenset[str]] = {
    "us": frozenset({"us", "usa", "u.s.", "u.s.a.", "united states"}),
    "uk": frozenset({"uk", "u.k.", "united kingdom"}),
    "eu": frozenset({"eu", "europe", "european union"}),
    # "ca" intentionally NOT here — collides with California (US state).
    # Users wanting Canada should search "canada" explicitly.
}


def _term_matches_location(term: str, location_lower: str) -> bool:
    """True when ``term`` matches ``location_lower`` either via curated
    synonym word-boundary check (short codes) or substring (longer
    terms)."""
    candidates = _LOCATION_SYNONYMS.get(term, {term})
    for candidate in candidates:
        if len(candidate) <= 3:
            if re.search(rf"\b{re.escape(candidate)}\b", location_lower):
                return True
        elif candidate in location_lower:
            return True
    return False


def _location_passes(
    location: str | None,
    *,
    exclude_terms: list[str],
    only_terms: list[str],
) -> bool:
    """True when a posting's ``location`` should be visible under the user's
    location filter. ``only_terms`` is OR (any match wins). ``exclude_terms``
    is OR (any match excludes). Missing location is OK for ``only_terms``
    (we can't prove it doesn't match) but excluded only when a term explicitly
    targets ``""`` — the typical case keeps it visible.

    Matching: word-boundary check for 2-3 char tokens + synonyms (so
    "us" doesn't match "Austin"); substring for longer terms. See
    ``_term_matches_location``.

    Note: when ``only_terms`` is set and the location is None/empty,
    this returns False — we can't confirm a match. This matches the
    pre-fix behaviour. If users start complaining about jobs being
    hidden when Greenhouse omits location, flip this to "include
    unknown" with a Sentry log so we know the population at risk.
    """
    loc = (location or "").lower()
    if only_terms and not any(_term_matches_location(term, loc) for term in only_terms):
        return False
    return not (exclude_terms and any(_term_matches_location(term, loc) for term in exclude_terms))


def _apply_location_filter(
    postings: list[dict[str, Any]],
    *,
    exclude_terms: list[str],
    only_terms: list[str],
) -> list[dict[str, Any]]:
    """Drop postings whose ``location`` field fails the include/exclude
    terms. Applied post-fetch so we don't have to thread Supabase ``or_``
    chains through every list-jobs path; the trade-off is that pagination
    becomes approximate (a page can shrink when the filter trims rows),
    which matches how ``status``/``search`` already work in the two-query
    fallback path. Acceptable for an opt-in filter that most users won't
    enable."""
    if not exclude_terms and not only_terms:
        return postings
    return [
        p
        for p in postings
        if _location_passes(
            p.get("location"),
            exclude_terms=exclude_terms,
            only_terms=only_terms,
        )
    ]


def _logistics_passes(posting: dict[str, Any], f: _LogisticsFilter) -> bool:
    """One posting against the active logistics filters.

    Semantics per plan-wyrdfold-logistics-chips.md:
    - ``remote_only`` — STRICT: keep only ``remote_status == "remote"`` (an
      unknown/``unspecified`` status is dropped; the user explicitly asked for
      remote, so surfacing unknowns would dilute the filter).
    - ``min_salary`` — STRICT: an undisclosed salary is dropped. The bound
      PREFERS the deterministic jobs-level columns (``salary_max``/``salary_min``
      parsed from the posting's own salary text — present for the whole corpus,
      yearly-USD gated) and falls back to the Phase-2 grader's
      ``logistics_filters.salary_max`` (per-target LLM output, graded rows
      only) when the posting carries no structured yearly-USD salary. Before
      the columns existed this filter silently dropped every ungraded row.
    - ``country`` — LENIENT: keep when ``location_country`` matches
      (case-insensitive) OR is absent (a remote role with no country anchor
      still passes).
    """
    log = posting.get("logistics_filters") or {}
    if f.remote_only and log.get("remote_status") != "remote":
        return False
    if f.min_salary is not None:
        if posting.get("salary_currency") == "USD" and posting.get("salary_period") == "yearly":
            bound = posting.get("salary_max") or posting.get("salary_min")
        else:
            bound = log.get("salary_max")
        if bound is None or bound < f.min_salary:
            return False
    if f.country:
        country = log.get("location_country")
        if country is not None and str(country).upper() != f.country.upper():
            return False
    return True


def _apply_logistics_filter(
    postings: list[dict[str, Any]], f: _LogisticsFilter
) -> list[dict[str, Any]]:
    """Drop postings failing the logistics filters (#86), reading each row's
    overlaid ``logistics_filters`` dict. Post-fetch like the location filter, so
    it composes with the status/company/search/preference filters already on the
    two-query path (and forces that path when active — the RPC keyset can't see
    it)."""
    if not f.active:
        return postings
    return [p for p in postings if _logistics_passes(p, f)]


# ── Per-user target preferences (#60) ───────────────────────────────────────
# A read-time filter over the SHARED, cached fit score. NEVER a re-grade.
#
# Score cutoff is pushed into ``min_score`` (server-side, exact, keeps
# pagination correct). The remaining filters (employment_type / seniority /
# location-via-metro-or-text) are POST-FETCH and read the job's firewall tag
# columns. The tags are SERVED as of the R2 release (added to
# ``_JP_SELECT_COLS`` here; to the list RPCs in the R2 migration) — before
# that they were written by the tagger but never selected, so these filters
# silently passed everything ("starved tags", schema audit 2026-07-30).
# Still LENIENT on absence: the tagger doesn't backfill, so a missing/NULL
# tag means "unknown" and the job is KEPT — only a KNOWN tag outside the
# preference drops it.


def _job_tag(posting: dict[str, Any], column: str) -> str | None:
    """Return a job's firewall tag value, or ``None`` when the column is
    absent (firewall PR not deployed) or NULL/blank (not backfilled).

    Feature-detection: ``posting.get(column)`` is ``None`` both when the SELECT
    never asked for the column and when the row's value is NULL — both collapse
    to "unknown", which the predicates treat leniently (keep the job)."""
    value = posting.get(column)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _employment_type_passes(posting: dict[str, Any], allowed: list[str] | None) -> bool:
    """Keep the job when its ``employment_type`` tag is in ``allowed``.

    Lenient: no preference set → keep; unknown job tag → keep. Only an
    explicit, KNOWN tag that's outside the allowed set drops the job."""
    if not allowed:
        return True
    tag = _job_tag(posting, "employment_type")
    if tag is None:
        return True  # unknown → keep (lenient)
    allowed_lower = {a.strip().lower() for a in allowed if a.strip()}
    return not allowed_lower or tag.lower() in allowed_lower


def _seniority_passes(
    posting: dict[str, Any],
    *,
    seniority_min: str | None,
    seniority_max: str | None,
) -> bool:
    """Keep the job when its ``seniority`` tag falls within the inclusive
    [min, max] range on the ``SENIORITY_ORDER`` ladder.

    Lenient: no range set → keep; unknown job tag → keep; a job tag that
    isn't on the known ladder → keep (we can't position it, so don't hide
    it). Only a KNOWN, on-ladder tag outside the range drops the job."""
    if seniority_min is None and seniority_max is None:
        return True
    tag = _job_tag(posting, "seniority")
    if tag is None:
        return True  # unknown → keep
    try:
        rank = SENIORITY_ORDER.index(cast(Any, tag.lower()))
    except ValueError:
        return True  # off-ladder tag → keep (can't compare)
    if seniority_min is not None and rank < SENIORITY_ORDER.index(cast(Any, seniority_min)):
        return False
    return not (
        seniority_max is not None and rank > SENIORITY_ORDER.index(cast(Any, seniority_max))
    )


def _job_is_remote(posting: dict[str, Any]) -> bool:
    """True when a job is remote. Prefers the ``is_remote`` firewall tag; falls
    back to the word "remote" appearing in the free-text ``location``."""
    flag = posting.get("is_remote")
    if isinstance(flag, bool):
        return flag
    location = _job_tag(posting, "location")
    return location is not None and "remote" in location.lower()


def _location_pref_passes(
    posting: dict[str, Any],
    *,
    locations: list[str] | None,
    remote_ok: bool,
) -> bool:
    """Keep the job when it matches the user's preferred ``locations``.

    Matching uses the ``metro`` firewall tag when present, else a free-text
    substring/synonym match on ``location`` (reusing ``_term_matches_location``
    so "us" doesn't match "Austin"). ``remote_ok`` lets remote roles through
    even when they don't match a preferred location — detected via the
    ``is_remote`` tag when present, else the word "remote" in ``location``.

    Lenient: no preference set → keep; a job with neither a ``metro`` tag nor
    a ``location`` string → keep (we can't prove it doesn't match), matching
    the existing location-chip behaviour for unknown locations."""
    if not locations:
        return True

    # Remote escape hatch first — a remote role passes regardless of metro.
    if remote_ok and _job_is_remote(posting):
        return True

    metro = _job_tag(posting, "metro")
    location = _job_tag(posting, "location")
    if metro is None and location is None:
        return True  # unknown location → keep (lenient)

    terms = [loc.strip().lower() for loc in locations if loc.strip()]
    if not terms:
        return True

    # Prefer the structured metro tag (exact-ish, case-insensitive) when
    # present; otherwise fall back to the free-text location match. A metro
    # that misses still gets a second chance against a free-text location.
    if metro is not None:
        metro_lower = metro.lower()
        if any(_term_matches_location(t, metro_lower) for t in terms):
            return True
        if location is None:
            return False
    if location is not None:
        location_lower = location.lower()
        return any(_term_matches_location(t, location_lower) for t in terms)
    return False


def _preferences_have_post_fetch_filter(
    preferences: "TargetPreferences | None",
) -> bool:
    """True when the preferences carry a filter that must run POST-FETCH
    (employment-type / seniority / location). The score cutoff is excluded —
    it's folded into ``min_score`` server-side, not applied here."""
    if preferences is None:
        return False
    return bool(
        preferences.pref_employment_types
        or preferences.pref_seniority_min
        or preferences.pref_seniority_max
        or preferences.pref_locations
    )


def _apply_preferences_filter(
    postings: list[dict[str, Any]],
    preferences: "TargetPreferences | None",
) -> list[dict[str, Any]]:
    """Drop postings that fail the user's post-fetch preference filters
    (employment-type / seniority / location). Score-cutoff is handled
    server-side via ``min_score`` and is NOT re-applied here.

    All predicates are lenient on missing/NULL job tags (keep the job), so
    this is a no-op until the firewall tag columns exist + are populated."""
    if preferences is None or not _preferences_have_post_fetch_filter(preferences):
        return postings
    prefs = preferences  # local non-None binding for the closure below
    return [
        p
        for p in postings
        if _employment_type_passes(p, prefs.pref_employment_types)
        and _seniority_passes(
            p,
            seniority_min=prefs.pref_seniority_min,
            seniority_max=prefs.pref_seniority_max,
        )
        and _location_pref_passes(
            p,
            locations=prefs.pref_locations,
            remote_ok=prefs.pref_remote_ok,
        )
    ]


def _apply_display_recency(postings: list[dict[str, Any]]) -> None:
    """Decay each posting's *displayed* ``score`` by its age at read time.

    The score overlay sets ``score`` to the fit/axis-weighted blend and
    preserves the undecayed fit in ``raw_score``. Users also expect a stale
    posting to visibly fade, so we multiply the displayed score by the age
    decay derived from the posted date *now* — not the stored
    ``recency_score`` (which the poller only refreshes for jobs it re-touches,
    so it freezes for postings that age off the boards). ``raw_score`` is left
    intact — set by the overlay, and defaulted here for any row the overlay
    didn't touch — so the pure fit stays available to the UI/debugging.

    In-place mutation of the ``postings`` list. No-op when the recency flag is
    off (the displayed score is then the raw fit, exactly as before). Only the
    two JWT list paths call this; the operator/api-key view keeps raw scores.
    """
    if not settings.recency_decay_enabled:
        return
    now = datetime.now(UTC)
    for p in postings:
        score = p.get("score")
        # ``bool`` is an ``int`` subclass — guard so a stray True/False
        # never gets treated as a score.
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            continue
        score_int = int(score)
        p.setdefault("raw_score", score_int)
        p["score"] = display_recency_score(
            score_int, p.get("source_posted_at") or p.get("cataloged_at"), now
        )


@router.get("")
def list_jobs(
    cursor: str | None = Query(None),
    page_size: int = Query(20, ge=1, le=100),
    sort: str = Query("score", pattern="^(score|created_at|company_name|title)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    min_score: int | None = Query(None, ge=0, le=100),
    status: str | None = Query(
        None,
        pattern="^(new|saved|resume_draft|resume_ready|applied|interviewing|offer|rejected|archived)$",
    ),
    company: str | None = Query(None, max_length=200),
    search: str | None = Query(None, max_length=200),
    target_id: str | None = Query(None),
    exclude_locations: str | None = Query(None, max_length=500),
    only_locations: str | None = Query(None, max_length=500),
    remote_only: bool = Query(False),
    min_salary: int | None = Query(None, ge=0),
    country: str | None = Query(None, max_length=4),
    # #88 dual-auth: JWT callers ride the RLS user client (jobs/scores/targets
    # are SELECT-true; user_jobs/user_targets/user_profiles self-scoped, and
    # the get_target_jobs RPC is SECURITY INVOKER with an authenticated EXECUTE
    # grant, so RLS still governs inside it). Api-key callers (user_id None,
    # the operator view below) get the service-role client — they have no JWT
    # to bind. The client choice mirrors get_current_user_id_optional.
    supabase: Client = Depends(get_supabase_for_caller),
    user_id: str | None = Depends(get_current_user_id_optional),
) -> dict[str, Any]:
    exclude_terms = _parse_location_list(exclude_locations)
    only_terms = _parse_location_list(only_locations)
    # Logistics filters (#86) — over the grader's scores.logistics_filters data.
    logistics = _LogisticsFilter(remote_only=remote_only, min_salary=min_salary, country=country)
    cursor_data = _decode_cursor(cursor)
    ascending = order == "asc"

    # Check cache (60s TTL — data only changes on poll/manual-add cycles).
    # user_id participates in the key so per-user views (saved/dismissed,
    # future filtering) never cross-leak between accounts.
    cache_key = make_cache_key(
        jobs_cache_prefix(target_id=target_id),
        cursor=cursor,
        page_size=page_size,
        sort=sort,
        order=order,
        min_score=min_score,
        status=status,
        company=company,
        search=search,
        # Comma-joined here so two callers with the same set of terms in
        # different order share a cache entry. Terms are already lowercased
        # by ``_parse_location_list``.
        exclude_locations=",".join(sorted(exclude_terms)) or None,
        only_locations=",".join(sorted(only_terms)) or None,
        # Logistics filters vary the result set, so they must vary the key
        # (country upper-cased since the filter matches case-insensitively).
        remote_only=remote_only or None,
        min_salary=min_salary,
        country=(country or "").upper() or None,
        user_id=user_id,
    )
    cached: dict[str, Any] | None = job_list_cache.get(cache_key)
    if cached is not None:
        return cached

    # JWT callers see only postings from their ACTIVE memberships — pausing
    # a target removes its jobs from the list (Group D decision, 2026-07-30;
    # the link itself and all authz/dedup surfaces stay any-status). The
    # api-key path (cron/poller) bypasses scoping — it operates on the
    # whole table by design (e.g. backfill, rescore-all, cost rollup).
    user_target_ids: set[str] | None = None
    if user_id is not None:
        user_target_ids = get_active_target_ids(supabase, user_id)
        if not user_target_ids:
            empty: dict[str, Any] = {
                "postings": [],
                "next_cursor": None,
                "total": 0,
                "applied_min_score": min_score,
            }
            job_list_cache.set(cache_key, empty)
            return empty
        if target_id and target_id not in user_target_ids:
            empty = {
                "postings": [],
                "next_cursor": None,
                "total": 0,
                "applied_min_score": min_score,
            }
            job_list_cache.set(cache_key, empty)
            return empty

    # When no chip is set, fall back to the user's stored threshold —
    # historically ``user_profiles.job_score_threshold`` only gated SMS
    # notifications, so a senior user with threshold 70 still saw 5k+
    # rows of noise in the list. Caller can pass ``min_score=0`` to
    # explicitly opt out of the default; ``applied_min_score`` is
    # echoed in the response so the UI can render a "filtered to ≥N"
    # chip with a clear affordance.
    if min_score is None and user_id is not None:
        min_score = _default_min_score_for_user(supabase, user_id)

    # Target view: sort/paginate by target-specific scores
    if target_id:
        # Per-pairing axis weights override the displayed score for this
        # user's target view. Per-pairing preferences (#60) filter/re-rank
        # that view at read time. Both are JWT-only — api-key callers get raw
        # scores + no preference filtering (no user identity to scope by) — and
        # both are read from the SAME user_targets row, so this is one round-trip.
        axis_weights: AxisWeights | None = None
        preferences: TargetPreferences | None = None
        if user_id is not None:
            ut = get_user_target(supabase, user_id, target_id)
            if ut is not None:
                axis_weights = ut.axis_weights
                preferences = preferences_from_user_target(ut)
                # Fold the preference score cutoff into the effective floor.
                # Take the MAX of any explicit/profile-default min_score and
                # the cutoff so the stricter bar wins, and so a user who set
                # min_score=0 to "see everything" still can't drop below a
                # cutoff they configured. Pushed server-side via min_score —
                # this is a filter over the shared cached score, not a re-grade.
                cutoff = preferences.pref_score_cutoff
                min_score = cutoff if min_score is None else max(min_score, cutoff)
        result = _list_jobs_for_target(
            supabase,
            target_id=target_id,
            page_size=page_size,
            sort=sort,
            ascending=ascending,
            min_score=min_score,
            status=status,
            company=company,
            search=search,
            exclude_terms=exclude_terms,
            only_terms=only_terms,
            cursor=cursor_data,
            axis_weights=axis_weights,
            preferences=preferences,
            user_id=user_id,
            logistics=logistics,
        )
        # Decay the displayed score by posting age (read-time, never stale).
        # raw_score keeps the undecayed fit. No-op when the flag is off.
        _apply_display_recency(result["postings"])
        result["applied_min_score"] = min_score
        job_list_cache.set(cache_key, result)
        return result

    # Untargeted list — for JWT callers, return the union of jobs scored
    # against any of the user's active targets (deduplicated). For api-key
    # callers (cron/poller) we keep the old "table scan" path: they need
    # to operate on the whole table by design (rescore-all, backfill).
    if user_target_ids is not None:
        # Build target_id -> AxisWeights map for any pairings that have
        # custom weights set. Missing entries fall through to raw score.
        weights_by_target: dict[str, AxisWeights] = {}
        for ut in list_user_targets(supabase, user_id):  # type: ignore[arg-type]
            if ut.axis_weights is not None:
                weights_by_target[ut.target_id] = ut.axis_weights
        result = _list_jobs_across_user_targets(
            supabase,
            user_target_ids=user_target_ids,
            page_size=page_size,
            sort=sort,
            ascending=ascending,
            min_score=min_score,
            status=status,
            company=company,
            search=search,
            exclude_terms=exclude_terms,
            only_terms=only_terms,
            cursor=cursor_data,
            weights_by_target=weights_by_target or None,
            user_id=user_id,
            logistics=logistics,
        )
        # Decay the displayed score by posting age (read-time, never stale).
        # raw_score keeps the undecayed fit. No-op when the flag is off.
        _apply_display_recency(result["postings"])
        result["applied_min_score"] = min_score
        job_list_cache.set(cache_key, result)
        return result

    # Operator path (api-key, no JWT): full table view, no target scoping.
    # The operator/global view only distinguishes live vs archived; per-user
    # statuses (saved/applied/…) don't apply without a user, so we derive a
    # ``status`` of "archived"/"new" from ``archived_at`` for the response.
    query = supabase.table("jobs").select(
        _JP_SELECT_COLS + ", archived_at",
        count=CountMethod.exact,
    )
    if min_score is not None:
        query = query.gte("score", min_score)
    if status == "archived":
        # Operators wanting an archive audit pass status='archived' to see
        # globally-dead jobs.
        query = query.not_.is_("archived_at", "null")
    else:
        # Default (status is None or any per-user value, which the operator
        # has no notion of): show only live jobs.
        query = query.is_("archived_at", "null")
    if company:
        query = query.eq("company_name", company)
    query = _apply_title_search(query, search)

    # Per-user status isn't sortable on the global view (column gone); fall
    # back to a safe default if a caller asks to sort by it.
    operator_sort = "created_at" if sort == "status" else sort

    # Operator view keeps offset pagination under an opaque cursor (it's a
    # bounded table scan, not the keyset hot path).
    operator_offset = _offset_from_cursor(cursor_data)

    def _finalize_operator_rows(
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        # Derive the response ``status`` from global liveness and drop the
        # helper column so the response shape is unchanged.
        for row in rows:
            row["status"] = "archived" if row.get("archived_at") else "new"
            row.pop("archived_at", None)
        return rows

    has_location_filter = bool(exclude_terms or only_terms)
    if has_location_filter:
        # Location is post-fetch — server-side ``.range()`` would return
        # a pre-filter page whose total is wrong and whose contents may
        # mostly get trimmed. Fetch the full (pre-location) set ordered
        # server-side, filter in Python, then paginate from the result.
        query = query.order(operator_sort, desc=not ascending).limit(_OPERATOR_LOCATION_SCAN_CAP)
        resp = query.execute()
        all_rows = cast(list[dict[str, Any]], list(resp.data or []))
        if len(all_rows) >= _OPERATOR_LOCATION_SCAN_CAP:
            logger.warning(
                "Operator location filter hit the %d-row scan cap; postings "
                "beyond it were not searched.",
                _OPERATOR_LOCATION_SCAN_CAP,
            )
        filtered = _apply_location_filter(
            all_rows,
            exclude_terms=exclude_terms,
            only_terms=only_terms,
        )
        operator_result: dict[str, Any] = {
            "postings": _finalize_operator_rows(
                filtered[operator_offset : operator_offset + page_size]
            ),
            "next_cursor": _offset_next_cursor(operator_offset, page_size, len(filtered)),
            "total": len(filtered),
            "applied_min_score": min_score,
        }
    else:
        query = query.order(operator_sort, desc=not ascending).range(
            operator_offset, operator_offset + page_size - 1
        )
        resp = query.execute()
        operator_total = resp.count or 0
        operator_result = {
            "postings": _finalize_operator_rows(cast(list[dict[str, Any]], list(resp.data or []))),
            "next_cursor": _offset_next_cursor(operator_offset, page_size, operator_total),
            "total": operator_total,
            "applied_min_score": min_score,
        }
    job_list_cache.set(cache_key, operator_result)
    return operator_result


_JOB_STATUSES = (
    "new",
    "saved",
    "resume_draft",
    "resume_ready",
    "applied",
    "interviewing",
    "offer",
    "rejected",
    "archived",
)


def _pipeline_counts_python(
    supabase: Client,
    *,
    target_ids: set[str],
    min_score: int | None,
    user_id: str | None,
) -> dict[str, int]:
    """Fallback used when the ``pipeline_counts`` RPC is unavailable
    (e.g. mid-deploy before the migration lands). Mirrors the JWT list
    path: scores rows for the user's targets (excluded=False, optional
    score floor), deduplicated by job, then grouped by the caller's
    per-user status (``user_jobs`` row; absent → ``'new'``)."""
    score_query = (
        supabase.table("scores")
        .select("job_posting_id")
        .in_("target_id", list(target_ids))
        .eq("excluded", False)
    )
    # Floor exempts Pending rows so the tab counts match the list (#47).
    score_query = _apply_score_floor(score_query, min_score)
    score_resp = score_query.execute()
    job_ids = sorted(
        {cast(str, r["job_posting_id"]) for r in cast(list[dict[str, Any]], score_resp.data or [])}
    )
    counts: dict[str, int] = {}
    for i in range(0, len(job_ids), _IN_CHUNK_SIZE):
        chunk = job_ids[i : i + _IN_CHUNK_SIZE]
        # Liveness gate (#75 C3) + non-US gate (#60): count only jobs that are
        # still live AND not confirmed non-US, so the tab totals match the list.
        live_resp = _gate_live_us(supabase.table("jobs").select("id").in_("id", chunk)).execute()
        live_ids = [cast(str, r["id"]) for r in cast(list[dict[str, Any]], live_resp.data or [])]
        # Resolve per-user status for the chunk; jobs with no user_jobs
        # row — and every job when there's no user identity — count as
        # 'new' (#75 "absent = new" rule).
        status_map: dict[str, str] = {}
        if user_id is not None:
            uj_resp = (
                supabase.table("user_jobs")
                .select("job_posting_id,status")
                .eq("user_id", user_id)
                .in_("job_posting_id", chunk)
                .execute()
            )
            status_map = {
                cast(str, r["job_posting_id"]): cast(str, r["status"])
                for r in cast(list[dict[str, Any]], uj_resp.data or [])
            }
        for jid in live_ids:
            st = status_map.get(jid, "new")
            counts[st] = counts.get(st, 0) + 1
    return counts


def _pipeline_counts_grouped(
    supabase: Client,
    *,
    target_ids: set[str],
    min_score: int | None,
    user_id: str | None,
) -> dict[str, int]:
    """Single grouped count via the ``pipeline_counts`` RPC; falls back
    to the client-side chunked variant if the RPC isn't deployed yet.

    Floored counts ride the RPC too: since 20260716050000 it mirrors
    ``_apply_score_floor`` exactly (the floor applies only to rows with
    ``scoring_status = 'complete'``; Pending rows always pass, #47).
    Floored users previously forced the Python path — ~1.6–2.6s of chunked
    round-trips inside the dashboard's hottest projection."""
    try:
        resp = supabase.rpc(
            "pipeline_counts",
            {
                "p_target_ids": sorted(target_ids),
                "p_min_score": min_score,
                "p_user_id": user_id,
            },
        ).execute()
    except Exception:
        logger.warning(
            "pipeline_counts RPC FAILED; degrading to the slower client-side count",
            exc_info=True,
        )
        return _pipeline_counts_python(
            supabase, target_ids=target_ids, min_score=min_score, user_id=user_id
        )
    return {
        cast(str, row["status"]): int(row["count"])
        for row in cast(list[dict[str, Any]], resp.data or [])
    }


@router.get("/pipeline-counts")
def pipeline_counts(
    # #88 dual-auth: get_current_user_id is JWT-required, so every caller that
    # reaches the body carries a JWT and rides the RLS user client (the
    # pipeline_counts RPC is SECURITY INVOKER; user_jobs/user_targets/
    # user_profiles are self-scoped, scores/jobs SELECT-true).
    supabase: Client = Depends(get_supabase_for_caller),
    user_id: str = Depends(get_current_user_id),
) -> dict[str, int]:
    """Per-status job counts for the calling user's pipeline.

    Projection endpoint for the dashboard — replaces seven
    ``/jobs?status=X&page_size=1`` round-trips that each ran the full
    list query just to read ``total``. Semantics match the untargeted
    JWT list view: union of jobs scored against any of the user's
    targets (``excluded=False``), with the user's ``list_min_score``
    default applied as the score floor.
    """
    cache_key = make_cache_key(
        jobs_cache_prefix(target_id=None),
        projection="pipeline_counts",
        user_id=user_id,
    )
    cached: dict[str, int] | None = job_list_cache.get(cache_key)
    if cached is not None:
        return cached

    counts: dict[str, int] = dict.fromkeys(_JOB_STATUSES, 0)
    # ACTIVE memberships only — the tab counts must match the list scope.
    target_ids = get_active_target_ids(supabase, user_id)
    if target_ids:
        min_score = _default_min_score_for_user(supabase, user_id)
        grouped = _pipeline_counts_grouped(
            supabase, target_ids=target_ids, min_score=min_score, user_id=user_id
        )
        for status_key, n in grouped.items():
            if status_key in counts:
                counts[status_key] = n
    job_list_cache.set(cache_key, counts)
    return counts


@router.post("/validate-url")
@limiter.limit("20/minute")
async def validate_url(
    request: Request,
    body: UrlValidateRequest,
) -> UrlValidateResponse:
    result = await validate_job_url(body.url)
    return UrlValidateResponse(
        is_valid=result.is_valid,
        final_url=result.final_url,
        warnings=result.warnings,
        rejection_reason=result.rejection_reason,
    )


@router.post("/manual")
@limiter.limit("10/minute")
async def add_manual_job(
    request: Request,
    body: ManualJobRequest,
    supabase: Client = Depends(get_supabase),
    # #6 R2 step 2: the manual-add scores writes are gated through SECURITY
    # DEFINER RPCs on the caller's client (user JWT → target ownership enforced
    # in-DB; api-key/operator → service-role, exempt). Reads + the job insert
    # stay on the service-role `supabase`.
    caller_supabase: Client = Depends(get_supabase_for_caller),
    user_id: str | None = Depends(get_current_user_id_optional),
) -> ManualJobResponse:
    """Add a job posting by URL. Extracts metadata via cascade."""
    warnings: list[str] = []

    # Layer 1: Format validation
    cleaned = validate_format(body.url)
    if cleaned is None:
        raise HTTPException(status_code=400, detail="Malformed URL")

    # Layer 2: Banned domain check
    hostname = urlparse(cleaned).hostname or ""
    if is_banned_domain(hostname):
        raise HTTPException(
            status_code=400,
            detail=f"Banned domain: {registrable_domain(hostname)}",
        )

    # SSRF defense — refuse to fetch URLs that resolve to private/internal IPs.
    try:
        assert_safe_host(hostname)
    except ValueError as exc:
        # Generic client message — echoing the resolved host/IP back lets a
        # caller enumerate which internal hostnames resolve to private ranges
        # (recon oracle, audit #29 R3 / H8). Keep specifics server-side.
        logger.warning("ssrf_reject host=%s: %s", hostname, exc)
        raise HTTPException(status_code=400, detail="This URL cannot be fetched") from exc

    # Fetch the page with a hard size cap — without this, a user
    # pasting a URL to a multi-GB payload (CDN downloads, infinite
    # streams) would OOM the API, since ``client.get()`` buffers the
    # whole body before returning. ``get_with_size_cap`` streams and
    # aborts past ``MAX_USER_FETCH_BYTES``.
    try:
        # validate_host gates every redirect hop (not just the first/final
        # URL) before connecting — closes the SSRF redirect gap (#110).
        resp, body_bytes = await get_with_size_cap(cleaned, validate_host=assert_safe_host)
        final_url = str(resp.url)
    except ResponseTooLargeError as exc:
        raise HTTPException(
            status_code=413,
            detail=f"Page too large to fetch ({exc.size} bytes > {exc.limit}).",
        ) from exc
    except UnsafeURLError as exc:
        # A redirect hop resolved to an internal address. Don't reflect the
        # resolved host/IP (audit #29 R3 / H8).
        logger.warning("ssrf_reject redirect for %s: %s", cleaned, exc)
        raise HTTPException(status_code=400, detail="This URL cannot be fetched") from exc
    except httpx.HTTPError:
        raise HTTPException(status_code=400, detail="Failed to fetch URL") from None

    # Check post-redirect domain
    final_hostname = urlparse(final_url).hostname or ""
    if is_banned_domain(final_hostname):
        raise HTTPException(
            status_code=400,
            detail=f"Redirects to banned domain: {registrable_domain(final_hostname)}",
        )
    if final_hostname and final_hostname != hostname:
        try:
            assert_safe_host(final_hostname)
        except ValueError as exc:
            # Don't reflect the resolved internal host/IP (audit #29 R3 / H8).
            logger.warning("ssrf_reject redirect host=%s: %s", final_hostname, exc)
            raise HTTPException(
                status_code=400,
                detail="This URL cannot be fetched",
            ) from exc
    if registrable_domain(hostname) != registrable_domain(final_hostname):
        warnings.append(
            f"redirect_domain_change:"
            f"{registrable_domain(hostname)}->"
            f"{registrable_domain(final_hostname)}"
        )

    # Extract metadata. ``body_bytes`` came from the size-capped
    # streaming read; ``resp.text`` is empty here because the stream
    # was consumed manually, so decode the bytes ourselves.
    html = body_bytes.decode("utf-8", errors="replace") if resp.status_code == 200 else ""
    extraction: ExtractionResult
    if html:
        extraction = extract_job_from_html(html, final_url)
    else:
        warnings.append(f"http_status:{resp.status_code}")
        extraction = ExtractionResult(tier="none", warnings=["fetch_non_200"])

    # Tier 3: Firecrawl fallback if extraction found nothing
    if extraction.tier == "none":
        fc_result = await _extract_from_firecrawl(final_url)
        if fc_result.tier != "none":
            extraction = fc_result
        else:
            warnings.extend(fc_result.warnings)

    warnings.extend(extraction.warnings)

    # Merge: user overrides take precedence
    title = body.title or extraction.title
    company_name = body.company_name or extraction.company_name or ""
    location = body.location or extraction.location
    description_html = extraction.description_html or ""

    extracted_summary = {
        "title": extraction.title,
        "company_name": extraction.company_name,
        "location": extraction.location,
    }

    # If no title, return partial result asking for manual fields
    if not title:
        return ManualJobResponse(
            success=False,
            extracted=extracted_summary,
            extraction_tier=extraction.tier,
            warnings=warnings,
            needs_manual_fields=True,
        )

    # Materialize the posting as a real job + score it against the caller's OWN
    # active targets. Scoping to the caller's targets is a privacy boundary: an
    # unscoped fan-out would write scores under every user's active target,
    # surfacing one user's pasted URL in every other user's /jobs list via the
    # scores→user_targets join. Operator/api-key callers (user_id None) keep the
    # global fan-out for cron/admin back-compat. Shared with the from-url target
    # flow via materialize_and_score_job (upsert + score + global + force-include).
    if user_id is not None:
        active_targets = await asyncio.to_thread(get_active_for_user, supabase, user_id)
    else:
        active_targets = await asyncio.to_thread(get_active_target, supabase)
    try:
        posting_id = await materialize_and_score_job(
            supabase,
            final_url=final_url,
            title=title,
            company_name=company_name,
            location=location,
            description_html=description_html,
            salary_text=extraction.salary_text,
            targets=active_targets,
        )
    except APIError as exc:
        # A clean 502 rather than leaking the raw Postgres/FK message.
        logger.error("Manual job upsert failed for url=%s: %s", final_url, exc, exc_info=exc)
        raise HTTPException(
            status_code=502,
            detail="Couldn't save this job right now — please try again.",
        ) from exc

    return ManualJobResponse(
        success=True,
        posting_id=posting_id,
        extracted=extracted_summary,
        extraction_tier=extraction.tier,
        warnings=warnings,
        needs_manual_fields=False,
    )


def _load_live_job(supabase: Client, job_id: str) -> dict[str, Any] | None:
    """Load a LIVE, US posting's scoring inputs (title + JD body) by id, or None.

    Mirrors ``job_search``'s live+US predicate exactly (archived_at IS NULL AND
    purged_at IS NULL AND is_us IS NOT FALSE) so "addable" ⇔ "searchable": a
    dead / purged / non-US id 404s instead of letting a caller resurrect a
    hidden row into their pipeline by guessing its UUID.
    """
    resp = (
        supabase.table("jobs")
        .select("id, title, description_html")
        .eq("id", job_id)
        .is_("archived_at", "null")
        .is_("purged_at", "null")
        .not_.is_("is_us", "false")
        .limit(1)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return rows[0] if rows else None


# Sync `def` (not `async def`): all the work is blocking supabase round-trips
# (score compute + gated upserts), so FastAPI runs it in its threadpool and
# keeps them off the event loop. See #107.
@router.post("/{job_id}/add-to-target", response_model=AddToTargetResponse)
@limiter.limit("30/minute")
def add_job_to_target(
    request: Request,
    job_id: str,
    body: AddToTargetRequest,
    user_id: str = Depends(get_current_user_id),
    # SEC-H2 (migration 20260718010000): the user_upsert_score /
    # user_set_scores_included SECURITY DEFINER RPCs are NOT executable by
    # `authenticated` — their in-DB check only verifies the caller *follows* the
    # target, which is insufficient for a SHARED, ownerless target catalog (a
    # follower could tamper with co-followers' scores). So all score writes go on
    # the SERVICE-ROLE client (auth.uid() NULL → the functions' service-role-exempt
    # branch), and OWNERSHIP is enforced in the API here (``get_user_target``
    # below) — exactly the model ``POST /jobs/manual`` uses. ``caller_supabase`` is
    # only for the RLS-backstopped reads (job load + the ownership check).
    caller_supabase: Client = Depends(get_user_supabase),
    service_supabase: Client = Depends(get_supabase),
) -> AddToTargetResponse:
    """Score an EXISTING posting (a search result's ``jobs.id``) against one of
    the caller's own targets and save it to their pipeline (#467 power-action).

    Unlike ``POST /jobs/manual`` this NEVER materializes a new job row — the
    posting is already in the shared corpus — so it can't create the
    manual-pseudo-source duplicate that ``materialize_and_score_job`` would.
    """
    # Load the posting's scoring inputs. Must be a live+US posting (exactly what
    # search surfaces); a dead / purged / unknown id 404s.
    job = _load_live_job(caller_supabase, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    # Ownership gate — THE enforcement (targets are a shared catalog, so the
    # service-role writes below carry no in-DB ownership check). Same 404 for
    # "target doesn't exist" and "not yours" so the response never leaks another
    # user's target ids. Read on the caller's client so RLS is the backstop.
    if get_user_target(caller_supabase, user_id, body.target_id) is None:
        raise HTTPException(status_code=404, detail="Target not found for user")

    target = get_target(service_supabase, body.target_id)
    if target is None:  # catalog row vanished between the two reads — defensive
        raise HTTPException(status_code=404, detail="Target not found")

    # Stage-2 score the existing posting against the chosen target on the
    # SERVICE-ROLE client. ``gated=True`` still routes through user_upsert_score,
    # but with auth.uid() NULL it takes the function's service-role-exempt branch
    # (the RPC isn't authenticated-executable post-lockdown — see above).
    try:
        result = score_and_upsert(
            service_supabase,
            job_posting_id=job_id,
            title=job["title"] or "",
            description_html=job["description_html"] or "",
            target=target,
            gated=True,
        )
    except APIError as exc:
        logger.error(
            "add-to-target scoring failed job=%s target=%s: %s",
            job_id,
            body.target_id,
            exc,
            exc_info=exc,
        )
        raise HTTPException(
            status_code=502,
            detail="Couldn't add this job right now — please try again.",
        ) from exc

    # Best-effort bookkeeping AFTER the score row is committed (mirrors
    # materialize_and_score_job): a transient failure in any of these must not
    # fail the whole action — the score (the core effect) is already written, so
    # a 500 here would read as "nothing happened" when the job IS scored and
    # will show under the target. Each step is independent and logged.
    #  1. Force-include under THIS one target so a negative-keyword ``excluded``
    #     flag can't hide a job the user deliberately added — scoped to the single
    #     target, on the service-role client (audit #24 F4).
    try:
        service_supabase.rpc(
            "user_set_scores_included",
            {"p_job_posting_id": job_id, "p_target_ids": [body.target_id]},
        ).execute()
    except Exception:
        logger.exception("add-to-target force-include failed for job %s", job_id)
    #  3. Mark it 'saved' in the caller's pipeline — parity with the from-url add
    #     path (targets/from_input.py): a deliberately-added posting is 'saved',
    #     not the auto-surfaced 'new'. Service-role, keyed by the caller's user_id.
    try:
        persistence.upsert_user_job(
            service_supabase, user_id=user_id, job_posting_id=job_id, status="saved"
        )
    except Exception:
        logger.exception("add-to-target user_job save failed for job %s", job_id)
    job_list_cache.invalidate()

    return AddToTargetResponse(
        success=True,
        job_posting_id=job_id,
        target_id=body.target_id,
        score=result.score,
    )


# Sync `def` (not `async def`): the bulk re-score is blocking supabase work,
# so FastAPI runs it in its threadpool and keeps the O(jobs x keywords) DB
# round-trips off the event loop. See #107.
@router.post("/rescore/{target_id}", dependencies=[Depends(verify_api_key)])
def rescore_for_target(
    target_id: str,
    supabase: Client = Depends(get_supabase),
) -> dict[str, Any]:
    """Re-score all jobs against a target's scoring profile.

    Admin / operator-only: gated by ``verify_api_key`` so an
    unauthenticated caller can't trigger an O(jobs × scoring_keywords)
    DB-heavy re-score by hitting the API directly. Not reachable from
    the wyrdfold FE — only invoked manually from the operator console
    or from CLI scripts that supply the api key.
    """
    target = get_target(supabase, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Target not found")

    scored = bulk_score_for_target(supabase, target)
    job_list_cache.invalidate()
    return {"target_id": target_id, "jobs_scored": scored}


@router.post("/backfill-salary", dependencies=[Depends(verify_api_key)])
def backfill_salary(
    supabase: Client = Depends(get_supabase),
) -> dict[str, Any]:
    """One-off: extract salary from description_html for jobs missing salary_text.

    Per batch of 500, extract salaries in Python then write all rows in a
    single `bulk_update_salaries` RPC — turns ~N row-by-row UPDATEs
    into one statement per batch.

    Admin-only: gated by ``verify_api_key`` so an unauthenticated
    caller can't trigger a full-table scan + per-row salary extraction.
    Not reachable from the wyrdfold FE.
    """
    batch_size = 500
    offset = 0
    updated = 0

    while True:
        resp = (
            supabase.table("jobs")
            .select("id, description_html")
            .is_("salary_text", "null")
            .range(offset, offset + batch_size - 1)
            .execute()
        )
        rows = cast(list[dict[str, Any]], resp.data or [])
        if not rows:
            break

        updates: list[dict[str, Any]] = []
        for row in rows:
            html = row.get("description_html") or ""
            if not html:
                continue
            salary = extract_salary_from_html(html)
            if salary:
                updates.append({"id": row["id"], "salary_text": salary, **salary_columns(salary)})

        if updates:
            supabase.rpc("bulk_update_salaries", {"p_updates": updates}).execute()
            updated += len(updates)

        if len(rows) < batch_size:
            break
        offset += batch_size

    job_list_cache.invalidate()
    return {"updated": updated}


def _assert_user_owns_posting(
    supabase: Client,
    posting_id: str,
    user_id: str,
    *,
    include_description: bool = False,
) -> dict[str, Any]:
    """Look up a posting and verify the caller is linked (via
    ``user_targets``) to at least one target that has scored this
    posting. 404 on either missing or unowned (don't leak existence of
    postings outside the user's targets).

    ``include_description=True`` adds ``description_html`` to the
    projection — needed by the per-posting detail GET, omitted from the
    other callers (delete, ownership-only checks) so we don't move the
    full JD body across the wire on every status mutation.

    Ownership is derived through ``scores``: the poller writes
    ``scores`` rows keyed by ``(job_posting_id, target_id)``, while
    ``jobs.target_id`` is **not** populated. Checking ``jobs.target_id``
    directly (the previous shape) always 404'd on real postings. This
    mirrors the fix applied in ``status.py`` (PR #676) — same root
    cause, separate copy of the helper.
    """
    # 1. Fetch the posting (and projection).
    select_cols = _JP_DETAIL_SELECT_COLS if include_description else _JP_SELECT_COLS
    posting_resp = (
        supabase.table("jobs").select(select_cols).eq("id", posting_id).limit(1).execute()
    )
    rows = posting_resp.data or []
    if not rows or not isinstance(rows[0], dict):
        raise HTTPException(status_code=404, detail="Posting not found")
    row = cast(dict[str, Any], rows[0])

    # 2. Resolve the caller's target ids.
    user_targets_resp = (
        supabase.table("user_targets").select("target_id").eq("user_id", user_id).execute()
    )
    user_target_ids = {cast(dict[str, Any], r)["target_id"] for r in user_targets_resp.data or []}
    if not user_target_ids:
        raise HTTPException(status_code=404, detail="Posting not found")

    # 3. Confirm at least one of the user's targets has a score row for
    # this posting. Exposing the matched target_id on the returned row
    # so callers can scope cache invalidation, mirroring the old
    # ``jobs.target_id`` contract. Also pull ``score`` + ``score_breakdown``
    # so detail callers can overlay them — ``jobs.score`` and
    # ``jobs.score_breakdown`` are vestigial pre-shared-targets columns
    # that the poller doesn't update (it writes ``scores``), so reading
    # them directly off the posting row yields stale ``0`` / ``{}``.
    score_resp = (
        supabase.table("scores")
        .select("target_id, score, score_breakdown")
        .eq("job_posting_id", posting_id)
        .in_("target_id", list(user_target_ids))
        .order("score", desc=True)
        .limit(1)
        .execute()
    )
    score_rows = cast(list[dict[str, Any]], score_resp.data or [])
    if not score_rows:
        raise HTTPException(status_code=404, detail="Posting not found")
    best = score_rows[0]
    row["target_id"] = best["target_id"]
    # Stash the live score onto the row under an alias so callers can opt
    # in to the overlay without changing the existing ``score`` /
    # ``score_breakdown`` semantics on routes that don't need it.
    row["_target_score"] = best.get("score")
    row["_target_score_breakdown"] = best.get("score_breakdown")
    return row


@router.get("/{posting_id}")
def get_job(
    posting_id: str,
    user_id: str = Depends(get_current_user_id),
    # #88 dual-auth: JWT-required endpoint, so callers ride the RLS user
    # client (ownership probe reads jobs/user_targets/scores — SELECT-true or
    # self-scoped; the status overlay reads the caller's own user_jobs row).
    supabase: Client = Depends(get_supabase_for_caller),
) -> dict[str, Any]:
    # Detail GET pulls ``description_html`` so the UI can render the JD
    # body. The list endpoint deliberately omits it for payload size, but
    # there's no rendering of a single posting without the JD text.
    row = _assert_user_owns_posting(supabase, posting_id, user_id, include_description=True)
    # Overlay the live per-target score + breakdown. The ``jobs.score`` /
    # ``jobs.score_breakdown`` columns are vestigial and never updated
    # by the poller — without this, the detail view reads stale ``0`` /
    # ``{}`` and the "Score Breakdown" panel renders "No factors
    # contributed to this score" for every posting. Use the best score
    # across the user's targets (matches the untargeted list view's
    # per-job aggregation).
    target_score = row.pop("_target_score", None)
    target_breakdown = row.pop("_target_score_breakdown", None)
    if target_score is not None:
        row["score"] = target_score
    if target_breakdown is not None:
        row["score_breakdown"] = target_breakdown
    # Decay the displayed score by posting age so the detail view matches
    # the (now age-decayed) list score; raw_score keeps the undecayed fit.
    # No-op when the recency flag is off.
    _apply_display_recency([row])
    # Drop the helper target_id column we only fetched for ownership.
    row.pop("target_id", None)
    # Overlay the per-user pipeline status (#75 C4: jobs.status was dropped).
    # Postings the user never touched have no user_jobs row and read as 'new'.
    uj_resp = (
        supabase.table("user_jobs")
        .select("status")
        .eq("user_id", user_id)
        .eq("job_posting_id", posting_id)
        .limit(1)
        .execute()
    )
    uj_rows = cast(list[dict[str, Any]], uj_resp.data or [])
    row["status"] = cast(str, uj_rows[0]["status"]) if uj_rows else "new"
    return row


@router.delete("/{posting_id}")
def delete_job(
    posting_id: str,
    user_id: str = Depends(get_current_user_id),
    # #88 Phase 3: RLS client — the only write is the caller's own user_jobs
    # row (full CRUD self-policy); the ownership probe reads shared-catalog
    # tables (SELECT true). RLS makes the cross-tenant-cascade class of bug
    # (audit #29 r3 H1) structurally impossible, not just guarded.
    supabase: Client = Depends(get_user_supabase),
) -> dict[str, Any]:
    """Per-user "delete" of a job from the caller's pipeline.

    ``jobs`` is a SHARED, deduplicated catalog with no owner column —
    per-user pipeline state lives in ``user_jobs``. The previous shape ran
    an unscoped service-role ``jobs.delete().eq('id', …)``; FK
    ``ON DELETE CASCADE`` then wiped ``scores`` / ``job_feedback`` /
    ``status_log`` / ``user_jobs`` for **every** other user following that
    posting — cross-tenant data destruction triggered by the ordinary
    "delete job" button (audit #29 round 3 / H1).

    The user-facing "delete" is therefore a per-user soft action: archive
    only the caller's own ``user_jobs`` row (``status='archived'``), which
    the list/counts endpoints already filter out. The shared ``jobs`` row
    and other users' state are never touched. A true global hard-delete,
    if ever needed, must be gated behind operator/api-key auth — not a
    follower's JWT. Mirrors the per-user write pattern in ``status.py``.
    """
    _assert_user_owns_posting(supabase, posting_id, user_id)

    persistence.upsert_user_job(
        supabase,
        user_id=user_id,
        job_posting_id=posting_id,
        status="archived",
    )

    job_list_cache.invalidate()
    return {"success": True, "deleted_id": posting_id}
