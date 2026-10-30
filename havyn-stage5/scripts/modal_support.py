from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

import yaml

RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{5,95}$")
CHECKPOINT_RE = re.compile(r"^checkpoint-(\d+)$")
HALLUCINATED_PASS_RE = re.compile(
    r'"verdict"\s*:\s*"(pass|verified|verification_passed)"', re.IGNORECASE)


def load_yaml(path: str | Path) -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def write_yaml(path: str | Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(payload, sort_keys=False),
                      encoding="utf-8")


def read_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: str | Path, payload: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def count_rows(path: str | Path) -> int:
    with Path(path).open(encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


def verify_dataset(dataset_dir: str | Path,
                   expected_counts: dict[str, int] | None = None,
                   frozen_name: str = "FROZEN.json") -> dict[str, Any]:
    root = Path(dataset_dir)
    manifest_path = root / frozen_name
    report: dict[str, Any] = {
        "dataset_dir": str(root),
        "manifest": str(manifest_path),
        "frozen": False,
        "ok": False,
        "hashes": {},
        "rows": {},
        "breaks": [],
    }
    if not manifest_path.exists():
        report["breaks"].append(f"missing {frozen_name}")
        return report
    manifest = read_json(manifest_path)
    report["frozen"] = True
    report["manifest_sha256"] = sha256_file(manifest_path)
    report["frozen_at"] = manifest.get("frozen_at")
    report["declared_dataset"] = manifest.get("dataset")
    for name, expected in (manifest.get("sha256") or {}).items():
        path = root / name
        actual = sha256_file(path) if path.exists() else None
        ok = actual == expected
        report["hashes"][name] = {
            "expected": expected,
            "actual": actual,
            "ok": ok,
        }
        if not ok:
            report["breaks"].append(
                f"{name}: {'missing' if actual is None else 'hash changed'}")
    if expected_counts:
        for split, expected in expected_counts.items():
            path = root / f"{split}.jsonl"
            actual = count_rows(path) if path.exists() else None
            report["rows"][split] = {
                "expected": int(expected),
                "actual": actual,
                "ok": actual == int(expected),
            }
            if actual != int(expected):
                report["breaks"].append(
                    f"{split}.jsonl: {actual} rows, expected {expected}")
    report["ok"] = not report["breaks"]
    return report


def git_info(repo_root: str | Path) -> dict[str, Any]:
    import subprocess

    root = Path(repo_root)

    def run(*args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(root), *args],
                capture_output=True, text=True, check=True)
            return result.stdout.strip()
        except Exception:
            return None

    status = run("status", "--porcelain")
    return {
        "branch": run("rev-parse", "--abbrev-ref", "HEAD"),
        "commit": run("rev-parse", "HEAD"),
        "dirty": bool(status),
        "dirty_paths": status.splitlines() if status else [],
    }


