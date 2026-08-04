"""The fit-payload staleness seam (BUG 2): resolve_current_payload must score
against a payload fresh vs. the CURRENT master document, deriving only when the
persisted optimized doc trails the latest prose.

#57 PR-G2e-5: ``resolve_current_payload`` rides the pooled async service client,
reading prose/optimized through the module-inline async helpers (``_prose_latest``
/ ``_optimized_latest``) and logging cost via ``cost_log.record_async`` — so the
tests patch those seams (the sync ``prose.get_latest`` / ``optimized.get_latest``
twins are no longer on this path).
"""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.experience import OptimizedDoc, OptimizedPayload, ProseDoc
from app.services.experience import derive, resolve
from app.services.experience.resolve import resolve_current_payload
from app.services.llm import cost_log


def _prose(doc_id: str = "p2", content: str = "master doc") -> ProseDoc:
    return ProseDoc(
        id=doc_id, user_id=None, version=2, content=content, created_at=datetime.now(UTC)
    )


def _opt(prose_doc_id: str = "p2", summary: str = "derived") -> OptimizedDoc:
    return OptimizedDoc(
        id="opt-1",
        user_id=None,
        prose_doc_id=prose_doc_id,
        version=1,
        payload=OptimizedPayload(summary=summary),
        markdown_view=None,
        source="llm",
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_fresh_optimized_doc_is_returned_without_deriving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Optimized doc already points at the latest prose → cached payload, NO LLM."""
    monkeypatch.setattr(resolve, "_prose_latest", AsyncMock(return_value=_prose(doc_id="p2")))
    monkeypatch.setattr(
        resolve,
        "_optimized_latest",
        AsyncMock(return_value=_opt(prose_doc_id="p2", summary="fresh")),
    )

    async def _must_not_derive(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("must not derive when the optimized doc is current")

    monkeypatch.setattr(derive, "derive_from_prose", _must_not_derive)

    payload, prose_doc_id = await resolve_current_payload(
        MagicMock(), MagicMock(), cost_supabase=MagicMock(), user_id=None
    )
    assert payload is not None and payload.summary == "fresh"
    assert prose_doc_id == "p2"  # E2 marker = the current prose master id


@pytest.mark.asyncio
async def test_stale_optimized_doc_derives_from_current_prose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prose advanced past the derived doc → derive fresh from the CURRENT prose."""
    monkeypatch.setattr(
        resolve,
        "_prose_latest",
        AsyncMock(return_value=_prose(doc_id="p3", content="NEW master doc")),
    )
    # The persisted doc still points at the older prose p2 — stale.
    monkeypatch.setattr(
        resolve,
        "_optimized_latest",
        AsyncMock(return_value=_opt(prose_doc_id="p2", summary="stale")),
    )

    derived_from: list[str] = []

    async def _derive(_llm: Any, *, prose_text: str) -> Any:
        derived_from.append(prose_text)
        return OptimizedPayload(summary="freshly-derived"), MagicMock()

    monkeypatch.setattr(derive, "derive_from_prose", _derive)
    cost_calls: list[dict[str, Any]] = []

    async def _record_async(*_a: Any, **k: Any) -> None:
        cost_calls.append(k)

    monkeypatch.setattr(cost_log, "record_async", _record_async)

    payload, prose_doc_id = await resolve_current_payload(
        MagicMock(), MagicMock(), cost_supabase=MagicMock(), user_id=None
    )
    assert payload is not None and payload.summary == "freshly-derived"
    assert derived_from == ["NEW master doc"]  # derived from the live master doc
    assert prose_doc_id == "p3"  # marker = the CURRENT prose, not the stale p2
    assert cost_calls and cost_calls[0]["purpose"] == derive.DEFAULT_PURPOSE


@pytest.mark.asyncio
async def test_never_derived_derives_from_prose(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prose exists but no optimized doc yet → derive from prose."""
    monkeypatch.setattr(resolve, "_prose_latest", AsyncMock(return_value=_prose(doc_id="p1")))
    monkeypatch.setattr(resolve, "_optimized_latest", AsyncMock(return_value=None))

    async def _derive(_llm: Any, *, prose_text: str) -> Any:
        return OptimizedPayload(summary="from-prose"), MagicMock()

    monkeypatch.setattr(derive, "derive_from_prose", _derive)
    monkeypatch.setattr(cost_log, "record_async", AsyncMock())

    payload, prose_doc_id = await resolve_current_payload(
        MagicMock(), MagicMock(), cost_supabase=MagicMock(), user_id=None
    )
    assert payload is not None and payload.summary == "from-prose"
    assert prose_doc_id == "p1"


@pytest.mark.asyncio
async def test_no_prose_falls_back_to_optimized(monkeypatch: pytest.MonkeyPatch) -> None:
    """No master doc to derive from → use whatever optimized doc exists, no LLM."""
    monkeypatch.setattr(resolve, "_prose_latest", AsyncMock(return_value=None))
    monkeypatch.setattr(
        resolve, "_optimized_latest", AsyncMock(return_value=_opt(summary="only-optimized"))
    )

    async def _must_not_derive(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("no prose → nothing to derive")

    monkeypatch.setattr(derive, "derive_from_prose", _must_not_derive)

    payload, prose_doc_id = await resolve_current_payload(
        MagicMock(), MagicMock(), cost_supabase=MagicMock(), user_id=None
    )
    assert payload is not None and payload.summary == "only-optimized"
    assert prose_doc_id is None  # no prose master → unversioned (stays stale)


@pytest.mark.asyncio
async def test_nothing_at_all_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resolve, "_prose_latest", AsyncMock(return_value=None))
    monkeypatch.setattr(resolve, "_optimized_latest", AsyncMock(return_value=None))
    payload, prose_doc_id = await resolve_current_payload(
        MagicMock(), MagicMock(), cost_supabase=MagicMock(), user_id=None
    )
    assert payload is None
    assert prose_doc_id is None
