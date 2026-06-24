"""L1 qualification heuristics — pure Python, no LLM (#60).

The cheap, deterministic first layer of the qualification firewall. Three
jobs:

1. **US location detection** (``is_us_location``). The canonical home for the
   permissive country guess the poller already used as its ingestion-time US
   gate. It lives here (not in ``poller``) so both the poller and the L2 tagger
   share one implementation; ``poller`` re-exports the name for back-compat.
2. **Description cleanup** (``clean_description``). Strip HTML tags and decode
   HTML entities so the L2 prompt sees readable prose, not ``&amp;`` and
   ``<div>`` noise — fewer tokens, sharper signal.
3. **Content hashing** (``qualification_hash``). A stable sha256 over the
   intrinsic fields (title + company + location + description) so the tagger
   can skip re-classifying a row whose content hasn't changed since the last
   poll.

It also exposes ``prefill_tags`` — the obvious-case pre-tagging the LLM
shouldn't need to be paid for (e.g. a clearly non-US location → ``is_us=False``
with high confidence). The L2 layer fills the rest.
"""

from __future__ import annotations

import hashlib
import html
import re

from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# US location detection. Moved verbatim from app/services/poller.py so the
# poller's ingestion gate and the L2 tagger's L1 pre-tag agree byte-for-byte.
# Permissive by design: empty/None and generic 'Remote' pass through as US
# because many US companies list remote roles with no country; we reject only
# on a known non-US whole-word hint with no explicit US marker present.
# ---------------------------------------------------------------------------

# Substrings that flag a location as non-US. Whole-word matched (see
# ``_NON_US_RE``) so US locations that merely *contain* a hint ("india" ⊂
# "Indianapolis, Indiana") are not falsely dropped.
#
# This is the poller's original ingestion-gate hint list, moved here verbatim
# (every entry preserved so the pinned poller US-gate behaviour in
# ``tests/test_poller.py`` is unchanged), with a small ``# --- #60 additions``
# block of cities the qualification firewall's dry-run surfaced that the
# original list missed (Taichung, Calgary, Bulgaria, ...). Additions only widen
# the non-US set; they never flip a previously-US location to non-US for any
# string the existing tests assert on.
_NON_US_HINTS: tuple[str, ...] = (
    "united kingdom",
    "england",
    "scotland",
    "wales",
    "ireland",
    "dublin",
    "germany",
    "berlin",
    "munich",
    "france",
    "paris",
    "netherlands",
    "amsterdam",
    "spain",
    "barcelona",
    "madrid",
    "italy",
    "rome",
    "milan",
    "sweden",
    "stockholm",
    "denmark",
    "copenhagen",
    "norway",
    "oslo",
    "finland",
    "helsinki",
    "switzerland",
    "zurich",
    "geneva",
    "austria",
    "vienna",
    "poland",
    "warsaw",
    "czech",
    "czechia",
    "prague",
    "portugal",
    "lisbon",
    "greece",
    "athens",
    "turkey",
    "istanbul",
    "canada",
    "toronto",
    "vancouver",
    "montreal",
    "ottawa",
    "mexico",
    "brazil",
    "são paulo",
    "sao paulo",
    "india",
    "bangalore",
    "bengaluru",
    "hyderabad",
    "mumbai",
    "delhi",
    "pune",
    "china",
    "beijing",
    "shanghai",
    "hong kong",
    "singapore",
    "japan",
    "tokyo",
    "korea",
    "seoul",
    "taiwan",
    "australia",
    "sydney",
    "melbourne",
    "new zealand",
    "auckland",
    "israel",
    "tel aviv",
    "south africa",
    "johannesburg",
    "argentina",
    "buenos aires",
    "chile",
    "colombia",
    "peru",
    "uae",
    "dubai",
    "abu dhabi",
    "emea",
    "apac",
    "latam",
    "europe",
    # --- #60 additions: cities/countries the dry-run hit that the original
    #     ingestion-gate list didn't cover. Whole-word matched, so none
    #     collide with a US place name the prior tests rely on.
    "calgary",
    "edmonton",
    "taichung",
    "taipei",
    "kaohsiung",
    "bulgaria",
    "sofia",
    "romania",
    "bucharest",
    "ukraine",
    "shenzhen",
)

