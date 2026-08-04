"""Unit coverage for the contribution-vote suppression wiring (#29).

The race fix moves the tally→compare→write into the atomic
``recompute_contribution_suppression`` RPC (a FOR UPDATE row lock serializes
concurrent recomputes). The correctness of the tally/quorum/rescue logic is
proven end-to-end against a live DB in
``tests/integration/test_contribution_voting.py``; these deterministic units
just pin that ``recompute_suppression`` delegates to that RPC (not the old
read-modify-write) and parses its single-row result.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.targets import votes


def _rpc_client(data: object) -> MagicMock:
    """A service client whose ``recompute_contribution_suppression`` RPC awaits to
    ``data`` (#57 PR-G2e-5: the tally RPC rides the async service client now)."""
    sb = MagicMock()
    sb.rpc.return_value.execute = AsyncMock(return_value=MagicMock(data=data))
    return sb


@pytest.mark.asyncio
async def test_recompute_suppression_calls_the_atomic_rpc() -> None:
    sb = _rpc_client([{"suppressed": True, "changed": True}])

    result = await votes.recompute_suppression(sb, reference_jd_id="ref-1", quorum=2)

    assert result == (True, True)
    # Delegated to the DB function (atomic under a row lock), not the old
    # Python read-modify-write over the votes / reference_jds tables.
    sb.rpc.assert_called_once_with(
        "recompute_contribution_suppression",
        {"p_reference_jd_id": "ref-1", "p_quorum": 2},
    )
    sb.table.assert_not_called()


@pytest.mark.asyncio
async def test_recompute_suppression_missing_row_is_noop() -> None:
    """Contribution deleted between vote and recompute → (False, False), no crash."""
    sb = _rpc_client([])

    assert await votes.recompute_suppression(sb, reference_jd_id="gone", quorum=2) == (
        False,
        False,
    )


@pytest.mark.asyncio
async def test_recompute_suppression_coerces_rpc_booleans() -> None:
    """The RPC returns real booleans; make sure they pass through as a tuple."""
    sb = _rpc_client([{"suppressed": False, "changed": False}])

    assert await votes.recompute_suppression(sb, reference_jd_id="ref-9", quorum=3) == (
        False,
        False,
    )
