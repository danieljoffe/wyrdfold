"""#60 workstream D: default job-list relevance floor.

`_default_min_score_for_user` applies `settings.default_list_min_score` when a
user hasn't configured their own `list_min_score`; an explicit stored 0 opts
out; a positive value is used as-is.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.routers.jobs import _default_min_score_for_user

# _default_min_score_for_user is async now (#57 slice 3) — it awaits the profile
# read on the async user client.
pytestmark = pytest.mark.asyncio


def _supabase(row: dict | None, *, resp_is_none: bool = False) -> MagicMock:
    sb = MagicMock()
    chain = sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value
    chain.execute = AsyncMock(return_value=None if resp_is_none else MagicMock(data=row))
    return sb


async def test_null_list_min_score_uses_system_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "default_list_min_score", 40)
    assert await _default_min_score_for_user(_supabase({"list_min_score": None}), "u1") == 40


async def test_missing_profile_row_uses_system_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "default_list_min_score", 40)
    assert await _default_min_score_for_user(_supabase(None), "u1") == 40
    assert await _default_min_score_for_user(_supabase(None, resp_is_none=True), "u1") == 40


async def test_explicit_zero_opts_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stored 0 is a deliberate 'no floor' — the default must not override it."""
    monkeypatch.setattr(settings, "default_list_min_score", 40)
    assert await _default_min_score_for_user(_supabase({"list_min_score": 0}), "u1") is None


async def test_positive_value_used_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "default_list_min_score", 40)
    assert await _default_min_score_for_user(_supabase({"list_min_score": 30}), "u1") == 30


async def test_default_disabled_instance_wide(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting the instance default to 0 disables the floor for unset users."""
    monkeypatch.setattr(settings, "default_list_min_score", 0)
    assert await _default_min_score_for_user(_supabase({"list_min_score": None}), "u1") is None
    assert await _default_min_score_for_user(_supabase(None), "u1") is None
