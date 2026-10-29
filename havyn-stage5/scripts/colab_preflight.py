"""Fail-fast preflight for Colab training/evaluation.

Every check returns a pass/fail with a useful message. Required checks make
the script exit 1 before any GPU work starts. The script never prints a token.

Usage:
    python colab_preflight.py                    # structural checks (CPU ok)
    python colab_preflight.py --require-gpu      # before bake-off/training
    python colab_preflight.py --require-gpu --require-hf --role lite
    python colab_preflight.py --json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for p in (str(REPO), str(REPO / "havyn-stage5" / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from hf_auth import status as hf_status  # noqa: E402

PY_MIN = (3, 11)
REQUIRED_PACKAGES = ["torch", "transformers", "trl", "peft", "datasets",
                     "accelerate", "huggingface_hub", "yaml"]
OPTIONAL_PACKAGES = ["unsloth", "bitsandbytes", "sentencepiece", "safetensors"]


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


def _pkg(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(REPO), *args],
                              capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:
        return ""


def _load_yaml(path: Path) -> dict:
    import yaml
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _freeze_check() -> tuple[bool, str]:
    try:
        from freeze_dataset import check as check_frozen
        dataset = REPO / "havyn-stage5" / "datasets" / "havyn-train-v1"
        breaks = check_frozen(dataset)
        return (not breaks), "frozen and byte-identical" if not breaks else "; ".join(breaks)
    except Exception as exc:  # noqa: BLE001
        return False, f"freeze check failed: {exc}"


def run(require_gpu: bool, require_hf: bool, check_hf_network: bool,
        role: str, allow_publish: bool) -> list[Check]:
    checks: list[Check] = []

    # Python and platform
    ok = sys.version_info >= PY_MIN
    checks.append(Check(
        "python", ok,
        f"{platform.python_version()} ({'ok' if ok else 'need >= 3.11'})"))
    checks.append(Check("platform", True, f"{platform.system()} {platform.machine()}"))

    # Repository and files
    files = [
        "pyproject.toml",
        "havyn-stage5/configs/models.yaml",
        "havyn-stage5/configs/train-standard.yaml",
        "havyn-stage5/configs/train-lite.yaml",
        "havyn-stage5/datasets/havyn-train-v1/FROZEN.json",
        "havyn-stage5/datasets/havyn-train-v1/train.jsonl",
        "havyn-stage5/datasets/havyn-train-v1/validation.jsonl",
        "havyn-stage5/datasets/havyn-train-v1/heldout.jsonl",
    ]
    missing = [f for f in files if not (REPO / f).exists()]
    checks.append(Check("required_files", not missing,
                        "all present" if not missing else f"missing: {missing}"))

    commit = _git("rev-parse", "HEAD")[:12]
    dirty = _git("status", "--porcelain", "--", "src")
    checks.append(Check(
        "git", bool(commit),
        f"HEAD {commit or 'unknown'}" + (" (+dirty src)" if dirty else "")))
    checks.append(Check(
        "engine_dirty", not dirty,
        "src/ is clean" if not dirty else
        "src/ has uncommitted changes; results are pinned as +dirty-src",
        required=False))

    frozen, freeze_detail = _freeze_check()
    checks.append(Check("dataset_freeze", frozen, freeze_detail))

    # Configs
    try:
        models = _load_yaml(REPO / "havyn-stage5/configs/models.yaml")
        counts = {
            "standard": len(models.get("standard_candidates", [])),
            "lite": len(models.get("lite_candidates", [])),
        }
        winner = any(
            "winner" in str(c.get("selection_status", ""))
            for group in ("standard_candidates", "lite_candidates")
            for c in models.get(group, [])
        )
        checks.append(Check(
            "model_registry", counts["standard"] >= 2 and counts["lite"] >= 2 and not winner,
            f"standard={counts['standard']} lite={counts['lite']} "
            f"winner_declared={winner}"))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("model_registry", False, f"cannot load registry: {exc}"))

    # Packages
    missing_pkgs = [p for p in REQUIRED_PACKAGES if not _pkg(p)]
    checks.append(Check(
        "packages", not missing_pkgs,
        "all required present" if not missing_pkgs else
        f"missing: {missing_pkgs} -> pip install -r havyn-stage5/requirements-colab.txt"))
    optional = {p: _pkg(p) for p in OPTIONAL_PACKAGES}
    checks.append(Check("optional_packages", True,
                        ", ".join(f"{k}={v}" for k, v in optional.items()),
                        required=False))

    # GPU / CUDA
    gpu_ok = False
    gpu_detail = "torch not installed"
    vram_gb = 0.0
    if _pkg("torch"):
        import torch
        gpu_ok = torch.cuda.is_available()
        if gpu_ok:
            vram_gb = torch.cuda.get_device_properties(0).total_memory / 1e9
            gpu_detail = (
                f"{torch.cuda.get_device_name(0)} | VRAM {vram_gb:.1f} GB | "
                f"torch {torch.__version__} CUDA {torch.version.cuda}")
        else:
            gpu_detail = f"CPU-only torch {torch.__version__} (CUDA unavailable)"
    checks.append(Check("gpu", gpu_ok or not require_gpu, gpu_detail,
                        required=require_gpu))

    if require_gpu:
        try:
            cfg = _load_yaml(REPO / "havyn-stage5/configs/models.yaml")
            _ = cfg
            min_vram = 12.0 if role == "standard" else 6.0
        except Exception:
            min_vram = 12.0 if role == "standard" else 6.0
        checks.append(Check(
            "vram", vram_gb >= min_vram,
            f"{vram_gb:.1f} GB available, {min_vram:.0f} GB recommended for {role}",
            required=True))

    # Hugging Face
    auth = hf_status()
    checks.append(Check("hf_token", auth.present or not require_hf,
                        f"present ({auth.source})" if auth.present else "absent",
                        required=require_hf))
    if check_hf_network and auth.present:
        try:
            from hf_auth import whoami
            who = whoami()
            checks.append(Check("hf_network", True, f"authenticated as {who['name']}"))
        except Exception as exc:  # noqa: BLE001
            checks.append(Check("hf_network", False, f"Hub check failed: {exc}"))
    else:
        checks.append(Check(
            "hf_network", True,
            "skipped (pass --check-hf-network to test the token)",
            required=False))

    # Publish safety
    publish = os.environ.get("HF_PUBLISH", "false").lower() in {"1", "true", "yes", "on"}
    checks.append(Check(
        "publish_disabled", not publish or allow_publish,
        "HF_PUBLISH is false (safe)" if not publish else
        "HF_PUBLISH=true is set; uploads still need --publish",
        required=False))

    # Disk space (5 GB headroom for adapters/datasets; not model weights)
    try:
        free = shutil.disk_usage(REPO).free / 1e9
        checks.append(Check("disk", free >= 5.0, f"{free:.1f} GB free under {REPO}"))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("disk", False, f"cannot stat disk: {exc}"))

    return checks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-gpu", action="store_true")
    parser.add_argument("--require-hf", action="store_true")
    parser.add_argument("--check-hf-network", action="store_true")
    parser.add_argument("--role", choices=["standard", "lite"], default="standard")
    parser.add_argument("--allow-publish", action="store_true",
                        help="do not warn when HF_PUBLISH=true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    checks = run(args.require_gpu, args.require_hf, args.check_hf_network,
                 args.role, args.allow_publish)
    failed_required = [c for c in checks if c.required and not c.ok]

    if args.json:
        print(json.dumps({
            "passed": not failed_required,
            "checks": [c.__dict__ for c in checks],
        }, indent=2))
    else:
        for check in checks:
            mark = "OK  " if check.ok else ("FAIL" if check.required else "WARN")
            print(f"[{mark}] {check.name}: {check.detail}")
        print("PREFLIGHT PASS" if not failed_required else "PREFLIGHT FAIL")
    if failed_required:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
