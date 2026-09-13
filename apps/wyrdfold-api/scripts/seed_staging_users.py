"""Create the staging persona accounts: locked-down users with real backgrounds.

WHY THIS EXISTS
Staging with an empty user table can only demonstrate the signed-out surface.
Everything that matters — matching, fit scores, tailoring, billing state — hangs
off an account, and hand-clicking five accounts through onboarding costs LLM
spend and produces something nobody can reproduce next month.

WHAT IT DOES NOT DO: open staging up. The opposite. Every persona is added to
``wyrdfold_beta_invites``, which is the allowlist the ``before_user_created``
auth hook enforces, so staging keeps refusing everyone else with GoTrue's
verbatim "User not found". Seeding accounts and locking the door are the same
action here: the list of people who can sign in IS this file.

WHY NOT SQL
`supabase/seed.sql` deliberately creates no accounts, and says why: auth users
come from GoTrue, and inserting `auth.users` by hand desyncs identities and
refresh tokens. So users are created through the Admin API and only their
APP-side state is written directly.

WHY THE PAYLOAD, NOT THE RESUME TEXT
``derive_fit_score()`` takes an ``OptimizedPayload`` — summary, roles, skills —
not ``uploaded_resumes.extracted_text``. Seeding the upload row alone would
produce five accounts that look different in the UI and score identically,
which is worse than no personas: it invites a conclusion about matching from
data that cannot vary. The distinguishing content therefore lives in
``experience_optimized_docs.payload``.

SAFETY
Writes users. Refuses if the destination resolves to production, reusing
``project_identity`` from the catalog importer rather than reimplementing it —
one copy of the identity logic means it cannot drift into disagreeing with
itself about which database is production.

USAGE
    cd apps/wyrdfold-api
    export STAGING_SUPABASE_URL=https://<ref>.supabase.co
    export STAGING_SERVICE_KEY=sb_secret_...

    uv run python scripts/seed_staging_users.py \
        --confirm-write-to <staging-ref> --dry-run
    # then drop --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Run correctly whether invoked as `python scripts/seed_staging_users.py` or
# `python -m scripts.seed_staging_users`. Without this the obvious form dies
# with ModuleNotFoundError on the `scripts.` imports below -- and it exits 1,
# which callers read as "the seed failed", not "you typed it wrong". Cheaper to
# make both spellings work than to make the operator remember which is which.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any

from scripts.staging_personas import PERSONAS
from scripts.sync_catalog_to_staging import (
    PRODUCTION_REF,
    SyncError,
    project_identity,
    require_web_url,
)


class SeedError(Exception):
    """A refusal or a failed write. The message is for the operator."""


# Reused helpers raise the IMPORTER's exception type, not this module's. Caught
# alongside SeedError everywhere, because the alternative is what testing a
# `file://` URL actually produced: a traceback and exit 1, which a caller reads
# as "the seed broke" rather than "the seed refused". A refusal that does not
# look like a refusal is the failure mode #1028 was about.
_REFUSALS = (SeedError, SyncError)


def assert_not_production(write_url: str, confirmed: str) -> str:
    """Refuse unless the destination is a named, identifiable non-production db.

    Deliberately NOT ``assert_safe_direction`` from the importer: that one also
    requires the READ side to be production, which is meaningless here (there
    is no read). Sharing ``project_identity`` is the part that matters — two
    implementations of "which project is this URL" could disagree, and the one
    that disagrees in the permissive direction is the one that writes personas
    into the live database.
    """
    dst = project_identity(write_url)
    if dst == PRODUCTION_REF:
        raise SeedError(
            f"REFUSING: the write target resolves to PRODUCTION ({dst}). "
            "These are fictional accounts; they do not go in the live database."
        )
    if dst.startswith("unknown:"):
        raise SeedError(
            f"REFUSING: could not identify the write target ({dst}). "
            "An unrecognised destination is not a safe one."
        )
    if confirmed != dst:
        raise SeedError(
            f"REFUSING: --confirm-write-to says {confirmed!r} but the target "
            f"resolves to {dst!r}. Name the database you actually mean."
        )
    return dst


def _request(url: str, key: str, method: str, path: str, body: Any = None) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(  # noqa: S310 — scheme enforced by require_web_url
        f"{url.rstrip('/')}{path}",
        data=data,
        method=method,
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=representation",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 — as above
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


def ensure_auth_user(url: str, key: str, email: str) -> str:
    """Create the GoTrue user, or return the existing one's id.

    Idempotent by necessity — this script is meant to be re-run, and a second
    run must not fail or duplicate. `email_confirm=True` because staging has no
    real mailbox behind an example.com address; without it the account exists
    but can never complete a magic-link round trip.
    """
    status, payload = _request(
        url,
        key,
        "POST",
        "/auth/v1/admin/users",
        {"email": email, "email_confirm": True},
    )
    if status in (200, 201) and isinstance(payload, dict) and payload.get("id"):
        return str(payload["id"])

    # Already exists (GoTrue answers 422 here). Look it up rather than guessing.
    status, payload = _request(url, key, "GET", f"/auth/v1/admin/users?filter={email}")
    users = payload.get("users", []) if isinstance(payload, dict) else []
    for user in users:
        if str(user.get("email", "")).lower() == email.lower():
            return str(user["id"])
    raise SeedError(f"could not create or find an auth user for {email}: {payload!r}")


def profile_row(persona: dict[str, Any], user_id: str, now: datetime) -> dict[str, Any]:
    """The `user_profiles` row for a persona, with every status field explicit.

    Explicit rather than defaulted: a persona whose plan came from the column
    default would silently stop testing that plan the day the default changes.
    """
    trial_age = persona.get("trial_age_days")
    return {
        "user_id": user_id,
        "email": persona["email"],
        "name": persona["name"],
        "location": persona["location"],
        "plan": persona["plan"],
        # Only meaningful for the trial plan, but always written so an account
        # switched to 'trial' later does not inherit a stale clock.
        "trial_started_at": (
            (now - timedelta(days=trial_age)).isoformat() if trial_age is not None else None
        ),
        "llm_enabled": persona.get("llm_enabled", True),
        "job_score_threshold": persona["job_score_threshold"],
        "job_notifications_enabled": not persona.get("unsubscribed", False),
        "unsubscribed_at": now.isoformat() if persona.get("unsubscribed") else None,
        "onboarding_completed_at": now.isoformat() if persona["onboarded"] else None,
        "onboarding_path": persona.get("onboarding_path"),
        "onboarding_current_step": persona.get("onboarding_current_step"),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="seed_staging_users",
        description="Create the staging persona accounts (never production).",
    )
    ap.add_argument(
        "--confirm-write-to",
        required=True,
        help="project ref of the database being written; must match the URL",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be created, write nothing",
    )
    args = ap.parse_args(argv)

    try:
        write_url = require_web_url(
            os.environ.get("STAGING_SUPABASE_URL", ""), "STAGING_SUPABASE_URL"
        )
        key = os.environ.get("STAGING_SERVICE_KEY", "")
        if not key:
            raise SeedError("STAGING_SERVICE_KEY is not set.")
        dst = assert_not_production(write_url, args.confirm_write_to)
    except _REFUSALS as exc:
        print(f"seed: {exc}", file=sys.stderr)
        return 2

    print(f"seed: writing {len(PERSONAS)} personas into {dst}")
    if args.dry_run:
        for p in PERSONAS:
            skills = ", ".join(s["name"] for s in p["payload"]["skills"][:3])
            print(
                f"  would create {p['email']:<32} plan={p['plan']:<8} "
                f"onboarded={p['onboarded']!s:<5} skills=[{skills}]"
            )
        print("seed: --dry-run, wrote nothing.")
        return 0

    now = datetime.now(UTC)
    for persona in PERSONAS:
        email = persona["email"]
        # The allowlist FIRST. If the run dies midway, the state it leaves is
        # "invited but no account" -- recoverable by signing in. The reverse
        # order would leave an account that the auth hook refuses to admit.
        _request(write_url, key, "POST", "/rest/v1/wyrdfold_beta_invites", [{"email": email}])
        user_id = ensure_auth_user(write_url, key, email)

        status, payload = _request(
            write_url,
            key,
            "POST",
            "/rest/v1/user_profiles?on_conflict=user_id",
            [profile_row(persona, user_id, now)],
        )
        if status >= 300:
            raise SeedError(f"profile write failed for {email}: {status} {payload!r}")

        status, payload = _request(
            write_url,
            key,
            "POST",
            # on_conflict is REQUIRED, not decorative: the table carries a
            # unique (user_id, version) constraint, so a plain insert makes
            # the SECOND run 409 and abandon every persona after the first.
            # Merging is also the right semantic — re-running should refresh
            # a persona's background to whatever staging_personas.py now says.
            "/rest/v1/experience_optimized_docs?on_conflict=user_id,version",
            [
                {
                    "user_id": user_id,
                    "version": 1,
                    "payload": persona["payload"],
                    "source": "llm",
                    "markdown_view": persona["payload"]["summary"],
                }
            ],
        )
        if status >= 300:
            raise SeedError(f"experience write failed for {email}: {status} {payload!r}")

        print(f"  {email:<32} plan={persona['plan']:<8} id={user_id[:8]}…")

    print(f"seed: done — {len(PERSONAS)} personas in {dst}.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except _REFUSALS as exc:  # a refusal reached from deeper in the run
        print(f"seed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
