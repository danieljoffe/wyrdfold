"""Tenant-isolation regression tests (audit #24).

F1 — reference-JD deletes must be constrained to the ownership-checked
target, not just the ref_jd_id.
F2 — /analysis must reject JWT callers not linked to the target_id they
name, since the LLM blend writes to the shared (job, target) scores row.
F3 — learn-llm must sit behind the LLM budget gate.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.dependencies import (
    enforce_llm_budget,
    get_current_user_id,
    get_current_user_id_optional,
    get_llm_client,
    get_supabase,
    verify_api_key_or_jwt,
)
from app.services.llm.mock import MockLLMClient
from app.services.targets import crud

# ---------------------------------------------------------------------------
# F1 — crud.delete_reference_jd is scoped to the target
# ---------------------------------------------------------------------------


def test_delete_reference_jd_constrains_on_target_id() -> None:
    supabase = MagicMock()
    delete_chain = supabase.table.return_value.delete.return_value
    delete_chain.eq.return_value.eq.return_value.execute.return_value.data = [
        {"id": "ref-1"}
    ]

    assert crud.delete_reference_jd(supabase, "ref-1", target_id="tgt-1") is True

    supabase.table.assert_called_once_with(crud.REF_JDS_TABLE)
    delete_chain.eq.assert_called_once_with("id", "ref-1")
    delete_chain.eq.return_value.eq.assert_called_once_with("target_id", "tgt-1")


def test_delete_reference_jd_returns_false_when_not_in_target() -> None:
    """A ref_jd_id belonging to a different target matches no row."""
    supabase = MagicMock()
    delete_chain = supabase.table.return_value.delete.return_value
    delete_chain.eq.return_value.eq.return_value.execute.return_value.data = []

    assert (
        crud.delete_reference_jd(supabase, "ref-other-target", target_id="tgt-1")
        is False
    )


# ---------------------------------------------------------------------------
# F2 — /analysis ownership gate
# ---------------------------------------------------------------------------


def _client_with_overrides(overrides: dict[Any, Any]) -> Any:
    from fastapi.testclient import TestClient

    from app.main import app

    app.dependency_overrides.update(overrides)
    return TestClient(app)


async def test_analysis_unowned_target_is_404_before_any_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JWT caller not linked to target_id → 404; no cache read, no LLM call."""
    monkeypatch.setattr(
        crud, "get_user_target_ids", lambda *_a, **_kw: {"tgt-owned"}
    )
    from app.services.analysis import persistence as persistence_mod

    get_cached = MagicMock()
    monkeypatch.setattr(persistence_mod, "get_cached", get_cached)

    llm = MockLLMClient()
    from app.main import app

    tc = _client_with_overrides(
        {
            get_supabase: lambda: MagicMock(),
            get_llm_client: lambda: llm,
            verify_api_key_or_jwt: lambda: "jwt",
            get_current_user_id_optional: lambda: "user-a",
            enforce_llm_budget: lambda: None,
        }
    )
    try:
        resp = tc.post("/analysis/job-1?target_id=tgt-not-mine")
        assert resp.status_code == 404
        assert len(llm.calls) == 0
        get_cached.assert_not_called()
    finally:
        app.dependency_overrides.clear()


async def test_analysis_owned_target_passes_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Linked caller proceeds past the ownership gate (404s later on the
    missing optimized doc, NOT on ownership)."""
    monkeypatch.setattr(
        crud, "get_user_target_ids", lambda *_a, **_kw: {"tgt-owned"}
    )
    from app.services.experience import optimized as opt_mod

    monkeypatch.setattr(opt_mod, "get_latest", lambda *_a, **_kw: None)

    from app.main import app

    tc = _client_with_overrides(
        {
            get_supabase: lambda: MagicMock(),
            get_llm_client: lambda: MockLLMClient(),
            verify_api_key_or_jwt: lambda: "jwt",
            get_current_user_id_optional: lambda: "user-a",
            enforce_llm_budget: lambda: None,
        }
    )
    try:
        resp = tc.post("/analysis/job-1?target_id=tgt-owned")
        assert resp.status_code == 404
        assert "optimized doc" in resp.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# F3 — learn-llm sits behind the budget gate
# ---------------------------------------------------------------------------


async def test_learn_llm_blocked_when_budget_exhausted() -> None:
    def _over_budget() -> None:
        raise HTTPException(status_code=429, detail="LLM budget exhausted")

    from app.main import app

    tc = _client_with_overrides(
        {
            get_supabase: lambda: MagicMock(),
            get_llm_client: lambda: MockLLMClient(),
            get_current_user_id: lambda: "user-a",
            enforce_llm_budget: _over_budget,
        }
    )
    try:
        resp = tc.post("/targets/tgt-1/learn-llm")
        assert resp.status_code == 429
    finally:
        app.dependency_overrides.clear()
