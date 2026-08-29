"""cost_log: RPC-first spend queries with Python fallback, plus enqueue."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.constants import SYSTEM_USER_ID
from app.models.embeddings import EmbeddingResult, EmbeddingUsage
from app.models.llm import LLMResult, LLMUsage
from app.services.llm import cost_log
from app.services.llm.cost_log_buffer import buffer


class _Resp:
    def __init__(self, data: Any) -> None:
        self.data = data


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
    sb = MagicMock()
    sb.rpc.side_effect = Exception("function does not exist")

    # Fallback path: select cost_usd, sum in Python.
    sel = sb.table.return_value.select.return_value
    sel.eq.return_value.gte.return_value.execute.return_value = _Resp(
        [{"cost_usd": 0.10}, {"cost_usd": 0.25}, {"cost_usd": 0.05}]
    )

    result = cost_log.total_spend(sb, user_id="u1", since=datetime.now(UTC) - timedelta(hours=1))
    assert result == pytest.approx(0.40)


def test_total_spend_fallback_reads_system_partition_for_none_user() -> None:
    # RPC unavailable + no user (cron) → the Python fallback filters on the
    # SYSTEM partition via eq, NOT the retired is_("user_id","null") branch.
    sb = MagicMock()
    sb.rpc.side_effect = Exception("not deployed")
    sel = sb.table.return_value.select.return_value
    sel.eq.return_value.gte.return_value.execute.return_value = _Resp([{"cost_usd": 0.5}])

    result = cost_log.total_spend(sb, user_id=None, since=datetime.now(UTC) - timedelta(hours=1))

    assert result == pytest.approx(0.5)
    sel.eq.assert_called_once_with("user_id", SYSTEM_USER_ID)
    sel.is_.assert_not_called()


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
    sb = MagicMock()
    sb.rpc.side_effect = Exception("not deployed")

    sel = sb.table.return_value.select.return_value
    sel.eq.return_value.execute.return_value = _Resp(
        [
            {"purpose": "job_analysis", "cost_usd": 0.10},
            {"purpose": "job_analysis", "cost_usd": 0.20},
            {"purpose": "tailor", "cost_usd": 0.05},
        ]
    )

    result = cost_log.spend_by_purpose(sb, user_id="u1")
    assert result == {"job_analysis": pytest.approx(0.30), "tailor": pytest.approx(0.05)}


# ---- cache_metrics_all -----------------------------------------------------


def test_cache_metrics_all_sums_token_buckets() -> None:
    sb = MagicMock()
    sel = sb.table.return_value.select.return_value
    sel.execute.return_value = _Resp(
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
    assert inserted["metadata"] == {"prose_doc_id": "p1", "cost_source": "estimated"}
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
    sb = MagicMock()
    sb.rpc.side_effect = Exception("function does not exist")

    # Fallback path: select cost_usd, sum in Python — awaited on the async client.
    sel = sb.table.return_value.select.return_value
    sel.eq.return_value.gte.return_value.execute = AsyncMock(
        return_value=_Resp([{"cost_usd": 0.10}, {"cost_usd": 0.25}, {"cost_usd": 0.05}])
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
    sb = MagicMock()
    sb.rpc.side_effect = Exception("not deployed")

    # Fallback: select (cost_usd, purpose), filter out excluded purposes, sum.
    sel = sb.table.return_value.select.return_value
    sel.eq.return_value.gte.return_value.execute = AsyncMock(
        return_value=_Resp(
            [
                {"cost_usd": 0.10, "purpose": "job_analysis"},
                {"cost_usd": 0.20, "purpose": "poll_scoring"},  # excluded
                {"cost_usd": 0.05, "purpose": "tailor"},
            ]
        )
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
    sb = MagicMock()
    sb.rpc.side_effect = Exception("function does not exist")

    # Fallback: select cost_usd across ALL users (no user filter), sum in Python.
    sel = sb.table.return_value.select.return_value
    sel.gte.return_value.execute = AsyncMock(
        return_value=_Resp([{"cost_usd": 1.00}, {"cost_usd": 0.50}, {"cost_usd": 0.25}])
    )

    result = await cost_log.total_spend_all_async(sb, since=datetime.now(UTC) - timedelta(days=1))
    assert result == pytest.approx(1.75)


@pytest.mark.asyncio
async def test_spend_by_purpose_all_async_groups_client_side() -> None:
    # No RPC variant — the async twin awaits the select and groups in Python.
    sb = MagicMock()
    sel = sb.table.return_value.select.return_value
    sel.gte.return_value.execute = AsyncMock(
        return_value=_Resp(
            [
                {"purpose": "phase1_triage", "cost_usd": 1.0},
                {"purpose": "phase1_triage", "cost_usd": 0.5},
                {"purpose": "fit.job", "cost_usd": 2.0},
            ]
        )
    )

    result = await cost_log.spend_by_purpose_all_async(sb, since=datetime.now(UTC))
    assert result == {"phase1_triage": pytest.approx(1.5), "fit.job": pytest.approx(2.0)}
    sb.rpc.assert_not_called()


@pytest.mark.asyncio
async def test_cache_metrics_all_async_sums_token_buckets() -> None:
    sb = MagicMock()
    sel = sb.table.return_value.select.return_value
    sel.gte.return_value.execute = AsyncMock(
        return_value=_Resp(
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
            ]
        )
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
    assert inserted["metadata"] == {"cost_source": "reported"}
