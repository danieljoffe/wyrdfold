"""Search-funnel beacon (#467 §10 PR6) — browser-originated conversion ticks.

``search`` events are stamped server-side by the two search endpoints; the
two ticks only the browser can see — a card opened into the detail modal,
the logged-out detail's "Sign up free" click — arrive here.

SECURITY POSTURE — same as ``public_search`` (the other unauth surface):
  - **BFF-only** (``require_bff_secret``): the Next.js BFF injects the shared
    secret and forwards the trusted client IP, so the per-IP limit below
    can't be rotated past with a forged ``X-Forwarded-For``.
  - **Per-IP rate-limited**, looser than search (a browsing user opens many
    cards) but still a hard brake on row-flood abuse.
  - **Strictly typed payload** (Literal event kinds, UUID job id) — junk is a
    422, never a row; the DB CHECKs backstop.
  - **No identity recorded**: the request carries no auth and the row builder
    has no user/IP parameters (privacy by construction — see the service).
    ``surface`` is the client's claim: analytics-grade, never authorization.
"""

from __future__ import annotations

import logging
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from app.dependencies import require_bff_secret
from app.rate_limit import limiter
from app.services import search_events

logger = logging.getLogger(__name__)

router = APIRouter(
    tags=["search-events"],
    dependencies=[Depends(require_bff_secret)],
)


class SearchEventBeacon(BaseModel):
    event_type: Literal["card_open", "signup_click"]
    surface: Literal["public", "authed"]
    job_posting_id: UUID | None = None


@router.post("/search-events", status_code=204)
@limiter.limit("60/minute;600/hour")
async def search_event_beacon(request: Request, body: SearchEventBeacon) -> Response:
    """Record one funnel tick. Fire-and-forget: O(1) in-memory enqueue
    (bulk-flushed by a background task), 204 always — the browser never
    waits on, retries, or surfaces analytics. ``request`` is required by
    slowapi to key the per-IP limit."""
    search_events.record_funnel_event(
        event_type=body.event_type,
        surface=body.surface,
        job_posting_id=str(body.job_posting_id) if body.job_posting_id else None,
    )
    return Response(status_code=204)
