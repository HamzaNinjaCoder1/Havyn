"""One-shot Colab environment setup for Havyn Stage 5.

Run from the first cell of any notebook:

    from colab_setup import setup
    env = setup()   # or setup(run_tests=False) to skip the offline suite

The function is intentionally verbose about what it verified: notebooks must
never train on top of an environment that silently skipped a step. Everything
is recorded in the returned report and re-exported by scripts/env_report.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO_URL = "https://github.com/HamzaNinjaCoder1/MCP-Packet-Tracer.git"
REPO_DIR = Path("/content/MCP-Packet-Tracer")


@dataclass
class SetupReport:
    started: str
    python_version: str
    repo_dir: str
    git_commit: str | None = None
    pip_editable_installed: bool = False
    tests_passed: bool | None = None
    test_summary: str | None = None
    drive_mounted: bool = False
    drive_dir: str | None = None
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def ensure_repo(token_env: str = "GITHUB_TOKEN") -> Path:
    """Clone (or update) the Havyn repository into /content.

    A private repository needs a fine-grained GitHub token supplied through
    the Colab Secrets manager (notebook: `from google.colab import userdata;
    userdata.get('GITHUB_TOKEN')` exported into the environment before this
    call). Tokens are read from the environment only - never hard-coded.
    """
    if REPO_DIR.exists() and (REPO_DIR / "pyproject.toml").exists():
        proc = _run(["git", "-C", str(REPO_DIR), "pull", "--ff-only"])
        if proc.returncode != 0:
            print(f"[setup] git pull failed (continuing with checkout): {proc.stderr.strip()}")
        return REPO_DIR

    env = dict(os.environ)
    token = env.get(token_env)
    if token:
        url = REPO_URL.replace("https://", f"https://x-access-token:{token}@")
        proc = _run(["git", "clone", "--depth", "1", url, str(REPO_DIR)], env=env)
    else:
        proc = _run(["git", "clone", "--depth", "1", REPO_URL, str(REPO_DIR)])
    if proc.returncode != 0:
        raise RuntimeError(f"git clone failed:\n{proc.stderr}")
    # Never leave the token inside .git/config.
    _run(["git", "-C", str(REPO_DIR), "remote", "set-url", "origin", REPO_URL])
    return REPO_DIR


def install_package(repo: Path) -> None:
    proc = _run(
        [sys.executable, "-m", "pip", "install", "-q", "-e", f"{repo}[test]"],
        cwd=repo,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"pip install failed:\n{proc.stderr[-2000:]}")
    os.environ["PYTHONPATH"] = f"{repo}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"


def run_offline_suite(repo: Path, timeout: int = 1200) -> tuple[bool, str]:
    proc = _run(
        [sys.executable, "-m", "pytest", "-q", "--no-header", "-x",
         "--ignore=tests/autopilot/test_live_preflight.py",
         "tests"],
        cwd=repo,
        timeout=timeout,
    )
    summary = (proc.stdout or "").strip().splitlines()[-1:] or ["(no output)"]
    return proc.returncode == 0, summary[0]


def mount_drive() -> Path:
    """Mount Google Drive and return the Havyn artifacts directory."""
    from google.colab import drive  # type: ignore[import-not-found]

    drive.mount("/content/drive")
    root = Path("/content/drive/MyDrive/havyn-stage5")
    root.mkdir(parents=True, exist_ok=True)
    return root


def setup(run_tests: bool = True, use_drive: bool = True) -> SetupReport:
    report = SetupReport(
        started=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        python_version=sys.version.split()[0],
        repo_dir=str(REPO_DIR),
    )
    try:
        repo = ensure_repo()
        print(f"[setup] repository at {repo}")
    except RuntimeError as exc:
        report.errors.append(str(exc))
        print(f"[setup] FAILED: {exc}")
        return report

    git_commit = _run(["git", "-C", str(repo), "rev-parse", "HEAD"]).stdout.strip()
    report.git_commit = git_commit or None
    print(f"[setup] commit {git_commit[:12]}")

    try:
        install_package(repo)
        report.pip_editable_installed = True
        print("[setup] package installed (editable, [test] extras)")
    except RuntimeError as exc:
        report.errors.append(str(exc))
        print(f"[setup] FAILED: {exc}")
        return report

    if run_tests:
        passed, summary = run_offline_suite(repo)
        report.tests_passed = passed
        report.test_summary = summary
        print(f"[setup] offline suite: {'PASS' if passed else 'FAIL'} ({summary})")
        if not passed:
            report.errors.append("offline test suite failed; do not train on this env")

    if use_drive:
        try:
            drive_dir = mount_drive()
            report.drive_mounted = True
            report.drive_dir = str(drive_dir)
            print(f"[setup] drive artifacts at {drive_dir}")
        except Exception as exc:  # noqa: BLE001 - notebook context, report it
            report.drive_mounted = False
            report.errors.append(f"drive mount failed: {exc}")
            print(f"[setup] drive mount failed: {exc}")

    return report


def save_report(report: SetupReport, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "setup-report.json"
    path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    print(f"[setup] report written to {path}")
    return path


if __name__ == "__main__":
    rep = setup()
    if rep.errors:
        sys.exit(1)
