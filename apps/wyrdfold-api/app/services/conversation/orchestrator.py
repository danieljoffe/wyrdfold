"""Conversation orchestrator.

Three entry points:
- `handle_turn(...)` — processes one user turn in onboarding or update mode,
  persists the assistant's reply, and appends to the prose doc if the LLM
  determined fresh career content was shared.
- `reset_content(...)` — destructive wipe of prose/optimized/chunks/turns.
  Preferences survive (they have their own DELETE endpoint).
- `next_probe(...)` — pure orchestration over the gap tracker + LLM: find
  the top-priority gap, ask the LLM to phrase it as a question.

Auto-derivation is NOT triggered on every turn — that would add LLM cost
to every conversation tick. The user calls POST /experience/derive when
they want the optimized doc regenerated from accumulated prose.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import uuid
from typing import Any, cast

from supabase import AsyncClient

from app.constants import UNVERIFIED_MARKER, resolve_owner
from app.models.conversation import (
    LLMTurnResponse,
    ProbeResult,
    ResetResult,
    TurnResult,
)
from app.models.experience import (
    Annotation,
    AnnotationCreate,
    ConversationType,
    OptimizedDoc,
    OptimizedDocSource,
    OptimizedPayload,
    ProseDoc,
)
from app.models.llm import Message, ModelId
from app.services.conversation.prompts import (
    ONBOARDING_SYSTEM,
    PROBE_SYSTEM,
    UPDATE_SYSTEM,
)
from app.services.experience import gap_tracker, optimized, prose, turns
from app.services.llm import cost_log
from app.services.llm.client import LLMClient, complete_json

logger = logging.getLogger(__name__)


# ---- Inline async reads/writes (#57 slice 4) ------------------------------
#
# The conversation handlers now run on the async user/service client. The
# experience-service helpers ``prose.get_latest`` / ``prose.create_version`` /
# ``optimized.get_latest`` / ``optimized.create_version`` are still called SYNC
# by not-yet-converted modules (tailor / analysis / targets / fit_refresh /
# annotations) that hold a sync client, so their sync twins stay. These thin
# async inlines run the same queries on the async client (the
# ``routers/insights.py::_user_target_ids`` pattern) — no sync+async twin in the
# shared layer. ``add_annotation`` was orchestrator-only, so its logic inlines
# here too rather than growing an async twin in ``annotations``.


async def _prose_latest(supabase: AsyncClient, user_id: str | None) -> ProseDoc | None:
    """Async inline of ``prose.get_latest`` (sync twin kept for fit_refresh)."""
    resp = await (
        supabase.table(prose.TABLE)
        .select("*")
        .order("version", desc=True)
        .limit(1)
        .eq("user_id", resolve_owner(user_id))
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return ProseDoc.model_validate(rows[0]) if rows else None


async def _prose_create_version(
    supabase: AsyncClient, user_id: str | None, content: str
) -> ProseDoc:
    """Async inline of ``prose.create_version`` (sync twin kept for other callers)."""
    latest = await _prose_latest(supabase, user_id)
    next_version = (latest.version + 1) if latest else 1
    resp = await (
        supabase.table(prose.TABLE)
        .insert({"user_id": user_id, "version": next_version, "content": content})
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        raise RuntimeError("Failed to insert prose doc version")
    return ProseDoc.model_validate(rows[0])


async def _optimized_latest(supabase: AsyncClient, user_id: str | None) -> OptimizedDoc | None:
    """Async inline of ``optimized.get_latest``. Reads fresh: the module TTL cache
    is populated/read only by the sync callers and every write invalidates it, so
    a direct read here is always coherent (#57 slice 4)."""
    resp = await (
        supabase.table(optimized.TABLE)
        .select("*")
        .order("version", desc=True)
        .limit(1)
        .eq("user_id", resolve_owner(user_id))
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return OptimizedDoc.model_validate(rows[0]) if rows else None


async def _optimized_create_version(
    supabase: AsyncClient,
    user_id: str | None,
    payload: OptimizedPayload,
    prose_doc_id: str | None,
    source: OptimizedDocSource,
) -> OptimizedDoc:
    """Async inline of ``optimized.create_version``. Invalidates the shared module
    TTL cache so sync readers never serve the pre-write version (#57 slice 4)."""
    latest = await _optimized_latest(supabase, user_id)
    next_version = (latest.version + 1) if latest else 1
    resp = await (
        supabase.table(optimized.TABLE)
        .insert(
            {
                "user_id": user_id,
                "prose_doc_id": prose_doc_id,
                "version": next_version,
                "payload": payload.model_dump(mode="json"),
                "markdown_view": None,
                "source": source,
            }
        )
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        raise RuntimeError("Failed to insert optimized doc version")
    doc = OptimizedDoc.model_validate(rows[0])
    optimized._doc_cache.invalidate(optimized._cache_key(user_id))
    return doc


async def _add_annotation(
    supabase: AsyncClient, user_id: str | None, body: AnnotationCreate
) -> OptimizedDoc:
    """Async inline of ``annotations.add_annotation`` (orchestrator-only caller).

    Appends an annotation to the latest optimized doc as a new ``user_edit``
    version. Raises ``ValueError`` when no optimized doc exists yet (the caller
    suppresses it)."""
    latest = await _optimized_latest(supabase, user_id)
    if latest is None:
        raise ValueError("no optimized doc to annotate")
    annotation = Annotation(
        id=str(uuid.uuid4()),
        action=body.action,
        ref_type=body.ref_type,
        ref_value=body.ref_value,
        target_label=body.target_label,
        reason=body.reason,
    )
    updated_payload = latest.payload.model_copy(
        update={"annotations": [*latest.payload.annotations, annotation]}
    )
    return await _optimized_create_version(
        supabase,
        user_id=user_id,
        payload=updated_payload,
        prose_doc_id=latest.prose_doc_id,
        source="user_edit",
    )

PURPOSE_TURN_ONBOARDING = "conversation.onboarding"
PURPOSE_TURN_UPDATE = "conversation.update"
PURPOSE_PROBE = "conversation.probe"

DEFAULT_TURN_MODEL: ModelId = "claude-haiku-4-5"
"""Haiku for interactive turns — latency matters, reasoning demands are modest."""

DEFAULT_PROBE_MODEL: ModelId = "claude-haiku-4-5"
"""Haiku for phrasing a probe — one-sentence output from structured input."""


def _system_for(conv_type: ConversationType) -> str:
    return ONBOARDING_SYSTEM if conv_type == "onboarding" else UPDATE_SYSTEM


def _purpose_for(conv_type: ConversationType) -> str:
    return PURPOSE_TURN_ONBOARDING if conv_type == "onboarding" else PURPOSE_TURN_UPDATE


# A bare number, optionally with thousands separators / a decimal. Compared on
# the digit-core (commas stripped) so "1,200" in the append is supported by
# "1200" in what the user said.
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
# A proper-noun-ish token: a capitalized word, an ALL-CAPS acronym, or a
# dotted/identifier name like "Next.js" / "Node.js". These are the company /
# product / skill names the LLM could invent.
_PROPER_NOUN_RE = re.compile(r"\b[A-Z][A-Za-z0-9.+#/-]{1,}\b")
# Sentence-initial / common words that get capitalized but aren't names — kept
# small and conservative so we don't flag legitimate prose.
_STOPWORD_NAMES = frozenset(
    {
        "i",
        "the",
        "a",
        "an",
        "and",
        "but",
        "or",
        "for",
        "to",
        "of",
        "in",
        "on",
        "at",
        "with",
        "as",
        "led",
        "built",
        "shipped",
        "worked",
        "this",
        "that",
        "they",
        "he",
        "she",
        "we",
    }
)


def _digit_core(raw: str) -> str:
    core = raw.replace(",", "")
    if "." in core:
        core = core.rstrip("0").rstrip(".")
    return core


def _prose_append_warnings(prose_append: str, user_content: str) -> list[str]:
    """Key tokens in ``prose_append`` that the user never said this turn (#47).

    ``prose_append`` is concatenated verbatim into the prose doc — the
    source-of-truth the tailor later faithfully reproduces — on the weakest
    (Haiku) model, with no faithfulness check today. A fabricated number or
    company/skill name silently becomes "truth". This cheap guard flags
    appends whose numbers or proper-noun names are absent from what the user
    actually said. Conservative by design: numbers (unambiguous) and
    proper-noun-ish tokens only, so we don't over-flag ordinary restatement.

    Returns warnings; the caller logs them and surfaces them. We do NOT drop
    the append — that would lose real career content; a human/de-bias step
    owns the disposition.
    """
    source = user_content.lower()
    source_numbers = {_digit_core(m) for m in _NUMBER_RE.findall(user_content)}

    warnings: list[str] = []

    bad_numbers: list[str] = []
    seen_num: set[str] = set()
    for raw in _NUMBER_RE.findall(prose_append):
        core = _digit_core(raw)
        if not core or core in source_numbers or raw in seen_num:
            continue
        seen_num.add(raw)
        bad_numbers.append(raw)
    if bad_numbers:
        warnings.append(
            f"prose_append introduced number(s) not in the user's message: {bad_numbers}"
        )

    bad_names: list[str] = []
    seen_name: set[str] = set()
    for token in _PROPER_NOUN_RE.findall(prose_append):
        low = token.lower()
        if low in _STOPWORD_NAMES or low in seen_name:
            continue
        if low in source:
            continue
        seen_name.add(low)
        bad_names.append(token)
    if bad_names:
        warnings.append(f"prose_append introduced name(s) not in the user's message: {bad_names}")

    return warnings


def _history_as_messages(
    history: list[Any],
    latest_user_content: str,
) -> list[Message]:
    """Convert persisted turns + the current user content into LLM messages.

    Assistant and user turns become the conversation history. `system`
    turns are skipped — they're metadata, not chat content.
    Skipped user turns are annotated inline so the LLM knows the user declined.
    """
    out: list[Message] = []
    for t in history:
        if t.role == "system":
            continue
        if t.role == "user" and t.skipped:
            out.append(Message(role="user", content="[skipped question]"))
        else:
            out.append(Message(role=t.role, content=t.content))
    out.append(Message(role="user", content=latest_user_content))
    return out


async def handle_turn(
    supabase: AsyncClient,
    llm: LLMClient,
    *,
    user_id: str | None,
    conversation_type: ConversationType,
    user_content: str,
    skipped: bool,
    # The cost ledger (llm_costs) has no INSERT policy for `authenticated`, so an
    # RLS caller passes a service-role client for the cost write while `supabase`
    # stays the RLS client for turns/prose. Defaults to `supabase` for
    # service-role callers (backward-compatible). #88/Phase-1 dual-client.
    cost_supabase: AsyncClient | None = None,
) -> TurnResult:
    """Persist the user turn, run the LLM, persist the assistant reply,
    and optionally append to the prose doc."""
    from app.config import settings

    # LLM context window, not history truncation: every turn stays persisted;
    # only the slice re-sent to the model is capped (#29 — previously the
    # entire history, unbounded, was resent every turn).
    history = await turns.list_recent_turns(
        supabase,
        user_id=user_id,
        conversation_type=conversation_type,
        limit=settings.conversation_history_max_turns,
    )
    current_prose = await _prose_latest(supabase, user_id=user_id)

    await turns.append(
        supabase,
        user_id=user_id,
        conversation_type=conversation_type,
        role="user",
        content=user_content,
        skipped=skipped,
        prose_doc_id=current_prose.id if current_prose else None,
    )

    messages = _history_as_messages(history, "[skipped question]" if skipped else user_content)
    if current_prose and current_prose.content.strip():
        prose_context = "[context: current prose doc]\n" + current_prose.content
        messages.insert(
            0,
            Message(
                role="user",
                content=prose_context,
                # Cache the prose-doc prefix (#73): it's the largest stable
                # chunk re-sent on every turn. cache_prefix_chars ==
                # len(content) marks the whole block as a prompt-cache
                # breakpoint (system + prose cached); the volatile
                # conversation turns that follow are billed normally. A
                # prose_append on a later turn simply misses and re-creates.
                cache_prefix_chars=len(prose_context),
            ),
        )

    purpose = _purpose_for(conversation_type)
    parsed, result = await complete_json(
        llm,
        model=DEFAULT_TURN_MODEL,
        system=_system_for(conversation_type),
        messages=messages,
        schema=LLMTurnResponse,
        purpose=purpose,
        cache_system=True,
    )
    await cost_log.record_async(
        cost_supabase or supabase,
        user_id=user_id,
        purpose=purpose,
        result=result,
        metadata={"conversation_type": conversation_type},
    )

    new_prose_version: int | None = None
    prose_updated = False
    prose_warnings: list[str] = []
    if parsed.prose_append and parsed.prose_append.strip():
        append_text = parsed.prose_append.strip()
        # Faithfulness guard (#47): the append becomes source-of-truth the
        # tailor later reproduces verbatim, written by the weakest model with
        # no check. Flag (don't drop) numbers/names the user never said — we
        # keep the content (it may be real) but log + surface so it gets a
        # human check before it hardens into "truth".
        skip_token = skipped or not user_content.strip()
        prose_warnings = [] if skip_token else _prose_append_warnings(append_text, user_content)
        if prose_warnings:
            logger.warning(
                "prose_append faithfulness flags (user_id=%s, type=%s): %s",
                user_id,
                conversation_type,
                "; ".join(prose_warnings),
            )
            # Marker net (audit 2026-07-01, decided C+B): the ask-first rule
            # in the turn prompt is the primary control; anything that still
            # arrives flagged persists under a visible marker instead of
            # verbatim, and derive is instructed not to mint outcomes/metrics
            # from marked blocks. Content is kept (it may be real) — the
            # marker makes it confirm-or-fix instead of silently "truth".
            append_text = f"{UNVERIFIED_MARKER}\n{append_text}"
        existing = current_prose.content if current_prose else ""
        new_content = (existing + "\n\n" + append_text).strip() if existing else append_text
        new_doc = await _prose_create_version(supabase, user_id=user_id, content=new_content)
        new_prose_version = new_doc.version
        prose_updated = True

    # Persist annotation directive if the LLM parsed one (#499).
    # ValueError is raised when no optimized doc exists yet — skip silently.
    if parsed.annotation:
        with contextlib.suppress(ValueError):
            await _add_annotation(
                supabase,
                user_id=user_id,
                body=AnnotationCreate(
                    action=parsed.annotation.action,
                    ref_type=parsed.annotation.ref_type,
                    ref_value=parsed.annotation.ref_value,
                    target_label=parsed.annotation.target_label,
                    reason=parsed.annotation.reason,
                ),
            )

    await turns.append(
        supabase,
        user_id=user_id,
        conversation_type=conversation_type,
        role="assistant",
        content=parsed.assistant_message,
        skipped=False,
        prose_doc_id=None,
    )

    return TurnResult(
        assistant_message=parsed.assistant_message,
        prose_updated=prose_updated,
        prose_version=new_prose_version,
        done=parsed.done,
        prose_warnings=prose_warnings,
    )


async def reset_content(
    supabase: AsyncClient,
    *,
    user_id: str | None,
    include_turns: bool = True,
) -> ResetResult:
    """Wipe prose and optimized (chunks cascade). Preserve preferences.

    ``include_turns=True`` (the default) also wipes conversation turns — the
    full "start over" behind POST /conversation/reset. Deleting just the
    master document (DELETE /experience/prose) passes ``include_turns=False``
    so the user's chat history survives a document delete.
    """

    def _scoped(table: str) -> Any:
        q = supabase.table(table).delete()
        return q.eq("user_id", resolve_owner(user_id))

    prose_resp = await _scoped("experience_prose_docs").execute()
    optimized_resp = await _scoped("experience_optimized_docs").execute()

    prose_deleted = len(cast(list[Any], prose_resp.data or []))
    optimized_deleted = len(cast(list[Any], optimized_resp.data or []))

    turns_deleted = 0
    if include_turns:
        turns_resp = await _scoped("experience_conversation_turns").execute()
        turns_deleted = len(cast(list[Any], turns_resp.data or []))

    return ResetResult(
        prose_versions_deleted=prose_deleted,
        optimized_versions_deleted=optimized_deleted,
        turns_deleted=turns_deleted,
    )


async def next_probe(
    supabase: AsyncClient,
    llm: LLMClient,
    *,
    user_id: str | None,
    cost_supabase: AsyncClient | None = None,  # service-role cost ledger; see handle_turn
) -> ProbeResult:
    """Find the top-priority gap and phrase it as a user-facing question."""
    current = await _optimized_latest(supabase, user_id=user_id)
    if current is None:
        return ProbeResult(
            question=(
                "Tell me about your most recent role — company, title, and one win you'd lead with."
            ),
            gap=None,
        )

    gap = gap_tracker.top_gap(current.payload)
    if gap is None:
        return ProbeResult(
            question="Your optimized doc looks complete. Share what you shipped this week.",
            gap=None,
        )

    result = await llm.complete(
        model=DEFAULT_PROBE_MODEL,
        system=PROBE_SYSTEM,
        messages=[Message(role="user", content=json.dumps(gap.model_dump()))],
        purpose=PURPOSE_PROBE,
        cache_system=True,
    )
    await cost_log.record_async(
        cost_supabase or supabase,
        user_id=user_id,
        purpose=PURPOSE_PROBE,
        result=result,
        metadata={"gap_kind": gap.kind, "gap_ref": gap.ref},
    )

    question = result.content.strip().strip('"').strip()
    return ProbeResult(question=question, gap=gap)
