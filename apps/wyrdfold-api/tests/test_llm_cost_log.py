"""cost_log: RPC-first spend queries with Python fallback, plus enqueue."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from postgrest.exceptions import APIError

from app.constants import SYSTEM_USER_ID
from app.models.embeddings import EmbeddingResult, EmbeddingUsage
from app.models.llm import LLMResult, LLMUsage
from app.services.llm import cost_log
from app.services.llm.cost_log_buffer import buffer


class _Resp:
    def __init__(self, data: Any) -> None:
        self.data = data


# ---- a fake that behaves like the real PostgREST, cap included -------------
#
# The old fakes here were plain MagicMock chains, so they answered whatever
# was asked and had no row cap. That is why nothing caught #1105: production
# PostgREST clamps one response at 1,000 rows, the aggregates read without
# paging, and every total was quietly computed from the first page. A fake
# that cannot truncate cannot fail the way the dependency really fails.
#
# This one enforces the cap and honours .range(), so a reader that forgets to
# page returns a SHORT total here exactly as it did in production.
_POSTGREST_MAX_ROWS = 1000


class _FakeQuery:
    """Minimal postgrest query builder: filters are recorded, not applied
    (the tests choose the row set), but RANGE and the row cap are real."""

    def __init__(self, rows: list[dict[str, Any]], rec: dict[str, list[Any]]) -> None:
        self._rows = rows
        self._rec = rec
        self._start = 0
        self._end: int | None = None

    def eq(self, *a: Any, **_k: Any) -> _FakeQuery:
        self._rec["eq"].append(a)
        return self

    def gte(self, *a: Any, **_k: Any) -> _FakeQuery:
        self._rec["gte"].append(a)
        return self

    def is_(self, *a: Any, **_k: Any) -> _FakeQuery:
        self._rec["is_"].append(a)
        return self

    def order(self, *_a: Any, **_k: Any) -> _FakeQuery:
        return self

    def limit(self, n: int) -> _FakeQuery:
        self._end = self._start + n - 1
        return self

    def range(self, start: int, end: int) -> _FakeQuery:
        self._start, self._end = start, end
        return self

    def _page(self) -> list[dict[str, Any]]:
        end = len(self._rows) - 1 if self._end is None else self._end
        window = self._rows[self._start : end + 1]
        self._rec["ranges"].append((self._start, end))
        # The real server will not return more than this however wide the
        # requested range is.
        return window[:_POSTGREST_MAX_ROWS]

    def execute(self) -> _Resp:
        return _Resp(self._page())


class _FakeAsyncQuery(_FakeQuery):
    async def execute(self) -> _Resp:  # type: ignore[override]
        return _Resp(self._page())


class _FakeTable:
    def __init__(self, rows: list[dict[str, Any]], rec: dict[str, list[Any]], *, is_async: bool):
        self._rows, self._rec, self._async = rows, rec, is_async

    def select(self, *_a: Any, **_k: Any) -> _FakeQuery:
        cls = _FakeAsyncQuery if self._async else _FakeQuery
        return cls(self._rows, self._rec)


def _fake_client(
    rows: list[dict[str, Any]], *, is_async: bool = False, rpc_error: Exception | None = None
) -> tuple[Any, dict[str, list[Any]]]:
    """Client whose RPC fails (forcing the fallback) and whose table reads
    behave like PostgREST, row cap included.

    Returns the client plus a record of what was asked: ``ranges`` (so a test
    can prove the reader paged) and the ``eq`` / ``gte`` / ``is_`` filters."""
    rec: dict[str, list[Any]] = {"ranges": [], "eq": [], "gte": [], "is_": []}
    sb = MagicMock()
    sb.rpc.side_effect = rpc_error or Exception("function does not exist")
    sb.table.return_value = _FakeTable(rows, rec, is_async=is_async)
    return sb, rec


def _llm_result(cost: float = 0.01) -> LLMResult:
    return LLMResult(
        content="ok",
        model="claude-haiku-4-5",
        usage=LLMUsage(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
        cost_usd=cost,
        latency_ms=42,
    )


# ---- total_spend RPC path -------------------------------------------------


def test_total_spend_uses_rpc_when_available() -> None:
    sb = MagicMock()
    sb.rpc.return_value.execute.return_value = _Resp(0.42)

    result = cost_log.total_spend(sb, user_id="u1", since=datetime.now(UTC))

    assert result == 0.42
    args, kwargs = sb.rpc.call_args
    assert args[0] == "total_spend_since"
    assert args[1]["p_user_id"] == "u1"
    # The supabase select-table API should NOT be touched.
    sb.table.assert_not_called()


def test_total_spend_stamps_system_owner_for_none_user_id() -> None:
    # A caller with no user (cron / api-key) reads the SYSTEM partition — the
    # Phase-0 replacement for the legacy `user_id IS NULL` marker. The RPC's own
    # `auth.uid()` authz guard still prevents a JWT user from passing SYSTEM.
    sb = MagicMock()
    sb.rpc.return_value.execute.return_value = _Resp(1.5)

    cost_log.total_spend(sb, user_id=None, since=None)

    args, _ = sb.rpc.call_args
    assert args[1]["p_user_id"] == SYSTEM_USER_ID
    assert args[1]["p_since"] is None


def test_total_spend_zero_when_rpc_returns_none() -> None:
    sb = MagicMock()
    sb.rpc.return_value.execute.return_value = _Resp(None)

    assert cost_log.total_spend(sb, user_id="u1") == 0.0


def test_total_spend_falls_back_to_python_when_rpc_unavailable() -> None:
    sb, _ = _fake_client([{"cost_usd": 0.10}, {"cost_usd": 0.25}, {"cost_usd": 0.05}])

    result = cost_log.total_spend(sb, user_id="u1", since=datetime.now(UTC) - timedelta(hours=1))
    assert result == pytest.approx(0.40)


def test_total_spend_fallback_reads_system_partition_for_none_user() -> None:
    # RPC unavailable + no user (cron) → the Python fallback filters on the
    # SYSTEM partition via eq, NOT the retired is_("user_id","null") branch.
    sb, rec = _fake_client([{"cost_usd": 0.5}], rpc_error=Exception("not deployed"))

    result = cost_log.total_spend(sb, user_id=None, since=datetime.now(UTC) - timedelta(hours=1))

    assert result == pytest.approx(0.5)
    assert rec["eq"] == [("user_id", SYSTEM_USER_ID)]
    assert rec["is_"] == []


def test_total_spend_rounds_to_six_decimals() -> None:
    sb = MagicMock()
    sb.rpc.return_value.execute.return_value = _Resp("0.0000004999")
    assert cost_log.total_spend(sb, user_id="u1") == pytest.approx(0.0)


# ---- spend_by_purpose RPC path --------------------------------------------


def test_spend_by_purpose_uses_rpc_when_available() -> None:
    sb = MagicMock()
    sb.rpc.return_value.execute.return_value = _Resp({"job_analysis": "1.25", "tailor": "0.50"})

    result = cost_log.spend_by_purpose(sb, user_id="u1")

    assert result == {"job_analysis": pytest.approx(1.25), "tailor": pytest.approx(0.50)}
    args, _ = sb.rpc.call_args
    assert args[0] == "spend_by_purpose_since"


def test_spend_by_purpose_empty_when_rpc_returns_empty_object() -> None:
    sb = MagicMock()
    sb.rpc.return_value.execute.return_value = _Resp({})
    assert cost_log.spend_by_purpose(sb, user_id="u1") == {}


def test_spend_by_purpose_falls_back_when_rpc_unavailable() -> None:
    sb, _ = _fake_client(
        [
            {"purpose": "job_analysis", "cost_usd": 0.10},
            {"purpose": "job_analysis", "cost_usd": 0.20},
            {"purpose": "tailor", "cost_usd": 0.05},
        ],
        rpc_error=Exception("not deployed"),
    )

    result = cost_log.spend_by_purpose(sb, user_id="u1")
    assert result == {"job_analysis": pytest.approx(0.30), "tailor": pytest.approx(0.05)}


# ---- cache_metrics_all -----------------------------------------------------


def test_cache_metrics_all_sums_token_buckets() -> None:
    sb, _ = _fake_client(
        [
            {
                "input_tokens": 100,
                "cache_read_input_tokens": 800,
                "cache_creation_input_tokens": 0,
            },
            {
                "input_tokens": 50,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 200,
            },
        ]
    )

    result = cost_log.cache_metrics_all(sb)
    assert result == {"cache_read": 800, "cache_creation": 200, "uncached_input": 150}


def test_cache_metrics_all_zero_when_no_rows() -> None:
    sb = MagicMock()
    sb.table.return_value.select.return_value.execute.return_value = _Resp([])

    result = cost_log.cache_metrics_all(sb)
    assert result == {"cache_read": 0, "cache_creation": 0, "uncached_input": 0}


# ---- enqueue helper --------------------------------------------------------


def test_enqueue_adds_one_row_to_module_buffer() -> None:
    # Drain anything left from prior tests.
    buffer._drain()
    cost_log.enqueue(user_id="u1", purpose="poll_scoring", result=_llm_result(cost=0.07))

    drained = buffer._drain()
    assert len(drained) == 1
    row = drained[0]
    assert row["user_id"] == "u1"
    assert row["purpose"] == "poll_scoring"
    assert row["model"] == "claude-haiku-4-5"
    assert row["cost_usd"] == 0.07
    assert row["input_tokens"] == 10
    assert row["output_tokens"] == 5


def test_enqueue_carries_metadata_when_provided() -> None:
    buffer._drain()
    cost_log.enqueue(
        user_id=None,
        purpose="poll_scoring",
        result=_llm_result(),
        metadata={"job_id": "abc", "target_id": "xyz"},
    )
    drained = buffer._drain()
    # Caller metadata is preserved; the row also carries the cost provenance
    # stamp every LLM row now gets (#933).
    assert drained[0]["metadata"] == {
        "job_id": "abc",
        "target_id": "xyz",
        "cost_source": "estimated",
        "transport": "unknown",
    }
    # user_id=None (a cron enqueue) is stamped SYSTEM at row-build time (#88
    # groundwork) — the buffered write is no longer a NULL-owner row.
    assert drained[0]["user_id"] == SYSTEM_USER_ID


# ---- async writers (record_async / record_embedding_async, #57 slice 3) -----


def _embedding_result(cost: float = 0.002) -> EmbeddingResult:
    return EmbeddingResult(
        embeddings=[[0.1, 0.2]],
        model="voyage-3.5",
        usage=EmbeddingUsage(input_tokens=8),
        cost_usd=cost,
        latency_ms=12,
    )


def _stored_row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "row-1",
        "user_id": "u1",
        "model": "claude-haiku-4-5",
        "purpose": "experience.derive",
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cost_usd": 0.01,
        "latency_ms": 42,
        "metadata": {},
        "created_at": datetime.now(UTC).isoformat(),
    }
    row.update(over)
    return row


@pytest.mark.asyncio
async def test_record_async_awaits_insert_and_returns_record() -> None:
    sb = MagicMock()
    sb.table.return_value.insert.return_value.execute = AsyncMock(
        return_value=_Resp([_stored_row()])
    )

    rec = await cost_log.record_async(
        sb,
        user_id="u1",
        purpose="experience.derive",
        result=_llm_result(0.01),
        metadata={"prose_doc_id": "p1"},
    )

    assert rec.id == "row-1"
    inserted = sb.table.return_value.insert.call_args[0][0]
    assert inserted["purpose"] == "experience.derive"
    assert inserted["cost_usd"] == 0.01
    assert inserted["output_tokens"] == 5
    assert inserted["metadata"] == {
        "prose_doc_id": "p1",
        "cost_source": "estimated",
        "transport": "unknown",
    }
    sb.table.return_value.insert.return_value.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_record_async_stamps_system_owner_for_none_user() -> None:
    sb = MagicMock()
    sb.table.return_value.insert.return_value.execute = AsyncMock(
        return_value=_Resp([_stored_row(user_id=SYSTEM_USER_ID)])
    )
    await cost_log.record_async(sb, user_id=None, purpose="p", result=_llm_result())
    inserted = sb.table.return_value.insert.call_args[0][0]
    assert inserted["user_id"] == SYSTEM_USER_ID


@pytest.mark.asyncio
async def test_record_async_raises_when_insert_returns_no_rows() -> None:
    sb = MagicMock()
    sb.table.return_value.insert.return_value.execute = AsyncMock(return_value=_Resp([]))
    with pytest.raises(RuntimeError, match="Failed to insert"):
        await cost_log.record_async(sb, user_id="u1", purpose="p", result=_llm_result())


@pytest.mark.asyncio
async def test_record_embedding_async_awaits_insert_and_zeroes_output_tokens() -> None:
    sb = MagicMock()
    sb.table.return_value.insert.return_value.execute = AsyncMock(
        return_value=_Resp([_stored_row(model="voyage-3.5", output_tokens=0)])
    )
    rec = await cost_log.record_embedding_async(
        sb,
        user_id="u1",
        purpose="experience.chunks",
        result=_embedding_result(0.002),
        metadata={"optimized_doc_id": "o1"},
    )
    assert rec.id == "row-1"
    inserted = sb.table.return_value.insert.call_args[0][0]
    # Embedding rows carry input tokens only — output/cache buckets are zeroed.
    assert inserted["input_tokens"] == 8
    assert inserted["output_tokens"] == 0
    assert inserted["cache_read_input_tokens"] == 0
    assert inserted["cache_creation_input_tokens"] == 0
    assert inserted["cost_usd"] == 0.002
    assert inserted["metadata"] == {"optimized_doc_id": "o1"}
    sb.table.return_value.insert.return_value.execute.assert_awaited_once()


# ---- async spend twins (total_spend_async / total_billable_spend_async, PR-F) --
# Mirror the sync total_spend / total_billable_spend tests: RPC-first path plus
# the client-side fallback when the RPC isn't deployed. The async client's
# ``.execute()`` is awaited, so it's mocked with AsyncMock.


@pytest.mark.asyncio
async def test_total_spend_async_uses_rpc_when_available() -> None:
    sb = MagicMock()
    sb.rpc.return_value.execute = AsyncMock(return_value=_Resp(0.42))

    result = await cost_log.total_spend_async(sb, user_id="u1", since=datetime.now(UTC))

    assert result == 0.42
    args, _ = sb.rpc.call_args
    assert args[0] == "total_spend_since"
    assert args[1]["p_user_id"] == "u1"
    # RPC path must not touch the select-table API.
    sb.table.assert_not_called()


@pytest.mark.asyncio
async def test_total_spend_async_stamps_system_owner_for_none_user_id() -> None:
    sb = MagicMock()
    sb.rpc.return_value.execute = AsyncMock(return_value=_Resp(1.5))

    await cost_log.total_spend_async(sb, user_id=None, since=None)

    args, _ = sb.rpc.call_args
    assert args[1]["p_user_id"] == SYSTEM_USER_ID
    assert args[1]["p_since"] is None


@pytest.mark.asyncio
async def test_total_spend_async_zero_when_rpc_returns_none() -> None:
    sb = MagicMock()
    sb.rpc.return_value.execute = AsyncMock(return_value=_Resp(None))

    assert await cost_log.total_spend_async(sb, user_id="u1") == 0.0


@pytest.mark.asyncio
async def test_total_spend_async_falls_back_to_python_when_rpc_unavailable() -> None:
    sb, _ = _fake_client(
        [{"cost_usd": 0.10}, {"cost_usd": 0.25}, {"cost_usd": 0.05}], is_async=True
    )

    result = await cost_log.total_spend_async(
        sb, user_id="u1", since=datetime.now(UTC) - timedelta(hours=1)
    )
    assert result == pytest.approx(0.40)


@pytest.mark.asyncio
async def test_total_billable_spend_async_uses_rpc_when_available() -> None:
    sb = MagicMock()
    sb.rpc.return_value.execute = AsyncMock(return_value=_Resp("0.75"))

    result = await cost_log.total_billable_spend_async(
        sb, user_id="u1", since=datetime.now(UTC), excluded_purposes=("poll_scoring",)
    )

    assert result == pytest.approx(0.75)
    args, _ = sb.rpc.call_args
    assert args[0] == "total_billable_spend_since"
    assert args[1]["p_excluded_purposes"] == ["poll_scoring"]
    sb.table.assert_not_called()


@pytest.mark.asyncio
async def test_total_billable_spend_async_falls_back_when_rpc_unavailable() -> None:
    sb, _ = _fake_client(
        [
            {"cost_usd": 0.10, "purpose": "job_analysis"},
            {"cost_usd": 0.20, "purpose": "poll_scoring"},  # excluded
            {"cost_usd": 0.05, "purpose": "tailor"},
        ],
        is_async=True,
        rpc_error=Exception("not deployed"),
    )

    result = await cost_log.total_billable_spend_async(
        sb,
        user_id="u1",
        since=datetime.now(UTC) - timedelta(hours=1),
        excluded_purposes=("poll_scoring",),
    )
    # 0.10 + 0.05 (poll_scoring excluded).
    assert result == pytest.approx(0.15)


# ---- async ALL-users twins (total_spend_all_async / spend_by_purpose_all_async /
# ---- cache_metrics_all_async, PR-G2c). These power the operator cost-summary on
# ---- the async service client; ``.execute()`` is awaited → AsyncMock. total_spend_all
# ---- keeps the RPC-first / client-fallback shape; the other two are client-side only.


@pytest.mark.asyncio
async def test_total_spend_all_async_uses_rpc_when_available() -> None:
    sb = MagicMock()
    sb.rpc.return_value.execute = AsyncMock(return_value=_Resp(3.75))

    result = await cost_log.total_spend_all_async(sb, since=datetime.now(UTC))

    assert result == 3.75
    args, _ = sb.rpc.call_args
    assert args[0] == "total_spend_all_since"
    assert "p_since" in args[1]
    # The all-users RPC carries no per-user id, and must not touch the table API.
    assert "p_user_id" not in args[1]
    sb.table.assert_not_called()


@pytest.mark.asyncio
async def test_total_spend_all_async_zero_when_rpc_returns_none() -> None:
    sb = MagicMock()
    sb.rpc.return_value.execute = AsyncMock(return_value=_Resp(None))
    assert await cost_log.total_spend_all_async(sb) == 0.0


@pytest.mark.asyncio
async def test_total_spend_all_async_falls_back_to_python_when_rpc_unavailable() -> None:
    sb, _ = _fake_client(
        [{"cost_usd": 1.00}, {"cost_usd": 0.50}, {"cost_usd": 0.25}], is_async=True
    )

    result = await cost_log.total_spend_all_async(sb, since=datetime.now(UTC) - timedelta(days=1))
    assert result == pytest.approx(1.75)


@pytest.mark.asyncio
async def test_spend_by_purpose_all_async_groups_client_side() -> None:
    # No RPC variant — the async twin awaits the select and groups in Python.
    sb, _ = _fake_client(
        [
            {"purpose": "phase1_triage", "cost_usd": 1.0},
            {"purpose": "phase1_triage", "cost_usd": 0.5},
            {"purpose": "fit.job", "cost_usd": 2.0},
        ],
        is_async=True,
    )

    result = await cost_log.spend_by_purpose_all_async(sb, since=datetime.now(UTC))
    assert result == {"phase1_triage": pytest.approx(1.5), "fit.job": pytest.approx(2.0)}
    sb.rpc.assert_not_called()


@pytest.mark.asyncio
async def test_cache_metrics_all_async_sums_token_buckets() -> None:
    sb, _ = _fake_client(
        [
            {
                "input_tokens": 100,
                "cache_read_input_tokens": 800,
                "cache_creation_input_tokens": 100,
            },
            {
                "input_tokens": 0,
                "cache_read_input_tokens": 200,
                "cache_creation_input_tokens": 0,
            },
        ],
        is_async=True,
    )

    result = await cost_log.cache_metrics_all_async(sb, since=datetime.now(UTC))
    assert result == {"cache_read": 1000, "cache_creation": 100, "uncached_input": 100}
    sb.rpc.assert_not_called()


# ---- cost provenance stamped on every LLM row (#933) ------------------------


@pytest.mark.parametrize("source", ["reported", "estimated"])
def test_every_llm_writer_stamps_the_cost_provenance(source: str) -> None:
    """`cost_usd` alone can't tell a reader whether it was billed or guessed.

    Stamped into the jsonb `metadata` (no migration), through all three
    writers, so "are we still estimating in prod?" is answerable from the
    table. The parametrize is the anti-vacuous half: the stamp must TRACK the
    result, not be a constant.
    """
    result = _llm_result()
    result = result.model_copy(update={"cost_source": source})
    assert result.cost_source == source  # precondition

    # 1. sync record
    sb = MagicMock()
    sb.table.return_value.insert.return_value.execute.return_value = _Resp([_stored_row()])
    cost_log.record(sb, user_id="u1", purpose="p", result=result)
    assert sb.table.return_value.insert.call_args[0][0]["metadata"]["cost_source"] == source

    # 2. buffered enqueue
    buffer._drain()
    cost_log.enqueue(user_id=None, purpose="p", result=result)
    assert buffer._drain()[0]["metadata"]["cost_source"] == source


@pytest.mark.asyncio
async def test_record_async_stamps_the_cost_provenance() -> None:
    sb = MagicMock()
    sb.table.return_value.insert.return_value.execute = AsyncMock(
        return_value=_Resp([_stored_row()])
    )
    result = _llm_result().model_copy(update={"cost_source": "reported"})

    await cost_log.record_async(sb, user_id="u1", purpose="p", result=result)

    inserted = sb.table.return_value.insert.call_args[0][0]
    assert inserted["metadata"] == {"cost_source": "reported", "transport": "unknown"}


# ---- #1105: a spend total is never allowed to be quietly short -------------
#
# PostgREST caps one response at 1,000 rows. Every aggregate here used to read
# without paging, so any window bigger than the cap produced a total that was
# too SMALL — and the budget guards read "too small" as "there is room left".
# These pin the three things that keep that from coming back: the readers page,
# a timeout is not swallowed, and an indeterminate total is never returned as a
# number.


def _rows(n: int, each: float = 0.001) -> list[dict[str, Any]]:
    return [{"cost_usd": each, "purpose": "tailor"} for _ in range(n)]


def _timeout() -> APIError:
    return APIError({"message": "canceling statement due to statement timeout", "code": "57014"})


def test_a_window_larger_than_the_row_cap_is_summed_in_full() -> None:
    """The bug itself. 2,500 rows against a 1,000-row cap: unpaged this
    returned a third of the real total and no error."""
    sb, rec = _fake_client(_rows(2500))

    result = cost_log.total_spend_all(sb, since=datetime.now(UTC) - timedelta(days=30))

    assert result == pytest.approx(2.5)  # not 1.0, which is what one page gives
    assert rec["ranges"] == [(0, 999), (1000, 1999), (2000, 2999)]


def test_the_operator_breakdown_also_pages() -> None:
    """``spend_by_purpose_all`` has no RPC behind it, so its read is the
    PRIMARY path — it was truncating on every call, not just on failure."""
    sb, rec = _fake_client(
        [{"purpose": "phase1_triage", "cost_usd": 0.001} for _ in range(1500)]
    )

    result = cost_log.spend_by_purpose_all(sb, since=datetime.now(UTC) - timedelta(days=30))

    assert result == {"phase1_triage": pytest.approx(1.5)}
    assert len(rec["ranges"]) == 2


def test_cache_metrics_also_page() -> None:
    sb, rec = _fake_client(
        [
            {"input_tokens": 1, "cache_read_input_tokens": 2, "cache_creation_input_tokens": 3}
            for _ in range(1200)
        ]
    )

    result = cost_log.cache_metrics_all(sb, since=datetime.now(UTC))

    assert result == {"cache_read": 2400, "cache_creation": 3600, "uncached_input": 1200}
    assert len(rec["ranges"]) == 2


def test_an_exactly_full_page_still_asks_for_the_next_one() -> None:
    """Off-by-one guard: at exactly the cap the reader cannot tell "that was
    everything" from "there is more", so it must ask again."""
    sb, rec = _fake_client(_rows(1000))

    assert cost_log.total_spend_all(sb) == pytest.approx(1.0)
    assert rec["ranges"] == [(0, 999), (1000, 1999)]


