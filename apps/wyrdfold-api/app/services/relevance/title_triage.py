"""Phase 1: per-target LLM binary title triage.

Replaces the cosine prefilter as the ingestion-time gate. Given a
batch of job titles + a target's label + the target's auto-generated
example title pools (from PR #788), the LLM returns a binary verdict
per title: PROMISING (worth deeper scoring) or UNPROMISING (drop).

Why this beats cosine
- voyage-3-lite cosines cluster all senior corporate roles in
  0.75-0.85 regardless of domain. No threshold reliably separates
  "Frontend Engineer" from "Product Designer" — both score ~0.8 vs
  a Frontend target. See ``plan-llm-scoring-migration.md``.
- An LLM with a few-shot prompt grounded in target-specific examples
  has the discrimination the embedding model lacks. Cheap enough
  (Haiku 4.5 at ~$0.0001/job) that the per-cycle cost is dominated
  by the polling itself.

Output contract
- Bool only. Phase 2 emits the 0-100 score; Phase 1 just gates.
- Bias is asymmetric: false positives in Phase 1 are cheap (Phase 2
  catches them); false negatives are lost forever. The prompt
  explicitly tells the model to lean PROMISING on close calls.

Persistence
- Phase 1 verdict is written to ``scores.promising`` for the
  (job, target) pair. ``True`` = admitted, ``NULL`` = legacy /
  pre-Phase-1 / Phase-1 unavailable (fail-open). ``False`` is not
  persisted because Phase-1-rejected jobs are not ingested under
  this target — no scores row exists for them at all (matches the
  prior cosine-skip semantics).

Batching
- 250 titles per LLM call by default. At Haiku 4.5 input pricing
  (~$1/1M) + ~50 tokens per title input + ~10 tokens per verdict
  output, that's ~$0.015 per batch. Voyage-style internal
  parallelization isn't needed — the model handles 250 inputs
  comfortably within its context window.

Failure vs dropped verdict — two different fail modes
- A FAILED call (network / auth / daily-limit error) returns ``({}, None)``.
  The poller marks titles "attempted" only on a REAL result, so a failed call
  leaves them un-attempted → they DEFER and re-triage next cycle. A dead key or
  spent daily limit therefore PAUSES admission rather than flooding 100% admit
  (#285/#294). This replaced the old "any error fail-opens" contract.
- A SUCCESSFUL call that DROPS a verdict (the model omits an id, or the
  ``title_prefix`` cross-check rejects a transposition) leaves that one
  attempted title with no verdict → the poller fail-opens it (admits) so a
  relevant posting isn't lost. That per-title hiccup is the only fail-open left,
  and it's intentional (false positives are cheap; Phase 2 catches them).
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from app.config import settings
from app.models.llm import LLMResult, Message, ModelId
from app.models.targets import JobTarget
from app.services.llm.client import LLMClient, complete_json
from app.services.llm.untrusted import UNTRUSTED_CONTENT_DIRECTIVE, wrap_untrusted

logger = logging.getLogger(__name__)

# The DEFAULT Phase-1 model — Haiku 4.5, the cheap-fast tier (~$1/1M input,
# two orders under Sonnet for a binary classification task). The RUNTIME model
# is ``settings.phase1_triage_model`` (env PHASE1_TRIAGE_MODEL), which can select
# deepseek-v3-2. Keep this constant in sync with that config default —
# test_prompt_regression snapshots it as the documented default.
PHASE1_MODEL: ModelId = "claude-haiku-4-5"
PHASE1_PURPOSE = "relevance.title_triage"

# Per-call batch size. Each batch fits in one Haiku prompt
# comfortably (250 x~50 input tokens = 12.5K, well under the 200K
# context). The poller chunks larger candidate sets and calls in
# series; Anthropic prompt caching on the static system + per-target
# context keeps the marginal cost of additional batches low.
PHASE1_BATCH_SIZE = 250

# Model-specific batch caps, applied on top of PHASE1_BATCH_SIZE. DeepSeek's
# OpenAI-shaped path caps completion output near 8K tokens — BELOW the 10,240
# this module requests — so a full 250-verdict batch (~34 output tokens per
# verdict ≈ 8.5K) can truncate mid-JSON on deepseek where Haiku is fine. The
# parser raises on truncation (safe: the batch defers, never admit-blind), but
# a batch that ALWAYS overflows would defer-retry forever, paying input tokens
# every cycle for zero verdicts — the paid retry loop again (cf. the
# 2026-07-12 embed-write loop). 150 verdicts ≈ 5.1K output leaves ~40%
# headroom under the 8K ceiling.
_MODEL_MAX_BATCH: dict[ModelId, int] = {"deepseek-v3-2": 150}


def phase1_batch_size(model: ModelId | None = None) -> int:
    """Effective per-call title-batch cap for ``model`` (default: the
    configured ``settings.phase1_triage_model``). Callers chunk to this."""
    resolved = model or settings.phase1_triage_model
    return min(PHASE1_BATCH_SIZE, _MODEL_MAX_BATCH.get(resolved, PHASE1_BATCH_SIZE))


_SYSTEM_PROMPT = (
    UNTRUSTED_CONTENT_DIRECTIVE
    + "\n\n"
    + """\
