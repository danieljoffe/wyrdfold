"""Async load generator for the wyrdfold-api — interactive p95 under contention.

Hits read-heavy USER endpoints with N concurrent virtual users for a fixed
duration and reports p50/p95/p99 per endpoint. Two things make it the #57
before/after rig:

- **JWT auth.** The user routers went JWT-only (#220), so ``x-api-key`` now
  401s on them. This mints a real local token via the Supabase magic-link admin
  flow (``--user-email`` + ``--service-role`` against ``--supabase-url``) and
  sends it as ``Authorization: Bearer``. Paste one with ``--token`` to skip
  minting.
- **``--with-poll`` contention mode.** Fires ``POST /poll`` (operator, needs
  ``--poll-key``) on an interval *during* the VU load, so we measure interactive
  p95 while the poll's DB write herd runs — the exact regression #57 targets.
  Run it with the sync build (baseline) and again with ``POLLER_ASYNC_DB=true``
  (after) and compare the p95 rows.
- **``--sample`` resource gate.** Samples DB connections (``pg_stat_activity``,
  incl. the PostgREST-attributed count) + the API process RSS/FDs during the run
  and prints peaks — the "bounded pooler connections + stable memory / no FD
  leak" evidence the #57 acceptance criteria need (async fans out harder than the
  threadpool). Pass ``--api-pid`` (the host uvicorn PID) for RSS/FDs. No new
  dependency — shells out to ``psql`` / ``ps`` / ``lsof``.

Seed realistic local data first (``scripts/seed_load_test.py``: mock sources +
user + active target + jobs/scores), then run the API via host ``uvicorn``
against the local stack (NOT the Docker image: on Mac the container's JWT ``iss``
pins it to 127.0.0.1:54321 + host networking doesn't expose the port back — the
executor/threading behavior under test is identical either way, so host uvicorn
is representative here):

    SUPABASE_URL=http://127.0.0.1:54321 \
    SUPABASE_SERVICE_ROLE_KEY=<LOCAL_SR_KEY> \
      uv run python scripts/seed_load_test.py

    ALLOWED_HOSTS='*' LLM_PROVIDER=mock EMBEDDINGS_PROVIDER=mock \
    MOCK_FETCHER_ENABLED=true WYRDFOLD_API_KEY=<POLL_KEY> \
    SUPABASE_URL=http://127.0.0.1:54321 SUPABASE_SERVICE_ROLE_KEY=<LOCAL_SR_KEY> \
    SUPABASE_ANON_KEY=<LOCAL_ANON_KEY> \
      uv run uvicorn app.main:app --port 8001 &

    uv run python scripts/load_test.py --duration 45 --vus 25 \
      --supabase-url http://127.0.0.1:54321 --service-role <LOCAL_SR_KEY> \
      --user-email loadtest@example.com \
      --with-poll --poll-key <POLL_KEY> \
      --sample --api-pid "$(pgrep -f 'uvicorn app.main' | tail -1)"

Baseline = the default build (POLLER_ASYNC_DB off); the "after" sets
``POLLER_ASYNC_DB=true`` on the uvicorn env — compare the p95 + resource rows.

Inspect hot queries after a run:

    psql -c "SELECT substring(query,1,80) q, calls, round(mean_exec_time::numeric,2) mean_ms
             FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 20;"
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import time
from dataclasses import dataclass, field

import httpx

# User (JWT-only) read endpoints — the interactive surface a poll burst must
# not starve.
ENDPOINTS: list[tuple[str, str]] = [
    ("GET", "/jobs?limit=50"),
    ("GET", "/jobs?limit=50&search=engineer"),
    ("GET", "/jobs/pipeline-counts"),
    ("GET", "/insights/pipeline"),
    ("GET", "/insights/targets"),
    ("GET", "/insights/skills-cost"),
    ("GET", "/targets/mine"),
    ("GET", "/targets/active"),
    ("GET", "/experience/optimized"),
    ("GET", "/experience/gap-health"),
]


@dataclass
class EndpointStats:
    method: str
    path: str
    durations_ms: list[float] = field(default_factory=list)
    statuses: dict[int, int] = field(default_factory=dict)
    errors: int = 0


@dataclass
class DbConn:
    host: str
    port: int
    user: str
    name: str
    password: str


@dataclass
class SampleConfig:
    db: DbConn
    interval: float
    api_pid: int | None


@dataclass
class ResourceSample:
    t: float
    pg_total: int  # all backends on the target db
    pg_pgrst: int  # backends attributed to PostgREST (the API's DB pool)
    rss_mb: float | None  # API process RSS
    fds: int | None  # API process open file descriptors


async def mint_local_jwt(supabase_url: str, service_role: str, email: str) -> str:
    """Mint a real access token for ``email`` via the GoTrue admin magic-link
    flow (no mailbox needed). Creates the user if absent, generates a
    magic-link ``hashed_token``, then verifies it into a session.

    Local Supabase issues ES256 tokens with a working JWKS, which is exactly
    what the API's asymmetric-only verify expects — so the minted token passes
    ``verify_api_key_or_jwt`` against a locally-run API.
    """
    base = supabase_url.rstrip("/")
    admin_headers = {"apikey": service_role, "Authorization": f"Bearer {service_role}"}
    async with httpx.AsyncClient(timeout=15.0) as c:
        # Idempotent: ignore "already registered".
        await c.post(
            f"{base}/auth/v1/admin/users",
            headers=admin_headers,
            json={"email": email, "email_confirm": True},
        )
        link = await c.post(
            f"{base}/auth/v1/admin/generate_link",
            headers=admin_headers,
            json={"type": "magiclink", "email": email},
        )
        link.raise_for_status()
        body = link.json()
        hashed = body.get("hashed_token") or body.get("properties", {}).get("hashed_token")
        if not hashed:
            raise RuntimeError(f"no hashed_token in generate_link response: {body}")
        # Verify the token_hash into a session. Try the modern shape, then legacy.
        for payload in (
            {"type": "magiclink", "token_hash": hashed},
            {"type": "magiclink", "token": hashed, "email": email},
        ):
            v = await c.post(
                f"{base}/auth/v1/verify", headers={"apikey": service_role}, json=payload
            )
            if v.status_code == 200 and v.json().get("access_token"):
                return str(v.json()["access_token"])
        raise RuntimeError(f"verify did not return an access_token (last status {v.status_code})")


async def _vu_loop(
    client: httpx.AsyncClient,
    deadline: float,
    stats: dict[str, EndpointStats],
) -> None:
    """One virtual user — round-robins through endpoints until deadline."""
    idx = 0
    while time.monotonic() < deadline:
        method, path = ENDPOINTS[idx % len(ENDPOINTS)]
        idx += 1
        slot = stats[f"{method} {path}"]
        started = time.perf_counter()
        try:
            response = await client.request(method, path)
            slot.durations_ms.append((time.perf_counter() - started) * 1000.0)
            slot.statuses[response.status_code] = slot.statuses.get(response.status_code, 0) + 1
        except (httpx.HTTPError, TimeoutError):
            slot.errors += 1


async def _poll_burst_loop(
    base_url: str, poll_key: str, deadline: float, interval: float, counter: list[int]
) -> None:
    """Fire POST /poll every ``interval`` s until deadline — the write herd
    that competes with the interactive VUs for the executor."""
    async with httpx.AsyncClient(
        base_url=base_url, headers={"x-api-key": poll_key}, timeout=30.0
    ) as c:
        while time.monotonic() < deadline:
            try:
                r = await c.post("/poll")
                if r.status_code in (200, 202):
                    counter[0] += 1
            except (httpx.HTTPError, TimeoutError):
                pass
            await asyncio.sleep(interval)


async def _run_cmd(*args: str, env: dict[str, str] | None = None) -> str:
    """Run a short command; return stdout, or "" on any failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        out, _ = await proc.communicate()
    except (OSError, ValueError):
        return ""
    return out.decode(errors="replace")


