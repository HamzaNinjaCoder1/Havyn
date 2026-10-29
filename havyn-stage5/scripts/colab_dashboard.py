"""Beginner-facing Colab dashboard: the ten facts before any expensive cell.

Prints GPU, VRAM, Python, commit, dataset version, model configuration, HF
authentication status (never the token), the next operation, and a storage
estimate. No network calls, no model downloads, no GPU work.

Usage:
    python colab_dashboard.py --next "03 model bake-off"
    python colab_dashboard.py --json
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for p in (str(REPO), str(REPO / "havyn-stage5" / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from hf_auth import status as hf_status  # noqa: E402

REGISTRY = REPO / "havyn-stage5" / "configs" / "models.yaml"
DATASET = REPO / "havyn-stage5" / "datasets" / "havyn-train-v1"


def _git_head() -> str:
    try:
        return subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                              capture_output=True, text=True,
                              check=True).stdout.strip()[:12]
    except Exception:
        return "unknown"


def _gpu() -> dict:
    try:
        import torch
    except ImportError:
        return {"detected": False, "name": None, "vram_gb": None,
                "detail": "torch not installed"}
    if not torch.cuda.is_available():
        return {"detected": False, "name": None, "vram_gb": None,
                "detail": f"CPU-only torch {torch.__version__}"}
    props = torch.cuda.get_device_properties(0)
    return {
        "detected": True,
        "name": props.name,
        "vram_gb": round(props.total_memory / 1e9, 1),
        "detail": f"torch {torch.__version__} CUDA {torch.version.cuda}",
    }


def _dataset() -> dict:
    frozen = DATASET / "FROZEN.json"
    audit = DATASET / "audit-report.json"
    if not frozen.exists():
        return {"version": None, "frozen": False, "rows": None,
                "detail": "FROZEN.json absent"}
    payload = json.loads(frozen.read_text(encoding="utf-8"))
    rows = None
    if audit.exists():
        rows = json.loads(audit.read_text(encoding="utf-8")).get("total")
    train_sha = payload.get("sha256", {}).get("train.jsonl", "")
    return {
        "version": f"havyn-train-v1 ({train_sha[:8]})",
        "frozen": True,
        "rows": rows,
        "frozen_at": payload.get("frozen_at"),
    }


def _models() -> dict:
    try:
        import yaml
        cfg = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"error": f"cannot read registry: {exc}"}
    return {
        "standard_candidates": [c["model_id"] for c in cfg.get("standard_candidates", [])],
        "lite_candidates": [c["model_id"] for c in cfg.get("lite_candidates", [])],
        "teacher": cfg.get("teacher", {}).get("model_id"),
        "review": cfg.get("review", {}).get("model_id"),
        "selection_status": cfg.get("selection_status"),
    }


def _storage(models: dict) -> dict:
    dataset_bytes = sum(
        p.stat().st_size for p in DATASET.glob("*.jsonl") if p.is_file()
    )
    # Rough planning numbers, not measurements: 4-bit weights ~0.55 GB/B.
    standard = 5.0   # 8B at 4-bit
    lite = 2.5       # 4B at 4-bit
    teacher = 18.0   # 32B at 4-bit, optional
    review = 14.0    # 24B at 4-bit, optional
    checkpoints = 6.0
    artifacts = 4.0
    return {
        "dataset_gb": round(dataset_bytes / 1e9, 2),
        "base_models_gb": {"standard": standard, "lite": lite,
                           "teacher_optional": teacher, "review_optional": review},
        "checkpoints_gb": checkpoints,
        "artifacts_gb": artifacts,
        "minimum_free_gb": round(dataset_bytes / 1e9 + standard + lite + checkpoints + artifacts, 1),
        "with_teacher_review_free_gb": round(
            dataset_bytes / 1e9 + standard + lite + teacher + review
            + checkpoints + artifacts, 1),
        "note": "planning estimates only; measure actual model sizes with hf_smoke_test.py",
    }


def build(next_operation: str) -> dict:
    gpu = _gpu()
    auth = hf_status()
    models = _models()
    return {
        "gpu_detected": gpu,
        "python": platform.python_version(),
        "repo_commit": _git_head(),
        "dataset": _dataset(),
        "models": models,
        "hf_auth": auth.to_dict(),
        "next_operation": next_operation,
        "storage": _storage(models),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--next", default="not specified")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    data = build(args.next)
    if args.json:
        print(json.dumps(data, indent=2))
        return

    gpu = data["gpu_detected"]
    print("Havyn Colab dashboard")
    print("-" * 60)
    print(f"1. GPU detected      : {'YES' if gpu['detected'] else 'NO'}")
    print(f"2. GPU name          : {gpu['name'] or 'n/a'} ({gpu['detail']})")
    print(f"3. VRAM              : {gpu['vram_gb'] or 'n/a'} GB")
    print(f"4. Python            : {data['python']}")
    print(f"5. Repository commit : {data['repo_commit']}")
    ds = data["dataset"]
    print(f"6. Dataset version   : {ds['version'] or 'not frozen'} "
          f"({ds['rows']} rows, frozen={ds['frozen']})")
    models = data["models"]
    print(f"7. Model config      : standard={len(models.get('standard_candidates', []))} "
          f"candidates, lite={len(models.get('lite_candidates', []))}, "
          f"teacher={models.get('teacher')}, review={models.get('review')}")
    auth = data["hf_auth"]
    print(f"8. HF authentication : {'present' if auth['present'] else 'absent'} "
          f"(source={auth['source']}, token=hidden)")
    print(f"9. Next operation    : {data['next_operation']}")
    storage = data["storage"]
    print(f"10. Storage estimate : dataset {storage['dataset_gb']} GB, "
          f"min free {storage['minimum_free_gb']} GB, "
          f"with teacher+review {storage['with_teacher_review_free_gb']} GB")
    print("-" * 60)
    if not gpu["detected"]:
        print("No GPU: only 01/02/dataset checks and DRY_RUN stages can run here.")


if __name__ == "__main__":
    main()
