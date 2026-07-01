"""Shared scoring-reasoning discipline (#47 H5).

Two parallel scorers emit a 0-100 score + a short reasoning string: the job
grader (``fit/job_fit.py``, job-vs-user-vs-target) and the user-vs-target fit
scorer (``targets/fit_score.py``, "how your master doc stacks against this
role"). For the two numbers to stay honest and comparable, both must enforce
the SAME evidence-first reasoning contract — chiefly a ban on confidence words
that assert quality without a fact behind them.

The grader already had that discipline; the fit scorer did not, and its
few-shot example literally opened with a banned word ("Strong React/TypeScript
foundation…"), few-shot-priming the exact vague, inflated output the grader was
built to suppress. This module is the single source of truth for the banned
list so the two prompts can't silently drift apart.
"""

from __future__ import annotations

# Confidence words banned from scoring reasoning — each claims strength without
# a fact behind it. Kept in the grader's original order/spelling: the grader's
# prompt lists exactly these (a test pins that it still does, so this stays in
# lockstep with it), and the fit scorer renders this list into its own rules.
BANNED_CONFIDENCE_WORDS: tuple[str, ...] = (
    "strong",
    "great",
    "well",
    "alignment",
    "synergy",
    "cultural fit",
)


def rendered_banned_words() -> str:
    """The banned list formatted as it appears in a prompt:
    ``"strong", "great", "well", "alignment", "synergy", "cultural fit"``."""
    return ", ".join(f'"{w}"' for w in BANNED_CONFIDENCE_WORDS)
