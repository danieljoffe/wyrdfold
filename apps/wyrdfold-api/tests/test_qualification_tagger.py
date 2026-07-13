"""Tests for the #60 job qualification firewall.

Two layers, mirroring the service split:

L1 (``heuristics``) — pure Python, asserted directly:
- HTML/entity stripping + whitespace collapse.
- The content hash: stable on unchanged input, changes on any field change,
  collision-safe across field boundaries.
- The permissive US guess on cases it can decide deterministically.

L2 (``tagger``) — the ONE structured LLM call, with the LLM **mocked** (never
the real API) via ``MockLLMClient`` scripted responses and via the
``complete_json`` monkeypatch pattern used by the Phase 1 triage tests:
- The user message embeds title/company/location + the L1 prior + the cleaned
  description.
- The structured schema round-trips through ``tag_job`` into
  ``QualificationTags`` for every hard case from the issue's validated dry-run
  (golden fixture).
- Enum domains are enforced by the schema (a bad ``role_family`` raises).
- Any LLM/parse error fails soft → ``(None, None)`` so the poller leaves the
  row NULL and never breaks.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

import pytest

from app.services.llm.mock import MockLLMClient
from app.services.qualification import (
    QUALIFICATION_PURPOSE,
    QualificationTags,
    clean_description,
    is_us_location,
    positively_us_location,
    qualification_hash,
    tag_job,
)
from app.services.qualification import tagger as tagger_mod

# ---- L1: clean_description -------------------------------------------------


class TestCleanDescription:
    def test_strips_tags_and_decodes_entities(self) -> None:
        out = clean_description("<p>Senior&nbsp;Engineer &amp; <b>Lead</b></p>")
        assert out == "Senior Engineer & Lead"

    def test_decodes_double_escaped_entities(self) -> None:
        # Some ATS feeds double-escape: "&amp;amp;" -> "&amp;" -> "&".
        assert clean_description("Tom &amp;amp; Jerry") == "Tom & Jerry"

    def test_collapses_whitespace(self) -> None:
        assert clean_description("a\n\n  b\t c") == "a b c"

    def test_none_and_empty(self) -> None:
        assert clean_description(None) == ""
        assert clean_description("") == ""


# ---- L1: qualification_hash -----------------------------------------------


class TestQualificationHash:
    def _h(self, **kw: Any) -> str:
        base = {
            "title": "PM",
            "company": "Acme",
            "location": "NYC",
            "description": "<p>hello</p>",
        }
        base.update(kw)
        return qualification_hash(**base)  # type: ignore[arg-type]

    def test_stable_on_unchanged_input(self) -> None:
        assert self._h() == self._h()

    def test_changes_on_any_field(self) -> None:
        base = self._h()
        assert self._h(title="PM II") != base
        assert self._h(company="Other") != base
        assert self._h(location="SF") != base
        assert self._h(description="<p>world</p>") != base

    def test_ignores_cosmetic_html_reencoding(self) -> None:
        # The description is cleaned before hashing, so "&amp;" vs "&" in the
        # raw HTML must not churn the hash (the cleaned text is identical).
        a = self._h(description="Tom &amp; Jerry")
        b = self._h(description="Tom & Jerry")
        assert a == b

    def test_field_boundary_collision_safe(self) -> None:
        # NUL-separated join: ("ab","c") and ("a","bc") must not collide.
        h1 = qualification_hash(title="ab", company="c", location="x", description="d")
        h2 = qualification_hash(title="a", company="bc", location="x", description="d")
        assert h1 != h2

    def test_is_sha256_hex(self) -> None:
        h = self._h()
        assert len(h) == 64
        int(h, 16)  # raises if not hex


# ---- L1: is_us_location (deterministic cases only) -------------------------


class TestIsUsLocationHeuristic:
    """L1 is permissive — it pre-tags only what it can decide from a hint
    list. The harder country-from-city inferences (London->UK,
    multi-location->US) are the LLM's job and are asserted in the L2 golden
    fixture below, not here."""

    @pytest.mark.parametrize(
        ("loc", "expected"),
        [
            (None, True),
            ("", True),
            ("Remote", True),
            ("Mountain View, CA", True),
            ("Remote - United States", True),
            ("San Francisco, CA", True),
            # Non-US cases L1 can decide from the hint list.
            ("Taichung", False),
            ("Remote (Bulgaria)", False),
            ("Calgary", False),
            ("Toronto, Canada", False),
            ("Berlin, Germany", False),
        ],
    )
    def test_cases(self, loc: str | None, expected: bool) -> None:
        assert is_us_location(loc) is expected


class TestPositivelyUsLocation:
    """The strict complement used to VETO archiving a tagger non-US verdict:
    True only on an UNAMBIGUOUS US marker with no foreign hint (#60 B)."""

    @pytest.mark.parametrize(
        ("loc", "expected"),
        [
            # Unambiguous US → veto the archive (protects tagger false-negatives).
            ("New York, NY, United States", True),  # the real conf-95 FN observed
            ("Austin, TX", True),
            ("USA", True),
            ("Remote (USA)", True),
            # Permissive-US but NOT positively US → do not veto (archive proceeds).
            (None, False),
            ("", False),
            ("Remote", False),
            # State-abbrev COLLISIONS that carry a foreign hint → not vetoed, so
            # these genuinely-non-US rows still archive.
            ("Munich, DE", False),  # DE=Delaware, but 'munich' is a non-US hint
            ("Bangalore, IN", False),  # IN=Indiana, but 'bangalore' is a hint
            ("Toronto, ON, CA", False),  # CA=California, but 'toronto' is a hint
            # Lower-case country code, no marker, no hint → not positively US.
            ("Jakarta, id", False),
        ],
    )
    def test_cases(self, loc: str | None, expected: bool) -> None:
        assert positively_us_location(loc) is expected


# ---- L2: prompt construction ----------------------------------------------


class TestUserMessage:
    def test_embeds_fields_and_l1_prior(self) -> None:
        msg = tagger_mod._build_user_message(
            title="Product Manager, EMEA",
            company="Globex",
            location="London, gb",
            description="Lead the EMEA product line.",
        )
        assert "Product Manager, EMEA" in msg
        assert "Globex" in msg
        assert "London, gb" in msg
        assert "Lead the EMEA product line." in msg
        # L1 prior is present (London isn't in the hint list, so L1 guesses
        # US=True here; the prompt tells the model to override — see golden).
        assert "Heuristic US guess" in msg

    def test_handles_missing_company_and_location(self) -> None:
        msg = tagger_mod._build_user_message(
            title="Engineer", company=None, location=None, description=""
        )
        assert "(unknown)" in msg
        assert "(unstated)" in msg


# ---- L2: input trim (cost-control regression, #60 overspend) ----------------


def _capture_user_message(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch ``complete_json`` to capture the user message ``tag_job`` builds,
    returning a recorder. The LLM is never called for real."""
    captured: dict[str, Any] = {}

    async def fake_complete_json(*_a: object, **kwargs: Any) -> object:
        messages = kwargs["messages"]
        captured["user_message"] = messages[0].content
        return QualificationTags(**_GOLDEN_CASES[0]["verdict"]), object()

    monkeypatch.setattr(tagger_mod, "complete_json", fake_complete_json)
    return captured


class TestInputTrim:
    """The tagger sends only a SHORT JD snippet, not the full body. Sending
    the full ~6000-char description burned ~3.4K input tokens/call against the
    backlog and drove the June overspend. These pin the trim so a regression
    that re-sends the whole JD fails CI."""

    @pytest.mark.asyncio
    async def test_long_jd_truncated_to_cap_and_keeps_header(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cap = 600
        # A long body whose tail carries a unique marker that must NOT be sent.
        head = "Lead the platform team. " * 30  # ~720 chars, > cap
        tail_marker = "ZZZ_VENDOR_FOOTER_BOILERPLATE_ZZZ"
        body = head + tail_marker
        assert len(body) > cap

        captured = _capture_user_message(monkeypatch)
        await tag_job(
            MockLLMClient(),
            title="Staff Engineer",
            company="Globex",
            location="Remote - United States",
            description=body,
            description_chars=cap,
        )

        msg = captured["user_message"]
        # Header fields are still present.
        assert "Staff Engineer" in msg
        assert "Globex" in msg
        assert "Remote - United States" in msg
        assert "Heuristic US guess" in msg
        # The verbose tail past the cap is NOT sent.
        assert tail_marker not in msg
        # The whole message stays small: header lines + at most `cap`
        # description chars + the fixed prompt-injection fence overhead
        # (~160 chars of <untrusted_*> tags). The point is to catch a
        # regression that re-sends the whole ~6000-char body, which would
        # blow past this by an order of magnitude.
        assert len(msg) <= cap + 400
        # And the leading slice of the body IS present (we truncate, not drop).
        assert "Lead the platform team." in msg
        # Defense-in-depth is wired in: the scraped description is fenced.
        assert "<untrusted_description>" in msg

    @pytest.mark.asyncio
    async def test_default_cap_comes_from_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With no explicit ``description_chars``, the cap is
        ``settings.qualification_jd_snippet_chars`` — the single config knob."""
        from app.config import settings as live_settings

        monkeypatch.setattr(live_settings, "qualification_jd_snippet_chars", 40)
        tail_marker = "TAIL_PAST_FORTY_CHARS_MUST_BE_DROPPED"
        body = "A" * 40 + tail_marker

        captured = _capture_user_message(monkeypatch)
        await tag_job(
            MockLLMClient(),
            title="t",
            company="c",
            location="z",
            description=body,
        )

        msg = captured["user_message"]
        assert tail_marker not in msg
        # Exactly 40 'A's of the body made it in (the cap), no more.
        assert "A" * 40 in msg
        assert "A" * 41 not in msg

    @pytest.mark.asyncio
    async def test_short_jd_passes_through_untruncated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A body under the cap is sent in full — the trim only bounds the
        long tail, it doesn't degrade short postings."""
        body = "Small but complete JD: US-based, senior, full-time."
        captured = _capture_user_message(monkeypatch)
        await tag_job(
            MockLLMClient(),
            title="t",
            company="c",
            location="z",
            description=body,
            description_chars=600,
        )
        assert body in captured["user_message"]

    @pytest.mark.asyncio
    async def test_zero_cap_sends_no_description_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``description_chars=0`` sends title/company/location only — the
        most aggressive cost setting still produces a valid prompt."""
        captured = _capture_user_message(monkeypatch)
        await tag_job(
            MockLLMClient(),
            title="Engineer",
            company="Acme",
            location="NYC",
            description="<p>This entire body must be dropped.</p>",
            description_chars=0,
        )
        msg = captured["user_message"]
        assert "Engineer" in msg
        assert "This entire body must be dropped" not in msg
        # Empty description renders the placeholder, not raw HTML.
        assert "(no description provided)" in msg


# ---- L2: golden fixture — schema mapping for the issue's hard cases --------

# Each case: the documented verdict from the validated dry-run (#60). We script
# the mock LLM to return it, call ``tag_job``, and assert the structured schema
# round-trips. The LLM is NEVER called for real.
_GOLDEN_CASES: list[dict[str, Any]] = [
    {
        "name": "PM EMEA / London",
        "title": "Product Manager, EMEA",
        "location": "London, gb",
        "verdict": {
            "is_us": False,
            "us_confidence": 95,
            "role_family": "product",
            "seniority": "ic",
            "employment_type": "full_time",
            "metro": "London",
            "is_remote": False,
            "is_genuine_role": True,
        },
        "expect": {"is_us": False, "role_family": "product", "seniority": "ic"},
    },
    {
        "name": "Legal Engineer Manager / London",
        "title": "Legal Engineer Manager, Product Specialist, EMEA",
        "location": "London",
        "verdict": {
            "is_us": False,
            "us_confidence": 90,
            "role_family": "legal",
            "seniority": "manager",
            "employment_type": "full_time",
            "metro": "London",
            "is_remote": False,
            "is_genuine_role": True,
        },
        "expect": {
            "is_us": False,
            "role_family": "legal",
            "seniority": "manager",
        },
    },
    {
        "name": "Director Customer Success / Mountain View",
        "title": "Director, Customer Success",
        "location": "Mountain View, CA",
        "verdict": {
            "is_us": True,
            "us_confidence": 100,
            "role_family": "customer_experience",
            "seniority": "director",
            "employment_type": "full_time",
            "metro": "Mountain View",
            "is_remote": False,
            "is_genuine_role": True,
        },
        "expect": {
            "is_us": True,
            "role_family": "customer_experience",
            "seniority": "director",
        },
    },
    {
        "name": "Senior Automation Design Specialist / Taichung",
        "title": "Senior Automation Design Specialist",
        "location": "Taichung",
        "verdict": {
            "is_us": False,
            "us_confidence": 97,
            "role_family": "engineering",
            "seniority": "senior_ic",
            "employment_type": "full_time",
            "metro": "Taichung",
            "is_remote": False,
            "is_genuine_role": True,
        },
        "expect": {"is_us": False},
    },
    {
        "name": "People Operations Analyst (Contract) / Remote US",
        "title": "People Operations Analyst (Contract)",
        "location": "Remote - United States",
        "verdict": {
            "is_us": True,
            "us_confidence": 98,
            "role_family": "people_hr",
            "seniority": "ic",
            "employment_type": "contract",
            "metro": None,
            "is_remote": True,
            "is_genuine_role": True,
        },
        "expect": {"is_us": True, "employment_type": "contract"},
    },
    {
        "name": "Junior Accountant Intern / London",
        "title": "Junior Accountant Intern",
        "location": "London",
        "verdict": {
            "is_us": False,
            "us_confidence": 92,
            "role_family": "finance",
            "seniority": "intern",
            "employment_type": "internship",
            "metro": "London",
            "is_remote": False,
            "is_genuine_role": True,
        },
        "expect": {
            "is_us": False,
            "role_family": "finance",
            "seniority": "intern",
            "employment_type": "internship",
        },
    },
    {
        "name": "Multi-location incl US",
        "title": "Staff Software Engineer",
        "location": (
            "Bellevue, Washington; Chicago, Illinois; New York; "
            "San Francisco; Toronto, Ontario, Canada"
        ),
        "verdict": {
            "is_us": True,
            "us_confidence": 96,
            "role_family": "engineering",
            "seniority": "senior_ic",
            "employment_type": "full_time",
            "metro": None,
            "is_remote": False,
            "is_genuine_role": True,
        },
        "expect": {"is_us": True},
    },
]


class TestGoldenSchemaMapping:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", _GOLDEN_CASES, ids=lambda c: c["name"])
    async def test_tag_job_maps_documented_verdict(self, case: dict[str, Any]) -> None:
        # MockLLMClient.complete_tool_use parses the scripted text as JSON and
        # returns it as the tool-input dict — exactly the shape the real client
        # produces server-side. complete_json then validates it into the schema.
        llm = MockLLMClient(scripted={QUALIFICATION_PURPOSE: json.dumps(case["verdict"])})

        tags, result = await tag_job(
            llm,
            title=case["title"],
            company="ACME",
            location=case["location"],
            description="<p>A real job posting body.</p>",
        )

        assert tags is not None, f"{case['name']}: tagger returned None"
        assert result is not None  # cost result present on success
        for field, expected in case["expect"].items():
            assert getattr(tags, field) == expected, (
                f"{case['name']}: {field} expected {expected!r}, got {getattr(tags, field)!r}"
            )

    @pytest.mark.asyncio
    async def test_full_schema_roundtrip(self) -> None:
        """Every column maps, including the nullable metro."""
        verdict = _GOLDEN_CASES[4]["verdict"]  # People Ops: metro=None, contract
        llm = MockLLMClient(scripted={QUALIFICATION_PURPOSE: json.dumps(verdict)})
        tags, _ = await tag_job(
            llm,
            title="x",
            company="y",
            location="Remote - United States",
            description="desc",
        )
        assert tags == QualificationTags(**verdict)
        assert tags is not None
        assert tags.metro is None  # nullable column round-trips as None


# ---- L2: schema enforcement + fail-soft -----------------------------------


class TestSchemaAndFailSoft:
    @pytest.mark.asyncio
    async def test_unknown_bool_degrades_not_drops_the_tag(self) -> None:
        """The exact prod bug (#60/#193): the tagger returned
        ``is_genuine_role='unknown'`` (a string for a bool) → the WHOLE payload
        was rejected + the job left untagged. Now that one field degrades to
        None and the rest of the classification is still written."""
        bad = dict(_GOLDEN_CASES[0]["verdict"])
        bad["is_genuine_role"] = "unknown"
        llm = MockLLMClient(scripted={QUALIFICATION_PURPOSE: json.dumps(bad)})
        tags, result = await tag_job(llm, title="x", company="y", location="z", description="d")
        assert tags is not None  # no longer dropped
        assert tags.is_genuine_role is None  # the offending field degraded
        assert tags.is_us == _GOLDEN_CASES[0]["verdict"]["is_us"]  # the rest kept
        assert result is not None

    @pytest.mark.asyncio
    async def test_bad_enum_degrades_to_catch_all(self) -> None:
        """A role_family outside the enum no longer drops the whole tag — it
        degrades to 'other', a value the DB role_family CHECK accepts, so the
        rest of the classification is still written (was: fail-soft to None)."""
        bad = dict(_GOLDEN_CASES[0]["verdict"])
        bad["role_family"] = "wizardry"  # not in the enum
        llm = MockLLMClient(scripted={QUALIFICATION_PURPOSE: json.dumps(bad)})
        tags, result = await tag_job(llm, title="x", company="y", location="z", description="d")
        assert tags is not None
        assert tags.role_family == "other"  # catch-all, tag preserved
        assert result is not None

    @pytest.mark.asyncio
    async def test_out_of_range_confidence_is_clamped(self) -> None:
        """An out-of-range us_confidence is clamped into [0,100] (the DB range
        CHECK) instead of dropping the tag."""
        bad = dict(_GOLDEN_CASES[0]["verdict"])
        bad["us_confidence"] = 250  # > 100
        llm = MockLLMClient(scripted={QUALIFICATION_PURPOSE: json.dumps(bad)})
        tags, _ = await tag_job(llm, title="x", company="y", location="z", description="d")
        assert tags is not None
        assert tags.us_confidence == 100

    @pytest.mark.asyncio
    async def test_llm_error_fails_soft(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def boom(*_a: object, **_k: object) -> object:
            raise RuntimeError("anthropic 503")

        monkeypatch.setattr(tagger_mod, "complete_json", boom)
        tags, result = await tag_job(
            MockLLMClient(),
            title="x",
            company="y",
            location="z",
            description="d",
        )
        assert tags is None
        assert result is None

    @pytest.mark.asyncio
    async def test_calls_haiku_with_qualification_purpose(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The tagger must use the pinned Haiku model + qualification purpose
        (so cost-logging groups correctly and the prompt-regression contract
        holds)."""
        captured: dict[str, object] = {}

        async def fake_complete_json(*_a: object, **kwargs: object) -> object:
            captured.update(kwargs)
            return QualificationTags(**_GOLDEN_CASES[0]["verdict"]), object()

        monkeypatch.setattr(tagger_mod, "complete_json", fake_complete_json)
        await tag_job(
            MockLLMClient(),
            title="x",
            company="y",
            location="z",
            description="d",
        )
        assert captured["model"] == "claude-haiku-4-5"
        assert captured["purpose"] == QUALIFICATION_PURPOSE
        assert captured["cache_system"] is True


class TestMalformedFieldTolerance:
    """#60/#193: a single malformed LLM field must DEGRADE to a safe value, not
    fail the whole classification. The prod bug this fixes: the tagger returned
    ``is_genuine_role='unknown'`` (a string for a bool) → the ENTIRE payload was
    rejected + the job left untagged. Now the offending field degrades (None for
    bools/confidence, the enum's catch-all otherwise) and the rest is kept."""

    _VALID: ClassVar[dict[str, object]] = {
        "is_us": True,
        "us_confidence": 90,
        "role_family": "engineering",
        "seniority": "senior_ic",
        "employment_type": "full_time",
        "metro": "New York",
        "is_remote": False,
        "is_genuine_role": True,
    }

    def test_unknown_bool_degrades_to_none_and_keeps_the_rest(self) -> None:
        # The exact prod regression.
        tags = QualificationTags.model_validate({**self._VALID, "is_genuine_role": "unknown"})
        assert tags.is_genuine_role is None  # degraded, not crashed
        assert tags.is_us is True  # the rest survives
        assert tags.role_family == "engineering"

    def test_out_of_enum_role_family_falls_back_to_other(self) -> None:
        tags = QualificationTags.model_validate({**self._VALID, "role_family": "misc"})
        assert tags.role_family == "other"

    def test_out_of_enum_seniority_and_employment_fall_back_to_unknown(self) -> None:
        tags = QualificationTags.model_validate(
            {**self._VALID, "seniority": "lead", "employment_type": "gig"}
        )
        assert tags.seniority == "unknown"
        assert tags.employment_type == "unknown"

    def test_bad_confidence_is_clamped_or_nulled(self) -> None:
        assert (
            QualificationTags.model_validate({**self._VALID, "us_confidence": 150}).us_confidence
            == 100
        )
        assert (
            QualificationTags.model_validate({**self._VALID, "us_confidence": "high"}).us_confidence
            is None
        )

    def test_bool_string_forms_are_coerced(self) -> None:
        tags = QualificationTags.model_validate({**self._VALID, "is_us": "true", "is_remote": "no"})
        assert tags.is_us is True
        assert tags.is_remote is False

    def test_valid_payload_passes_through_unchanged(self) -> None:
        tags = QualificationTags.model_validate(self._VALID)
        assert tags.is_us is True
        assert tags.role_family == "engineering"
        assert tags.is_genuine_role is True
        assert tags.us_confidence == 90

    def test_missing_fields_default_safely(self) -> None:
        tags = QualificationTags.model_validate({"is_us": True})
        assert tags.role_family == "other"  # missing enum → catch-all
        assert tags.seniority == "unknown"
        assert tags.is_genuine_role is None  # missing bool → None
        assert tags.us_confidence is None


# ---- runtime model resolution (QUALIFICATION_MODEL env flip, task #31) ------


class TestModelResolution:
    """The runtime model comes from ``settings.qualification_model`` — resolved
    at call time so the QUALIFICATION_MODEL env flip (the deepseek cost swap)
    takes effect without re-import — while the module constant stays the
    documented default pinned by the prompt-regression golden contract."""

    def _capture_model(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        captured: dict[str, Any] = {}

        async def fake_complete_json(*_a: object, **kwargs: Any) -> object:
            captured["model"] = kwargs["model"]
            return QualificationTags(**_GOLDEN_CASES[0]["verdict"]), object()

        monkeypatch.setattr(tagger_mod, "complete_json", fake_complete_json)
        return captured

    @pytest.mark.asyncio
    async def test_defaults_to_configured_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = self._capture_model(monkeypatch)
        await tag_job(
            MockLLMClient(), title="T", company=None, location=None, description=None
        )
        assert captured["model"] == tagger_mod.QUALIFICATION_MODEL

    @pytest.mark.asyncio
    async def test_env_flip_takes_effect_without_reimport(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.config import settings

        monkeypatch.setattr(settings, "qualification_model", "deepseek-v3-2")
        captured = self._capture_model(monkeypatch)
        await tag_job(
            MockLLMClient(), title="T", company=None, location=None, description=None
        )
        assert captured["model"] == "deepseek-v3-2"

    @pytest.mark.asyncio
    async def test_explicit_model_wins_over_settings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.config import settings

        monkeypatch.setattr(settings, "qualification_model", "deepseek-v3-2")
        captured = self._capture_model(monkeypatch)
        await tag_job(
            MockLLMClient(),
            title="T",
            company=None,
            location=None,
            description=None,
            model="claude-haiku-4-5",
        )
        assert captured["model"] == "claude-haiku-4-5"

    def test_setting_default_matches_golden_constant(self) -> None:
        # The prompt-regression contract pins the CONSTANT; this pins the
        # SETTING default to it, so neither can drift silently.
        from app.config import Settings

        assert (
            Settings.model_fields["qualification_model"].default
            == tagger_mod.QUALIFICATION_MODEL
        )