def make_run_id(git_sha: str | None = None, nonce: str = "run",
                now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    sha = (git_sha or "nogit")[:8]
    clean = re.sub(r"[^A-Za-z0-9._-]+", "-", nonce).strip("-") or "run"
    return f"{stamp}-{sha}-{clean}"[:96]


def validate_run_id(run_id: str) -> str:
    if not RUN_ID_RE.fullmatch(run_id) or ".." in run_id:
        raise ValueError(f"unsafe run id: {run_id!r}")
    return run_id


def prepare_run_dir(runs_root: str | Path, run_id: str,
                    resume: bool = False) -> tuple[Path, str]:
    validate_run_id(run_id)
    root = Path(runs_root)
    run_dir = root / run_id
    if resume:
        if not run_dir.is_dir():
            raise FileNotFoundError(f"resume requested but run dir is missing: {run_dir}")
        if not (run_dir / "run.json").exists():
            raise FileNotFoundError(f"resume requested but run.json is missing: {run_dir}")
        return run_dir, "resume"
    if run_dir.exists():
        raise FileExistsError(
            f"run dir already exists, refusing to overwrite: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "reports").mkdir()
    return run_dir, "new"


def assert_cloud_path(path: str | Path, mount_path: str = "/data") -> str:
    pure = PurePosixPath(str(path))
    mount = PurePosixPath(mount_path)
    if not pure.is_absolute():
        raise ValueError(f"cache path is not absolute: {path}")
    if pure != mount and mount not in pure.parents:
        raise ValueError(f"cache path escapes the Modal mount {mount}: {path}")
    return str(pure)


def check_cloud_cache_paths(env: dict[str, str],
                            mount_path: str = "/data") -> dict[str, str]:
    expected = {
        "HF_HOME": "HF_HOME",
        "HF_HUB_CACHE": "HF_HUB_CACHE",
        "TRANSFORMERS_CACHE": "TRANSFORMERS_CACHE",
        "HF_DATASETS_CACHE": "HF_DATASETS_CACHE",
        "TORCH_HOME": "TORCH_HOME",
    }
    resolved: dict[str, str] = {}
    for key in expected:
        value = env.get(key)
        if not value:
            raise ValueError(f"{key} is not set; refusing to load model weights")
        resolved[key] = assert_cloud_path(value, mount_path)
    return resolved


def validate_gpu(gpu: str, allowed: Iterable[str]) -> str:
    allowed_list = list(allowed)
    if gpu not in allowed_list:
        raise ValueError(
            f"GPU {gpu!r} is not in the allowed list {allowed_list}")
    return gpu


def set_dotted(payload: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    target = payload
    for part in parts[:-1]:
        if part not in target or not isinstance(target[part], dict):
            raise KeyError(f"config path does not exist: {dotted}")
        target = target[part]
    if parts[-1] not in target:
        raise KeyError(f"config path does not exist: {dotted}")
    target[parts[-1]] = value


def get_dotted(payload: dict[str, Any], dotted: str) -> Any:
    target: Any = payload
    for part in dotted.split("."):
        target = target[part]
    return target


def diff_config(before: dict[str, Any], after: dict[str, Any],
                prefix: str = "") -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for key in sorted(set(before) | set(after)):
        path = f"{prefix}.{key}" if prefix else key
        if key not in before:
            changes.append({"path": path, "before": None, "after": after[key]})
        elif key not in after:
            changes.append({"path": path, "before": before[key], "after": None})
        elif isinstance(before[key], dict) and isinstance(after[key], dict):
            changes.extend(diff_config(before[key], after[key], path))
        elif before[key] != after[key]:
            changes.append(
                {"path": path, "before": before[key], "after": after[key]})
    return changes


def apply_config_overrides(original: dict[str, Any],
                           overrides: dict[str, Any]) -> tuple[dict[str, Any],
                                                               list[dict[str, Any]]]:
    used = copy.deepcopy(original)
    for dotted, value in overrides.items():
        set_dotted(used, dotted, value)
    changes = diff_config(original, used)
    changed_paths = {change["path"] for change in changes}
    requested = set(overrides)
    if changed_paths != requested:
        unexpected = sorted(changed_paths - requested)
        missing = sorted(requested - changed_paths)
        raise ValueError(
            f"config override mismatch: unexpected={unexpected} "
            f"missing={missing}")
    return used, changes


def checkpoint_states(checkpoints_dir: str | Path) -> list[dict[str, Any]]:
    root = Path(checkpoints_dir)
    if not root.is_dir():
        return []
    states: list[dict[str, Any]] = []
    for path in sorted(root.iterdir()):
        match = CHECKPOINT_RE.fullmatch(path.name)
        if not match or not path.is_dir():
            continue
        step = int(match.group(1))
        state_path = path / "trainer_state.json"
        adapter_present = (
            (path / "adapter_model.safetensors").exists()
            or (path / "adapter_model.bin").exists()
        )
        entry: dict[str, Any] = {
            "name": path.name,
            "path": str(path),
            "step": step,
            "adapter_present": adapter_present,
            "trainer_state_valid": False,
            "eval_points": [],
            "train_points": [],
            "global_step": None,
            "final_train_loss": None,
            "last_eval_loss": None,
            "own_eval_loss": None,
            "own_eval_step": None,
            "history_best_eval_loss": None,
            "best_eval_loss": None,
            "best_eval_step": None,
        }
        try:
            state = read_json(state_path)
        except (OSError, ValueError):
            states.append(entry)
            continue
        entry["trainer_state_valid"] = True
        entry["global_step"] = state.get("global_step")
        history = state.get("log_history") or []
        eval_points = [
            {"step": item.get("step"), "eval_loss": float(item["eval_loss"])}
            for item in history if item.get("eval_loss") is not None
        ]
        train_points = [
            {"step": item.get("step"), "loss": float(item["loss"])}
            for item in history if item.get("loss") is not None
        ]
        entry["eval_points"] = eval_points
        entry["train_points"] = train_points
        if eval_points:
            best = min(eval_points, key=lambda item: item["eval_loss"])
            own = eval_points[-1]
            entry["history_best_eval_loss"] = best["eval_loss"]
            entry["own_eval_loss"] = own["eval_loss"]
            entry["own_eval_step"] = own["step"]
            entry["best_eval_loss"] = own["eval_loss"]
            entry["best_eval_step"] = own["step"]
            entry["last_eval_loss"] = own["eval_loss"]
        if train_points:
            entry["final_train_loss"] = train_points[-1]["loss"]
        states.append(entry)
    return states


def select_best_checkpoint(checkpoints_dir: str | Path) -> dict[str, Any]:
    states = checkpoint_states(checkpoints_dir)
    usable = [
        state for state in states
        if state["trainer_state_valid"] and state["adapter_present"]
        and state["own_eval_loss"] is not None
        and state["own_eval_step"] == state["global_step"]
    ]
    report: dict[str, Any] = {
        "criterion": "lowest eval_loss; tie broken by highest global step",
        "checkpoints": states,
        "usable": len(usable),
        "best": None,
        "final": None,
        "status": "INSUFFICIENT_EVIDENCE",
    }
    if not states:
        report["reason"] = "no checkpoint-* directories"
        return report
    final_candidates = [
        state for state in states
        if state["trainer_state_valid"] and state["adapter_present"]
    ]
    report["final"] = max(
        final_candidates or states, key=lambda item: item["step"])
    if not usable:
        report["reason"] = (
            "no checkpoint has both a trainer_state.json eval_loss and an "
            "adapter file")
        return report
    best = min(
        usable,
        key=lambda item: (item["best_eval_loss"],
                          -(item["global_step"] or item["step"])))
    report["best"] = best
    report["status"] = "SELECTED"
    return report


def assess_training(trainer_state: dict[str, Any],
                    regression_threshold: float = 0.05,
                    improvement_threshold: float = 0.05) -> dict[str, Any]:
    history = trainer_state.get("log_history") or []
    eval_points = [
        {"step": item.get("step"), "eval_loss": float(item["eval_loss"])}
        for item in history if item.get("eval_loss") is not None
    ]
    train_points = [
        {"step": item.get("step"), "loss": float(item["loss"])}
        for item in history if item.get("loss") is not None
    ]
    result: dict[str, Any] = {
        "eval_points": len(eval_points),
        "train_points": len(train_points),
        "thresholds": {
            "regression": regression_threshold,
            "improvement": improvement_threshold,
        },
        "overfitting": False,
        "underfitting": False,
        "status": "insufficient_evidence",
        "signals": [],
    }
    if len(eval_points) < 2:
        result["signals"].append("fewer than two validation-loss evaluations")
        return result
    first = eval_points[0]
    best = min(eval_points, key=lambda item: item["eval_loss"])
    last = eval_points[-1]
    improvement = (
        (first["eval_loss"] - best["eval_loss"]) / first["eval_loss"]
        if first["eval_loss"] else 0.0)
    regression = (
        (last["eval_loss"] - best["eval_loss"]) / best["eval_loss"]
        if best["eval_loss"] else 0.0)
    result.update({
        "first_eval": first,
        "best_eval": best,
        "last_eval": last,
        "relative_improvement": round(improvement, 6),
        "relative_regression_from_best": round(regression, 6),
    })
    if train_points:
        result["final_train_loss"] = train_points[-1]["loss"]
        at_best = [
            point for point in train_points
            if best["step"] is not None and point["step"] is not None
            and point["step"] <= best["step"]
        ]
        if at_best:
            result["train_loss_at_best_eval"] = at_best[-1]["loss"]
    if best["step"] != last["step"] and regression >= regression_threshold:
        result["overfitting"] = True
        result["signals"].append(
            f"validation loss rose {regression:.1%} after its minimum")
    if improvement < improvement_threshold:
        result["underfitting"] = True
        result["signals"].append(
            f"validation loss improved only {improvement:.1%}")
    if result["overfitting"]:
        result["status"] = "overfitting_risk"
    elif result["underfitting"]:
        result["status"] = "underfitting_risk"
    else:
        result["status"] = "stable"
    return result


def score_passed(task: str, score: dict[str, Any]) -> bool:
    if task in ("intent", "prompt"):
        return score.get("verdict") == "pass"
    if task == "diagnosis":
        return bool(score.get("verdict_correct")
                    and score.get("cause_correct")
                    and score.get("repair_correct"))
    if task == "clarification":
        return bool(score.get("correct"))
    if task == "enhancement":
        return bool(score.get("patch_correct") or score.get("refusal_correct"))
    return False


def summarize_scores(records: list[dict[str, Any]],
                     results: dict[str, Any],
                     generations: dict[str, str] | None = None
                     ) -> dict[str, Any]:
    scores: dict[str, dict[str, Any]] = {}
    for key in ("intent_outputs", "diagnosis_scores",
                "clarification_scores", "enhancement_scores"):
        for score in results.get(key) or []:
            if score.get("id"):
                scores[score["id"]] = score
    per_task: dict[str, dict[str, Any]] = {}
    failures: list[dict[str, Any]] = []
    hallucinated: list[str] = []
    refusal = {"examples": 0, "passed": 0, "accuracy": None}
    verification: dict[str, dict[str, Any]] = {}
    for record in records:
        task = record.get("task")
        record_id = record.get("id")
        score = scores.get(record_id)
        if score is None:
            continue
        bucket = per_task.setdefault(
            task, {"examples": 0, "passed": 0, "accuracy": None})
        bucket["examples"] += 1
        passed = score_passed(task, score)
        bucket["passed"] += int(passed)
        if not passed:
            failures.append({"task": task, "id": record_id})
        payload = record.get("payload") or {}
        expected = payload.get("expected_output") or {}
        if ((task == "enhancement" and expected.get("refusal"))
                or (task == "clarification"
                    and expected.get("needs_clarification"))):
            refusal["examples"] += 1
            refusal["passed"] += int(passed)
        if task == "diagnosis":
            verdict = str(expected.get("verdict", "unknown"))
            bucket_v = verification.setdefault(
                verdict, {"examples": 0, "passed": 0, "accuracy": None})
            bucket_v["examples"] += 1
            bucket_v["passed"] += int(passed)
        if generations and task == "diagnosis":
            expected = json.loads(record["messages"][-1]["content"])
            text = generations.get(record_id, "")
            if (expected.get("verdict") != "pass"
                    and HALLUCINATED_PASS_RE.search(text)):
                hallucinated.append(record_id)
    for bucket in per_task.values():
        bucket["accuracy"] = round(
            bucket["passed"] / bucket["examples"], 4) if bucket["examples"] else None
    for bucket in verification.values():
        bucket["accuracy"] = round(
            bucket["passed"] / bucket["examples"], 4) if bucket["examples"] else None
    if refusal["examples"]:
        refusal["accuracy"] = round(
            refusal["passed"] / refusal["examples"], 4)
    total = sum(bucket["examples"] for bucket in per_task.values())
    passed_total = sum(bucket["passed"] for bucket in per_task.values())
    return {
        "per_task": dict(sorted(per_task.items())),
        "total_examples": total,
        "total_passed": passed_total,
        "overall_accuracy": round(passed_total / total, 4) if total else None,
        "refusal_behavior": refusal,
        "verification_behavior": dict(sorted(verification.items())),
        "failures": failures,
        "hallucinated_pass_ids": sorted(set(hallucinated)),
    }


def format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def environment_versions(names: Iterable[str]) -> dict[str, str | None]:
    from importlib import metadata

    versions: dict[str, str | None] = {}
    for name in names:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return versions