async def _sample_pg(db: DbConn) -> tuple[int, int]:
    """(total backends, PostgREST-attributed backends) on the target db.

    PostgREST is the API's only path to Postgres (Kong -> PostgREST -> its own
    bounded pool), so its attributed count is the real "is the API's DB pool
    bounded" signal; total is the coarse pooler-pressure proxy. Note the
    sampler's own psql connection is one of the totals.
    """
    sql = (
        "select count(*), "
        "count(*) filter (where application_name ilike '%postgrest%') "
        "from pg_stat_activity where datname = current_database()"
    )
    out = await _run_cmd(
        "psql", "-h", db.host, "-p", str(db.port), "-U", db.user, "-d", db.name,
        "-tAc", sql,
        env={**os.environ, "PGPASSWORD": db.password},
    )
    line = out.strip().splitlines()[0] if out.strip() else ""
    total_s, _, pgrst_s = line.partition("|")
    try:
        return int(total_s), int(pgrst_s)
    except ValueError:
        return 0, 0


async def _sample_proc(pid: int) -> tuple[float | None, int | None]:
    """(RSS MB, open FD count) for pid via ps/lsof — no psutil dependency."""
    rss_out = (await _run_cmd("ps", "-o", "rss=", "-p", str(pid))).strip()
    rss_mb = int(rss_out) / 1024.0 if rss_out.isdigit() else None  # ps rss is KB
    fd_out = await _run_cmd("lsof", "-p", str(pid))
    fds: int | None = None
    if fd_out:
        rows = [ln for ln in fd_out.splitlines() if ln.strip()]
        fds = max(0, len(rows) - 1)  # drop the header row
    return rss_mb, fds


