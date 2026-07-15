"""Cron-facing bulk source discovery.

The per-target endpoint (``/targets/{id}/discover-sources``) is JWT-gated
and operator-triggered. This router is the API-key-gated counterpart for
scheduled callers (pg_cron, GitHub Actions, Railway cron) — it walks every
target and runs discovery for each, so new boards keep appearing without
anyone pressing a button.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from app.background import spawn_detached
from app.dependencies import verify_api_key
from app.services.source_discovery import run_discovery_all_targets_locked

router = APIRouter(prefix="/discovery", tags=["discovery"], dependencies=[Depends(verify_api_key)])


@router.post("/run", status_code=status.HTTP_202_ACCEPTED)
async def trigger_discovery_run() -> dict[str, str]:
    """Run source discovery across EVERY target (active + inactive).

    Returns ``202 Accepted`` immediately and runs the (~10-minute) discovery
    pass detached from the request, so manual/external triggers stop hitting
    the edge's 300s timeout — the full pass used to run synchronously inside
    the request, so a curl caller got a 499/502 timeout even when discovery
    itself was fine.

    The pass is routed through ``run_discovery_all_targets_locked``, which
    takes a Postgres advisory lock (DISTINCT from the poll lock): a manual
    trigger and the scheduled discovery tick can't run concurrently, and a
    trigger fired while a pass is already running logs "discovery already
    running, skipping" and exits cleanly. The body wraps the work in
    try/except so a failure is logged rather than silently swallowed. The
    Brave-key gate still applies inside the run — an empty
    ``BRAVE_SEARCH_API_KEY`` makes every per-target pass a clean no-op.

    Spawned via :func:`app.background.spawn_detached`, NOT starlette
    ``BackgroundTasks`` — its advisory-lock RPCs (and, as slices of #57
    land, its DB work) ride the pooled async client, which deadlocks inside
    the request's background machinery under uvloop (see
    ``app/background.py``).
    """
    spawn_detached(run_discovery_all_targets_locked(), name="discovery-run")
    return {"status": "scheduled"}
