"""Cron-facing bulk source discovery.

The per-target endpoint (``/targets/{id}/discover-sources``) is JWT-gated
and operator-triggered. This router is the API-key-gated counterpart for
scheduled callers (pg_cron, GitHub Actions, Railway cron) — it walks every
target and runs discovery for each, so new boards keep appearing without
anyone pressing a button.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, status

from app.dependencies import verify_api_key
from app.services.source_discovery import run_discovery_all_targets_locked

router = APIRouter(prefix="/discovery", tags=["discovery"], dependencies=[Depends(verify_api_key)])


@router.post("/run", status_code=status.HTTP_202_ACCEPTED)
async def trigger_discovery_run(background_tasks: BackgroundTasks) -> dict[str, str]:
    """Run source discovery across EVERY target (active + inactive).

    Returns ``202 Accepted`` immediately and runs the (~10-minute) discovery
    pass in the background, so manual/external triggers stop hitting the edge's
    300s timeout — the full pass used to run synchronously inside the request,
    so a curl caller got a 499/502 timeout even when discovery itself was fine.

    The background pass is routed through ``run_discovery_all_targets_locked``,
    which takes a Postgres advisory lock (DISTINCT from the poll lock): a manual
    trigger and the scheduled discovery tick can't run concurrently, and a
    trigger fired while a pass is already running logs "discovery already
    running, skipping" and exits cleanly. The background task wraps the work in
    try/except so a failure is logged rather than silently swallowed. The
    Brave-key gate still applies inside the run — an empty
    ``BRAVE_SEARCH_API_KEY`` makes every per-target pass a clean no-op.
    """
    background_tasks.add_task(run_discovery_all_targets_locked)
    return {"status": "scheduled"}
