"""S5.17/S5.18 LoRA fine-tuning (Standard Qwen3-8B, Lite Qwen3-4B).

GPU path runs on Colab (Unsloth fast path, transformers+PEFT+TRL fallback);
this module is import-safe without a GPU so the data half can be verified
offline. The GPU half itself is NOT verifiable offline (repo rule 10): the
first real run happens in the Colab notebook and its report is the evidence.

Usage (see notebooks/09-training.ipynb):
  python train_lora.py --config havyn-stage5/configs/train-standard.yaml \
      --resume --output-dir "$HAVYN_DRIVE/checkpoints"

Training must survive a runtime disconnect: checkpoints go to Drive, the
trainer keeps save_total_limit > 1 and ``--resume`` picks the newest
checkpoint back up.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for p in (str(REPO), str(REPO / "havyn-stage5" / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from format_examples import TASKS, load_config, load_training_data  # noqa: E402


def _git_pin() -> str | None:
    try:
        head = subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(REPO), "status", "--porcelain", "--", "src"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return f"{head}+dirty-src" if dirty else head
    except Exception:
        return None


def _sha256_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            while chunk := fh.read(1 << 20):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _freeze_info(dataset_dir: Path) -> dict:
    frozen = dataset_dir / "FROZEN.json"
    if not frozen.exists():
        return {"frozen": False, "manifest_sha256": None, "frozen_at": None}
    payload = json.loads(frozen.read_text(encoding="utf-8"))
    return {
        "frozen": True,
        "manifest_sha256": _sha256_file(frozen),
        "frozen_at": payload.get("frozen_at"),
        "file_sha256": payload.get("sha256", {}),
    }


def adjust_for_vram(train_cfg: dict, free_bytes: int,
                    weights_bytes: int = 0) -> tuple[dict, str]:
    """Safest-config fallback: halve the batch and double accumulation until
    the per-step batch plausibly fits the free VRAM that remains after the
    quantised base model is loaded. Never raises; reports every change."""
    note = "no adjustment needed"
    batch = int(train_cfg.get("per_device_train_batch_size", 2))
    accum = int(train_cfg.get("gradient_accumulation_steps", 8))
    # ~0.5 GB per sequence at 4-bit 8B with activation checkpointing; keep
    # 60% headroom for optimizer state spikes.
    per_seq_bytes = 0.5 * 1e9
    if free_bytes <= weights_bytes:
        note = (
            f"base model weights (~{weights_bytes / 1e9:.1f} GB) do not fit in "
            f"{free_bytes / 1e9:.1f} GB free VRAM; preflight must block training"
        )
        budget = 0
    else:
        budget = int((free_bytes - weights_bytes) * 0.6)
    while batch > 1 and batch * per_seq_bytes > budget:
        batch //= 2
        accum *= 2
        note = (
            f"batch halved to {batch}, accumulation doubled to {accum} after "
            f"reserving {weights_bytes / 1e9:.1f} GB for weights"
        )
    if batch == 1 and batch * per_seq_bytes > budget and budget > 0:
        note += "; one sequence still exceeds the remaining budget"
    train_cfg["per_device_train_batch_size"] = batch
    train_cfg["gradient_accumulation_steps"] = accum
    return train_cfg, note


def build_sft_dataset(examples: list[dict], tokenizer: Any) -> list[dict]:
    """Render chat messages through the tokenizer's template for TRL."""
    rendered = []
    for ex in examples:
        text = tokenizer.apply_chat_template(
            ex["messages"], tokenize=False, add_generation_prompt=False)
        rendered.append({"text": text})
    return rendered


def latest_checkpoint(checkpoint_dir: Path) -> Path | None:
    """Newest trainer checkpoint by step number (never lexicographic:
    checkpoint-1000 sorts before checkpoint-250 as a string)."""
    def _step(path: Path) -> int:
        try:
            return int(path.name.rsplit("-", 1)[1])
        except (IndexError, ValueError):
            return -1
    candidates = sorted(
        (p for p in checkpoint_dir.glob("checkpoint-*") if p.is_dir()),
        key=_step)
    return candidates[-1] if candidates else None


