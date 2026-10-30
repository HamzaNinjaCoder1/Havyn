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
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
        return os.environ.get("HAVYN_CODE_COMMIT")


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


def latest_valid_checkpoint(checkpoint_dir: Path) -> Path | None:
    candidates = sorted(
        (p for p in checkpoint_dir.glob("checkpoint-*") if p.is_dir()),
        key=lambda p: int(p.name.rsplit("-", 1)[1])
        if p.name.rsplit("-", 1)[1].isdigit() else -1,
    )
    for candidate in reversed(candidates):
        try:
            json.loads((candidate / "trainer_state.json").read_text(
                encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (candidate / "adapter_model.safetensors").exists() or (
                candidate / "adapter_model.bin").exists():
            return candidate
    return None


def warmup_plan(train_size: int, batch: int, accumulation: int, epochs: int,
                ratio: float) -> dict:
    micro_batches_per_epoch = max(1, (train_size + batch - 1) // batch)
    updates_per_epoch = max(
        1, (micro_batches_per_epoch + accumulation - 1) // accumulation)
    total_updates = max(1, updates_per_epoch * epochs)
    steps = max(1, round(total_updates * ratio))
    return {
        "train_examples": train_size,
        "micro_batches_per_epoch": micro_batches_per_epoch,
        "updates_per_epoch": updates_per_epoch,
        "total_updates": total_updates,
        "warmup_steps": min(steps, max(1, total_updates - 1)),
    }


def train(config_path: str, resume: bool, output_dir: str | None,
          dataset_dir: str | None, preflight: bool = False,
          extra_callbacks: list | None = None) -> dict:
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
    train_limit = train_cfg.get("max_train_examples")
    val_limit = train_cfg.get("max_validation_examples")
    if train_limit:
        examples = examples[: int(train_limit)]
    if val_limit:
        val = val[: int(val_limit)]
    stats["train_used"] = len(examples)
    stats["validation_used"] = len(val)

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
        "status": "preflight" if preflight else "started",
    }
    if not preflight:
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

    use_bf16 = bool(train_cfg.get("bf16", True)) and torch.cuda.is_available()
    if use_bf16:
        capability = torch.cuda.get_device_capability(0)
        use_bf16 = bool(torch.cuda.is_bf16_supported()) and capability[0] >= 8
    compute_dtype = torch.bfloat16 if use_bf16 else torch.float16
    report["precision"] = "bf16" if use_bf16 else "fp16"
    report["device_capability"] = (
        ".".join(str(part) for part in torch.cuda.get_device_capability(0))
        if torch.cuda.is_available() else None)

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
        tokenizer = AutoTokenizer.from_pretrained(
            cfg["base_model"], trust_remote_code=True)
        if tokenizer.pad_token is None:
            tokenizer.pad_token = tokenizer.eos_token
        quant_config = None
        if cfg.get("load_in_4bit", True) and torch.cuda.is_available():
            from transformers import BitsAndBytesConfig

            quant_config = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=compute_dtype,
                bnb_4bit_use_double_quant=True,
            )
        model = AutoModelForCausalLM.from_pretrained(
            cfg["base_model"],
            torch_dtype="auto",
            device_map={"": 0},
            quantization_config=quant_config,
            trust_remote_code=True,
        )
        model = get_peft_model(model, LoraConfig(
            r=int(cfg["lora"]["r"]),
            lora_alpha=int(cfg["lora"]["lora_alpha"]),
            lora_dropout=float(cfg["lora"]["lora_dropout"]),
            target_modules=list(cfg["lora"]["target_modules"]),
            bias=cfg["lora"].get("bias", "none"),
            task_type="CAUSAL_LM",
        ))
        if bool(cfg["lora"].get("use_gradient_checkpointing", True)):
            model.gradient_checkpointing_enable(
                gradient_checkpointing_kwargs={"use_reentrant": False})
            model.enable_input_require_grads()
            model.config.use_cache = False

    from datasets import Dataset
    from trl import SFTConfig, SFTTrainer

    batch = int(train_cfg["per_device_train_batch_size"])
    accumulation = int(train_cfg["gradient_accumulation_steps"])
    epochs = int(train_cfg["epochs"])
    train_ds = Dataset.from_list(build_sft_dataset(examples, tokenizer))
    val_ds = Dataset.from_list(build_sft_dataset(val, tokenizer))
    plan = warmup_plan(
        len(train_ds), batch, accumulation, epochs,
        float(train_cfg.get("warmup_ratio", 0.05)),
    )
    save_steps = int(train_cfg.get("save_steps", 250))
    eval_steps = int(train_cfg.get("eval_steps", save_steps))
    raw_limit = train_cfg.get("save_total_limit", 3)
    save_total_limit = int(raw_limit) if raw_limit is not None else None
    max_steps = int(train_cfg["max_steps"]) if train_cfg.get("max_steps") else -1
    early = train_cfg.get("early_stopping") or {}
    early_patience = int(early.get("patience", 0) or 0)
    if early_patience and eval_steps != save_steps:
        raise SystemExit(
            "early stopping requires eval_steps == save_steps so the best "
            "checkpoint is retained")
    resume_from = None
    if resume:
        found = latest_valid_checkpoint(checkpoint_dir)
        if found:
            resume_from = str(found)

    summary = {
        "base_model": cfg["base_model"],
        "max_seq_length": int(cfg["max_seq_length"]),
        "per_device_train_batch_size": batch,
        "gradient_accumulation_steps": accumulation,
        "effective_batch_size": batch * accumulation,
        "epochs": epochs,
        "max_steps": max_steps,
        "learning_rate": float(train_cfg["learning_rate"]),
        "warmup_ratio": float(train_cfg.get("warmup_ratio", 0.05)),
        "precision": report["precision"],
        "train_examples": len(train_ds),
        "validation_examples": len(val_ds),
        "checkpoint_dir": str(checkpoint_dir),
        "resume_from_checkpoint": resume_from,
        "early_stopping_patience": early_patience,
        **plan,
    }
    if not preflight:
        print(json.dumps(summary, indent=2), flush=True)

    sft_cfg = SFTConfig(
        output_dir=str(checkpoint_dir),
        num_train_epochs=epochs,
        max_steps=max_steps,
        per_device_train_batch_size=batch,
        gradient_accumulation_steps=accumulation,
        per_device_eval_batch_size=1,
        learning_rate=float(train_cfg["learning_rate"]),
        lr_scheduler_type=train_cfg.get("lr_scheduler", "cosine"),
        warmup_steps=plan["warmup_steps"],
        weight_decay=float(train_cfg.get("weight_decay", 0.01)),
        bf16=use_bf16,
        fp16=not use_bf16,
        seed=seed,
        save_strategy="steps",
        save_steps=save_steps,
        save_total_limit=save_total_limit,
        logging_steps=10,
        eval_strategy="steps",
        eval_steps=eval_steps,
        load_best_model_at_end=True,
        metric_for_best_model="eval_loss",
        greater_is_better=False,
        gradient_checkpointing=not _unsloth,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        optim="paged_adamw_8bit",
        report_to=[],
        dataset_text_field="text",
        max_length=int(cfg["max_seq_length"]),
    )
    callbacks = list(extra_callbacks or [])
    if early_patience:
        from transformers import EarlyStoppingCallback

        callbacks.append(EarlyStoppingCallback(
            early_stopping_patience=early_patience,
            early_stopping_threshold=float(early.get("threshold", 0.0)),
        ))
    trainer = SFTTrainer(
        model=model,
        args=sft_cfg,
        train_dataset=train_ds,
        eval_dataset=val_ds,
        processing_class=tokenizer,
        callbacks=callbacks or None,
    )
    report["training_plan"] = plan
    report["precision"] = summary["precision"]
    report["effective_batch_size"] = summary["effective_batch_size"]
    if resume_from:
        report["resumed_from"] = resume_from
    if preflight:
        print(json.dumps({"status": "preflight-pass", **summary}, indent=2),
              flush=True)
        return {"status": "preflight-pass", **summary}
    try:
        trainer.train(resume_from_checkpoint=resume_from)
        model.save_pretrained(str(checkpoint_dir / "adapter"))
        tokenizer.save_pretrained(str(checkpoint_dir / "adapter"))
    except BaseException as exc:  # noqa: BLE001
        report["status"] = "failed"
        report["reason"] = f"{type(exc).__name__}: {exc}"
        report["finished"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds")
        report["latest_checkpoint"] = (
            str(found) if (found := latest_valid_checkpoint(checkpoint_dir)) else None
        )
        (report_dir / "training-report.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8")
        raise

    report["status"] = "completed"
    report["finished"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report["checkpoint_dir"] = str(checkpoint_dir)
    report["latest_checkpoint"] = (
        str(found) if (found := latest_valid_checkpoint(checkpoint_dir)) else None
    )
    report["adapter_dir"] = str(checkpoint_dir / "adapter")
    metrics = getattr(trainer, "state", None)
    if metrics is not None and getattr(metrics, "log_history", None):
        report["final_loss"] = metrics.log_history[-1].get("train_loss")
        report["global_step"] = getattr(metrics, "global_step", None)
        report["best_metric"] = getattr(metrics, "best_metric", None)
        report["log_history"] = [
            entry for entry in metrics.log_history
            if "loss" in entry or "eval_loss" in entry
        ]
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
    parser.add_argument("--preflight", action="store_true",
                        help="load model, attach LoRA, build trainer, do not train")
    args = parser.parse_args()

    if args.dry_run:
        cfg = load_config(args.config)
        root = Path(args.dataset_dir or cfg["train"]["dataset_dir"])
        examples, val, stats = load_training_data(
            root, int(cfg["train"].get("seed", 20261028)))
        print(json.dumps({"dry_run": True, "dataset_stats": stats}, indent=2))
        return
    try:
        report = train(args.config, args.resume, args.output_dir,
                       args.dataset_dir, preflight=args.preflight)
    except BaseException as exc:  # noqa: BLE001
        cfg = load_config(args.config)
        report_dir = Path(cfg["output"]["report_dir"])
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / "training-report.json"
        failed = {}
        if report_path.exists():
            failed = json.loads(report_path.read_text(encoding="utf-8"))
        failed.update({
            "status": "failed",
            "reason": f"{type(exc).__name__}: {exc}",
            "finished": datetime.now(timezone.utc).isoformat(
                timespec="seconds"),
        })
        report_path.write_text(json.dumps(failed, indent=2), encoding="utf-8")
        raise
    print(json.dumps({"status": report["status"]}, indent=2))
    if report["status"] == "blocked":
        sys.exit(2)


if __name__ == "__main__":
    main()
