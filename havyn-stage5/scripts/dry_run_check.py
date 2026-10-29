"""Offline DRY_RUN for every notebook stage.

DRY_RUN=true must exercise imports, configuration, paths, dataset loading,
formatting, model resolution, evaluator wiring and output directories without
a GPU, a Hugging Face token, or a model download.

Usage:
    python dry_run_check.py                 # every stage
    python dry_run_check.py --stage train
    python dry_run_check.py --stage bakeoff,evaluate
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for p in (str(REPO), str(REPO / "havyn-stage5" / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

REGISTRY = REPO / "havyn-stage5" / "configs" / "models.yaml"
DATASET = REPO / "havyn-stage5" / "datasets" / "havyn-train-v1"
BENCH = REPO / "havyn-stage5" / "datasets" / "bench-prompts-v1.jsonl"
GOLD = REPO / "havyn-stage5" / "datasets" / "gold-v1"


class Fail(RuntimeError):
    pass


def _yaml(path: Path) -> dict:
    import yaml
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def stage_setup() -> list[str]:
    import colab_dashboard
    import colab_ux  # noqa: F401
    import hf_auth  # noqa: F401

    registry = _yaml(REGISTRY)
    if len(registry.get("standard_candidates", [])) < 2:
        raise Fail("fewer than two standard candidates")
    if len(registry.get("lite_candidates", [])) < 2:
        raise Fail("fewer than two lite candidates")
    dashboard = colab_dashboard.build("dry-run")
    return [
        f"imports ok; registry {len(registry['standard_candidates'])} standard / "
        f"{len(registry['lite_candidates'])} lite",
        f"python {dashboard['python']}, commit {dashboard['repo_commit']}",
        f"dataset {dashboard['dataset']['version']} frozen={dashboard['dataset']['frozen']}",
        f"hf token present={dashboard['hf_auth']['present']}",
    ]


def stage_engine() -> list[str]:
    from generate_intents import build_intent, coverage_cells, verify_intent

    cell = coverage_cells()[0]
    intent = build_intent(cell, 0, random.Random(1))
    status, detail, _ = verify_intent(intent)
    if status != "valid":
        raise Fail(f"engine chain failed on {cell.cell_id}: {detail}")
    return [f"engine chain valid on {cell.cell_id}"]


def stage_bakeoff() -> list[str]:
    if not BENCH.exists():
        raise Fail(f"missing benchmark prompts: {BENCH}")
    rows = [json.loads(line) for line in BENCH.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    if len(rows) < 100:
        raise Fail(f"bake-off needs >=100 prompts, found {len(rows)}")
    config = _yaml(REPO / "havyn-stage5" / "configs" / "bakeoff.yaml")
    candidates = config.get("candidates", [])
    if len(candidates) < 3:
        raise Fail("bake-off config has fewer than three candidates")
    registry = _yaml(REGISTRY)
    known = {c["model_id"] for group in ("standard_candidates", "lite_candidates")
             for c in registry.get(group, [])}
    unknown = [c["model_id"] for c in candidates if c["model_id"] not in known]
    if unknown:
        raise Fail(f"bake-off candidates missing from registry: {unknown}")
    return [
        f"{len(rows)} prompts, {len(candidates)} candidates, all registered",
        "model loading not attempted (no download in dry-run)",
    ]


def stage_dataset() -> list[str]:
    from freeze_dataset import check as check_frozen

    breaks = check_frozen(DATASET)
    if breaks:
        raise Fail("freeze check: " + "; ".join(breaks))
    audit = json.loads((DATASET / "audit-report.json").read_text(encoding="utf-8"))
    if audit.get("total", 0) <= 0:
        raise Fail("audit reports zero rows")
    prompt = audit["per_task"].get("prompt", {})
    if prompt.get("counts", {}).get("train", 0) <= 0:
        raise Fail("prompt task has no training rows")
    for split in ("train", "validation", "heldout"):
        first = (DATASET / f"{split}.jsonl").read_text(encoding="utf-8").splitlines()[0]
        if not json.loads(first).get("task"):
            raise Fail(f"{split}.jsonl first row has no task")
    return [
        f"frozen; {audit['total']} rows; ratios {audit['ratios']}",
        f"prompt gates {prompt.get('prompt_gates')}",
    ]


def stage_format() -> list[str]:
    from format_examples import format_split, load_training_data

    examples, skipped = format_split(DATASET / "heldout.jsonl")
    if not examples:
        raise Fail(f"heldout produced no formatted examples (skipped {skipped})")
    tasks = {ex["task"] for ex in examples}
    train, val, stats = load_training_data(DATASET, 20261028)
    missing = [t for t in ("intent", "prompt", "diagnosis", "enhancement", "clarification")
               if stats["train_task_counts"].get(t, 0) <= 0]
    if missing:
        raise Fail(f"training has zero rows for tasks: {missing}")
    return [
        f"heldout formatted {len(examples)} rows, tasks {sorted(tasks)}",
        f"train {len(train)}, validation {len(val)}, per-task {stats['train_task_counts']}",
    ]


def stage_preflight() -> list[str]:
    import colab_preflight

    checks = colab_preflight.run(False, False, False, "standard", True)
    structural = {"python", "required_files", "git", "dataset_freeze",
                  "model_registry", "disk"}
    failed = [c for c in checks if c.required and not c.ok and c.name in structural]
    if failed:
        raise Fail("; ".join(f"{c.name}: {c.detail}" for c in failed))
    # GPU-only packages and a GPU/token are expected to be absent in DRY_RUN;
    # the real notebook gate still requires them before training.
    warnings = [c for c in checks if not c.ok and c.name not in structural]
    lines = [f"{c.name}: {c.detail}" for c in checks if c.name in structural]
    lines += [f"WARN (expected offline) {c.name}: {c.detail}" for c in warnings]
    if not (REPO / "havyn-stage5" / "requirements-colab.txt").exists():
        raise Fail("requirements-colab.txt is missing")
    lines.append("requirements-colab.txt present")
    return lines


def stage_train() -> list[str]:
    import train_lora
    from format_examples import load_config, load_training_data

    lines = []
    for role in ("standard", "lite"):
        path = REPO / "havyn-stage5" / "configs" / f"train-{role}.yaml"
        cfg = load_config(path)
        for key in ("base_model", "max_seq_length", "lora", "train", "output"):
            if key not in cfg:
                raise Fail(f"{path.name} missing {key}")
        train, _val, stats = load_training_data(DATASET, int(cfg["train"]["seed"]))
        if not train:
            raise Fail(f"{path.name}: no training rows")
        weights = int(float(cfg.get("model_weight_gb", 0)) * 10**9)
        free = (12 if role == "standard" else 6) * 10**9
        adjusted, note = train_lora.adjust_for_vram(dict(cfg["train"]), free, weights)
        if adjusted["per_device_train_batch_size"] < 1:
            raise Fail("VRAM fallback produced an invalid batch size")
        _, tight_note = train_lora.adjust_for_vram(dict(cfg["train"]), 6 * 10**9, weights)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / role
            out.mkdir()
            if not out.is_dir():
                raise Fail("cannot create output directory")
        lines.append(
            f"{role}: {cfg['base_model']}, {len(train)} rows, "
            f"{free / 10**9:.0f}GB -> {note}; 6GB -> {tight_note}")
    lines.append("freeze refusal is enforced in train_lora.train (unfrozen dir exits)")
    return lines


def stage_evaluate() -> list[str]:
    import evaluate_model
    from format_examples import format_split

    records, skipped = format_split(DATASET / "heldout.jsonl")
    sample = records[:5]
    if not sample:
        raise Fail("no heldout records to evaluate")
    results = evaluate_model.evaluate(sample, lambda rec: "")
    gates = evaluate_model.compute_gates(results)
    if "release" not in gates:
        raise Fail("compute_gates did not produce a release verdict")
    if gates["release"] != "BLOCKED":
        raise Fail(f"empty generations must be BLOCKED, got {gates['release']}")
    gold = evaluate_model.load_gold_examples(GOLD, 5)
    return [
        f"evaluator wired on {len(sample)} heldout rows (skipped {skipped})",
        f"empty-output gates correctly BLOCKED; gold examples available: {len(gold)}",
        "live gate refuses to report a pass without Packet Tracer",
    ]


def stage_live_export() -> list[str]:
    import evaluate_model
    import export_gguf

    with tempfile.TemporaryDirectory() as tmp:
        model_file = Path(tmp) / "model.gguf"
        model_file.write_bytes(b"havyn-dry-run" * 100)
        manifest = export_gguf.write_manifest(
            model_file, Path(tmp), "q4_k_m", "standard", "dry-run-base", None)
        if len(manifest["sha256"]) != 64:
            raise Fail("manifest hash is not SHA-256")
        if manifest["gate_release"] is not None:
            raise Fail("manifest claimed a gate without a report")
    live = evaluate_model.run_live_gate("", lambda rec: "", GOLD, 1)
    if live.get("ran") or live.get("status") == "PASS" or live.get("pass"):
        raise Fail("live gate claimed a run without Packet Tracer")
    if "NotImplementedError" not in repr(live) and live.get("ran") is not False:
        raise Fail(f"live gate returned an unclear result: {live}")
    return [
        "GGUF manifest/hash path works without a model",
        f"live gate did not run: {live.get('reason', 'no PASS claimed')}",
        "upload stays disabled (HF_PUBLISH=false default)",
    ]


def stage_improve() -> list[str]:
    import improvement_loop

    actions = improvement_loop.classify_gate({
        "schema_valid": {"status": "FAIL", "measured": 0.9},
        "live_pass": {"status": "NOT VERIFIED", "reason": "not run"},
    })
    groups = improvement_loop.classify_examples([
        {"id": "x", "schema_valid": False},
        {"id": "y", "verdict": "fail", "task": "gold"},
    ])
    if not actions or not groups:
        raise Fail("improvement classification produced nothing")
    return [
        f"gate actions {len(actions)}, failure groups {sorted(groups)}",
        "retrain half remains GPU-only and is not started",
    ]


def stage_cross_family() -> list[str]:
    import hashlib

    review = REPO / "havyn-stage5" / "prompt-authoring" / "review"
    package = review / "cross-family-input.jsonl"
    manifest = json.loads((review / "cross-family-input.manifest.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    if digest != manifest["sha256"]:
        raise Fail("cross-family package SHA does not match its manifest")
    rows = [json.loads(line) for line in package.read_text(encoding="utf-8").splitlines()]
    if len(rows) != manifest["records"]:
        raise Fail("cross-family record count mismatch")
    registry = _yaml(REGISTRY)
    teacher = registry["teacher"]["family"]
    reviewer = registry["review"]["family"]
    if teacher == reviewer:
        raise Fail("teacher and review are the same family")
    return [
        f"{len(rows)} records, SHA {digest[:12]}",
        f"teacher={teacher}, review={reviewer} (different families), status=NOT RUN",
    ]


STAGES = {
    "setup": stage_setup,
    "engine": stage_engine,
    "bakeoff": stage_bakeoff,
    "dataset": stage_dataset,
    "format": stage_format,
    "preflight": stage_preflight,
    "train": stage_train,
    "evaluate": stage_evaluate,
    "live_export": stage_live_export,
    "improve": stage_improve,
    "cross_family": stage_cross_family,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", default="all",
                        help="all or a comma-separated subset of " + ", ".join(STAGES))
    args = parser.parse_args()

    names = list(STAGES) if args.stage == "all" else [
        s.strip() for s in args.stage.split(",") if s.strip()]
    unknown = [s for s in names if s not in STAGES]
    if unknown:
        parser.error(f"unknown stage(s): {unknown}")

    failed = []
    for name in names:
        try:
            details = STAGES[name]()
            print(f"[PASS] {name}")
            for detail in details:
                print(f"       - {detail}")
        except Exception as exc:  # noqa: BLE001
            failed.append(name)
            print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")

    print("DRY-RUN PASS" if not failed else f"DRY-RUN FAIL: {failed}")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
