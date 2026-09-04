"""#604: the set-based recency sweep — SQL↔Python parity, scope, and no-op guard.

``refresh_all_recency_scores`` moved its arithmetic into the
``sweep_recency_scores`` SQL function, so the decay curve now has two
implementations: :func:`app.services.recency.compute_recency_score` (the
poll-path and display source of truth) and the migration's SQL expression.
These run the REAL sweep loop against real Postgres and compare the stored
results to the Python function on the same inputs — a drift in either
implementation fails here.

Rounding note: Python ``round`` is half-to-even, ``round(numeric)`` is
half-away-from-zero. The two can only disagree when ``score × multiplier``
is EXACTLY x.5 as a double, which real float multipliers (products of the
inexact 0.015/0.3 doubles and a never-exactly-integral age) do not produce
— every fixture here lands comfortably off the .5 boundary, as prod values
do.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from supabase import AsyncClient, Client

import app.services.recency as recency_mod
from app.config import settings
from app.services.recency import compute_recency_score, refresh_all_recency_scores

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _job(
    jid: str,
    source_id: str,
    title: str,
    *,
    posted: datetime | None,
    archived: datetime | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": jid,
        "external_id": f"ext-{jid[:8]}",
        "source_id": source_id,
        "title": title,
        "company_name": "Acme",
        "role_family": "engineering",
        "is_us": True,
        "cataloged_at": _iso(datetime.now(UTC) - timedelta(minutes=5)),
    }
    if posted is not None:
        row["source_posted_at"] = _iso(posted)
    if archived is not None:
        row["archived_at"] = _iso(archived)
    return row


@pytest.fixture()
def _corpus(service_client: Client) -> Any:
    """Seed one target + jobs at controlled ages + one scores row per job.

    ``scores_sync_denorm`` initialises ``recency_score := score`` on insert,
    so pre-sweep every stored value mirrors the raw score — rows old enough
    to decay are exactly the rows the first sweep must rewrite.
    """
    now = datetime.now(UTC)
    source_id = str(uuid.uuid4())
    target_id = str(uuid.uuid4())
    # (key, score, age_days | None for "no provider date", archived)
    spec: list[tuple[str, int, float | None, bool]] = [
        ("fresh", 80, 0.5, False),  # inside grace → no decay
        ("grace-edge", 90, 6.5, False),  # still inside grace
        ("mid-decay", 90, 27.0, False),  # mult 0.7 → 63
        ("mid-decay-odd", 37, 27.0, False),  # 25.9 → 26 (round-up leg)
        ("floored", 90, 80.0, False),  # past ~54d → floor 0.3 → 27
        ("zero-score", 0, 27.0, False),  # stays 0
        ("undated", 75, None, False),  # falls back to cataloged_at → fresh
        ("archived", 90, 60.0, True),  # job archived → row untouched
    ]
    jobs = {
        key: _job(
            str(uuid.uuid4()),
            source_id,
            f"sweep {key} {uuid.uuid4().hex[:6]}",
            posted=(now - timedelta(days=age)) if age is not None else None,
            archived=now if archived else None,
        )
        for key, _score, age, archived in spec
    }
    try:
        service_client.table("sources").insert(
            {
                "id": source_id,
                "board_token": f"sweep-{uuid.uuid4().hex[:10]}",
                "company_name": "Acme",
                "provider": "greenhouse",
            }
        ).execute()
        service_client.table("targets").insert(
            {"id": target_id, "label": "Sweep Target", "role_family": "engineering"}
        ).execute()
        service_client.table("jobs").insert(list(jobs.values())).execute()
        score_rows = [
            {
                "job_posting_id": jobs[key]["id"],
                "target_id": target_id,
                "score": score,
                "excluded": False,
                "scoring_status": "stage2",
            }
            for key, score, _age, _archived in spec
        ]
        # An excluded row for an aged job: the sweep must not touch it even
        # though its stored value is stale.
        excluded_target = str(uuid.uuid4())
        service_client.table("targets").insert(
            {"id": excluded_target, "label": "Sweep Excl", "role_family": "engineering"}
        ).execute()
        score_rows.append(
            {
                "job_posting_id": jobs["mid-decay"]["id"],
                "target_id": excluded_target,
                "score": 90,
                "excluded": True,
                "scoring_status": "stage2",
            }
        )
        service_client.table("scores").insert(score_rows).execute()
        yield {
            "now": now,
            "spec": spec,
            "jobs": jobs,
            "target_id": target_id,
            "excluded_target": excluded_target,
        }
    finally:
        job_ids = [j["id"] for j in jobs.values()]
        service_client.table("scores").delete().in_("job_posting_id", job_ids).execute()
        service_client.table("jobs").delete().in_("id", job_ids).execute()
        service_client.table("targets").delete().in_("id", [target_id, excluded_target]).execute()
        service_client.table("sources").delete().eq("id", source_id).execute()


def _stored(service_client: Client, job_id: str, target_id: str) -> int:
    return int(
        service_client.table("scores")
        .select("recency_score")
        .eq("job_posting_id", job_id)
        .eq("target_id", target_id)
        .single()
        .execute()
        .data["recency_score"]
    )


async def test_sweep_matches_python_and_respects_scope(
    _corpus: dict[str, Any],
    service_client: Client,
    async_service_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "recency_decay_enabled", True)
    # Batch of 3 forces the keyset loop through several REAL cursor hops over
    # the whole scores table — the corpus rows land in different batches
    # whatever their uuids are.
    monkeypatch.setattr(recency_mod, "_SWEEP_BATCH_SIZE", 3)

    written = await refresh_all_recency_scores(async_service_client)
    assert written > 0

    corpus, jobs = _corpus, _corpus["jobs"]
    for key, score, age, archived in corpus["spec"]:
        stored = _stored(service_client, jobs[key]["id"], corpus["target_id"])
        if archived:
            # Archived job → the sweep leaves the row frozen at its
            # insert-time value (recency == raw score, per the denorm
            # trigger), even though a live row this old would decay.
            assert stored == score, key
            continue
        expected = compute_recency_score(score, age or 0.0, enabled=True)
        assert stored == expected, (
            f"{key}: stored {stored} != python {expected} (score={score}, age={age})"
        )

    # The excluded row kept its insert-time value.
    assert _stored(service_client, jobs["mid-decay"]["id"], corpus["excluded_target"]) == 90

    # No-op guard: run 1 settled every live row (corpus AND leftovers), so an
    # immediate second full sweep must write ZERO rows — the ``IS DISTINCT
    # FROM`` arm is what stops the nightly rewrite-everything churn (#604).
    # ``written`` is the observable: values alone can't tell "skipped" from
    # "rewrote the same number".
    assert await refresh_all_recency_scores(async_service_client) == 0


async def test_sweep_flag_off_is_the_identity(
    _corpus: dict[str, Any],
    service_client: Client,
    async_service_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decay disabled → every live row mirrors its raw score, however old.

    Non-vacuous by construction: fresh inserts already satisfy
    ``recency == score`` (the denorm trigger), so first STALE one row to a
    decayed value — the identity sweep must actively restore it, and a sweep
    that silently did nothing fails here.
    """
    monkeypatch.setattr(settings, "recency_decay_enabled", False)
    corpus, jobs = _corpus, _corpus["jobs"]
    service_client.table("scores").update({"recency_score": 63}).eq(
        "job_posting_id", jobs["mid-decay"]["id"]
    ).eq("target_id", corpus["target_id"]).execute()
    assert _stored(service_client, jobs["mid-decay"]["id"], corpus["target_id"]) == 63

    written = await refresh_all_recency_scores(async_service_client)

    assert written >= 1  # at least the staled row was repaired
    for key, score, _age, archived in corpus["spec"]:
        if archived:
            continue
        assert _stored(service_client, jobs[key]["id"], corpus["target_id"]) == score, key
