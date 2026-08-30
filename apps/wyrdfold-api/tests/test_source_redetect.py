"""A board that stops answering may have MOVED, not died (#912).

Prod, 2026-08-29, once #913 made ``consecutive_failures`` count for the first
time: 139 enabled sources failing, 93 of them on a bare 404, and the boards
belonged to companies that are obviously still hiring — Notion, Ramp, Plaid,
Sentry, Supabase, every one of them having migrated Greenhouse -> Ashby. A 404
is evidence the board TOKEN is stale. Acting on the identifier as though it
described the entity is the bug.

Measured against that cohort before any of this was written:

    27 resolve to a DIFFERENT live board  -> re-point, don't disable
    33 are still live on the token we hold -> transient; disabling is wrong
     5 resolve onto a token another source owns -> skip (UNIQUE(board_token))
     4 resolve to a live board with 0 postings  -> not evidence enough
    70 resolve to nothing                       -> disable, as today

THE FAKE IS THE LOAD-BEARING PART. ``_BoardFleet`` serves only the boards a
test registers and 404s everything else, and every test drives the REAL
probers in ``ats_detect`` through it rather than stubbing ``detect_ats``. A
fake that always "found" a board would make every assertion here pass, so the
misses (``test_nothing_found_is_not_a_recovery``) share one fake with the hits
(``test_repoints_to_a_different_live_board``): the same object has to be able
to say no.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.ashby import ASHBY_BASE
from app.services.greenhouse import GREENHOUSE_BASE
from app.services.lever import LEVER_BASE
from app.services.smartrecruiters import SMARTRECRUITERS_BASE

pytestmark = pytest.mark.asyncio


# ---- the fake fleet ---------------------------------------------------------


class _Resp:
    def __init__(self, status_code: int, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


class _BoardFleet:
    """Serves the boards a test registers; 404s every other slug.

    Speaks each provider's real wire shape so the production probers do the
    parsing, which is what makes a "not found" here a genuine negative rather
    than a stubbed one.
    """

    def __init__(self) -> None:
        # (provider, slug) -> job count
        self.boards: dict[tuple[str, str], int] = {}
        self.requested: list[str] = []

    def add(self, provider: str, slug: str, jobs: int = 3) -> _BoardFleet:
        self.boards[(provider, slug)] = jobs
        return self

    # -- transport -----------------------------------------------------------

    async def get(self, url: str, **_kwargs: Any) -> _Resp:
        self.requested.append(url)

        if url.startswith(GREENHOUSE_BASE):
            rest = url[len(GREENHOUSE_BASE) :].lstrip("/")
            if rest.endswith("/jobs"):
                slug = rest[: -len("/jobs")]
                n = self.boards.get(("greenhouse", slug))
                if n is None:
                    return _Resp(404)
                return _Resp(200, {"jobs": [{"id": i} for i in range(n)]})
            n = self.boards.get(("greenhouse", rest))
            if n is None:
                return _Resp(404)
            return _Resp(200, {"name": rest.title()})

        if url.startswith(LEVER_BASE):
            slug = url[len(LEVER_BASE) :].lstrip("/").split("?")[0]
            n = self.boards.get(("lever", slug))
            if not n:
                return _Resp(404)
            return _Resp(200, [{"id": i} for i in range(n)])

        if url.startswith(ASHBY_BASE):
            slug = url[len(ASHBY_BASE) :].lstrip("/")
            n = self.boards.get(("ashby", slug))
            if n is None:
                return _Resp(404)
            return _Resp(200, {"organizationName": slug.title(), "jobs": [{"id": i} for i in range(n)]})

        if url.startswith(SMARTRECRUITERS_BASE):
            slug = url[len(SMARTRECRUITERS_BASE) :].lstrip("/").split("/")[0]
            n = self.boards.get(("smartrecruiters", slug))
            if not n:
                return _Resp(404)
            return _Resp(200, {"content": [{"id": i} for i in range(n)], "totalFound": n})

        return _Resp(404)

    async def post(self, url: str, **_kwargs: Any) -> _Resp:
        self.requested.append(url)
        # https://{tenant}.wdN.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
        if "/wday/cxs/" in url:
            tail = url.split("/wday/cxs/", 1)[1]
            parts = tail.split("/")
            if len(parts) >= 2:
                n = self.boards.get(("workday", f"{parts[0]}|{parts[1]}"))
                if n is None:
                    return _Resp(404)
                return _Resp(200, {"total": n})
        return _Resp(404)

    def probed(self, needle: str) -> bool:
        return any(needle in u for u in self.requested)


@pytest.fixture(autouse=True)
def _clear_still_live_memo():
    """``_STILL_LIVE_UNTIL`` is module-global; a leaked entry would let one
    test silently suppress another's probe."""
    from app.services.source_redetect import _STILL_LIVE_UNTIL

    _STILL_LIVE_UNTIL.clear()
    yield
    _STILL_LIVE_UNTIL.clear()


