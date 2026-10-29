"""Build a private-Hugging-Face-ready snapshot of the frozen training set.

This script never uploads anything. It creates a self-contained directory:

    havyn-stage5/datasets/hf-export/<version>/
        README.md              dataset card (from the template)
        LICENSE                repository MIT licence
        data/train.jsonl
        data/validation.jsonl
        data/heldout.jsonl
        manifest.json          counts, provenance, engine pins, SHA-256
        gold-exclusion.json    Gold prompt hashes + exclusion statement

It refuses to run if the source is not frozen or its bytes changed.

Usage:
    python prepare_hf_dataset.py --dry-run
    python prepare_hf_dataset.py
    python prepare_hf_dataset.py --source <dir> --out <dir> --version <name>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for p in (str(REPO), str(REPO / "havyn-stage5" / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from freeze_dataset import check as check_frozen  # noqa: E402

DEFAULT_SOURCE = REPO / "havyn-stage5" / "datasets" / "havyn-train-v1"
DEFAULT_OUT = REPO / "havyn-stage5" / "datasets" / "hf-export"
TEMPLATE = REPO / "havyn-stage5" / "datasets" / "hf-dataset-card-template.md"
GOLD = REPO / "havyn-stage5" / "datasets" / "gold-v1"
LICENSE = REPO / "LICENSE"

PROVENANCE = {
    "origin": "synthetic_ai",
    "human_contributor": False,
    "consent": "not_applicable",
    "owner_authored": False,
    "gold_records_included": 0,
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def count_rows(path: Path) -> int:
    with path.open(encoding="utf-8") as fh:
        return sum(1 for line in fh if line.strip())


def _version(freeze: dict) -> str:
    train_sha = freeze["sha256"]["train.jsonl"]
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"havyn-sft-{day}-{train_sha[:8]}"


def _task_table(audit: dict) -> str:
    lines = ["| Task | Examples | Train | Validation | Heldout |",
             "| --- | --- | --- | --- | --- |"]
    for task, data in audit.get("per_task", {}).items():
        counts = data.get("counts", {})
        lines.append(
            f"| {task} | {data.get('examples', 0)} | {counts.get('train', 0)} | "
            f"{counts.get('validation', 0)} | {counts.get('heldout', 0)} |"
        )
    return "\n".join(lines)


def _gold_exclusion() -> dict:
    hashes = {}
    for path in sorted(GOLD.glob("gold-*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        prompt = payload.get("prompt")
        if prompt:
            hashes[path.stem] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    return {
        "statement": (
            "datasets/gold-v1 is private owner evaluation data. It is not part "
            "of this dataset and must never be used for training. These hashes "
            "are checked against every split by validate_training_split.py."
        ),
        "gold_records": len(hashes),
        "gold_prompt_sha256": hashes,
    }


def build(source: Path, out_root: Path, version: str | None, dry_run: bool) -> dict:
    breaks = check_frozen(source)
    if breaks:
        raise SystemExit(
            "refusing to export an unfrozen or byte-changed dataset: " + "; ".join(breaks)
        )
    freeze = json.loads((source / "FROZEN.json").read_text(encoding="utf-8"))
    audit = json.loads((source / "audit-report.json").read_text(encoding="utf-8"))
    version = version or _version(freeze)
    out = out_root / version

    files = {name: source / f"{name}.jsonl"
             for name in ("train", "validation", "heldout")}
    for path in files.values():
        if not path.exists():
            raise SystemExit(f"missing split: {path}")

    manifest = {
        "dataset": version,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provenance": PROVENANCE,
        "source_directory": str(source),
        "freeze": {
            "frozen_at": freeze.get("frozen_at"),
            "sha256": freeze.get("sha256", {}),
            "manifest_sha256": sha256_file(source / "FROZEN.json"),
        },
        "row_counts": {name: count_rows(path) for name, path in files.items()},
        "per_task": audit.get("per_task", {}),
        "engine_commits": audit.get("engine_commits", []),
        "verification_commit": audit.get("verification_commit"),
        "gold_records_included": 0,
        "license": "MIT (repository); synthetic content, no human authorship claimed",
    }

    if dry_run:
        print(json.dumps({
            "dry_run": True,
            "version": version,
            "out": str(out),
            "row_counts": manifest["row_counts"],
            "provenance": PROVENANCE,
            "would_write": ["README.md", "LICENSE", "manifest.json",
                            "gold-exclusion.json", "data/*.jsonl"],
        }, indent=2))
        return manifest

    if out.exists():
        raise SystemExit(
            f"{out} already exists; choose a new --version or remove it deliberately"
        )
    (out / "data").mkdir(parents=True)

    for name, path in files.items():
        shutil.copyfile(path, out / "data" / f"{name}.jsonl")

    card = TEMPLATE.read_text(encoding="utf-8")
    card = card.replace("{{VERSION}}", version)
    card = card.replace("{{TRAIN_ROWS}}", str(manifest["row_counts"]["train"]))
    card = card.replace("{{VALIDATION_ROWS}}", str(manifest["row_counts"]["validation"]))
    card = card.replace("{{HELDOUT_ROWS}}", str(manifest["row_counts"]["heldout"]))
    card = card.replace("{{TASK_TABLE}}", _task_table(audit))
    (out / "README.md").write_text(card, encoding="utf-8")

    shutil.copyfile(LICENSE, out / "LICENSE")
    (out / "gold-exclusion.json").write_text(
        json.dumps(_gold_exclusion(), indent=2), encoding="utf-8")

    manifest["file_sha256"] = {
        name: sha256_file(out / "data" / f"{name}.jsonl")
        for name in files
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=str(DEFAULT_SOURCE))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--version", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    manifest = build(Path(args.source), Path(args.out), args.version, args.dry_run)
    if not args.dry_run:
        print(json.dumps({
            "dataset": manifest["dataset"],
            "row_counts": manifest["row_counts"],
            "provenance": manifest["provenance"],
            "out": str(Path(args.out) / manifest["dataset"]),
        }, indent=2))


if __name__ == "__main__":
    main()
