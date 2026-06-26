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
from typing import Any

import pytest

from app.services.llm.mock import MockLLMClient
from app.services.qualification import (
    QUALIFICATION_PURPOSE,
    QualificationTags,
    clean_description,
    is_us_location,
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
        # description chars. (Header is well under 200 chars.)
        assert len(msg) <= cap + 200
        # And the leading slice of the body IS present (we truncate, not drop).
        assert "Lead the platform team." in msg

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
    async def test_bad_enum_is_rejected_by_schema(self) -> None:
        """A role_family outside the enum must not silently pass — the schema
        validation in complete_json raises, and tag_job fails soft to
        (None, None) so a malformed model response leaves the row NULL rather
        than writing garbage that the DB CHECK would then reject."""
        bad = dict(_GOLDEN_CASES[0]["verdict"])
        bad["role_family"] = "wizardry"  # not in the enum
        llm = MockLLMClient(scripted={QUALIFICATION_PURPOSE: json.dumps(bad)})
        tags, result = await tag_job(llm, title="x", company="y", location="z", description="d")
        assert tags is None
        assert result is None

    @pytest.mark.asyncio
    async def test_us_confidence_out_of_range_rejected(self) -> None:
        bad = dict(_GOLDEN_CASES[0]["verdict"])
        bad["us_confidence"] = 250  # > 100
        llm = MockLLMClient(scripted={QUALIFICATION_PURPOSE: json.dumps(bad)})
        tags, _ = await tag_job(llm, title="x", company="y", location="z", description="d")
        assert tags is None

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
