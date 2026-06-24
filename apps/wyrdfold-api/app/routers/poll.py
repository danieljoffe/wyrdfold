from fastapi import APIRouter, BackgroundTasks, Depends, status
from supabase import Client

from app.cache import job_list_cache
from app.dependencies import get_supabase, verify_api_key
from app.models.schemas import PollResult
from app.scheduler import run_force_poll_locked
from app.services.poller import poll_due_sources

router = APIRouter(tags=["poll"], dependencies=[Depends(verify_api_key)])


@router.post("/poll", status_code=status.HTTP_202_ACCEPTED)
async def trigger_poll(background_tasks: BackgroundTasks) -> dict[str, str]:
    """Force-poll every enabled source. Ignores ``poll_interval_minutes``.

    Returns ``202 Accepted`` immediately and runs the (multi-minute) poll
    in the background, so manual/external triggers stop hitting the edge's
    300s timeout — the full poll used to run synchronously inside the
    request, so a curl caller got a 499/502 timeout even when the poll
    itself was fine.

    The background poll is routed through ``run_force_poll_locked``, which
    takes the SAME advisory lock as the scheduler: a manual trigger and the
    scheduled due-poll can't run concurrently, and a trigger fired while a
    poll is already running logs "poll already running, skipping" and exits
    cleanly. The background task wraps the poll in try/except so a failure
    is logged rather than silently swallowed.

    Use sparingly — this is the manual hammer. Routine polling should
    go through ``/poll/due`` (or the in-process scheduler that calls
    ``poll_due_sources`` directly).
    """
    background_tasks.add_task(run_force_poll_locked)
    return {"status": "scheduled"}


@router.post("/poll/due", response_model=PollResult)
async def trigger_poll_due(supabase: Client = Depends(get_supabase)) -> PollResult:
    """Poll only sources whose interval has elapsed.

    Same authentication as ``/poll`` but cheap to call frequently —
    sources that were polled recently are skipped. Mirrors what the
    in-process scheduler does, exposed as an HTTP endpoint so external
    cron callers (pg_cron, GitHub Actions) can drive it without
    running APScheduler in the API.
    """
    result = await poll_due_sources(supabase)
    if result.sources_polled > 0:
        job_list_cache.invalidate()
    return result
