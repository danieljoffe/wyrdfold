"""Copy a bounded slice of the REAL job catalog from production into staging.

WHY
Staging ships with `supabase/seed.sql` — 16 fictional jobs, enough to prove the
surfaces render. It cannot reproduce what real data does to them: HTML that
needs sanitising, salary strings in a dozen shapes, titles that defeat the
matcher, companies whose names collide. This copies the real thing.

WHAT IT COPIES, AND WHY ONLY THAT
`jobs` and `sources`. Nothing else. **21 public tables carry a `user_id`** and
none of them are here, deliberately:

  * a second copy of user data has its own deletion lifecycle. Production
    honours an account deletion; staging would not, unless this script also
    deleted — so every deletion would need to reach two databases, and the one
    nobody watches is the one that keeps the data.
  * `targets.description` names real employers, and targets are SHARED between
    users, so even "just targets" leaks.

Verified before writing this, not assumed: `jobs` and `sources` have zero
columns matching user/owner/email and zero foreign keys into `auth` or any
`user*` table.

Scores are the real loss — they need targets, which need users. Create a
couple of SYNTHETIC targets in staging instead: real jobs, fabricated owner.

DIRECTION IS THE DANGEROUS PART
This reads one database and writes another, so reversing it would push staging
rows into production. Three independent refusals, all fail-closed:

  1. the WRITE target's project ref must not be production's;
  2. the READ source and WRITE target must differ;
  3. `--confirm-write-to` must name the writing project's ref, so the caller
     states the destination rather than inheriting it from whatever the
     environment happened to hold.

Anything unrecognised refuses. A gate that returns success for a condition it
did not anticipate converts "we didn't check" into "we checked and it's fine"
(#1028).

SOURCES ARRIVE DISABLED, ALWAYS
5,225 of production's 5,255 sources are enabled. Copied as-is, staging would
poll thousands of real ATS boards — duplicating third-party load, and worse: a
poll that finds none of a source's jobs ARCHIVES them, so staging would delete
the catalog this script just imported. `enabled` is forced false on write and
there is no flag to turn that off.

USAGE
    cd apps/wyrdfold-api
    export PROD_DB_URL=...            # read-only; the ledger/source of truth
    export STAGING_SUPABASE_URL=https://<ref>.supabase.co
    export STAGING_SERVICE_KEY=sb_secret_...

    uv run python scripts/sync_catalog_to_staging.py \
        --confirm-write-to <staging-ref> --limit 5000 --dry-run
    # then drop --dry-run

THIS IS AN ADDITIVE IMPORT, NOT A MIRROR
The read excludes rows production has archived or purged, and merge-upsert
never removes their existing staging copies, so repeated runs accumulate and
staging drifts toward showing jobs production no longer serves. The word
"sync" invites the opposite assumption, hence this paragraph.

RESETTING STAGING
There is deliberately no --replace flag. An earlier revision had one, and it
was wrong: "clear jobs+sources" reads like two tables, but both carry inbound
ON DELETE CASCADE, so the real blast radius enumerated against the live schema
is eleven relationships —

    jobs     <- analyses, job_feedback, notifications_sent, scores,
                status_log, user_jobs, job_embeddings,
                user_target_job_removals          (all CASCADE)
             <- documents.job_posting_id          (SET NULL)
    sources  <- jobs (and therefore all of the above again),
                source_registrations              (both CASCADE)

`user_jobs` is the user's saved and applied jobs — application-tracking state,
not catalog. A flag advertised as a catalog refresh would silently destroy the
seeded personas and any manual test state on staging, which is the opposite of
what a disposable environment is FOR.

If you genuinely need a clean catalog, do it deliberately and visibly:

    supabase db reset --linked          # via `pnpm db:push`-style targeting
    # then re-run this importer

or write the TRUNCATE by hand, having read the list above. Neither is wrapped
in a flag here on purpose: a destructive reset should cost more keystrokes
than an additive import, and should not be one typo away from it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Any, cast
from urllib.parse import urlparse

# Production's project ref. Committed already (supabase/config.toml), not a
# secret, and hard-coded here on purpose: the point is that this script knows
# which database it must never write to, without being told at runtime.
PRODUCTION_REF = "swxiuutaikxbirauivjg"

# Batch size for PostgREST writes. Kept well under the URL/body limits that
# bite on large `in_()` filters (#57: 414s above ~150-200 for filters); writes
# are body-carried so they tolerate more, but there is no prize for maximising
# it and a smaller batch fails more legibly.
BATCH = 500

SAFE_TABLES = ("sources", "jobs")

# Upper bound on a single run. Production holds ~88k live jobs; the point of
# this script is a WORKING SLICE, not a clone, and staging runs on the smallest
# compute tier. A cap makes "I typed an extra zero" a refusal instead of a long
# transfer nobody meant to start.
MAX_LIMIT = 25_000


class SyncError(Exception):
    """A refusal, written for the operator."""


def project_identity(raw: str) -> str:
    """A comparable identity for a Supabase target, from a URL of either shape.

    Deliberately the same logic as ``preflight_migrations._project_identity``:
    two independent env vars can describe different databases, so both ends get
    reduced to a ref and compared. Unrecognised shapes return ``unknown:<host>``
    so they compare equal to nothing — an unknown target must never read as a
    match.
    """
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "::1", "host.docker.internal"}:
        return "local"
    for pattern in (r"^([a-z0-9]{16,})\.supabase\.co$", r"^db\.([a-z0-9]{16,})\.supabase\.co$"):
        m = re.match(pattern, host)
        if m:
            return m.group(1)
    m = re.match(r"^postgres\.([a-z0-9]{16,})$", parsed.username or "")
    if m:
        return m.group(1)
    return f"unknown:{host}"


def assert_safe_direction(read_url: str, write_url: str, confirmed: str) -> tuple[str, str]:
    """Refuse unless this is provably production -> not-production.

    Both halves are checked. Proving only that the destination is not
    production would leave the *source* unconstrained, so the script would
    happily copy an empty local database over staging and report success —
    the contract in the name ("the real catalog") would be silently unmet.

    Order matters, because the first refusal is the message the operator reads.
    Writing to production is the worst outcome, so it is checked first; a
    wrong-source complaint would bury it. "Same project both ends" comes next
    because it names the confusion precisely. Only then the source check, which
    is the catch-all for everything else pointed at PROD_DB_URL.
    """
    src = project_identity(read_url)
    dst = project_identity(write_url)

    if dst == PRODUCTION_REF:
        raise SyncError(
            f"REFUSING: the write target resolves to PRODUCTION ({dst}). "
            "This script only ever writes to a non-production database."
        )
    if dst.startswith("unknown:"):
        raise SyncError(
            f"REFUSING: could not identify the write target ({dst}). An "
            "unrecognised destination is not a safe one."
        )
    if src == dst:
        raise SyncError(
            f"REFUSING: read and write resolve to the SAME project ({src}). "
            "Nothing to copy, and it suggests one of the two is misconfigured."
        )

    if src != PRODUCTION_REF:
        raise SyncError(
            f"REFUSING: the read source resolves to {src!r}, not production "
            f"({PRODUCTION_REF}). This script imports the REAL catalog; point "
            "PROD_DB_URL at production, or use a different tool."
        )
    if confirmed != dst:
        raise SyncError(
            f"REFUSING: --confirm-write-to says {confirmed!r} but the write target "
            f"resolves to {dst!r}. Name the destination you actually mean."
        )
    return src, dst


_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def checked_limit(value: int) -> int:
    """Refuse a limit that is not a deliberate, bounded row count.

    Postgres rejects a negative LIMIT outright ("LIMIT must not be negative"),
    so a negative value fails anyway — but it fails deep inside a psql call
    with a message about SQL, which tells the operator nothing about the flag
    they mistyped. Zero is accepted by Postgres and silently copies nothing,
    which looks like success. Both refuse here, at the boundary, by name.
    """
    if value < 1:
        raise SyncError(f"--limit must be at least 1, got {value}")
    if value > MAX_LIMIT:
        raise SyncError(
            f"--limit {value} exceeds the {MAX_LIMIT} cap. This copies a working slice, "
            "not the whole catalog; raise MAX_LIMIT deliberately if you really mean it."
        )
    return value


# The state every copied source is forced into, as data so the regression test
# can assert the whole outgoing payload rather than the one field someone
# remembered.
#
# Keeping this honest is the hard part, and prose has now failed twice: the
# first revision missed `disabled_at`, the second still carried `last_error`
# while its own comment claimed to be complete. So every column of `sources`
# must appear either here or in _COPIED_AS_IS below, and an integration test
# (tests/integration/test_sync_source_columns.py) reads the LIVE table and
# fails on any column classified in neither. A new column cannot be added to
# the table without someone deciding, in this file, what the sync does with it.
#
# `disabled_at` is the subtle one and is why this is a dict. Setting
# `enabled=False` is not sufficient to keep a source inert, because
# `recover_stale_sources()` re-enables exactly the rows where
#
#     enabled = false AND disabled_at IS NOT NULL AND disabled_at < now() - 24h
#
# and the column comment in 20260623150000_ingestion_resilience.sql defines
# NULL as "never auto-disabled (or an operator disabled it manually)". So a
# production source that the failure backoff disabled more than
# SOURCE_RECOVERY_AFTER_HOURS ago arrives here carrying its own re-enable
# order: staging's next poll cycle turns it back on, polls a real ATS board,
# and — because a poll that finds none of a source's jobs ARCHIVES them —
# deletes the catalog this script just imported. Copying `disabled_at`
# unchanged is the one field that can undo every other field here.
#
# Measured against production the day this was written: 19 of 5,255 sources
# carried a `disabled_at`, none yet older than the 24h cooldown. That is a
# latent bug, not a dormant one — those 19 age into eligibility, so a sync that
# is safe today is not safe next week. Hence forced, not defaulted.
_INERT_SOURCE_STATE: dict[str, Any] = {
    "enabled": False,
    # Auto-recovery marker. NULL means "manual", which is never overridden.
    "disabled_at": None,
    # Polling lifecycle. Reset so staging does not inherit production's
    # history and immediately look unhealthy, or misreport its own cadence.
    "last_polled_at": None,
    "last_candidate_at": None,
    "consecutive_failures": 0,
    # Failure diagnostics. Harmless to polling — nothing reads them to decide
    # whether to poll — but staging would report PRODUCTION's last failure as
    # its own, which is the kind of inherited noise that sends someone
    # debugging an outage that happened in another database.
    "last_error": None,
    "last_error_at": None,
    # Denormalised counter the poller maintains. Production's value describes
    # production's catalog, not the subset copied here, so it would be wrong
    # either way; 0 is at least CONSISTENT with last_polled_at being NULL —
    # "this database has never polled this source". It is surfaced by
    # routers/sources.py and diagnostics, so a stale value is visible, not inert.
    "job_count": 0,
}


# Columns copied through unchanged, declared rather than assumed. Identity and
# descriptive fields: they are the POINT of the import (a source with a blanked
# board_token is not a real source), and none of them influences whether or how
# the poller runs.
#
# Listed explicitly so the integration test can tell "deliberately copied" from
# "nobody has looked at this yet". A new column defaults to neither, which is
# what makes the test fail loudly instead of passing vacuously.
_COPIED_AS_IS: frozenset[str] = frozenset(
    {
        "id",
        "board_token",
        "company_name",
        "provider",
        "domain",
        "created_at",
        "poll_interval_minutes",
    }
)


def disabled_source(row: dict[str, Any]) -> dict[str, Any]:
    """A source row rendered safe to insert into a non-production database.

    Forced, never defaulted. An enabled copy lets staging poll a real ATS board,
    and a poll that finds none of that source's jobs ARCHIVES them — staging
    would delete the catalog this script just imported.
    """
    return {**row, **_INERT_SOURCE_STATE}


def uuid_in_list(values: list[str]) -> str:
    """A quoted SQL ``IN`` list, refusing anything that is not a UUID.

    The ids come from a prior query against the same database, so they are not
    attacker-controlled — but string-built SQL is the pattern that bites, and a
    malformed id here would be interpolated verbatim. Validating the shape
    costs nothing and removes the class rather than annotating it away.
    """
    bad = [v for v in values if not _UUID.match(str(v))]
    if bad:
        raise SyncError(
            f"refusing to interpolate {len(bad)} non-UUID source id(s), e.g. {bad[0]!r}"
        )
    return ",".join(f"'{v}'" for v in values)


def require_web_url(raw: str, what: str) -> str:
    """Refuse a write target that is not plain http(s).

    ``urlopen`` honours ``file:`` and other schemes; a mistyped or injected
    value could otherwise reach the local filesystem instead of a database.
    """
    scheme = urlparse(raw).scheme.lower()
    if scheme not in {"http", "https"}:
        raise SyncError(f"{what} must be an http(s) URL, got scheme {scheme!r}")
    return raw


def psql_json(url: str, sql: str) -> list[dict[str, Any]]:
    """Run a read-only query against *url*, returning decoded rows."""
    import shutil

    binary = shutil.which("psql") or "/opt/homebrew/opt/libpq/bin/psql"
    wrapped = f"SELECT coalesce(json_agg(t), '[]'::json) FROM ({sql}) t;"  # noqa: S608 — callers pass constant/validated SQL; see uuid_in_list
    proc = subprocess.run(  # noqa: S603 — constant SQL, resolved binary
        [binary, url, "-X", "-q", "-A", "-t"],
        input=wrapped,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if proc.returncode != 0:
        raise SyncError(f"read query failed: {proc.stderr.strip()[:300]}")
    return cast("list[dict[str, Any]]", json.loads(proc.stdout.strip() or "[]"))


def post_rows(base_url: str, key: str, table: str, rows: list[dict[str, Any]]) -> int:
    """Upsert *rows* into *table* via PostgREST, in batches."""
    import urllib.error
    import urllib.request

    written = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i : i + BATCH]
        req = urllib.request.Request(  # noqa: S310 — scheme enforced by require_web_url
            f"{base_url.rstrip('/')}/rest/v1/{table}",
            method="POST",
            data=json.dumps(chunk).encode(),
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                # merge-duplicates makes a re-run a no-op rather than a
                # duplicate-key failure: this is meant to be run repeatedly.
                "Prefer": "resolution=merge-duplicates,return=minimal",
            },
        )
        try:
            urllib.request.urlopen(req, timeout=180)  # noqa: S310 — scheme enforced by require_web_url
        except urllib.error.HTTPError as exc:
            body = exc.read().decode()[:300]
            raise SyncError(f"write to {table} failed at row {i}: HTTP {exc.code} {body}") from exc
        written += len(chunk)
        print(f"    {table}: {written}/{len(rows)}", flush=True)
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Copy real jobs+sources into a non-production database."
    )
    ap.add_argument("--confirm-write-to", required=True, help="project ref of the write target")
    ap.add_argument(
        "--limit",
        type=int,
        default=5000,
        help=f"most-recent jobs to copy (1..{MAX_LIMIT}, default 5000)",
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="report what would be copied, write nothing"
    )
    args = ap.parse_args(argv)

    read_url = os.environ.get("PROD_DB_URL", "")
    write_url = os.environ.get("STAGING_SUPABASE_URL", "")
    write_key = os.environ.get("STAGING_SERVICE_KEY", "")
    missing = [
        n
        for n, v in (
            ("PROD_DB_URL", read_url),
            ("STAGING_SUPABASE_URL", write_url),
            ("STAGING_SERVICE_KEY", write_key),
        )
        if not v
    ]
    if missing:
        print(f"sync: missing env: {', '.join(missing)}", file=sys.stderr)
        return 2

    try:
        limit = checked_limit(args.limit)
        src, dst = assert_safe_direction(read_url, write_url, args.confirm_write_to)
    except SyncError as exc:
        print(f"sync: {exc}", file=sys.stderr)
        return 2
    try:
        require_web_url(write_url, "STAGING_SUPABASE_URL")
    except SyncError as exc:
        print(f"sync: {exc}", file=sys.stderr)
        return 2
    print(
        f"sync: reading {src} -> writing {dst}  (limit {limit}, mode=additive)"
    )

    jobs = psql_json(
        read_url,
        "SELECT * FROM public.jobs WHERE archived_at IS NULL AND purged_at IS NULL "  # noqa: S608 — only interpolation is an int() cast
        f"ORDER BY cataloged_at DESC NULLS LAST LIMIT {limit}",
    )
    if not jobs:
        print("sync: no jobs matched; nothing to do.")
        return 0
    source_ids = sorted({j["source_id"] for j in jobs if j.get("source_id")})
    sources = psql_json(
        read_url,
        f"SELECT * FROM public.sources WHERE id IN ({uuid_in_list(source_ids)})",  # noqa: S608 — ids UUID-validated by uuid_in_list
    )

    sources = [disabled_source(r) for r in sources]

    print(f"sync: {len(jobs)} jobs across {len(sources)} sources (all forced enabled=false)")
    if args.dry_run:
        print("sync: --dry-run, wrote nothing.")
        for j in jobs[:3]:
            print(f"    would copy: {j.get('title')} | {j.get('company_name')}")
        return 0

    # This is an ADDITIVE import, not a mirror — see RESETTING STAGING in the
    # module docstring for why there is deliberately no --replace flag here.
    post_rows(write_url, write_key, "sources", sources)
    post_rows(write_url, write_key, "jobs", jobs)
    print(f"sync: done — {len(sources)} sources, {len(jobs)} jobs into {dst}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
