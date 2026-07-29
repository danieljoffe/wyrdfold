"""Deterministic job-location parsing → structured (city, state, country, remote).

ATS boards deliver ``location`` as free text in wildly inconsistent shapes —
the regression corpus in ``tests/test_location_parse.py`` is sampled verbatim
from prod and is the contract for this module. Representative inputs:

    "San Francisco"                       "San Francisco, CA"
    "San Francisco, California, United States"
    "Remote - US"    "US Remote"    "United States (Remote)"
    "US-CA-Menlo Park"                    (Workday)
    "London, gb"     "London - The River Building HQ"
    "San Mateo, CA United States"         (missing comma)
    "San Francisco, CA, US; Remote, US"   (multi-location)
    "2 Locations"    "Hybrid"             (unparseable → all None)

Design rules:
- Parse only what the string SAYS, plus a small explicit metro map for the
  bare-city forms that dominate prod ("San Francisco" alone is >1k rows).
  Unknown shapes yield all-``None`` parts — the UI falls back to the raw
  string, so a miss degrades to today's behavior, never worse.
- ``remote`` is derived ONLY from explicit remote wording in the location
  string. It is deliberately a separate signal from the LLM tagger's
  ``jobs.is_remote`` (JD-inferred): this one is board-stated ground truth.
- Multi-location strings parse their FIRST segment as the primary place;
  ``remote``/``country`` are enriched from the remaining segments.

The poller re-derives these fields from the fresh board payload every cycle
(#514), so parser improvements back-apply to live rows automatically — same
convergence model as ``extract_salary_from_html``. Grow the corpus with every
real-world miss.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["LocationParts", "parse_location"]


@dataclass(frozen=True)
class LocationParts:
    city: str | None
    state: str | None
    country: str | None
    remote: bool

    @property
    def is_empty(self) -> bool:
        return self.city is None and self.state is None and self.country is None


_EMPTY = LocationParts(None, None, None, False)

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

_US_STATES: dict[str, str] = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
    "district of columbia": "DC",
}
_US_STATE_ABBREVS = set(_US_STATES.values())

# Country tokens → canonical display name. US/UK stay abbreviated (the
# display convention); everything else is the common English name.
_COUNTRIES: dict[str, str] = {
    "us": "US",
    "usa": "US",
    "u.s.": "US",
    "u.s.a.": "US",
    "united states": "US",
    "united states of america": "US",
    "uk": "UK",
    "u.k.": "UK",
    "united kingdom": "UK",
    "england": "UK",
    "scotland": "UK",
    "gb": "UK",
    "great britain": "UK",
    "canada": "Canada",
    "germany": "Germany",
    "de": "Germany",
    "deutschland": "Germany",
    "france": "France",
    "fr": "France",
    "netherlands": "Netherlands",
    "nl": "Netherlands",
    "ireland": "Ireland",
    "ie": "Ireland",
    "spain": "Spain",
    "es": "Spain",
    "portugal": "Portugal",
    "pt": "Portugal",
    "italy": "Italy",
    "it": "Italy",
    "poland": "Poland",
    "pl": "Poland",
    "india": "India",
    "australia": "Australia",
    "au": "Australia",
    "new zealand": "New Zealand",
    "nz": "New Zealand",
    "japan": "Japan",
    "jp": "Japan",
    "singapore": "Singapore",
    "sg": "Singapore",
    "israel": "Israel",
    "brazil": "Brazil",
    "br": "Brazil",
    "mexico": "Mexico",
    "mx": "Mexico",
    "philippines": "Philippines",
    "ph": "Philippines",
    # Canadian provinces collapse to the country (no US-state-style
    # abbreviation convention in the display format).
    "ontario": "Canada",
    "british columbia": "Canada",
    "quebec": "Canada",
}
# NOTE deliberate omissions: bare "CA"/"IL"/"IN" always resolve as US states
# (California/Illinois/Indiana) — the US-centric corpus makes the state
# reading overwhelmingly more likely than Canada/Israel/India ISO codes.
# Lowercase ISO codes ("de", "gb") only win when the token is not an
# uppercase two-letter state abbreviation — see the token loop order.

# Bare-city inference — only metros that are unambiguous in this corpus and
# frequent enough to matter. (city → (canonical city, state, country))
_KNOWN_METROS: dict[str, tuple[str, str | None, str]] = {
    "san francisco": ("San Francisco", "CA", "US"),
    "sf": ("San Francisco", "CA", "US"),
    "new york": ("New York", "NY", "US"),
    "new york city": ("New York", "NY", "US"),
    "nyc": ("New York", "NY", "US"),
    "los angeles": ("Los Angeles", "CA", "US"),
    "boston": ("Boston", "MA", "US"),
    "seattle": ("Seattle", "WA", "US"),
    "austin": ("Austin", "TX", "US"),
    "chicago": ("Chicago", "IL", "US"),
    "denver": ("Denver", "CO", "US"),
    "washington": ("Washington", "DC", "US"),
    "washington dc": ("Washington", "DC", "US"),
    "washington, d.c.": ("Washington", "DC", "US"),
    "atlanta": ("Atlanta", "GA", "US"),
    "miami": ("Miami", "FL", "US"),
    "portland": ("Portland", "OR", "US"),
    "san diego": ("San Diego", "CA", "US"),
    "san jose": ("San Jose", "CA", "US"),
    "sunnyvale": ("Sunnyvale", "CA", "US"),
    "palo alto": ("Palo Alto", "CA", "US"),
    "mountain view": ("Mountain View", "CA", "US"),
    "philadelphia": ("Philadelphia", "PA", "US"),
    "phoenix": ("Phoenix", "AZ", "US"),
    "dallas": ("Dallas", "TX", "US"),
    "houston": ("Houston", "TX", "US"),
    "nashville": ("Nashville", "TN", "US"),
    "minneapolis": ("Minneapolis", "MN", "US"),
    "london": ("London", None, "UK"),
    "toronto": ("Toronto", None, "Canada"),
    "vancouver": ("Vancouver", None, "Canada"),
    "berlin": ("Berlin", None, "Germany"),
    "paris": ("Paris", None, "France"),
    "amsterdam": ("Amsterdam", None, "Netherlands"),
    "dublin": ("Dublin", None, "Ireland"),
    "sydney": ("Sydney", None, "Australia"),
    "singapore": ("Singapore", None, "Singapore"),
    "bangalore": ("Bangalore", None, "India"),
    "bengaluru": ("Bengaluru", None, "India"),
    "gurugram": ("Gurugram", None, "India"),
    "tel aviv": ("Tel Aviv", None, "Israel"),
    "tokyo": ("Tokyo", None, "Japan"),
    "manila": ("Manila", None, "Philippines"),
}

# City aliases applied even when the city arrives WITH a state ("New York
# City, NY" → "New York, NY").
_CITY_ALIASES: dict[str, str] = {
    "new york city": "New York",
    "nyc": "New York",
    "sf": "San Francisco",
    "washington dc": "Washington",
    "washington d.c.": "Washington",
}

# Whole-word remote markers. "Remote-first" / "fully remote" reduce to the
# same signal. "Hybrid" is NOT remote — it parses like any other token and
# unrecognized strings fall back to the raw display.
_REMOTE_RE = re.compile(r"\b(?:fully[ -])?remote(?:[ -]first)?\b", re.IGNORECASE)

# "City, ST United States" — a state abbrev glued to a trailing country in one
# comma token (missing comma, seen in prod: "San Mateo, CA United States").
_STATE_THEN_COUNTRY_RE = re.compile(
    r"^([A-Za-z]{2})\s+(united states(?: of america)?|usa|u\.s\.a?\.?|united kingdom|canada)$",
    re.IGNORECASE,
)

# Workday-style "CC-RR-City..." ("US-CA-Menlo Park",
# "IT-FI-FLORENCE-VIA FELICE MATTEUCCI 2"). City stops at the next hyphen —
# the tail is street/site detail.
_WORKDAY_RE = re.compile(r"^([A-Z]{2})-([A-Z]{2})-([^-]+)(?:-.*)?$")

_MULTI_SPLIT_RE = re.compile(r"\s*[;|•]\s*")
# " - " with spaces = human separator ("London - The River Building HQ",
# "Remote - US", "United States - Remote"). Hyphens WITHOUT spaces (Workday,
# "Winston-Salem") are not split.
_DASH_SPLIT_RE = re.compile(r"\s+[-–—]\s+")
_PARENS_RE = re.compile(r"\(([^)]*)\)")
_WS_RE = re.compile(r"\s+")


def _canon(token: str) -> str:
    return _WS_RE.sub(" ", token.strip().strip(",")).strip().lower()


def _title_case(raw: str) -> str:
    """Title-case a city while leaving already-mixed-case names alone
    ("FLORENCE" → "Florence", "McLean" stays "McLean")."""
    stripped = _WS_RE.sub(" ", raw.strip())
    if not stripped:
        return stripped
    if stripped.isupper() or stripped.islower():
        return stripped.title()
    return stripped


def _lookup_country(token: str) -> str | None:
    return _COUNTRIES.get(_canon(token))


def _lookup_state(token: str) -> str | None:
    canon = _canon(token)
    if canon in _US_STATES:
        return _US_STATES[canon]
    bare = token.strip().strip(",.")
    # Two-letter abbreviations must arrive UPPERCASE to count as a state —
    # that's how boards write them ("Austin, TX"), and it keeps lowercase
    # ISO country codes ("gb", "de") and prose fragments from colliding
    # with Georgia/Delaware/Indiana.
    if len(bare) == 2 and bare.isupper() and bare in _US_STATE_ABBREVS:
        return bare
    return None


def _parse_segment(segment: str) -> LocationParts:
    """Parse one location segment (no multi-location separators inside)."""
    remote = False

    # Pull remote markers out first — they can wrap anything.
    if _REMOTE_RE.search(segment):
        remote = True
        segment = _REMOTE_RE.sub(" ", segment)

    # Parenthesized qualifiers: "(Remote)" handled above; "(US)" → merge as
    # a comma token.
    segment = _PARENS_RE.sub(lambda m: f", {m.group(1)}", segment)

    # Workday CC-RR-City ("US-CA-Menlo Park"). The region code is a US
    # state when the country is US; for other countries it's a province
    # code kept verbatim ("IT-FI-FLORENCE-…" → state "FI").
    wd = _WORKDAY_RE.match(segment.strip())
    if wd:
        country = _lookup_country(wd.group(1)) or wd.group(1)
        return LocationParts(_title_case(wd.group(3)), wd.group(2), country, remote)

    # Spaced-dash segments: classify each side; junk suffixes ("… - The
    # River Building HQ", "… - Downtown") are dropped, recognized sides
    # (countries, cities) merge.
    dash_parts = _DASH_SPLIT_RE.split(segment)
    if len(dash_parts) > 1:
        merged = _EMPTY
        for part in dash_parts:
            got = _parse_tokens(part)
            if merged.is_empty:
                merged = got
            else:
                merged = LocationParts(
                    merged.city or got.city,
                    merged.state or got.state,
                    merged.country or got.country,
                    False,
                )
        return LocationParts(merged.city, merged.state, merged.country, remote)

    parts = _parse_tokens(segment)
    return LocationParts(parts.city, parts.state, parts.country, remote)


def _parse_tokens(segment: str) -> LocationParts:
    """Comma-token classification for one dash-free, remote-free segment."""
    city: str | None = None
    state: str | None = None
    country: str | None = None

    cleaned = _WS_RE.sub(" ", segment).strip(" ,")
    if not cleaned:
        return _EMPTY

    # Whole-string metro / alias / country / state lookups first — the bare
    # forms ("San Francisco", "US", "California") dominate prod.
    whole = _canon(cleaned)
    if whole in _KNOWN_METROS:
        c, s, co = _KNOWN_METROS[whole]
        return LocationParts(c, s, co, False)
    whole_country = _lookup_country(cleaned)
    if whole_country:
        return LocationParts(None, None, whole_country, False)
    whole_state = _lookup_state(cleaned)
    if whole_state:
        return LocationParts(None, whole_state, "US", False)

    tokens = [t for t in (p.strip() for p in cleaned.split(",")) if t]
    # A single token that survived every map lookup above is NOT assumed to
    # be a city — "Hybrid", "Asia", "Washington University Medical Campus",
    # "2 Locations" would all masquerade as cities. Comma-anchored strings
    # ("Hawthorne, CA") keep free-city acceptance below because the other
    # token corroborates a real place.
    if len(tokens) < 2:
        return _EMPTY
    for token in tokens:
        # Glued "ST Country" token ("CA United States" — missing comma).
        glued = _STATE_THEN_COUNTRY_RE.match(token)
        if glued and glued.group(1).upper() in _US_STATE_ABBREVS:
            state = state or glued.group(1).upper()
            country = country or _lookup_country(glued.group(2))
            continue
        # Uppercase two-letter abbreviations are ALWAYS states ("Austin, TX")
        # — checked before countries so "DE"/"IN" read as Delaware/Indiana,
        # not Germany/India. Full state NAMES only count once a city is
        # taken: "Washington, DC" is a city, "Seattle, Washington" a state.
        bare = token.strip(",.")
        if state is None and len(bare) == 2 and bare.isupper() and bare in _US_STATE_ABBREVS:
            state = bare
            continue
        if state is None and city is not None and _canon(token) in _US_STATES:
            state = _US_STATES[_canon(token)]
            continue
        tok_country = _lookup_country(token)
        if tok_country and country is None:
            country = tok_country
            continue
        if city is None and not any(ch.isdigit() for ch in token):
            alias = _CITY_ALIASES.get(_canon(token))
            city = alias or _title_case(token)

    # Known-metro enrichment: fill missing state/country from the map, but
    # never contradict explicit tokens ("Vancouver, WA" stays Washington
    # state, not Canada).
    if city:
        metro = _KNOWN_METROS.get(city.lower())
        if metro:
            m_city, m_state, m_country = metro
            city = m_city
            if state is None and (country is None or country == m_country):
                state = m_state
            if country is None and (state is None or state == m_state):
                country = m_country
        city = _CITY_ALIASES.get(city.lower(), city)

    # A US state implies the country.
    if state and country is None:
        country = "US"

    if city is None and state is None and country is None:
        return _EMPTY
    return LocationParts(city, state, country, False)


def parse_location(raw: str | None) -> LocationParts:
    """Parse a raw ATS location string into structured parts.

    Unrecognized strings ("2 Locations", "Hybrid", campus names) return
    all-``None`` parts with ``remote`` still derived — callers fall back to
    displaying the raw string.
    """
    if not raw or not raw.strip():
        return _EMPTY

    segments = [s for s in _MULTI_SPLIT_RE.split(raw.strip()) if s.strip()]
    if not segments:
        return _EMPTY

    primary = _parse_segment(segments[0])
    remote = primary.remote
    city, state, country = primary.city, primary.state, primary.country

    # Enrich from the remaining segments of a multi-location string: any
    # remote marker counts; a pure-country segment fills a missing country.
    for seg in segments[1:]:
        got = _parse_segment(seg)
        remote = remote or got.remote
        if country is None and got.country and got.city is None:
            country = got.country
        # The first segment might be pure-remote ("Remote; New York, NY").
        if city is None and state is None and got.city:
            city, state = got.city, got.state
            country = country or got.country

    return LocationParts(city, state, country, remote)
