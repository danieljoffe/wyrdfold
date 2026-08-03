"""Unit tests for the shared ``job_ingest.materialize_and_score_job`` helper.

These exercise the helper's own guarantees directly (not just indirectly through
``POST /jobs/manual``), because it is now the durable centerpiece shared with the
from-url target flow:

- materialize-but-skip-scoring when there is no title or no targets (the posting
  still becomes a row so it can be scored later)
- per-target scoring failures are isolated — one bad target never loses the rest,
  fails the materialize, or skips the global-score / force-include steps
- force-include is best-effort — an RPC failure never fails the materialize
- ``set_included=False`` skips the force-include RPC entirely
- an upsert that returns no row yields ``None`` and scores nothing
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.cache import job_list_cache
from app.models.targets import JobTarget, ScoringProfile
from app.services import job_ingest
from app.services.extract import MANUAL_SOURCE_ID

# A realistic-ish posting body so salary extraction + sanitize run on real text.
DESCRIPTION_HTML = "<p>Build things. Compensation: $180,000 - $220,000.</p>"


def _target(id: str = "tgt-1") -> JobTarget:
    return JobTarget(
        id=id,
        label="Senior Engineer",
        scoring_profile=ScoringProfile(),
        app_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _supabase(*, job_id: str | None = "job-1") -> MagicMock:
    """A service-role client whose jobs upsert returns one row (or none)."""
    supabase = MagicMock()
    data = [{"id": job_id}] if job_id is not None else []
    supabase.table.return_value.upsert.return_value.execute.return_value = MagicMock(data=data)
    return supabase


def _rpc_calls(supabase: MagicMock) -> list[str]:
    return [c.args[0] for c in supabase.rpc.call_args_list]


@pytest.fixture
def invalidations(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Neutralize the scoring collaborators + record cache invalidations.

    Individual tests re-patch ``score_and_upsert`` when they want to assert
    on those calls.
    """
    monkeypatch.setattr(job_ingest, "parse_jd", lambda _desc: {})
    monkeypatch.setattr(job_ingest, "sanitize_html", lambda s: s)
    hits: list[int] = []
    # Same singleton job_ingest imported — patching here neutralizes it there.
    monkeypatch.setattr(job_list_cache, "invalidate", lambda: hits.append(1))
    return hits


async def _materialize(supabase: MagicMock, **overrides: object) -> str | None:
    kwargs: dict[str, object] = {
        "final_url": "https://example.com/jobs/1",
        "title": "Senior Engineer",
        "company_name": "Acme",
        "location": None,
        "description_html": DESCRIPTION_HTML,
        "salary_text": None,
        "targets": [_target()],
    }
    kwargs.update(overrides)
    return await job_ingest.materialize_and_score_job(supabase, **kwargs)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_no_title_materializes_but_skips_scoring(
    monkeypatch: pytest.MonkeyPatch, invalidations: list[int]
) -> None:
    """No extracted title → the job row is still created (so it can be scored
    later) but scoring + force-include are skipped."""
    supabase = _supabase()
    score = MagicMock()
    monkeypatch.setattr(job_ingest, "score_and_upsert", score)

    posting_id = await _materialize(supabase, title=None)

    assert posting_id == "job-1"  # row materialized
    score.assert_not_called()  # nothing scored
    assert "user_set_scores_included" not in _rpc_calls(supabase)  # not force-included
    assert invalidations == [1]  # cache still invalidated (a new row appeared)
    # The upsert kept the manual pseudo-source + a null title.
    row = supabase.table.return_value.upsert.call_args[0][0]
    assert row["source_id"] == MANUAL_SOURCE_ID
    assert row["title"] is None


