"""#470: domain enrichment — candidate generation, probe ordering, writes.

The probe fake answers per-domain so ordering assertions are real; the
supabase fake records the update chain so the never-clobber guard is
assertable (and its absence fails — see the guard test).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

import app.services.company_domain as cd
from app.services.company_domain import (
    candidate_domains,
    enrich_missing_source_domains,
    homepage_is_usable,
    page_title,
    resolve_company_domain,
)

pytestmark = pytest.mark.asyncio


# ---- candidate generation (pure) -------------------------------------------


@pytest.mark.parametrize(
    ("name", "slug", "expected"),
    [
        # Name and slug agree after normalization → one stem, two TLDs.
        ("dbt Labs", "dbtlabs", ["dbtlabs.com", "dbtlabs.io"]),
        # Slug carries ATS digit noise → stripped; name stem leads.
        ("Datadog", "datadog81", ["datadog.com", "datadog.io"]),
        # Distinct stems → name-derived first (what a human would type).
        (
            "Storybook / Chromatic",
            "chromatic",
            [
                "storybookchromatic.com",
                "storybookchromatic.io",
                "chromatic.com",
                "chromatic.io",
            ],
        ),
        # The pooled manual pseudo-source gets NO candidates — any domain
        # stored there would be wrong for most of its jobs.
        ("Manually Added", "manual", []),
        # Too-short stems are squatter bait, not candidates.
        ("X", "x1", []),
    ],
)
def test_candidate_domains(name: str, slug: str, expected: list[str]) -> None:
    assert candidate_domains(name, slug) == expected


# ---- probe ordering ---------------------------------------------------------


class _FakeHttp:
    """Answers only the domains it's told to; records probe order."""

    def __init__(self, alive: set[str], status: int = 200, title: str = "Acme") -> None:
        self._alive = alive
        self._status = status
        self._title = title
        self.probed: list[str] = []

    async def get(self, url: str, follow_redirects: bool = True) -> Any:
        domain = url.removeprefix("https://")
        self.probed.append(domain)
        if domain not in self._alive:
            raise ConnectionError(domain)
        # Real responses carry a body; the resolver reads <title> from it.
        return MagicMock(status_code=self._status, text=f"<title>{self._title}</title>")


async def test_first_answering_candidate_wins_and_stops_probing() -> None:
    http = _FakeHttp(alive={"datadog.com", "datadog.io"})
    got = await resolve_company_domain("Datadog", "datadog81", client=http)  # type: ignore[arg-type]
    assert got == "datadog.com"
    assert http.probed == ["datadog.com"]  # first hit ends the cascade


async def test_all_misses_resolve_to_none() -> None:
    http = _FakeHttp(alive=set())
    assert await resolve_company_domain("Datadog", "datadog81", client=http) is None  # type: ignore[arg-type]
    assert http.probed == ["datadog.com", "datadog.io"]


async def test_bot_blocked_4xx_still_answers_but_5xx_does_not() -> None:
    bot_blocked = _FakeHttp(alive={"datadog.com"}, status=403)
    assert (
        await resolve_company_domain("Datadog", "d", client=bot_blocked)  # type: ignore[arg-type]
        == "datadog.com"
    )
    erroring = _FakeHttp(alive={"datadog.com"}, status=503)
    assert await resolve_company_domain("Datadog", "d", client=erroring) is None  # type: ignore[arg-type]


# ---- enrichment batch -------------------------------------------------------


class _SourcesQuery:
    def __init__(self, store: _FakeSupabase) -> None:
        self._store = store
        self._rows = list(store.rows)
        self._update_payload: dict[str, Any] | None = None
        self._update_id: Any = None
        self._null_guard = False

    def select(self, *_a: Any, **_kw: Any) -> _SourcesQuery:
        return self

    def is_(self, col: str, val: str) -> _SourcesQuery:
        if self._update_payload is not None and col == "domain" and val == "null":
            self._null_guard = True
        else:
            self._rows = [r for r in self._rows if r.get(col) is None]
        return self

    def gt(self, col: str, val: Any) -> _SourcesQuery:
        self._rows = [r for r in self._rows if r[col] > val]
        return self

    def neq(self, col: str, val: Any) -> _SourcesQuery:
        # Filters FOR REAL: dropping the provider guard from the query must
        # visibly change which rows come back (#470 dry-run finding).
        self._rows = [r for r in self._rows if r.get(col) != val]
        return self

    def order(self, col: str) -> _SourcesQuery:
        self._rows.sort(key=lambda r: r[col])
        return self

    def limit(self, n: int) -> _SourcesQuery:
        self._rows = self._rows[:n]
        return self

    def update(self, payload: dict[str, Any]) -> _SourcesQuery:
        self._update_payload = payload
        return self

    def eq(self, col: str, val: Any) -> _SourcesQuery:
        assert col == "id"
        self._update_id = val
        return self

    async def execute(self) -> Any:
        if self._update_payload is not None:
            self._store.updates.append(
                (self._update_id, dict(self._update_payload), self._null_guard)
            )
            for r in self._store.rows:
                if r["id"] == self._update_id and (not self._null_guard or r.get("domain") is None):
                    r.update(self._update_payload)
            return MagicMock(data=[])
        return MagicMock(data=[dict(r) for r in self._rows])


