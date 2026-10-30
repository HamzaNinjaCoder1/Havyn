from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "havyn-stage5" / "scripts"
for entry in (str(REPO), str(SCRIPTS)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import evaluate_model
import modal_support as support


def _sha(path: Path) -> str:
    return support.sha256_file(path)


def _dataset(root: Path, rows: dict[str, int] | None = None) -> dict:
    rows = rows or {"train": 2, "validation": 1, "heldout": 1}
    root.mkdir(parents=True)
    hashes = {}
    for split, count in rows.items():
        path = root / f"{split}.jsonl"
        path.write_text(
            "".join(json.dumps({"row": i}) + "\n" for i in range(count)),
            encoding="utf-8")
        hashes[path.name] = _sha(path)
    audit = root / "audit-report.json"
    audit.write_text("{}", encoding="utf-8")
    hashes[audit.name] = _sha(audit)
    card = root / "dataset-card.json"
    card.write_text("{}", encoding="utf-8")
    hashes[card.name] = _sha(card)
    (root / "FROZEN.json").write_text(json.dumps({
        "dataset": root.name,
        "frozen_at": "2026-10-30T00:00:00+00:00",
        "sha256": hashes,
    }), encoding="utf-8")
    return rows


def _checkpoint(root: Path, step: int, eval_losses: list[tuple[int, float]],
                adapter: bool = True, valid: bool = True,
                train_loss: float = 1.0) -> Path:
    path = root / f"checkpoint-{step}"
    path.mkdir(parents=True)
    history = []
    for train_step in range(10, step + 1, 10):
        history.append({"step": train_step, "loss": train_loss})
    for eval_step, loss in eval_losses:
        history.append({"step": eval_step, "eval_loss": loss})
    payload = json.dumps({"global_step": step, "log_history": history})
    (path / "trainer_state.json").write_text(
        payload if valid else "{", encoding="utf-8")
    if adapter:
        (path / "adapter_model.safetensors").write_bytes(b"adapter")
    return path


def test_verify_dataset_passes_and_detects_tamper(tmp_path: Path) -> None:
    root = tmp_path / "ds"
    rows = _dataset(root)
    good = support.verify_dataset(root, rows)
    assert good["ok"] and not good["breaks"]
    assert good["rows"]["train"]["actual"] == 2
    (root / "train.jsonl").write_text('{"row": 999}\n', encoding="utf-8")
    bad = support.verify_dataset(root, rows)
    assert not bad["ok"]
    assert any("train.jsonl" in break_ for break_ in bad["breaks"])


def test_verify_dataset_reports_count_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "ds"
    _dataset(root)
    report = support.verify_dataset(root, {"train": 99})
    assert not report["ok"]
    assert report["rows"]["train"] == {
        "expected": 99, "actual": 2, "ok": False}


def test_verify_dataset_without_manifest_fails(tmp_path: Path) -> None:
    report = support.verify_dataset(tmp_path)
    assert not report["ok"]
    assert "missing FROZEN.json" in report["breaks"][0]


def test_apply_config_overrides_changes_only_paths() -> None:
    original = support.load_yaml(
        REPO / "havyn-stage5" / "configs" / "train-standard.yaml")
    used, changes = support.apply_config_overrides(original, {
        "train.dataset_dir": "/data/ds",
        "output.checkpoint_dir": "/data/ckpt",
        "output.report_dir": "/data/report",
    })
    assert {change["path"] for change in changes} == {
        "train.dataset_dir", "output.checkpoint_dir", "output.report_dir"}
    assert used["train"]["epochs"] == original["train"]["epochs"]
    assert used["train"]["learning_rate"] == original["train"]["learning_rate"]
    assert used["lora"] == original["lora"]
    assert used["train"]["dataset_dir"] == "/data/ds"


def test_apply_config_overrides_can_disable_checkpoint_deletion() -> None:
    original = support.load_yaml(
        REPO / "havyn-stage5" / "configs" / "train-standard.yaml")
    used, changes = support.apply_config_overrides(
        original, {"train.save_total_limit": None})
    assert used["train"]["save_total_limit"] is None
    assert changes == [{"path": "train.save_total_limit",
                        "before": 3, "after": None}]


def test_apply_config_overrides_rejects_unknown_path() -> None:
    original = support.load_yaml(
        REPO / "havyn-stage5" / "configs" / "train-standard.yaml")
    with pytest.raises(KeyError):
        support.apply_config_overrides(
            original, {"train.not_a_field": 1})


def test_prepare_run_dir_refuses_overwrite_and_resumes(tmp_path: Path) -> None:
    run_dir, mode = support.prepare_run_dir(tmp_path, "20261030T000000Z-abc-run")
    assert mode == "new" and run_dir.is_dir()
    with pytest.raises(FileExistsError):
        support.prepare_run_dir(tmp_path, "20261030T000000Z-abc-run")
    support.write_json(run_dir / "run.json", {"run_id": run_dir.name})
    resumed, mode = support.prepare_run_dir(
        tmp_path, run_dir.name, resume=True)
    assert mode == "resume" and resumed == run_dir
    with pytest.raises(FileNotFoundError):
        support.prepare_run_dir(tmp_path, "20261030T000000Z-missing", resume=True)


def test_run_id_is_deterministic_and_traversal_is_rejected() -> None:
    fixed = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)
    run_id = support.make_run_id("abcdef123456", "train", fixed)
    assert run_id == "20261030T120000Z-abcdef12-train"
    support.validate_run_id(run_id)
    for bad in ("../escape", "a/b", "short", "has space"):
        with pytest.raises(ValueError):
            support.validate_run_id(bad)