@pytest.fixture
def fleet(mock_http_client: MagicMock) -> _BoardFleet:
    f = _BoardFleet()
    mock_http_client.get = AsyncMock(side_effect=f.get)
    mock_http_client.post = AsyncMock(side_effect=f.post)
    return f


# ---- the sources table ------------------------------------------------------


class _Chain:
    def __init__(self, table: _SourcesTable, kind: str, payload: dict[str, Any] | None) -> None:
        self._table = table
        self._kind = kind
        self._payload = payload
        self._token: str | None = None

    def eq(self, column: str, value: Any) -> _Chain:
        if column == "board_token":
            self._token = value
        return self

    def limit(self, _n: int) -> _Chain:
        return self

    def execute(self) -> Any:
        if self._kind == "update":
            if self._table.update_raises:
                raise RuntimeError("duplicate key value violates unique constraint")
            self._table.updates.append(dict(self._payload or {}))
            return MagicMock(data=[])
        rows = [{"id": self._table.owners[self._token]}] if self._token in self._table.owners else []
        return MagicMock(data=rows)


class _SourcesTable:
    def __init__(self, owners: dict[str, str] | None = None) -> None:
        # board_token -> owning source id
        self.owners = owners or {}
        self.updates: list[dict[str, Any]] = []
        self.update_raises = False

    def select(self, *_a: Any, **_k: Any) -> _Chain:
        return _Chain(self, "select", None)

    def update(self, payload: dict[str, Any]) -> _Chain:
        return _Chain(self, "update", payload)


def _supabase(owners: dict[str, str] | None = None) -> tuple[MagicMock, _SourcesTable]:
    table = _SourcesTable(owners)
    sb = MagicMock()
    sb.table.return_value = table
    return sb, table


_ACME = {
    "id": "src-1",
    "provider": "greenhouse",
    "board_token": "acme",
    "company_name": "Acme",
    "consecutive_failures": 9,
}

# The still-live cohort in prod is Workday to a source, and Workday is the one
# provider ``detect_ats`` cannot reach from a slug — so this row is the case
# that only a direct probe of the held token can answer.
_TRICON = {
    "id": "src-wd",
    "provider": "workday",
    "board_token": "https://tricon.wd3.myworkdayjobs.com|tricon|tricon",
    "company_name": "Tricon",
    "consecutive_failures": 9,
}


# ---- slug ladder ------------------------------------------------------------


