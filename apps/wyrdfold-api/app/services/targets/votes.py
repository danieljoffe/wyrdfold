"""Anonymous voting on reference-JD contributions (#5 P3).

Two trust tiers, mirroring the #6 write-segregation split:
- The caller's own vote is written through their JWT/RLS client, so Postgres
  RLS is the backstop that a user can only ever touch their OWN vote row.
- The suppression tally reads ALL votes for a contribution and flips
  ``reference_jds.suppressed`` atomically, under a row lock, via the
  ``recompute_contribution_suppression`` SECURITY DEFINER RPC — that needs the
  service-role client (RLS hides other users' votes), and ``reference_jds``
  stays a service-layer write per the #6 R2 decision.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from supabase import Client

VOTES_TABLE = "contribution_votes"


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
    """Atomically tally all votes and reconcile ``suppressed`` (service-role).

    A contribution is suppressed once its NET down-votes (down minus up) reach
    ``quorum`` — up-votes can rescue it back. Returns ``(suppressed_now,
    changed)``; ``changed`` tells the caller whether to re-merge the profile.

    The tally→compare→write runs inside the ``recompute_contribution_suppression``
    SECURITY DEFINER function under a ``FOR UPDATE`` row lock on the contribution,
    so concurrent recomputes on the same (shared) reference_jd serialize instead
    of racing — a stale tally can no longer clobber a fresh one and silently
    un/re-suppress a contribution everyone merges (audit #29 lost-update race).
    The function is granted to ``service_role`` only, so it must be called on the
    service-role client (the tally reads every user's vote, hidden from any single
    caller by RLS).
    """
    resp = service_client.rpc(
        "recompute_contribution_suppression",
        {"p_reference_jd_id": reference_jd_id, "p_quorum": quorum},
    ).execute()
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        return False, False
    row = rows[0]
    return bool(row["suppressed"]), bool(row["changed"])
