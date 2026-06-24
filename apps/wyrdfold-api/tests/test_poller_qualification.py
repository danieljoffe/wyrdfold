"""Poller wiring for the #60 qualification firewall.

Covers ``poller._qualify_one_job`` / ``_qualify_jobs``:

- A new row (no ``qualified_hash``) is tagged: the LLM is called, cost is
  enqueued, and the full tag payload (+ ``qualified_at`` / ``qualified_hash``)
  is written back to the row.
- An unchanged row (``qualified_hash`` already matches its content + a prior
  ``qualified_at``) is skipped: no LLM call, no DB write — the content-hash
  cache makes a re-poll free.
- A changed row (content differs from the stored hash) is re-tagged.
- The step is best-effort: a tagger failure (tags=None) writes nothing and
  never raises; a write failure is swallowed.
- It bills the instance key (``get_llm_client(supabase, None)``), never a
  per-target payer.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.services import poller as poller_mod
from app.services.qualification import QualificationTags, qualification_hash

_TAGS = QualificationTags(
    is_us=True,
    us_confidence=98,
    role_family="engineering",
    seniority="senior_ic",
    employment_type="full_time",
    metro="San Francisco",
    is_remote=False,
    is_genuine_role=True,
)


def _row(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "job-1",
        "title": "Staff Engineer",
        "company_name": "Acme",
        "location": "San Francisco, CA",
        "description_html": "<p>Build things.</p>",
    }
    base.update(kw)
    return base


def _patch_common(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tag_result: tuple[QualificationTags | None, object | None],
) -> dict[str, Any]:
    """Patch the poller's LLM client + tagger + cost-log + DB write, returning
    a recorder dict the tests assert against."""
    rec: dict[str, Any] = {
        "tag_calls": 0,
        "writes": [],
        "cost_calls": 0,
        "client_user_id": "UNSET",
    }

    def fake_get_client(supabase: object, user_id: str | None) -> object:
        rec["client_user_id"] = user_id
        return MagicMock(name="instance-client")

    async def fake_tag_job(_llm: object, **kwargs: Any) -> Any:
        rec["tag_calls"] += 1
        rec["last_tag_kwargs"] = kwargs
        return tag_result

    def fake_enqueue(user_id: str | None, purpose: str, result: object) -> None:
        rec["cost_calls"] += 1
        rec["cost_user_id"] = user_id
        rec["cost_purpose"] = purpose

    def fake_execute_with_retry_sync(fn: Any, *, label: str = "") -> Any:
        # The poller passes ``supabase.table(...).update(payload).eq(...).execute``
        # — a bound MagicMock method. We don't need to run it; record that a
        # write was attempted. The payload is captured separately below.
        return MagicMock(data=[])

    monkeypatch.setattr(poller_mod, "get_llm_client", fake_get_client)
    monkeypatch.setattr(poller_mod, "tag_job", fake_tag_job)
    monkeypatch.setattr(poller_mod, "enqueue_llm_cost", fake_enqueue)
    monkeypatch.setattr(
        poller_mod, "execute_with_retry_sync", fake_execute_with_retry_sync
    )
    return rec


def _supabase_capturing_updates(rec: dict[str, Any]) -> MagicMock:
    """A supabase mock whose ``.table('jobs').update(payload)`` records the
    payload into ``rec['writes']``."""
    sb = MagicMock()

    def update(payload: dict[str, Any]) -> MagicMock:
        rec["writes"].append(payload)
        chain = MagicMock()
        chain.eq.return_value.execute = MagicMock()
        return chain

    sb.table.return_value.update.side_effect = update
    return sb


class TestQualifyOneJob:
    @pytest.mark.asyncio
    async def test_new_row_is_tagged_and_written(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = _patch_common(monkeypatch, tag_result=(_TAGS, object()))
        sb = _supabase_capturing_updates(rec)

        await poller_mod._qualify_jobs(sb, [_row()])

        assert rec["tag_calls"] == 1
        assert rec["cost_calls"] == 1
        assert rec["cost_user_id"] is None  # instance key, not a payer
        assert rec["cost_purpose"] == "qualification.tagger"
        assert len(rec["writes"]) == 1
        payload = rec["writes"][0]
        # Every tag column maps through.
        assert payload["is_us"] is True
        assert payload["role_family"] == "engineering"
        assert payload["seniority"] == "senior_ic"
        assert payload["employment_type"] == "full_time"
        assert payload["metro"] == "San Francisco"
        assert payload["is_remote"] is False
        assert payload["is_genuine_role"] is True
        assert payload["us_confidence"] == 98
        assert payload["qualified_at"] is not None
        # The persisted hash matches the row's content hash.
        assert payload["qualified_hash"] == qualification_hash(
            title="Staff Engineer",
            company="Acme",
            location="San Francisco, CA",
            description="<p>Build things.</p>",
        )

    @pytest.mark.asyncio
    async def test_unchanged_row_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = _patch_common(monkeypatch, tag_result=(_TAGS, object()))
        sb = _supabase_capturing_updates(rec)

        existing_hash = qualification_hash(
            title="Staff Engineer",
            company="Acme",
            location="San Francisco, CA",
            description="<p>Build things.</p>",
        )
        row = _row(qualified_hash=existing_hash, qualified_at="2026-06-24T00:00:00Z")

        await poller_mod._qualify_jobs(sb, [row])

        # Cache hit: no LLM call, no cost, no write.
        assert rec["tag_calls"] == 0
        assert rec["cost_calls"] == 0
        assert rec["writes"] == []

    @pytest.mark.asyncio
    async def test_changed_content_is_retagged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = _patch_common(monkeypatch, tag_result=(_TAGS, object()))
        sb = _supabase_capturing_updates(rec)

        # Stored hash is for the OLD description; the row's current content
        # differs, so the cache must miss and re-tag.
        stale_hash = qualification_hash(
            title="Staff Engineer",
            company="Acme",
            location="San Francisco, CA",
            description="<p>OLD body.</p>",
        )
        row = _row(qualified_hash=stale_hash, qualified_at="2026-06-24T00:00:00Z")

        await poller_mod._qualify_jobs(sb, [row])

        assert rec["tag_calls"] == 1
        assert len(rec["writes"]) == 1

    @pytest.mark.asyncio
    async def test_tagger_failure_writes_nothing_and_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # tag_job fails soft → (None, None). The poller must not write or raise.
        rec = _patch_common(monkeypatch, tag_result=(None, None))
        sb = _supabase_capturing_updates(rec)

        await poller_mod._qualify_jobs(sb, [_row()])

        assert rec["tag_calls"] == 1
        assert rec["cost_calls"] == 0
        assert rec["writes"] == []

    @pytest.mark.asyncio
    async def test_client_resolution_failure_skips_silently(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(_sb: object, _uid: str | None) -> object:
            raise RuntimeError("no client")

        monkeypatch.setattr(poller_mod, "get_llm_client", boom)
        # tag_job should never be reached.
        called = {"n": 0}

        async def fake_tag_job(*_a: object, **_k: object) -> Any:
            called["n"] += 1
            return None, None

        monkeypatch.setattr(poller_mod, "tag_job", fake_tag_job)

        # Must not raise even though the client can't be resolved.
        await poller_mod._qualify_jobs(MagicMock(), [_row()])
        assert called["n"] == 0
