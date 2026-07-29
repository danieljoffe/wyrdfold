from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from app.models.targets import (
    CategoryProfile,
    JobTarget,
    ScoringProfile,
    SeniorityProfile,
)
from app.services.poller import (
    _is_us_location,
    _passes_free_gates,
    _title_matches_any_target,
    _title_matches_target,
)
from app.services.standard_job import StandardJob


def _make_target(core_keywords: dict[str, int]) -> JobTarget:
    """Create a minimal target with the given core_skills keywords."""
    return JobTarget(
        id="test-target",
        label="Test Target",
        scoring_profile=ScoringProfile(
            categories={
                "core_skills": CategoryProfile(keywords=core_keywords, weight=2.0),
            },
            seniority=SeniorityProfile(signals=["senior", "staff", "lead"]),
        ),
        search_keywords=[],
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def test_title_matches_target_with_keyword():
    targets = [_make_target({"react": 3, "typescript": 3})]
    assert _title_matches_any_target("Senior React Engineer", targets) is True


def test_title_no_match():
    targets = [_make_target({"react": 3, "typescript": 3})]
    assert _title_matches_any_target("Marketing Specialist", targets) is False


def test_title_matches_seniority_signal():
    targets = [_make_target({"react": 3})]
    assert _title_matches_any_target("Senior Software Engineer", targets) is True


def test_title_matches_with_multiple_targets():
    targets = [
        _make_target({"java": 3}),
        _make_target({"react": 3}),
    ]
    assert _title_matches_any_target("React Developer", targets) is True


def test_empty_targets_no_match():
    assert _title_matches_any_target("Senior React Engineer", []) is False


# ---- _title_matches_target token-overlap behaviour --------------------------
#
# The previous matcher did a plain substring check: "director of cx operations"
# had to literally appear in the title. Real postings almost never match that
# verbatim — companies rewrite titles ("Director, Customer Experience"
# without "of cx", "VP Customer Operations" with no "director"). The new
# matcher tokenizes both sides and requires a content-token overlap, which
# is the actual behaviour these tests guard.


def test_multi_token_keyword_matches_reordered_title():
    """The bug-from-prod case: pasted "director of cx operations" keyword
    against a title that contains the same content tokens in a different
    order with different filler words.
    """
    keywords = ["director of cx operations"]
    assert _title_matches_target("Director, CX Operations", keywords) is True


def test_multi_token_keyword_matches_when_stopwords_differ():
    """Filler words ('of', 'and') shouldn't gate the match either way."""
    keywords = ["head of customer experience"]
    assert _title_matches_target("Head, Customer Experience", keywords) is True


def test_single_token_keyword_still_substring_matches():
    """1-token keywords (the existing common case) keep the old behaviour:
    pure substring against the title. Plurals and compound words still match.
    """
    keywords = ["engineer"]
    assert _title_matches_target("Senior Software Engineer", keywords) is True
    assert _title_matches_target("Engineering Manager", keywords) is True


def test_overlap_below_threshold_does_not_match():
    """A keyword with 5 content tokens needs >= 3 to land. Only 1 hit on a
    long keyword should be rejected so we don't surface every job whose title
    happens to mention "director".
    """
    keywords = ["director of customer experience transformation"]
    # Only "director" overlaps — 1 of 4 content tokens, below the 0.6 ratio.
    assert _title_matches_target("Director of Engineering", keywords) is False


def test_disjoint_keywords_do_not_match():
    keywords = ["software engineer", "frontend developer"]
    assert _title_matches_target("Director of Customer Experience", keywords) is False


def test_any_one_of_several_keywords_is_enough():
    """The function returns True as soon as one keyword in the list
    overlaps — used by the caller to spread a target's full keyword list
    against each posting.
    """
    keywords = ["product manager", "director of cx operations"]
    assert _title_matches_target("Director, CX Operations", keywords) is True


def test_empty_keyword_list_does_not_match():
    assert _title_matches_target("Director of Engineering", []) is False


def test_empty_title_does_not_match():
    assert _title_matches_target("", ["director of cx operations"]) is False


# ---- distinctive-token precision (the #60 generic-overlap leak) -------------
#
# Real prod finding: ~593 off-target jobs/week rode generic-token overlap into
# the active targets. On a 3-token keyword the 0.6 ratio is met by any 2 tokens,
# so generic role words (director, operations, engineer) carried the match while
# the distinctive DOMAIN token was absent. The matcher now also requires ≥1
# distinctive token in the title.


def test_generic_overlap_without_distinctive_token_is_rejected():
    """ "Finance Operations Director" shares director+operations (2 of 3) with
    "director of customer operations" but lacks the distinctive "customer"."""
    kw = ["director of customer operations"]
    assert _title_matches_target("Senior Director, Finance Operations", kw) is False
    assert _title_matches_target("Director of Sales Operations", kw) is False
    assert _title_matches_target("VP of Finance Operations", ["vp of customer operations"]) is False


def test_generic_role_type_overlap_is_rejected():
    """Frontend: "Kubernetes Platform Engineer" shares platform+engineer with
    "frontend platform engineer" but lacks the distinctive "frontend"."""
    kw = ["frontend platform engineer"]
    assert _title_matches_target("Kubernetes Platform Engineer", kw) is False
    assert _title_matches_target("Machine Learning Platform Engineer", kw) is False


def test_distinctive_token_present_still_matches():
    """Recall preserved: title-word swaps (director↔head↔vp) still match while
    the distinctive domain token is present."""
    ops = ["director of customer operations"]
    assert _title_matches_target("Head of Customer Operations", ops) is True
    assert _title_matches_target("VP, Customer Operations", ops) is True
    assert (
        _title_matches_target("Director, Customer Success", ["director of customer success"])
        is True
    )
    assert _title_matches_target("Staff Frontend Engineer", ["frontend engineer"]) is True
    assert _title_matches_target("React Developer", ["react developer"]) is True


def test_all_generic_keyword_keeps_overlap_only():
    """A keyword with no distinctive token (all generic) can't gate on one, so
    it keeps overlap-only behaviour — GIGO for an inherently generic target."""
    assert _title_matches_target("Finance Operations Manager", ["operations manager"]) is True


# ---- ingest hole: no active targets → ingest nothing (#60) ------------------


def _search_kw_target(search_keywords: list[str], core: dict[str, int]) -> JobTarget:
    return JobTarget(
        id="t-kw",
        label="KW Target",
        scoring_profile=ScoringProfile(
            categories={"core_skills": CategoryProfile(keywords=core, weight=2.0)},
            seniority=SeniorityProfile(signals=["senior", "staff", "lead"]),
        ),
        search_keywords=search_keywords,
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _us_job(title: str) -> StandardJob:
    return StandardJob(
        external_id="e1",
        title=title,
        location_name="New York, NY",
        department=None,
        content="",
        updated_at="2026-01-01",
        absolute_url="https://example.com/j/1",
    )


def test_free_gates_reject_everything_when_no_active_targets():
    """The whole-board hole: with NO active targets there is nothing to match,
    so a US, otherwise-ingestable role is dropped instead of ingested."""
    assert _passes_free_gates(_us_job("Director of Customer Operations"), []) is False


def test_free_gates_admit_when_a_target_matches():
    """Control: the gate still admits a genuine match (so the hole fix didn't
    just break ingestion)."""
    target = _search_kw_target(
        ["director of customer operations"], {"customer": 1, "operations": 1}
    )
    assert _passes_free_gates(_us_job("Director of Customer Operations"), [target]) is True


class TestIsUsLocation:
    def test_none_is_allowed(self):
        assert _is_us_location(None) is True

    def test_empty_string_is_allowed(self):
        assert _is_us_location("") is True

    def test_remote_is_allowed(self):
        assert _is_us_location("Remote") is True

    def test_us_city_state_is_allowed(self):
        assert _is_us_location("San Francisco, CA") is True
        assert _is_us_location("New York, NY") is True
        assert _is_us_location("Austin, TX") is True

    def test_us_remote_is_allowed(self):
        assert _is_us_location("Remote - US") is True
        assert _is_us_location("US (Remote)") is True

    def test_uk_rejected(self):
        assert _is_us_location("London, United Kingdom") is False

    def test_germany_rejected(self):
        assert _is_us_location("Berlin, Germany") is False

    def test_canada_rejected(self):
        assert _is_us_location("Toronto, Canada") is False
        assert _is_us_location("Vancouver, BC") is False

    def test_india_rejected(self):
        assert _is_us_location("Bangalore, India") is False

    def test_emea_rejected(self):
        assert _is_us_location("Remote - EMEA") is False

    def test_europe_rejected(self):
        assert _is_us_location("Europe") is False

    def test_apac_rejected(self):
        assert _is_us_location("APAC") is False

    def test_case_insensitive(self):
        assert _is_us_location("BERLIN, GERMANY") is False
        assert _is_us_location("berlin") is False

    def test_us_locations_containing_hint_substrings_are_allowed(self):
        """Regression: substring matching dropped real US locations.

        "india" ⊂ "Indianapolis"/"Indiana" was the worst offender; word-
        boundary matching fixes the fragment cases.
        """
        assert _is_us_location("Indianapolis, IN") is True
        assert _is_us_location("Indianapolis, Indiana") is True
        assert _is_us_location("Remote - Indiana") is True

    def test_us_cities_sharing_non_us_names_are_allowed_with_state(self):
        """US cities named after non-US cities pass when a state is present."""
        assert _is_us_location("Dublin, OH") is True
        assert _is_us_location("Dublin, CA") is True
        assert _is_us_location("Athens, GA") is True
        assert _is_us_location("Rome, GA") is True
        assert _is_us_location("Milan, MI") is True

    def test_usa_marker_wins_over_city_hint(self):
        assert _is_us_location("Dublin, Ohio, USA") is True
        assert _is_us_location("Athens (United States)") is True

    def test_non_us_versions_still_rejected(self):
        assert _is_us_location("Dublin, Ireland") is False
        assert _is_us_location("Athens, Greece") is False
        assert _is_us_location("Rome, Italy") is False
        assert _is_us_location("Milan") is False
        assert _is_us_location("Bangalore, India") is False

    def test_czechia_rejected(self):
        assert _is_us_location("Prague, Czechia") is False

    def test_lowercase_state_letters_do_not_count_as_us_marker(self):
        # ", ca" lowercase isn't the "City, ST" form — "toronto, canada"
        # style strings must not accidentally hit the state fastpath.
        assert _is_us_location("toronto, canada") is False


# ---- AND-semantics ingestion gate ------------------------------------------
#
# The gate used to admit on (matched_keywords or excluded). When a target
# has search_keywords set, admission now also requires a search-keyword
# token-overlap with the title — so incidental keyword hits don't ingest
# off-topic postings into the user's list.


def _target_with_keywords(
    core_keywords: dict[str, int],
    search_keywords: list[str],
) -> JobTarget:
    return JobTarget(
        id="t-with-kw",
        label="Director CX",
        scoring_profile=ScoringProfile(
            categories={
                "core_skills": CategoryProfile(keywords=core_keywords, weight=2.0),
            },
            seniority=SeniorityProfile(signals=["director", "head of"]),
        ),
        search_keywords=search_keywords,
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def test_and_semantics_admits_when_search_keyword_overlaps():
    target = _target_with_keywords(
        {"Zendesk": 3},
        ["director of customer experience", "head of cx"],
    )
    # Title has a search-keyword overlap AND a seniority signal hit
    # ("director"), so it passes both halves of the AND.
    assert _title_matches_any_target("Director of Customer Experience", [target]) is True


def test_and_semantics_rejects_keyword_match_without_search_overlap():
    """Regression: a job whose title only matches a generic profile signal
    ('director') but has no search-keyword overlap should NOT be ingested."""
    target = _target_with_keywords(
        {"Zendesk": 3},
        ["director of customer experience"],
    )
    # "Director of Engineering" hits the "director" seniority signal but
    # is not a customer-experience role — rejected at the door.
    assert _title_matches_any_target("Director of Engineering", [target]) is False


def test_and_semantics_falls_back_when_search_keywords_empty():
    """Targets with empty search_keywords (legacy/draft profiles) keep
    the old OR semantics so they don't accidentally ingestion-block."""
    legacy_target = _make_target({"react": 3})  # search_keywords=[]
    assert _title_matches_any_target("Senior React Engineer", [legacy_target]) is True


def test_and_semantics_admits_excluded_for_audit():
    """Hard-exclude (negative keyword) still admits so the scorer can
    record excluded=True — preserves the audit trail."""
    target = _target_with_keywords(
        {"Zendesk": 3},
        ["director of cx"],
    )
    target.scoring_profile.negative.keywords = ["junior"]
    # The title hits 'junior' (negative) but no search-keyword overlap.
    # Excluded path admits regardless.
    assert _title_matches_any_target("Junior Random Role", [target]) is True


# ---- poll_sources_for_target: inactive-target guard -----------------------


def _full_target(*, is_active: bool, search_keywords: list[str]) -> JobTarget:
    """Build a target with a real search_keywords list so the inactive
    guard is exercised in isolation from the 'empty keywords' guard.
    """
    return JobTarget(
        id="t-1",
        label="Staff Frontend Engineer",
        scoring_profile=ScoringProfile(
            categories={
                "core_skills": CategoryProfile(keywords={"react": 3}, weight=2.0),
            },
            seniority=SeniorityProfile(signals=["staff"]),
        ),
        search_keywords=search_keywords,
        is_active=is_active,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def test_poll_sources_for_target_skips_inactive_target() -> None:
    """Inactive targets short-circuit before any sources query.

    ``targets.is_active=False`` means no user currently has the target
    enabled (trigger OR across user_targets). Fanning out a per-target
    poll across every ATS source for a target nobody will see is pure
    waste — return an empty PollResult immediately.
    """
    import asyncio
    from unittest.mock import MagicMock

    from app.services.poller import poll_sources_for_target

    supabase = MagicMock()
    target = _full_target(is_active=False, search_keywords=["frontend engineer"])

    result = asyncio.run(poll_sources_for_target(supabase, target))

    assert result.sources_polled == 0
    assert result.new_jobs == 0
    assert result.updated_jobs == 0
    assert result.archived_jobs == 0
    assert result.errors == []
    # Critically: no DB traffic.
    supabase.table.assert_not_called()


# ---- mass-archive guard -----------------------------------------------------


def _make_poll_supabase(existing_rows: list[dict]) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Mock Supabase for a ``_poll_one_source`` run with no upserts.

    Returns ``(supabase, jobs_table, sources_table)`` so tests can assert
    on the archive UPDATE (jobs table) separately from the
    ``last_polled_at`` stamp (sources table).
    """
    jobs_table = MagicMock()
    # Admit path (e.g. the budget re-check tests): a triaged-in job upserts,
    # so default jobs.upsert to "no rows" rather than a bare MagicMock.
    jobs_table.upsert.return_value.execute.return_value.data = []
    # #514: the poller reads the source's FULL external_id set (engaged and
    # archived rows included) for Phase-1 admission scoping. Default it to
    # the same ids as the RPC's live-unengaged view; tests that need the
    # wider set (engaged rows) override this chain.
    jobs_table.select.return_value.eq.return_value.execute.return_value.data = [
        {"external_id": r.get("external_id")} for r in existing_rows
    ]

    sources_table = MagicMock()
    # Stage-3 junction, empty (no subscribed users) so the post-triage
    # scoring path runs to completion when a job is admitted, instead of
    # KeyError-ing on an unmapped table.
    user_targets_table = MagicMock()
    (
        user_targets_table.select.return_value.eq.return_value.in_.return_value.execute.return_value.data
    ) = []
    # #514: Stage 2 reads the persisted ``promising`` floor for refreshed
    # known rows. Empty by default; floor tests override.
    scores_table = MagicMock()
    (
        scores_table.select.return_value.eq.return_value.in_.return_value.execute.return_value.data
    ) = []
    supabase = MagicMock()
    supabase.table.side_effect = lambda name: {
        "jobs": jobs_table,
        "sources": sources_table,
        "user_targets": user_targets_table,
        "scores": scores_table,
    }[name]

    # #93: both the existing-rows read and the stale-archive write are
    # server-side RPCs now, so route ``supabase.rpc(name, ...)`` by name:
    #   - ``source_live_unengaged_jobs`` (read): live, unengaged rows for the
    #     source via a NOT EXISTS anti-join — replaces the old
    #     ``.table("jobs").select().eq().is_()`` + ``user_jobs`` engaged fetch
    #     + client-side ``.not_.in_("id", …)``. The engaged set never leaves
    #     Postgres, so there's no user_jobs round-trip to mock.
    #   - ``archive_jobs_by_ids`` (write): one set-based UPDATE stamping
    #     archived_at/updated_at — replaces the chunked
    #     ``.table("jobs").update().in_("id", chunk)``.
    rpc_handles: dict[str, MagicMock] = {}

    def _rpc(name: str, *_args: object, **_kwargs: object) -> MagicMock:
        handle = rpc_handles.setdefault(name, MagicMock())
        if name == "source_live_unengaged_jobs":
            handle.execute.return_value.data = existing_rows
        else:
            handle.execute.return_value.data = []
        return handle

    supabase.rpc.side_effect = _rpc
    return supabase, jobs_table, sources_table


_GUARD_SOURCE = {
    "id": "src-1",
    "board_token": "acme",
    "provider": "greenhouse",
    "company_name": "Acme",
}


@pytest.mark.asyncio
async def test_zero_job_fetch_with_existing_rows_skips_archiving(monkeypatch):
    """Regression: fetchers like workday return [] on API errors. A
    zero-job fetch must NOT archive the source's existing jobs — that
    turns a transient upstream hiccup into a wiped source."""
    from app.services import poller as poller_mod

    existing = [
        {"id": "job-1", "external_id": "e-1", "title": "T1", "company_name": "Acme"},
        {"id": "job-2", "external_id": "e-2", "title": "T2", "company_name": "Acme"},
    ]
    supabase, jobs_table, sources_table = _make_poll_supabase(existing)

    async def empty_fetch(_token: str) -> list[StandardJob]:
        return []

    monkeypatch.setitem(poller_mod.FETCHERS, "greenhouse", empty_fetch)
    monkeypatch.setattr(poller_mod, "get_active_target", lambda _sb: [])

    summary = await poller_mod._poll_one_source(dict(_GUARD_SOURCE), supabase)

    assert summary["polled"] is True
    assert summary["archived"] == 0
    # No jobs-table UPDATE at all — the only UPDATE this cycle is the
    # sources-table last_polled_at stamp.
    jobs_table.update.assert_not_called()
    sources_table.update.assert_called_once()


@pytest.mark.asyncio
async def test_nonzero_fetch_still_archives_stale_rows(monkeypatch):
    """The guard must not break normal stale-archiving: a fetch that
    returns jobs archives existing rows missing from the board."""
    from app.services import poller as poller_mod

    existing = [
        {"id": "job-1", "external_id": "gone-1", "title": "T1", "company_name": "Acme"},
    ]
    supabase, jobs_table, _sources_table = _make_poll_supabase(existing)

    async def one_job_fetch(_token: str) -> list[StandardJob]:
        # Non-US location so the job is dropped pre-upsert — keeps the
        # test on the no-upsert path while the fetch itself is non-empty.
        return [
            StandardJob(
                external_id="live-1",
                title="Director of CX",
                location_name="London, United Kingdom",
                department=None,
                content="",
                updated_at="2026-01-01",
                absolute_url="https://example.com/j/1",
            )
        ]

    monkeypatch.setitem(poller_mod.FETCHERS, "greenhouse", one_job_fetch)
    monkeypatch.setattr(poller_mod, "get_active_target", lambda _sb: [])

    summary = await poller_mod._poll_one_source(dict(_GUARD_SOURCE), supabase)

    assert summary["archived"] == 1
    # #75 C3 / #93: stale jobs are flagged globally-dead via the
    # ``archive_jobs_by_ids`` RPC (ids in the jsonb body, archived_at stamped
    # server-side), not a per-user jobs.status UPDATE.
    jobs_table.update.assert_not_called()
    archive_calls = [c for c in supabase.rpc.call_args_list if c.args[0] == "archive_jobs_by_ids"]
    assert len(archive_calls) == 1
    assert archive_calls[0].args[1] == {"p_ids": ["job-1"]}


@pytest.mark.asyncio
async def test_stale_archive_uses_single_rpc_with_all_ids(monkeypatch):
    """A large stale-archive set is ONE ``archive_jobs_by_ids`` RPC carrying
    every id in the jsonb body — no ``id=in.(...)`` URL filter to overflow,
    no chunking — covering every stale id exactly once (#93). The RPC stamps
    one shared archived_at server-side, so single-UPDATE semantics hold."""
    from app.services import poller as poller_mod

    # 250 existing rows, all delisted (none appears in the live fetch).
    existing = [
        {
            "id": f"job-{i}",
            "external_id": f"gone-{i}",
            "title": f"T{i}",
            "company_name": "Acme",
        }
        for i in range(250)
    ]
    supabase, jobs_table, _sources_table = _make_poll_supabase(existing)

    async def one_job_fetch(_token: str) -> list[StandardJob]:
        # Non-US location → dropped pre-upsert (no-upsert archive path),
        # while the fetch itself is non-empty so the mass-archive guard
        # doesn't skip the stale pass.
        return [
            StandardJob(
                external_id="live-1",
                title="Director of CX",
                location_name="London, United Kingdom",
                department=None,
                content="",
                updated_at="2026-01-01",
                absolute_url="https://example.com/j/1",
            )
        ]

    monkeypatch.setitem(poller_mod.FETCHERS, "greenhouse", one_job_fetch)
    monkeypatch.setattr(poller_mod, "get_active_target", lambda _sb: [])

    summary = await poller_mod._poll_one_source(dict(_GUARD_SOURCE), supabase)

    assert summary["archived"] == 250
    # No client-side jobs UPDATE at all — the archive is the RPC.
    jobs_table.update.assert_not_called()
    archive_calls = [c for c in supabase.rpc.call_args_list if c.args[0] == "archive_jobs_by_ids"]
    # Exactly one RPC carrying every stale id in the body, no duplicates.
    assert len(archive_calls) == 1
    archived_ids = archive_calls[0].args[1]["p_ids"]
    assert sorted(archived_ids) == sorted(r["id"] for r in existing)


# ---- Phase 1 triage: known jobs are not re-triaged --------------------------


@pytest.mark.asyncio
async def test_phase1_triage_skips_known_external_ids(monkeypatch):
    """Jobs already in the DB must not be re-sent to the Phase 1 LLM on
    every cycle — only new external_ids are triaged, and verdict indices
    map back to the jobs' global positions."""
    from unittest.mock import AsyncMock

    from app.config import settings as live_settings
    from app.services import poller as poller_mod
    from app.services.relevance.title_triage import TitleVerdict

    monkeypatch.setattr(live_settings, "phase1_triage_enabled", True)

    existing = [
        {"id": "job-1", "external_id": "known-1", "title": "Old Role", "company_name": "Acme"},
    ]
    supabase, _jobs_table, _sources_table = _make_poll_supabase(existing)

    async def two_job_fetch(_token: str) -> list[StandardJob]:
        return [
            StandardJob(
                external_id="known-1",
                title="Old Role",
                location_name="Remote",
                department=None,
                content="",
                updated_at="2026-01-01",
                absolute_url="https://example.com/j/1",
            ),
            StandardJob(
                external_id="new-1",
                title="Brand New Role",
                location_name="Remote",
                department=None,
                content="",
                updated_at="2026-01-01",
                absolute_url="https://example.com/j/2",
            ),
        ]

    # The NEW title must pass the free gates (which now run before
    # triage), while the KNOWN one must not — so the only reason the
    # new job reaches triage and the known one doesn't is the
    # known-external-id skip under test.
    target = _target_with_keywords({"brand": 3}, ["brand new role"])
    # Triage rejects the (only) submitted title. Id 1 = first title in the
    # submitted subset, which must be the NEW job.
    fake_triage = AsyncMock(return_value=({1: TitleVerdict(id=1, promising=False)}, None))

    monkeypatch.setitem(poller_mod.FETCHERS, "greenhouse", two_job_fetch)
    monkeypatch.setattr(poller_mod, "get_active_target", lambda _sb: [target])
    monkeypatch.setattr(poller_mod, "get_llm_client", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(poller_mod, "triage_titles", fake_triage)

    # Permissive payer-budget gate — this test is about triage scoping,
    # not allowance enforcement.
    open_gate = MagicMock()
    open_gate.target_blocked.return_value = False

    summary = await poller_mod._poll_one_source(
        dict(_GUARD_SOURCE), supabase, budget_gate=open_gate
    )

    assert summary["polled"] is True
    assert summary["error"] is None
    # Exactly one triage call, carrying ONLY the new job's title.
    assert fake_triage.await_count == 1
    assert fake_triage.await_args.kwargs["titles"] == ["Brand New Role"]
    # Neither job reaches the upsert (new one rejected by Phase 1, known
    # one fails the title prematch), so nothing was scored this cycle.
    assert summary["new"] == 0
    assert summary["updated"] == 0


async def _run_triage_with_budget(monkeypatch, *, exhausted, fake_triage) -> tuple[dict, MagicMock]:
    """Drive ``_poll_one_source`` through the Phase-1 triage path with the
    global-budget re-check stubbed to ``exhausted`` (a predicate or an
    exception-raiser). Returns the poll summary."""
    from app.config import settings as live_settings
    from app.services import poller as poller_mod
    from app.services.relevance.title_triage import TitleVerdict  # noqa: F401

    monkeypatch.setattr(live_settings, "phase1_triage_enabled", True)
    supabase, _jobs, _sources = _make_poll_supabase([])

    async def one_new_job(_token: str) -> list[StandardJob]:
        return [
            StandardJob(
                external_id="new-1",
                title="Brand New Role",
                location_name="Remote",
                department=None,
                content="",
                updated_at="2026-01-01",
                absolute_url="https://example.com/j/2",
            )
        ]

    target = _target_with_keywords({"brand": 3}, ["brand new role"])
    monkeypatch.setitem(poller_mod.FETCHERS, "greenhouse", one_new_job)
    monkeypatch.setattr(poller_mod, "get_active_target", lambda _sb: [target])
    monkeypatch.setattr(poller_mod, "get_llm_client", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(poller_mod, "triage_titles", fake_triage)
    monkeypatch.setattr(poller_mod, "_global_budget_exhausted", exhausted)

    open_gate = MagicMock()
    open_gate.target_blocked.return_value = False
    summary = await poller_mod._poll_one_source(
        dict(_GUARD_SOURCE), supabase, budget_gate=open_gate
    )
    return summary, _jobs


@pytest.mark.asyncio
async def test_phase1_triage_defers_when_global_budget_exhausted(monkeypatch):
    """When the global daily cap is reached, the per-batch re-check stops
    Phase 1 triage mid-cycle: ``triage_titles`` is never called AND the
    un-triaged job is DEFERRED, not ingested (#285 f/u). It re-polls and
    re-triages next cycle once the budget resets — there is no admit-everything
    fallback on budget exhaustion (prod was ~55% fail-open on budget, all
    off-target)."""
    from unittest.mock import AsyncMock

    fake_triage = AsyncMock()  # must NOT be awaited
    summary, jobs_table = await _run_triage_with_budget(
        monkeypatch, exhausted=lambda _sb: True, fake_triage=fake_triage
    )

    assert summary["error"] is None
    assert fake_triage.await_count == 0  # gate stopped the spend
    # THE fix, end-to-end: a budget-deferred (never-triaged) job is DROPPED
    # from the upsert — nothing is written, so it re-polls + re-triages next
    # cycle. Under the old fail-open it WOULD have been upserted (admitted
    # blind). (summary["new"] is mock-gated to 0 here regardless, so we assert
    # on the upsert call itself — that's what the gate actually controls.)
    jobs_table.upsert.assert_not_called()


@pytest.mark.asyncio
async def test_phase1_triage_fails_open_on_budget_read_error(monkeypatch):
    """A spend-meter read error during triage must NOT break the poll and must
    NOT silently drop the precision filter — triage proceeds (fail-open),
    because the once-per-cycle breaker already bounds the blast radius."""
    from unittest.mock import AsyncMock

    from app.models.llm import LLMResult, LLMUsage
    from app.services.relevance.title_triage import TitleVerdict

    def boom(_sb: object) -> bool:
        raise RuntimeError("spend meter unreachable")

    # A real result (not None): the read error doesn't stop the LLM call, which
    # succeeds and returns a promising verdict → the job ingests as before.
    ok = LLMResult(
        content="{}", model="claude-haiku-4-5", usage=LLMUsage(), cost_usd=0.0, latency_ms=1
    )
    fake_triage = AsyncMock(return_value=({1: TitleVerdict(id=1, promising=True)}, ok))
    summary, jobs_table = await _run_triage_with_budget(
        monkeypatch, exhausted=boom, fake_triage=fake_triage
    )

    assert summary["error"] is None
    assert fake_triage.await_count == 1  # fail-open: triage still ran
    jobs_table.upsert.assert_called()  # real result → attempted → promising job ingests


@pytest.mark.asyncio
async def test_phase1_triage_defers_when_the_llm_call_fails(monkeypatch):
    """Budget is fine so triage RUNS, but the LLM call FAILS (result None — e.g.
    OpenRouter 401 / spent credit-limit / error): the un-triaged job DEFERS, not
    fail-open admits. A dead key / spent cap must PAUSE the pipeline, never flood
    100% admit — the exact prod incident that motivated this."""
    from unittest.mock import AsyncMock

    fake_triage = AsyncMock(return_value=({}, None))  # attempted, but FAILED
    summary, jobs_table = await _run_triage_with_budget(
        monkeypatch, exhausted=lambda _sb: False, fake_triage=fake_triage
    )

    assert summary["error"] is None
    assert fake_triage.await_count == 1  # the call WAS made
    jobs_table.upsert.assert_not_called()  # failed → deferred, NOT a fail-open flood


# ---- alert-row refresh ------------------------------------------------------


@pytest.mark.asyncio
async def test_load_alert_rows_returns_post_scoring_state():
    """Regression: alerts dispatched with the upsert-time rows, where
    ``score`` is the column default 0 — so no alert ever cleared the
    threshold. The refresh must return the DB rows written by scoring,
    re-read via the ``get_jobs_by_ids`` RPC (#93: ids in the jsonb body,
    not the request URL)."""
    from app.services.poller import _load_alert_rows

    stale = [{"id": "job-1", "title": "T1", "score": 0}]
    refreshed_row = {"id": "job-1", "title": "T1", "score": 88}

    supabase = MagicMock()
    supabase.rpc.return_value.execute.return_value.data = [refreshed_row]

    rows = await _load_alert_rows(supabase, stale)

    assert rows == [refreshed_row]
    supabase.rpc.assert_called_once_with("get_jobs_by_ids", {"p_ids": ["job-1"]})


@pytest.mark.asyncio
async def test_load_alert_rows_falls_back_to_stale_rows_on_error():
    from app.services.poller import _load_alert_rows

    stale = [{"id": "job-1", "score": 0}]
    supabase = MagicMock()
    supabase.rpc.side_effect = RuntimeError("db down")

    rows = await _load_alert_rows(supabase, stale)

    assert rows == stale


@pytest.mark.asyncio
async def test_load_alert_rows_no_ids_short_circuits():
    from app.services.poller import _load_alert_rows

    supabase = MagicMock()
    rows = await _load_alert_rows(supabase, [{"title": "no id"}])

    assert rows == [{"title": "no id"}]
    supabase.rpc.assert_not_called()


# ---- id lists ride the RPC jsonb body, not the request URL (#93) ------------


@pytest.mark.asyncio
async def test_batch_fetch_job_scores_uses_rpc_body() -> None:
    """A large score lookup is ONE ``get_job_scores_by_ids`` RPC with the
    full id list in the jsonb body (no URL ``.in_()`` chunking), folded into
    the same ``{id: score}`` dict the chunked read produced."""
    from app.services.poller import _batch_fetch_job_scores

    job_ids = [f"job-{i}" for i in range(450)]
    rpc_rows = [{"id": jid, "score": idx} for idx, jid in enumerate(job_ids)]

    supabase = MagicMock()
    supabase.rpc.return_value.execute.return_value.data = rpc_rows

    scores = await _batch_fetch_job_scores(supabase, job_ids)

    supabase.rpc.assert_called_once_with("get_job_scores_by_ids", {"p_ids": job_ids})
    # Every id keyed once, identical to the old single-query result.
    assert len(scores) == 450
    assert scores["job-0"] == 0
    assert scores["job-449"] == 449


@pytest.mark.asyncio
async def test_load_alert_rows_uses_rpc_body() -> None:
    """A large alert refresh is ONE ``get_jobs_by_ids`` RPC with the full id
    list in the jsonb body (no URL ``.in_()`` chunking); the returned rows
    pass through unchanged."""
    from app.services.poller import _load_alert_rows

    new_rows = [{"id": f"job-{i}"} for i in range(450)]
    ids = [r["id"] for r in new_rows]
    rpc_rows = [{"id": jid, "score": 90} for jid in ids]

    supabase = MagicMock()
    supabase.rpc.return_value.execute.return_value.data = rpc_rows

    refreshed = await _load_alert_rows(supabase, new_rows)

    supabase.rpc.assert_called_once_with("get_jobs_by_ids", {"p_ids": ids})
    assert refreshed == rpc_rows
    assert {r["id"] for r in refreshed} == set(ids)


# ---- free gates run before Phase 1 triage (cost ordering) -------------------


def _job(external_id: str, title: str, location: str) -> StandardJob:
    return StandardJob(
        external_id=external_id,
        title=title,
        location_name=location,
        department=None,
        content="",
        updated_at="2026-01-01",
        absolute_url=f"https://example.com/j/{external_id}",
    )


@pytest.mark.asyncio
async def test_phase1_triage_only_sees_free_gate_survivors(monkeypatch):
    """Titles the FREE gates reject (title prematch, non-US location)
    must never be sent to the Phase 1 LLM — classifying them is pure
    spend since the per-job loop drops them regardless of verdict."""
    from unittest.mock import AsyncMock

    from app.config import settings as live_settings
    from app.services import poller as poller_mod

    monkeypatch.setattr(live_settings, "phase1_triage_enabled", True)
    monkeypatch.setattr(live_settings, "validate_poll_urls", False)

    supabase, jobs_table, _sources_table = _make_poll_supabase([])
    jobs_table.upsert.return_value.execute.return_value.data = []

    async def fetch(_token: str) -> list[StandardJob]:
        return [
            _job("n1", "Marketing Specialist", "Remote"),  # prematch miss
            _job("n2", "Director of Customer Experience", "London, United Kingdom"),
            _job("n3", "Director of Customer Experience", "Remote"),  # survivor
        ]

    target = _target_with_keywords({"Zendesk": 3}, ["director of customer experience"])
    fake_triage = AsyncMock(return_value=({}, None))

    monkeypatch.setitem(poller_mod.FETCHERS, "greenhouse", fetch)
    monkeypatch.setattr(poller_mod, "get_llm_client", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(poller_mod, "triage_titles", fake_triage)

    open_gate = MagicMock()
    open_gate.target_blocked.return_value = False

    summary = await poller_mod._poll_one_source(
        dict(_GUARD_SOURCE),
        supabase,
        budget_gate=open_gate,
        active_targets=[target],
        stage3_users=({}, {}),
    )

    assert summary["polled"] is True
    assert summary["error"] is None
    # Exactly one triage call, carrying ONLY the survivor's title.
    assert fake_triage.await_count == 1
    assert fake_triage.await_args.kwargs["titles"] == ["Director of Customer Experience"]


@pytest.mark.asyncio
async def test_phase1_verdicts_keyed_by_original_indices_after_free_gates(monkeypatch):
    """Verdicts come back keyed by position WITHIN the triaged subset;
    they must be remapped to the jobs' ORIGINAL 1-based indices so the
    per-job loop and Stage 2 lookups keep working."""
    from unittest.mock import AsyncMock

    from app.config import settings as live_settings
    from app.services import poller as poller_mod
    from app.services.relevance.title_triage import TitleVerdict

    monkeypatch.setattr(live_settings, "phase1_triage_enabled", True)
    monkeypatch.setattr(live_settings, "validate_poll_urls", False)

    supabase, jobs_table, _sources_table = _make_poll_supabase([])
    jobs_table.upsert.return_value.execute.return_value.data = []

    async def fetch(_token: str) -> list[StandardJob]:
        return [
            # idx 0: free-gate reject (non-US) — never triaged.
            _job("x0", "Director of Customer Experience", "Berlin, Germany"),
            # idx 1: survivor — subset position 1, rejected by the LLM.
            _job("s1", "Director of Customer Experience", "Remote"),
            # idx 2: survivor — subset position 2, admitted.
            _job("s2", "Head of CX", "Remote"),
        ]

    target = _target_with_keywords(
        {"Zendesk": 3}, ["director of customer experience", "head of cx"]
    )
    from app.models.llm import LLMResult, LLMUsage

    ok = LLMResult(
        content="{}", model="claude-haiku-4-5", usage=LLMUsage(), cost_usd=0.0, latency_ms=1
    )
    fake_triage = AsyncMock(
        return_value=(
            {
                1: TitleVerdict(id=1, promising=False),
                2: TitleVerdict(id=2, promising=True),
            },
            ok,  # a real result -> the batch is attempted -> verdicts apply
        )
    )

    monkeypatch.setitem(poller_mod.FETCHERS, "greenhouse", fetch)
    monkeypatch.setattr(poller_mod, "get_llm_client", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(poller_mod, "triage_titles", fake_triage)

    open_gate = MagicMock()
    open_gate.target_blocked.return_value = False

    summary = await poller_mod._poll_one_source(
        dict(_GUARD_SOURCE),
        supabase,
        budget_gate=open_gate,
        active_targets=[target],
        stage3_users=({}, {}),
    )

    assert summary["error"] is None
    # Only the LLM-admitted survivor reaches the upsert: subset id 1
    # mapped back to original idx 2 (s1, dropped) and subset id 2 to
    # original idx 3 (s2, admitted). A naive subset-as-global keying
    # would have dropped s2 instead.
    upserted = jobs_table.upsert.call_args.args[0]
    assert [r["external_id"] for r in upserted] == ["s2"]


# ---- targeted path: free gates before triage + Phase 2 ----------------------


def _make_targeted_poll_supabase() -> tuple[MagicMock, MagicMock, MagicMock]:
    """Mock Supabase for ``_poll_one_source_for_target`` runs.

    ``jobs.upsert`` returns no rows by default (override per test) and
    the ``user_targets`` junction is empty (no stage-3 users).
    """
    jobs_table = MagicMock()
    jobs_table.upsert.return_value.execute.return_value.data = []
    # #514: known-external-id read for Phase-1 admission scoping (this path
    # has no live-unengaged RPC read). Empty by default; override per test.
    jobs_table.select.return_value.eq.return_value.execute.return_value.data = []
    sources_table = MagicMock()
    user_targets_table = MagicMock()
    (
        user_targets_table.select.return_value.eq.return_value.in_.return_value.execute.return_value.data
    ) = []
    # #514: Stage-2 persisted-``promising`` floor read for refreshed rows.
    scores_table = MagicMock()
    (
        scores_table.select.return_value.eq.return_value.in_.return_value.execute.return_value.data
    ) = []
    supabase = MagicMock()
    supabase.table.side_effect = lambda name: {
        "jobs": jobs_table,
        "sources": sources_table,
        "user_targets": user_targets_table,
        "scores": scores_table,
    }[name]
    # #93: the legacy Stage 3 score pre-fetch (``_batch_fetch_job_scores``)
    # is the ``get_job_scores_by_ids`` RPC now, not ``jobs.select().in_()``.
    # Default it to an empty score set; tests override as needed.
    supabase.rpc.return_value.execute.return_value.data = []
    return supabase, jobs_table, user_targets_table


@pytest.mark.asyncio
async def test_targeted_triage_only_sees_free_gate_survivors(monkeypatch):
    """``_poll_one_source_for_target`` mirrors the shared path: keyword
    misses and non-US locations are dropped for free BEFORE the Phase 1
    LLM call, and verdicts stay keyed by original indices."""
    from unittest.mock import AsyncMock

    from app.config import settings as live_settings
    from app.services import poller as poller_mod

    monkeypatch.setattr(live_settings, "phase1_triage_enabled", True)
    monkeypatch.setattr(live_settings, "validate_poll_urls", False)

    supabase, _jobs_table, _user_targets = _make_targeted_poll_supabase()

    async def fetch(_token: str) -> list[StandardJob]:
        return [
            _job("k1", "Office Manager", "Remote"),  # keyword miss
            _job("k2", "Staff Frontend Engineer", "Berlin, Germany"),  # non-US
            _job("k3", "Staff Frontend Engineer", "Remote"),  # survivor
        ]

    target = _full_target(is_active=True, search_keywords=["frontend engineer"])
    fake_triage = AsyncMock(return_value=({}, None))

    monkeypatch.setitem(poller_mod.FETCHERS, "greenhouse", fetch)
    monkeypatch.setattr(poller_mod, "get_llm_client", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(poller_mod, "triage_titles", fake_triage)

    summary = await poller_mod._poll_one_source_for_target(
        dict(_GUARD_SOURCE), supabase, target, payer_user_id="payer-1"
    )

    assert summary["polled"] is True
    assert summary["error"] is None
    assert fake_triage.await_count == 1
    assert fake_triage.await_args.kwargs["titles"] == ["Staff Frontend Engineer"]


def _upserted_row() -> dict:
    return {
        "id": "j1",
        "external_id": "k3",
        "title": "Staff Frontend Engineer",
        "description_html": "",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:01Z",
    }


def _wire_targeted_stage3(monkeypatch, *, phase2_enabled: bool):
    """Common harness for the targeted Stage-3 flag tests. Returns
    ``(supabase, fake_phase2, fake_legacy, target)``."""
    from unittest.mock import AsyncMock

    from app.config import settings as live_settings
    from app.services import poller as poller_mod

    monkeypatch.setattr(live_settings, "phase1_triage_enabled", False)
    monkeypatch.setattr(live_settings, "phase2_enabled", phase2_enabled)
    monkeypatch.setattr(live_settings, "validate_poll_urls", False)

    supabase, jobs_table, user_targets = _make_targeted_poll_supabase()
    jobs_table.upsert.return_value.execute.return_value.data = [_upserted_row()]
    # Legacy Stage 3's pre-fetch of stage-2 scores is the
    # ``get_job_scores_by_ids`` RPC now (#93); the harness defaults it to an
    # empty score set in ``_make_targeted_poll_supabase``.
    (
        user_targets.select.return_value.eq.return_value.in_.return_value.execute.return_value.data
    ) = [{"target_id": "t-1", "user_id": "u1"}]

    async def fetch(_token: str) -> list[StandardJob]:
        return [_job("k3", "Staff Frontend Engineer", "Remote")]

    target = _full_target(is_active=True, search_keywords=["frontend engineer"])
    doc = MagicMock()
    doc.payload = {"profile": "stub"}

    fake_phase2 = AsyncMock(return_value=1)
    fake_legacy = AsyncMock()

    monkeypatch.setitem(poller_mod.FETCHERS, "greenhouse", fetch)
    monkeypatch.setattr(poller_mod, "get_llm_client", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(poller_mod, "get_latest_optimized", lambda _sb, _uid: doc)
    # The scoring helpers are the async ``*_poll`` seam variants now (#57) —
    # the poller awaits them directly, so their stand-ins must be awaitable.
    monkeypatch.setattr(poller_mod, "target_title_score_and_upsert", AsyncMock())
    monkeypatch.setattr(poller_mod, "target_score_and_upsert", AsyncMock())
    monkeypatch.setattr(poller_mod, "batch_update_global_scores", AsyncMock())
    monkeypatch.setattr(poller_mod, "run_phase2_for_jobs", fake_phase2)
    monkeypatch.setattr(poller_mod, "_run_llm_scoring_for_row", fake_legacy)

    return supabase, fake_phase2, fake_legacy, target


@pytest.mark.asyncio
async def test_targeted_stage3_uses_phase2_when_flag_on(monkeypatch):
    """The activation path must stop defaulting to the legacy Sonnet
    full-JD call ($0.038/job): with ``phase2_enabled`` it runs the same
    capped ``run_phase2_for_jobs`` grading as ``_poll_one_source``."""
    from app.services import poller as poller_mod

    supabase, fake_phase2, fake_legacy, target = _wire_targeted_stage3(
        monkeypatch, phase2_enabled=True
    )

    summary = await poller_mod._poll_one_source_for_target(
        dict(_GUARD_SOURCE), supabase, target, payer_user_id="payer-1"
    )

    assert summary["error"] is None
    assert fake_phase2.await_count == 1
    kwargs = fake_phase2.await_args.kwargs
    assert kwargs["target"] is target
    assert kwargs["user_id"] == "payer-1"
    assert [j["id"] for j in kwargs["jobs"]] == ["j1"]
    fake_legacy.assert_not_awaited()


@pytest.mark.asyncio
async def test_targeted_stage3_legacy_fallback_when_flag_off(monkeypatch):
    """Flag off → the legacy Stage 3 path runs unchanged."""
    from app.services import poller as poller_mod

    supabase, fake_phase2, fake_legacy, target = _wire_targeted_stage3(
        monkeypatch, phase2_enabled=False
    )

    summary = await poller_mod._poll_one_source_for_target(
        dict(_GUARD_SOURCE), supabase, target, payer_user_id="payer-1"
    )

    assert summary["error"] is None
    assert fake_legacy.await_count == 1
    fake_phase2.assert_not_awaited()


@pytest.mark.asyncio
async def test_targeted_grading_uses_payer_byok_key(monkeypatch):
    """#5 P3: background grading resolves the LLM client on the payer's own
    OpenRouter key (``get_client(supabase, payer)``), not the instance key."""
    from app.services import poller as poller_mod

    supabase, fake_phase2, _fake_legacy, target = _wire_targeted_stage3(
        monkeypatch, phase2_enabled=True
    )
    seen_payers: list[str | None] = []
    monkeypatch.setattr(
        poller_mod,
        "get_llm_client",
        lambda _sb, user_id: seen_payers.append(user_id) or MagicMock(),
    )

    summary = await poller_mod._poll_one_source_for_target(
        dict(_GUARD_SOURCE), supabase, target, payer_user_id="payer-1"
    )

    assert summary["error"] is None
    assert fake_phase2.await_count == 1
    assert seen_payers == ["payer-1"]


@pytest.mark.asyncio
async def test_targeted_grading_deferred_when_payer_has_no_byok_key(monkeypatch):
    """#5 P3: hosted require-mode with no stored key → grading defers
    gracefully (no exception, jobs still ingest, never billing the operator
    key), exactly like the over-allowance defer."""
    from app.services import poller as poller_mod
    from app.services.llm import MissingUserKeyError

    supabase, fake_phase2, _fake_legacy, target = _wire_targeted_stage3(
        monkeypatch, phase2_enabled=True
    )

    def _no_key(_sb, _uid):
        raise MissingUserKeyError("openrouter")

    monkeypatch.setattr(poller_mod, "get_llm_client", _no_key)

    summary = await poller_mod._poll_one_source_for_target(
        dict(_GUARD_SOURCE), supabase, target, payer_user_id="payer-1"
    )

    assert summary["error"] is None  # graceful defer, not a poll failure
    fake_phase2.assert_not_awaited()  # no grading on the operator's key


# ---- adaptive cadence: last_candidate_at stamp ------------------------------


@pytest.mark.asyncio
async def test_last_candidate_at_stamped_when_candidates_upserted(monkeypatch):
    """A poll that produces at least one ingestible candidate must stamp
    ``sources.last_candidate_at`` alongside ``last_polled_at`` so the
    lifecycle cadence sweep can tell productive sources from cold ones."""
    from app.config import settings as live_settings
    from app.services import poller as poller_mod

    monkeypatch.setattr(live_settings, "validate_poll_urls", False)

    supabase, jobs_table, sources_table = _make_poll_supabase([])
    jobs_table.upsert.return_value.execute.return_value.data = []

    async def fetch(_token: str) -> list[StandardJob]:
        return [
            StandardJob(
                external_id="c-1",
                title="Brand New Role",
                location_name="Remote",
                department=None,
                content="",
                updated_at="2026-01-01",
                absolute_url="https://example.com/j/1",
            )
        ]

    target = _target_with_keywords({"brand": 3}, ["brand new role"])
    monkeypatch.setitem(poller_mod.FETCHERS, "greenhouse", fetch)

    open_gate = MagicMock()
    open_gate.target_blocked.return_value = False

    summary = await poller_mod._poll_one_source(
        dict(_GUARD_SOURCE),
        supabase,
        budget_gate=open_gate,
        active_targets=[target],
        stage3_users=({}, {}),
    )

    assert summary["error"] is None
    payload = sources_table.update.call_args.args[0]
    assert "last_candidate_at" in payload
    assert "last_polled_at" in payload


@pytest.mark.asyncio
async def test_no_candidates_leaves_last_candidate_at_unstamped(monkeypatch):
    """A poll that fetched jobs but produced no ingestible candidates
    must NOT touch the stamp — staleness is what the cadence sweep keys
    on."""
    from app.services import poller as poller_mod

    existing = [
        {"id": "job-1", "external_id": "gone-1", "title": "T1", "company_name": "Acme"},
    ]
    supabase, _jobs_table, sources_table = _make_poll_supabase(existing)

    async def fetch(_token: str) -> list[StandardJob]:
        return [
            StandardJob(
                external_id="c-1",
                title="Director of CX",
                location_name="London, United Kingdom",  # dropped at the US gate
                department=None,
                content="",
                updated_at="2026-01-01",
                absolute_url="https://example.com/j/1",
            )
        ]

    monkeypatch.setitem(poller_mod.FETCHERS, "greenhouse", fetch)
    monkeypatch.setattr(poller_mod, "get_active_target", lambda _sb: [])

    summary = await poller_mod._poll_one_source(dict(_GUARD_SOURCE), supabase)

    assert summary["error"] is None
    payload = sources_table.update.call_args.args[0]
    assert "last_candidate_at" not in payload
    assert "last_polled_at" in payload


# ---- POLLER_ASYNC_DB flag-on: the whole cycle rides the async client (#57) --


class _SyncClientForbidden:
    """Stand-in for the sync service-role client that MUST stay untouched
    when the flag is on and the async client is up: any attribute access is
    a test failure. The poller still passes this object around (it is the
    fail-safe fallback), but no seam call may build a query on it."""

    def __getattr__(self, name: str):  # pragma: no cover - failure path
        raise AssertionError(f"sync supabase client touched via .{name} with flag on")


class _AsyncChain:
    """One query chain on the fake async client: records ops, returns self
    for any builder method, async-executes via the parent's router."""

    def __init__(self, parent: "_AsyncSeamClient", ops: list) -> None:
        self._parent = parent
        self.ops = ops

    def __getattr__(self, name: str):
        if name == "not_":
            self.ops.append(("not_",))
            return self

        def _record(*args, **kwargs):
            self.ops.append((name, args, kwargs))
            return self

        return _record

    async def execute(self):
        self._parent.executed.append(self.ops)
        return self._parent.respond(self.ops)


class _AsyncSeamClient:
    """Generic async supabase stand-in: every table()/rpc() starts a fresh
    recorded chain; ``respond`` synthesizes realistic response shapes for
    the queries one ``_poll_one_source`` cycle issues."""

    def __init__(self) -> None:
        self.executed: list[list] = []

    def table(self, name: str) -> _AsyncChain:
        return _AsyncChain(self, [("table", name)])

    def rpc(self, name: str, params: dict) -> _AsyncChain:
        return _AsyncChain(self, [("rpc", name, params)])

    @staticmethod
    def _resp(data):
        resp = MagicMock()
        resp.data = data
        return resp

    def respond(self, ops: list):
        head = ops[0]
        if head[0] == "rpc":
            # source_live_unengaged_jobs / get_jobs_by_ids /
            # get_job_scores_by_ids / bulk_update_scores → empty sets are
            # valid for a first-poll cycle.
            return self._resp([])
        table = head[1]
        op_names = {o[0] for o in ops[1:]}
        if table == "jobs" and "upsert" in op_names:
            rows = next(o[1][0] for o in ops[1:] if o[0] == "upsert")
            return self._resp(
                [
                    {
                        **row,
                        "id": f"job-{row['external_id']}",
                        "created_at": "2026-07-15T00:00:00Z",
                        "updated_at": "2026-07-15T00:00:00Z",
                    }
                    for row in rows
                ]
            )
        if table == "scores" and "upsert" in op_names:
            row = next(o[1][0] for o in ops[1:] if o[0] == "upsert")
            return self._resp(
                [
                    {
                        **row,
                        "id": f"score-{row['job_posting_id']}",
                        "created_at": "2026-07-15T00:00:00Z",
                        "updated_at": "2026-07-15T00:00:00Z",
                    }
                ]
            )
        # selects (purge guard, etc.) and updates (mark-polled) — empty data.
        return self._resp([])

    def chains(self, *prefix) -> list[list]:
        """Executed chains whose head matches ``prefix`` (e.g. ("table",
        "jobs"))."""
        return [ops for ops in self.executed if tuple(ops[0][: len(prefix)]) == prefix]


@pytest.mark.asyncio
async def test_flag_on_full_source_poll_runs_entirely_on_async_client(monkeypatch):
    """End-to-end ``_poll_one_source`` with POLLER_ASYNC_DB on: every DB
    touch (existing-rows read, purge guard, jobs upsert, Stage 1/2 score
    upserts through the ``*_poll`` seam variants, global-score RPC,
    mark-polled update) must execute on the async client; the sync client
    must never even be attribute-accessed."""
    from unittest.mock import AsyncMock

    from app.config import settings as live_settings
    from app.services import db_write
    from app.services import poller as poller_mod
    from app.services.targets.payers import PayerBudgetGate

    monkeypatch.setattr(live_settings, "phase1_triage_enabled", False)
    monkeypatch.setattr(live_settings, "phase2_enabled", False)
    monkeypatch.setattr(live_settings, "validate_poll_urls", False)
    monkeypatch.setattr(live_settings, "qualification_enabled", False)
    monkeypatch.setattr(live_settings, "prescan_embed_enabled", False)
    monkeypatch.setattr(live_settings, "prescan_shadow_enabled", False)
    monkeypatch.setattr(live_settings, "recency_decay_enabled", False)
    monkeypatch.setattr(db_write.settings, "poller_async_db", True)

    async_client = _AsyncSeamClient()
    monkeypatch.setattr(db_write, "get_async_supabase", lambda: async_client)
    # poll_lock and any other module-level import of the getter must agree,
    # but _poll_one_source itself never takes the lock — db_write is enough.

    async def fetch(_token: str) -> list[StandardJob]:
        return [
            _job("a-1", "Staff Frontend Engineer", "Remote, USA"),
            _job("a-2", "Senior React Engineer", "New York, NY"),
        ]

    monkeypatch.setitem(poller_mod.FETCHERS, "greenhouse", fetch)
    monkeypatch.setattr(
        poller_mod,
        "notify",
        MagicMock(send_alerts_for_new_jobs=AsyncMock(), send_sms_alerts_for_new_jobs=AsyncMock()),
    )

    target = _full_target(is_active=True, search_keywords=["frontend engineer", "react"])
    sync_client = _SyncClientForbidden()

    summary = await poller_mod._poll_one_source(
        dict(_GUARD_SOURCE),
        sync_client,
        PayerBudgetGate(),
        active_targets=[target],
        stage3_users=({}, {}),
    )

    assert summary["error"] is None
    assert summary["polled"] is True
    assert summary["new"] == 2

    # The jobs upsert ran on the async client with both rows.
    jobs_upserts = [
        ops
        for ops in async_client.chains("table", "jobs")
        if any(o[0] == "upsert" for o in ops[1:])
    ]
    assert len(jobs_upserts) == 1
    upserted = next(o[1][0] for o in jobs_upserts[0][1:] if o[0] == "upsert")
    assert [r["external_id"] for r in upserted] == ["a-1", "a-2"]

    # Stage 1 + Stage 2 score upserts (2 jobs x 1 target x 2 stages).
    score_upserts = [
        ops
        for ops in async_client.chains("table", "scores")
        if any(o[0] == "upsert" for o in ops[1:])
    ]
    assert len(score_upserts) == 4

    # Global-score aggregation RPC ran on the async client too.
    assert async_client.chains("rpc", "bulk_update_scores")

    # Mark-polled update landed on sources via the async client.
    source_updates = [
        ops
        for ops in async_client.chains("table", "sources")
        if any(o[0] == "update" for o in ops[1:])
    ]
    assert source_updates, "mark-polled update must ride the async client"


# ---- #514: known jobs must survive the Phase-1 admit gate -------------------


@pytest.mark.asyncio
async def test_known_job_refreshes_when_phase1_rejects_a_candidate(monkeypatch):
    """#514 regression — the prod starvation. A cycle that triages ≥1 NEW
    candidate must still conflict-update already-known rows: the #285
    budget-defer rule treated a known job (never a triage candidate, so no
    attempt entry) as budget-deferred and dropped it from the upsert. Any
    source with a standing rejected candidate therefore never refreshed
    its known rows — board-side JD edits, the escaped-HTML heal, and
    salary re-extraction all silently stopped propagating."""
    from unittest.mock import AsyncMock

    from app.config import settings as live_settings
    from app.services import poller as poller_mod
    from app.services.relevance.title_triage import TitleVerdict

    monkeypatch.setattr(live_settings, "phase1_triage_enabled", True)
    monkeypatch.setattr(live_settings, "validate_poll_urls", False)

    existing = [
        {
            "id": "job-known",
            "external_id": "known-1",
            "title": "Brand New Role",
            "company_name": "Acme",
        }
    ]
    supabase, jobs_table, _sources = _make_poll_supabase(existing)

    async def fetch(_token: str) -> list[StandardJob]:
        return [
            StandardJob(
                external_id="known-1",
                title="Brand New Role",
                location_name="Remote",
                department=None,
                content="<p>edited JD with a shiny new $100k salary</p>",
                updated_at="2026-01-02",
                absolute_url="https://example.com/j/1",
            ),
            StandardJob(
                external_id="new-1",
                title="Brand New Role II",
                location_name="Remote",
                department=None,
                content="",
                updated_at="2026-01-02",
                absolute_url="https://example.com/j/2",
            ),
        ]

    target = _target_with_keywords({"brand": 3}, ["brand new role"])
    # The single candidate (the NEW job) is rejected → verdicts are
    # non-empty, which is exactly the state that starved known rows.
    fake_triage = AsyncMock(return_value=({1: TitleVerdict(id=1, promising=False)}, None))

    monkeypatch.setitem(poller_mod.FETCHERS, "greenhouse", fetch)
    monkeypatch.setattr(poller_mod, "get_active_target", lambda _sb: [target])
    monkeypatch.setattr(poller_mod, "get_llm_client", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(poller_mod, "triage_titles", fake_triage)

    open_gate = MagicMock()
    open_gate.target_blocked.return_value = False
    summary = await poller_mod._poll_one_source(
        dict(_GUARD_SOURCE), supabase, budget_gate=open_gate
    )

    assert summary["error"] is None
    # Known title never re-triaged (unchanged behavior).
    assert fake_triage.await_args.kwargs["titles"] == ["Brand New Role II"]
    # THE fix: the known row is refreshed; the rejected candidate stays out.
    jobs_table.upsert.assert_called_once()
    payload = jobs_table.upsert.call_args.args[0]
    assert [r["external_id"] for r in payload] == ["known-1"]
    # Every upsert row carries the structured salary columns (the
    # ``salary_columns`` spread) so text and parts can never diverge.
    assert {"salary_min", "salary_max", "salary_currency", "salary_period"} <= set(payload[0])


@pytest.mark.asyncio
async def test_engaged_job_refreshes_despite_missing_from_unengaged_view(monkeypatch):
    """#514: admission scoping keys on the source's FULL external_id set,
    not the live-unengaged RPC view — a saved/applied (engaged) row is
    still a known row. Before the widening it re-entered triage as a
    candidate and could be starved by a rejecting verdict exactly like the
    unengaged case."""
    from unittest.mock import AsyncMock

    from app.config import settings as live_settings
    from app.services import poller as poller_mod
    from app.services.relevance.title_triage import TitleVerdict

    monkeypatch.setattr(live_settings, "phase1_triage_enabled", True)
    monkeypatch.setattr(live_settings, "validate_poll_urls", False)

    # Unengaged view is EMPTY — the engaged row only appears in the full set.
    supabase, jobs_table, _sources = _make_poll_supabase([])
    jobs_table.select.return_value.eq.return_value.execute.return_value.data = [
        {"external_id": "engaged-1"}
    ]

    async def fetch(_token: str) -> list[StandardJob]:
        return [
            StandardJob(
                external_id="engaged-1",
                title="Brand New Role",
                location_name="Remote",
                department=None,
                content="<p>fresh content for the row the user saved</p>",
                updated_at="2026-01-02",
                absolute_url="https://example.com/j/1",
            ),
            StandardJob(
                external_id="new-1",
                title="Brand New Role II",
                location_name="Remote",
                department=None,
                content="",
                updated_at="2026-01-02",
                absolute_url="https://example.com/j/2",
            ),
        ]

    target = _target_with_keywords({"brand": 3}, ["brand new role"])
    fake_triage = AsyncMock(return_value=({1: TitleVerdict(id=1, promising=False)}, None))

    monkeypatch.setitem(poller_mod.FETCHERS, "greenhouse", fetch)
    monkeypatch.setattr(poller_mod, "get_active_target", lambda _sb: [target])
    monkeypatch.setattr(poller_mod, "get_llm_client", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(poller_mod, "triage_titles", fake_triage)

    open_gate = MagicMock()
    open_gate.target_blocked.return_value = False
    summary = await poller_mod._poll_one_source(
        dict(_GUARD_SOURCE), supabase, budget_gate=open_gate
    )

    assert summary["error"] is None
    # The engaged title is known → not a triage candidate (LLM sees only
    # the genuinely new title).
    assert fake_triage.await_args.kwargs["titles"] == ["Brand New Role II"]
    jobs_table.upsert.assert_called_once()
    payload = jobs_table.upsert.call_args.args[0]
    assert [r["external_id"] for r in payload] == ["engaged-1"]


@pytest.mark.asyncio
async def test_known_job_stage2_preserves_phase1_floor(monkeypatch):
    """#514: Stage 2 must not re-litigate Phase 1 for a refreshed known row.
    The persisted ``promising`` verdict is the floor (False → the
    (job, target) pair stays excluded, mirroring bulk_score_for_target's
    re-score contract); the stored verdict/confidence columns are left
    untouched (None args). A True/NULL floor scores un-floored."""
    from unittest.mock import AsyncMock

    from app.config import settings as live_settings
    from app.services import poller as poller_mod
    from app.services.relevance.title_triage import TitleVerdict

    monkeypatch.setattr(live_settings, "phase1_triage_enabled", True)
    monkeypatch.setattr(live_settings, "validate_poll_urls", False)

    existing = [
        {
            "id": "job-a",
            "external_id": "known-a",
            "title": "Brand New Role",
            "company_name": "Acme",
        },
        {
            "id": "job-b",
            "external_id": "known-b",
            "title": "Brand New Role Live",
            "company_name": "Acme",
        },
    ]
    supabase, jobs_table, _sources = _make_poll_supabase(existing)
    # The conflict-update touches both known rows. job-b deliberately has
    # created_at == updated_at: a conflict-update never bumps
    # jobs.updated_at (no trigger, not in the payload), so EVERY refreshed
    # row keeps equal timestamps — known-ness must key on the pre-upsert
    # external_id set, or refreshed rows get misclassified as new and
    # defer-excluded on any cycle with verdicts.
    jobs_table.upsert.return_value.execute.return_value.data = [
        {
            "id": "job-a",
            "external_id": "known-a",
            "title": "Brand New Role",
            "description_html": "",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
        },
        {
            "id": "job-b",
            "external_id": "known-b",
            "title": "Brand New Role Live",
            "description_html": "",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        },
    ]
    # Persisted Phase-1 verdicts: job-a was rejected for this target,
    # job-b admitted.
    scores_table = supabase.table("scores")
    (
        scores_table.select.return_value.eq.return_value.in_.return_value.execute.return_value.data
    ) = [
        {"job_posting_id": "job-a", "promising": False},
        {"job_posting_id": "job-b", "promising": True},
    ]

    async def fetch(_token: str) -> list[StandardJob]:
        return [
            StandardJob(
                external_id="known-a",
                title="Brand New Role",
                location_name="Remote",
                department=None,
                content="",
                updated_at="2026-01-02",
                absolute_url="https://example.com/j/1",
            ),
            StandardJob(
                external_id="known-b",
                title="Brand New Role Live",
                location_name="Remote",
                department=None,
                content="",
                updated_at="2026-01-02",
                absolute_url="https://example.com/j/2",
            ),
            StandardJob(
                external_id="new-1",
                title="Brand New Role II",
                location_name="Remote",
                department=None,
                content="",
                updated_at="2026-01-02",
                absolute_url="https://example.com/j/3",
            ),
        ]

    target = _target_with_keywords({"brand": 3}, ["brand new role"])
    fake_triage = AsyncMock(return_value=({1: TitleVerdict(id=1, promising=False)}, None))
    fake_stage2 = AsyncMock()

    monkeypatch.setitem(poller_mod.FETCHERS, "greenhouse", fetch)
    monkeypatch.setattr(poller_mod, "get_active_target", lambda _sb: [target])
    monkeypatch.setattr(poller_mod, "get_llm_client", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(poller_mod, "triage_titles", fake_triage)
    monkeypatch.setattr(poller_mod, "target_title_score_and_upsert", AsyncMock())
    monkeypatch.setattr(poller_mod, "target_score_and_upsert", fake_stage2)
    monkeypatch.setattr(poller_mod, "batch_update_global_scores", AsyncMock())

    open_gate = MagicMock()
    open_gate.target_blocked.return_value = False
    summary = await poller_mod._poll_one_source(
        dict(_GUARD_SOURCE), supabase, budget_gate=open_gate
    )

    assert summary["error"] is None
    by_job = {c.kwargs["job_posting_id"]: c.kwargs for c in fake_stage2.await_args_list}
    assert set(by_job) == {"job-a", "job-b"}
    # promising=False floor → stays excluded; verdict columns untouched.
    assert by_job["job-a"]["excluded_by_prefilter"] is True
    assert by_job["job-a"]["promising"] is None
    assert by_job["job-a"]["phase1_confidence"] is None
    # promising=True floor → scores un-floored; columns untouched.
    assert by_job["job-b"]["excluded_by_prefilter"] is False
    assert by_job["job-b"]["promising"] is None


@pytest.mark.asyncio
async def test_targeted_poll_ingests_fail_open_when_payer_over_budget(monkeypatch):
    """#514: the targeted path documents over-budget as 'skip Phase-1 spend
    while still ingesting fail-open' — but with triage enabled and no LLM
    (``phase1_attempted`` empty) the gate dropped EVERY job, fail-closed.
    Free-gate survivors must ingest; grading defers."""
    from app.config import settings as live_settings
    from app.services import poller as poller_mod

    monkeypatch.setattr(live_settings, "phase1_triage_enabled", True)
    monkeypatch.setattr(live_settings, "validate_poll_urls", False)

    supabase, jobs_table, _user_targets = _make_targeted_poll_supabase()

    async def fetch(_token: str) -> list[StandardJob]:
        return [_job("k3", "Staff Frontend Engineer", "Remote")]

    target = _full_target(is_active=True, search_keywords=["frontend engineer"])
    monkeypatch.setitem(poller_mod.FETCHERS, "greenhouse", fetch)

    summary = await poller_mod._poll_one_source_for_target(
        dict(_GUARD_SOURCE),
        supabase,
        target,
        payer_user_id="payer-1",
        payer_over_budget=True,
    )

    assert summary["error"] is None
    jobs_table.upsert.assert_called_once()
    payload = jobs_table.upsert.call_args.args[0]
    assert [r["external_id"] for r in payload] == ["k3"]


@pytest.mark.asyncio
async def test_targeted_poll_known_job_bypasses_triage_and_refreshes(monkeypatch):
    """#514: the targeted path re-triaged KNOWN titles every cycle (pure
    LLM waste — the shared path's comment calls this out) and let a
    flipped verdict starve the row. Known external_ids are excluded from
    its candidates and bypass the admit gate, same as the shared path."""
    from unittest.mock import AsyncMock

    from app.config import settings as live_settings
    from app.models.llm import LLMResult, LLMUsage
    from app.services import poller as poller_mod
    from app.services.relevance.title_triage import TitleVerdict

    monkeypatch.setattr(live_settings, "phase1_triage_enabled", True)
    monkeypatch.setattr(live_settings, "validate_poll_urls", False)

    supabase, jobs_table, _user_targets = _make_targeted_poll_supabase()
    jobs_table.select.return_value.eq.return_value.execute.return_value.data = [
        {"external_id": "k-known"}
    ]

    async def fetch(_token: str) -> list[StandardJob]:
        return [
            _job("k-known", "Staff Frontend Engineer", "Remote"),
            _job("k-new", "Senior Frontend Engineer", "Remote"),
        ]

    target = _full_target(is_active=True, search_keywords=["frontend engineer"])
    # The one candidate (the NEW title) is rejected via a REAL LLM result —
    # a None result means a failed call, whose candidates defer un-attempted.
    ok = LLMResult(
        content="{}", model="claude-haiku-4-5", usage=LLMUsage(), cost_usd=0.0, latency_ms=1
    )
    fake_triage = AsyncMock(return_value=({1: TitleVerdict(id=1, promising=False)}, ok))

    monkeypatch.setitem(poller_mod.FETCHERS, "greenhouse", fetch)
    monkeypatch.setattr(poller_mod, "get_llm_client", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(poller_mod, "triage_titles", fake_triage)

    summary = await poller_mod._poll_one_source_for_target(
        dict(_GUARD_SOURCE), supabase, target, payer_user_id="payer-1"
    )

    assert summary["error"] is None
    # The LLM only ever sees the genuinely new title.
    assert fake_triage.await_args.kwargs["titles"] == ["Senior Frontend Engineer"]
    # The known row refreshes; the rejected candidate stays out.
    jobs_table.upsert.assert_called_once()
    payload = jobs_table.upsert.call_args.args[0]
    assert [r["external_id"] for r in payload] == ["k-known"]


@pytest.mark.asyncio
async def test_refreshed_known_row_does_not_realert(monkeypatch):
    """#514 guard: refreshed known rows must never re-enter the new-job
    alert path. ``jobs.updated_at`` is not bumped by a conflict-update, so
    the old created==updated split classified EVERY refresh as new — with
    known rows now flowing again, that would have re-fired email/SMS
    alerts for the same job on every poll cycle. New-ness keys on the
    pre-upsert external_id set instead."""
    from unittest.mock import AsyncMock

    from app.config import settings as live_settings
    from app.services import poller as poller_mod

    monkeypatch.setattr(live_settings, "phase1_triage_enabled", False)
    monkeypatch.setattr(live_settings, "validate_poll_urls", False)

    existing = [
        {
            "id": "job-known",
            "external_id": "known-1",
            "title": "Brand New Role",
            "company_name": "Acme",
        }
    ]
    supabase, jobs_table, _sources = _make_poll_supabase(existing)
    # The refreshed row comes back with created == updated (the real
    # conflict-update shape); the fresh row is genuinely new.
    jobs_table.upsert.return_value.execute.return_value.data = [
        {
            "id": "job-known",
            "external_id": "known-1",
            "title": "Brand New Role",
            "description_html": "",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        },
        {
            "id": "job-new",
            "external_id": "new-1",
            "title": "Brand New Role II",
            "description_html": "",
            "created_at": "2026-01-02T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
        },
    ]

    async def fetch(_token: str) -> list[StandardJob]:
        return [
            StandardJob(
                external_id="known-1",
                title="Brand New Role",
                location_name="Remote",
                department=None,
                content="",
                updated_at="2026-01-02",
                absolute_url="https://example.com/j/1",
            ),
            StandardJob(
                external_id="new-1",
                title="Brand New Role II",
                location_name="Remote",
                department=None,
                content="",
                updated_at="2026-01-02",
                absolute_url="https://example.com/j/2",
            ),
        ]

    target = _target_with_keywords({"brand": 3}, ["brand new role"])
    fake_email = AsyncMock()
    fake_sms = AsyncMock()
    fake_load = AsyncMock(side_effect=lambda _sb, rows: rows)

    monkeypatch.setitem(poller_mod.FETCHERS, "greenhouse", fetch)
    monkeypatch.setattr(poller_mod, "get_active_target", lambda _sb: [target])
    monkeypatch.setattr(poller_mod, "target_title_score_and_upsert", AsyncMock())
    monkeypatch.setattr(poller_mod, "target_score_and_upsert", AsyncMock())
    monkeypatch.setattr(poller_mod, "batch_update_global_scores", AsyncMock())
    monkeypatch.setattr(poller_mod, "_load_alert_rows", fake_load)
    monkeypatch.setattr(poller_mod.notify, "send_alerts_for_new_jobs", fake_email)
    monkeypatch.setattr(poller_mod.notify, "send_sms_alerts_for_new_jobs", fake_sms)

    summary = await poller_mod._poll_one_source(dict(_GUARD_SOURCE), supabase)

    assert summary["error"] is None
    assert summary["new"] == 1
    assert summary["updated"] == 1
    # Only the genuinely new row reaches the alert path.
    alerted = [r["external_id"] for r in fake_email.await_args.args[1]]
    assert alerted == ["new-1"]
    assert fake_sms.await_count == 1


@pytest.mark.asyncio
async def test_url_validation_skips_known_rows(monkeypatch):
    """#514 guard: with ``validate_poll_urls`` on, only NEW rows hit the
    URL validator — a known row's URL was validated at ingest and is
    url_health-monitored; re-validating per cycle is a HEAD storm and a
    transient upstream blip would null a working absolute_url."""

    from app.config import settings as live_settings
    from app.services import poller as poller_mod

    monkeypatch.setattr(live_settings, "phase1_triage_enabled", False)
    monkeypatch.setattr(live_settings, "validate_poll_urls", True)

    existing = [
        {
            "id": "job-known",
            "external_id": "known-1",
            "title": "Brand New Role",
            "company_name": "Acme",
        }
    ]
    supabase, jobs_table, _sources = _make_poll_supabase(existing)

    async def fetch(_token: str) -> list[StandardJob]:
        return [
            StandardJob(
                external_id="known-1",
                title="Brand New Role",
                location_name="Remote",
                department=None,
                content="",
                updated_at="2026-01-02",
                absolute_url="https://example.com/j/known",
            ),
            StandardJob(
                external_id="new-1",
                title="Brand New Role II",
                location_name="Remote",
                department=None,
                content="",
                updated_at="2026-01-02",
                absolute_url="https://example.com/j/new",
            ),
        ]

    target = _target_with_keywords({"brand": 3}, ["brand new role"])
    seen_urls: list[str] = []

    class _Valid:
        def __init__(self, url: str) -> None:
            self.is_valid = True
            self.warnings: list[str] = []
            self.final_url = url

    async def fake_validate(url: str) -> object:
        seen_urls.append(url)
        return _Valid(url)

    monkeypatch.setitem(poller_mod.FETCHERS, "greenhouse", fetch)
    monkeypatch.setattr(poller_mod, "get_active_target", lambda _sb: [target])
    monkeypatch.setattr(poller_mod, "validate_job_url", fake_validate)

    summary = await poller_mod._poll_one_source(dict(_GUARD_SOURCE), supabase)

    assert summary["error"] is None
    # Only the NEW row's URL was validated; both rows still upserted.
    assert seen_urls == ["https://example.com/j/new"]
    payload = jobs_table.upsert.call_args.args[0]
    assert sorted(r["external_id"] for r in payload) == ["known-1", "new-1"]


# ---- Phase 1 negative-verdict cache (#514 residual) -------------------------


def _rejecting_triage():
    """Triage stub that rejects every submitted title, with a REAL (non-None)
    result object so the titles count as attempted — the cache must only ever
    hold verdicts the LLM actually returned."""
    from unittest.mock import AsyncMock

    from app.services.relevance.title_triage import TitleVerdict

    async def _reject(_llm, *, target, titles):
        verdicts = {i + 1: TitleVerdict(id=i + 1, promising=False) for i in range(len(titles))}
        return verdicts, MagicMock()

    return AsyncMock(side_effect=_reject)


def _wire_rejection_cache_poll(monkeypatch, *, ttl_hours: float):
    """Shared setup for the scheduled-path cache tests: one NEW job that
    passes the free gates, an LLM that rejects it. Returns
    ``(supabase, fake_triage, target, run)`` where ``run()`` awaits one poll
    cycle."""
    from app.config import settings as live_settings
    from app.services import poller as poller_mod

    monkeypatch.setattr(live_settings, "phase1_triage_enabled", True)
    monkeypatch.setattr(live_settings, "phase1_rejection_ttl_hours", ttl_hours)

    supabase, jobs_table, _sources = _make_poll_supabase([])
    fake_triage = _rejecting_triage()
    target = _target_with_keywords({"brand": 3}, ["brand new role"])

    async def one_new_job(_token: str) -> list[StandardJob]:
        return [_job("new-1", "Brand New Role", "Remote")]

    monkeypatch.setitem(poller_mod.FETCHERS, "greenhouse", one_new_job)
    monkeypatch.setattr(poller_mod, "get_active_target", lambda _sb: [target])
    monkeypatch.setattr(poller_mod, "get_llm_client", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(poller_mod, "triage_titles", fake_triage)

    open_gate = MagicMock()
    open_gate.target_blocked.return_value = False

    async def run() -> dict:
        return await poller_mod._poll_one_source(
            dict(_GUARD_SOURCE), supabase, budget_gate=open_gate
        )

    return supabase, fake_triage, target, run


@pytest.mark.asyncio
async def test_phase1_rejection_cache_skips_llm_on_next_cycle(monkeypatch):
    """A title the LLM rejected must NOT be re-sent on the next cycle while
    the TTL holds: the cache re-injects a synthetic reject, so the job is
    still dropped from the upsert (rejected, not budget-deferred) but the
    verdict is free the second time."""
    _supabase, fake_triage, _target, run = _wire_rejection_cache_poll(monkeypatch, ttl_hours=24.0)

    first = await run()
    assert first["error"] is None
    assert fake_triage.await_count == 1
    assert fake_triage.await_args.kwargs["titles"] == ["Brand New Role"]
    assert first["new"] == 0  # rejected → never ingested

    second = await run()
    assert second["error"] is None
    assert fake_triage.await_count == 1  # cache hit — LLM not consulted again
    assert second["new"] == 0  # still rejected, still not ingested


@pytest.mark.asyncio
async def test_phase1_rejection_cache_disabled_at_zero_ttl(monkeypatch):
    """``phase1_rejection_ttl_hours=0`` restores the legacy behavior: every
    cycle re-pays the verdict."""
    _supabase, fake_triage, _target, run = _wire_rejection_cache_poll(monkeypatch, ttl_hours=0.0)

    await run()
    await run()
    assert fake_triage.await_count == 2


@pytest.mark.asyncio
async def test_phase1_rejection_cache_keyed_on_profile_version(monkeypatch):
    """A profile edit bumps ``profile_version``; cached rejections under the
    old profile must not gag the new one — the title is re-judged."""
    from app.services import poller as poller_mod

    _supabase, fake_triage, target, run = _wire_rejection_cache_poll(monkeypatch, ttl_hours=24.0)

    await run()
    assert fake_triage.await_count == 1

    edited = target.model_copy(update={"profile_version": target.profile_version + 1})
    monkeypatch.setattr(poller_mod, "get_active_target", lambda _sb: [edited])
    await run()
    assert fake_triage.await_count == 2  # new profile → fresh verdict


@pytest.mark.asyncio
async def test_targeted_phase1_rejection_cache_skips_llm(monkeypatch):
    """Same cache on the activation path (``_poll_one_source_for_target``):
    the second pass over an unchanged board sends nothing to the LLM."""
    from app.config import settings as live_settings
    from app.services import poller as poller_mod

    monkeypatch.setattr(live_settings, "phase1_triage_enabled", True)
    monkeypatch.setattr(live_settings, "phase1_rejection_ttl_hours", 24.0)
    monkeypatch.setattr(live_settings, "validate_poll_urls", False)

    supabase, _jobs_table, _user_targets = _make_targeted_poll_supabase()
    fake_triage = _rejecting_triage()
    target = _full_target(is_active=True, search_keywords=["frontend engineer"])

    async def fetch(_token: str) -> list[StandardJob]:
        return [_job("k3", "Staff Frontend Engineer", "Remote")]

    monkeypatch.setitem(poller_mod.FETCHERS, "greenhouse", fetch)
    monkeypatch.setattr(poller_mod, "get_llm_client", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(poller_mod, "triage_titles", fake_triage)

    first = await poller_mod._poll_one_source_for_target(
        dict(_GUARD_SOURCE), supabase, target, payer_user_id="payer-1"
    )
    assert first["error"] is None
    assert fake_triage.await_count == 1

    second = await poller_mod._poll_one_source_for_target(
        dict(_GUARD_SOURCE), supabase, target, payer_user_id="payer-1"
    )
    assert second["error"] is None
    assert fake_triage.await_count == 1  # cached rejection — no second call


def test_phase1_rejection_helpers_roundtrip(monkeypatch):
    """Unit contract of the cache helpers: keyed per (target, profile_version,
    normalized title); an expired entry stops matching; TTL=0 records nothing."""
    import time as time_mod

    from app.config import settings as live_settings
    from app.services import poller as poller_mod

    monkeypatch.setattr(live_settings, "phase1_rejection_ttl_hours", 24.0)
    target = _target_with_keywords({"brand": 3}, ["brand new role"])

    poller_mod._phase1_record_rejection(target, "Brand  New Role")
    # Whitespace/case-normalized title matches; other titles don't.
    assert poller_mod._phase1_cached_rejection(target, "brand new role") is True
    assert poller_mod._phase1_cached_rejection(target, "Other Role") is False
    # Same title under a bumped profile_version is a different key.
    edited = target.model_copy(update={"profile_version": target.profile_version + 1})
    assert poller_mod._phase1_cached_rejection(edited, "brand new role") is False

    # Force-expire the stored entry: it must stop matching.
    key = poller_mod._phase1_rejection_key(target, "brand new role")
    poller_mod._PHASE1_REJECTIONS[key] = time_mod.monotonic() - 1.0
    assert poller_mod._phase1_cached_rejection(target, "brand new role") is False

    # TTL=0: nothing is recorded, nothing matches.
    poller_mod._PHASE1_REJECTIONS.clear()
    monkeypatch.setattr(live_settings, "phase1_rejection_ttl_hours", 0.0)
    poller_mod._phase1_record_rejection(target, "brand new role")
    assert poller_mod._PHASE1_REJECTIONS == {}


def test_phase1_rejection_cache_size_bound(monkeypatch):
    """At the hard cap the cache prunes expired entries first, and clears
    outright when still full — it can grow stale-heavy but never unbounded."""
    import time as time_mod

    from app.config import settings as live_settings
    from app.services import poller as poller_mod

    monkeypatch.setattr(live_settings, "phase1_rejection_ttl_hours", 24.0)
    monkeypatch.setattr(poller_mod, "_PHASE1_REJECTIONS_CAP", 2)
    target = _target_with_keywords({"brand": 3}, ["brand new role"])

    # Full of EXPIRED entries → pruned, then the new entry lands.
    poller_mod._PHASE1_REJECTIONS.update(
        {("t", 1, "a"): time_mod.monotonic() - 1, ("t", 1, "b"): time_mod.monotonic() - 1}
    )
    poller_mod._phase1_record_rejection(target, "fresh title")
    assert list(poller_mod._PHASE1_REJECTIONS) == [
        poller_mod._phase1_rejection_key(target, "fresh title")
    ]

    # Full of LIVE entries → blunt clear, then the new entry lands alone.
    poller_mod._PHASE1_REJECTIONS.update(
        {("t", 1, "a"): time_mod.monotonic() + 999, ("t", 1, "b"): time_mod.monotonic() + 999}
    )
    poller_mod._phase1_record_rejection(target, "another title")
    assert list(poller_mod._PHASE1_REJECTIONS) == [
        poller_mod._phase1_rejection_key(target, "another title")
    ]
