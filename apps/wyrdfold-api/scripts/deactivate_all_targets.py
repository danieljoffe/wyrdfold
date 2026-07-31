"""One-shot operator action (#845 cost stop-gap): deactivate ALL user targets.

Sets ``user_targets.is_active = false`` for every active link. Since the P0
re-semantics (2026-07-31) there is no trigger and no cached flag on
``targets`` — pipeline-active is derived as ``app_active OR EXISTS(active
membership)`` — so zeroing memberships removes every USER-sponsored target
from the pipeline.

The ``app_active`` instance floor (app-owned catalog, #543) is deliberately
NOT touched by default: catalog ingestion is instance-paid Phase-1 only and
is the corpus lifeline. Pass ``--include-floor`` to also clear ``app_active``
(a full pipeline stop, e.g. a hard cost emergency).
"""

from __future__ import annotations

import argparse
import os

from supabase import create_client


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--include-floor",
        action="store_true",
        help="Also clear targets.app_active (stops catalog ingestion too).",
    )
    args = parser.parse_args()

    url = os.environ.get("SUPABASE_URL") or os.environ["NEXT_PUBLIC_SUPABASE_URL"]
    sb = create_client(url, os.environ["SUPABASE_SERVICE_ROLE_KEY"])

    ut_active = (
        sb.table("user_targets").select("id", count="exact").eq("is_active", True).execute().count
        or 0
    )
    floor = (
        sb.table("targets").select("id", count="exact").eq("app_active", True).execute().count or 0
    )
    print(f"before: user_targets active={ut_active}, app_active floor={floor}")

    if ut_active:
        sb.table("user_targets").update({"is_active": False}).eq("is_active", True).execute()

    if args.include_floor and floor:
        sb.table("targets").update({"app_active": False}).eq("app_active", True).execute()

    ut_final = (
        sb.table("user_targets").select("id", count="exact").eq("is_active", True).execute().count
        or 0
    )
    floor_final = (
        sb.table("targets").select("id", count="exact").eq("app_active", True).execute().count or 0
    )
    print(f"after:  user_targets active={ut_final}, app_active floor={floor_final}")
    if args.include_floor:
        print("OK" if ut_final == 0 and floor_final == 0 else "!! STILL ACTIVE ROWS — investigate")
    else:
        print(
            "OK (floor untouched — catalog keeps ingesting)"
            if ut_final == 0
            else "!! STILL ACTIVE MEMBERSHIPS — investigate"
        )


if __name__ == "__main__":
    main()
