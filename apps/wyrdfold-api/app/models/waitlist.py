"""Pydantic shapes for the public marketing waitlist.

Mirrors the ``waitlist_signups`` table created in
``supabase/migrations/20260623120000_waitlist_signups.sql``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# Length cap mirrors the DB CHECK constraint (3..320). 320 is the practical
# RFC 5321 ceiling (64 local + @ + 255 domain). Pragmatic, not pedantic.
MIN_EMAIL_LENGTH = 3
MAX_EMAIL_LENGTH = 320


class WaitlistSignup(BaseModel):
    """Body for ``POST /waitlist``.

    Only the email is accepted. ``min_length``/``max_length`` give a cheap
    422 on absurd payloads before any DB round-trip; the router does the
    shape (RFC-ish regex) check and lower-case normalisation.
    """

    email: str = Field(min_length=MIN_EMAIL_LENGTH, max_length=MAX_EMAIL_LENGTH)


class WaitlistSignupResponse(BaseModel):
    """Generic success envelope.

    Deliberately identical for new, already-present, and duplicate-race
    signups: the endpoint never reveals whether an address already exists
    (no enumeration).
    """

    ok: bool = True
