"""Merge a LoRA adapter into its base model (GPU/CPU, prepared not run).

Training saves an adapter directory. GGUF export and a "merged model" upload
need a standalone model, so this step produces one and records the hashes.

Usage:
    python merge_lora.py --dry-run \
        --base-model Qwen/Qwen3-8B --adapter <adapter> --out <merged>
    python merge_lora.py \
        --base-model Qwen/Qwen3-8B --adapter <adapter> --out <merged>

Dry-run validates the adapter layout, prints the plan and touches nothing.
The real merge loads the base weights, so it needs enough RAM/VRAM for bf16.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _git_pin() -> str | None:
    try:
        head = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                              capture_output=True, text=True,
                              check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain",
                                "--", "src"], capture_output=True, text=True,
                               check=True).stdout.strip()
        return f"{head}+dirty-src" if dirty else head
    except Exception:
        return None


def merge(base_model: str, adapter: Path, out_dir: Path, dry_run: bool,
          trust_remote_code: bool = False) -> dict:
    if not adapter.is_dir():
        raise SystemExit(f"adapter directory not found: {adapter}")
    if not (adapter / "adapter_config.json").exists():
        raise SystemExit(
            f"{adapter} has no adapter_config.json; pass the directory "
            "train_lora.py saved (checkpoints/<role>/adapter)"
        )
    plan = {
        "base_model": base_model,
        "adapter": str(adapter),
        "out": str(out_dir),
        "dry_run": dry_run,
        "code_commit": _git_pin(),
    }
    if dry_run:
        print(json.dumps(plan, indent=2))
        return plan

    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(
        base_model, trust_remote_code=trust_remote_code)
    model = AutoModelForCausalLM.from_pretrained(
        base_model, torch_dtype="auto", device_map="auto",
        trust_remote_code=trust_remote_code)
    model = PeftModel.from_pretrained(model, str(adapter))
    merged = model.merge_and_unload()
    out_dir.mkdir(parents=True, exist_ok=True)
    merged.save_pretrained(str(out_dir), safe_serialization=True)
    tokenizer.save_pretrained(str(out_dir))

    plan["created"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    plan["files"] = {
        path.name: {"sha256": _sha256(path), "size_bytes": path.stat().st_size}
        for path in sorted(out_dir.iterdir())
        if path.is_file() and path.suffix in {".json", ".safetensors", ".model",
                                              ".txt", ".py"}
    }
    (out_dir / "merge-manifest.json").write_text(
        json.dumps(plan, indent=2), encoding="utf-8")
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-model", required=True)
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--trust-remote-code", action="store_true")
    args = parser.parse_args()

    plan = merge(args.base_model, Path(args.adapter), Path(args.out),
                 args.dry_run, args.trust_remote_code)
    if not args.dry_run:
        print(json.dumps({"merged": plan["out"], "files": list(plan["files"])},
                         indent=2))


if __name__ == "__main__":
    main()