You are a job-title relevance gate. For each candidate title in the \
batch you receive, decide whether it is plausibly a match for the \
user's target role.

Be GENEROUS on close calls. A downstream grader does deeper analysis on \
everything you mark PROMISING; a false-positive there is cheap. A \
false-negative here is lost forever — the user will never see the \
posting. When uncertain, mark PROMISING.

What "promising" means
- Same career direction as the target. A "Staff Web Engineer" for a \
"Staff Frontend Engineer" target = PROMISING. A "Senior Backend \
Engineer" for a Frontend target = UNPROMISING.
- Same role function. Engineering vs design vs PM vs marketing vs \
sales vs ops are different functions. Tech-keyword overlap (TypeScript, \
accessibility, design system) is NOT enough — the ROLE FUNCTION must \
match.
- Seniority can flex by one rung in either direction. Stricter \
seniority match isn't your job; downstream code handles that.
- Adjacent specializations are fine when they share the target's core \
discipline (e.g., "Full-Stack Engineer" for a Frontend target = \
PROMISING because the core skill set overlaps).

What "unpromising" means
- Different role function. Designer vs engineer. Marketing vs CX. \
Sales vs operations.
- Wildly different domain (e.g., a hospital "Care Coordinator" for a \
tech "Director of CX" target).
- Clearly off-discipline even if the title shares a word ("Director of \
Sales" for a "Director of CX" target).

Also include a confidence score (0-100) per verdict — how certain you \
are about the promising/unpromising call. Use this scale:

- 90-100: bullseye obvious. "Director of CX Operations" for a "Director \
of CX Operations" target (promising 100); "Senior Frontend Engineer" \
for the same target (unpromising 100).
- 70-89: confident. Adjacent specialisation or one-rung-off seniority \
that fits cleanly under the lean-promising rule (or its inverse for \
unpromising).
- 40-69: legitimately ambiguous — could go either way. Use this band \
honestly; downstream tooling will rely on it to deprioritise borderline \
cases when the Phase 2 cap bites.
- 0-39: hedged. You're guessing. Still emit a verdict (the lean-promising \
rule applies here too), but flag your low certainty.

Confidence is the model's certainty in its verdict, NOT a fit score. \
A high-confidence UNPROMISING is just as useful as a high-confidence \
PROMISING — both let downstream tooling skip Phase 2 work.

Return JSON matching this exact schema:

{
  "verdicts": [
    {"id": 1, "promising": true,  "confidence": 95, "title_prefix": "Frontend Engineer"},
    {"id": 2, "promising": false, "confidence": 88, "title_prefix": "Account Executive"},
    {"id": 3, "promising": true,  "confidence": 55, "title_prefix": "Full-Stack Developer"}
  ]
}