@pytest.mark.asyncio
async def test_no_targets_materializes_but_skips_scoring(
    monkeypatch: pytest.MonkeyPatch, invalidations: list[int]
) -> None:
    supabase = _supabase()
    score = MagicMock()
    monkeypatch.setattr(job_ingest, "score_and_upsert", score)

    posting_id = await _materialize(supabase, targets=[])

    assert posting_id == "job-1"
    score.assert_not_called()
    assert "user_set_scores_included" not in _rpc_calls(supabase)
    assert invalidations == [1]


@pytest.mark.asyncio
async def test_scores_each_target_and_force_includes(
    monkeypatch: pytest.MonkeyPatch, invalidations: list[int]
) -> None:
    supabase = _supabase()
    scored: list[str] = []
    monkeypatch.setattr(
        job_ingest,
        "score_and_upsert",
        AsyncMock(side_effect=lambda _c, **kw: scored.append(kw["target"].id)),
    )
    posting_id = await _materialize(supabase, targets=[_target("tgt-1"), _target("tgt-2")])

    assert posting_id == "job-1"
    assert sorted(scored) == ["tgt-1", "tgt-2"]  # every target scored
    # Force-include RPC carried the posting + BOTH target ids.
    rpc = next(c for c in supabase.rpc.call_args_list if c.args[0] == "user_set_scores_included")
    assert rpc.args[1]["p_job_posting_id"] == "job-1"
    assert set(rpc.args[1]["p_target_ids"]) == {"tgt-1", "tgt-2"}
    assert invalidations == [1]


@pytest.mark.asyncio
async def test_per_target_score_failure_is_isolated(
    monkeypatch: pytest.MonkeyPatch, invalidations: list[int]
) -> None:
    """One target's scoring blowing up must not lose the others, fail the
    materialize, or skip the force-include step."""
    supabase = _supabase()
    scored: list[str] = []

    def flaky(_c: object, **kw: object) -> None:
        target = kw["target"]
        if target.id == "tgt-bad":  # type: ignore[attr-defined]
            raise RuntimeError("scoring exploded")
        scored.append(target.id)  # type: ignore[attr-defined]

    monkeypatch.setattr(job_ingest, "score_and_upsert", AsyncMock(side_effect=flaky))

    posting_id = await _materialize(supabase, targets=[_target("tgt-bad"), _target("tgt-ok")])

    assert posting_id == "job-1"  # materialize did NOT fail
    assert scored == ["tgt-ok"]  # the healthy target still scored
    assert "user_set_scores_included" in _rpc_calls(supabase)  # force-include still ran


@pytest.mark.asyncio
async def test_set_included_false_skips_force_include(
    monkeypatch: pytest.MonkeyPatch, invalidations: list[int]
) -> None:
    supabase = _supabase()
    monkeypatch.setattr(job_ingest, "score_and_upsert", AsyncMock(return_value=None))

    await _materialize(supabase, set_included=False)

    assert "user_set_scores_included" not in _rpc_calls(supabase)


@pytest.mark.asyncio
async def test_force_include_failure_is_swallowed(
    monkeypatch: pytest.MonkeyPatch, invalidations: list[int]
) -> None:
    """A force-include RPC failure is best-effort — the job is already saved, so
    visibility is nice-to-have and must never fail the materialize."""
    supabase = _supabase()
    supabase.rpc.return_value.execute.side_effect = RuntimeError("rpc down")
    monkeypatch.setattr(job_ingest, "score_and_upsert", AsyncMock(return_value=None))

    posting_id = await _materialize(supabase)

    assert posting_id == "job-1"  # did not propagate
    assert invalidations == [1]  # still invalidated after the swallowed failure


@pytest.mark.asyncio
async def test_upsert_no_row_returns_none(
    monkeypatch: pytest.MonkeyPatch, invalidations: list[int]
) -> None:
    supabase = _supabase(job_id=None)  # upsert returned no row
    score = MagicMock()
    monkeypatch.setattr(job_ingest, "score_and_upsert", score)

    posting_id = await _materialize(supabase)

    assert posting_id is None
    score.assert_not_called()  # nothing to score
    assert invalidations == [1]
