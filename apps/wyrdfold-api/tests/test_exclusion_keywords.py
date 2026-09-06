"""#959: the exclusion REASON is recorded, not just the exclusion.

``_title_matches_any_target`` admits negative-keyword rejections into the
catalog specifically so the pipeline can record them "for audit […] a user
asking 'why isn't this in my list?' gets an answer". These tests cover the
half that was missing: storing the answer.

The load-bearing tests here are the negative ones. It is easy to write a
capture that fires on everything and looks green — so each positive case is
paired with a case that must record NOTHING, and the body-vs-title split
(#845) is asserted in both directions.
"""

from app.models.targets import (
    CategoryProfile,
    NegativeProfile,
    ScoringProfile,
    SeniorityProfile,
)
from app.services.scoring import score_job_with_profile, score_title_against_profile
from app.services.target_scoring import _score_row_payload


def _profile(
    *,
    core: dict[str, int] | None = None,
    negative_keywords: list[str] | None = None,
    seniority_level: str | None = None,
) -> ScoringProfile:
    cats: dict[str, CategoryProfile] = {}
    if core is not None:
        cats["core_skills"] = CategoryProfile(keywords=core, weight=2.0)
    return ScoringProfile(
        categories=cats,
        seniority=SeniorityProfile(level=seniority_level, signals=[]),
        negative=NegativeProfile(keywords=negative_keywords or []),
    )


# ---- Stage 1 (title-only) --------------------------------------------------


def test_title_exclusion_records_the_keyword_that_fired():
    profile = _profile(core={"React": 3}, negative_keywords=["junior", "intern"])
    result = score_title_against_profile("Junior React Developer", profile)
    assert result.excluded
    assert result.exclusion_keywords == ["junior"]


def test_title_exclusion_records_every_keyword_that_fired():
    """Two negatives in one title — a single-value field would lose one."""
    profile = _profile(core={"React": 3}, negative_keywords=["junior", "contract"])
    result = score_title_against_profile("Junior Contract React Developer", profile)
    assert result.excluded
    assert sorted(result.exclusion_keywords) == ["contract", "junior"]


def test_title_not_excluded_records_nothing():
    """The guard must stay silent when it does not fire — otherwise every
    'skipped' row would carry a reason and the field would prove nothing."""
    profile = _profile(core={"React": 3}, negative_keywords=["junior"])
    result = score_title_against_profile("Senior React Engineer", profile)
    assert not result.excluded
    assert result.exclusion_keywords == []


def test_title_no_negatives_configured_records_nothing():
    profile = _profile(core={"React": 3})
    result = score_title_against_profile("Junior React Developer", profile)
    assert result.exclusion_keywords == []


# ---- Stage 2 (full JD) -----------------------------------------------------


def test_full_jd_title_exclusion_records_the_keyword():
    profile = _profile(core={"React": 3}, negative_keywords=["intern"])
    result = score_job_with_profile("Intern React Developer", "<p>Build React things.</p>", profile)
    assert result.excluded
    assert result.exclusion_keywords == ["intern"]


def test_full_jd_body_match_is_a_penalty_not_an_exclusion():
    """#845: a negative keyword in the BODY is a soft penalty, never a hard
    exclude. If this ever records a reason, the capture has drifted onto the
    body path and would explain rejections that never happened."""
    profile = _profile(core={"React": 3}, negative_keywords=["intern"])
    result = score_job_with_profile(
        "Staff React Engineer",
        "<p>Requirements: you will mentor our intern cohort.</p>",
        profile,
    )
    assert not result.excluded
    assert result.exclusion_keywords == []


def test_full_jd_body_and_title_records_only_the_title_keyword():
    """Both paths fire on different keywords; only the title one excludes."""
    profile = _profile(core={"React": 3}, negative_keywords=["junior", "intern"])
    result = score_job_with_profile(
        "Junior React Developer",
        "<p>Requirements: work alongside our intern cohort.</p>",
        profile,
    )
    assert result.excluded
    assert result.exclusion_keywords == ["junior"]


# ---- Persistence -----------------------------------------------------------


def _payload(**overrides: object) -> dict:
    from app.models.schemas import ScoreBreakdown

    base = {
        "job_posting_id": "11111111-1111-1111-1111-111111111111",
        "target_id": "22222222-2222-2222-2222-222222222222",
        "score": 0,
        "breakdown": ScoreBreakdown(),
        "matched_keywords": [],
        "excluded": True,
        "exclusion_keywords": ["junior"],
        "scoring_status": "stage1",
    }
    base.update(overrides)
    return _score_row_payload(**base)  # type: ignore[arg-type]