async def _resource_sampler_loop(
    cfg: SampleConfig, deadline: float, samples: list[ResourceSample]
) -> None:
    """Sample DB connections + API RSS/FDs every ``cfg.interval`` s until the
    deadline — the "bounded pooler / stable memory / no FD leak" evidence the
    #57 acceptance gate needs (async fans out harder than the threadpool)."""
    while time.monotonic() < deadline:
        pg_total, pg_pgrst = await _sample_pg(cfg.db)
        rss_mb = fds = None
        if cfg.api_pid is not None:
            rss_mb, fds = await _sample_proc(cfg.api_pid)
        samples.append(ResourceSample(time.monotonic(), pg_total, pg_pgrst, rss_mb, fds))
        await asyncio.sleep(cfg.interval)


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    sv = sorted(values)
    k = max(0, min(len(sv) - 1, int(round((pct / 100.0) * len(sv)) - 1)))
    return sv[k]


def _print_report(stats: dict[str, EndpointStats], elapsed: float, vus: int, polls: int) -> None:
    total = sum(len(s.durations_ms) for s in stats.values())
    errors = sum(s.errors for s in stats.values())
    rps = total / elapsed if elapsed > 0 else 0

    print()
    print("=" * 96)
    print(
        f"  duration={elapsed:.1f}s  vus={vus}  requests={total}  errors={errors}  "
        f"rps={rps:.1f}  polls_fired={polls}"
    )
    print("=" * 96)
    print(f"  {'endpoint':<48} {'n':>6} {'p50':>8} {'p95':>8} {'p99':>8} {'mean':>8}")
    print("-" * 96)
    for slot in sorted(stats.values(), key=lambda s: -_percentile(s.durations_ms, 95)):
        if not slot.durations_ms:
            continue
        n = len(slot.durations_ms)
        label = f"{slot.method} {slot.path}"
        print(
            f"  {label:<48} {n:>6} {_percentile(slot.durations_ms, 50):>7.1f}ms "
            f"{_percentile(slot.durations_ms, 95):>7.1f}ms "
            f"{_percentile(slot.durations_ms, 99):>7.1f}ms "
            f"{statistics.mean(slot.durations_ms):>7.1f}ms"
        )
        non_2xx = {c: n for c, n in slot.statuses.items() if c >= 300}
        if non_2xx or slot.errors:
            print(f"      ↳ non-2xx={non_2xx} errors={slot.errors}")
    print("=" * 96)


def _print_resource_report(samples: list[ResourceSample]) -> None:
    if not samples:
        return
    pg_totals = [s.pg_total for s in samples]
    pg_pgrst = [s.pg_pgrst for s in samples]
    rss = [s.rss_mb for s in samples if s.rss_mb is not None]
    fds = [s.fds for s in samples if s.fds is not None]
    print(f"  RESOURCES  (n={len(samples)} samples)")
    print("-" * 96)
    print(f"    pg backends       peak={max(pg_totals):>4}  median={int(statistics.median(pg_totals)):>4}")
    if any(pg_pgrst):
        print(f"    postgREST conns   peak={max(pg_pgrst):>4}  median={int(statistics.median(pg_pgrst)):>4}")
    if rss:
        span_min = (samples[-1].t - samples[0].t) / 60.0
        slope = (rss[-1] - rss[0]) / span_min if span_min > 0 else 0.0
        print(
            f"    api RSS (MB)      peak={max(rss):>6.0f}  start={rss[0]:>6.0f}  "
            f"end={rss[-1]:>6.0f}  slope={slope:>+6.1f} MB/min"
        )
    if fds:
        print(f"    api open FDs      peak={max(fds):>4}  start={fds[0]:>4}  end={fds[-1]:>4}")
    print("=" * 96)