def test_slug_candidates_cleans_dedups_and_unpacks_workday() -> None:
    from app.services.source_redetect import slug_candidates

    # Board noise is stripped before sluggification: providers let a company
    # title its board "Acme Careers page" and that title becomes company_name.
    assert slug_candidates(
        {"provider": "greenhouse", "board_token": "acme-inc", "company_name": "Acme Careers page"}
    ) == ["acme", "acme-inc"]

    # One-word name: rungs 1 and 3 collapse to the same slug and must not be
    # probed twice.
    assert slug_candidates(
        {"provider": "greenhouse", "board_token": "notion", "company_name": "Notion"}
    ) == ["notion"]

    # Multi-word: the plain form (rung 1), the held token (rung 2), then the
    # hyphenated form (rung 3), in that order.
    assert slug_candidates(
        {
            "provider": "greenhouse",
            "board_token": "thinkingmachines",
            "company_name": "Thinking Machines Lab",
        }
    ) == ["thinkingmachineslab", "thinkingmachines", "thinking-machines-lab"]

    # A Workday token is ``{base_url}|{tenant}|{site}``; only the tenant is a
    # usable slug for the four slug-based providers.
    assert slug_candidates(
        {
            "provider": "workday",
            "board_token": "https://cff.wd1.myworkdayjobs.com|cff|External",
            "company_name": "Cff",
        }
    ) == ["cff"]

    # A one-character slug is a coin flip against every board on four
    # providers, not an identifier.
    assert slug_candidates({"provider": "greenhouse", "board_token": "x", "company_name": "X"}) == []


# ---- redetect_source verdicts ----------------------------------------------


async def test_repoints_to_a_different_live_board(fleet: _BoardFleet) -> None:
    """The Notion/Ramp/Plaid shape: Greenhouse token dead, Ashby board live."""
    from app.services.ats_detect import probe_board
    from app.services.source_redetect import redetect_source

    fleet.add("ashby", "acme", jobs=7)
    sb, _table = _supabase()

    # Precondition: the board we hold really is dead in this fake, so a
    # "repoint" below cannot be an artifact of a fake that answers everything.
    assert await probe_board("greenhouse", "acme") is None

    outcome = await redetect_source(sb, _ACME)

    assert outcome.action == "repoint"
    assert (outcome.provider, outcome.board_token) == ("ashby", "acme")
    assert outcome.job_count == 7


async def test_same_token_still_live_short_circuits(fleet: _BoardFleet) -> None:
    """A live board answers the question in one request; the cross-provider
    fan-out must not run at all.

    The company name and the board token deliberately differ (the Underdog
    shape), so a run that reached the ladder would probe every provider for
    ``underdog`` before rediscovering the board we already hold.
    """
    from app.services.source_redetect import redetect_source

    fleet.add("greenhouse", "underdogfantasy", jobs=4)
    sb, _table = _supabase()
    source = {
        "id": "src-ud",
        "provider": "greenhouse",
        "board_token": "underdogfantasy",
        "company_name": "Underdog",
    }

    outcome = await redetect_source(sb, source)

    assert outcome.action == "still_live"
    assert (outcome.provider, outcome.board_token) == ("greenhouse", "underdogfantasy")
    assert not fleet.probed(ASHBY_BASE), (
        "a live current board answers the question — the cross-provider "
        "fan-out is wasted requests against third-party boards"
    )


async def test_a_live_workday_board_is_reachable_only_by_probing_what_we_hold(
    fleet: _BoardFleet,
) -> None:
    """The actual prod cohort: all 33 still-live sources are Workday.

    ``detect_ats`` cannot reach Workday from a slug at all — the site segment
    lives in the URL path and only ``{base_url}|{tenant}|{site}`` is pollable,
    so the slug ladder returns nothing for every one of them. Without a direct
    probe of the token we already hold, all 33 read as dead boards and get
    disabled while serving jobs.
    """
    from app.services.ats_detect import detect_ats
    from app.services.source_redetect import redetect_source, slug_candidates

    fleet.add("workday", "tricon|tricon", jobs=29)
    sb, _table = _supabase()

    # Precondition: the ladder genuinely finds nothing for this source.
    for slug in slug_candidates(_TRICON):
        assert await detect_ats(slug) is None

    outcome = await redetect_source(sb, _TRICON)
    assert outcome.action == "still_live"
    assert outcome.job_count == 29