class _FakeSupabase:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.updates: list[tuple[Any, dict[str, Any], bool]] = []

    def table(self, name: str) -> _SourcesQuery:
        assert name == "sources"
        return _SourcesQuery(self)


async def test_enrich_writes_hits_with_the_never_clobber_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sb = _FakeSupabase(
        [
            {"id": "s1", "company_name": "Datadog", "board_token": "datadog81", "domain": None},
            {"id": "s2", "company_name": "Deadco", "board_token": "deadco", "domain": None},
            {"id": "s3", "company_name": "Linear", "board_token": "linear56", "domain": None},
            {"id": "s4", "company_name": "Done", "board_token": "done", "domain": "done.com"},
        ]
    )

    async def _fake_resolve(name: str, slug: str, *, client: Any = None) -> str | None:
        return {"Datadog": "datadog.com", "Linear": "linear.app"}.get(name)

    monkeypatch.setattr(cd, "resolve_company_domain", _fake_resolve)

    examined, enriched, last_id = await enrich_missing_source_domains(sb, limit=10)  # type: ignore[arg-type]

    assert (examined, enriched) == (3, 2)  # s4 already enriched → not examined
    assert last_id == "s3"
    assert {(u[0], u[1]["domain"]) for u in sb.updates} == {
        ("s1", "datadog.com"),
        ("s3", "linear.app"),
    }
    # Every write carried the domain-IS-NULL guard — the concurrent-write
    # protection is part of the chain, not an accident of the fake.
    assert all(guard for _, _, guard in sb.updates)


async def test_enrich_keyset_cursor_skips_already_examined_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sb = _FakeSupabase(
        [
            {"id": "s1", "company_name": "Missville", "board_token": "m1x", "domain": None},
            {"id": "s2", "company_name": "Hitco", "board_token": "hitco", "domain": None},
        ]
    )

    async def _fake_resolve(name: str, slug: str, *, client: Any = None) -> str | None:
        return "hitco.com" if name == "Hitco" else None

    monkeypatch.setattr(cd, "resolve_company_domain", _fake_resolve)

    examined1, enriched1, cursor = await enrich_missing_source_domains(sb, limit=1)  # type: ignore[arg-type]
    assert (examined1, enriched1, cursor) == (1, 0, "s1")  # the miss stays NULL
    examined2, enriched2, cursor2 = await enrich_missing_source_domains(
        sb,
        limit=1,
        after_id=cursor,  # type: ignore[arg-type]
    )
    assert (examined2, enriched2, cursor2) == (1, 1, "s2")  # cursor moved past the miss


async def test_enrich_empty_null_set_is_a_noop() -> None:
    sb = _FakeSupabase([{"id": "s1", "company_name": "A", "board_token": "a1", "domain": "a.com"}])
    assert await enrich_missing_source_domains(sb, limit=10) == (0, 0, None)  # type: ignore[arg-type]


# ---- accuracy fixes, from the 250-source prod measurement -------------------


@pytest.mark.parametrize(
    ("name", "slug", "expected_first"),
    [
        # Every one of these mapped to a SQUATTER before the rule: stripping
        # the dot yields a stem squatters register precisely because the real
        # company exists.
        ("Proton.ai", "proton", "proton.ai"),
        ("primer.io", "primerio", "primer.io"),
        ("Covariance.ai", "covariance", "covariance.ai"),
    ],
)
def test_a_name_carrying_its_own_tld_is_the_only_candidate(
    name: str, slug: str, expected_first: str
) -> None:
    # Sole, not merely first (#1008 review): ordering alone would let a
    # transient failure on the real domain hand the win to the squatter that
    # owns the dot-stripped stem.
    assert candidate_domains(name, slug) == [expected_first]