def test_a_statement_timeout_does_not_fall_back_to_the_client_side_sum() -> None:
    """A timeout means the exact answer timed out — not that the function is
    missing. Substituting a paged walk of the same rows under the same clock
    is not a fix, and historically it substituted a TRUNCATED one."""
    sb, rec = _fake_client(_rows(5), rpc_error=_timeout())

    with pytest.raises(cost_log.SpendTotalUnavailableError):
        cost_log.total_spend_all(sb, since=datetime.now(UTC))

    assert rec["ranges"] == []  # the fallback was not attempted


def test_a_missing_rpc_still_falls_back() -> None:
    """The case the fallback was actually written for — a deploy where the
    migration has not landed — must keep working."""
    sb, _ = _fake_client(_rows(5), rpc_error=Exception("function does not exist"))

    assert cost_log.total_spend_all(sb, since=datetime.now(UTC)) == pytest.approx(0.005)


def test_a_read_too_large_to_page_refuses_rather_than_returning_a_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pagination itself has a ceiling. Hitting it must raise, not hand
    back the rows gathered so far — a caller that asked for every row and got
    some of them has no way to tell."""
    monkeypatch.setattr(cost_log, "_MAX_READ_PAGES", 2)
    sb, _ = _fake_client(_rows(5000))

    with pytest.raises(cost_log.SpendTotalUnavailableError):
        cost_log.total_spend_all(sb, since=datetime.now(UTC))


@pytest.mark.asyncio
async def test_the_async_readers_page_too() -> None:
    sb, rec = _fake_client(_rows(2500), is_async=True)

    result = await cost_log.total_spend_all_async(sb, since=datetime.now(UTC))

    assert result == pytest.approx(2.5)
    assert rec["ranges"] == [(0, 999), (1000, 1999), (2000, 2999)]
