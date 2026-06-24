"""Normalize ATS posting dates into a Postgres ``timestamptz``-safe value.

Every fetcher (Workday, SmartRecruiters, Greenhouse, Lever, Ashby,
JSON-LD, Firecrawl) populates ``StandardJob.updated_at`` from whatever
its source happens to expose, and the poller writes that straight into
the ``greenhouse_updated_at`` column (``timestamp with time zone``).

The problem (live Railway prod failures): the raw values are *not*
always ISO-8601. Real shapes seen failing the upsert with PostgREST
``22007``/``22008``:

- Workday detail/list ``postedOn`` is the human-facing relative string,
  e.g. ``"Posted Today"``, ``"Posted Yesterday"``, ``"Posted 5 Days Ago"``
  — Postgres rejects with ``invalid input syntax for type timestamp with
  time zone``.
- Some SmartRecruiters ``releasedDate`` values arrive as a 13-digit
  millisecond epoch, e.g. ``"1779198175584"`` — Postgres reads that as
  a year far in the future and rejects with ``date/time field value out
  of range``.

A single bad row in a batch upsert fails the *whole* upsert, which fails
the *whole* source for that poll cycle. So normalization happens once,
centrally, at the upsert boundary: every fetcher's value flows through
``normalize_posted_at`` before it is written.

Contract: return an ISO-8601 UTC string PostgREST accepts, or ``None``
(written as SQL NULL) when the value is missing or unparseable. It must
**never raise** — a date we can't parse must degrade to NULL, never crash
the poll.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

# "Posted 5 Days Ago", "Posted 11 Days Ago", "5 days ago", "1 day ago".
_RELATIVE_DAYS_RE = re.compile(r"(\d+)\s+days?\s+ago", re.IGNORECASE)

# Plausible-epoch bounds. A 13-digit ms-epoch / 10-digit s-epoch only
# makes sense as a posting date inside a sane window; outside it we'd
# rather NULL than write a bogus far-future/ancient timestamp.
# 2000-01-01 .. 2100-01-01 in seconds.
_MIN_EPOCH_S = 946_684_800
_MAX_EPOCH_S = 4_102_444_800


def _today_utc() -> datetime:
    """Midnight UTC today. Relative strings ("Today", "N Days Ago") are
    day-granular, so we anchor them at start-of-day rather than ``now()``."""
    now = datetime.now(UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _from_epoch(raw: str) -> datetime | None:
    """Parse a bare numeric epoch. 13 digits → milliseconds, 10 → seconds.

    Returns ``None`` (rather than a wild timestamp) when the resulting
    instant falls outside the plausible window.
    """
    if not raw.isdigit():
        return None
    value = int(raw)
    digits = len(raw)
    if digits >= 12:  # milliseconds (13 digits typical; 12 covers older)
        seconds = value / 1000.0
    elif digits >= 9:  # seconds (10 digits typical)
        seconds = float(value)
    else:
        # Too few digits to be a real epoch (e.g. a bare year/id).
        return None
    if not (_MIN_EPOCH_S <= seconds <= _MAX_EPOCH_S):
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _from_iso(raw: str) -> datetime | None:
    """Parse an ISO-8601 string (tolerating a trailing ``Z``). Assume UTC
    when no offset is present so the stored value is always tz-aware."""
    candidate = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _from_relative(raw: str) -> datetime | None:
    """Parse the human-facing relative strings Workday emits."""
    lowered = raw.lower()
    if "today" in lowered:
        return _today_utc()
    if "yesterday" in lowered:
        return _today_utc() - timedelta(days=1)
    match = _RELATIVE_DAYS_RE.search(lowered)
    if match:
        return _today_utc() - timedelta(days=int(match.group(1)))
    return None


def normalize_posted_at(raw: object) -> str | None:
    """Coerce an ATS posting date into an ISO-8601 UTC string or ``None``.

    Accepts ISO strings, bare ms/s epochs, and the relative
    ("Posted Today" / "Posted Yesterday" / "Posted N Days Ago") strings.
    Anything else — empty, ``None``, or unparseable — becomes ``None`` so
    the upsert writes SQL NULL instead of failing the whole batch.

    Never raises.
    """
    if raw is None:
        return None

    # A datetime already in hand (a fetcher could pass one through).
    if isinstance(raw, datetime):
        dt = raw if raw.tzinfo is not None else raw.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat()

    text = str(raw).strip()
    if not text:
        return None

    # Order matters: try ISO first (the common, cheap case), then epoch,
    # then the relative phrasings.
    parsed = _from_iso(text) or _from_epoch(text) or _from_relative(text)
    if parsed is None:
        return None
    return parsed.astimezone(UTC).isoformat()
