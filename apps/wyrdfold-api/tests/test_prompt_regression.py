"""Regression guard for the LLM matching/scoring behaviour contract (#27).

WyrdFold's product promise is match quality, but the eval harness
(``scripts/eval_*.py``) costs real LLM spend and runs manually — so a prompt
edit, a model swap, or a prompt-version bump can merge fully green while
silently shifting quality. This test pins the *inputs* that determine that
quality — every scoring/matching/generation system prompt, the per-purpose
default model, and prompt-version markers — into one committed golden snapshot.
Any drift fails CI, turning a silent change into a visible, reviewable diff and
a prompt to re-run the evals before merging.

It is **spend-free and deterministic**: it only reads module constants (no LLM
calls, no network, no DB), so it runs inside the standard ``pytest`` CI job with
no extra workflow wiring. It does NOT measure model quality — that still needs
the periodic, spend-bearing eval run (see CONTRIBUTING.md -> "Touching prompts
or scoring code"). What it guarantees is that none of the quality-bearing
*inputs* changes without a human deliberately re-baselining.

Regenerate intentionally (after re-running the relevant evals):
    UPDATE_PROMPT_GOLDENS=1 uv run pytest tests/test_prompt_regression.py
"""

from __future__ import annotations

import difflib
import importlib
import os
from pathlib import Path
from typing import get_args

from app.models.llm import ModelId

GOLDEN = Path(__file__).parent / "golden" / "llm_behavior_contract.txt"

# label -> "module:attr". The scoring/matching/generation SYSTEM PROMPTS the
# eval harness exercises. Keep in lockstep with scripts/eval_*.py and the file
# list in CONTRIBUTING.md -> "Touching prompts or scoring code".
_PROMPTS: tuple[tuple[str, str], ...] = (
    ("phase1_triage.system", "app.services.relevance.title_triage:_SYSTEM_PROMPT"),
    ("phase2_fit.system", "app.services.fit.job_fit:_SYSTEM_PROMPT"),
    ("phase2_fit.logistics_addendum", "app.services.fit.job_fit:_LOGISTICS_PROMPT_ADDENDUM"),
    (
        "derive_target_from_label.generic",
        "app.services.targets.derive_profile_from_label:SYSTEM_PROMPT_GENERIC",
    ),
    ("derive_target_from_jd.system", "app.services.targets.derive_profile:SYSTEM_PROMPT"),
    ("target_suggestion.system", "app.services.targets.suggest:SYSTEM_PROMPT"),
    ("lateral_discovery.system", "app.services.targets.lateral_discovery:_SYSTEM_PROMPT"),
    ("target_fit_score.system", "app.services.targets.fit_score:SYSTEM_PROMPT"),
    ("cover_letter.system", "app.services.tailor.prompts:COVER_LETTER_SYSTEM"),
    ("resume_tailor.system", "app.services.tailor.prompts:TAILOR_SYSTEM"),
)

# label -> "module:attr". Per-purpose default model selection + version markers.
# A model swap (e.g. grading dropped from sonnet to haiku) changes match
# quality just as much as a prompt edit, so it's part of the pinned contract.
_SCALARS: tuple[tuple[str, str], ...] = (
    ("model.phase1_triage", "app.services.relevance.title_triage:PHASE1_MODEL"),
    ("model.job_fit", "app.services.fit.job_fit:JOB_FIT_MODEL"),
    (
        "model.derive_target_from_label",
        "app.services.targets.derive_profile_from_label:DEFAULT_MODEL",
    ),
    ("model.derive_target_from_jd", "app.services.targets.derive_profile:DEFAULT_MODEL"),
    ("model.target_suggestion", "app.services.targets.suggest:DEFAULT_MODEL"),
    ("model.lateral_discovery", "app.services.targets.lateral_discovery:DEFAULT_MODEL"),
    ("model.target_fit_score", "app.services.targets.fit_score:DEFAULT_MODEL"),
    ("prompt_version.derive_target_from_jd", "app.services.targets.derive_profile:PROMPT_VERSION"),
)

_REGEN_HINT = "UPDATE_PROMPT_GOLDENS=1 uv run pytest tests/test_prompt_regression.py"


def _resolve(path: str) -> object:
    """Import ``module:attr`` and return the attribute value.

    A rename/removal of a pinned constant raises here with a clear pointer —
    the contract moved and the test's entry lists must be updated deliberately.
    """
    module_name, attr = path.split(":")
    try:
        module = importlib.import_module(module_name)
        value: object = getattr(module, attr)
    except (ImportError, AttributeError) as exc:  # pragma: no cover - failure path
        raise AssertionError(
            f"LLM-contract symbol moved or removed: '{path}' ({exc}). "
            "A pinned prompt/model constant was renamed — update _PROMPTS/_SCALARS "
            f"in {Path(__file__).name} and regenerate the golden:\n  {_REGEN_HINT}"
        ) from exc
    return value


def _render_contract() -> str:
    lines: list[str] = [
        "# WyrdFold LLM matching/scoring behaviour contract - golden snapshot.",
        "# Pinned by tests/test_prompt_regression.py (#27). Do NOT hand-edit.",
        "# Regenerate intentionally, after re-running the evals:",
        f"#   {_REGEN_HINT}",
        "",
        "===== MODELS & VERSIONS =====",
    ]
    lines += [f"{label} = {_resolve(path)}" for label, path in _SCALARS]
    lines.append(f"model_allowlist = {', '.join(get_args(ModelId))}")
    lines.append("")
    for label, path in _PROMPTS:
        lines.append(f"===== PROMPT: {label} =====")
        lines.append(str(_resolve(path)).rstrip("\n"))
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def test_llm_behaviour_contract_matches_golden() -> None:
    current = _render_contract()

    if os.environ.get("UPDATE_PROMPT_GOLDENS"):
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(current, encoding="utf-8")
        return

    assert GOLDEN.exists(), f"Golden missing: {GOLDEN}\nGenerate it with:\n  {_REGEN_HINT}"
    expected = GOLDEN.read_text(encoding="utf-8")
    if current == expected:
        return

    diff = "\n".join(
        difflib.unified_diff(
            expected.splitlines(),
            current.splitlines(),
            fromfile="golden (committed)",
            tofile="current (code)",
            lineterm="",
        )
    )
    raise AssertionError(
        "An LLM scoring/matching prompt, default model, or prompt version changed "
        "vs the committed golden. This can silently shift match quality.\n"
        "  1. Re-run the relevant eval(s) in scripts/eval_*.py against the baselines "
        "(see CONTRIBUTING.md -> 'Touching prompts or scoring code') and attach a "
        "before/after summary to the PR.\n"
        "  2. If the change is intended, regenerate the golden:\n"
        f"       {_REGEN_HINT}\n\n"
        f"{diff}"
    )
