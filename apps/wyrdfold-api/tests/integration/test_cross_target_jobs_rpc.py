"""Equivalence: the cross-target list RPC (#365) must match the Python path.

``get_cross_target_jobs`` replaces the untargeted list's Python
scan-everything-then-rank path (``_list_jobs_across_user_targets_two_query``)
with DB-side dedup + status filter + rank + paginate. The risk of a durable
perf rewrite is SEMANTIC DRIFT — a subtle divergence in dedup preference,
off-family gating, the Pending-exempt floor, or the graded-first sort would
silently reorder or hide jobs. This runs BOTH paths against one live-seeded
fixture and asserts identical pages across a sort × direction × status × floor
matrix, so any drift fails loudly.

The fixture deliberately packs the divergence-prone cases:
  * cross-target dedup (a job scored by two targets — best representative wins),
  * off-family drop on the BEST rep (high off-family score hides the job) vs
    keep-via-null-family target,
  * liveness drops (non-US, archived, purged),
  * gradedness (axis_scores present) vs Pending, and the Pending-exempt floor,
  * per-user status (user_jobs) resolution.

Self-skips when the local stack is unreachable (see conftest).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from supabase import AsyncClient, Client

from app.routers.jobs import (
    _list_jobs_across_user_targets_rpc,
    _list_jobs_across_user_targets_two_query,
    _list_jobs_for_target,
    _list_jobs_for_target_two_query,
)
from tests.integration.conftest import create_auth_user, delete_auth_user

pytestmark = pytest.mark.integration


def _iso(dt: datetime) -> str:
    return dt.isoformat()


@pytest.fixture
def seeded_cross_target(service_client: Client) -> Iterator[tuple[str, set[str]]]:
    """Seed one user + two targets (engineering-family, null-family) + a spread
    of jobs/scores/statuses. Yields ``(user_id, {target_ids})``; cleans up via
    the source cascade + target/user deletes."""
    now = datetime.now(UTC)
    user_id = create_auth_user(service_client)
    source_id = str(uuid.uuid4())
    t_eng = str(uuid.uuid4())  # role_family = engineering
    t_null = str(uuid.uuid4())  # role_family = NULL (ungated)

    # jobs: (key, family, is_us, archived, purged, first_seen offset days)
    j = {
        k: str(uuid.uuid4())
        for k in ("hi", "lo", "pend", "offdrop", "offkeep", "nonus", "arch", "purged", "dedup")
    }
    board = f"x365-{uuid.uuid4().hex[:10]}"
    try:
        service_client.table("sources").insert(
            {
                "id": source_id,
                "board_token": board,
                "company_name": "Acme",
                "provider": "greenhouse",
            }
        ).execute()
        service_client.table("targets").insert(
            [
                {"id": t_eng, "label": "Eng Target", "role_family": "engineering"},
                {"id": t_null, "label": "Null Target", "role_family": None},
            ]
        ).execute()
        # Distinct title / company_name / created_at per job so the non-score
        # sorts are fully ordered — a tie's order is unspecified in the Python
        # path (stable-sort fetch order), so only distinct keys are comparable.
        # (The RPC adds a deterministic job_posting_id tiebreak the Python path
        # lacked — a pagination-stability win, not a divergence to assert away.)
        service_client.table("jobs").insert(
            [
                # Aged 40d past the 7d grace: decays to ~45 (0.505*90) with
                # decay on, dropping it below the fresh lower-raw-score jobs.
                # Exercises the decay-aware sort; raw score keeps it top with
                # decay off. (Distinct age also keeps created_at/first_seen sort
                # deterministic.)
                _job(
                    j["hi"],
                    source_id,
                    "Alpha Graded High",
                    "Acme",
                    "engineering",
                    is_us=True,
                    fs=now - timedelta(days=40),
                    created=now - timedelta(days=40),
                ),
                _job(
                    j["lo"],
                    source_id,
                    "Bravo Graded Low",
                    "Bristol",
                    "engineering",
                    is_us=True,
                    fs=now - timedelta(days=2),
                    created=now - timedelta(days=2),
                ),
                _job(
                    j["pend"],
                    source_id,
                    "Charlie Pending",
                    "Cargo",
                    None,
                    is_us=True,
                    fs=now - timedelta(hours=3),
                    created=now - timedelta(hours=3),
                ),
                # Scored HIGH by the off-family (eng) target → best rep is off-family → dropped.
                _job(
                    j["offdrop"],
                    source_id,
                    "Delta OffFamily Drop",
                    "Dynatech",
                    "finance",
                    is_us=True,
                    fs=now,
                    created=now,
                ),
                # Scored best by the null-family target → ungated → kept.
                _job(
                    j["offkeep"],
                    source_id,
                    "Echo OffFamily Keep",
                    "Echelon",
                    "finance",
                    is_us=True,
                    fs=now - timedelta(hours=1),
                    created=now - timedelta(hours=1),
                ),
                _job(
                    j["nonus"],
                    source_id,
                    "Foxtrot NonUS",
                    "Foundry",
                    "engineering",
                    is_us=False,
                    fs=now,
                    created=now,
                ),
                _job(
                    j["arch"],
                    source_id,
                    "Golf Archived",
                    "Grid",
                    "engineering",
                    is_us=True,
                    fs=now,
                    created=now,
                    archived=now,
                ),
                _job(
                    j["purged"],
                    source_id,
                    "Hotel Purged",
                    "Helix",
                    "engineering",
                    is_us=True,
                    fs=now,
                    created=now,
                    # purged implies archived (jobs_purged_implies_archived CHECK,
                    # 20260721120000): a tombstone is always archived first. Set
                    # both so the fixture models a reachable prod state.
                    archived=now,
                    purged=now,
                ),
                _job(
                    j["dedup"],
                    source_id,
                    "India Dedup",
                    "Ionic",
                    "engineering",
                    is_us=True,
                    fs=now - timedelta(days=3),
                    created=now - timedelta(days=3),
                ),
            ]
        ).execute()
        service_client.table("scores").insert(
            [
                _score(j["hi"], t_eng, 90, graded=True),
                _score(j["lo"], t_eng, 70, graded=True),
                _score(j["pend"], t_eng, 65, graded=False),  # Pending
                _score(j["offdrop"], t_eng, 88, graded=True),  # high, but job=finance vs eng → drop
                _score(j["offkeep"], t_null, 55, graded=True),  # null-family target → kept
                _score(j["nonus"], t_eng, 80, graded=True),  # dropped: is_us false
                _score(j["arch"], t_eng, 80, graded=True),  # dropped: archived
                _score(j["purged"], t_eng, 80, graded=True),  # dropped: purged
                # Dedup: same job scored by BOTH targets. eng=graded 60, null=graded 75.
                # Best rep = null (higher raw score), which is ungated → kept at 75.
                _score(j["dedup"], t_eng, 60, graded=True),
                _score(j["dedup"], t_null, 75, graded=True),
            ]
        ).execute()
        # Per-user status: mark the top graded job saved; one job archived-status.
        service_client.table("user_jobs").insert(
            [
                {"user_id": user_id, "job_posting_id": j["hi"], "status": "saved"},
                {"user_id": user_id, "job_posting_id": j["lo"], "status": "applied"},
            ]
        ).execute()
        yield user_id, {t_eng, t_null}
    finally:
        service_client.table("sources").delete().eq("id", source_id).execute()
        service_client.table("targets").delete().in_("id", [t_eng, t_null]).execute()
        delete_auth_user(service_client, user_id)


def _job(jid, source_id, title, company, family, *, is_us, fs, created, archived=None, purged=None):
    row = {
        "id": jid,
        "external_id": f"ext-{jid[:8]}",
        "source_id": source_id,
        "title": title,
        "company_name": company,
        "role_family": family,
        "is_us": is_us,
        # fs = the posting's age driver (provider posted date since R2);
        # created = when we cataloged it.
        "source_posted_at": _iso(fs),
        "cataloged_at": _iso(created),
    }
    if archived is not None:
        row["archived_at"] = _iso(archived)
    if purged is not None:
        row["purged_at"] = _iso(purged)
    return row


def _axes(score: int) -> dict[str, int]:
    """Distinct-per-axis payload (#457): a non-uniform weight vector produces a
    blend that differs from — and can reorder vs — the raw score, so the weighted
    parity test isn't vacuous. Equal (default) weights reproduce the raw score
    exactly: the four offsets sum to zero, so their mean is ``score`` (every
    fixture score is in [10, 90], so nothing clamps)."""
    return {
        "title_fit": min(100, score + 5),
        "skills_fit": max(0, score - 10),
        "seniority_fit": min(100, score + 10),
        "domain_fit": max(0, score - 5),
    }


def _score(job_id, target_id, score, *, graded):
    return {
        "job_posting_id": job_id,
        "target_id": target_id,
        "score": score,
        "excluded": False,
        "scoring_status": "complete" if graded else "stage2",
        # axis_scores present ⇔ graded (the real _is_pending signal).
        "axis_scores": _axes(score) if graded else None,
    }


async def _rpc_page(client, user_id, target_ids, **kw):
    return await _list_jobs_across_user_targets_rpc(
        client,
        user_target_ids=target_ids,
        page_size=kw.get("page_size", 50),
        sort=kw["sort"],
        ascending=kw["ascending"],
        min_score=kw.get("min_score"),
        status=kw.get("status"),
        company=kw.get("company"),
        search=kw.get("search"),
        cursor=kw.get("cursor", {}),
        weights_by_target=kw.get("weights_by_target"),
        user_id=user_id,
    )


async def _py_page(client, user_id, target_ids, **kw):
    return await _list_jobs_across_user_targets_two_query(
        client,
        user_target_ids=target_ids,
        page_size=kw.get("page_size", 50),
        sort=kw["sort"],
        ascending=kw["ascending"],
        min_score=kw.get("min_score"),
        status=kw.get("status"),
        company=kw.get("company"),
        search=kw.get("search"),
        exclude_terms=[],
        only_terms=[],
        cursor=kw.get("cursor", {}),
        weights_by_target=kw.get("weights_by_target"),
        user_id=user_id,
    )


def _ids(page):
    return [p["id"] for p in page["postings"]]


@pytest.mark.parametrize("decay", [False, True], ids=["decay-off", "decay-on"])
@pytest.mark.parametrize("sort", ["score", "created_at", "title", "company_name"])
@pytest.mark.parametrize("ascending", [False, True])
@pytest.mark.parametrize("status", [None, "new"])
@pytest.mark.parametrize("min_score", [0, 60])
async def test_rpc_matches_python_across_matrix(
    service_client: Client,
    async_service_client: AsyncClient,
    seeded_cross_target: tuple[str, set[str]],
    monkeypatch: pytest.MonkeyPatch,
    decay: bool,
    sort: str,
    ascending: bool,
    status: str | None,
    min_score: int,
) -> None:
    # Both paths read settings.recency_decay_enabled: the RPC forwards it as
    # p_recency_decay, the two-query path applies it in _display_sort_value.
    from app.config import settings

    monkeypatch.setattr(settings, "recency_decay_enabled", decay)
    user_id, target_ids = seeded_cross_target
    kw = {"sort": sort, "ascending": ascending, "status": status, "min_score": min_score}
    rpc = await _rpc_page(async_service_client, user_id, target_ids, **kw)
    py = await _py_page(async_service_client, user_id, target_ids, **kw)

    # The exact same jobs, in the exact same order.
    assert _ids(rpc) == _ids(py), (
        f"order drift @ sort={sort} asc={ascending} status={status} min={min_score}: "
        f"rpc={_ids(rpc)} py={_ids(py)}"
    )
    # ...and the same per-row status + Pending badge.
    rpc_meta = {p["id"]: (p["status"], p["pending"]) for p in rpc["postings"]}
    py_meta = {p["id"]: (p["status"], p["pending"]) for p in py["postings"]}
    assert rpc_meta == py_meta


# Skewed axis weights (title + seniority heavy) that pull the blend off the raw
# score — sums to 1.0 so the renormalization is a no-op the math still exercises.
_SKEWED = {"title_fit": 0.4, "skills_fit": 0.1, "seniority_fit": 0.4, "domain_fit": 0.1}


@pytest.mark.parametrize("decay", [False, True], ids=["decay-off", "decay-on"])
@pytest.mark.parametrize("ascending", [False, True])
@pytest.mark.parametrize("min_score", [0, 60])
async def test_rpc_weighted_blend_matches_python(
    service_client: Client,
    async_service_client: AsyncClient,
    seeded_cross_target: tuple[str, set[str]],
    monkeypatch: pytest.MonkeyPatch,
    decay: bool,
    ascending: bool,
    min_score: int,
) -> None:
    """#457: with custom axis weights the RPC computes the weighted display blend
    DB-side (``p_weights`` -> wyrdfold_display_score) instead of falling to the
    scan-everything Python path. It must match the Python two-query overlay
    EXACTLY — same order AND same per-row displayed score — across the same
    divergence-prone fixture (dedup, off-family, liveness, Pending floor, decay).
    This is the guardrail against SQL-vs-Python blend drift."""
    from app.config import settings
    from app.models.targets import AxisWeights

    monkeypatch.setattr(settings, "recency_decay_enabled", decay)
    user_id, target_ids = seeded_cross_target
    weights = {tid: AxisWeights(**_SKEWED) for tid in target_ids}
    kw = {
        "sort": "score",
        "ascending": ascending,
        "min_score": min_score,
        "weights_by_target": weights,
    }
    rpc = await _rpc_page(async_service_client, user_id, target_ids, **kw)
    py = await _py_page(async_service_client, user_id, target_ids, **kw)

    assert _ids(rpc) == _ids(py), (
        f"weighted order drift asc={ascending} min={min_score} decay={decay}: "
        f"rpc={_ids(rpc)} py={_ids(py)}"
    )
    # The displayed (weighted, undecayed-then-decayed) score matches per row.
    rpc_score = {p["id"]: p["score"] for p in rpc["postings"]}
    py_score = {p["id"]: p["score"] for p in py["postings"]}
    assert rpc_score == py_score, f"weighted score drift: rpc={rpc_score} py={py_score}"


async def test_weighted_blend_actually_moves_the_score(
    service_client: Client,
    async_service_client: AsyncClient,
    seeded_cross_target: tuple[str, set[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard against a vacuous parity test: prove the skewed weights actually pull
    the displayed score off the raw score for at least one row. Without this, the
    RPC==Python assertion would still pass if the blend were silently a no-op
    (both returning the raw score)."""
    from app.config import settings
    from app.models.targets import AxisWeights

    monkeypatch.setattr(settings, "recency_decay_enabled", False)
    user_id, target_ids = seeded_cross_target
    weights = {tid: AxisWeights(**_SKEWED) for tid in target_ids}

    weighted = await _rpc_page(
        async_service_client,
        user_id,
        target_ids,
        sort="score",
        ascending=False,
        weights_by_target=weights,
    )
    # raw_score rides along on every RPC row now (#457) — the undecayed Sonnet fit.
    moved = [p for p in weighted["postings"] if not p["pending"] and p["score"] != p["raw_score"]]
    assert moved, (
        "skewed weights moved no displayed score off raw_score — the DB-side "
        f"blend was not applied: {[(p['title'], p['score'], p['raw_score']) for p in weighted['postings']]}"
    )


async def test_rpc_drops_offfamily_liveness_and_dedups(
    service_client: Client,
    async_service_client: AsyncClient,
    seeded_cross_target: tuple[str, set[str]],
) -> None:
    """Pin the fixture's semantics directly (not just RPC==Python): the
    non-live + off-family-on-best-rep jobs are gone, and the cross-target job
    appears once at its best representative's score."""
    user_id, target_ids = seeded_cross_target
    page = await _rpc_page(async_service_client, user_id, target_ids, sort="score", ascending=False)
    by_title = {p["title"]: p for p in page["postings"]}

    # Live + in-family (or null-family) jobs present; dead + off-family-drop gone.
    assert "Alpha Graded High" in by_title
    assert "Echo OffFamily Keep" in by_title  # kept via null-family target
    assert "India Dedup" in by_title
    for gone in ("Delta OffFamily Drop", "Foxtrot NonUS", "Golf Archived", "Hotel Purged"):
        assert gone not in by_title, f"{gone} should have been gated out"

    # Dedup: one row, best representative = the null-family target's 75 (not eng's 60).
    dedup_rows = [p for p in page["postings"] if p["title"] == "India Dedup"]
    assert len(dedup_rows) == 1
    assert dedup_rows[0]["score"] == 75


async def test_rpc_company_and_search_filters_match_python(
    service_client: Client,
    async_service_client: AsyncClient,
    seeded_cross_target: tuple[str, set[str]],
) -> None:
    """The rare page path joins jobs for a company/search filter (the denormalized
    columns don't carry title/company). Assert it still matches Python."""
    user_id, target_ids = seeded_cross_target
    for kw in (
        {"company": "Acme"},  # only "Alpha Graded High"
        {"search": "Graded"},  # "Alpha Graded High" + "Bravo Graded Low"
        {"company": "Bristol", "search": "Bravo"},  # both filters together
    ):
        rpc = await _rpc_page(
            async_service_client, user_id, target_ids, sort="score", ascending=False, **kw
        )
        py = await _py_page(
            async_service_client, user_id, target_ids, sort="score", ascending=False, **kw
        )
        assert _ids(rpc) == _ids(py), f"filter {kw}: rpc={_ids(rpc)} py={_ids(py)}"


async def test_jobs_archival_trigger_syncs_and_drops(
    service_client: Client,
    async_service_client: AsyncClient,
    seeded_cross_target: tuple[str, set[str]],
) -> None:
    """The denormalization's liveness can go stale if a job is archived AFTER
    scoring. The jobs AFTER-UPDATE trigger must fan job_is_live out to scores so
    the RPC drops the now-dead job — matching the Python path, which reads jobs
    live. This is the failure mode denormalization introduces; pin it."""
    user_id, target_ids = seeded_cross_target
    before = {
        p["title"]
        for p in (
            await _rpc_page(
                async_service_client, user_id, target_ids, sort="score", ascending=False
            )
        )["postings"]
    }
    assert "Alpha Graded High" in before

    job = (
        service_client.table("jobs")
        .select("id")
        .eq("title", "Alpha Graded High")
        .single()
        .execute()
        .data
    )
    service_client.table("jobs").update({"archived_at": _iso(datetime.now(UTC))}).eq(
        "id", job["id"]
    ).execute()

    # Trigger flipped every score row's denormalized liveness.
    rows = (
        service_client.table("scores")
        .select("job_is_live")
        .eq("job_posting_id", job["id"])
        .execute()
        .data
    )
    assert rows and all(r["job_is_live"] is False for r in rows), (
        "jobs trigger did not sync job_is_live"
    )

    # And the RPC now drops it, still matching the live-reading Python path.
    rpc = await _rpc_page(async_service_client, user_id, target_ids, sort="score", ascending=False)
    py = await _py_page(async_service_client, user_id, target_ids, sort="score", ascending=False)
    assert "Alpha Graded High" not in {p["title"] for p in rpc["postings"]}
    assert _ids(rpc) == _ids(py)


async def test_jobs_refamily_trigger_syncs_and_gates(
    service_client: Client,
    async_service_client: AsyncClient,
    seeded_cross_target: tuple[str, set[str]],
) -> None:
    """Re-tagging a job's role_family must sync to scores so the off-family gate
    re-evaluates against the denormalized value — matching Python."""
    user_id, target_ids = seeded_cross_target
    # "Bravo Graded Low" is engineering, scored only by the eng target → kept.
    job = (
        service_client.table("jobs")
        .select("id")
        .eq("title", "Bravo Graded Low")
        .single()
        .execute()
        .data
    )
    service_client.table("jobs").update({"role_family": "finance"}).eq("id", job["id"]).execute()

    rows = (
        service_client.table("scores")
        .select("job_role_family")
        .eq("job_posting_id", job["id"])
        .execute()
        .data
    )
    assert rows and all(r["job_role_family"] == "finance" for r in rows), (
        "jobs trigger did not sync role_family"
    )

    # Now off-family for the eng target → gated out, matching Python.
    rpc = await _rpc_page(async_service_client, user_id, target_ids, sort="score", ascending=False)
    py = await _py_page(async_service_client, user_id, target_ids, sort="score", ascending=False)
    assert "Bravo Graded Low" not in {p["title"] for p in rpc["postings"]}
    assert _ids(rpc) == _ids(py)


async def test_per_target_score_sort_routes_through_cross_target_and_gates(
    service_client: Client,
    async_service_client: AsyncClient,
    seeded_cross_target: tuple[str, set[str]],
) -> None:
    """#2: per-target `/jobs?target_id=…&sort=score` used to run the Python
    scan-everything path (~8.6s cold). It now routes through the cross-target RPC
    restricted to the one target — the same gated + graded-first + decay +
    Pending-floor ranking, index-only. The one intended behavior change: score
    sort now applies the off-family gate (like the per-target non-score sorts and
    the dashboard), where the old scan-everything path did not. Assert exactly
    that: off-family drops, and nothing else in the ranking moves."""
    user_id, target_ids = seeded_cross_target
    tmap = {
        t["id"]: t["role_family"]
        for t in service_client.table("targets")
        .select("id,role_family")
        .in_("id", list(target_ids))
        .execute()
        .data
    }
    t_eng = next(t for t, fam in tmap.items() if fam == "engineering")
    common: dict = {
        "target_id": t_eng,
        "page_size": 50,
        "sort": "score",
        "ascending": False,
        "min_score": None,
        "status": None,
        "company": None,
        "search": None,
        "exclude_terms": [],
        "only_terms": [],
        "cursor": {},
        "user_id": user_id,
    }
    new = await _list_jobs_for_target(async_service_client, **common)  # new routing
    old = await _list_jobs_for_target_two_query(async_service_client, **common)  # prior behavior
    new_titles = [p["title"] for p in new["postings"]]
    old_titles = [p["title"] for p in old["postings"]]

    # The off-family job (finance job scored by the eng target) is now gated out,
    # where the scan-everything path kept it.
    assert "Delta OffFamily Drop" in old_titles
    assert "Delta OffFamily Drop" not in new_titles
    # Everything else — same jobs, same order — once off-family is removed.
    assert new_titles == [t for t in old_titles if t != "Delta OffFamily Drop"]


async def test_rpc_offset_pagination_and_has_more(
    service_client: Client,
    async_service_client: AsyncClient,
    seeded_cross_target: tuple[str, set[str]],
) -> None:
    """page_size=2 yields a next cursor; walking it covers the full set once."""
    user_id, target_ids = seeded_cross_target
    full = _ids(
        await _rpc_page(async_service_client, user_id, target_ids, sort="score", ascending=False)
    )
    assert len(full) >= 4  # enough to page

    seen: list[str] = []
    cursor: dict = {}
    for _ in range(10):  # bounded walk
        page = await _rpc_page(
            async_service_client,
            user_id,
            target_ids,
            sort="score",
            ascending=False,
            page_size=2,
            cursor=cursor,
        )
        seen.extend(_ids(page))
        if not page["next_cursor"]:
            break
        from app.routers.jobs import _decode_cursor

        cursor = _decode_cursor(page["next_cursor"])

    assert seen == full  # no gaps, no dupes, same order as the single-page fetch


async def test_rpcs_serve_title_display(
    service_client: Client,
    async_service_client: AsyncClient,
    seeded_cross_target: tuple[str, set[str]],
) -> None:
    """title_display stage 2 (2026-08-14): both list RPCs serve the cleaned
    display title. Stage 1 added the column + ingest writes + the direct-query
    projections; these two functions were the last readers still projecting
    only the raw title, so the FE's ``displayTitle()`` fallback was permanent
    on the main list paths."""
    user_id, target_ids = seeded_cross_target
    service_client.table("jobs").update({"title_display": "Alpha Graded High (Clean)"}).eq(
        "title", "Alpha Graded High"
    ).execute()

    # Cross-target RPC: repaired row carries the cleaned form; untouched rows
    # carry an explicit NULL (a MISSING key would mean the projection silently
    # dropped the column again).
    page = await _rpc_page(async_service_client, user_id, target_ids, sort="score", ascending=False)
    by_title = {p["title"]: p for p in page["postings"]}
    assert by_title["Alpha Graded High"]["title_display"] == "Alpha Graded High (Clean)"
    assert "title_display" in by_title["Bravo Graded Low"]
    assert by_title["Bravo Graded Low"]["title_display"] is None

    # Per-target RPC (get_target_jobs), driven raw so the assertion holds even
    # if the router helper's dispatch changes shape.
    resp = await async_service_client.rpc(
        "get_target_jobs", {"p_target_id": next(iter(target_ids)), "p_limit": 50}
    ).execute()
    rows = resp.data or []
    assert rows, "seeded target should list at least one job"
    assert all("title_display" in r for r in rows)
    cleaned = {r["title"]: r["title_display"] for r in rows}
    if "Alpha Graded High" in cleaned:
        assert cleaned["Alpha Graded High"] == "Alpha Graded High (Clean)"
