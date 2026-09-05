"""#836 §7: the ranker's overlap becomes a bucket the UI can group on.

`frontend engineer` returning `DevOps Engineer, Senior` on page one is not a
bug — it matched one of two groups and sorted correctly beneath the strong
matches. But with nothing said, a visitor cannot tell a weak match from a
broken search. These pin the flag that lets the UI say so, including the
cases where saying it would be WRONG (a blank browse, a single-term query).
"""

from __future__ import annotations

import pytest

from app.services.job_search import _groups, _is_strong_match, _tokenize


def _row(title: str) -> dict[str, object]:
    return {"title": title}


@pytest.mark.parametrize(
    ("query", "title", "strong"),
    [
        # Both groups present → strong.
        ("frontend engineer", "Senior Frontend Engineer", True),
        # Only "engineer" → the #836 §7 exhibit; sorted below, now labelled.
        ("frontend engineer", "DevOps Engineer, Senior", False),
        ("frontend engineer", "Software Engineering Manager", False),
        # Synonym-aware, not literal: "developer" canonicalizes with engineer.
        ("frontend developer", "Frontend Engineer, Platform", True),
        # Single-term query: matching it at all IS matching everything.
        ("engineer", "DevOps Engineer, Senior", True),
    ],
)
def test_strength_follows_group_overlap(query: str, title: str, strong: bool) -> None:
    assert _is_strong_match(_groups(_tokenize(query)), _row(title)) is strong


def test_blank_browse_marks_everything_strong() -> None:
    """A blank query BROWSES the pool (#834). There are no terms to have
    missed, so nothing may be demoted — otherwise bare /search would sprout
    a "Related roles" divider over a list the user asked for wholesale."""
    assert _is_strong_match(set(), _row("Anything At All")) is True


def test_missing_title_is_not_strong_and_does_not_raise() -> None:
    assert _is_strong_match({"engineer"}, {}) is False
