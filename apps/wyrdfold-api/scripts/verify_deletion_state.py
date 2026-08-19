"""Read-only snapshot of one user's deletion-relevant state (throwaway).

Run before and after `DELETE /profile/account` to prove what erasure did:

    railway run uv run python scripts/verify_deletion_state.py <user_id> <target_id>...

Reports, per target: whether the row still exists, its `app_active` flag, and
how many `user_targets` memberships remain. Plus the per-user tables that
erasure is supposed to clear. Nothing is written.
"""

from __future__ import annotations

import os
import sys

from supabase import create_client

USER_TABLES = (
    "user_profiles",
    "user_targets",
    "job_feedback",
    "target_learning_log",
    "user_target_job_removals",
    "llm_costs",
)


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: verify_deletion_state.py <user_id> [target_id ...]")
    user_id, target_ids = sys.argv[1], sys.argv[2:]

    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])

    print(f"user_id = {user_id}\n")
    print("-- per-user rows " + "-" * 44)
    for table in USER_TABLES:
        try:
            resp = sb.table(table).select("*", count="exact").eq("user_id", user_id).execute()
            print(f"  {table:<28} {resp.count}")
        except Exception as exc:  # table may not have user_id / may not exist
            print(f"  {table:<28} n/a ({str(exc)[:60]})")

    print("\n-- targets " + "-" * 50)
    for tid in target_ids:
        rows = sb.table("targets").select("id,label,app_active").eq("id", tid).execute().data or []
        if not rows:
            print(f"  {tid}  DELETED")
            continue
        row = rows[0]
        members = (
            sb.table("user_targets")
            .select("user_id", count="exact")
            .eq("target_id", tid)
            .execute()
            .count
        )
        print(
            f"  {tid}  EXISTS  app_active={row.get('app_active')!s:<5} "
            f"members={members}  label={row.get('label')!r}"
        )

    print("\n-- auth user " + "-" * 48)
    try:
        user = sb.auth.admin.get_user_by_id(user_id)
        print(f"  auth.users: PRESENT ({getattr(user.user, 'email', '?')})")
    except Exception as exc:
        print(f"  auth.users: ABSENT / error — {str(exc)[:80]}")


if __name__ == "__main__":
    main()
