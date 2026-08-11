"""Public waitlist signup for non-invited marketing-homepage visitors.

The landing page is publicly indexed; most visitors are NOT invited to the
private beta. This is the one PUBLIC (unauthenticated) write endpoint — anyone
can join the waitlist.

SECURITY POSTURE (audit #29):
  - Writes go through the service-role Supabase client
    (``get_async_service_supabase``) into ``waitlist_signups``, an RLS deny-all
    table. Service-role lives ONLY in
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

import logging
import re
from typing import Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from supabase import AsyncClient

from app.dependencies import get_async_service_supabase, require_bff_secret
from app.models.waitlist import WaitlistSignup, WaitlistSignupResponse
from app.rate_limit import limiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["waitlist"])

# Single ``@``, non-empty local part, a dot-bearing domain, no whitespace.
# Conservative on purpose — a junk gate, not an RFC parser. The DB CHECK
# constraint (3..320) plus the Pydantic length cap are the size backstop.
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


async def _insert_signup(supabase: AsyncClient, email: str) -> None:
    """Idempotent insert via the async service-role client.

    ``ignore_duplicates=True`` → ``ON CONFLICT DO NOTHING``: a duplicate is
    NOT an error here, it just means the address is already on the list, which
    we surface identically to a fresh signup (no enumeration). The
    case-insensitive unique index de-dupes regardless of caller casing.
    """
    await (
        supabase.table("waitlist_signups")
        .upsert(
            {"email": email},
            on_conflict="email",
            ignore_duplicates=True,
        )
        .execute()
    )


@router.post(
    "/waitlist",
    response_model=WaitlistSignupResponse,
    # SEC-5: BFF-only. The per-IP limit below is only spoof-proof if the
    # endpoint can't be hit directly with a forged X-Forwarded-For.
    dependencies=[Depends(require_bff_secret)],
)
@limiter.limit("5/minute;20/hour")
async def join_waitlist(
    request: Request,
    body: WaitlistSignup,
    supabase: AsyncClient = Depends(get_async_service_supabase),
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
        raise HTTPException(status_code=422, detail="Please enter a valid email address.")

    try:
        # Native async round-trip on the pooled service client (#57 slice 4).
        await _insert_signup(supabase, email)
    except Exception:
        # Generic 500 — no internal detail (PostgREST/SQL fragments) leaked.
        logger.exception("waitlist signup failed")
        raise HTTPException(
            status_code=500, detail="Something went wrong. Please try again."
        ) from None

    return WaitlistSignupResponse(ok=True)


class SignupModeResponse(BaseModel):
    mode: Literal["closed", "open"]


# Module-level async helper so the handler holds no inline ``.execute()`` on the
# loop (#57 slice 4) — the CI guard scans only router handlers.
async def _read_signup_mode_rows(supabase: AsyncClient) -> list[dict[str, str]]:
    resp = await supabase.table("app_settings").select("value").eq("key", "signup_mode").execute()
    return cast("list[dict[str, str]]", resp.data or [])


# Native async handler (#57 slice 4): the read runs on the pooled async service
# client instead of tying up a threadpool worker.
@router.get(
    "/signup-mode",
    response_model=SignupModeResponse,
    dependencies=[Depends(require_bff_secret)],  # SEC-5: BFF-only (see join_waitlist)
)
@limiter.limit("30/minute")
async def get_signup_mode(
    request: Request,
    supabase: AsyncClient = Depends(get_async_service_supabase),
) -> SignupModeResponse:
    """Public perimeter probe (Phase 3 slice 5).

    The FE reads this to decide sign-in vs sign-up presentation
    (homepage CTA, `shouldCreateUser`). Pre-auth by design — it reveals
    only whether signup is open, which the signup form itself would
    reveal anyway. Fail-safe: any read problem reports 'closed', and an
    unknown value is treated as 'closed' (mirrors the hook's posture).
    """
    mode: Literal["closed", "open"] = "closed"
    try:
        rows = await _read_signup_mode_rows(supabase)
        if rows and rows[0].get("value") == "open":
            mode = "open"
    except Exception:
        logger.warning("signup-mode read failed — reporting closed", exc_info=True)
    return SignupModeResponse(mode=mode)
