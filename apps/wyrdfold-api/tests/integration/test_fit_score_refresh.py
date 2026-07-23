"""E2 lazy fit-score refresh against a LIVE local Supabase stack.

The load-bearing guarantee: a background rescore updates only the score, its
reasoning, and the version marker — and NEVER ``is_active`` (which would flip the
active flag / trip the active-target cap as a side effect). That's why the
refresh uses the targeted ``update_fit_score`` UPDATE rather than the
``link_user_to_target`` upsert. Proven here against real Postgres.
"""

from __future__ import annotations

import contextlib
import uuid

import pytest
from supabase import Client

from app.services.targets import crud
from tests.integration.conftest import create_auth_user, delete_auth_user

pytestmark = pytest.mark.integration


def _seed_link(service_client: Client, uid: str, *, is_active: bool, marker: str) -> str:
    target_id = (
        service_client.table("targets")
        .insert({"label": f"E2 {uuid.uuid4().hex[:8]}", "scoring_profile": {}})
        .execute()
        .data[0]["id"]
    )
    service_client.table("user_targets").insert(
        {
            "user_id": uid,
            "target_id": target_id,
            "is_active": is_active,
            "fit_score": 50,
            "fit_score_reasoning": "old",
            "fit_score_prose_doc_id": marker,
        }
    ).execute()
    return target_id


def _cleanup(service_client: Client, target_id: str) -> None:
    with contextlib.suppress(Exception):
        service_client.table("user_targets").delete().eq("target_id", target_id).execute()
    with contextlib.suppress(Exception):
        service_client.table("targets").delete().eq("id", target_id).execute()


def test_update_fit_score_refreshes_without_touching_is_active(service_client: Client) -> None:
    uid = create_auth_user(service_client)
    old_marker, new_marker = str(uuid.uuid4()), str(uuid.uuid4())
    # An ACTIVE link with a stale marker — the dangerous case: a naive upsert
    # would re-assert is_active and could trip the cap.
    target_id = _seed_link(service_client, uid, is_active=True, marker=old_marker)
    try:
        crud.update_fit_score(
            service_client,
            user_id=uid,
            target_id=target_id,
            fit_score=91,
            fit_score_reasoning="fresh",
            fit_score_prose_doc_id=new_marker,
        )
        row = (
            service_client.table("user_targets")
            .select("is_active, fit_score, fit_score_reasoning, fit_score_prose_doc_id")
            .eq("user_id", uid)
            .eq("target_id", target_id)
            .single()
            .execute()
            .data
        )
        assert row["is_active"] is True  # NOT flipped by the rescore
        assert row["fit_score"] == 91  # score refreshed
        assert row["fit_score_reasoning"] == "fresh"
        assert row["fit_score_prose_doc_id"] == new_marker  # re-stamped to current
    finally:
        _cleanup(service_client, target_id)
        delete_auth_user(service_client, uid)


def test_get_marker_reads_current_version(service_client: Client) -> None:
    uid = create_auth_user(service_client)
    marker = str(uuid.uuid4())
    target_id = _seed_link(service_client, uid, is_active=False, marker=marker)
    try:
        got = crud.get_fit_score_prose_doc_id(service_client, user_id=uid, target_id=target_id)
        assert got == marker
        # After a refresh the re-read reflects the new version (the concurrency
        # re-check depends on this).
        new_marker = str(uuid.uuid4())
        crud.update_fit_score(
            service_client,
            user_id=uid,
            target_id=target_id,
            fit_score=77,
            fit_score_reasoning="x",
            fit_score_prose_doc_id=new_marker,
        )
        assert crud.get_fit_score_prose_doc_id(service_client, user_id=uid, target_id=target_id) == (
            new_marker
        )
    finally:
        _cleanup(service_client, target_id)
        delete_auth_user(service_client, uid)
