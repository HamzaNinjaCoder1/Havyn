"""Shared Colab notebook UX: DRY_RUN, artifacts root, GPU gates, warnings."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "havyn-stage5" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _flag(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in {
        "1", "true", "yes", "on",
    }


def dry_run() -> bool:
    """DRY_RUN=true validates the pipeline without spending GPU."""
    return _flag("DRY_RUN")


def is_colab() -> bool:
    return "COLAB_GPU" in os.environ or Path("/content").exists()


def artifacts_root() -> Path:
    """Drive when configured, otherwise runtime storage."""
    explicit = os.environ.get("HAVYN_DRIVE") or os.environ.get("HAVYN_RUNTIME")
    if explicit:
        return Path(explicit)
    if is_colab() and Path("/content/drive/MyDrive").exists():
        return Path("/content/drive/MyDrive/havyn-stage5")
    return Path("/content/havyn-artifacts") if is_colab() else REPO / "havyn-stage5" / "artifacts"


def ensure_dir(path: str | Path) -> Path:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def banner(text: str) -> None:
    line = "=" * 72
    print(f"\n{line}\n{text}\n{line}\n")


def warn_expensive(operation: str, estimate: str) -> None:
    banner(f"WARNING: THIS CELL STARTS {operation.upper()}.\n{estimate}")


def gate(stage: str, role: str = "standard", require_hf: bool = True) -> None:
    """Block expensive stages until preflight passes; dry-run stays offline.

    Raises SystemExit when the gate fails, so a notebook stops before spending
    GPU time on a broken environment.
    """
    if dry_run():
        banner(f"DRY_RUN=true: validating '{stage}' without GPU or model download")
        cmd = [sys.executable, str(SCRIPTS / "dry_run_check.py"), "--stage", stage]
        subprocess.run(cmd, check=True, cwd=REPO,
                       env={**os.environ, "PYTHONPATH": f"{REPO}{os.pathsep}{SCRIPTS}"})
        banner(f"DRY_RUN '{stage}' PASSED - real run not started")
        return

    cmd = [sys.executable, str(SCRIPTS / "colab_preflight.py"),
           "--require-gpu", "--role", role]
    if require_hf:
        cmd.append("--require-hf")
    subprocess.run(cmd, check=True, cwd=REPO,
                   env={**os.environ, "PYTHONPATH": f"{REPO}{os.pathsep}{SCRIPTS}"})
