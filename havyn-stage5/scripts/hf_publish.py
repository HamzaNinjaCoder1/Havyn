"""Publish a dataset or model artifact to Hugging Face — DISABLED BY DEFAULT.

Two independent switches are required:
  1. environment HF_PUBLISH=true
  2. the --publish command-line flag

Without both, the script prints exactly what it would upload and exits 0.
It never prints the token. Repositories are created PRIVATE.

Usage (only after explicit authorisation):
    HF_PUBLISH=true python hf_publish.py --kind dataset \
        --repo-id <user>/<name> --path havyn-stage5/datasets/hf-export/<version> --publish
    HF_PUBLISH=true python hf_publish.py --kind model \
        --repo-id <user>/<name> --path <checkpoints+gguf dir> \
        --gate-report <eval>/gate-report.json --publish
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "havyn-stage5" / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "havyn-stage5" / "scripts"))

from hf_auth import get_token, status  # noqa: E402


def publish_enabled(cli_flag: bool) -> bool:
    env = os.environ.get("HF_PUBLISH", "false").strip().lower()
    return cli_flag and env in {"1", "true", "yes", "on"}


def _gate_release(path: str | None) -> str | None:
    if not path:
        return None
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return "UNREADABLE"
    return payload.get("gates", {}).get("release")


def _plan(kind: str, repo_id: str, path: Path, private: bool) -> dict:
    files = sum(1 for p in path.rglob("*") if p.is_file())
    size = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    return {
        "kind": kind,
        "repo_id": repo_id,
        "path": str(path),
        "files": files,
        "size_gb": round(size / 1e9, 3),
        "private": private,
        "hf_token": status().to_dict(),
        "upload": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=["dataset", "model"], required=True)
    parser.add_argument("--repo-id", required=True,
                        help="<owner>/<name>; the repository is created private")
    parser.add_argument("--path", required=True)
    parser.add_argument("--gate-report", default=None,
                        help="model uploads require a GO gate report")
    parser.add_argument("--allow-unverified", action="store_true",
                        help="allow a model upload with a NOT VERIFIED card")
    parser.add_argument("--publish", action="store_true",
                        help="second switch; HF_PUBLISH=true is still required")
    args = parser.parse_args()

    path = Path(args.path)
    if not path.exists():
        raise SystemExit(f"path does not exist: {path}")
    plan = _plan(args.kind, args.repo_id, path, private=True)

    if not publish_enabled(args.publish):
        plan["reason"] = (
            "publishing disabled: set HF_PUBLISH=true AND pass --publish to enable"
        )
        print(json.dumps(plan, indent=2))
        return

    if args.kind == "model":
        release = _gate_release(args.gate_report)
        plan["gate_release"] = release
        if release != "GO" and not args.allow_unverified:
            plan["reason"] = (
                "model upload refused: gate report is not GO. Re-run evaluation "
                "or pass --allow-unverified to publish with a NOT VERIFIED card."
            )
            print(json.dumps(plan, indent=2))
            raise SystemExit(1)
        if release != "GO":
            (path / "NOT_VERIFIED.md").write_text(
                "# NOT VERIFIED\n\nThis model has no PASS gate report. Do not "
                "present it as evaluated or release-ready.\n", encoding="utf-8")
            plan["model_card"] = "NOT_VERIFIED.md added"

    token = get_token(required=True)
    from huggingface_hub import HfApi

    api = HfApi(token=token)
    api.create_repo(repo_id=args.repo_id, repo_type=args.kind, private=True,
                    exist_ok=True)
    api.upload_folder(
        repo_id=args.repo_id,
        repo_type=args.kind,
        folder_path=str(path),
        commit_message="Havyn Stage 5 upload (explicitly enabled)",
    )
    plan["upload"] = True
    plan["url"] = f"https://huggingface.co/{args.kind}/{args.repo_id}"
    print(json.dumps(plan, indent=2))


if __name__ == "__main__":
    main()
