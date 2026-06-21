"""Guards the recurring-eval-cadence wiring (#27).

The synthetic fixture generator must keep producing something the eval harness
can load — otherwise the on-demand eval workflow silently breaks. PII-free and
spend-free (no LLM calls): just builds the fixture and checks both eval loaders'
expectations.
"""

from __future__ import annotations

from app.models.experience import OptimizedPayload
from app.models.targets import JobTarget
from scripts.gen_sample_eval_set import build_eval_set


def test_sample_eval_set_is_consumable_by_the_eval_loaders() -> None:
    fx = build_eval_set()
    # Marks the fixture as fabricated, never a real-data snapshot.
    assert fx.get("synthetic") is True

    # eval_phase1_triage: _rehydrate_targets validates targets[*]["target"]
    # as JobTarget; _titles_by_target reads cases[*].{target_id,title}.
    assert fx["targets"], "expected at least one target"
    for meta in fx["targets"].values():
        JobTarget.model_validate(meta["target"])
        assert meta.get("label")
    assert fx["cases"], "expected title cases for phase-1 triage"
    for case in fx["cases"]:
        assert case["target_id"] in fx["targets"]
        assert case.get("title")

    # eval_derive_target: _load_payload reads the first target's payload as
    # an OptimizedPayload.
    first = next(iter(fx["targets"]))
    OptimizedPayload.model_validate(fx["targets"][first]["payload"])
