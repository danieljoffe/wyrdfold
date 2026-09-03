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

# Substrings that flag a location as non-US. Whole-word matched (see the
# regexes below) so US locations that merely *contain* a hint ("india" ⊂
# "Indianapolis, Indiana") are not falsely dropped.
#
# TWO LISTS, split by consequence (#950). A hint does two unrelated jobs:
#
# - **Ingest admission** (``is_us_location``): a hint with no US marker
#   present REJECTS the listing — it silently never enters the catalog. Every
#   entry here must survive the US-place-name screen, because a collision
#   loses real listings ("Stuttgart, Arkansas" is a real town).
# - **State-code disambiguation** (``positively_us_location``): a hint only
#   stops a trailing ", XX" from being read as a US state, declining to veto
#   an archive. The superset: ingest entries plus names that are ALSO real US
#   places and therefore must never reject at ingest.
#
# ``_FOREIGN_HINTS_FOR_INGEST`` is the poller's original ingestion-gate hint
# list, moved here verbatim (every entry preserved so the pinned poller
# US-gate behaviour in ``tests/test_poller.py`` is unchanged), with a small
# ``# --- #60 additions`` block of cities the qualification firewall's
# dry-run surfaced that the original list missed (Taichung, Calgary,
# Bulgaria, ...). Additions only widen the non-US set; they never flip a
# previously-US location to non-US for any string the existing tests assert
# on.
_FOREIGN_HINTS_FOR_INGEST: tuple[str, ...] = (
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
    # --- Cities in countries whose ISO code collides with a USPS state
    # abbreviation (IN/DE/GA/MT/ID/IL/MD/MN/MO/LA/TN/SC/SD/CO...).
    #
    # Why these specifically: ``positively_us_location`` treats a trailing
    # ", XX" as a US state and its docstring claims the no-foreign-hint clause
    # stops the collisions — naming "Bangalore, IN" as covered. The clause only
    # works for cities the hint list KNOWS, and it did not know Chennai or
    # Ahmedabad, so ``Chennai, IN`` read as Indiana and VETOED the non-US
    # archive for a genuinely Indian posting. Observed 3 times in 30 days.
    #
    # DELIBERATELY EXCLUDED FROM BOTH LISTS, because each is also a real US
    # place name: "georgia" (the state), "panama" (Panama City, FL),
    # "hamburg" (Hamburg, NY/PA), "cologne" (Cologne, MN), "malta" (Malta,
    # NY/IL), "morocco" (Morocco, IN), "sudan" (Sudan, TX), "waterloo"
    # (Waterloo, IA), plain "bogota" (Bogota, NJ). The accented "bogotá" is
    # safe and included; the unaccented form is not. "frankfurt" is included —
    # the Kentucky capital is spelled "Frankfort".
    #
    # They stay out of the DISAMBIGUATION list too: ``positively_us_location``
    # checks the hint BEFORE the positive US markers, so a US-colliding hint
    # would strip the archive veto from genuine US strings that spell the
    # marker out ("Augusta, Georgia, USA" would lose the veto that exists to
    # protect exactly it).
    "chennai",
    "ahmedabad",
    "kolkata",
    "gurgaon",
    "gurugram",
    "noida",
    "jaipur",
    "dusseldorf",
    "d\u00fcsseldorf",
    "leipzig",
    "dortmund",
    "frankfurt",
    "tbilisi",
    "valletta",
    "indonesia",
    "jakarta",
    "haifa",
    "chisinau",
    "chi\u0219in\u0103u",
    "ulaanbaatar",
    "macau",
    "vientiane",
    "tunis",
    "khartoum",
    "tirana",
    "noumea",
    "medellin",
    "medell\u00edn",
    "bogot\u00e1",
)

# Foreign names that are ALSO real US place names, so they must never reject
# at ingest — but are still needed to stop their country's ISO code from
# reading as a US state ("Stuttgart, DE" is Germany, not Delaware). Both were
# merged into the single list by #949 and silently rejected the US towns at
# ingest (#950): Stuttgart, Arkansas (~9,000 people, Riceland Foods HQ);
# Jerusalem, New York / Ohio / Arkansas. The abbreviated US forms
# ("Stuttgart, AR") were never affected — the state-abbreviation check
# short-circuits before the hint is consulted — but the spelled-out forms
# ("Stuttgart, Arkansas") and the bare names were rejected.
_DISAMBIGUATION_ONLY_HINTS: tuple[str, ...] = (
    "stuttgart",
    "jerusalem",
)

_FOREIGN_HINTS_FOR_STATE_DISAMBIGUATION: tuple[str, ...] = (
    _FOREIGN_HINTS_FOR_INGEST + _DISAMBIGUATION_ONLY_HINTS
)