async def test_nothing_found_is_not_a_recovery(fleet: _BoardFleet) -> None:
    """The control for the whole file: the SAME fake, nothing registered."""
    from app.services.source_redetect import redetect_source

    sb, _table = _supabase()

    outcome = await redetect_source(sb, _ACME)

    assert outcome.action == "not_found"
    # ...and it really did look: all three rungs of the ladder plus the
    # current-board probe.
    assert fleet.probed(f"{ASHBY_BASE}/acme")


async def test_live_board_with_zero_postings_is_not_a_repoint(fleet: _BoardFleet) -> None:
    """4 of the 139. Greenhouse answers 200 for a board with no open roles, so
    "the org exists" is not "this is where the company hires now" — the same
    bar ``source_registration`` applies when it refuses a ``dead_board``.
    """
    from app.services.ats_detect import detect_ats
    from app.services.source_redetect import redetect_source

    fleet.add("greenhouse", "acmeco", jobs=0)
    sb, _table = _supabase()
    source = {**_ACME, "company_name": "AcmeCo"}

    # Precondition: detection DOES find this board — the rejection below is
    # the job_count gate, not a failed probe.
    found = await detect_ats("acmeco")
    assert found is not None and found.job_count == 0

    outcome = await redetect_source(sb, source)
    assert outcome.action == "not_found"


async def test_collision_with_an_existing_source_is_reported(fleet: _BoardFleet) -> None:
    """5 of the 139. ``sources`` carries UNIQUE(board_token), so re-pointing
    onto a token another row holds would raise 23505 — and duplicate a company
    even without the constraint."""
    from app.services.source_redetect import redetect_source

    fleet.add("ashby", "acme", jobs=7)
    sb, _table = _supabase(owners={"acme": "src-other"})

    outcome = await redetect_source(sb, _ACME)

    assert outcome.action == "collision"
    assert outcome.blocked_by == "src-other"
    assert outcome.board_token == "acme"


async def test_collision_check_ignores_the_source_itself(fleet: _BoardFleet) -> None:
    """Anti-vacuous partner to the test above: the ownership check must not
    trip on the row being re-pointed, or a same-token/new-provider move (the
    Underdog shape) could never happen."""
    from app.services.source_redetect import redetect_source

    fleet.add("ashby", "acme", jobs=7)
    sb, _table = _supabase(owners={"acme": "src-1"})  # ...which IS _ACME's id

    outcome = await redetect_source(sb, _ACME)
    assert outcome.action == "repoint"


async def test_held_token_recovers_a_company_its_name_cannot(fleet: _BoardFleet) -> None:
    """Rung 2, worth +3 on the measured cohort: "Thinking Machines Lab"
    sluggifies to ``thinkingmachineslab``, but the board is
    ``thinkingmachines`` — which is exactly the stale token we already hold."""
    from app.services.source_redetect import redetect_source

    fleet.add("ashby", "thinkingmachines", jobs=38)
    sb, _table = _supabase()
    source = {
        "id": "src-tml",
        "provider": "greenhouse",
        "board_token": "thinkingmachines",
        "company_name": "Thinking Machines Lab",
    }

    outcome = await redetect_source(sb, source)

    assert outcome.action == "repoint"
    assert (outcome.provider, outcome.board_token) == ("ashby", "thinkingmachines")
    # Rung 1 was tried first and missed — the recovery came from rung 2.
    assert fleet.probed(f"{ASHBY_BASE}/thinkingmachineslab")


async def test_hyphenated_name_recovers_a_company_the_other_rungs_cannot(
    fleet: _BoardFleet,
) -> None:
    """Rung 3, worth +2: "Black Forest Labs" -> ``black-forest-labs``."""
    from app.services.source_redetect import redetect_source

    fleet.add("ashby", "black-forest-labs", jobs=16)
    sb, _table = _supabase()
    source = {
        "id": "src-bfl",
        "provider": "greenhouse",
        "board_token": "bflabs",
        "company_name": "Black Forest Labs",
    }

    outcome = await redetect_source(sb, source)

    assert outcome.action == "repoint"
    assert outcome.board_token == "black-forest-labs"
    assert fleet.probed(f"{ASHBY_BASE}/blackforestlabs")  # rung 1 missed
    assert fleet.probed(f"{ASHBY_BASE}/bflabs")  # rung 2 missed


