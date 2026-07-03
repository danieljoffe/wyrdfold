"""RPC-gated writes to the SHARED ``targets.scoring_profile`` (#191).

Every user-attributed mutation of a shared target's profile goes through a
SECURITY DEFINER RPC instead of a raw ``.update()``: the DB re-checks (under
a ``FOR UPDATE`` row lock) that the acting user actually follows the target
and that ``profile_version`` still equals the version the caller computed
against — so a Python authz bug can't silently poison a profile every
follower shares, and concurrent writers can't lose updates.

Two write shapes:
- ``apply_profile_patch_rpc`` — the learner's ProfilePatch applies
  (``apply_target_profile_patch``, 20260703090000) and any other
  profile+version-only write (reference-JD delete re-merge, vote re-merge).
- ``apply_profile_merge_rpc`` — the reference-JD merge write
  (``apply_target_profile_merge``, 20260703110000), which also carries the
  derived ``search_keywords`` / example-title pools (written only when
  non-None).

Operator paths (api-key callers with no user identity) intentionally stay
on the direct service-role write — there is no follower to check.
"""

from __future__ import annotations

from typing import Any, cast

from supabase import Client

Outcome = str  # 'applied' | 'version_conflict' | 'not_a_follower' | 'target_not_found'


def _call(
    supabase: Client, fn: str, params: dict[str, Any]
) -> tuple[Outcome, int | None]:
    resp = supabase.rpc(fn, params).execute()
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        raise RuntimeError(
            f"{fn} returned no row — RPC missing or grants misconfigured"
        )
    return (
        cast(str, rows[0]["outcome"]),
        cast(int | None, rows[0]["new_version"]),
    )


def apply_profile_patch_rpc(
    supabase: Client,
    *,
    user_id: str,
    target_id: str,
    next_profile: dict[str, Any],
    expected_version: int,
) -> tuple[Outcome, int | None]:
    """Write ``scoring_profile`` + version bump through the #191 patch RPC."""
    return _call(
        supabase,
        "apply_target_profile_patch",
        {
            "p_user_id": user_id,
            "p_target_id": target_id,
            "p_next_profile": next_profile,
            "p_expected_version": expected_version,
        },
    )


def apply_profile_merge_rpc(
    supabase: Client,
    *,
    user_id: str,
    target_id: str,
    next_profile: dict[str, Any],
    expected_version: int,
    search_keywords: list[str] | None = None,
    example_promising: list[str] | None = None,
    example_unpromising: list[str] | None = None,
) -> tuple[Outcome, int | None]:
    """Write a reference-JD merge through the #191 merge RPC.

    The extra fields are only written when non-None (SQL ``COALESCE`` keeps
    the current value otherwise) — a staged-then-approved merge updates
    them from the stage-time ``merge_payload``; re-merges that carry no
    derivation (delete, vote flips) pass None.
    """
    return _call(
        supabase,
        "apply_target_profile_merge",
        {
            "p_user_id": user_id,
            "p_target_id": target_id,
            "p_next_profile": next_profile,
            "p_expected_version": expected_version,
            "p_search_keywords": search_keywords,
            "p_example_promising": example_promising,
            "p_example_unpromising": example_unpromising,
        },
    )