def test_payload_persists_the_keywords():
    assert _payload()["exclusion_keywords"] == ["junior"]


def test_payload_always_writes_the_key_even_when_empty():
    """#928: a bulk upsert writes the UNION of the batch's keys to every row.
    A sometimes-present key would stamp one row's reason onto rows excluded for
    a different reason — or not excluded at all. So the key must be present
    unconditionally, including on the not-excluded rows that share the batch."""
    row = _payload(excluded=False, exclusion_keywords=[])
    assert "exclusion_keywords" in row
    assert row["exclusion_keywords"] == []


def test_payload_key_is_present_on_every_row_of_a_mixed_batch():
    """The #928 failure mode, exercised as a batch rather than asserted about:
    a page mixing excluded and clean rows must have a uniform key set, or the
    union write corrupts the clean rows."""
    batch = [
        _payload(excluded=True, exclusion_keywords=["junior"]),
        _payload(excluded=False, exclusion_keywords=[]),
        _payload(excluded=True, exclusion_keywords=["intern", "contract"]),
    ]
    key_sets = {frozenset(row) for row in batch}
    assert len(key_sets) == 1, "rows disagree on their key set — #928 union hazard"
    assert [row["exclusion_keywords"] for row in batch] == [
        ["junior"],
        [],
        ["intern", "contract"],
    ]


# ---- The exclusion state machine has more than one writer -------------------


def test_known_writers_of_excluded_are_pinned():
    """``exclusion_keywords`` describes ONE cause. That is only safe while we
    know what the other causes are, so this pins the set of places that write
    the ``excluded`` key into a scores payload.

    Review of #1018 caught the original comments claiming title keywords were
    the sole writer of ``excluded``; they are not — the Phase-2 empty-JD drop
    is another. The invariant cannot be proved, so this enforces the next best
    thing: a new writer cannot appear without someone deciding, here, whether
    it should also record a reason.

    A TRIPWIRE, NOT PROOF. It finds writers lexically, by dict entries matching
    ``"excluded":``. A helper that mutates the column through a differently
    shaped payload would evade it, so this must not be cited as exhaustive
    evidence in a later design discussion. The binding contract is the semantic
    one in ``models/schemas.py`` and the column COMMENT; this only makes the
    common case impossible to do by accident.
    """
    import re
    from pathlib import Path

    app = Path(__file__).resolve().parents[1] / "app"
    pattern = re.compile(r"""["']excluded["']\s*:""")
    found = {str(f.relative_to(app)) for f in app.rglob("*.py") if pattern.search(f.read_text())}
    known = {
        # The scoring write path — carries exclusion_keywords alongside.
        "services/target_scoring.py",
        # Phase 2's empty-JD terminal drop. An UPDATE that deliberately does
        # NOT touch exclusion_keywords: the scoring pass's finding ("no title
        # keyword fired") stays true, and this exclusion has its own cause.
        "services/fit/score_persistence.py",
        # The Phase-1 backfill, which ORs its verdict in:
        #   "excluded": bool(was_excluded or not promising)
        # It preserves an existing keyword exclusion and never writes
        # exclusion_keywords, so a recorded finding survives — but it CAN set
        # excluded purely from `not promising`, which is a third way for a row
        # to read excluded=True with an empty keyword list. This guard found
        # this writer; neither the review nor I had named it.
        "services/relevance/phase1_backfill.py",
    }
    assert found == known, (
        f"the set of writers of `excluded` changed: {found ^ known}. "
        "If you added one, decide whether it should record a reason, and "
        "update the semantics comments in models/schemas.py and the "
        "scores.exclusion_keywords column comment to match."
    )


def test_empty_jd_drop_does_not_claim_a_keyword_reason():
    """The Phase-2 empty-JD payload must not write ``exclusion_keywords``.

    If it ever did — or defaulted it to [] — an empty-JD exclusion would assert
    'no title keyword fired' about a pass that never looked at keywords, which
    is the overclaim this field's semantics were narrowed to avoid.
    """
    import inspect

    from app.services.fit import score_persistence

    src = inspect.getsource(score_persistence)
    empty_jd_block = src.split("if not jd_text.strip():", 1)[1].split("return None", 1)[0]
    assert '"excluded": True' in empty_jd_block, "anchor moved — retarget this test"
    assert "exclusion_keywords" not in empty_jd_block


