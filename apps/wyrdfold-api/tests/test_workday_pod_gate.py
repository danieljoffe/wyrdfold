"""#646 — pod-level throttle for Workday's shared-host rate limit.

Workday rate-limits at the pod edge, not per tenant: the enabled catalog is
pod-concentrated (wd1 373 tenants, wd5 252, wd3 149), the poller runs several
sources at once, and each fans out detail fetches — so same-pod tenants
stacked 15-20 requests on one host and drew 429 storms. A detail fetch that
exhausts retries DROPS the posting, so the storm silently under-ingested.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.services import workday as wd

pytestmark = pytest.mark.asyncio


def test_pod_host_strips_tenant() -> None:
    assert wd._pod_host("https://cisco.wd5.myworkdayjobs.com") == "wd5.myworkdayjobs.com"
    assert wd._pod_host("https://msd.wd5.myworkdayjobs.com") == "wd5.myworkdayjobs.com"
    # Different pod → different gate key.
    assert wd._pod_host("https://assurant.wd1.myworkdayjobs.com") == "wd1.myworkdayjobs.com"
    # No tenant label / unparseable → gate on the host itself (never wider).
    assert wd._pod_host("https://myworkdayjobs.com") == "myworkdayjobs.com"
    assert wd._pod_host("not-a-url") == "not-a-url"


class _ConcurrencyProbe:
    """Records max simultaneous in-flight requests per pod host."""

    def __init__(self) -> None:
        self.live: dict[str, int] = {}
        self.peak: dict[str, int] = {}

    async def request(self, _method: str, url: str, **_kw: Any) -> Any:
        pod = wd._pod_host(url)
        self.live[pod] = self.live.get(pod, 0) + 1
        self.peak[pod] = max(self.peak.get(pod, 0), self.live[pod])
        try:
            await asyncio.sleep(0.02)
        finally:
            self.live[pod] -= 1

        class _Resp:
            status_code = 200

            @staticmethod
            def json() -> dict[str, Any]:
                if url.endswith("/jobs"):
                    return {
                        "jobPostings": [
                            {"externalPath": f"/job/x{i}", "title": f"T{i}"} for i in range(10)
                        ],
                        "total": 10,
                    }
                return {"jobPostingInfo": {"jobDescription": "<p>body</p>", "externalUrl": url}}

        return _Resp()


async def test_same_pod_tenants_share_the_gate(monkeypatch) -> None:
    """Four tenants on ONE pod, polled concurrently, must never exceed
    _POD_CONCURRENCY in flight — the per-source cap alone allowed 4x5=20."""
    probe = _ConcurrencyProbe()
    monkeypatch.setattr(wd, "request_with_retry", probe.request)
    wd._pod_gates.clear()

    tenants = ["cisco", "msd", "usfca", "zillow"]
    results = await asyncio.gather(
        *(wd.fetch_workday_jobs(f"https://{t}.wd5.myworkdayjobs.com|{t}|Careers") for t in tenants)
    )

    assert probe.peak["wd5.myworkdayjobs.com"] <= wd._POD_CONCURRENCY
    # Throttling must not cost postings: every tenant still returns its board.
    assert all(len(r) == 10 for r in results)


async def test_distinct_pods_run_in_parallel(monkeypatch) -> None:
    """The gate is per POD, not global — wd1 work must not queue behind wd5."""
    probe = _ConcurrencyProbe()
    monkeypatch.setattr(wd, "request_with_retry", probe.request)
    wd._pod_gates.clear()

    await asyncio.gather(
        wd.fetch_workday_jobs("https://cisco.wd5.myworkdayjobs.com|cisco|Careers"),
        wd.fetch_workday_jobs("https://assurant.wd1.myworkdayjobs.com|assurant|Careers"),
    )

    # Both pods saw real concurrency of their own (proves no global chokepoint).
    assert probe.peak["wd5.myworkdayjobs.com"] >= 2
    assert probe.peak["wd1.myworkdayjobs.com"] >= 2


def test_gate_is_per_event_loop() -> None:
    """A module-global Semaphore binds to the loop that first awaits it and
    raises on the next one (every async test gets a fresh loop). Keying on the
    running loop is what makes a module-scoped gate safe. Sync test: it owns
    its loops rather than nesting inside a running one."""
    wd._pod_gates.clear()

    async def _get(url: str) -> asyncio.Semaphore:
        return wd._pod_gate(url)

    loop1 = asyncio.new_event_loop()
    try:
        first = loop1.run_until_complete(_get("https://cisco.wd5.myworkdayjobs.com"))
        same_pod = loop1.run_until_complete(_get("https://msd.wd5.myworkdayjobs.com"))
    finally:
        loop1.close()
    assert same_pod is first, "same pod, same loop → one shared gate"

    loop2 = asyncio.new_event_loop()
    try:
        other_loop = loop2.run_until_complete(_get("https://cisco.wd5.myworkdayjobs.com"))
    finally:
        loop2.close()
    assert other_loop is not first, "different loop → distinct gate (no cross-loop reuse)"