def train(config_path: str, resume: bool, output_dir: str | None,
          dataset_dir: str | None) -> dict:
    cfg = load_config(config_path)
    train_cfg = cfg["train"]
    seed = int(train_cfg.get("seed", 20261028))
    root = Path(dataset_dir or train_cfg["dataset_dir"])
    # S5.16: training consumes the frozen bytes or it does not start.
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from freeze_dataset import check as check_frozen

        breaks = check_frozen(root)
    except Exception as exc:  # noqa: BLE001
        breaks = [f"freeze check unavailable: {exc}"]
    if breaks:
        raise SystemExit(
            f"refusing to train on an unfrozen or byte-changed dataset ({root}): "
            + "; ".join(breaks)
        )
    freeze = _freeze_info(root)
    examples, val, stats = load_training_data(root, seed)

    checkpoint_dir = Path(output_dir) if output_dir else Path(cfg["output"]["checkpoint_dir"])
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    report_dir = Path(cfg["output"]["report_dir"])
    report_dir.mkdir(parents=True, exist_ok=True)

    missing = [t for t in TASKS if t not in stats["train_task_counts"]]
    report = {
        "stage": "S5.17/S5.18",
        "role": cfg.get("role"),
        "base_model": cfg.get("base_model"),
        "config": str(config_path),
        "config_sha256": _sha256_file(Path(config_path)),
        "config_values": cfg,
        "code_commit": _git_pin(),
        "dataset": str(root),
        "dataset_version": (
            f"{root.name}@{freeze['manifest_sha256'][:12]}"
            if freeze.get("manifest_sha256") else root.name
        ),
        "dataset_freeze": freeze,
        "dataset_stats": stats,
        "missing_tasks": missing,
        "seed": seed,
        "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "started",
    }
    (report_dir / "training-report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8")

    # GPU half: imports happen here so the offline half never needs torch.
    try:
        import torch
    except ImportError:
        report["status"] = "blocked"
        report["reason"] = "torch not installed - GPU training runs on Colab"
        (report_dir / "training-report.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report["reason"]))
        return report

    missing = [
        name for name in ("transformers", "trl", "peft", "datasets", "accelerate")
        if importlib.util.find_spec(name) is None
    ]
    if missing:
        raise SystemExit(
            "missing training packages: " + ", ".join(missing) +
            ". Install them first: pip install -r "
            "havyn-stage5/requirements-colab.txt"
        )

    try:
        from unsloth import FastLanguageModel
        _unsloth = True
    except ImportError:
        _unsloth = False

    weights_bytes = int(float(cfg.get("model_weight_gb", 0)) * 1e9)
    if torch.cuda.is_available():
        free_bytes, _total = torch.cuda.mem_get_info()
        if weights_bytes and free_bytes <= weights_bytes:
            raise SystemExit(
                f"not enough free VRAM to load {cfg['base_model']}: "
                f"{free_bytes / 1e9:.1f} GB free, ~{weights_bytes / 1e9:.1f} GB "
                "needed for the quantised weights. Use a smaller role or a "
                "larger GPU; the script will not silently degrade the model."
            )
        train_cfg, vram_note = adjust_for_vram(train_cfg, free_bytes, weights_bytes)
        report["free_vram_gb"] = round(free_bytes / 1e9, 2)
        report["model_weight_gb"] = round(weights_bytes / 1e9, 2)
        report["vram_adjustment"] = vram_note

    if _unsloth:
        from unsloth import FastLanguageModel
        model, tokenizer = FastLanguageModel.from_pretrained(
            model_name=cfg["base_model"],
            max_seq_length=int(cfg["max_seq_length"]),
            load_in_4bit=bool(cfg.get("load_in_4bit", True)),
        )
        model = FastLanguageModel.get_peft_model(
            model,
            r=int(cfg["lora"]["r"]),
            lora_alpha=int(cfg["lora"]["lora_alpha"]),
            lora_dropout=float(cfg["lora"]["lora_dropout"]),
            target_modules=list(cfg["lora"]["target_modules"]),
            bias=cfg["lora"].get("bias", "none"),
            use_gradient_checkpointing="unsloth",
        )
    else:
        from peft import LoraConfig, get_peft_model
        from transformers import AutoModelForCausalLM, AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained(cfg["base_model"])
        quant_config = None
        if cfg.get("load_in_4bit", True) and torch.cuda.is_available():
            from transformers import BitsAndBytesConfig

            quant_config = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=torch.bfloat16,
                bnb_4bit_use_double_quant=True,
            )
        model = AutoModelForCausalLM.from_pretrained(
            cfg["base_model"],
            torch_dtype="auto",
            device_map="auto",
            quantization_config=quant_config,
        )
        model = get_peft_model(model, LoraConfig(
            r=int(cfg["lora"]["r"]),
            lora_alpha=int(cfg["lora"]["lora_alpha"]),
            lora_dropout=float(cfg["lora"]["lora_dropout"]),
            target_modules=list(cfg["lora"]["target_modules"]),
            bias=cfg["lora"].get("bias", "none"),
            task_type="CAUSAL_LM",
        ))

    from trl import SFTConfig, SFTTrainer

    sft_cfg = SFTConfig(
        output_dir=str(checkpoint_dir),
        num_train_epochs=int(train_cfg["epochs"]),
        per_device_train_batch_size=int(train_cfg["per_device_train_batch_size"]),
        gradient_accumulation_steps=int(train_cfg["gradient_accumulation_steps"]),
        learning_rate=float(train_cfg["learning_rate"]),
        lr_scheduler_type=train_cfg.get("lr_scheduler", "cosine"),
        warmup_ratio=float(train_cfg.get("warmup_ratio", 0.05)),
        weight_decay=float(train_cfg.get("weight_decay", 0.01)),
        bf16=bool(train_cfg.get("bf16", True)),
        seed=seed,
        save_steps=int(train_cfg.get("save_steps", 250)),
        save_total_limit=int(train_cfg.get("save_total_limit", 3)),
        logging_steps=10,
        report_to=[],
        dataset_text_field="text",
        max_length=int(cfg["max_seq_length"]),
    )
    train_ds = build_sft_dataset(examples, tokenizer)
    val_ds = build_sft_dataset(val, tokenizer)
    trainer = SFTTrainer(
        model=model,
        args=sft_cfg,
        train_dataset=train_ds,
        eval_dataset=val_ds,
        processing_class=tokenizer,
    )
    resume_from = None
    if resume:
        found = latest_checkpoint(checkpoint_dir)
        if found:
            resume_from = str(found)
            report["resumed_from"] = resume_from
    trainer.train(resume_from_checkpoint=resume_from)
    model.save_pretrained(str(checkpoint_dir / "adapter"))
    tokenizer.save_pretrained(str(checkpoint_dir / "adapter"))

    report["status"] = "completed"
    report["finished"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report["checkpoint_dir"] = str(checkpoint_dir)
    report["latest_checkpoint"] = (
        str(found) if (found := latest_checkpoint(checkpoint_dir)) else None
    )
    report["adapter_dir"] = str(checkpoint_dir / "adapter")
    metrics = getattr(trainer, "state", None)
    if metrics is not None and getattr(metrics, "log_history", None):
        report["final_loss"] = metrics.log_history[-1].get("train_loss")
    (report_dir / "training-report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--output-dir", default=None,
                        help="override the config checkpoint dir (Drive)")
    parser.add_argument("--dataset-dir", default=None,
                        help="override the frozen dataset directory")
    parser.add_argument("--dry-run", action="store_true",
                        help="format the data, write the report, do not train")
    args = parser.parse_args()

    if args.dry_run:
        cfg = load_config(args.config)
        root = Path(args.dataset_dir or cfg["train"]["dataset_dir"])
        examples, val, stats = load_training_data(
            root, int(cfg["train"].get("seed", 20261028)))
        print(json.dumps({"dry_run": True, "dataset_stats": stats}, indent=2))
        return
    report = train(args.config, args.resume, args.output_dir, args.dataset_dir)
    print(json.dumps({"status": report["status"]}, indent=2))
    if report["status"] == "blocked":
        sys.exit(2)


if __name__ == "__main__":
    main()
