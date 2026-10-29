"""Stage 5 Colab/HF preparation: data fidelity, VRAM reserve, publish gate."""

from __future__ import annotations

import random
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "havyn-stage5" / "scripts"
for p in (str(REPO), str(SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

import hf_auth  # noqa: E402
import hf_publish  # noqa: E402
from colab_dashboard import build as build_dashboard  # noqa: E402
from format_examples import _dedupe, format_record  # noqa: E402
from generate_intents import build_intent, coverage_cells  # noqa: E402
from prepare_hf_dataset import DEFAULT_SOURCE, build as build_hf_export  # noqa: E402
from train_lora import adjust_for_vram  # noqa: E402


def _intent() -> dict:
    return build_intent(coverage_cells()[0], 0, random.Random(1)).model_dump(mode="json")


def test_intent_request_is_rendered_not_source_excerpt() -> None:
    intent = _intent()
    intent["source_excerpt"] = "a generic placeholder that does not describe this intent"
    example = format_record({"task": "intent", "family": "c",
                             "payload": {"intent": intent}})
    assert example is not None
    assert example["request_source"] == "rendered_intent"
    assert "Design a network" in example["messages"][1]["content"]
    assert "generic placeholder" not in example["messages"][1]["content"]


def test_clarification_always_shows_the_draft() -> None:
    intent = _intent()
    intent["source_excerpt"] = "unrelated text"
    example = format_record({
        "task": "clarification", "family": "c",
        "payload": {"prompt_intent": intent, "expected_output": {"questions": []}},
    })
    assert example is not None
    assert example["request_source"] == "draft_json"
    assert "draft network intent" in example["messages"][1]["content"]


def test_dedupe_removes_identical_chat_rows() -> None:
    row = {"messages": [{"role": "user", "content": "same"}]}
    kept, removed = _dedupe([row, dict(row), {"messages": [{"role": "user", "content": "other"}]}])
    assert len(kept) == 2
    assert removed == 1


def test_vram_fallback_reserves_model_weights() -> None:
    cfg = {"per_device_train_batch_size": 4, "gradient_accumulation_steps": 2}
    adjusted, note = adjust_for_vram(dict(cfg), 6 * 10**9, 5 * 10**9)
    assert adjusted["per_device_train_batch_size"] < 4
    assert adjusted["gradient_accumulation_steps"] > 2
    assert "weights" in note


def test_model_registry_has_required_fields_and_no_winner() -> None:
    registry = yaml.safe_load(
        (REPO / "havyn-stage5" / "configs" / "models.yaml").read_text(encoding="utf-8"))
    required = {"model_id", "hf_repository", "family", "parameter_size", "license",
                "license_url", "context_length", "quantization", "tokenizer",
                "chat_template", "recommended_vram_gb", "inference_backend",
                "training_compatible", "known_limitations", "selection_status"}
    for group in ("standard_candidates", "lite_candidates"):
        assert len(registry[group]) >= 2
        for candidate in registry[group]:
            assert required <= set(candidate), (group, candidate.get("model_id"))
            assert "winner" not in candidate["selection_status"]
    assert registry["teacher"]["model_id"]
    assert registry["review"]["model_id"]
    assert registry["teacher"]["family"] != registry["review"]["family"]


def test_publish_requires_both_switches(monkeypatch) -> None:
    monkeypatch.setenv("HF_PUBLISH", "false")
    assert not hf_publish.publish_enabled(True)
    monkeypatch.setenv("HF_PUBLISH", "true")
    assert not hf_publish.publish_enabled(False)
    assert hf_publish.publish_enabled(True)


def test_hf_status_never_contains_a_token(monkeypatch) -> None:
    monkeypatch.delenv("HF_TOKEN", raising=False)
    status = hf_auth.status().to_dict()
    assert status["present"] is False
    assert status["token"] is None
    assert "value" not in status


def test_hf_dataset_export_dry_run_stays_read_only() -> None:
    manifest = build_hf_export(DEFAULT_SOURCE, REPO / "havyn-stage5" / "datasets" / "hf-export",
                               "test-dry-run", dry_run=True)
    assert manifest["provenance"]["human_contributor"] is False
    assert manifest["gold_records_included"] == 0
    assert manifest["row_counts"]["train"] > 0
    assert not (REPO / "havyn-stage5" / "datasets" / "hf-export" / "test-dry-run").exists()


def test_dashboard_hides_the_token(monkeypatch) -> None:
    monkeypatch.delenv("HF_TOKEN", raising=False)
    data = build_dashboard("test")
    assert data["hf_auth"]["token"] in (None, "hidden")
    assert "storage" in data and "minimum_free_gb" in data["storage"]


def test_merge_lora_dry_run_touches_nothing(tmp_path) -> None:
    from merge_lora import merge

    adapter = tmp_path / "adapter"
    adapter.mkdir()
    (adapter / "adapter_config.json").write_text("{}", encoding="utf-8")
    plan = merge("dry-run-base", adapter, tmp_path / "merged", dry_run=True)
    assert plan["dry_run"] is True
    assert not (tmp_path / "merged").exists()


def test_preflight_checks_sequence_and_gguf_tooling() -> None:
    import colab_preflight

    checks = {c.name: c for c in colab_preflight.run(False, False, False, "standard", True)}
    assert checks["sequence_length"].ok, checks["sequence_length"].detail
    assert "gguf_tooling" in checks
    assert checks["dataset_freeze"].ok
