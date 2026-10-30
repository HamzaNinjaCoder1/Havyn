from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "havyn-stage5"))
sys.path.insert(0, str(REPO / "havyn-stage5" / "scripts"))

import modal_support as support

CONFIG = support.load_yaml(REPO / "havyn-stage5" / "configs" / "modal.yaml")
CHECKS: dict[str, dict] = {}


def record(name: str, ok: bool, detail) -> None:
    CHECKS[name] = {"status": "PASS" if ok else "FAIL", "detail": detail}


def check_dataset() -> None:
    report = support.verify_dataset(
        REPO / CONFIG["dataset"]["repo_dir"],
        CONFIG["dataset"]["expected_counts"],
        CONFIG["dataset"]["frozen_manifest"])
    record("dataset", report["ok"], {
        "manifest_sha256": report.get("manifest_sha256"),
        "rows": report["rows"],
        "breaks": report["breaks"],
    })
    diff = subprocess.run(
        ["git", "-C", str(REPO), "diff", "--quiet", "HEAD", "--",
         "havyn-stage5/datasets/havyn-train-v1"],
        capture_output=True, text=True, check=False)
    record("dataset_unchanged_from_head", diff.returncode == 0,
           {"git_diff_exit": diff.returncode})


def check_configs() -> None:
    paths = {
        "standard": REPO / CONFIG["configs"]["standard"],
        "smoke": REPO / CONFIG["configs"]["smoke"],
        "evaluate": REPO / CONFIG["configs"]["evaluate"],
        "modal": REPO / "havyn-stage5" / "configs" / "modal.yaml",
    }
    missing = [name for name, path in paths.items() if not path.exists()]
    record("configs", not missing, {
        "missing": missing,
        "hashes": {name: support.sha256_file(path)
                   for name, path in paths.items() if path.exists()},
    })
    standard = support.load_yaml(paths["standard"])
    smoke = support.load_yaml(paths["smoke"])
    preserved = {
        "standard_base_model": standard["base_model"],
        "smoke_base_model": smoke["base_model"],
        "lora_preserved": standard["lora"] == smoke["lora"],
        "teacher_family": support.load_yaml(
            REPO / "havyn-stage5" / "configs" / "models.yaml"
        )["teacher"]["family"],
        "review_family": support.load_yaml(
            REPO / "havyn-stage5" / "configs" / "models.yaml"
        )["review"]["family"],
    }
    record("model_recipe_preserved", preserved["lora_preserved"]
           and preserved["standard_base_model"] == smoke["base_model"],
           preserved)
    tracked = [
        REPO / "havyn-stage5" / "configs" / "train-standard.yaml",
        REPO / "havyn-stage5" / "configs" / "train-lite.yaml",
        REPO / "havyn-stage5" / "configs" / "models.yaml",
    ]
    statuses = {}
    for path in tracked:
        diff = subprocess.run(
            ["git", "-C", str(REPO), "diff", "--quiet", "HEAD", "--",
             str(path.relative_to(REPO))],
            capture_output=True, text=True, check=False)
        statuses[path.name] = diff.returncode == 0
    record("recipe_files_unchanged_from_head", all(statuses.values()),
           statuses)


def check_requirements() -> None:
    path = REPO / "havyn-stage5" / "requirements-modal.txt"
    pins = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")]
    expected = {"torch", "transformers", "trl", "peft", "accelerate",
                "datasets", "bitsandbytes", "safetensors", "pyyaml",
                "pydantic"}
    names = {pin.split("==")[0].lower() for pin in pins}
    record("requirements", expected <= names, {
        "pins": pins,
        "missing": sorted(expected - names),
    })


def check_cloud_paths() -> None:
    storage = CONFIG["storage"]
    paths = {
        "HF_HOME": storage["hf_home"],
        "HF_HUB_CACHE": storage["hf_hub_cache"],
        "TRANSFORMERS_CACHE": storage["transformers_cache"],
        "HF_DATASETS_CACHE": storage["datasets_cache"],
        "TORCH_HOME": storage["torch_home"],
    }
    try:
        resolved = support.check_cloud_cache_paths(paths, CONFIG["mount_path"])
        ok, detail = True, resolved
    except ValueError as exc:
        ok, detail = False, str(exc)
    record("cloud_cache_paths", ok, detail)
    local_hf = os.environ.get("HF_HOME")
    inside_repo = bool(
        local_hf and Path(local_hf).resolve().is_relative_to(REPO.resolve()))
    record("local_cache_outside_repo", not inside_repo,
           {"local_HF_HOME": local_hf})


def check_modal() -> None:
    try:
        import modal

        version = modal.__version__
        ok = True
    except Exception as exc:
        version, ok = f"{type(exc).__name__}: {exc}", False
    record("modal_installed", ok, {"version": version})
    try:
        result = subprocess.run(
            [sys.executable, "-m", "modal", "token", "info"],
            capture_output=True, text=True, check=False, timeout=60)
        authenticated = result.returncode == 0
        combined = f"{result.stdout}\n{result.stderr}"
        if "Token missing" in combined:
            message = "Token missing. Could not authenticate client."
        elif authenticated:
            message = "authenticated"
        else:
            lines = [line.strip() for line in combined.splitlines()
                     if line.strip()]
            message = lines[-1] if lines else "unknown error"
    except Exception as exc:
        authenticated, message = False, f"{type(exc).__name__}: {exc}"
    record("modal_authenticated", authenticated, {"message": message})
    try:
        import modal_app

        record("modal_app", True, {
            "app": modal_app.CONFIG["app_name"],
            "volume": modal_app.CONFIG["volume_name"],
            "default_gpu": modal_app.CONFIG["gpu"]["default"],
            "allowed_gpus": modal_app.CONFIG["gpu"]["allowed"],
        })
    except Exception as exc:
        record("modal_app", False, f"{type(exc).__name__}: {exc}")


def check_runbook() -> None:
    path = REPO / "havyn-stage5" / "reports" / "MODAL_RUNBOOK.md"
    record("runbook", path.exists(), {"path": str(path)})


def main() -> None:
    check_dataset()
    check_configs()
    check_requirements()
    check_cloud_paths()
    check_modal()
    check_runbook()
    failed = [name for name, check in CHECKS.items()
              if check["status"] == "FAIL"]
    token_missing = (
        CHECKS.get("modal_authenticated", {}).get("status") != "PASS")
    status = "PASS" if not failed else "BLOCKED"
    print(json.dumps({
        "status": status,
        "failed": failed,
        "next_action": (
            "python -m modal token new" if token_missing else
            "python -m modal run havyn-stage5/modal_app.py --stage smoke"),
        "checks": CHECKS,
    }, indent=2))
    raise SystemExit(0 if status == "PASS" else 2)


if __name__ == "__main__":
    main()
