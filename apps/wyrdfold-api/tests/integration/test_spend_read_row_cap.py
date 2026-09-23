"""#1105: real PostgREST caps a response at 1,000 rows — prove paging beats it.

The unit tests use a fake that ASSERTS the cap. Only the real server proves
the cap is there, and that is the whole premise of the fix: every spend
aggregate used to read in one shot, so any window wider than the cap produced
a total that was too SMALL, and the budget guards read "too small" as "there
is room left".

This seeds more rows than one page holds and checks two things against the
real stack: that an unpaged read really does come back short (so the bug was
real, not theoretical), and that the shipped reader returns the full total.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from supabase import Client

from app.services.llm import cost_log

pytestmark = pytest.mark.integration

_ROWS = 1250  # more than one page, few enough to seed quickly
_EACH = 0.002


@pytest.fixture()
def _seeded_user(service_client: Client) -> Any:
    from tests.integration.conftest import create_auth_user, delete_auth_user

    uid = create_auth_user(service_client)
    now = datetime.now(UTC)
    rows = [
        {
            "id": str(uuid.uuid4()),
            "user_id": uid,
            "model": "claude-haiku-4-5",
            "purpose": "tailor",
            "input_tokens": 1,
            "output_tokens": 1,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cost_usd": _EACH,
            "latency_ms": 1,
            "metadata": {},
            "created_at": (now - timedelta(minutes=i % 600)).isoformat(),
        }
        for i in range(_ROWS)
    ]
    for i in range(0, len(rows), 500):
        service_client.table("llm_costs").insert(rows[i : i + 500]).execute()
    try:
        yield uid
    finally:
        service_client.table("llm_costs").delete().eq("user_id", uid).execute()
        delete_auth_user(service_client, uid)


def test_an_unpaged_read_really_is_capped(_seeded_user: str, service_client: Client) -> None:
    """The premise. If this ever stops being true the fix can be simplified,
    so assert it rather than trusting the note."""
    resp = (
        service_client.table("llm_costs")
        .select("cost_usd")
        .eq("user_id", _seeded_user)
        .execute()
    )
    assert len(resp.data) == 1000, "PostgREST no longer caps at 1,000 — revisit #1105"
    assert len(resp.data) < _ROWS


def test_the_shipped_reader_returns_the_full_total(
    _seeded_user: str, service_client: Client
) -> None:
    """What the guards actually call. Against real PostgREST, over a window
    wider than one page, the total must be complete."""
    total = cost_log._total_spend_python(service_client, _seeded_user, None)

    assert total == pytest.approx(round(_ROWS * _EACH, 6))
    # And it is strictly more than one capped page would have produced — the
    # number the guards were being given before (#1105).
    assert total > round(1000 * _EACH, 6)
