"""Display titles for job postings (ux-sweep 2026-08-12 §B1 follow-through).

Boards deliver titles with ingest junk — normalizer artifacts
("Project Engineer _field_ Application Engineering"), trailing req codes,
all-lowercased or SHOUTED text — and the FE has been repairing a subset of it
per-surface (``insights/smartTitleCase.ts``). This module is the single
server-side cleaner behind ``jobs.title_display``.

The invariant that shapes everything here: ``jobs.title`` (and
``company_name``) feed the poller's content-dedupe key
(``poller._content_dedupe_key``), so STORED titles are never rewritten —
cleaning happens into a separate additive column, and only when it actually
changes something (``None`` means "raw is already fine; serve it as-is").

Deliberately deterministic and conservative — no LLM, and recasing only fires
on titles with no case signal at all (all-lower / all-upper). A mixed-case
title is evidence the board cased it on purpose ("Make IT Work" must not
become "Make It Work"), matching the FE caser's false-positive stance.
"""

from __future__ import annotations

import re

# Tokens that are (near-)always acronyms in a job title. Kept in sync with
# the FE's ``smartTitleCase.ts`` set (which remains for the near-miss chips —
# those render ``phase1_rejections.title_norm``, a different store).
_ACRONYMS = frozenset(
    {
        "ai",
        "api",
        "ar",
        "asic",
        "aws",
        "b2b",
        "b2c",
        "cad",
        "cd",
        "ci",
        "cnc",
        "cpu",
        "crm",
        "css",
        "d2c",
        "erp",
        "etl",
        "fpga",
        "gcp",
        "gpu",
        "gtm",
        "hr",
        "html",
        "hvac",
        "iot",
        "it",
        "llm",
        "ml",
        "nlp",
        "php",
        "plc",
        "qa",
        "rf",
        "sap",
        "sdet",
        "sdk",
        "sql",
        "sre",
        "ui",
        "ux",
        "vr",
    }
)

# Mixed-case brand/term spellings that neither capitalize nor uppercase.
_SPECIAL = {
    "devops": "DevOps",
    "iaas": "IaaS",
    "ios": "iOS",
    "javascript": "JavaScript",
    "github": "GitHub",
    "macos": "macOS",
    "mysql": "MySQL",
    "nosql": "NoSQL",
    "paas": "PaaS",
    "postgresql": "PostgreSQL",
    "saas": "SaaS",
    "typescript": "TypeScript",
}

# ii/iii/iv/vi…ix as level suffixes ("Engineer III"). Single i/v/x are
# handled fine by plain capitalization.
_ROMAN = re.compile(r"^(?:i{2,3}|iv|vi{1,3}|ix)$")

# A word-run keeps +, # and apostrophes ("c++", "c#", "master's"); &, / and -
# split runs so "cd&ai" and "c/c++" case per part.
_WORD_RUN = re.compile(r"[a-z0-9][a-z0-9+#'’]*", re.IGNORECASE)

# Trailing requisition/job codes. Conservative on purpose: a trailing
# parenthesized/bracketed token that is an UPPERCASE-prefixed number
# ("(REQ 20441)", "[R-12345]") or bare digits of 5+ ("(20441)"), or a
# dash-separated REQ/JR/R-prefixed number. Plain trailing years survive —
# and so do parenthesized TERMS: the first prod dry-run flagged
# "(Fall 2026)" because a mixed-case prefix + a 4-digit year fit the old
# pattern; seasons/months are content, req codes are not. Uppercase-only
# prefixes and the 5-digit floor for bare numbers encode that split.
_REQ_CODE_PAREN = re.compile(r"\s*[\(\[](?:[A-Z]{1,4}[-#\s]?\d{4,}|\d{5,})[\)\]]\s*$")
_REQ_CODE_DASH = re.compile(r"\s*[-–—]\s*(?:REQ|JR|R)[-#\s]?\d{4,}\s*$", re.IGNORECASE)

# Leading/trailing separator junk left behind by feeds and the strips above.
_EDGE_SEPARATORS = re.compile(r"^[\s\-–—|:;,/]+|[\s\-–—|:;,/]+$")


def _transform_word(match: re.Match[str]) -> str:
    lower = match.group(0).lower()
    special = _SPECIAL.get(lower)
    if special:
        return special
    if lower in _ACRONYMS or _ROMAN.match(lower):
        return lower.upper()
    return lower[:1].upper() + lower[1:]


def _recase(title: str) -> str:
    return _WORD_RUN.sub(_transform_word, title)


def clean_title_display(raw: str | None) -> str | None:
    """The display form of a posting title, or ``None`` when raw is fine.

    ``None`` (not an echo of raw) keeps the column an explicit "this needed
    repair" signal and lets serving fall back with ``coalesce``/``??``.
    """
    if raw is None:
        return None

    cleaned = raw.replace("_", " ")
    cleaned = " ".join(cleaned.split())
    cleaned = _REQ_CODE_PAREN.sub("", cleaned)
    cleaned = _REQ_CODE_DASH.sub("", cleaned)
    cleaned = _EDGE_SEPARATORS.sub("", cleaned)
    cleaned = " ".join(cleaned.split())

    if cleaned:
        has_upper = any(c.isupper() for c in cleaned)
        has_lower = any(c.islower() for c in cleaned)
        # No case signal at all → reconstruct. Mixed case is deliberate.
        if not has_upper or not has_lower:
            cleaned = _recase(cleaned)

    if not cleaned or cleaned == raw:
        return None
    return cleaned
