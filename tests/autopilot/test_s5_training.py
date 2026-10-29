"""Offline tests for the S5.17-S5.22 half that needs no GPU: training
example formatting, gate scoring/aggregation, GGUF manifest writing and the
improvement-loop classification."""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "havyn-stage5" / "scripts"
for p in (str(REPO), str(SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

import export_gguf  # noqa: E402
import freeze_dataset  # noqa: E402
import improvement_loop  # noqa: E402
from evaluate_model import (  # noqa: E402
    apply_json_patch,
    compute_gates,
    diagnosis_class_of,
    score_clarification_output,
    score_diagnosis_output,
    score_enhancement_output,
    score_intent_output,
)
from format_examples import (  # noqa: E402
    balance_tasks,
    format_record,
    format_split,
    load_training_data,
)
from train_lora import adjust_for_vram, latest_checkpoint

# --- formatting ---------------------------------------------------------------


def _row(task: str, payload: dict, family: str = "f1") -> dict:
    return {"task": task, "family": family, "payload": payload}


def _intent() -> dict:
    return {
        "intent_version": "1", "name": "net1",
        "sites": [{"id": "s1", "areas": [{"id": "a1", "host_groups": [
            {"id": "g1", "area": "a1", "count": 2, "device": "pc",
             "vlan": "v1"}]}]}],
        "vlans": [{"id": "v1", "name": "V1", "site": "s1",
                   "required_hosts": 5}],
        "source_excerpt": "Do PCs on VLAN v1.",
    }


def test_prompt_row_becomes_chat_triplet() -> None:
    ex = format_record(_row("prompt", {
        "prompt": "Make VLAN v1 with 2 PCs.", "intent": _intent(),
        "language": "en", "style": "template/it-admin",
        "coverage_cell": "f1"}))
    assert ex is not None and ex["task"] == "prompt"
    roles = [m["role"] for m in ex["messages"]]
    assert roles == ["system", "user", "assistant"]
    assert ex["messages"][1]["content"] == "Make VLAN v1 with 2 PCs."
    assert json.loads(ex["messages"][2]["content"])["name"] == "net1"


def test_prompt_row_without_payload_id_gets_stable_hash_id() -> None:
    row = _row("prompt", {"prompt": "Make VLAN v1 with 2 PCs.",
                          "intent": _intent()})
    a = format_record(row)
    b = format_record(json.loads(json.dumps(row)))
    assert a["id"] and a["id"] == b["id"]


def test_intent_row_renders_a_faithful_request() -> None:
    ex = format_record(_row("intent", {"intent": _intent()}))
    assert ex is not None
    assert ex["request_source"] == "rendered_intent"
    user = ex["messages"][1]["content"]
    assert "Design a network called net1" in user
    assert "v1" in user
    # The stored excerpt was an invented placeholder; it must not be used.
    assert "Do PCs on VLAN v1." not in user


def test_intent_row_without_excerpt_is_not_dropped() -> None:
    intent = _intent()
    intent["source_excerpt"] = None
    ex = format_record(_row("intent", {"intent": intent}))
    assert ex is not None
    assert ex["request_source"] == "rendered_intent"


def test_diagnosis_row_carries_spec_and_evidence_without_labels() -> None:
    payload = {
        "id": "dx-1", "kind": "fault", "spec": {"name": "s"},
        "evidence": {"flow": {"source": "a", "target": "b"}},
        "expected_output": {"verdict": "confirmed",
                            "confirmed_cause": "wrong-access-vlan",
                            "repair_recommended": True},
    }
    ex = format_record(_row("diagnosis", payload, family="fault:wrong-access-vlan"))
    assert ex is not None
    user = ex["messages"][1]["content"]
    assert '"name": "s"' in user and '"flow"' in user
    assert "wrong-access-vlan" not in user  # no label leakage into the prompt
    assert json.loads(ex["messages"][2]["content"])["confirmed_cause"] == \
        "wrong-access-vlan"


def test_enhancement_and_clarification_rows_format() -> None:
    ex = format_record(_row("enhancement", {
        "before_intent": _intent(), "request": "Add VLAN v2.",
        "expected_output": {"refusal": None,
                            "patch": [{"op": "add", "path": "/vlans",
                                       "value": {"id": "v2"}}]}}))
    assert ex is not None and "Add VLAN v2." in ex["messages"][1]["content"]
    draft = _intent()
    draft["source_excerpt"] = None
    ex2 = format_record(_row("clarification", {
        "prompt_intent": draft,
        "expected_output": {"needs_clarification": True,
                            "questions": ["How many?"]}}))
    assert ex2 is not None and "draft network intent" in ex2["messages"][1]["content"]


def test_balance_tasks_is_deterministic_and_covers_every_example() -> None:
    rows = ([{"task": "intent", "family": "f", "messages": [], "id": f"i{i}"}
             for i in range(10)]
            + [{"task": "diagnosis", "family": "f", "messages": [],
                "id": f"d{i}"} for i in range(2)])
    rng = random.Random(7)
    a = balance_tasks(rows, rng)
    b = balance_tasks(rows, random.Random(7))
    assert a == b
    counts: dict[str, int] = {}
    for ex in a:
        counts[ex["task"]] = counts.get(ex["task"], 0) + 1
    assert counts["intent"] == counts["diagnosis"] == 10
    ids = {ex["id"] for ex in a}
    assert {"d0", "d1"} <= ids and {"i9"} <= ids


def test_balance_tasks_caps_repetition_of_tiny_tasks() -> None:
    rows = [{"task": "intent", "family": "f", "messages": [], "id": f"i{i}"}
            for i in range(50)]
    rows.append({"task": "diagnosis", "family": "f", "messages": [],
                 "id": "d0"})
    balanced = balance_tasks(rows, random.Random(7), max_repeat=3)
    counts: dict[str, int] = {}
    for ex in balanced:
        counts[ex["task"]] = counts.get(ex["task"], 0) + 1
    assert counts["diagnosis"] == 3 and counts["intent"] == 50


def test_load_training_data_on_frozen_smoke_dataset() -> None:
    root = REPO / "havyn-stage5" / "datasets" / "havyn-train-v1"
    if not (root / "train.jsonl").exists():
        pytest.skip("frozen smoke dataset not present")
    examples, val, stats = load_training_data(root, 20261028)
    assert stats["train_formatted"] > 0 and stats["validation"] > 0
    assert set(stats["train_task_counts"]) <= {
        "intent", "prompt", "diagnosis", "enhancement", "clarification"}


# --- scoring ------------------------------------------------------------------


def test_score_intent_exact_match_passes_with_lint() -> None:
    intent = _intent()
    text = json.dumps(intent)
    score = score_intent_output(text, intent, expect_refusal=False)
    assert score["schema_valid"] and score["lint_pass"]
    assert score["field_accuracy"] == 1.0 and score["verdict"] == "pass"


def test_score_intent_garbage_fails_without_exception() -> None:
    score = score_intent_output("I cannot do that", _intent(), False)
    assert score["verdict"] == "fail" and not score["schema_valid"]


def test_score_intent_refusal_expected() -> None:
    score = score_intent_output(
        "Guest isolation is not supported by Packet Tracer.",
        None, expect_refusal=True)
    assert score["verdict"] == "pass" and score["refusal_correct"]
    invented = score_intent_output(json.dumps(_intent()), None,
                                   expect_refusal=True)
    assert invented["verdict"] == "fail"


def test_score_diagnosis_cause_and_repair_must_match() -> None:
    expected = {"verdict": "confirmed", "confirmed_cause": "route-missing",
                "repair_recommended": True}
    good = score_diagnosis_output(json.dumps(expected), expected, "fault")
    assert good["verdict_correct"] and good["cause_correct"] \
        and good["repair_correct"]
    wrong_repair = score_diagnosis_output(json.dumps(
        {**expected, "repair_recommended": False}), expected, "fault")
    assert not wrong_repair["repair_correct"]
    multi = score_diagnosis_output(json.dumps({
        "verdict": "confirmed",
        "confirmed_causes": ["port-shutdown", "wrong-access-vlan"],
        "repair_recommended": True}), {
            "verdict": "confirmed",
            "confirmed_causes": ["wrong-access-vlan", "port-shutdown"],
            "repair_recommended": True}, "multi-fault")
    assert multi["cause_correct"]


def test_score_clarification_question_discipline() -> None:
    expected = {"needs_clarification": True, "questions": ["How many hosts?"]}
    ok = score_clarification_output(
        json.dumps({"needs_clarification": True,
                    "questions": ["How many hosts?"]}), expected)
    assert ok["correct"]
    silent = score_clarification_output(
        json.dumps({"needs_clarification": True, "questions": []}), expected)
    assert not silent["correct"]
    complete = {"needs_clarification": False, "questions": []}
    no_question = score_clarification_output(json.dumps(complete), complete)
    assert no_question["correct"]


def test_apply_json_patch_supports_add_remove_replace() -> None:
    doc = {"vlans": [{"id": "v1"}], "internet": {"nat": True}}
    patched = apply_json_patch(doc, [
        {"op": "add", "path": "/vlans/-", "value": {"id": "v2"}},
        {"op": "replace", "path": "/internet/nat", "value": False},
    ])
    assert patched["vlans"] == [{"id": "v1"}, {"id": "v2"}]
    assert patched["internet"]["nat"] is False
    assert doc["internet"]["nat"] is True  # input untouched


def test_score_enhancement_patch_must_equal_after_intent() -> None:
    before = _intent()
    after = json.loads(json.dumps(before))
    after["vlans"].append({"id": "v2", "name": "V2", "site": "s1",
                           "required_hosts": 4})
    payload = {"before_intent": before, "after_intent": after,
               "expected_output": {"refusal": None, "patch": [
                   {"op": "add", "path": "/vlans/-",
                    "value": {"id": "v2", "name": "V2", "site": "s1",
                              "required_hosts": 4}}]}}
    good = score_enhancement_output(json.dumps(
        {"patch": payload["expected_output"]["patch"]}), payload)
    assert good["patch_correct"]
    drifted = score_enhancement_output(json.dumps(
        {"patch": [{"op": "add", "path": "/vlans/-",
                    "value": {"id": "v9"}}]}), payload)
    assert not drifted["patch_correct"]
    refusal = {"before_intent": before, "after_intent": after,
               "expected_output": {"refusal": "guest-isolation-unsupported",
                                   "patch": None}}
    named = score_enhancement_output(
        "guest isolation is not supported in Packet Tracer", refusal)
    assert named["refusal_correct"]


def test_diagnosis_class_mapping() -> None:
    assert diagnosis_class_of("kind:healthy") == "healthy"
    assert diagnosis_class_of("kind:unknown") == "unknown"
    assert diagnosis_class_of("fault:route-missing") == "fault"
    assert diagnosis_class_of("fault:multi") == "multi-fault"


# --- gates --------------------------------------------------------------------


def _intent_pass() -> dict:
    return {"schema_valid": True, "lint_pass": True, "verdict": "pass",
            "field_accuracy": 1.0, "is_valid_json": True}


def test_compute_gates_all_green_still_needs_live() -> None:
    results = {
        "intent_outputs": [_intent_pass() for _ in range(20)],
        "gold_scores": [{"verdict": "pass"} for _ in range(20)],
        "diagnosis_scores": [
            {"class": cls, "verdict_correct": True, "cause_correct": True,
             "repair_correct": True}
            for cls in ("fault", "healthy", "unknown", "multi-fault")
            for _ in range(5)],
        "live": {"ran": True, "passed": 46, "total": 50},
    }
    gates = compute_gates(results)
    assert gates["release"] == "GO"
    assert gates["live_pass"]["passed"] == 46 and gates["live_pass"]["total"] == 50


def test_compute_gates_live_45_of_50_fails_the_gate() -> None:
    results = {
        "intent_outputs": [_intent_pass() for _ in range(10)],
        "gold_scores": [{"verdict": "pass"} for _ in range(10)],
        "diagnosis_scores": [
            {"class": "fault", "verdict_correct": True, "cause_correct": True,
             "repair_correct": True}],
        "live": {"ran": True, "passed": 44, "total": 50},
    }
    assert compute_gates(results)["live_pass"]["status"] == "FAIL"


def test_compute_gates_live_exactly_90_percent_passes() -> None:
    results = {
        "intent_outputs": [_intent_pass()],
        "gold_scores": [{"verdict": "pass"}],
        "diagnosis_scores": [
            {"class": "fault", "verdict_correct": True, "cause_correct": True,
             "repair_correct": True}],
        "live": {"ran": True, "passed": 45, "total": 50},
    }
    assert compute_gates(results)["live_pass"]["status"] == "PASS"


def test_compute_gates_diagnosis_threshold_is_85_percent_per_class() -> None:
    def _results(fault_correct: int, fault_total: int) -> dict:
        rows = [{"class": "fault", "verdict_correct": i < fault_correct,
                 "cause_correct": i < fault_correct,
                 "repair_correct": i < fault_correct}
                for i in range(fault_total)]
        return {
            "intent_outputs": [_intent_pass()],
            "gold_scores": [{"verdict": "pass"}],
            "diagnosis_scores": rows,
            "live": {"ran": False, "reason": "x"},
        }

    # 4/5 = 0.80 < 0.85 must fail; 17/20 = 0.85 exactly must pass.
    assert compute_gates(_results(4, 5))["diagnosis_class"]["fault"][
        "status"] == "FAIL"
    assert compute_gates(_results(17, 20))["diagnosis_class"]["fault"][
        "status"] == "PASS"


def test_compute_gates_not_verified_blocks_release() -> None:
    results = {
        "intent_outputs": [_intent_pass()],
        "gold_scores": [],
        "diagnosis_scores": [],
        "live": {"ran": False, "reason": "PT not reachable"},
    }
    gates = compute_gates(results)
    assert gates["gold_score"]["status"] == "NOT VERIFIED"
    for cls in ("fault", "healthy", "unknown", "multi-fault"):
        assert gates["diagnosis_class"][cls]["status"] == "NOT VERIFIED"
    assert gates["release"] == "BLOCKED"


def test_compute_gates_schema_gate_requires_exactly_100_percent() -> None:
    results = {
        "intent_outputs": [_intent_pass() for _ in range(99)]
        + [{"schema_valid": False, "lint_pass": True, "verdict": "fail"}],
        "gold_scores": [{"verdict": "pass"} for _ in range(10)],
        "diagnosis_scores": [],
        "live": {"ran": False, "reason": "x"},
    }
    gates = compute_gates(results)
    assert gates["schema_valid"]["status"] == "FAIL"
    assert gates["release"] == "BLOCKED"


# --- export manifests ---------------------------------------------------------


def test_manifest_and_model_card_record_sha256_and_gate(tmp_path: Path) -> None:
    gguf = tmp_path / "model-q4_k_m.gguf"
    gguf.write_bytes(b"GGUF-fake-bytes")
    gate_report = tmp_path / "gate-report.json"
    gate_report.write_text(json.dumps({"gates": {"release": "GO"}}),
                           encoding="utf-8")
    manifest = export_gguf.write_manifest(
        gguf, tmp_path, "q4_k_m", "standard", "Qwen/Qwen3-8B", gate_report)
    assert manifest["sha256"] == export_gguf.sha256_file(gguf)
    assert manifest["size_bytes"] == gguf.stat().st_size
    assert manifest["gate_release"] == "GO"
    card = export_gguf.write_model_card(
        tmp_path, manifest, None)
    text = card.read_text(encoding="utf-8")
    assert manifest["sha256"] in text
    assert "NOT VERIFIED" not in text  # gate attached and green

    no_gate = export_gguf.write_manifest(
        gguf, tmp_path, "q4_k_m", "lite", "Qwen/Qwen3-4B", None)
    card2 = export_gguf.write_model_card(tmp_path, no_gate, None)
    assert "NOT VERIFIED" in card2.read_text(encoding="utf-8")


# --- improvement loop ---------------------------------------------------------


def _gate_report(tmp_path: Path, release: str = "BLOCKED") -> Path:
    report = {
        "gates": {
            "schema_valid": {"status": "PASS", "measured": 1.0},
            "lint_pass": {"status": "FAIL", "measured": 0.9},
            "gold_score": {"status": "FAIL", "measured": 0.8},
            "diagnosis_class": {
                "fault": {"status": "FAIL", "accuracy": 0.7, "examples": 5},
                "healthy": {"status": "PASS", "accuracy": 1.0, "examples": 5},
                "unknown": {"status": "NOT VERIFIED", "accuracy": None,
                            "examples": 0},
                "multi-fault": {"status": "PASS", "accuracy": 1.0,
                                "examples": 5},
            },
            "live_pass": {"ran": False, "status": "NOT VERIFIED",
                          "reason": "PT not reachable"},
            "release": release,
        },
        "per_example": [
            {"task": "heldout", "id": "gen-1", "schema_valid": False,
             "verdict": "fail"},
            {"task": "heldout", "id": "dx-1", "class": "fault",
             "verdict_correct": False},
            {"task": "gold", "id": "gold-001", "verdict": "fail",
             "schema_valid": True},
        ],
    }
    path = tmp_path / "gate-report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def test_improvement_loop_classifies_gates_and_examples(tmp_path: Path) -> None:
    plan, failures = improvement_loop.build_plan(_gate_report(tmp_path))
    gates = {a["gate"] for a in plan["gate_actions"]}
    assert "lint_pass" in gates and "gold_score" in gates
    assert "diagnosis_class/fault" in gates
    assert "diagnosis_class/unknown" in gates  # NOT VERIFIED demands action
    assert "live_pass" in gates
    modes = set(failures)
    assert "invalid-json" in modes
    assert "diagnosis/fault" in modes
    assert "gold-miss" in modes


def test_improvement_loop_build_delta_writes_diagnosis_rows_only(
        tmp_path: Path) -> None:
    report = _gate_report(tmp_path)
    dataset = tmp_path / "ds"
    dataset.mkdir()
    heldout = {
        "task": "diagnosis", "family": "fault:route-missing",
        "payload": {"id": "dx-1", "evidence": {}, "expected_output": {}},
    }
    intent_row = {
        "task": "intent", "family": "cell-x",
        "payload": {"id": "gen-1", "intent": {}},
    }
    (dataset / "heldout.jsonl").write_text(
        json.dumps(heldout) + "\n" + json.dumps(intent_row) + "\n",
        encoding="utf-8")
    out = tmp_path / "improvement"
    # drive main() through argv
    sys_argv = sys.argv
    try:
        sys.argv = [
            "improvement_loop.py", "--evaluation", str(report),
            "--out", str(out), "--build-delta", "--dataset-dir", str(dataset)]
        with pytest.raises(SystemExit) as excinfo:
            improvement_loop.main()
        assert excinfo.value.code == 1  # release was BLOCKED
    finally:
        sys.argv = sys_argv
    plan = json.loads((out / "improvement-plan.json").read_text(encoding="utf-8"))
    assert plan["delta_written"] == 1
    delta = (out / "delta" / "delta.jsonl").read_text(encoding="utf-8")
    rows = [json.loads(line) for line in delta.splitlines() if line.strip()]
    assert len(rows) == 1 and rows[0]["task"] == "diagnosis"


# --- freeze (S5.16) ------------------------------------------------------------


def test_freeze_pins_hashes_and_check_detects_tampering(tmp_path: Path) -> None:
    (tmp_path / "train.jsonl").write_text('{"a": 1}\n', encoding="utf-8")
    (tmp_path / "validation.jsonl").write_text('{"a": 2}\n', encoding="utf-8")
    (tmp_path / "heldout.jsonl").write_text('{"a": 3}\n', encoding="utf-8")
    (tmp_path / "audit-report.json").write_text("{}", encoding="utf-8")
    (tmp_path / "dataset-card.json").write_text("{}", encoding="utf-8")
    manifest = freeze_dataset.freeze(tmp_path)
    assert len(manifest["sha256"]) == 5
    assert freeze_dataset.check(tmp_path) == []
    (tmp_path / "train.jsonl").write_text('{"a": 999}\n', encoding="utf-8")
    breaks = freeze_dataset.check(tmp_path)
    assert breaks == ["train.jsonl: hash changed since freeze"]
    (tmp_path / "heldout.jsonl").unlink()
    breaks = freeze_dataset.check(tmp_path)
    assert any("MISSING" in b for b in breaks)


def test_freeze_check_without_frozen_manifest_fails(tmp_path: Path) -> None:
    assert freeze_dataset.check(tmp_path) == [
        f"no FROZEN.json in {tmp_path} - dataset is not frozen"]


# --- VRAM fallback ------------------------------------------------------------


def test_latest_checkpoint_picks_highest_step_not_lexicographic(
        tmp_path: Path) -> None:
    for step in (250, 500, 1000):
        (tmp_path / f"checkpoint-{step}").mkdir()
    (tmp_path / "checkpoint-notanumber").mkdir()
    assert latest_checkpoint(tmp_path).name == "checkpoint-1000"
    empty = tmp_path / "empty"
    empty.mkdir()
    assert latest_checkpoint(empty) is None


def test_adjust_for_vram_halves_batch_doubles_accum() -> None:
    cfg = {"per_device_train_batch_size": 4,
           "gradient_accumulation_steps": 4}
    tiny_free = int(1.0 * 1e9)
    adjusted, note = adjust_for_vram(dict(cfg), tiny_free)
    assert adjusted["per_device_train_batch_size"] == 1
    assert adjusted["gradient_accumulation_steps"] == 16
    assert "halved" in note
    big_free = int(40.0 * 1e9)
    untouched, note2 = adjust_for_vram(dict(cfg), big_free)
    assert untouched == cfg and note2 == "no adjustment needed"