def test_select_best_checkpoint_uses_lowest_eval_and_tie_breaks_latest(
        tmp_path: Path) -> None:
    _checkpoint(tmp_path, 100, [(100, 0.9)])
    _checkpoint(tmp_path, 200, [(200, 0.7)])
    _checkpoint(tmp_path, 300, [(300, 0.8)])
    _checkpoint(tmp_path, 400, [(400, 0.7)])
    _checkpoint(tmp_path, 500, [(500, 0.1)], valid=False)
    _checkpoint(tmp_path, 600, [(600, 0.1)], adapter=False)
    report = support.select_best_checkpoint(tmp_path)
    assert report["status"] == "SELECTED"
    assert report["best"]["name"] == "checkpoint-400"
    assert report["final"]["name"] == "checkpoint-400"
    assert report["usable"] == 4


def test_select_best_ignores_earlier_eval_carried_in_later_state(
        tmp_path: Path) -> None:
    _checkpoint(tmp_path, 200, [(200, 0.7)])
    _checkpoint(tmp_path, 300, [(200, 0.7), (300, 0.9)])
    report = support.select_best_checkpoint(tmp_path)
    assert report["status"] == "SELECTED"
    assert report["best"]["name"] == "checkpoint-200"
    assert report["checkpoints"][1]["history_best_eval_loss"] == 0.7
    assert report["checkpoints"][1]["own_eval_loss"] == 0.9


def test_select_best_checkpoint_insufficient_without_eval_loss(
        tmp_path: Path) -> None:
    _checkpoint(tmp_path, 10, [])
    report = support.select_best_checkpoint(tmp_path)
    assert report["status"] == "INSUFFICIENT_EVIDENCE"
    assert report["best"] is None


def _state(points: list[tuple[int, float]],
           train_loss: float = 1.0) -> dict:
    history = [{"step": step, "loss": train_loss}
               for step in range(10, max(step for step, _ in points) + 1, 10)]
    history.extend({"step": step, "eval_loss": loss} for step, loss in points)
    return {"global_step": points[-1][0], "log_history": history}


def test_assess_training_stable() -> None:
    report = support.assess_training(_state(
        [(100, 1.0), (200, 0.8), (300, 0.6)]))
    assert report["status"] == "stable"
    assert not report["overfitting"] and not report["underfitting"]


def test_assess_training_overfitting_risk() -> None:
    report = support.assess_training(_state(
        [(100, 1.0), (200, 0.8), (300, 0.9)]))
    assert report["status"] == "overfitting_risk"
    assert report["overfitting"]
    assert report["best_eval"]["step"] == 200


def test_assess_training_underfitting_risk() -> None:
    report = support.assess_training(_state(
        [(100, 1.0), (200, 0.99), (300, 0.98)]))
    assert report["status"] == "underfitting_risk"
    assert report["underfitting"]


def test_assess_training_insufficient_with_one_eval_point() -> None:
    report = support.assess_training(_state([(100, 1.0)]))
    assert report["status"] == "insufficient_evidence"
    assert "fewer than two" in report["signals"][0]