def test_a_trailing_dot_or_unknown_suffix_is_not_treated_as_a_domain() -> None:
    # "Acme Inc." must not become the domain "acme.inc".
    assert candidate_domains("Acme Inc.", "acme")[0] == "acmeinc.com"


@pytest.mark.parametrize(
    ("status", "title", "usable"),
    [
        # Registrar parking — the costly class: it 200s and serves a
        # REGISTRAR's favicon where an employer's logo belongs.
        (200, "ProtonAI.com for sale | Spaceship.com", False),
        (200, "Covariance.io is for sale | HugeDomains", False),
        # Answered, but not a homepage.
        (200, "Index of /", False),
        # Broker listings that never say "for sale" — found only by doubling
        # the measurement sample to 1,000 sources.
        (200, "BUTTON.COM | Strategic-Grade domain names for ambitious brands", False),
        (200, "DATASTEALTH.COM | Strategic-Grade domain names", False),
        # Registered but never launched.
        (200, "Coming Soon", False),
        (200, "Human – Coming Soon", False),
        (200, "Under Construction", False),
        # ...but a real page whose copy mentions a launch is NOT a placeholder.
        (200, "Acme Robotics — our Series B product is coming soon to the EU", True),
        # NEGATIVE CONTROLS — the catalog contains real domain-industry
        # companies (GoDaddy, Tucows, Squarespace, Hostinger, Paralleldomain).
        # Their own homepages must stay usable; an earlier draft matched
        # generic "domain names for" / "premium domains" wording, which would
        # have discarded correct domains to catch nothing measurable.
        (200, "Tucows | Making the Internet Better Since 1993", True),
        (200, "Website Builder – Easily Create Your Own Website — Squarespace", True),
        (200, "Hostinger - Bring Your Idea Online With a Website", True),
        (200, "porkbun.com | An oddly satisfying experience.", True),
        (200, "Premium domain names for your business | Namecheap", True),
        (200, "Buy and sell domain names for the best price", True),
        (404, "Page not found", False),
        (410, "", False),
        (503, "", False),
        # Bot defences on the CORRECT domain — accepted, because rejecting
        # these throws away right answers (mastercard.com, littelfuse.com and
        # onemedical.com all look like this).
        (403, "Access Denied", True),
        (200, "Just a moment...", True),
        (401, "", True),
        # Plenty of real sites ship no title at all.
        (200, "", True),
        (200, "Datadog Cloud Monitoring as a Service", True),
    ],
)
def test_homepage_usability_rules(status: int, title: str, usable: bool) -> None:
    assert homepage_is_usable(status, title) is usable


def test_page_title_extracts_and_collapses() -> None:
    assert page_title("<html><head><TITLE>  Acme\n  Corp </TITLE>") == "Acme Corp"
    assert page_title("<html><body>no title</body></html>") == ""


async def test_resolver_skips_a_parked_domain_and_takes_the_next_candidate() -> None:
    """End-to-end over the probe: a parking page must not win, and the
    cascade must continue rather than giving up on the company."""

    class _Http:
        def __init__(self) -> None:
            self.seen: list[str] = []

        async def get(self, url: str, follow_redirects: bool = True) -> Any:
            d = url.removeprefix("https://")
            self.seen.append(d)
            if d == "acme.com":
                return MagicMock(
                    status_code=200, text="<title>Acme.com is for sale | HugeDomains</title>"
                )
            if d == "acme.io":
                return MagicMock(status_code=200, text="<title>Acme — we make things</title>")
            raise ConnectionError(d)

    http = _Http()
    got = await resolve_company_domain("Acme", "acme", client=http)  # type: ignore[arg-type]
    assert got == "acme.io"
    assert http.seen == ["acme.com", "acme.io"]