def _word_boundary_re(hints: tuple[str, ...]) -> re.Pattern[str]:
    """Word-boundary pattern over the hints. Plain substring matching produced
    false drops on US locations that merely *contain* a hint: "india" ⊂
    "Indianapolis, Indiana", "rome" ⊂ "Rome, GA", etc."""
    return re.compile(r"\b(?:" + "|".join(re.escape(h) for h in hints) + r")\b")


_NON_US_INGEST_RE = _word_boundary_re(_FOREIGN_HINTS_FOR_INGEST)
_NON_US_DISAMBIGUATION_RE = _word_boundary_re(_FOREIGN_HINTS_FOR_STATE_DISAMBIGUATION)

# Explicit US markers that short-circuit the non-US rejection. Needed for
# US cities that share a name with a non-US hint city: "Dublin, OH",
# "Dublin, CA", "Athens, GA", "Milan, MI" are all real US locations that
# the hint list would otherwise reject.
_US_COUNTRY_RE = re.compile(r"\b(?:usa|u\.s\.a?|united states)\b", re.I)

# The bare alpha-2 ``US``, which ``_US_COUNTRY_RE`` deliberately does not carry.
# CASE-SENSITIVE, and that is the whole point: lower-case "us" is the English
# pronoun, and a case-insensitive ``\bus\b`` would read "Join us in Berlin" or
# "Contact us" as a US marker — in ``positively_us_location`` that marker VETOES
# a non-US archive, so a false positive keeps foreign postings in a US-only
# catalog. Upper-case ``US`` in a location field is the country code.
#
# Why it was missing: real prod strings ("Remote - US", "US") matched none of
# the spelled-out forms, so a genuinely US remote posting carried no positive
# marker and lost the archive veto that exists to protect exactly it.
_US_CODE_RE = re.compile(r"\bUS\b")

_US_STATE_ABBREVS: frozenset[str] = frozenset(
    {
        "AL",
        "AK",
        "AZ",
        "AR",
        "CA",
        "CO",
        "CT",
        "DE",
        "FL",
        "GA",
        "HI",
        "ID",
        "IL",
        "IN",
        "IA",
        "KS",
        "KY",
        "LA",
        "ME",
        "MD",
        "MA",
        "MI",
        "MN",
        "MS",
        "MO",
        "MT",
        "NE",
        "NV",
        "NH",
        "NJ",
        "NM",
        "NY",
        "NC",
        "ND",
        "OH",
        "OK",
        "OR",
        "PA",
        "RI",
        "SC",
        "SD",
        "TN",
        "TX",
        "UT",
        "VT",
        "VA",
        "WA",
        "WV",
        "WI",
        "WY",
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

    Consults the CONSERVATIVE hint list (``_FOREIGN_HINTS_FOR_INGEST``): a
    hint here rejects the listing at ingest, so US-place-name collisions
    (Stuttgart, Arkansas) are kept out of it (#950).
    """
    if not location:
        return True
    if _US_COUNTRY_RE.search(location) or _US_CODE_RE.search(location):
        return True
    if any(m.group(1) in _US_STATE_ABBREVS for m in _US_STATE_ABBREV_RE.finditer(location)):
        return True
    return not _NON_US_INGEST_RE.search(location.lower())


def positively_us_location(location: str | None) -> bool:
    """True only when the location string UNAMBIGUOUSLY names the US — an
    explicit ``United States`` / ``USA`` marker, or a ``City, ST`` state
    abbreviation — AND carries no known non-US hint.

    The strict complement of the permissive :func:`is_us_location`: where that
    returns True for empty / 'Remote' / ambiguous, this returns True *only* on
    a positive US signal. It's the archive veto (#60 workstream B): even a
    high-confidence L2 non-US verdict must NOT archive a job whose location
    plainly says US (a tagger false-negative on ``New York, NY, United States``
    was observed at confidence 95). The no-foreign-hint clause keeps it from
    vetoing collision cases the state-abbrev alone would trip — ``Munich, DE``
    (Delaware), ``Bangalore, IN`` (Indiana), ``Toronto, ON, CA`` (California)
    all carry a non-US hint, so they still archive.

    Consults the SUPERSET hint list
    (``_FOREIGN_HINTS_FOR_STATE_DISAMBIGUATION``): a hint here only declines
    to veto an archive, so it can carry US-colliding names like "stuttgart"
    that the ingest gate must not (#950).
    """
    if not location:
        return False
    if _NON_US_DISAMBIGUATION_RE.search(location.lower()):
        return False
    if _US_COUNTRY_RE.search(location) or _US_CODE_RE.search(location):
        return True
    return any(m.group(1) in _US_STATE_ABBREVS for m in _US_STATE_ABBREV_RE.finditer(location))


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
