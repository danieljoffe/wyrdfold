"""#285 sweep: liveness check + archival of dead listings. NO LLM SPEND.

Covers:
- ``validate.liveness_verdict`` — the live/dead/unknown classifier over a
  ``ValidationResult``. A 404 is ``is_valid=True`` (that flag only guards
  format/SSRF/banned), so deadness is read from ``final_status`` /
  ``looks_like_job``.
- ``poller._backfill_qualify_stale`` — walks a rotating batch of untagged,
  unarchived jobs, ARCHIVES the dead ones, and leaves LIVE / UNKNOWN
  (transient) / URL-less rows untouched.

The sweep's TAGGING half is gone (lazy tagging): "untagged" is the normal
state of the catalog now, so a sweep that tagged what it selected would have
re-bought the whole catalog's tags 50 rows a cycle — exactly the spend lazy
tagging removes. ``test_sweep_performs_no_llm_tagging`` is the regression, and
the rotating cursor keeps the (now non-shrinking) selection advancing.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.services import poller as poller_mod
from app.services.qualification import materialize
from app.services.validate import ValidationResult, liveness_verdict


@pytest.fixture(autouse=True)
def _reset_sweep_cursor() -> Any:
    """The rotation cursor is a module global — reset it so tests can't leak
    an offset into each other."""
    poller_mod._QUALIFY_BACKFILL_OFFSET = 0
    yield
    poller_mod._QUALIFY_BACKFILL_OFFSET = 0


def _vr(**kw: Any) -> ValidationResult:
    base: dict[str, Any] = {"is_valid": True, "final_url": "https://x/j"}
    base.update(kw)
    return ValidationResult(**base)


class TestLivenessVerdict:
    def test_live_200_job(self) -> None:
        assert liveness_verdict(_vr(final_status=200, looks_like_job=True)) == "live"

    def test_dead_404(self) -> None:
        assert liveness_verdict(_vr(final_status=404)) == "dead"

    def test_dead_410_gone(self) -> None:
        assert liveness_verdict(_vr(final_status=410)) == "dead"

    def test_unknown_200_not_a_job_not_archived(self) -> None:
        # A 200 whose body isn't a job (a "position filled" / listing page) is
        # NOT tagged, but NOT hard-archived either — _verify_content is a soft
        # signal, so a live job with odd markup can't be wrongly removed.
        assert liveness_verdict(_vr(final_status=200, looks_like_job=False)) == "unknown"

    def test_unknown_5xx_is_transient(self) -> None:
        assert liveness_verdict(_vr(final_status=503)) == "unknown"

    def test_unknown_fetch_failed_none_status(self) -> None:
        assert liveness_verdict(_vr(final_status=None)) == "unknown"

    def test_unknown_security_reject_never_dead(self) -> None:
        # is_valid=False (malformed/banned/SSRF) is never a clean dead signal —
        # don't archive on it.
        r = ValidationResult(is_valid=False, final_url="x", rejection_reason="banned_domain:x")
        assert liveness_verdict(r) == "unknown"

    def test_200_without_content_verdict_defaults_live(self) -> None:
        assert liveness_verdict(_vr(final_status=200, looks_like_job=None)) == "live"


def _backfill_supabase(
    rows: list[dict[str, Any]], archived: list[str], keysets: list[str | None] | None = None
) -> MagicMock:
    """Fake supabase: the select chain returns ``rows``; a
    ``jobs.update(...).in_('id', ids)`` records the archived ids into
    ``archived``. ``keysets`` (when given) records the ``.or_(...)`` keyset
    predicate each call asked for — ``None`` when it asked for the oldest page
    with no cursor."""
    select_chain = MagicMock()
    select_chain.is_.return_value = select_chain
    select_chain.order.return_value = select_chain
    select_chain.limit.return_value = select_chain

    def _or(expr: str) -> MagicMock:
        if keysets is not None:
            keysets[-1] = expr
        return select_chain

    select_chain.or_.side_effect = _or

    def _execute() -> MagicMock:
        return MagicMock(data=rows)

    select_chain.execute = MagicMock(side_effect=_execute)

    def _in(_col: str, ids: list[str]) -> MagicMock:
        archived.extend(ids)
        chain = MagicMock()
        chain.execute = MagicMock(return_value=MagicMock(data=[]))
        return chain

    update_chain = MagicMock()
    update_chain.in_.side_effect = _in

    def _table(_name: str) -> MagicMock:
        t = MagicMock()

        def _select(*_a: object, **_k: object) -> MagicMock:
            if keysets is not None:
                keysets.append(None)  # replaced by _or if a cursor was applied
            return select_chain

        t.select.side_effect = _select
        t.update.return_value = update_chain
        return t

    sb = MagicMock()
    sb.table.side_effect = _table
    return sb


def _patch_backfill(monkeypatch: pytest.MonkeyPatch, verdicts: dict[str, str]) -> dict[str, Any]:
    """Patch ``validate_job_url`` (verdict keyed by url) and spy on the TAGGER.

    The spy sits on ``materialize.tag_job`` — the module global every route
    into the tagger resolves at call time — so it fires no matter how a future
    change reaches the model (``ensure_job_tags``, ``_qualify_one_job``, a
    re-added direct call). ``rec['tag_calls']`` is therefore a real "did this
    sweep spend?" measurement, not a stub nobody consults.
    """
    rec: dict[str, Any] = {"tag_calls": 0}

    async def fake_validate(url: str) -> ValidationResult:
        v = verdicts.get(url, "unknown")
        if v == "live":
            return ValidationResult(
                is_valid=True, final_url=url, final_status=200, looks_like_job=True
            )
        if v == "dead":
            return ValidationResult(is_valid=True, final_url=url, final_status=404)
        return ValidationResult(is_valid=True, final_url=url, final_status=503)

    async def spy_tag_job(*_a: object, **_kw: object) -> Any:
        rec["tag_calls"] += 1
        raise AssertionError("the #285 sweep must not call the tagger")

    monkeypatch.setattr(poller_mod, "validate_job_url", fake_validate)
    monkeypatch.setattr(materialize, "tag_job", spy_tag_job)
    return rec



def _keyset_supabase(table: list[dict[str, Any]], seen: list[str]) -> MagicMock:
    """Fake supabase backed by a REAL list, honouring the keyset predicate.

    Lets a test mutate ``table`` between pages and assert on coverage, which is
    what distinguishes a keyset walk from an offset one.
    """
    state: dict[str, tuple[str, str] | None] = {"after": None}
    limits: dict[str, int] = {"n": 0}

    select_chain = MagicMock()
    select_chain.is_.return_value = select_chain
    select_chain.order.return_value = select_chain

    def _or(expr: str) -> MagicMock:
        at = expr.split('cataloged_at.gt."')[1].split('"')[0]
        rid = expr.split('id.gt."')[1].split('"')[0]
        state["after"] = (at, rid)
        return select_chain

    def _limit(n: int) -> MagicMock:
        limits["n"] = n
        return select_chain

    select_chain.or_.side_effect = _or
    select_chain.limit.side_effect = _limit

    def _execute() -> MagicMock:
        rows = sorted(table, key=lambda r: (r["cataloged_at"], r["id"]))
        after = state["after"]
        if after is not None:
            rows = [r for r in rows if (r["cataloged_at"], r["id"]) > after]
        page = rows[: limits["n"]]
        seen.extend(str(r["id"]) for r in page)
        return MagicMock(data=page)

    select_chain.execute = MagicMock(side_effect=_execute)

    def _table(_name: str) -> MagicMock:
        t = MagicMock()

        def _select(*_a: object, **_k: object) -> MagicMock:
            state["after"] = None  # a fresh query; _or re-applies any cursor
            return select_chain

        t.select.side_effect = _select
        upd = MagicMock()
        upd.in_.return_value = MagicMock(execute=MagicMock(return_value=MagicMock(data=[])))
        t.update.return_value = upd
        return t

    sb = MagicMock()
    sb.table.side_effect = _table
    return sb


class TestBackfillQualifyStale:
    @pytest.mark.asyncio
    async def test_sweep_performs_no_llm_tagging(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The regression that keeps lazy tagging lazy.

        Under lazy tagging ``role_family IS NULL`` is the NORMAL state, so this
        sweep — which selects exactly that, every cycle — would re-buy the whole
        catalog's tags a batch at a time if it still tagged what it found. Its
        liveness half must still work, which is the anti-vacuous control: the
        dead row IS archived in the same run, so the zero below is the tagging
        half being gone rather than a sweep that did nothing.
        """
        rows: list[dict[str, Any]] = [
            {"id": "live-1", "absolute_url": "https://x/live1"},
            {"id": "dead-1", "absolute_url": "https://x/dead1"},
            {"id": "unk-1", "absolute_url": "https://x/unk1"},
            {"id": "nourl-1", "absolute_url": None},
        ]
        rec = _patch_backfill(
            monkeypatch,
            {
                "https://x/live1": "live",
                "https://x/dead1": "dead",
                "https://x/unk1": "unknown",
            },
        )
        archived: list[str] = []
        sb = _backfill_supabase(rows, archived)

        await poller_mod._backfill_qualify_stale(sb, limit=10)

        assert rec["tag_calls"] == 0  # ZERO LLM spend — the whole point
        assert archived == ["dead-1"]  # ...and the sweep genuinely ran
        # live-1 (still listed), unk-1 (transient 5xx) and nourl-1 (no URL)
        # touch neither path.

    @pytest.mark.asyncio
    async def test_walks_by_keyset_and_wraps_on_a_short_page(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing fills ``role_family`` for these rows anymore, so an
        oldest-first LIMIT would re-check the SAME batch forever. The cursor
        walks a batch per cycle and wraps on a short page."""
        _patch_backfill(monkeypatch, {})
        archived: list[str] = []
        keysets: list[str | None] = []
        full_page = [
            {"id": f"j-{i}", "absolute_url": None, "cataloged_at": f"2026-01-0{i}T00:00:00+00:00"}
            for i in range(3)
        ]
        sb = _backfill_supabase(full_page, archived, keysets)

        await poller_mod._backfill_qualify_stale(sb, limit=3)
        # Precondition: the first pass has no cursor — it starts at the oldest.
        assert keysets == [None]

        await poller_mod._backfill_qualify_stale(sb, limit=3)
        # A FULL page, so the cursor advanced to the LAST pair of that page and
        # asks for everything strictly after it — by key, never by offset.
        assert keysets[-1] is not None
        assert 'cataloged_at.gt."2026-01-02T00:00:00+00:00"' in keysets[-1]
        assert 'id.gt."j-2"' in keysets[-1]

        # A short page means the end of the untagged set → wrap to the oldest.
        short = _backfill_supabase(
            [{"id": "j-9", "absolute_url": None, "cataloged_at": "2026-01-09T00:00:00+00:00"}],
            archived,
            keysets,
        )
        await poller_mod._backfill_qualify_stale(short, limit=3)
        await poller_mod._backfill_qualify_stale(short, limit=3)
        assert keysets[-1] is None

    @pytest.mark.asyncio
    async def test_an_unstable_selection_cannot_skip_rows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE regression this cursor exists for.

        The selection is unstable: a row LEAVES it the moment this sweep
        archives it. Under an OFFSET walk the surviving rows shift backward
        while the cursor only moves forward, so rows slide past unchecked and
        are never visited. Modelled here with a real table the fake filters:
        every page archives its first row, and every row must still be seen.
        """
        _patch_backfill(monkeypatch, {})
        table = [
            {
                "id": f"j-{i:02d}",
                "absolute_url": None,
                "cataloged_at": f"2026-01-01T00:00:{i:02d}+00:00",
            }
            for i in range(9)
        ]
        seen: list[str] = []
        sb = _keyset_supabase(table, seen)

        for _ in range(3):
            await poller_mod._backfill_qualify_stale(sb, limit=3)
            # Simulate the sweep's own archival removing the page's first row.
            if seen:
                table[:] = [r for r in table if r["id"] != seen[-3]]

        # Precondition: the set really did shrink underneath the walk.
        assert len(table) == 6, table
        # Every row visited, none skipped, none re-visited within the pass.
        assert seen == [f"j-{i:02d}" for i in range(9)], seen

    @pytest.mark.asyncio
    async def test_limit_zero_is_noop_no_query(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _patch_backfill(monkeypatch, {})
        archived: list[str] = []
        rows: list[dict[str, Any]] = [{"id": "x", "absolute_url": "https://x/j"}]
        sb = _backfill_supabase(rows, archived)

        await poller_mod._backfill_qualify_stale(sb, limit=0)

        assert rec["tag_calls"] == 0
        assert archived == []
        sb.table.assert_not_called()  # returns before it even queries

    @pytest.mark.asyncio
    async def test_no_untagged_rows_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _patch_backfill(monkeypatch, {})
        archived: list[str] = []
        sb = _backfill_supabase([], archived)

        await poller_mod._backfill_qualify_stale(sb, limit=10)

        assert rec["tag_calls"] == 0
        assert archived == []
