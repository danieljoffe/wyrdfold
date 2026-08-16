"""``document_versions.payload_md`` must survive serialization.

Regression cover for the /jobs sweep P0: the resume review page restores a
version from ``payload_md``, but ``ResumeVersion`` had no such field and set
``model_config = {"extra": "ignore"}``. ``list_for_resume`` does
``select("*")`` — so Postgres returned the markdown and Pydantic dropped it on
the floor, making *every* version fail restore with "This version predates
markdown — cannot restore", including one generated seconds earlier.

The column has existed since the base schema and ``record()`` has always
written it. Only the serializer was missing, which is why the bug was invisible
from the database side.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.tailor import TailoredResumeRecord
from app.services.tailor import versions

pytestmark = pytest.mark.asyncio


def _row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "ver-1",
        "resume_id": "res-1",
        "payload": {"summary": "structured"},
        "payload_md": "# Daniel Joffe\n\nSummary here.",
        "source": "user_edit",
        "created_at": "2026-08-16T01:12:19.597088Z",
    }
    row.update(over)
    return row


def _supabase_returning(rows: list[dict[str, Any]]) -> MagicMock:
    supabase = MagicMock()
    chain = supabase.table.return_value.select.return_value.eq.return_value
    chain.order.return_value.limit.return_value.execute = AsyncMock(
        return_value=MagicMock(data=rows)
    )
    return supabase


class TestResumeVersionSerialization:
    async def test_payload_md_survives_list_for_resume(self) -> None:
        """The field the UI restores from must reach the caller."""
        supabase = _supabase_returning([_row()])

        result = await versions.list_for_resume(supabase, "res-1")

        assert len(result) == 1
        assert result[0].payload_md == "# Daniel Joffe\n\nSummary here."

    async def test_payload_md_survives_model_dump(self) -> None:
        """The router returns ``v.model_dump(mode="json")`` — the exact step
        that used to erase the markdown."""
        supabase = _supabase_returning([_row()])

        result = await versions.list_for_resume(supabase, "res-1")
        dumped = result[0].model_dump(mode="json")

        assert "payload_md" in dumped
        assert dumped["payload_md"] == "# Daniel Joffe\n\nSummary here."

    async def test_markdown_less_rows_are_still_readable(self) -> None:
        """Rows genuinely written without markdown must not blow up — they're
        the case the 'predates markdown' guard legitimately exists for."""
        supabase = _supabase_returning([_row(payload_md=None)])

        result = await versions.list_for_resume(supabase, "res-1")

        assert result[0].payload_md is None

    async def test_missing_column_defaults_to_none(self) -> None:
        row = _row()
        del row["payload_md"]
        supabase = _supabase_returning([row])

        result = await versions.list_for_resume(supabase, "res-1")

        assert result[0].payload_md is None


class TestVersionRecording:
    async def test_record_persists_payload_md(self) -> None:
        supabase = MagicMock()
        insert = supabase.table.return_value.insert
        insert.return_value.execute = AsyncMock(return_value=MagicMock(data=[]))
        supabase.table.return_value.select.return_value.eq.return_value.order.return_value.execute = AsyncMock(
            return_value=MagicMock(data=[])
        )

        await versions.record(
            supabase,
            resume_id="res-1",
            payload={"summary": "s"},
            source="initial",
            payload_md="# md",
        )

        written = insert.call_args.args[0]
        assert written["payload_md"] == "# md"

    async def test_update_payload_forwards_markdown_to_the_snapshot(self) -> None:
        """``persistence.update_payload`` used to snapshot without markdown,
        producing versions that could be listed but never restored."""
        from app.services.tailor import persistence

        # Build the row from the real model rather than hand-rolling a dict —
        # ``update_payload`` validates the response, so a partial fixture fails
        # on missing columns instead of on the behaviour under test.
        record = TailoredResumeRecord(
            id="res-1",
            user_id=None,
            job_posting_id=None,
            document_type="resume",
            resume_type="generic",
            jd_snapshot="JD",
            jd_snapshot_hash="h",
            payload={"summary": "s"},
            payload_md=None,
            docx_payload_md_hash=None,
            storage_path=None,
            warnings=[],
            model="claude-sonnet-4-6",
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.0,
            latency_ms=1,
            created_at=datetime(2026, 8, 16, 1, 0, 0, tzinfo=UTC),
            approved_at=None,
        )
        supabase = MagicMock()
        supabase.table.return_value.update.return_value.eq.return_value.eq.return_value.execute = (
            AsyncMock(return_value=MagicMock(data=[record.model_dump(mode="json")]))
        )

        recorded: dict[str, Any] = {}

        async def _capture(_sb: Any, **kwargs: Any) -> None:
            recorded.update(kwargs)

        original = versions.record
        versions.record = _capture  # type: ignore[assignment]
        try:
            await persistence.update_payload(
                supabase,
                "res-1",
                {"summary": "s"},
                user_id=None,
                payload_md="# restored me",
            )
        finally:
            versions.record = original  # type: ignore[assignment]

        assert recorded["payload_md"] == "# restored me"
