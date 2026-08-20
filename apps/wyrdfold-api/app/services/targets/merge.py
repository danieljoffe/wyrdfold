"""Merge scoring profiles from multiple reference JDs.

When a target has multiple reference JDs, each with its own extracted profile,
this module merges them into a single composite profile.

Strategy (per fitted-scope.md):
- Categories: union keywords; overlapping keywords get averaged weights
  (rounded to nearest int, min 1). Category weight = average across profiles.
- Seniority: most common level (mode); union of signals.
- Domain: union signals; average weight.
- Negative: union keywords; keep the most negative weight.

"Overlapping" is decided case-INSENSITIVELY throughout. Extractions are LLM
output and do not agree on casing: a real merge on prod produced
``Microservices: 3`` and ``microservices: 2`` side by side in one category,
double-counting the concept at 1.67x its intended weight. Seniority, domain
and negative already deduped on ``.lower()``; categories did not.
"""

from collections import Counter

from app.models.targets import (
    CategoryProfile,
    DomainProfile,
    NegativeProfile,
    ScoringProfile,
    SeniorityProfile,
    TargetReferenceJD,
)

# Reference JDs with no contributor (legacy rows, operator/system seeds) are
# pooled under one synthetic contributor so they collectively count as a single
# voice in the de-bias, not one-per-JD.
_SYSTEM_CONTRIBUTOR = "__system__"


def _dedupe_keywords(keywords: dict[str, int]) -> dict[str, int]:
    """Collapse case-variants of one keyword into a single entry.

    The first spelling encountered wins (dict order is insertion order, so for
    a merge that is the earliest-contributed profile's casing) and the weights
    of every variant are averaged, matching the averaging the caller applies to
    genuine cross-profile overlaps.
    """
    grouped: dict[str, tuple[str, list[int]]] = {}
    for keyword, weight in keywords.items():
        key = keyword.lower()
        if key in grouped:
            grouped[key][1].append(weight)
        else:
            grouped[key] = (keyword, [weight])
    return {display: max(1, round(sum(ws) / len(ws))) for display, ws in grouped.values()}


def merge_profiles(profiles: list[ScoringProfile]) -> ScoringProfile:
    """Merge N extracted profiles into one composite profile."""
    if not profiles:
        return ScoringProfile()
    if len(profiles) == 1:
        # Still normalize casing. This branch is the COMMON one — a target with
        # a single reference JD reaches it through `merge_by_contributor`'s two
        # `merge_profiles` calls — so leaving it verbatim would exempt most
        # targets from the dedup. Only categories are touched; seniority /
        # domain / negative keep their single-profile passthrough semantics
        # (`_merge_negative` in particular floors the weight at -10, which a
        # one-profile merge must not impose).
        only = profiles[0].model_copy(deep=True)
        for cat in only.categories.values():
            cat.keywords = _dedupe_keywords(cat.keywords)
        return only

    return ScoringProfile(
        categories=_merge_categories(profiles),
        seniority=_merge_seniority(profiles),
        domain=_merge_domain(profiles),
        negative=_merge_negative(profiles),
    )


def merge_by_contributor(
    profiles_by_contributor: list[list[ScoringProfile]],
) -> ScoringProfile:
    """De-bias the composite by contributor.

    Two-level merge: collapse each contributor's JDs into one per-contributor
    profile, then merge those per-contributor profiles with equal weight. A user
    who contributes five JDs therefore counts the same as a user who contributes
    one — the shared rubric reflects the *breadth* of contributors, not whoever
    was most prolific (#5 refinement layer). Reuses ``merge_profiles`` at both
    levels so the averaging/union semantics stay identical to the single-level
    merge it replaces.
    """
    per_contributor = [merge_profiles(profiles) for profiles in profiles_by_contributor if profiles]
    return merge_profiles(per_contributor)


def merge_reference_jds(ref_jds: list[TargetReferenceJD]) -> ScoringProfile:
    """Merge a target's reference JDs into the shared profile, de-biased by
    contributor. ``suppressed`` contributions (down-voted past the quorum, #5
    P3) are excluded. Groups the rest by ``user_id`` (NULL → one shared
    "system" contributor) in first-contributed order, then defers to
    :func:`merge_by_contributor`.
    """
    by_contributor: dict[str, list[ScoringProfile]] = {}
    for jd in ref_jds:
        if jd.suppressed:
            continue
        key = jd.user_id or _SYSTEM_CONTRIBUTOR
        by_contributor.setdefault(key, []).append(jd.extracted_profile)
    return merge_by_contributor(list(by_contributor.values()))


def _merge_categories(
    profiles: list[ScoringProfile],
) -> dict[str, CategoryProfile]:
    """Union all categories, averaging keyword weights and category weights."""
    # Collect keyword weights per category, keyed case-insensitively so the
    # same concept spelled two ways lands in one bucket:
    #   {cat_name: {lowercased: (first-seen spelling, [weights])}}
    cat_keywords: dict[str, dict[str, tuple[str, list[int]]]] = {}
    cat_weights: dict[str, list[float]] = {}

    for profile in profiles:
        for cat_name, cat in profile.categories.items():
            if cat_name not in cat_keywords:
                cat_keywords[cat_name] = {}
                cat_weights[cat_name] = []

            cat_weights[cat_name].append(cat.weight)

            for keyword, weight in cat.keywords.items():
                key = keyword.lower()
                if key in cat_keywords[cat_name]:
                    cat_keywords[cat_name][key][1].append(weight)
                else:
                    cat_keywords[cat_name][key] = (keyword, [weight])

    merged: dict[str, CategoryProfile] = {}
    for cat_name, entries in cat_keywords.items():
        keywords = {display: max(1, round(sum(ws) / len(ws))) for display, ws in entries.values()}
        cat_w = sum(cat_weights[cat_name]) / len(cat_weights[cat_name])
        merged[cat_name] = CategoryProfile(keywords=keywords, weight=round(cat_w, 2))

    return merged


def _merge_seniority(profiles: list[ScoringProfile]) -> SeniorityProfile:
    """Mode of levels, union of signals."""
    levels = [p.seniority.level for p in profiles if p.seniority.level]
    level = Counter(levels).most_common(1)[0][0] if levels else None

    signals: list[str] = []
    seen: set[str] = set()
    for p in profiles:
        for s in p.seniority.signals:
            key = s.lower()
            if key not in seen:
                seen.add(key)
                signals.append(s)

    return SeniorityProfile(level=level, signals=signals)


def _merge_domain(profiles: list[ScoringProfile]) -> DomainProfile:
    """Union signals, average weight."""
    signals: list[str] = []
    seen: set[str] = set()
    weights: list[float] = []

    for p in profiles:
        weights.append(p.domain.weight)
        for s in p.domain.signals:
            key = s.lower()
            if key not in seen:
                seen.add(key)
                signals.append(s)

    avg_weight = sum(weights) / len(weights) if weights else 0.5
    return DomainProfile(signals=signals, weight=round(avg_weight, 2))


def _merge_negative(profiles: list[ScoringProfile]) -> NegativeProfile:
    """Union keywords, keep the most negative weight."""
    keywords: list[str] = []
    seen: set[str] = set()
    min_weight = -10.0

    for p in profiles:
        min_weight = min(min_weight, p.negative.weight)
        for kw in p.negative.keywords:
            key = kw.lower()
            if key not in seen:
                seen.add(key)
                keywords.append(kw)

    return NegativeProfile(keywords=keywords, weight=min_weight)
