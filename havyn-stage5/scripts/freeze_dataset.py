"""S5.16 dataset freeze: hash the frozen splits and pin the manifest.

Freeze = the moment the dataset stops changing. This writes FROZEN.json
with the SHA-256 of every split file plus the audit report and dataset
card; ``--check`` re-verifies: any byte changed since the freeze (a new
generation pass, an accidental rewrite) breaks the hash and the check
fails. Training (train_lora.py) consumes the frozen directory as-is.

The gold set (gold-v1) is deliberately NOT frozen here - it is private,
owner-authored and never enters the training pipeline; its verification
lives in validate_gold.py.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

FROZEN_FILES = ("train.jsonl", "validation.jsonl", "heldout.jsonl",
                "audit-report.json", "dataset-card.json")


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def freeze(dataset_dir: Path) -> dict:
    hashes = {}
    for name in FROZEN_FILES:
        path = dataset_dir / name
        if not path.exists():
            raise SystemExit(f"cannot freeze: missing {path}")
        hashes[name] = sha256_file(path)
    manifest = {
        "stage": "S5.16",
        "dataset": dataset_dir.name,
        "frozen_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sha256": hashes,
    }
    (dataset_dir / "FROZEN.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def check(dataset_dir: Path) -> list[str]:
    manifest_path = dataset_dir / "FROZEN.json"
    if not manifest_path.exists():
        return [f"no FROZEN.json in {dataset_dir} - dataset is not frozen"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    breaks = []
    for name, expected in manifest.get("sha256", {}).items():
        path = dataset_dir / name
        if not path.exists():
            breaks.append(f"{name}: MISSING")
        elif sha256_file(path) != expected:
            breaks.append(f"{name}: hash changed since freeze")
    return breaks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset_dir",
                        default="havyn-stage5/datasets/havyn-train-v1",
                        nargs="?")
    parser.add_argument("--check", action="store_true",
                        help="verify the freeze instead of creating it")
    args = parser.parse_args()
    dataset_dir = Path(args.dataset_dir)
    if args.check:
        breaks = check(dataset_dir)
        if breaks:
            print(json.dumps({"frozen": False, "breaks": breaks}, indent=2))
            sys.exit(1)
        print(json.dumps({"frozen": True, "dataset": str(dataset_dir)},
                         indent=2))
        return
    manifest = freeze(dataset_dir)
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