# ---- What exactly lands in the array ---------------------------------------


def test_alias_match_persists_the_configured_keyword_not_the_title_wording():
    """The predicate is ``_keyword_or_alias_in_text``, so a configured keyword
    can fire on one of its aliases. What gets persisted is the CONFIGURED term,
    not the surface form found in the title.

    That is the behaviour we want — it records the user's rule rather than
    scraped wording, which is stable across postings and is what a person would
    recognise as "my negative keyword". Pinned here because #959 will turn this
    array into a human explanation, and a UI author would otherwise reasonably
    assume it contains verbatim matched title text. Raised in review of #1018.
    """
    # Configured canonical term, alias present in the title.
    result = score_title_against_profile(
        "Junior JS Developer", _profile(core={"React": 3}, negative_keywords=["javascript"])
    )
    assert result.excluded
    assert result.exclusion_keywords == ["javascript"], "must record the rule, not the title"

    # The reverse direction: configured alias, canonical present in the title.
    reverse = score_title_against_profile(
        "JavaScript Intern", _profile(core={"React": 3}, negative_keywords=["js"])
    )
    assert reverse.excluded
    assert reverse.exclusion_keywords == ["js"]

    # The SAME contract on the full-JD scorer, which is a separate code path.
    # Covered explicitly because a sabotage check exposed the gap: the other
    # full-JD tests use a keyword that coincides with the title's first word,
    # so recording the title wording instead of the rule would have passed them.
    full_jd = score_job_with_profile(
        "Senior JS Engineer",
        "<p>Build things.</p>",
        _profile(core={"React": 3}, negative_keywords=["javascript"]),
    )
    assert full_jd.excluded
    assert full_jd.exclusion_keywords == ["javascript"]


def test_case_differences_persist_the_configured_casing():
    """Same contract for casing: the match is case-insensitive, the record is
    the configured spelling, so a UI can echo it back as the user typed it."""
    result = score_title_against_profile(
        "JUNIOR Engineer", _profile(core={"React": 3}, negative_keywords=["Junior"])
    )
    assert result.excluded
    assert result.exclusion_keywords == ["Junior"]


# ---- Provenance: whose profile version do the keywords belong to? ----------


def test_payload_stamps_the_profile_version_that_wrote_the_keywords():
    row = _payload(scored_profile_version=3)
    assert row["exclusion_keywords"] == ["junior"]
    assert row["exclusion_keywords_version"] == 3
    assert row["scored_profile_version"] == 3


def test_a_later_writer_advancing_the_version_makes_the_keywords_detectably_stale():
    """The hole this column closes (review of #1018).

    Phase 2's writers set ``scored_profile_version = target.profile_version``
    and deliberately leave ``exclusion_keywords`` alone. Without provenance the
    row then reads as current at the new version while carrying a keyword fact
    from the old one — under which the keyword may no longer be a negative at
    all — and the comment claiming the array records "THIS scoring pass" would
    be false.
    """
    row = _payload(scored_profile_version=3)
    # A Phase-2 write at v4 — the empty-JD drop's payload shape, which touches
    # scored_profile_version but not the keyword array.
    row.update({"excluded": True, "scoring_status": "complete", "scored_profile_version": 4})

    assert row["exclusion_keywords"] == ["junior"], "the historical fact is preserved"
    assert row["exclusion_keywords_version"] == 3, "and still attributed to v3"
    # The reader rule: equal => current, different => historical.
    assert row["exclusion_keywords_version"] != row["scored_profile_version"]


def test_a_rescore_at_the_new_version_realigns_the_pair():
    """The other half: once the SCORING path runs again, the array and its
    version advance together, so the reader rule reports 'current' again."""
    row = _payload(scored_profile_version=4, exclusion_keywords=[])
    assert row["exclusion_keywords_version"] == row["scored_profile_version"] == 4
    assert row["exclusion_keywords"] == []


def test_provenance_key_is_present_on_every_row_of_a_mixed_batch():
    """Same #928 union hazard as the array itself — the two must travel
    together, or a bulk page could carry the array without its provenance."""
    batch = [
        _payload(excluded=True, exclusion_keywords=["junior"], scored_profile_version=2),
        _payload(excluded=False, exclusion_keywords=[], scored_profile_version=2),
    ]
    assert len({frozenset(r) for r in batch}) == 1
    assert all("exclusion_keywords_version" in r for r in batch)
