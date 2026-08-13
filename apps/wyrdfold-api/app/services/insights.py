"""Aggregation logic for the insights dashboard (#512).

Most compute functions fetch filtered rows from Supabase and aggregate in
Python. The large per-status pipeline tally (funnel + KPI status
distribution) is pushed into the ``insights_pipeline_status_counts`` GROUP
BY RPC so the ~11k posting + ~11k user_jobs rows never leave Postgres
(#101). The intricate ``status_log`` time-series (applied→interview
response-time pairing, weekly velocity) stays in Python — it runs on ~1 row
at beta scale, so a byte-identical SQL rewrite is risk without payoff (#101).
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

from supabase import AsyncClient

from app.models.analysis import Scorecard
from app.models.insights import (
    CostBucket,
    FunnelStage,
    MissingSkill,
    PipelineInsights,
    PipelinePeriodKpis,
    PurposeCost,
    ScoreBucket,
    ScoreTrendPoint,
    SkillFrequency,
    SkillsCostInsights,
    TargetComparison,
    TargetInsights,
    WeeklyCount,
)
from app.services.analysis.scoring import scorecard_to_numeric
from app.services.supabase_retry import execute_with_retry

logger = logging.getLogger(__name__)

# Supabase .execute().data is typed as list[JSON] (a broad union).
# In practice every row is a dict — this alias makes casts readable.
Row = dict[str, Any]


async def _cost_by_purpose(
    supabase: AsyncClient, user_id: str | None, since: datetime | None
) -> tuple[dict[str, float], dict[str, int]]:
    """Per-purpose ``(total_cost, call_count)`` for the user's ``llm_costs``.

    RPC-first (``cost_by_purpose_since`` does the GROUP BY server-side and
    returns raw sums + counts), falling back to a client-side fetch+group.
    The fallback is also the only path for the ``user_id is None`` "sum every
    row" case, which the per-user RPC deliberately doesn't model. Sums are
    returned RAW; the caller rounds in Python (Postgres vs Python rounding
    differ — see the migration's byte-identity note).
    """
    if user_id is not None:
        try:
            resp = await supabase.rpc(
                "cost_by_purpose_since",
                {
                    "p_user_id": user_id,
                    "p_since": since.isoformat() if since is not None else None,
                },
            ).execute()
            raw = resp.data
            if isinstance(raw, dict):
                totals = {p: float(v["sum"]) for p, v in raw.items()}
                counts = {p: int(v["count"]) for p, v in raw.items()}
                return totals, counts
        except Exception:
            logger.debug("cost_by_purpose_since RPC unavailable, falling back to client-side group")

    cq = supabase.table("llm_costs").select("purpose, cost_usd")
    if since:
        cq = cq.gte("created_at", since.isoformat())
    if user_id is not None:
        cq = cq.eq("user_id", user_id)
    rows = await _rows(cq, label="insights/skills_cost_llm_costs")

    totals_acc: defaultdict[str, float] = defaultdict(float)
    counts_acc: Counter[str] = Counter()
    for cl in rows:
        purpose = cl.get("purpose", "unknown")
        totals_acc[purpose] += float(cl.get("cost_usd") or 0)
        counts_acc[purpose] += 1
    return dict(totals_acc), dict(counts_acc)


async def _rows(query: Any, label: str) -> list[Row]:
    """Run a built supabase query through the transient-retry wrapper and
    return rows as ``list[Row]``.

    The insights endpoints fan out 4–10 supabase calls per request,
    each independent. Without the retry wrap a single
    ``httpx.ReadError`` ([Errno 104] Connection reset by peer — the
    HTTP/2 stream drops we already handle in the poller via
    ``supabase_retry``) returns a 500 from the whole endpoint, which
    crashes the chart on the dashboard. Wrap each query so a transient
    failure is absorbed by one retry instead of bubbling.
    """
    # ``retry_statement_timeout``: the 2026-08-05 drive caught
    # /insights/targets 500ing on a single 57014 while the identical read
    # succeeded 23s later (#604) — one backoff retry absorbs that class.
    resp = await execute_with_retry(
        query.execute, label=label, retry_statement_timeout=True
    )
    return cast(list[Row], resp.data or [])


# Max ids per ``.in_(...)`` filter. PostgREST encodes the list into the
# request URL, so one ``.in_("id", [...])`` with N ids produces roughly
# ``N * 38`` chars (a UUID + its url-escaped comma). A real beta user has
# ~11,300 postings under their targets — a single un-chunked ``.in_()``
# there builds a ~400KB URL that the server truncates/rejects, silently
# dropping rows (a latent correctness bug, #93). 200 keeps each URL well
# under safe limits and matches the existing ``_user_status_map`` chunking.
_IN_CHUNK = 200


async def _fetch_in_chunks(
    make_query: Any,
    ids: list[str],
    label: str,
    chunk: int = _IN_CHUNK,
) -> list[Row]:
    """Run an ``.in_(...)``-bounded query over *ids* in batches of *chunk*
    and concatenate the result rows.

    *make_query* takes one batch of ids and returns the fully-built query
    (it must apply the ``.in_(...)`` for that batch plus every other filter
    — since/until/eq — so each batch carries the same predicate). The union
    of per-batch rows equals what a single ``.in_(all_ids)`` would return;
    callers fold these into order-independent Counters/dicts, so plain
    concatenation preserves output identity.

    *ids* is assumed non-empty (callers guard the empty case, since an
    empty ``.in_([])`` must NOT relax to an unbounded SELECT).
    """
    out: list[Row] = []
    for i in range(0, len(ids), chunk):
        batch = ids[i : i + chunk]
        out.extend(await _rows(make_query(batch), label=label))
    return out


# Funnel stage ordering (used for consistent display)
FUNNEL_ORDER = [
    "new",
    "saved",
    "resume_draft",
    "resume_ready",
    "applied",
    "interviewing",
    "offer",
]

APPLIED_STATUSES = {"applied", "interviewing", "offer"}


def _iso_week_start(dt: datetime | date) -> date:
    """Return the Monday of the ISO week containing *dt*."""
    if isinstance(dt, datetime):
        d = dt.date()
    else:
        d = dt
    return d - timedelta(days=d.weekday())


def _parse_dt(value: str) -> datetime:
    """Parse an ISO timestamp from Supabase (may include tz offset)."""
    return datetime.fromisoformat(value)


# ── Target membership ────────────────────────────────────────────────────────


# PostgREST caps every read at ``max_rows`` (1000 on this project — see
# ``supabase/config.toml``). ``_posting_target_map`` reads the whole (job,
# target) score set for a user's targets, which is unbounded and routinely
# exceeds that (a real beta user has ~12.5k non-excluded rows on a single busy
# target), so a single un-paginated read SILENTLY TRUNCATES the membership map
# to the first 1000 rows — the exact latent-correctness class this module
# already guards for ``.in_()`` chunking (see ``_IN_CHUNK``). Keyset-paginate
# by the ``scores`` PK so the full set is read, in pages the server returns
# whole (page size == max_rows).
_SCORES_PAGE_SIZE = 1000

# Wall-clock budget for the fallback-only scores-membership walk (#635):
# many small statements evade the per-statement timeout, so the LOOP needs
# its own ceiling — a degraded DB must fail fast and visibly, never hold a
# connection for the 25 minutes observed in #634 F1.
_MEMBERSHIP_BUDGET_S = 30


async def _posting_target_map(
    supabase: AsyncClient, target_ids: set[str] | None
) -> dict[str, set[str]] | None:
    """Return ``{job_posting_id → {target_id, ...}}`` for the user's targets.

    Returns ``None`` when ``target_ids`` is ``None`` (caller wants the
    unscoped global view). Returns an empty dict when the user has no
    targets — callers must early-return on an empty membership.

    The whole insights module previously filtered by ``jobs.target_id``
    (and ``documents.target_id``). ``jobs.target_id`` is a vestigial
    pre-shared-targets column the poller never populates — same root
    cause as the bugs fixed in #676 / #678 — so every per-target
    aggregation silently returned zero rows. ``documents.target_id``
    doesn't exist at all (the table was renamed from
    ``tailored_resumes`` without ever adding that column), which is
    why the pipeline + skills-cost endpoints currently 500. Pivot all
    target scoping through the ``scores`` table, which is the actual
    source of truth.

    The read is keyset-paginated by the ``scores`` PK because the result set
    is unbounded and would otherwise truncate at PostgREST's ``max_rows``
    (see ``_SCORES_PAGE_SIZE``) — losing membership rows silently.
    """
    if target_ids is None:
        return None
    if not target_ids:
        return {}
    target_list = list(target_ids)
    out: dict[str, set[str]] = defaultdict(set)
    after: str | None = None
    while True:
        query = (
            supabase.table("scores")
            .select("id, job_posting_id, target_id")
            .in_("target_id", target_list)
            .eq("excluded", False)
            .order("id")
            .limit(_SCORES_PAGE_SIZE)
        )
        if after is not None:
            query = query.gt("id", after)
        page = await _rows(query, label="insights/posting_target_map")
        if not page:
            break
        for r in page:
            out[r["job_posting_id"]].add(r["target_id"])
        if len(page) < _SCORES_PAGE_SIZE:
            break
        after = page[-1]["id"]
    return dict(out)


def _flatten_posting_ids(
    membership: dict[str, set[str]] | None,
) -> set[str] | None:
    """Flatten ``_posting_target_map`` into a posting-id set.

    Returns ``None`` for the unscoped global view, propagating that
    "no filter" signal to consumers."""
    if membership is None:
        return None
    return set(membership.keys())


# ── Pipeline ─────────────────────────────────────────────────────────────────


async def _user_status_map(
    supabase: AsyncClient,
    user_id: str | None,
    posting_ids: list[str],
) -> dict[str, str]:
    """Per-user pipeline status (#75 C4): ``jobs.status`` was dropped, so
    pipeline state now lives in ``user_jobs`` keyed by ``(user_id,
    job_posting_id)``. Return a ``posting_id -> status`` map; postings the
    user never touched are absent and read as ``'new'`` by the callers
    (the "absent = new" rule). Empty when there's no user identity."""
    if user_id is None or not posting_ids:
        return {}
    out: dict[str, str] = {}
    for i in range(0, len(posting_ids), 200):
        chunk = posting_ids[i : i + 200]
        rows = await _rows(
            supabase.table("user_jobs")
            .select("job_posting_id, status")
            .eq("user_id", user_id)
            .in_("job_posting_id", chunk),
            label="insights/user_jobs_status",
        )
        for r in rows:
            out[str(r["job_posting_id"])] = cast(str, r["status"])
    return out


async def _pipeline_status_counts_python(
    supabase: AsyncClient,
    since: datetime | None,
    until: datetime | None,
    target_ids: set[str] | None,
    user_id: str | None,
) -> Counter[str]:
    """Fallback for ``_pipeline_status_counts`` when the RPC is unavailable
    (e.g. mid-deploy before the migration lands).

    Reproduces exactly what the old ``_fetch_postings_window`` + Python status
    Counter did: postings under the caller's targets (``excluded = false``)
    within the window, deduplicated by posting id, grouped by the caller's
    per-user status (``user_jobs`` row; absent → ``'new'``)."""
    # Budgeted like the targets fallback (#635): the membership walk is many
    # small statements — a degraded DB must fail fast, not hold the line.
    async with asyncio.timeout(_MEMBERSHIP_BUDGET_S):
        posting_ids = _flatten_posting_ids(await _posting_target_map(supabase, target_ids))
    if posting_ids is not None and not posting_ids:
        return Counter()

    def _base() -> Any:
        q = supabase.table("jobs").select("id, cataloged_at")
        if since:
            q = q.gte("cataloged_at", since.isoformat())
        if until:
            q = q.lt("cataloged_at", until.isoformat())
        return q

    if posting_ids is not None:
        # Chunk the id filter so the PostgREST URL stays under safe limits
        # at multi-thousand-posting scale (#93).
        rows = await _fetch_in_chunks(
            lambda batch: _base().in_("id", batch),
            list(posting_ids),
            label="insights/pipeline_status_counts_fallback",
        )
    else:
        rows = await _rows(_base(), label="insights/pipeline_status_counts_fallback")
    status_map = await _user_status_map(supabase, user_id, [str(r["id"]) for r in rows])
    counts: Counter[str] = Counter()
    for r in rows:
        counts[status_map.get(str(r["id"]), "new")] += 1
    return counts


async def _pipeline_status_counts(
    supabase: AsyncClient,
    since: datetime | None,
    until: datetime | None,
    target_ids: set[str] | None,
    user_id: str | None,
) -> Counter[str]:
    """Per-(per-user-)status counts of the caller's postings in the window,
    computed by the ``insights_pipeline_status_counts`` GROUP BY RPC so the
    ~11k posting + ~11k user_jobs rows never leave Postgres (#101). Feeds BOTH
    the funnel and the ``_kpis_from`` status distribution.

    The RPC needs a bounded target set; the unscoped global path
    (``target_ids is None``) — admin/tests only, the router always passes the
    caller's targets — keeps the client-side fallback. Falls back to the
    client-side count if the RPC isn't deployed yet (mirrors
    ``_pipeline_counts_grouped`` in routers/jobs.py)."""
    if target_ids is None:
        return await _pipeline_status_counts_python(supabase, since, until, target_ids, user_id)
    if not target_ids:
        return Counter()
    try:
        resp = await execute_with_retry(
            supabase.rpc(
                "insights_pipeline_status_counts",
                {
                    "p_target_ids": sorted(target_ids),
                    "p_since": since.isoformat() if since else None,
                    "p_until": until.isoformat() if until else None,
                    "p_user_id": user_id,
                },
            ).execute,
            label="insights/pipeline_status_counts",
        )
    except Exception:
        return await _pipeline_status_counts_python(supabase, since, until, target_ids, user_id)
    return Counter(
        {cast(str, row["status"]): int(row["count"]) for row in cast(list[Row], resp.data or [])}
    )


async def _fetch_status_logs_window(
    supabase: AsyncClient,
    since: datetime | None,
    until: datetime | None,
    posting_ids: set[str] | None,
    user_id: str | None,
) -> list[Row]:
    if posting_ids is not None and not posting_ids:
        return []

    def _base() -> Any:
        sq = supabase.table("status_log").select("posting_id, old_status, new_status, created_at")
        # Per-user scoping (#113): a posting in the shared catalog can sit
        # under several users' targets, so the audit log must be filtered to
        # the caller's own transitions — otherwise one user's response-time
        # KPIs absorb another's. user_id is None only on the admin/global
        # rollup (the "sum every user" path), which intentionally skips it.
        if user_id is not None:
            sq = sq.eq("user_id", user_id)
        if since:
            sq = sq.gte("created_at", since.isoformat())
        if until:
            sq = sq.lt("created_at", until.isoformat())
        return sq

    if posting_ids is not None:
        # Chunk the id filter so the PostgREST URL stays under safe limits
        # at multi-thousand-posting scale (#93).
        return await _fetch_in_chunks(
            lambda batch: _base().in_("posting_id", batch),
            list(posting_ids),
            label="insights/status_logs_window",
        )
    return await _rows(_base(), label="insights/status_logs_window")


def _kpis_from(status_counts: Counter[str], status_logs: list[Row]) -> PipelinePeriodKpis:
    """Pure aggregation: derive the 5 top-line KPIs from the per-status counts
    (computed server-side by ``_pipeline_status_counts``, #101) + the
    already-fetched status logs for one window. Used for both the current and
    prior periods so the math stays in one place. The applied→interview
    response-time pairing stays in Python — it runs on ``status_log``, which
    is ~1 row at beta scale (#101 deferral)."""
    total_applications = sum(status_counts.get(s, 0) for s in APPLIED_STATUSES)
    total_interviews = status_counts.get("interviewing", 0) + status_counts.get("offer", 0)
    total_offers = status_counts.get("offer", 0)
    response_rate = (total_interviews / total_applications) if total_applications > 0 else None

    applied_times: dict[str, datetime] = {}
    interview_times: dict[str, datetime] = {}
    for log in status_logs:
        posting_id = log["posting_id"]
        ts = _parse_dt(log["created_at"])
        if log["new_status"] == "applied" and posting_id not in applied_times:
            applied_times[posting_id] = ts
        if log["new_status"] == "interviewing" and posting_id not in interview_times:
            interview_times[posting_id] = ts

    response_days: list[float] = []
    for pid, interview_ts in interview_times.items():
        if pid in applied_times:
            delta = (interview_ts - applied_times[pid]).total_seconds() / 86400
            if delta >= 0:
                response_days.append(delta)
    avg_days = (sum(response_days) / len(response_days)) if response_days else None

    return PipelinePeriodKpis(
        total_applications=total_applications,
        total_interviews=total_interviews,
        total_offers=total_offers,
        response_rate=round(response_rate, 3) if response_rate is not None else None,
        avg_days_to_response=round(avg_days, 1) if avg_days is not None else None,
    )


async def compute_pipeline(
    supabase: AsyncClient,
    since: datetime | None,
    prior_window: tuple[datetime, datetime] | None = None,
    target_ids: set[str] | None = None,
    user_id: str | None = None,
) -> PipelineInsights:
    """Compute pipeline insights for the current window. When *prior_window*
    is supplied as ``(prior_since, prior_until)``, also compute the same KPIs
    over that window so the dashboard can render trend deltas. Velocity and
    funnel are scoped to the current window only.

    When *target_ids* is supplied, all queries are bounded to the user's
    target set — required for multi-tenant safety since wyrdfold-api uses
    service-role and bypasses RLS. *user_id* resolves the per-user pipeline
    status from ``user_jobs`` (#75 C4: ``jobs.status`` was dropped).
    """
    # Per-status funnel/KPI tallies come from the server-side GROUP BY RPC
    # (#101) — the ~11k posting + ~11k user_jobs rows never leave Postgres.
    # The windowed posting-id SET is still resolved client-side to bound the
    # velocity / response-time follow-ups (status_log + resume documents),
    # whose time-series logic stays in Python (status_log is ~1 row at beta
    # scale — moving it is risk without payoff, #101).
    # #260-perf: scope the velocity / response-time follow-ups (status_log +
    # resume documents) by ``user_id`` directly — both tables carry it — instead
    # of resolving the user's window posting-id SET client-side and chunking over
    # it. The old `_fetch_window_posting_ids` pulled ~tens of thousands of score
    # rows to the API to bound two tiny queries (status_log is ~a handful of rows
    # per user; documents likewise), which dominated /insights at scale.
    status_logs = await _fetch_status_logs_window(supabase, since, None, None, user_id)
    status_counts = await _pipeline_status_counts(supabase, since, None, target_ids, user_id)

    # Tailored-resume velocity — scope by ``documents.user_id`` (the user's own
    # resumes are exactly the set the weekly velocity counts). Was a fan-out over
    # the window posting-id set.
    def _resume_base() -> Any:
        rq = supabase.table("documents").select("job_posting_id, created_at")
        if since:
            rq = rq.gte("created_at", since.isoformat())
        return rq.eq("document_type", "resume")

    resumes: list[Row]
    if user_id is not None:
        resumes = await _rows(
            _resume_base().eq("user_id", user_id), label="insights/pipeline_resumes"
        )
    else:
        resumes = await _rows(_resume_base(), label="insights/pipeline_resumes")

    # --- Funnel counts (from the GROUP BY RPC, #101) ---
    funnel = [FunnelStage(stage=s, count=status_counts.get(s, 0)) for s in FUNNEL_ORDER]

    # --- Top-line KPIs (current window) ---
    current = _kpis_from(status_counts, status_logs)

    # --- Prior-period KPIs (only when caller asked for a comparison) ---
    previous: PipelinePeriodKpis | None = None
    if prior_window is not None:
        prior_since, prior_until = prior_window
        prior_logs = await _fetch_status_logs_window(
            supabase, prior_since, prior_until, None, user_id
        )
        prior_counts = await _pipeline_status_counts(
            supabase, prior_since, prior_until, target_ids, user_id
        )
        previous = _kpis_from(prior_counts, prior_logs)

    # --- Weekly velocity ---
    week_resumes: Counter[date] = Counter()
    for r in resumes:
        week_resumes[_iso_week_start(_parse_dt(r["created_at"]))] += 1

    week_apps: Counter[date] = Counter()
    for log in status_logs:
        if log["new_status"] in APPLIED_STATUSES and log["old_status"] not in APPLIED_STATUSES:
            week_apps[_iso_week_start(_parse_dt(log["created_at"]))] += 1

    # Contiguous weeks from the first activity through TODAY's week — not
    # just weeks that have data. Plotting only active weeks silently dropped
    # trailing quiet weeks, so the chart's x-axis ended at the last burst of
    # activity and recent silence was invisible (ux-sweep 2026-08-12 §B7:
    # a 30d view whose axis stopped 10 days before today).
    data_weeks = set(week_resumes.keys()) | set(week_apps.keys())
    velocity: list[WeeklyCount] = []
    if data_weeks:
        week = min(data_weeks)
        last = max(_iso_week_start(datetime.now(UTC).date()), max(data_weeks))
        while week <= last:
            velocity.append(
                WeeklyCount(
                    week_start=week,
                    resumes_generated=week_resumes.get(week, 0),
                    applications_submitted=week_apps.get(week, 0),
                )
            )
            week += timedelta(days=7)

    return PipelineInsights(
        total_applications=current.total_applications,
        total_interviews=current.total_interviews,
        total_offers=current.total_offers,
        response_rate=current.response_rate,
        avg_days_to_response=current.avg_days_to_response,
        velocity=velocity,
        funnel=funnel,
        previous=previous,
    )


# ── Targets ──────────────────────────────────────────────────────────────────


# Raw (un-rounded) per-target aggregates from the GROUP BY pass. Averages and
# conversion rates are deliberately NOT rolled up here — they're rounded in
# Python (see _assemble_target_insights) so Postgres' half-away-from-zero
# round() can never diverge from Python's banker's round().
#   per_target: target_id -> (job_count, score_sum, score_n, applied, interview)
#   distribution: bucket_idx (0..9) -> count   (nonzero scores only)
#   trend: list of (week_start, score_sum, score_n)  best-per-posting scores
#   unscored: postings under the caller's targets that never saw a nonzero score
_TargetAgg = tuple[int, int, int, int, int]


class _TargetGroupBy:
    __slots__ = ("distribution", "per_target", "trend", "unscored")

    def __init__(
        self,
        per_target: dict[str, _TargetAgg],
        distribution: dict[int, int],
        trend: list[tuple[date, int, int]],
        unscored: int,
    ) -> None:
        self.per_target = per_target
        self.distribution = distribution
        self.trend = trend
        self.unscored = unscored


async def _targets_groupby_python(
    supabase: AsyncClient,
    since: datetime | None,
    target_ids: set[str],
    target_labels: dict[str, str],
    membership: dict[str, set[str]],
    posting_ids: set[str],
    user_id: str | None,
) -> _TargetGroupBy:
    """Client-side fallback for ``_targets_groupby`` (mid-deploy, before the
    ``insights_targets_groupby`` migration lands — mirrors
    ``_pipeline_status_counts_python``).

    Reproduces the OLD scoped fetch+aggregate exactly: postings under the
    caller's targets within the window, with per-target scores from the
    ``scores`` table and the per-user status overlaid from ``user_jobs``. It
    returns the SAME raw aggregates the RPC returns so both feed
    ``_assemble_target_insights`` — guaranteeing the RPC and fallback produce
    byte-identical output (the rounding all happens downstream, in Python)."""

    # Postings within the window, hydrated with cataloged_at + per-user status.
    def _postings_base() -> Any:
        q = supabase.table("jobs").select("id, cataloged_at")
        if since:
            q = q.gte("cataloged_at", since.isoformat())
        return q

    if posting_ids:
        # Chunk the id filter so the PostgREST URL stays under safe limits
        # at multi-thousand-posting scale (#93).
        postings = await _fetch_in_chunks(
            lambda batch: _postings_base().in_("id", batch),
            list(posting_ids),
            label="insights/targets_postings",
        )
    else:
        postings = []
    status_map = await _user_status_map(supabase, user_id, [str(p["id"]) for p in postings])
    for p in postings:
        p["status"] = status_map.get(str(p["id"]), "new")

    # Per-target score lookup: ``(posting_id, target_id) → score``.
    score_lookup: dict[tuple[str, str], int] = {}
    if posting_ids:

        def _scores_base() -> Any:
            return (
                supabase.table("scores")
                .select("job_posting_id, target_id, score")
                .eq("excluded", False)
                .in_("target_id", list(target_ids))
            )

        # Chunk the job_posting_id filter so the PostgREST URL stays under
        # safe limits at multi-thousand-posting scale (#93).
        score_rows = await _fetch_in_chunks(
            lambda batch: _scores_base().in_("job_posting_id", batch),
            list(posting_ids),
            label="insights/targets_scores",
        )
        for r in score_rows:
            score_lookup[(r["job_posting_id"], r["target_id"])] = int(r.get("score") or 0)

    # --- Per-target aggregation (target_labels-filtered) ---
    target_sum: defaultdict[str, int] = defaultdict(int)
    target_n: Counter[str] = Counter()
    target_applied: Counter[str] = Counter()
    target_interview: Counter[str] = Counter()
    distribution: Counter[int] = Counter()
    unscored = 0
    for p in postings:
        pid = str(p["id"])
        status = p["status"]
        tids = membership.get(pid, set())
        seen_any_score = False
        for tid in tids:
            if tid not in target_labels:
                continue
            score = score_lookup.get((pid, tid), 0)
            target_n[tid] += 1
            target_sum[tid] += score
            if status in APPLIED_STATUSES:
                target_applied[tid] += 1
            if status in {"interviewing", "offer"}:
                target_interview[tid] += 1
            if score:
                seen_any_score = True
                clamped = max(0, min(score, 100))
                distribution[min(clamped // 10, 9)] += 1
        if not seen_any_score:
            unscored += 1

    per_target: dict[str, _TargetAgg] = {
        tid: (
            target_n[tid],
            target_sum[tid],
            target_n[tid],
            target_applied[tid],
            target_interview[tid],
        )
        for tid in target_n
    }

    # --- Score trend (raw membership, best per posting) ---
    week_sum: defaultdict[date, int] = defaultdict(int)
    week_n: Counter[date] = Counter()
    for p in postings:
        pid = str(p["id"])
        tids = membership.get(pid, set())
        best = max((score_lookup.get((pid, t), 0) for t in tids), default=0)
        if best <= 0:
            continue
        w = _iso_week_start(_parse_dt(p["cataloged_at"]))
        week_sum[w] += best
        week_n[w] += 1
    trend = [(w, week_sum[w], week_n[w]) for w in week_sum]

    return _TargetGroupBy(per_target, dict(distribution), trend, unscored)


async def _targets_groupby(
    supabase: AsyncClient,
    since: datetime | None,
    target_ids: set[str],
    target_labels: dict[str, str],
    user_id: str | None,
) -> _TargetGroupBy:
    """Per-target metrics + score distribution + score trend + unscored count
    for the caller's targets in the window, computed by the
    ``insights_targets_groupby`` GROUP BY RPC so the ~11k posting / ~11k
    user_jobs / ~11k×2 scores rows never leave Postgres (#101).

    Returns RAW aggregates (counts + SUM/COUNT, no rounding) — the byte-
    identity contract: Postgres ``round()`` is half-away-from-zero while
    Python ``round()`` is banker's, so all rounding stays in
    ``_assemble_target_insights``. Falls back to the client-side aggregate
    when the RPC isn't deployed yet (mirrors ``_pipeline_status_counts``).

    The scores-membership pre-pass (``_posting_target_map``) lives INSIDE
    the fallback branch, budgeted (#635): it keyset-walks every scores row
    under the user's targets in ~1k pages, and computing it eagerly for a
    result only the fallback consumes is what let one /insights/targets
    call issue 25 MINUTES of sequential statements under DB contention
    (2026-08-06 stress sweep, #634 F1) — each page under the statement
    timeout, the sum unbounded, the loop feeding the very contention that
    slowed it. The happy path is now labels + one RPC statement, which the
    statement timeout genuinely governs."""
    try:
        resp = await execute_with_retry(
            supabase.rpc(
                "insights_targets_groupby",
                {
                    "p_target_ids": sorted(target_ids),
                    "p_since": since.isoformat() if since else None,
                    "p_user_id": user_id,
                },
            ).execute,
            label="insights/targets_groupby",
        )
    except Exception:
        # Wall-clock budget on the fallback's membership walk: a degraded DB
        # must produce a fast, visible failure (the FE's load-error state),
        # never a half-hour connection hostage.
        async with asyncio.timeout(_MEMBERSHIP_BUDGET_S):
            membership = await _posting_target_map(supabase, target_ids) or {}
        return await _targets_groupby_python(
            supabase,
            since,
            target_ids,
            target_labels,
            membership,
            set(membership.keys()),
            user_id,
        )

    payload = cast(dict[str, Any], resp.data or {})
    per_target: dict[str, _TargetAgg] = {}
    for row in cast(list[Row], payload.get("targets") or []):
        per_target[str(row["target_id"])] = (
            int(row["job_count"]),
            int(row["score_sum"]),
            int(row["score_n"]),
            int(row["applied_count"]),
            int(row["interview_count"]),
        )
    distribution: dict[int, int] = {
        int(row["bucket_idx"]): int(row["count"])
        for row in cast(list[Row], payload.get("distribution") or [])
    }
    trend: list[tuple[date, int, int]] = [
        (
            date.fromisoformat(cast(str, row["week_start"])),
            int(row["score_sum"]),
            int(row["score_n"]),
        )
        for row in cast(list[Row], payload.get("trend") or [])
    ]
    unscored = int(payload.get("unscored") or 0)
    return _TargetGroupBy(per_target, distribution, trend, unscored)


def _assemble_target_insights(target_labels: dict[str, str], agg: _TargetGroupBy) -> TargetInsights:
    """Build the ``TargetInsights`` response from the raw GROUP BY aggregates.

    This is the ONLY place rounding happens — keeping it in Python (banker's
    round) preserves byte-identity with the pre-#101 implementation regardless
    of whether *agg* came from the RPC or the client-side fallback. The
    comparison list is emitted in ``target_labels`` fetch order (skipping
    targets with no rows in the window), exactly like the old
    ``for tid, label in target_labels.items()`` loop."""
    comparisons: list[TargetComparison] = []
    for tid, label in target_labels.items():
        row = agg.per_target.get(tid)
        if row is None:
            continue
        job_count, score_sum, score_n, applied, interviews = row
        comparisons.append(
            TargetComparison(
                target_id=tid,
                target_label=label,
                job_count=job_count,
                avg_score=round(score_sum / score_n, 1),
                applied_count=applied,
                interview_count=interviews,
                conversion_rate=round(interviews / applied, 3) if applied > 0 else None,
            )
        )

    buckets = [
        ScoreBucket(
            bucket=f"{lo}-{lo + 10 if lo < 90 else 100}",
            count=agg.distribution.get(lo // 10, 0),
        )
        for lo in range(0, 100, 10)
    ]

    score_trend = sorted(
        [ScoreTrendPoint(week_start=w, avg_score=round(s / n, 1)) for w, s, n in agg.trend],
        key=lambda x: x.week_start,
    )

    return TargetInsights(
        targets=comparisons,
        score_distribution=buckets,
        score_trend=score_trend,
        unscored_count=agg.unscored,
    )


async def compute_targets(
    supabase: AsyncClient,
    since: datetime | None,
    target_ids: set[str] | None = None,
    user_id: str | None = None,
) -> TargetInsights:
    """Compute target insights. When *target_ids* is supplied, only those
    targets and their postings are aggregated. *user_id* resolves the
    per-user pipeline status from ``user_jobs`` (#75 C4: ``jobs.status`` was
    dropped) for the applied/interview comparisons."""
    # Fetch only the user's targets for labels
    tq = supabase.table("targets").select("id, label")
    if target_ids is not None:
        tq = tq.in_("id", list(target_ids))
    targets_data = await _rows(tq, label="insights/targets_labels")
    target_labels = {t["id"]: t["label"] for t in targets_data}

    # --- Scoped path (the only path the /insights/targets router takes) ---
    # The per-target metrics + score distribution + score trend + unscored
    # count are computed in one server-side GROUP BY pass (#101) — the ~11k
    # posting / ~11k user_jobs / two ~11k scores reads never leave Postgres.
    # The RPC returns RAW aggregates; all rounding stays in Python (banker's
    # vs Postgres half-away-from-zero) so the output is byte-identical.
    #
    # #635: no membership pre-pass here. The old code walked EVERY scores
    # row under the user's targets (keyset pages of 1k) before this branch,
    # purely to feed the RPC-unavailable fallback and an empty-set early
    # return the RPC reproduces anyway (empty aggregates assemble to the
    # identical zero-bucket shape). Under DB contention that walk ran 25
    # minutes (#634 F1). Membership now resolves lazily inside
    # ``_targets_groupby``'s fallback branch, budgeted.
    if target_ids is not None:
        agg = await _targets_groupby(supabase, since, target_ids, target_labels, user_id)
        return _assemble_target_insights(target_labels, agg)

    # --- Global / admin path (target_ids is None — tests only) ---
    # The legacy inline ``jobs.target_id`` / ``jobs.score`` columns this path
    # once read were DROPPED in R2 (they were vestigial/NULL in production).
    # The path now degrades coherently: every posting counts as unscored and
    # the per-target comparison stays empty — it remains only so the api-key
    # global view returns a well-formed (if hollow) shape instead of a 400.
    def _postings_base() -> Any:
        q = supabase.table("jobs").select("id, cataloged_at")
        if since:
            q = q.gte("cataloged_at", since.isoformat())
        return q

    postings = await _rows(_postings_base(), label="insights/targets_postings")
    status_map = await _user_status_map(supabase, user_id, [str(p["id"]) for p in postings])
    for p in postings:
        p["status"] = status_map.get(str(p["id"]), "new")

    # --- Per-target aggregation ---
    # Targets with no jobs in the window are dropped from the response
    # (they're noise in the comparison chart). Postings with no signal
    # — null OR zero score — are tracked separately as unscored_count
    # so they don't bloat the 0-10 bucket of the distribution. (A
    # legitimate score of 0 is vanishingly rare; in practice 0 means
    # "default value, never scored".)
    target_jobs: defaultdict[str, list[tuple[Row, int]]] = defaultdict(list)
    scored_values: list[int] = []
    unscored_count = 0
    for p in postings:
        # Admin / global path — read ``target_id`` and ``score`` straight off
        # the posting row.
        inline_score = p.get("score")
        if inline_score is None or inline_score == 0:
            unscored_count += 1
        else:
            scored_values.append(int(inline_score))
        inline_tid = p.get("target_id")
        if inline_tid and inline_tid in target_labels:
            target_jobs[inline_tid].append((p, int(inline_score or 0)))

    comparisons: list[TargetComparison] = []
    for tid, label in target_labels.items():
        rows = target_jobs.get(tid, [])
        if not rows:
            continue
        scores = [s for _, s in rows]
        statuses = [r["status"] for r, _ in rows]
        applied = sum(1 for s in statuses if s in APPLIED_STATUSES)
        interviews = sum(1 for s in statuses if s in {"interviewing", "offer"})
        comparisons.append(
            TargetComparison(
                target_id=tid,
                target_label=label,
                job_count=len(rows),
                avg_score=round(sum(scores) / len(scores), 1),
                applied_count=applied,
                interview_count=interviews,
                conversion_rate=round(interviews / applied, 3) if applied > 0 else None,
            )
        )

    # --- Score distribution (excluding unscored postings) ---
    bucket_counts: Counter[str] = Counter()
    for s in scored_values:
        clamped = max(0, min(s, 100))
        bucket_idx = min(clamped // 10, 9)
        lo = bucket_idx * 10
        hi = lo + 10 if lo < 90 else 100
        bucket_counts[f"{lo}-{hi}"] += 1

    buckets = [
        ScoreBucket(
            bucket=f"{lo}-{lo + 10 if lo < 90 else 100}",
            count=bucket_counts.get(f"{lo}-{lo + 10 if lo < 90 else 100}", 0),
        )
        for lo in range(0, 100, 10)
    ]

    # --- Score trend by week (scored postings only) ---
    # Global path: fall back to the inline ``jobs.score`` column.
    week_scores: defaultdict[date, list[int]] = defaultdict(list)
    for p in postings:
        inline = p.get("score")
        if inline is None:
            continue
        week_scores[_iso_week_start(_parse_dt(p["cataloged_at"]))].append(int(inline))

    score_trend = sorted(
        [
            ScoreTrendPoint(week_start=w, avg_score=round(sum(ss) / len(ss), 1))
            for w, ss in week_scores.items()
        ],
        key=lambda x: x.week_start,
    )

    return TargetInsights(
        targets=comparisons,
        score_distribution=buckets,
        score_trend=score_trend,
        unscored_count=unscored_count,
    )


# ── Skills + Cost ────────────────────────────────────────────────────────────


async def compute_skills_cost(
    supabase: AsyncClient,
    since: datetime | None,
    user_id: str | None = None,
    target_ids: set[str] | None = None,
) -> SkillsCostInsights:
    """Compute skills + cost insights.

    *target_ids* scopes job-posting / analyses / documents queries to the
    user's targets. *user_id* scopes ``llm_costs`` which has its own
    direct ``user_id`` column.
    """

    # Skills come ONLY from ``analyses`` (scorecards), and ``analyses.target_id``
    # is populated for all recent rows — so scope analyses DIRECTLY by target
    # instead of first resolving the target's posting ids via ``scores`` (tens of
    # thousands at scale) and fetching every one of their ``jobs`` rows. That
    # membership pull (~49k rows) + per-posting fetch (~17.5k rows over ~88
    # chunked round-trips) was the /insights bottleneck (9-41s) for a handful of
    # analyses. #60-perf
    def _analyses_base() -> Any:
        aq = supabase.table("analyses").select("job_posting_id, scorecard, created_at")
        if since:
            aq = aq.gte("created_at", since.isoformat())
        return aq

    analyses: list[Row]
    if target_ids is not None:
        analyses = await _rows(
            _analyses_base().in_("target_id", list(target_ids)),
            label="insights/skills_cost_analyses",
        )
    else:
        analyses = await _rows(_analyses_base(), label="insights/skills_cost_analyses")

    # The analysis's own scorecard weights the missing-skill priority —
    # derived in-hand from rows we already fetched. (Until R2 this read the
    # legacy ``jobs.llm_score`` copy, which was stale-or-NULL on all but a
    # few hundred rows; the scorecard is the same signal at the source.)
    posting_scores: dict[str, float] = {}
    for a in analyses:
        pid = a.get("job_posting_id")
        card = a.get("scorecard")
        if pid and isinstance(card, dict):
            try:
                posting_scores[str(pid)] = float(
                    scorecard_to_numeric(Scorecard.model_validate(card))
                )
            except Exception:
                # Malformed legacy scorecard — log once at debug and skip
                # its weight; the skill still counts, just unweighted.
                logger.debug("insights: unparseable scorecard for posting %s", pid)

    # Per-purpose LLM spend — aggregated server-side via the
    # cost_by_purpose_since RPC (#105), client-side fallback inside the helper.
    purpose_totals, purpose_counts = await _cost_by_purpose(supabase, user_id, since)

    # Tailored-resume costs scope directly by ``documents.user_id`` — documents
    # has no target_id, and the user's own resumes are exactly the set we want
    # (the endpoint unions all of the user's targets). Was a fan-out over every
    # target posting id.
    def _resume_base() -> Any:
        rq = supabase.table("documents").select("cost_usd, created_at")
        if since:
            rq = rq.gte("created_at", since.isoformat())
        return rq.eq("document_type", "resume")

    resume_costs: list[Row]
    if user_id is not None:
        resume_costs = await _rows(
            _resume_base().eq("user_id", user_id),
            label="insights/skills_cost_resumes",
        )
    else:
        resume_costs = await _rows(_resume_base(), label="insights/skills_cost_resumes")

    # --- Skill frequencies ---
    matched_counts: Counter[str] = Counter()
    missing_counts: Counter[str] = Counter()
    missing_score_sum: defaultdict[str, float] = defaultdict(float)
    missing_score_count: Counter[str] = Counter()

    for a in analyses:
        scorecard = a.get("scorecard")
        if not isinstance(scorecard, dict):
            continue
        posting_id = a.get("job_posting_id")
        score = posting_scores.get(str(posting_id)) if posting_id else None

        for sm in scorecard.get("skills_matched", []):
            if isinstance(sm, dict) and sm.get("name"):
                if sm.get("matched"):
                    matched_counts[sm["name"]] += 1
                else:
                    missing_counts[sm["name"]] += 1
                    if score is not None:
                        missing_score_sum[sm["name"]] += score
                        missing_score_count[sm["name"]] += 1
        for skill_name in scorecard.get("skills_missing", []):
            if isinstance(skill_name, str):
                missing_counts[skill_name] += 1
                if score is not None:
                    missing_score_sum[skill_name] += score
                    missing_score_count[skill_name] += 1

    # Combine and rank by total frequency
    all_skills = set(matched_counts.keys()) | set(missing_counts.keys())
    skill_freqs = sorted(
        [
            SkillFrequency(
                skill=s,
                matched_count=matched_counts.get(s, 0),
                missing_count=missing_counts.get(s, 0),
            )
            for s in all_skills
        ],
        key=lambda x: x.matched_count + x.missing_count,
        reverse=True,
    )[:15]

    # Top missing (skills never matched), ranked by score-weighted priority.
    # priority_score = sum(llm_score for jobs missing this skill); skills
    # missing in many high-scoring jobs rank highest. When no job has a
    # score, fall back to raw missing_count so ranking stays stable.
    # Limitation: skills are still treated as binary present/absent per
    # analysis — a skill matched in some jobs but missing in others is
    # excluded entirely.
    pure_missing_skills = [s for s in missing_counts if matched_counts.get(s, 0) == 0]
    missing_records = []
    for skill in pure_missing_skills:
        count = missing_counts[skill]
        score_count = missing_score_count[skill]
        score_sum = missing_score_sum[skill]
        avg = (score_sum / score_count) if score_count > 0 else None
        priority = score_sum if score_count > 0 else float(count)
        missing_records.append(
            MissingSkill(
                skill=skill,
                missing_count=count,
                avg_job_score=avg,
                priority_score=round(priority, 4),
            )
        )
    pure_missing = sorted(
        missing_records,
        key=lambda r: r.priority_score,
        reverse=True,
    )[:10]

    # --- Cost over time ---
    week_cost: defaultdict[date, float] = defaultdict(float)
    week_resume_count: Counter[date] = Counter()
    for rc in resume_costs:
        w = _iso_week_start(_parse_dt(rc["created_at"]))
        week_cost[w] += float(rc.get("cost_usd") or 0)
        week_resume_count[w] += 1

    all_cost_weeks = sorted(set(week_cost.keys()) | set(week_resume_count.keys()))
    cost_over_time = [
        CostBucket(
            week_start=w,
            total_cost=round(week_cost.get(w, 0), 4),
            resume_count=week_resume_count.get(w, 0),
        )
        for w in all_cost_weeks
    ]

    # --- Cost by purpose ---
    cost_by_purpose = sorted(
        [
            PurposeCost(
                purpose=p,
                total_cost=round(purpose_totals[p], 4),
                call_count=purpose_counts[p],
            )
            for p in purpose_totals
        ],
        key=lambda x: x.total_cost,
        reverse=True,
    )

    # --- Totals ---
    total_cost = sum(purpose_totals.values())
    total_resumes = len(resume_costs)
    avg_cost_per_resume = (total_cost / total_resumes) if total_resumes > 0 else None

    return SkillsCostInsights(
        top_skills=skill_freqs,
        top_missing=pure_missing,
        cost_over_time=cost_over_time,
        cost_by_purpose=cost_by_purpose,
        total_cost=round(total_cost, 4),
        avg_cost_per_resume=(
            round(avg_cost_per_resume, 4) if avg_cost_per_resume is not None else None
        ),
    )
