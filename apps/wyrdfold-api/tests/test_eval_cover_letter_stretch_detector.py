"""The stretch eval's ``invented`` metric, exercised without spending money.

The eval itself is spend-bearing (it calls a model), but its detector is pure —
so the part that can silently rot is guarded here for free.

Why this file exists: the detector originally substring-matched design-tool
names, which scored a letter *naming its own gap* ("my artifacts were code
rather than Figma files") as having *invented* Figma. That inverts the metric —
it rewards a letter that stays silent about the gap over an honest one, and it
failed the real gate on honest output. The HONEST_* fixtures below are verbatim
sentences from real model runs that the old detector flagged.
"""

from __future__ import annotations

from scripts.eval_cover_letter_stretch import _PAYLOAD, _invented_skills, _refuses

# Verbatim from captured stretch/control letters — every one of these names the
# gap out loud, which is the behaviour the prompt is supposed to produce.
HONEST_SENTENCES = [
    (
        "The OptimizedPayload I was given describes a full-stack engineer and "
        "founder, with no design portfolio, no Figma experience, and no UX "
        "research background."
    ),
    (
        "A Senior UX Designer role requiring 6+ years of product design "
        "experience, a portfolio of high-fidelity UI work, and hands-on Figma "
        "fluency is not a match I can credibly claim."
    ),
    (
        "Defining component APIs, writing documentation, and thinking about how a "
        "shared system constrains and enables downstream work is directly adjacent "
        "to contributing to a design system, even if my artifacts were code rather "
        "than Figma files."
    ),
    "The tooling gap is Figma; the systems thinking is not a gap.",
    (
        "I recognize this is a reach application and that a portfolio of Figma "
        "work is the standard bar for this role."
    ),
]

# Genuine fabrication — the payload is a full-stack engineer with none of this.
FABRICATED_SENTENCES = [
    "I have six years of Figma experience leading design systems at scale.",
    "My Figma prototypes shipped to 5,500 enterprise clients.",
    "Proficient in Sketch and InVision, I delivered 40 high-fidelity mockups.",
    "I conducted user research across twelve enterprise accounts.",
    "I ran usability testing sessions every sprint for three years.",
    "My design portfolio spans fintech, healthcare, and security products.",
    "I wireframed the entire onboarding funnel in Figma.",
]


def test_honest_gap_disclosure_is_not_invention() -> None:
    """Naming a missing skill must never score as claiming it."""
    for sentence in HONEST_SENTENCES:
        assert _invented_skills(sentence, _PAYLOAD) == [], sentence


def test_fabricated_possession_is_detected() -> None:
    """Without this the metric asserts nothing — it would pass on any letter."""
    for sentence in FABRICATED_SENTENCES:
        assert _invented_skills(sentence, _PAYLOAD), sentence


def test_honest_and_fabricated_in_one_letter_still_flags() -> None:
    """A letter that concedes the gap in one paragraph and fabricates in the
    next must still be caught — the concession must not launder the claim."""
    letter = HONEST_SENTENCES[2] + " " + FABRICATED_SENTENCES[0]
    assert _invented_skills(letter, _PAYLOAD) == ["Figma"]


def test_skills_present_in_the_payload_are_never_invented() -> None:
    """React/WCAG are genuinely in the payload; claiming them is not invention."""
    assert _invented_skills(
        "I built a React component library and resolved 200 WCAG violations.",
        _PAYLOAD,
    ) == []


def test_refusal_detector_separates_decline_from_honest_framing() -> None:
    """The two must not be conflated: conceding a gap is the desired behaviour,
    declining to write the letter is the bug this PR fixes."""
    assert _refuses("I am flagging this mismatch rather than generating a "
                    "misleading letter.")
    assert _refuses("This role is not a fit for my background.")
    assert not _refuses(
        "I want to be direct: this role is a reach from my background, and I "
        "am applying because the overlap is worth a conversation."
    )
