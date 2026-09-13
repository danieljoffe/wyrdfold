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
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Any
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
    """Refuse unless the write target is provably not production."""
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
    if confirmed != dst:
        raise SyncError(
            f"REFUSING: --confirm-write-to says {confirmed!r} but the write target "
            f"resolves to {dst!r}. Name the destination you actually mean."
        )
    return src, dst


_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def uuid_in_list(values: list[str]) -> str:
    """A quoted SQL ``IN`` list, refusing anything that is not a UUID.

    The ids come from a prior query against the same database, so they are not
    attacker-controlled — but string-built SQL is the pattern that bites, and a
    malformed id here would be interpolated verbatim. Validating the shape
    costs nothing and removes the class rather than annotating it away.
    """
    bad = [v for v in values if not _UUID.match(str(v))]
    if bad:
        raise SyncError(f"refusing to interpolate {len(bad)} non-UUID source id(s), e.g. {bad[0]!r}")
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
    return json.loads(proc.stdout.strip() or "[]")


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
    ap = argparse.ArgumentParser(description="Copy real jobs+sources into a non-production database.")
    ap.add_argument("--confirm-write-to", required=True, help="project ref of the write target")
    ap.add_argument("--limit", type=int, default=5000, help="most-recent jobs to copy (default 5000)")
    ap.add_argument("--dry-run", action="store_true", help="report what would be copied, write nothing")
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
        src, dst = assert_safe_direction(read_url, write_url, args.confirm_write_to)
    except SyncError as exc:
        print(f"sync: {exc}", file=sys.stderr)
        return 2
    try:
        require_web_url(write_url, "STAGING_SUPABASE_URL")
    except SyncError as exc:
        print(f"sync: {exc}", file=sys.stderr)
        return 2
    print(f"sync: reading {src} -> writing {dst}  (limit {args.limit})")

    jobs = psql_json(
        read_url,
        "SELECT * FROM public.jobs WHERE archived_at IS NULL AND purged_at IS NULL "  # noqa: S608 — only interpolation is an int() cast
        f"ORDER BY cataloged_at DESC NULLS LAST LIMIT {int(args.limit)}",
    )
    if not jobs:
        print("sync: no jobs matched; nothing to do.")
        return 0
    source_ids = sorted({j["source_id"] for j in jobs if j.get("source_id")})
    sources = psql_json(
        read_url,
        f"SELECT * FROM public.sources WHERE id IN ({uuid_in_list(source_ids)})",  # noqa: S608 — ids UUID-validated by uuid_in_list
    )

    # Forced, not defaulted. A copied-enabled source would let staging poll a
    # real board, and a poll that finds none of its jobs archives them — the
    # catalog this script just imported would delete itself.
    for s in sources:
        s["enabled"] = False
        s["last_polled_at"] = None
        s["consecutive_failures"] = 0

    print(f"sync: {len(jobs)} jobs across {len(sources)} sources (all forced enabled=false)")
    if args.dry_run:
        print("sync: --dry-run, wrote nothing.")
        for j in jobs[:3]:
            print(f"    would copy: {j.get('title')} | {j.get('company_name')}")
        return 0

    post_rows(write_url, write_key, "sources", sources)
    post_rows(write_url, write_key, "jobs", jobs)
    print(f"sync: done — {len(sources)} sources, {len(jobs)} jobs into {dst}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
