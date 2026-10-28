"""Colab environment report: everything a Stage 5 run must record to be
reproducible. Writes a JSON env report the notebooks save next to their
artifacts. No secrets are read or written.
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
from datetime import datetime, timezone


def git_commit(repo_dir: str) -> str | None:
    try:
        return subprocess.run(
            ["git", "-C", repo_dir, "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:
        return None


def gpu_info() -> dict:
    info = {"cuda_available": False}
    try:
        import torch

        info["torch_version"] = torch.__version__
        info["cuda_available"] = torch.cuda.is_available()
        if torch.cuda.is_available():
            props = torch.cuda.get_device_properties(0)
            info["gpu_name"] = props.name
            info["vram_gb"] = round(props.total_memory / 1e9, 2)
            info["cuda_version"] = torch.version.cuda
            info["gpu_count"] = torch.cuda.device_count()
    except Exception as exc:
        info["error"] = str(exc)
    return info


def build_report(repo_dir: str, dataset_version: str | None = None) -> dict:
    report = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python_version": sys.version,
        "platform": platform.platform(),
        "git_commit": git_commit(repo_dir),
        "gpu": gpu_info(),
        "packages": {},
    }
    for package in (
        "torch", "transformers", "trl", "peft", "unsloth",
        "datasets", "accelerate", "bitsandbytes", "pydantic",
    ):
        try:
            report["packages"][package] = __import__(package).__version__
        except Exception:
            report["packages"][package] = None
    if dataset_version:
        report["dataset_version"] = dataset_version
    return report


def recommend_config(gpu: dict) -> dict:
    """The safest supported training config for the detected GPU.

    Never silently fails on low VRAM: downgrades deterministically and says
    so in the report.
    """
    vram = gpu.get("vram_gb", 0)
    if not gpu.get("cuda_available"):
        return {"model": "none", "reason": "no CUDA GPU; benchmark/eval only"}
    if vram >= 20:
        return {"model": "qwen3-8b", "load_in_4bit": True, "max_seq_len": 4096,
                "batch": 2, "grad_accum": 4}
    if vram >= 12:
        return {"model": "qwen3-4b", "load_in_4bit": True, "max_seq_len": 2048,
                "batch": 2, "grad_accum": 8}
    return {"model": "qwen3-4b", "load_in_4bit": True, "max_seq_len": 1024,
            "batch": 1, "grad_accum": 16,
            "reason": f"{vram} GB VRAM: safest config, longer runtime expected"}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".")
    parser.add_argument("--out", default="env-report.json")
    parser.add_argument("--dataset-version", default=None)
    args = parser.parse_args()
    report = build_report(args.repo, args.dataset_version)
    report["recommended_training_config"] = recommend_config(report["gpu"])
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
    print(json.dumps(report, indent=2))
