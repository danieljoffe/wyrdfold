"""PERF-H2/H3: the poll cycle's LLM-qualify and URL-validation fan-outs are
bounded cycle-wide by per-loop semaphores. Without these bounds POLL_CONCURRENCY
sources × a whole chunk each opened hundreds of simultaneous OpenRouter calls /
URL validations.
"""

from __future__ import annotations

import asyncio

from app.services import poller


async def _peak_concurrency(sem_factory, workers: int) -> int:
    active = 0
    peak = 0

    async def worker() -> None:
        nonlocal active, peak
        async with sem_factory():
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.005)  # hold the slot so contention is real
            active -= 1

    await asyncio.gather(*(worker() for _ in range(workers)))
    return peak


async def test_qualify_llm_fanout_is_bounded() -> None:
    peak = await _peak_concurrency(poller._qualify_llm_semaphore, workers=60)
    # 60 workers >> the cap → it must saturate AND never exceed it.
    assert peak == poller.QUALIFY_LLM_CONCURRENCY


async def test_url_validation_fanout_is_bounded() -> None:
    peak = await _peak_concurrency(poller._validate_semaphore, workers=80)
    assert peak == poller.VALIDATE_CONCURRENCY