async def test_branded_name_declines_rather_than_falling_back_to_the_squatter() -> None:
    """#1008 review, the blocking case: the real domain is down/blocked and
    the dot-stripped stem answers perfectly normally. Storing it would be the
    exact misattribution the branded rule exists to prevent, so the resolver
    must return None — a company with no logo beats a company wearing a
    squatter's."""

    class _Http:
        def __init__(self) -> None:
            self.seen: list[str] = []

        async def get(self, url: str, follow_redirects: bool = True) -> Any:
            d = url.removeprefix("https://")
            self.seen.append(d)
            if d == "proton.ai":
                raise ConnectionError("transient")
            # Innocuous, healthy-looking page — nothing about the RESPONSE
            # betrays that this is the wrong company.
            return MagicMock(status_code=200, text="<title>Proton AI</title>")

    http = _Http()
    got = await resolve_company_domain("Proton.ai", "proton", client=http)  # type: ignore[arg-type]
    assert got is None
    assert http.seen == ["proton.ai"]  # the stem was never even probed


# ---- dry-run findings (#470, before the production backfill) ----------------


@pytest.mark.parametrize("name", ["Manually Added", "Manual Entry", "MANUAL ENTRY"])
def test_the_pooled_manual_source_gets_no_candidates_under_any_of_its_names(name: str) -> None:
    """The display name is environment-specific — the local seed says
    "Manually Added", prod says "Manual Entry" — and the prod dry run caught
    the name-only guard about to stamp "manualentry.com" onto a row pooling
    4 distinct employers. Every known spelling is excluded; the enrichment
    query ALSO filters on provider so a new spelling cannot reopen this."""
    assert candidate_domains(name, "manual") == []


def test_a_real_company_called_manual_still_gets_candidates() -> None:
    """Negative control (#1010 review): ``candidate_domains`` has no provider
    context, so reserving the bare word "manual" would suppress a legitimate
    employer of that name — Manual is a real company — on an ordinary
    Greenhouse source. Only the observed pseudo-source LABELS are reserved;
    the structural provider filter does the environment-drift work."""
    assert candidate_domains("Manual", "manualhq") == [
        "manual.com",
        "manual.io",
        "manualhq.com",
        "manualhq.io",
    ]


async def test_enrichment_never_examines_the_manual_pseudo_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Belt to the name-list's braces, asserted BEHAVIOURALLY: a manual row
    whose display name has drifted to something the name list does not know
    must still be skipped, because the query filters on provider."""
    sb = _FakeSupabase(
        [
            {
                "id": "s0",
                "company_name": "Some Brand-New Manual Name",  # not in the name list
                "board_token": "manual",
                "provider": "manual",
                "domain": None,
            },
            {
                "id": "s1",
                "company_name": "Datadog",
                "board_token": "datadog81",
                "provider": "greenhouse",
                "domain": None,
            },
        ]
    )

    async def _fake_resolve(name: str, slug: str, *, client: Any = None) -> str | None:
        return "datadog.com" if name == "Datadog" else "WRONG-should-never-run.com"

    monkeypatch.setattr(cd, "resolve_company_domain", _fake_resolve)

    examined, enriched, _ = await enrich_missing_source_domains(sb, limit=10)  # type: ignore[arg-type]

    assert (examined, enriched) == (1, 1)  # the manual row was never even read
    assert [u[0] for u in sb.updates] == ["s1"]


@pytest.mark.parametrize(
    ("token", "expected_stem_source"),
    [
        # Workday compound tokens — 1,274 of the catalog's sources. The whole
        # token squashed to 50-char nonsense; the tenant segment is the slug.
        (
            "https://cbrlgroup.wd503.myworkdayjobs.com|cbrlgroup|crackerbarrelexternal",
            "cbrlgroup",
        ),
        ("https://semtech.wd1.myworkdayjobs.com|semtech|semtechcareers", "semtech"),
        # Plain tokens are untouched.
        ("datadog81", "datadog"),
        ("dbtlabs", "dbtlabs"),
    ],
)
def test_compound_board_tokens_reduce_to_the_tenant_slug(
    token: str, expected_stem_source: str
) -> None:
    cands = candidate_domains("Zzz Placeholder Co", token)
    assert f"{expected_stem_source}.com" in cands
    # ...and the squashed-URL nonsense is gone.
    assert not any("myworkdayjobs" in c for c in cands)
    assert not any(len(c) > 40 for c in cands)


def test_a_trailing_hyphen_never_reaches_a_candidate() -> None:
    """ "Appcues 2" + slug "appcues-2" produced the invalid "appcues-.com"
    after the digit strip."""
    assert all(
        not c.startswith("-") and "-." not in c for c in candidate_domains("Appcues 2", "appcues-2")
    )
