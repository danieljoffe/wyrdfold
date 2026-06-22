"""Anonymous voting on reference-JD contributions (#5 P3).

Two trust tiers, mirroring the #6 write-segregation split:
- The caller's own vote is written through their JWT/RLS client, so Postgres
  RLS is the backstop that a user can only ever touch their OWN vote row.
- The suppression tally reads ALL votes for a contribution and flips
  ``reference_jds.suppressed`` — that needs the service-role client (RLS hides
  other users' votes), and ``reference_jds`` stays a service-layer write per
  the #6 R2 decision.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from supabase import Client

VOTES_TABLE = "contribution_votes"
REF_JDS_TABLE = "reference_jds"


def set_user_vote(
    user_client: Client, *, reference_jd_id: str, user_id: str, value: int
) -> None:
    """Record (or clear) the caller's vote via their RLS client.

    ``value`` 0 deletes the caller's vote; -1/+1 upserts it. RLS's WITH CHECK
    (``auth.uid() = user_id``) guarantees a caller can only write their own row.
    """
    if value == 0:
        user_client.table(VOTES_TABLE).delete().eq(
            "reference_jd_id", reference_jd_id
        ).eq("user_id", user_id).execute()
        return
    user_client.table(VOTES_TABLE).upsert(
        {
            "reference_jd_id": reference_jd_id,
            "user_id": user_id,
            "value": value,
            "updated_at": datetime.now(UTC).isoformat(),
        },
        on_conflict="reference_jd_id,user_id",
    ).execute()


def get_user_vote(
    user_client: Client, *, reference_jd_id: str, user_id: str
) -> int:
    """The caller's own vote for a contribution (0 if they haven't voted)."""
    resp = (
        user_client.table(VOTES_TABLE)
        .select("value")
        .eq("reference_jd_id", reference_jd_id)
        .eq("user_id", user_id)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return int(rows[0]["value"]) if rows else 0


def recompute_suppression(
    service_client: Client, *, reference_jd_id: str, quorum: int
) -> tuple[bool, bool]:
    """Tally all votes (service-role) and reconcile ``suppressed``.

    A contribution is suppressed once its NET down-votes (down minus up) reach
    ``quorum`` — up-votes can rescue it back. Returns ``(suppressed_now,
    changed)``; ``changed`` tells the caller whether to re-merge the profile.
    """
    resp = (
        service_client.table(VOTES_TABLE)
        .select("value")
        .eq("reference_jd_id", reference_jd_id)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    net_down = -sum(int(r["value"]) for r in rows)  # down(-1) minus up(+1)
    suppressed = net_down >= quorum

    cur_resp = (
        service_client.table(REF_JDS_TABLE)
        .select("suppressed")
        .eq("id", reference_jd_id)
        .single()
        .execute()
    )
    cur = cast(dict[str, Any] | None, cur_resp.data)
    current = bool(cur["suppressed"]) if cur else False

    if suppressed != current:
        service_client.table(REF_JDS_TABLE).update({"suppressed": suppressed}).eq(
            "id", reference_jd_id
        ).execute()
        return suppressed, True
    return suppressed, False
