"""One-off cleanup for #868: remove résumé-derived text from shared targets.

``targets`` is the shared catalog — every co-follower reads the row and it
survives the author's account deletion. #879 stopped NEW rows carrying
résumé-informed descriptions; this repairs the ones already written.

Two actions, decided with the owner:

1. **Re-derive** a followed target whose description contains second-person or
   résumé-echo text. ``derive_profile_from_label`` is grounded in the label
   ALONE, so the replacement is role-generic by construction. One LLM call per
   target, billed to the instance key (an operator action, not a user's).

2. **Delete** an orphan — no ``user_targets`` membership and not ``app_active``.
   That already violates the lifecycle invariant (``NOT app_active ⇒ has a
   membership``); carrying a deleted user's employer names makes it urgent
   rather than merely untidy.

Dry-run by default. Run with ``--apply`` to write:

    railway run uv run python scripts/cleanup_personal_target_descriptions.py
    railway run uv run python scripts/cleanup_personal_target_descriptions.py --apply

Detection is deliberately BROAD (a false positive costs one re-derive, a false
negative leaves PII in a shared row), and every match is printed for eyeballing
before anything is written.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path
from typing import Any, cast

# Running a file from ``scripts/`` puts THAT directory on sys.path, not the
# package root — so ``import app`` fails even under ``uv run --package``.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from supabase import create_client

# Second-person address and the résumé-echo phrasings seen in prod. Broad on
# purpose — see the module docstring.
PERSONAL = re.compile(r"\b(your|you|this user|their track record)\b", re.IGNORECASE)


def _preview(text: str, limit: int = 96) -> str:
    flat = " ".join(text.split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


async def main() -> None:
    apply = "--apply" in sys.argv
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])

    targets = cast(
        "list[dict[str, Any]]",
        sb.table("targets").select("id,label,description,app_active").execute().data or [],
    )
    links = cast(
        "list[dict[str, Any]]",
        sb.table("user_targets").select("target_id").execute().data or [],
    )
    followed = {row["target_id"] for row in links}

    to_rederive: list[dict[str, Any]] = []
    to_delete: list[dict[str, Any]] = []
    for row in targets:
        description = (row.get("description") or "").strip()
        orphan = row["id"] not in followed and not row.get("app_active")
        if orphan:
            to_delete.append(row)
        elif description and PERSONAL.search(description):
            to_rederive.append(row)

    print(f"{'APPLY' if apply else 'DRY RUN'} — {len(targets)} targets scanned\n")

    print(f"RE-DERIVE ({len(to_rederive)}) — followed rows with personal text:")
    for row in to_rederive:
        print(f"  {row['id'][:8]}  {row['label'][:40]!r}")
        print(f"            before: {_preview(row['description'])}")

    print(f"\nDELETE ({len(to_delete)}) — orphans (no membership, not app_active):")
    for row in to_delete:
        print(f"  {row['id'][:8]}  {row['label'][:40]!r}")

    if not apply:
        print("\nNothing written. Re-run with --apply.")
        return

    from app.services.llm import get_default_client
    from app.services.targets.derive_profile_from_label import derive_profile_from_label

    llm = get_default_client()

    print()
    for row in to_rederive:
        derived, _result = await derive_profile_from_label(llm, label=row["label"])
        new_description = (derived.description or "").strip()
        if not new_description:
            print(f"  {row['id'][:8]}  SKIPPED — derive emitted no description")
            continue
        if PERSONAL.search(new_description):
            # Should not happen (label-only input), but never trade one leak
            # for another silently.
            print(f"  {row['id'][:8]}  SKIPPED — replacement still looks personal")
            continue
        sb.table("targets").update({"description": new_description}).eq("id", row["id"]).execute()
        print(f"  {row['id'][:8]}  rewritten: {_preview(new_description)}")

    for row in to_delete:
        sb.table("targets").delete().eq("id", row["id"]).execute()
        print(f"  {row['id'][:8]}  deleted")

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