async def test_a_recent_still_live_verdict_is_not_re_probed(fleet: _BoardFleet) -> None:
    """A ``still_live`` verdict does NOT reset ``consecutive_failures`` — the
    counter is real signal that the normal fetch path is still failing — so the
    source stays above the threshold and every later failed poll would re-ask a
    question we just answered. For the Workday cohort that is the steady state,
    so the re-verification is what gets throttled, not the counter.
    """
    from app.services.source_redetect import redetect_source

    fleet.add("workday", "tricon|tricon", jobs=29)
    sb, _table = _supabase()

    first = await redetect_source(sb, _TRICON)
    assert first.action == "still_live"
    assert first.from_cooldown is False
    probes_after_first = len(fleet.requested)
    assert probes_after_first > 0, "the first verdict must come from a real probe"

    second = await redetect_source(sb, _TRICON)

    assert second.action == "still_live"
    assert second.from_cooldown is True, "the caller must be able to tell it was remembered"
    assert len(fleet.requested) == probes_after_first, "the board must not be re-probed"


async def test_the_cooldown_is_per_source_and_switchable(
    fleet: _BoardFleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anti-vacuous partner: prove the silence above is the cooldown and not a
    fleet that stopped answering — with the window at 0 the same second call
    probes again, and a different source is never covered by another's entry.
    """
    from app.services import source_redetect

    fleet.add("workday", "tricon|tricon", jobs=29)
    fleet.add("greenhouse", "underdogfantasy", jobs=4)
    sb, _table = _supabase()

    # A cooldown entry for one source must not silence another.
    await source_redetect.redetect_source(sb, _TRICON)
    before = len(fleet.requested)
    other = await source_redetect.redetect_source(
        sb,
        {"id": "src-ud", "provider": "greenhouse", "board_token": "underdogfantasy",
         "company_name": "Underdog"},
    )
    assert other.action == "still_live"
    assert other.from_cooldown is False
    assert len(fleet.requested) > before

    # Window of 0 disables the memo entirely.
    monkeypatch.setattr(
        source_redetect.settings, "source_redetect_still_live_cooldown_hours", 0
    )
    source_redetect._STILL_LIVE_UNTIL.clear()
    await source_redetect.redetect_source(sb, _TRICON)
    mark = len(fleet.requested)
    again = await source_redetect.redetect_source(sb, _TRICON)
    assert again.from_cooldown is False
    assert len(fleet.requested) > mark, "with the cooldown off, every attempt re-probes"


async def test_an_expired_cooldown_re_verifies(fleet: _BoardFleet) -> None:
    """The window must actually expire — a permanent entry would suppress the
    disable forever for a board that later genuinely dies."""
    from app.services import source_redetect

    fleet.add("workday", "tricon|tricon", jobs=29)
    sb, _table = _supabase()

    await source_redetect.redetect_source(sb, _TRICON)
    before = len(fleet.requested)

    # Age the entry past its deadline rather than sleeping 12 hours.
    source_redetect._STILL_LIVE_UNTIL[_TRICON["id"]] = time.monotonic() - 1.0

    third = await source_redetect.redetect_source(sb, _TRICON)
    assert third.from_cooldown is False
    assert len(fleet.requested) > before


async def test_the_poller_suppresses_from_cooldown_without_probing(
    fleet: _BoardFleet,
) -> None:
    """End to end: the second failed poll still refuses to disable, and does it
    without touching the provider."""
    fleet.add("workday", "tricon|tricon", jobs=29)

    first, _t1 = await _record(_TRICON)
    assert first is not None and "enabled" not in first
    probes = len(fleet.requested)
    assert probes > 0

    second, _t2 = await _record({**_TRICON, "consecutive_failures": 10})

    assert second is not None
    assert "enabled" not in second, "a board verified live recently must not be disabled"
    assert second["consecutive_failures"] == 11, "the counter still climbs"
    assert len(fleet.requested) == probes, "no second probe"


async def test_a_provider_we_cannot_interpret_is_never_guessed_at(
    fleet: _BoardFleet,
) -> None:
    """Prod holds one ``manual`` source whose board_token is the literal string
    "manual". Feeding that to the slug ladder would probe four providers for a
    board called "manual" and re-point a hand-curated row onto whatever
    answered."""
    from app.services.source_redetect import redetect_source

    # A board that WOULD be found, so the refusal below is the provider guard
    # and not an empty fleet.
    fleet.add("greenhouse", "manual", jobs=12)
    sb, _table = _supabase()

    outcome = await redetect_source(
        sb,
        {"id": "src-m", "provider": "manual", "board_token": "manual", "company_name": "Manual"},
    )

    assert outcome.action == "not_found"
    assert fleet.requested == [], "an uninterpretable token must not be probed at all"


async def test_a_hanging_board_cannot_wedge_the_poll_cycle(
    mock_http_client: MagicMock,
) -> None:
    """The ladder can issue 13 requests. Unbounded, one hanging board stalls
    the whole cycle behind it."""
    from app.services.source_redetect import redetect_source

    async def _hang(*_a: Any, **_k: Any) -> Any:
        await asyncio.sleep(30)

    mock_http_client.get = AsyncMock(side_effect=_hang)
    mock_http_client.post = AsyncMock(side_effect=_hang)
    sb, _table = _supabase()

    outcome = await asyncio.wait_for(
        redetect_source(sb, _ACME, timeout_s=0.05), timeout=5
    )
    assert outcome.action == "not_found"


async def test_a_broken_probe_degrades_to_todays_behaviour(
    mock_http_client: MagicMock,
) -> None:
    """A re-detection that cannot run must never be the reason a source escapes
    the backoff."""
    from app.services.source_redetect import redetect_source

    mock_http_client.get = AsyncMock(side_effect=RuntimeError("boom"))
    mock_http_client.post = AsyncMock(side_effect=RuntimeError("boom"))
    sb, _table = _supabase()

    outcome = await redetect_source(sb, _ACME)
    assert outcome.action == "not_found"


async def test_unreadable_ownership_check_blocks_the_repoint(fleet: _BoardFleet) -> None:
    """Without a clean answer we cannot know the write is safe; a 23505 is a
    worse outcome than one more cycle of the status quo."""
    from app.services.source_redetect import redetect_source

    fleet.add("ashby", "acme", jobs=7)
    sb = MagicMock()
    sb.table.side_effect = RuntimeError("postgrest down")

    outcome = await redetect_source(sb, _ACME)
    assert outcome.action == "not_found"


# ---- what the poller persists ----------------------------------------------


async def _record(
    source: dict[str, Any],
    owners: dict[str, str] | None = None,
    error: str = "greenhouse acme returned 404",
) -> tuple[dict[str, Any] | None, _SourcesTable]:
    from app.services import poller

    sb, table = _supabase(owners)
    await poller._record_source_failure(sb, source, error=error)
    return (table.updates[-1] if table.updates else None), table


@pytest.fixture(autouse=True)
def _threshold_ten(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import poller

    monkeypatch.setattr(poller.settings, "source_failure_disable_threshold", 10)
    monkeypatch.setattr(poller.settings, "source_redetect_on_disable_enabled", True)


async def test_repoint_rewrites_the_row_and_clears_the_failure_state(
    fleet: _BoardFleet,
) -> None:
    """Re-point the EXISTING row: ``jobs.source_id`` is an FK, so registering a
    new source would orphan every listing we already hold for this company."""
    fleet.add("ashby", "acme", jobs=7)

    payload, table = await _record(_ACME)

    assert payload is not None
    assert payload["provider"] == "ashby"
    assert payload["board_token"] == "acme"
    assert payload["consecutive_failures"] == 0, "a re-point resolves the failure"
    assert payload["last_error"] is None
    assert payload["last_error_at"] is None
    assert "enabled" not in payload, "a re-pointed source must not be disabled"
    assert "disabled_at" not in payload
    # company_name is user-visible and feeds the dedup key — a probe is not
    # licence to rewrite it.
    assert "company_name" not in payload
    assert len(table.updates) == 1, "the failure payload must not also be written"


async def test_still_live_suppresses_the_disable_but_keeps_counting(
    fleet: _BoardFleet,
) -> None:
    """The prod cohort exactly: a Workday board serving 29 jobs whose polls
    keep failing on ``returned 200 with a non-JSON body``."""
    fleet.add("workday", "tricon|tricon", jobs=29)
    prod_error = (
        "workday https://tricon.wd3.myworkdayjobs.com|tricon|tricon "
        "(offset 0) returned 200 with a non-JSON body"
    )

    payload, _table = await _record(_TRICON, error=prod_error)

    assert payload is not None
    assert "enabled" not in payload, "a live board must not be disabled"
    assert "disabled_at" not in payload
    # Still a failed poll: the counter climbs and the cause stays queryable,
    # so a permanently unreadable-but-live board is visible in SQL.
    assert payload["consecutive_failures"] == 10
    assert payload["last_error"] == prod_error
    assert "board_token" not in payload


async def test_nothing_found_disables_exactly_as_before(fleet: _BoardFleet) -> None:
    payload, _table = await _record(_ACME)

    assert payload is not None
    assert payload["enabled"] is False
    assert payload["disabled_at"] is not None
    assert payload["consecutive_failures"] == 10
    assert "board_token" not in payload


async def test_collision_disables_and_never_writes_the_duplicate_token(
    fleet: _BoardFleet,
) -> None:
    fleet.add("ashby", "acme", jobs=7)

    payload, _table = await _record(_ACME, owners={"acme": "src-other"})

    assert payload is not None
    assert payload["enabled"] is False
    assert "board_token" not in payload, "would violate UNIQUE(board_token)"
    assert "provider" not in payload


async def test_a_failed_repoint_write_falls_back_to_disabling(fleet: _BoardFleet) -> None:
    """A lost race on UNIQUE(board_token) must not leave the source in limbo."""
    from app.services import poller

    fleet.add("ashby", "acme", jobs=7)
    sb, table = _supabase()
    table.update_raises = True

    await poller._record_source_failure(sb, _ACME, error="greenhouse acme returned 404")

    # Both writes raised (the fake table refuses every update), so the proof is
    # that it went on to ATTEMPT the disable rather than returning after the
    # re-point failed.
    assert sb.table.call_count == 3  # owner read, re-point write, disable write


async def test_below_the_threshold_nothing_is_probed(fleet: _BoardFleet) -> None:
    """Probing is gated on the threshold, not on every failure — otherwise
    every transient blip across ~4,900 sources becomes third-party traffic."""
    fleet.add("ashby", "acme", jobs=7)

    payload, _table = await _record({**_ACME, "consecutive_failures": 3})

    assert payload is not None
    assert payload["consecutive_failures"] == 4
    assert "enabled" not in payload
    assert fleet.requested == [], "no board should have been probed"


async def test_the_off_switch_restores_the_pre_912_behaviour(
    fleet: _BoardFleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import poller

    monkeypatch.setattr(poller.settings, "source_redetect_on_disable_enabled", False)
    # A board that WOULD have been recovered, to prove the flag is what stops
    # it rather than a missing board.
    fleet.add("ashby", "acme", jobs=7)

    payload, _table = await _record(_ACME)

    assert payload is not None
    assert payload["enabled"] is False
    assert "board_token" not in payload
    assert fleet.requested == []
