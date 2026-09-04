"""#842: the insights target scope is ACTIVE memberships only.

The dashboard's advertised counts (score distribution chips, target
comparison) link into /jobs, which lists active targets only. Every
insights endpoint resolves its scope through ``_user_target_ids``, so
this pins that helper's filter — with a fake that filters FOR REAL, so
dropping either ``.eq`` from the query visibly changes the result
(``feedback_mocks_that_cant_fail``: a fake that ignores the filters
would bless any query shape).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.routers.insights import _user_target_ids

pytestmark = pytest.mark.asyncio

_ROWS = [
    {"user_id": "u1", "target_id": "t-active-1", "is_active": True},
    {"user_id": "u1", "target_id": "t-active-2", "is_active": True},
    {"user_id": "u1", "target_id": "t-inactive", "is_active": False},
    {"user_id": "u2", "target_id": "t-other-user", "is_active": True},
    {"user_id": "u4", "target_id": "t-u4-inactive", "is_active": False},
]


class _Resp:
    def __init__(self, data: list[dict[str, Any]]) -> None:
        self.data = data


class _Query:
    """Filters the seeded membership rows by every ``.eq`` the code sends."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def select(self, *_a: Any, **_kw: Any) -> _Query:
        return self

    def eq(self, col: str, value: Any) -> _Query:
        self._rows = [r for r in self._rows if r.get(col) == value]
        return self

    async def execute(self) -> _Resp:
        return _Resp([{"target_id": r["target_id"]} for r in self._rows])


class _Supabase:
    def table(self, name: str) -> _Query:
        assert name == "user_targets"
        return _Query(list(_ROWS))


async def test_scope_is_the_callers_active_memberships_only() -> None:
    ids = await _user_target_ids(_Supabase(), "u1")  # type: ignore[arg-type]
    assert ids == {"t-active-1", "t-active-2"}


async def test_zero_active_memberships_scope_is_empty() -> None:
    """A user whose targets are ALL inactive gets an empty scope — every
    endpoint's existing empty-payload early-return then fires, which is
    the state the dashboard names ("activate a target"). Before #842 this
    user got full aggregates over work that wasn't happening."""
    ids = await _user_target_ids(_Supabase(), "u4")  # type: ignore[arg-type]
    assert ids == set()
