"""Public waitlist signup for non-invited marketing-homepage visitors.

The landing page is publicly indexed; most visitors are NOT invited to the
private beta. This is the one PUBLIC (unauthenticated) write endpoint — anyone
can join the waitlist.

SECURITY POSTURE (audit #29):
  - Writes go through the service-role Supabase client (``get_supabase``) into
    ``waitlist_signups``, an RLS deny-all table. Service-role lives ONLY in
    this backend's env — the Next.js frontend never holds it. The browser can
    neither read nor write the table directly; it POSTs to the BFF
    (``/api/waitlist``) which forwards here.
  - Email is validated (Pydantic length cap + shape regex) before any DB call.
  - Rate-limited per client IP (slowapi) to brake automated list-stuffing.
  - NO ENUMERATION: the response is a generic success whether the email is
    new, already present, or a duplicate race (``ON CONFLICT DO NOTHING``).
    Failures return a generic 500 with no internal detail leaked.
"""

from __future__ import annotations

import asyncio
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from supabase import Client

from app.dependencies import get_supabase
from app.models.waitlist import WaitlistSignup, WaitlistSignupResponse
from app.rate_limit import limiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["waitlist"])

# Single ``@``, non-empty local part, a dot-bearing domain, no whitespace.
# Conservative on purpose — a junk gate, not an RFC parser. The DB CHECK
# constraint (3..320) plus the Pydantic length cap are the size backstop.
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _insert_signup(supabase: Client, email: str) -> None:
    """Idempotent insert via the service-role client.

    ``ignore_duplicates=True`` → ``ON CONFLICT DO NOTHING``: a duplicate is
    NOT an error here, it just means the address is already on the list, which
    we surface identically to a fresh signup (no enumeration). The
    case-insensitive unique index de-dupes regardless of caller casing.
    """
    (
        supabase.table("waitlist_signups")
        .upsert(
            {"email": email},
            on_conflict="email",
            ignore_duplicates=True,
        )
        .execute()
    )


@router.post("/waitlist", response_model=WaitlistSignupResponse)
@limiter.limit("5/minute;20/hour")
async def join_waitlist(
    request: Request,
    body: WaitlistSignup,
    supabase: Client = Depends(get_supabase),
) -> WaitlistSignupResponse:
    """Public, unauthenticated waitlist join.

    ``request`` is required by slowapi's ``@limiter.limit`` to key the limit
    (falls through to client IP since there is no JWT on this public route).

    Returns a generic success in every non-rate-limited, non-server-error
    case so the endpoint can't be used to probe which emails are on the list.
    """
    email = body.email.strip().lower()
    if not _EMAIL_RE.match(email):
        # 422 is the shape rejection (well-formed request, bad value). It does
        # NOT depend on prior state, so it leaks nothing about existing rows.
        raise HTTPException(
            status_code=422, detail="Please enter a valid email address."
        )

    try:
        # supabase-py is synchronous; ``to_thread`` keeps the blocking
        # ``.execute()`` round-trip off the event loop (repo #107 convention,
        # enforced by tests/test_no_blocking_supabase_in_async_handlers.py).
        await asyncio.to_thread(_insert_signup, supabase, email)
    except Exception:
        # Generic 500 — no internal detail (PostgREST/SQL fragments) leaked.
        logger.exception("waitlist signup failed")
        raise HTTPException(
            status_code=500, detail="Something went wrong. Please try again."
        ) from None

    return WaitlistSignupResponse(ok=True)