def _records() -> tuple[list[dict], dict, dict]:
    records = [
        {"task": "intent", "id": "i1", "messages": [
            {"role": "assistant", "content": "{}"}], "payload": {}},
        {"task": "diagnosis", "id": "d1", "messages": [
            {"role": "assistant", "content": json.dumps(
                {"verdict": "confirmed"})}], "payload": {
                    "expected_output": {"verdict": "confirmed"}}},
        {"task": "clarification", "id": "c1", "messages": [
            {"role": "assistant", "content": "{}"}], "payload": {
                "expected_output": {"needs_clarification": True}}},
        {"task": "enhancement", "id": "e1", "messages": [
            {"role": "assistant", "content": "{}"}], "payload": {
                "expected_output": {"refusal": "guest-isolation-unsupported"}}},
        {"task": "enhancement", "id": "e2", "messages": [
            {"role": "assistant", "content": "{}"}], "payload": {
                "expected_output": {"refusal": "guest-isolation-unsupported"}}},
    ]
    results = {
        "intent_outputs": [{"id": "i1", "verdict": "pass"}],
        "diagnosis_scores": [{"id": "d1", "verdict_correct": True,
                              "cause_correct": True,
                              "repair_correct": True}],
        "clarification_scores": [{"id": "c1", "correct": True}],
        "enhancement_scores": [
            {"id": "e1", "refusal_correct": True},
            {"id": "e2", "refusal_correct": False},
        ],
    }
    generations = {"d1": json.dumps({"verdict": "pass"})}
    return records, results, generations


def test_summarize_scores_tracks_refusal_verification_and_hallucination() -> None:
    records, results, generations = _records()
    summary = support.summarize_scores(records, results, generations)
    assert summary["per_task"]["intent"]["accuracy"] == 1.0
    assert summary["per_task"]["enhancement"]["accuracy"] == 0.5
    assert summary["refusal_behavior"] == {
        "examples": 3, "passed": 2, "accuracy": 0.6667}
    assert summary["verification_behavior"]["confirmed"] == {
        "examples": 1, "passed": 1, "accuracy": 1.0}
    assert summary["hallucinated_pass_ids"] == ["d1"]
    assert summary["total_examples"] == 5


def test_evaluate_keeps_enhancement_ids_for_behavioral_scoring() -> None:
    record = {
        "task": "enhancement",
        "id": "e1",
        "messages": [
            {"role": "system", "content": ""},
            {"role": "user", "content": "add guest vlan"},
            {"role": "assistant", "content": "{}"},
        ],
        "payload": {
            "expected_output": {
                "refusal": "guest-isolation-unsupported", "patch": None},
        },
    }
    results = evaluate_model.evaluate(
        [record], lambda _record: "guest isolation is not supported")
    assert results["enhancement_scores"][0]["id"] == "e1"
    assert results["enhancement_scores"][0]["refusal_correct"]


def test_cloud_cache_paths_must_stay_under_the_mount() -> None:
    env = {
        "HF_HOME": "/data/hf-cache",
        "HF_HUB_CACHE": "/data/hf-cache/hub",
        "TRANSFORMERS_CACHE": "/data/hf-cache/transformers",
        "HF_DATASETS_CACHE": "/data/hf-cache/datasets",
        "TORCH_HOME": "/data/torch",
    }
    assert support.check_cloud_cache_paths(env)["HF_HOME"] == "/data/hf-cache"
    leaky = dict(env, HF_HOME="/root/.cache/huggingface")
    with pytest.raises(ValueError):
        support.check_cloud_cache_paths(leaky)
    missing = dict(env)
    missing.pop("HF_HOME")
    with pytest.raises(ValueError):
        support.check_cloud_cache_paths(missing)


def test_gpu_allowlist_rejects_h100() -> None:
    allowed = ["L4", "A10G", "L40S", "A100-40GB"]
    assert support.validate_gpu("A10G", allowed) == "A10G"
    with pytest.raises(ValueError):
        support.validate_gpu("H100", allowed)


def test_duration_formatting() -> None:
    assert support.format_duration(45) == "45s"
    assert support.format_duration(125) == "2m05s"
    assert support.format_duration(3725) == "1h02m05s"


def test_training_source_keeps_best_checkpoint_and_max_steps() -> None:
    source = (SCRIPTS / "train_lora.py").read_text(encoding="utf-8")
    block = source[source.index("sft_cfg = SFTConfig("):
                   source.index("callbacks = list(")]
    assert "load_best_model_at_end=True" in block
    assert 'metric_for_best_model="eval_loss"' in block
    assert "max_steps=max_steps" in block
    assert "save_total_limit" in block