One verdict per input title, keyed by the input id. Do NOT omit ids — \
if you can't decide, default to PROMISING with confidence < 50. \
``title_prefix`` is the first two or three words of the title you graded for \
that id, copied verbatim from the input — it lets the caller confirm the id \
lines up with the right title (a safeguard against a miscounted/transposed \
id). Return ONLY the JSON object. No prose, no markdown, no code fences."""
)


class TitleVerdict(BaseModel):
    """One LLM verdict for one input title.

    ``id`` matches the 1-based position in the input batch — the caller
    maps it back to a job_id. We use integer ids (not the job UUID)
    because numeric ids are cheaper for the model to track than UUIDs.

    ``confidence`` is optional for back-compat: pre-confidence-prompt
    responses don't include it (defaults to None). When present, it's
    the model's 0-100 certainty in the ``promising`` verdict (not a fit
    score — see prompt). The poller persists this to
    ``scores.phase1_confidence`` so ``phase2_runner`` can order
    Phase 2 candidates by confidence DESC.
    """

    id: int = Field(ge=1)
    promising: bool
    confidence: int | None = Field(default=None, ge=0, le=100)
    title_prefix: str = ""
    """The first few words of the title the model graded for this id, echoed
    verbatim. Cross-checked against the input title so a transposed/miscounted
    id is caught and dropped (fail-open) rather than mis-assigned (#47).
    Optional/back-compat: an empty echo skips the check (trusts the id)."""


class TitleTriageResponse(BaseModel):
    """LLM response: one ``TitleVerdict`` per input title."""

    verdicts: list[TitleVerdict] = Field(default_factory=list)


def admitted(verdict: TitleVerdict | None, *, min_confidence: int) -> bool:
    """Phase-1 admission decision: promising AND confident enough.

    A ``promising`` verdict the model is only guessing at (confidence below
    ``min_confidence``) is NOT admitted — the confidence signal gates
    admission, not just Phase-2 ordering (#47). Fail-open like the rest of
    Phase 1: a missing verdict (None) or a pre-confidence verdict (NULL
    confidence) admits, preserving the lean-promising default.

    Returning ``False`` makes the persisted ``promising`` column False, which
    both excludes the row (``excluded_by_prefilter``) and makes
    ``_needs_phase2`` skip it — so a low-confidence guess costs no deep grade.
    """
    if verdict is None:
        return True
    if not verdict.promising:
        return False
    if verdict.confidence is None:
        return True
    return verdict.confidence >= min_confidence


# How many mismatched (title, prefix) pairs to log per call, and how much of
# each string. The drops repeat every poll cycle, so a handful of examples is
# enough to spot the pattern — logging all of them on every cycle would be
# noise that gets filtered out, which is how this went undiagnosed (#652).
_DROP_SAMPLE_LIMIT = 5
_DROP_SAMPLE_CHARS = 80


def _prefix_matches_title(title: str, prefix: str) -> bool:
    """Whether the model's echoed ``prefix`` is a leading fragment of ``title``.

    Case- and whitespace-normalized so a faithfully-copied prefix always
    matches, but a prefix copied from a DIFFERENT title (a transposed id) does
    not. An empty prefix returns True — the check is skipped (back-compat with
    responses that don't echo, and never a reason to drop a verdict on absence).
    """
    p = " ".join(prefix.lower().split())
    if not p:
        return True
    return " ".join(title.lower().split()).startswith(p)