async def _run(
    base_url: str,
    token: str,
    duration: int,
    vus: int,
    poll_key: str | None,
    poll_interval: float,
    sample_cfg: SampleConfig | None = None,
) -> None:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    stats = {f"{m} {p}": EndpointStats(method=m, path=p) for m, p in ENDPOINTS}
    deadline = time.monotonic() + duration
    started = time.monotonic()
    polls = [0]
    samples: list[ResourceSample] = []

    async with httpx.AsyncClient(
        base_url=base_url,
        headers=headers,
        timeout=httpx.Timeout(30.0),
        limits=httpx.Limits(max_connections=vus * 2, max_keepalive_connections=vus),
    ) as client:
        tasks = [asyncio.create_task(_vu_loop(client, deadline, stats)) for _ in range(vus)]
        if poll_key:
            tasks.append(
                asyncio.create_task(
                    _poll_burst_loop(base_url, poll_key, deadline, poll_interval, polls)
                )
            )
        if sample_cfg is not None:
            tasks.append(
                asyncio.create_task(_resource_sampler_loop(sample_cfg, deadline, samples))
            )
        await asyncio.gather(*tasks)

    _print_report(stats, time.monotonic() - started, vus, polls[0])
    _print_resource_report(samples)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=int, default=30, help="seconds (default 30)")
    parser.add_argument("--vus", type=int, default=20, help="virtual users (default 20)")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("WYRDFOLD_API_BASE_URL", "http://localhost:8001"),
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("WYRDFOLD_JWT", ""),
        help="Bearer JWT for user endpoints (else auto-mint)",
    )
    parser.add_argument("--supabase-url", default=os.environ.get("SUPABASE_URL", ""))
    parser.add_argument("--service-role", default=os.environ.get("SUPABASE_SERVICE_ROLE_KEY", ""))
    parser.add_argument("--user-email", default="loadtest@example.com")
    parser.add_argument(
        "--with-poll", action="store_true", help="fire POST /poll during the run (contention mode)"
    )
    parser.add_argument(
        "--poll-key",
        default=os.environ.get("WYRDFOLD_API_KEY", ""),
        help="operator key for POST /poll (required with --with-poll)",
    )
    parser.add_argument("--poll-interval", type=float, default=8.0)
    parser.add_argument(
        "--sample",
        action="store_true",
        help="sample DB connections + API RSS/FDs during the run (the resource gate)",
    )
    parser.add_argument(
        "--api-pid",
        type=int,
        default=None,
        help="PID of the API (host uvicorn) to sample RSS/FDs; omit to sample DB only",
    )
    parser.add_argument("--sample-interval", type=float, default=2.0)
    parser.add_argument("--db-host", default=os.environ.get("PGHOST", "127.0.0.1"))
    parser.add_argument("--db-port", type=int, default=int(os.environ.get("PGPORT", "54322")))
    parser.add_argument("--db-user", default=os.environ.get("PGUSER", "postgres"))
    parser.add_argument("--db-name", default=os.environ.get("PGDATABASE", "postgres"))
    parser.add_argument("--db-password", default=os.environ.get("PGPASSWORD", "postgres"))
    args = parser.parse_args()

    token = args.token
    if not token:
        if args.supabase_url and args.service_role:
            token = asyncio.run(
                mint_local_jwt(args.supabase_url, args.service_role, args.user_email)
            )
            print(f"minted local JWT for {args.user_email}")
        else:
            print(
                "warning: no --token and no --supabase-url/--service-role — user endpoints will 401"
            )

    poll_key = args.poll_key if args.with_poll else None
    if args.with_poll and not poll_key:
        parser.error("--with-poll requires --poll-key (or WYRDFOLD_API_KEY)")

    sample_cfg = None
    if args.sample:
        sample_cfg = SampleConfig(
            db=DbConn(
                host=args.db_host,
                port=args.db_port,
                user=args.db_user,
                name=args.db_name,
                password=args.db_password,
            ),
            interval=args.sample_interval,
            api_pid=args.api_pid,
        )

    asyncio.run(
        _run(
            args.base_url,
            token,
            args.duration,
            args.vus,
            poll_key,
            args.poll_interval,
            sample_cfg,
        )
    )


if __name__ == "__main__":
    main()