# Word-boundary pattern over the hints. Plain substring matching produced
# false drops on US locations that merely *contain* a hint: "india" ⊂
# "Indianapolis, Indiana", "rome" ⊂ "Rome, GA", etc.
_NON_US_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(h) for h in _NON_US_HINTS) + r")\b"
)

# Explicit US markers that short-circuit the non-US rejection. Needed for
# US cities that share a name with a non-US hint city: "Dublin, OH",
# "Dublin, CA", "Athens, GA", "Milan, MI" are all real US locations that
# the hint list would otherwise reject.
_US_COUNTRY_RE = re.compile(r"\b(?:usa|u\.s\.a?|united states)\b", re.I)

_US_STATE_ABBREVS: frozenset[str] = frozenset(
    {
        "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
        "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
        "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
        "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
        "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
        "DC",
    }
)

# ", XX" with XX upper-case — the standard "City, ST" form. Checked against
# the original casing so lowercase words ("ca" in "Africa") can't match.
_US_STATE_ABBREV_RE = re.compile(r",\s*([A-Z]{2})\b")


def is_us_location(location: str | None) -> bool:
    """Return True if the location looks like it's in the US (or is ambiguous).

    Permissive by design: empty/None and generic 'Remote' pass through,
    since many US companies list remote roles with no country. Rejects
    only when a known non-US country or major city name is detected as a
    whole word AND no explicit US marker (country name or "City, ST"
    state abbreviation) is present. The US marker wins ties on purpose —
    a rare "Berlin, DE" style ISO-code listing slips through as US, which
    the downstream scoring tolerates far better than silently dropping
    every "Dublin, CA".

    A multi-location string that includes ANY explicit US marker (e.g.
    "New York, Stamford, London") is treated as US — the US marker
    short-circuits before the non-US hint is even consulted.
    """
    if not location:
        return True
    if _US_COUNTRY_RE.search(location):
        return True
    if any(
        m.group(1) in _US_STATE_ABBREVS
        for m in _US_STATE_ABBREV_RE.finditer(location)
    ):
        return True
    return not _NON_US_RE.search(location.lower())


# ---------------------------------------------------------------------------
# Description cleanup.
# ---------------------------------------------------------------------------

_WHITESPACE_RE = re.compile(r"\s+")


def clean_description(raw: str | None) -> str:
    """Strip HTML tags + decode HTML entities + collapse whitespace.

    ATS descriptions arrive as HTML (Greenhouse/Lever/Ashby) or as
    entity-escaped text. ``BeautifulSoup`` removes the tags;
    ``html.unescape`` decodes anything the parser left as a raw entity
    (e.g. double-escaped ``&amp;amp;``); whitespace is collapsed so the
    L2 prompt is compact. Returns ``""`` for None/empty input.
    """
    if not raw:
        return ""
    text = BeautifulSoup(raw, "html.parser").get_text(separator=" ")
    text = html.unescape(text)
    return _WHITESPACE_RE.sub(" ", text).strip()


# ---------------------------------------------------------------------------
# Content hashing.
# ---------------------------------------------------------------------------


def qualification_hash(
    *,
    title: str | None,
    company: str | None,
    location: str | None,
    description: str | None,
) -> str:
    """Stable sha256 over the intrinsic fields the tagger reads.

    Cleared/recomputed only when one of (title, company, location,
    description) changes — so a re-poll that returns the same posting
    skips the LLM call. The description is cleaned first so cosmetic HTML
    re-encoding (a vendor switching ``&amp;`` ↔ ``&``) doesn't churn the
    hash. Fields are NUL-separated so ``("ab", "c")`` and ``("a", "bc")``
    can't collide.
    """
    parts = [
        (title or "").strip(),
        (company or "").strip(),
        (location or "").strip(),
        clean_description(description),
    ]
    joined = "\x00".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()