def _split_user_message(target: JobTarget, titles: list[str]) -> tuple[str, str]:
    """Compose the per-call user message as ``(static_prefix, dynamic_suffix)``.

    Target context (label + example pools) sits at the top so it's a
    natural prompt-cache prefix when grading multiple batches for the
    same target in one poll cycle. The split boundary feeds
    ``Message.cache_prefix_chars`` — the prefix depends only on the
    target, the suffix carries the per-batch titles. Concatenating the
    two halves yields the exact message Phase 1 has always sent (the
    boundary newline lives in the suffix so the cached prefix bytes
    never vary with the batch).
    """
    static_lines: list[str] = [f"Target role: {target.label}"]

    if target.example_promising_titles:
        static_lines.append("")
        static_lines.append("Examples of PROMISING titles for this target:")
        for ex in target.example_promising_titles:
            static_lines.append(f"- {ex}")

    if target.example_unpromising_titles:
        static_lines.append("")
        static_lines.append("Examples of UNPROMISING titles:")
        for ex in target.example_unpromising_titles:
            static_lines.append(f"- {ex}")

    dynamic_lines: list[str] = [""]
    dynamic_lines.append(
        f"Grade the following {len(titles)} candidate titles. Return one verdict per id."
    )
    dynamic_lines.append("")
    # The titles are scraped, attacker-controllable text. Fence the whole
    # numbered list (one fence keeps the per-batch token overhead flat) so a
    # malicious title can't pass instructions to the gate. The trusted "N. "
    # numbering is ours — it carries no fence tokens, so wrapping the joined
    # block leaves it untouched while defanging any forged fence in a title.
    numbered = "\n".join(f"{idx}. {title}" for idx, title in enumerate(titles, start=1))
    dynamic_lines.append(wrap_untrusted(numbered, name="candidate_titles"))

    return "\n".join(static_lines), "\n" + "\n".join(dynamic_lines)


def _build_user_message(target: JobTarget, titles: list[str]) -> str:
    """Full user message — concatenation of the cache-split halves."""
    static_prefix, dynamic_suffix = _split_user_message(target, titles)
    return static_prefix + dynamic_suffix


