"""reference_jds SET NULL FK — the anonymize-on-erasure semantics (#6).

`reference_jds.user_id` is nullable uuid with `ON DELETE SET NULL` (migration
20260702150000): shared-catalog content with optional attribution. Deleting the
auth user must NOT delete the shared JD — the row survives, the personal link
drops to NULL. This is the DB-level automation of the #29 anonymize step, and
the exact opposite of the per-user tables' CASCADE, so pin it explicitly.
"""

from __future__ import annotations

import pytest
from supabase import Client

from tests.integration.conftest import create_auth_user, delete_auth_user

pytestmark = pytest.mark.integration


def test_user_deletion_anonymizes_but_keeps_reference_jd(
    service_client: Client,
) -> None:
    uid = create_auth_user(service_client)
    target = service_client.table("targets").insert({"label": "RefJD FK"}).execute().data[0]
    ref = (
        service_client.table("reference_jds")
        .insert({"target_id": target["id"], "user_id": uid, "jd_text": "shared JD text", "extracted_profile": {}, "suppressed": False})
        .execute()
        .data[0]
    )
    try:
        delete_auth_user(service_client, uid)

        rows = (
            service_client.table("reference_jds")
            .select("id, user_id")
            .eq("id", ref["id"])
            .execute()
            .data
        )
        assert len(rows) == 1, "shared JD must SURVIVE the user's deletion"
        assert rows[0]["user_id"] is None, "attribution must drop to NULL (anonymized)"
    finally:
        service_client.table("reference_jds").delete().eq("id", ref["id"]).execute()
        service_client.table("targets").delete().eq("id", target["id"]).execute()


def test_orphan_attribution_is_rejected(service_client: Client) -> None:
    """A user_id not present in auth.users fails the FK (23503) — attribution,
    when present, must be real."""
    from postgrest.exceptions import APIError

    target = service_client.table("targets").insert({"label": "RefJD orphan"}).execute().data[0]
    try:
        with pytest.raises(APIError):
            service_client.table("reference_jds").insert(
                {
                    "target_id": target["id"],
                    "user_id": "99999999-9999-4999-8999-999999999999",
                    "jd_text": "orphan",
                    "extracted_profile": {},
                    "suppressed": False,
                }
            ).execute()
    finally:
        service_client.table("targets").delete().eq("id", target["id"]).execute()