async def triage_titles(
    llm: LLMClient,
    *,
    target: JobTarget,
    titles: list[str],
    model: ModelId | None = None,
    purpose: str = PHASE1_PURPOSE,
) -> tuple[dict[int, TitleVerdict], LLMResult | None]:
    """Grade up to ``PHASE1_BATCH_SIZE`` titles against one target.

    Returns ``(verdicts_by_index, llm_result)`` where:
    - ``verdicts_by_index`` maps the 1-based input index to a
      ``TitleVerdict`` carrying ``promising`` (bool) and ``confidence``
      (int 0-100, or None on legacy / partial responses). Indices missing
      from the dict mean "no verdict"; callers must treat that as
      fail-open (admit the job) so a partial LLM hiccup doesn't drop
      relevant postings.
    - ``llm_result`` is the LLMResult for cost logging, or ``None`` if
      the call failed entirely (caller logs the exception via the
      ``logger`` import).

    Pass ``titles`` in the same order as the candidate job rows — the
    1-based id in the verdict map is the position into that list.
    """
    if not titles:
        return {}, None

    # Default to the configured Phase-1 model (Haiku, or deepseek-v3-2 when
    # PHASE1_TRIAGE_MODEL is set). Resolved at call time so an env flip / test
    # override takes effect without re-import. An explicit ``model=`` still wins.
    if model is None:
        model = settings.phase1_triage_model

    cap = phase1_batch_size(model)
    if len(titles) > cap:
        # Defensive: callers are expected to chunk to phase1_batch_size().
        # We don't truncate silently because losing the tail would be a
        # confusing data loss; raise loudly so the caller's batching
        # bug is visible.
        raise ValueError(
            f"triage_titles batch size {len(titles)} exceeds the {cap}-title "
            f"cap for model {model}. Chunk the input with phase1_batch_size()."
        )

    static_prefix, dynamic_suffix = _split_user_message(target, titles)
    user_message = static_prefix + dynamic_suffix

    try:
        parsed, result = await complete_json(
            llm,
            model=model,
            system=_SYSTEM_PROMPT,
            # ``cache_prefix_chars`` marks the per-target context (label
            # + example pools) as a prompt-cache breakpoint — the second
            # cacheable prefix after the system prompt. Bytes-identical
            # split, see Message model.
            messages=[
                Message(
                    role="user",
                    content=user_message,
                    cache_prefix_chars=len(static_prefix),
                )
            ],
            schema=TitleTriageResponse,
            purpose=purpose,
            # Output is ~250 verdicts; the per-verdict ``title_prefix`` echo
            # adds a few tokens each (~34 tok/verdict now), so lift the cap to
            # keep clear of a mid-JSON truncation (which fails the whole batch
            # open — #112 raises on a max_tokens stop). The model only emits
            # what it needs, so the higher ceiling costs nothing unless used.
            max_tokens=10240,
            cache_system=True,
        )
    except Exception:
        # Return ({}, None). The caller (poller) marks titles "attempted" only on
        # a real LLM result, so a failed call leaves them un-attempted → they
        # DEFER (re-triaged next cycle), NOT fail-open admit. A dead key / spent
        # daily limit must PAUSE admission, never flood 100% admit (#285/#294).
        logger.exception(
            "Phase 1 title triage call failed for target %s (%s); returning no "
            "verdicts — the poller DEFERS these un-attempted titles (not admit)",
            target.id,
            target.label,
        )
        return {}, None

    # Map verdicts by id. Tolerate the model returning duplicates (last one
    # wins) or omitting ids (treated as fail-open by the caller's
    # ``.get(i)``-returns-None pattern). Additionally, cross-check the echoed
    # ``title_prefix`` against the id's input title (#47): a transposed or
    # out-of-range id would mis-assign one title's verdict to another —
    # admitting an off-topic role and dropping a relevant one — so drop those
    # and let the caller fail-open (admit) instead.
    verdicts_by_id: dict[int, TitleVerdict] = {}
    # The two drop causes are counted SEPARATELY (#652). They were one counter,
    # which made the failure undiagnosable: an out-of-range id means the model
    # invented an index, while a prefix mismatch means it echoed text that is
    # not a leading fragment of the title it was numbered against. Those have
    # nothing to do with each other, and prod showed EXACTLY 7 drops per batch
    # across six independent targets — a deterministic pattern the old log
    # could not distinguish from sporadic model error.
    out_of_range = 0
    prefix_mismatch: list[tuple[int, str, str]] = []
    for v in parsed.verdicts:
        if not (1 <= v.id <= len(titles)):
            out_of_range += 1
            continue
        title = titles[v.id - 1]
        if not _prefix_matches_title(title, v.title_prefix):
            prefix_mismatch.append((v.id, title, v.title_prefix))
            continue
        verdicts_by_id[v.id] = v

    dropped = out_of_range + len(prefix_mismatch)
    if dropped:
        logger.warning(
            "Phase 1 dropped %d/%d verdict(s) for target %s (%s) — "
            "%d id out-of-range, %d title_prefix mismatch; those titles "
            "fail-open (admit)",
            dropped,
            len(parsed.verdicts),
            target.id,
            target.label,
            out_of_range,
            len(prefix_mismatch),
        )
    if prefix_mismatch:
        # The actual (title, prefix) pairs — the payload #652 needed and the
        # only way to tell WHY a prefix failed (abbreviation? punctuation?
        # unicode? a genuinely transposed id?). Bounded to a sample: the drops
        # repeat every cycle, so a few examples reveal the pattern without
        # flooding the log.
        logger.warning(
            "Phase 1 title_prefix mismatches for target %s (%s) — sample %d/%d: %s",
            target.id,
            target.label,
            min(len(prefix_mismatch), _DROP_SAMPLE_LIMIT),
            len(prefix_mismatch),
            "; ".join(
                f"id={i} title={t[:_DROP_SAMPLE_CHARS]!r} prefix={pfx[:_DROP_SAMPLE_CHARS]!r}"
                for i, t, pfx in prefix_mismatch[:_DROP_SAMPLE_LIMIT]
            ),
        )
    return verdicts_by_id, result
