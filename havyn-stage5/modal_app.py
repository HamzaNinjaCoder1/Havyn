from __future__ import annotations

import gc
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import modal

_REPO_ENV = os.environ.get("HAVYN_REPO_ROOT")
REPO = Path(_REPO_ENV) if _REPO_ENV else Path(__file__).resolve().parents[1]
REMOTE_REPO = PurePosixPath("/root/havyn")
SCRIPTS = REPO / "havyn-stage5" / "scripts"
for entry in (str(REPO), str(SCRIPTS)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import modal_support as support

CONFIG = support.load_yaml(REPO / "havyn-stage5" / "configs" / "modal.yaml")
MOUNT = CONFIG["mount_path"]
ROOT = Path(CONFIG["root"])
DATASET_DIR = Path(CONFIG["dataset"]["volume_dir"])
RUNS_ROOT = ROOT / CONFIG["artifacts"]["runs"]
CHECKPOINTS_ROOT = ROOT / CONFIG["artifacts"]["checkpoints"]
MODELS_ROOT = ROOT / CONFIG["artifacts"]["models"]
REPORTS_ROOT = ROOT / CONFIG["artifacts"]["reports"]
MANIFESTS_ROOT = ROOT / CONFIG["artifacts"]["manifests"]
LOGS_ROOT = ROOT / CONFIG["artifacts"]["logs"]
SMOKE_REPORTS = REPORTS_ROOT / "smoke"
EVAL_REPORTS = REPORTS_ROOT / "evaluation"

CACHE_ENV = {
    "HF_HOME": CONFIG["storage"]["hf_home"],
    "HF_HUB_CACHE": CONFIG["storage"]["hf_hub_cache"],
    "TRANSFORMERS_CACHE": CONFIG["storage"]["transformers_cache"],
    "HF_DATASETS_CACHE": CONFIG["storage"]["datasets_cache"],
    "TORCH_HOME": CONFIG["storage"]["torch_home"],
    "XDG_CACHE_HOME": CONFIG["storage"]["xdg_cache_home"],
    "TOKENIZERS_PARALLELISM": "false",
    "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "HAVYN_REPO_ROOT": str(REMOTE_REPO),
}

VOLUME = modal.Volume.from_name(
    CONFIG["volume_name"], create_if_missing=True)

IGNORED_PARTS = {
    ".git", ".venv", "__pycache__", ".pytest_cache", ".audit-tmp",
    "autopilot_evidence", "docs", "demo", "data", "library", "probes",
    "runner_results", "runner_s119", "fault_lab_results", "tests",
    "EXTENSION", ".github", "skill", "examples",
}
IGNORED_RELATIVE = {
    "havyn-stage5/datasets/havyn-train-v1",
    "havyn-stage5/datasets/gold-v1",
    "havyn-stage5/datasets/hf-export",
    "havyn-stage5/prompt-authoring",
    "havyn-stage5/certificates",
    "havyn-stage5/runs",
    "havyn-stage5/checkpoints",
    "havyn-stage5/evaluation",
    "havyn-stage5/artifacts",
    "havyn-stage5/reports",
}


def _ignored(path: Path) -> bool:
    try:
        relative = path.resolve().relative_to(REPO.resolve())
    except ValueError:
        relative = path
    if any(part in IGNORED_PARTS for part in relative.parts):
        return True
    text = relative.as_posix()
    if any(text == item or text.startswith(item + "/")
           for item in IGNORED_RELATIVE):
        return True
    return path.suffix.lower() in {".pkt", ".docx", ".pdf", ".mp4", ".html"}


def _container_requirements() -> list[str]:
    path = REPO / "havyn-stage5" / "requirements-modal.txt"
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines
            if line.strip() and not line.strip().startswith("#")]


_REQUIREMENTS = _container_requirements()
_TORCH_PINS = [item for item in _REQUIREMENTS if item.startswith("torch")]
_OTHER_PINS = [item for item in _REQUIREMENTS if not item.startswith("torch")]

IMAGE = (
    modal.Image.debian_slim(
        python_version=CONFIG["runtime"]["python_version"])
    .pip_install(*_TORCH_PINS)
    .uv_pip_install(*_OTHER_PINS)
    .env(CACHE_ENV)
    .workdir(str(REMOTE_REPO))
    .add_local_dir(
        str(REPO), remote_path=str(REMOTE_REPO), copy=True, ignore=_ignored)
)

app = modal.App(CONFIG["app_name"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_layout() -> None:
    for path in (ROOT, RUNS_ROOT, CHECKPOINTS_ROOT, MODELS_ROOT,
                 REPORTS_ROOT, MANIFESTS_ROOT, LOGS_ROOT, SMOKE_REPORTS,
                 EVAL_REPORTS, DATASET_DIR.parent):
        path.mkdir(parents=True, exist_ok=True)


def _dataset_identity() -> dict:
    report = support.verify_dataset(
        DATASET_DIR, CONFIG["dataset"]["expected_counts"],
        CONFIG["dataset"]["frozen_manifest"])
    if not report["ok"]:
        raise RuntimeError(
            "dataset verification failed: " + "; ".join(report["breaks"]))
    return {
        "path": str(DATASET_DIR),
        "manifest_sha256": report["manifest_sha256"],
        "frozen_at": report["frozen_at"],
        "hashes": report["hashes"],
        "rows": report["rows"],
    }


def _gpu_report() -> dict:
    import torch

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is not available in this container")
    name = torch.cuda.get_device_name(0)
    capability = torch.cuda.get_device_capability(0)
    free, total = torch.cuda.mem_get_info()
    bf16 = bool(torch.cuda.is_bf16_supported()) and capability[0] >= 8
    checked = None
    if bf16:
        probe = torch.randn(1024, 1024, device="cuda", dtype=torch.bfloat16)
        product = probe @ probe
        torch.cuda.synchronize()
        checked = bool(torch.isfinite(product).all().item())
    return {
        "name": name,
        "capability": list(capability),
        "vram_total_gb": round(total / 1024**3, 2),
        "vram_free_gb": round(free / 1024**3, 2),
        "bf16_supported": bf16,
        "torch_version": torch.__version__,
        "cuda_runtime": torch.version.cuda,
        "bf16_matmul_ok": checked,
        "device_count": torch.cuda.device_count(),
    }


def _environment_report() -> dict:
    import platform

    import torch

    versions = support.environment_versions([
        "torch", "transformers", "trl", "peft", "accelerate", "datasets",
        "bitsandbytes", "safetensors", "sentencepiece", "huggingface-hub",
        "pyyaml", "pydantic", "numpy",
    ])
    missing = [name for name, version in versions.items() if version is None]
    if missing:
        raise RuntimeError("missing packages: " + ", ".join(missing))
    caches = support.check_cloud_cache_paths(dict(os.environ), MOUNT)
    expected = CONFIG["smoke_gate"]["expected_python"]
    python_version = platform.python_version()
    if not python_version.startswith(expected):
        raise RuntimeError(
            f"python {python_version} does not match {expected}")
    import bitsandbytes
    from huggingface_hub import constants as hub_constants

    hub_cache = str(Path(hub_constants.HF_HUB_CACHE).resolve())
    expected_hub = str(Path(CACHE_ENV["HF_HUB_CACHE"]).resolve())
    torch_cache = str(Path(torch.hub.get_dir()).resolve())
    expected_torch = str(Path(CACHE_ENV["TORCH_HOME"]).resolve())
    if hub_cache != expected_hub:
        raise RuntimeError(
            f"huggingface_hub resolved {hub_cache}, expected {expected_hub}")
    if torch_cache != expected_torch:
        raise RuntimeError(
            f"torch resolved {torch_cache}, expected {expected_torch}")
    frozen = subprocess.run(
        [sys.executable, "-m", "pip", "freeze"],
        capture_output=True, text=True, check=False).stdout.strip().splitlines()
    ephemeral = shutil.disk_usage("/")
    volume_disk = shutil.disk_usage(MOUNT)
    return {
        "python": python_version,
        "platform": platform.platform(),
        "versions": versions,
        "pip_freeze": sorted(frozen),
        "caches": caches,
        "resolved_hub_cache": hub_cache,
        "resolved_torch_cache": torch_cache,
        "bitsandbytes_version": bitsandbytes.__version__,
        "ephemeral_disk_free_gb": round(ephemeral.free / 1024**3, 2),
        "volume_free_gb": round(volume_disk.free / 1024**3, 2),
        "torch_cuda_build": torch.version.cuda,
    }


def _token_lengths(tokenizer, examples: list[dict],
                   max_length: int) -> dict:
    lengths = []
    for example in examples:
        text = tokenizer.apply_chat_template(
            example["messages"], tokenize=False, add_generation_prompt=False)
        ids = tokenizer(text, add_special_tokens=False)["input_ids"]
        lengths.append(len(ids))
    if not lengths:
        raise RuntimeError("no tokenized examples")
    ordered = sorted(lengths)
    p95 = ordered[max(0, int(len(ordered) * 0.95) - 1)]
    return {
        "examples": len(lengths),
        "min": min(lengths),
        "mean": round(sum(lengths) / len(lengths), 2),
        "p95": p95,
        "max": max(lengths),
        "over_max_length": sum(1 for value in lengths if value > max_length),
        "max_length": max_length,
    }


def _dataset_report(tokenizer, max_length: int, seed: int) -> dict:
    from format_examples import format_split, load_training_data

    identity = _dataset_identity()
    validation, validation_skipped = format_split(
        DATASET_DIR / "validation.jsonl")
    heldout, heldout_skipped = format_split(DATASET_DIR / "heldout.jsonl")
    train, val, stats = load_training_data(DATASET_DIR, seed)
    if validation_skipped or heldout_skipped:
        raise RuntimeError(
            f"unformattable rows: validation={validation_skipped} "
            f"heldout={heldout_skipped}")
    if not train or not val or not heldout:
        raise RuntimeError("a split formatted to zero examples")
    if len(val) != len(validation):
        raise RuntimeError("validation formatting is not stable")
    token_stats = _token_lengths(tokenizer, train[:32], max_length)
    standard_stats = _token_lengths(tokenizer, train[:32], 2048)
    return {
        "identity": identity,
        "formatting": {
            "stats": stats,
            "validation_formatted": len(validation),
            "heldout_formatted": len(heldout),
            "validation_skipped": validation_skipped,
            "heldout_skipped": heldout_skipped,
        },
        "tokenization": token_stats,
        "tokenization_standard_2048": standard_stats,
    }


def _load_model(cfg: dict):
    import torch
    from transformers import (AutoModelForCausalLM, AutoTokenizer,
                              BitsAndBytesConfig)

    if not torch.cuda.is_available():
        raise RuntimeError("model loading requires CUDA")
    if not (torch.cuda.is_bf16_supported()
            and torch.cuda.get_device_capability(0)[0] >= 8):
        raise RuntimeError("BF16 is required and not supported on this GPU")
    tokenizer = AutoTokenizer.from_pretrained(
        cfg["base_model"], trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    quant_config = None
    if cfg.get("load_in_4bit", True):
        quant_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_use_double_quant=True,
        )
    torch.cuda.reset_peak_memory_stats()
    torch.cuda.empty_cache()
    model = AutoModelForCausalLM.from_pretrained(
        cfg["base_model"],
        torch_dtype="auto",
        device_map={"": 0},
        quantization_config=quant_config,
        trust_remote_code=True,
    )
    model.eval()
    return model, tokenizer, quant_config


def _model_report(model, tokenizer, cfg: dict) -> dict:
    import torch

    config = model.config
    model_type = str(getattr(config, "model_type", ""))
    model_name = str(getattr(config, "_name_or_path", "") or
                     getattr(model, "name_or_path", ""))
    parameter_count = sum(p.numel() for p in model.parameters())
    quant_modules = sum(
        1 for module in model.modules()
        if module.__class__.__name__ in {"Linear4bit", "Linear8BitLt"})
    is_quantized = bool(getattr(model, "is_quantized", False)
                        or quant_modules)
    if "qwen3" not in model_type.lower():
        raise RuntimeError(
            f"unexpected model architecture {model_type!r}; expected qwen3")
    if not 7_000_000_000 <= parameter_count <= 9_000_000_000:
        raise RuntimeError(
            f"unexpected parameter count {parameter_count}; expected ~8.2B")
    if cfg.get("load_in_4bit", True) and not is_quantized:
        raise RuntimeError("4-bit was requested but no quantized modules loaded")
    if torch.cuda.get_device_capability(0)[0] < 8:
        raise RuntimeError("GPU does not support BF16")
    weights_dtype = str(next(model.parameters()).dtype)
    peak_gb = round(torch.cuda.max_memory_allocated() / 1024**3, 2)
    reserved_gb = round(torch.cuda.memory_reserved() / 1024**3, 2)
    chat_template = tokenizer.chat_template or ""
    return {
        "model_id": cfg["base_model"],
        "resolved_name": model_name,
        "model_type": model_type,
        "architectures": list(getattr(config, "architectures", []) or []),
        "parameter_count": parameter_count,
        "parameter_billions": round(parameter_count / 1e9, 3),
        "parameter_dtype": weights_dtype,
        "torch_dtype_config": str(getattr(config, "torch_dtype", "")),
        "is_quantized": is_quantized,
        "quantized_modules": quant_modules,
        "load_in_4bit": bool(cfg.get("load_in_4bit", True)),
        "quantization": "nf4-double-quant-bf16-compute" if is_quantized else None,
        "device": str(next(model.parameters()).device),
        "device_map": str(getattr(model, "hf_device_map", "")),
        "peak_memory_gb": peak_gb,
        "reserved_memory_gb": reserved_gb,
        "tokenizer_id": tokenizer.name_or_path,
        "tokenizer_class": tokenizer.__class__.__name__,
        "vocab_size": len(tokenizer),
        "pad_token": tokenizer.pad_token,
        "eos_token": tokenizer.eos_token,
        "chat_template_sha256": support.sha256_bytes(
            chat_template.encode("utf-8")),
        "chat_template_has_thinking": "enable_thinking" in chat_template,
        "revision": getattr(config, "_commit_hash", None),
    }


def _lora_report(model, cfg: dict) -> dict:
    trainable = [(name, param) for name, param in model.named_parameters()
                 if param.requires_grad]
    total_params = sum(p.numel() for p in model.parameters())
    trainable_params = sum(p.numel() for _, p in trainable)
    unexpected = [name for name, _ in trainable if "lora_" not in name]
    if unexpected:
        raise RuntimeError(
            "non-LoRA parameters are trainable: " + ", ".join(unexpected[:5]))
    if not trainable_params:
        raise RuntimeError("no trainable LoRA parameters")
    percent = 100.0 * trainable_params / total_params
    if percent > 5.0:
        raise RuntimeError(f"trainable share is {percent:.2f}% (>5%)")
    module_names = [name for name, _ in model.named_modules()]
    target_counts = {}
    for target in cfg["lora"]["target_modules"]:
        count = sum(1 for name in module_names
                    if name == target or name.endswith("." + target))
        target_counts[target] = count
    missing = [name for name, count in target_counts.items() if count == 0]
    if missing:
        raise RuntimeError(
            "LoRA target modules matched nothing: " + ", ".join(missing))
    return {
        "total_parameters": total_params,
        "trainable_parameters": trainable_params,
        "trainable_percent": round(percent, 4),
        "trainable_tensors": len(trainable),
        "trainable_names_sample": [name for name, _ in trainable[:8]],
        "base_parameters_frozen": not unexpected,
        "target_modules": list(cfg["lora"]["target_modules"]),
        "target_module_counts": target_counts,
        "rank": int(cfg["lora"]["r"]),
        "alpha": int(cfg["lora"]["lora_alpha"]),
        "dropout": float(cfg["lora"]["lora_dropout"]),
        "bias": cfg["lora"].get("bias", "none"),
        "gradient_checkpointing": bool(
            cfg["lora"].get("use_gradient_checkpointing", True)),
    }


def _attach_lora(model, cfg: dict):
    from peft import LoraConfig, get_peft_model

    lora = LoraConfig(
        r=int(cfg["lora"]["r"]),
        lora_alpha=int(cfg["lora"]["lora_alpha"]),
        lora_dropout=float(cfg["lora"]["lora_dropout"]),
        target_modules=list(cfg["lora"]["target_modules"]),
        bias=cfg["lora"].get("bias", "none"),
        task_type="CAUSAL_LM",
    )
    model = get_peft_model(model, lora)
    if bool(cfg["lora"].get("use_gradient_checkpointing", True)):
        model.gradient_checkpointing_enable(
            gradient_checkpointing_kwargs={"use_reentrant": False})
        model.enable_input_require_grads()
        model.config.use_cache = False
    return model


def _training_paths(run_id: str, resume: bool) -> tuple[Path, Path]:
    run_dir, mode = support.prepare_run_dir(RUNS_ROOT, run_id, resume=resume)
    checkpoint_dir = CHECKPOINTS_ROOT / run_id
    if resume:
        if not checkpoint_dir.is_dir():
            raise FileNotFoundError(
                f"resume requested but checkpoint dir is missing: {checkpoint_dir}")
    else:
        if checkpoint_dir.exists():
            raise FileExistsError(
                f"checkpoint dir already exists, refusing to overwrite: {checkpoint_dir}")
        checkpoint_dir.mkdir(parents=True, exist_ok=False)
    return run_dir, checkpoint_dir


def _used_config(source_config: Path, dataset_dir: Path,
                 checkpoint_dir: Path, report_dir: Path,
                 extra: dict | None = None) -> tuple[dict, Path, list]:
    original = support.load_yaml(source_config)
    overrides = {
        "train.dataset_dir": str(dataset_dir),
        "output.checkpoint_dir": str(checkpoint_dir),
        "output.report_dir": str(report_dir),
    }
    if extra:
        overrides.update(extra)
    used, changes = support.apply_config_overrides(original, overrides)
    used_path = report_dir.parent / "config.used.yaml"
    support.write_yaml(used_path, used)
    support.write_yaml(report_dir.parent / "config.original.yaml", original)
    return used, used_path, changes


def _training_manifest(run_id: str, kind: str, cfg: dict, config_path: Path,
                       config_changes: list, dataset: dict, gpu: dict,
                       versions: dict, git: dict, smoke: dict | None,
                       resume: bool, resume_from: str | None,
                       original_run_id: str | None) -> dict:
    return {
        "run_id": run_id,
        "kind": kind,
        "created": _now(),
        "status": "started",
        "resume": resume,
        "original_run_id": original_run_id or run_id,
        "resume_from_checkpoint": resume_from,
        "git": git,
        "dataset": dataset,
        "base_model": cfg["base_model"],
        "model_weight_gb": cfg.get("model_weight_gb"),
        "config_path": str(config_path),
        "config_sha256": support.sha256_file(config_path),
        "config_values": cfg,
        "config_overrides": config_changes,
        "gpu": gpu,
        "versions": versions,
        "smoke_report": smoke,
        "seed": cfg["train"].get("seed"),
        "precision": "bf16",
    }


def _write_run_state(run_dir: Path, payload: dict) -> None:
    support.write_json(run_dir / "run.json", payload)


def _tiny_train(smoke_id: str, gpu_rate: float, git: dict) -> dict:
    import torch

    from train_lora import train as run_training
    from transformers import TrainerCallback

    class VolumeCommit(TrainerCallback):
        def on_save(self, args, state, control, **kwargs):
            VOLUME.commit()

    run_dir, checkpoint_dir = _training_paths(smoke_id, resume=False)
    source_config = REPO / CONFIG["configs"]["smoke"]
    used, used_path, changes = _used_config(
        source_config, DATASET_DIR, checkpoint_dir, run_dir / "reports")
    dataset = _dataset_identity()
    versions = support.environment_versions([
        "torch", "transformers", "trl", "peft", "accelerate", "datasets",
        "bitsandbytes"])
    manifest = _training_manifest(
        smoke_id, "smoke", used, used_path, changes, dataset,
        {"requested": "smoke", "name": torch.cuda.get_device_name(0)},
        versions, git, None, False, None, None)
    _write_run_state(run_dir, manifest)
    os.environ["HAVYN_CODE_COMMIT"] = str(git.get("commit") or "")
    started = time.time()
    try:
        report = run_training(
            str(used_path), False, None, None, False, [VolumeCommit()])
    except BaseException as exc:
        manifest.update({
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
            "finished": _now(),
            "duration_seconds": round(time.time() - started, 2),
        })
        _write_run_state(run_dir, manifest)
        VOLUME.commit()
        raise
    duration = time.time() - started
    if report.get("status") != "completed":
        raise RuntimeError(
            f"tiny training did not complete: {report.get('status')}")
    if int(report.get("global_step") or 0) < 1:
        raise RuntimeError("tiny training produced no optimizer steps")
    selection = support.select_best_checkpoint(checkpoint_dir)
    if selection["status"] != "SELECTED":
        raise RuntimeError(
            "tiny training produced no selectable checkpoint: "
            + str(selection.get("reason")))
    final_state = support.read_json(
        Path(selection["final"]["path"]) / "trainer_state.json")
    assessment = support.assess_training(final_state)
    support.write_json(run_dir / "selection.json", selection)
    support.write_json(run_dir / "training-analysis.json", assessment)
    manifest.update({
        "status": "completed",
        "finished": _now(),
        "duration_seconds": round(duration, 2),
        "estimated_gpu_usd": round(duration / 3600 * gpu_rate, 4),
        "global_step": report.get("global_step"),
        "final_loss": report.get("final_loss"),
        "eval_points": len([
            item for item in final_state.get("log_history") or []
            if item.get("eval_loss") is not None]),
        "best_checkpoint": selection["best"]["name"],
        "best_eval_loss": selection["best"]["best_eval_loss"],
        "selection": selection,
        "assessment": assessment,
    })
    _write_run_state(run_dir, manifest)
    support.write_json(
        MANIFESTS_ROOT / f"{smoke_id}.json", manifest)
    VOLUME.commit()
    return manifest


def _smoke_gate() -> dict:
    candidates = []
    for path in SMOKE_REPORTS.glob("*/smoke-report.json"):
        try:
            payload = support.read_json(path)
        except (OSError, ValueError):
            continue
        stamp = payload.get("finished") or payload.get("started") or ""
        candidates.append((stamp, path, payload))
    if not candidates:
        raise RuntimeError(
            "no smoke report found; run `modal run havyn-stage5/modal_app.py "
            "--stage smoke` first")
    _, path, latest = max(candidates, key=lambda item: item[0])
    if latest.get("status") != "PASS":
        raise RuntimeError(
            f"latest smoke report is {latest.get('status')}: {path}")
    dataset = _dataset_identity()
    if latest.get("dataset_manifest_sha256") != dataset["manifest_sha256"]:
        raise RuntimeError(
            "smoke report was produced against a different dataset manifest")
    return {"path": str(path), "report": latest}


@app.function(
    image=IMAGE,
    volumes={MOUNT: VOLUME},
    gpu=CONFIG["gpu"]["default"],
    timeout=CONFIG["runtime"]["function_timeout_seconds"],
    startup_timeout=CONFIG["runtime"]["startup_timeout_seconds"],
    max_containers=1,
    retries=0,
)
def run_smoke(request: dict) -> dict:
    import torch
    from transformers import AutoTokenizer

    _ensure_layout()
    smoke_id = support.validate_run_id(request["run_id"])
    started_at = _now()
    started = time.time()
    report: dict = {
        "stage": "smoke",
        "run_id": smoke_id,
        "requested_gpu": request.get("gpu"),
        "started": started_at,
        "status": "RUNNING",
        "tests": {},
        "error": None,
    }
    try:
        gpu = _gpu_report()
        report["tests"]["A_gpu"] = gpu
        if CONFIG["smoke_gate"]["require_bf16"] and not gpu["bf16_supported"]:
            raise RuntimeError("BF16 is required by the smoke gate")
        if gpu["vram_total_gb"] < CONFIG["smoke_gate"]["min_vram_gb"]:
            raise RuntimeError(
                f"VRAM {gpu['vram_total_gb']} GB is below the "
                f"{CONFIG['smoke_gate']['min_vram_gb']} GB gate")
        env = _environment_report()
        report["tests"]["B_environment"] = env
        cfg = support.load_yaml(REPO / CONFIG["configs"]["smoke"])
        report["base_model"] = cfg["base_model"]
        report["git"] = request.get("git") or {}
        tokenizer = AutoTokenizer.from_pretrained(
            cfg["base_model"], trust_remote_code=True)
        dataset = _dataset_report(
            tokenizer, int(cfg["max_seq_length"]),
            int(cfg["train"].get("seed", 20261028)))
        report["tests"]["C_dataset"] = dataset
        model, tokenizer, _ = _load_model(cfg)
        model_info = _model_report(model, tokenizer, cfg)
        report["tests"]["D_model"] = model_info
        print("MODEL VERIFICATION", json.dumps(model_info, indent=2), flush=True)
        model = _attach_lora(model, cfg)
        lora_info = _lora_report(model, cfg)
        report["tests"]["E_lora"] = lora_info
        print("LORA VERIFICATION", json.dumps(lora_info, indent=2), flush=True)
        del model
        gc.collect()
        torch.cuda.empty_cache()
        tiny = _tiny_train(
            smoke_id, float(request.get("gpu_rate", 0.0)),
            request.get("git") or {})
        report["tests"]["F_tiny_train"] = tiny
        report["status"] = "PASS"
    except BaseException as exc:
        report["status"] = "FAIL"
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["traceback"] = traceback.format_exc()
    finally:
        duration = time.time() - started
        report["finished"] = _now()
        report["duration_seconds"] = round(duration, 2)
        report["estimated_gpu_usd"] = round(
            duration / 3600 * float(request.get("gpu_rate", 0.0)), 4)
        try:
            dataset = _dataset_identity()
            report["dataset_manifest_sha256"] = dataset["manifest_sha256"]
            report["dataset"] = dataset
        except Exception:
            report["dataset_manifest_sha256"] = None
        report.setdefault("base_model", None)
        try:
            report["source_config_sha256"] = support.sha256_file(
                REPO / CONFIG["configs"]["smoke"])
            report["standard_config_sha256"] = support.sha256_file(
                REPO / CONFIG["configs"]["standard"])
        except OSError:
            report["source_config_sha256"] = None
            report["standard_config_sha256"] = None
        support.write_json(
            SMOKE_REPORTS / smoke_id / "smoke-report.json", report)
        support.write_json(
            MANIFESTS_ROOT / f"smoke-{smoke_id}.json", report)
        VOLUME.commit()
    return report


@app.function(
    image=IMAGE,
    volumes={MOUNT: VOLUME},
    gpu=CONFIG["gpu"]["default"],
    timeout=CONFIG["runtime"]["function_timeout_seconds"],
    startup_timeout=CONFIG["runtime"]["startup_timeout_seconds"],
    max_containers=1,
    retries=0,
)
def run_train(request: dict) -> dict:
    from transformers import TrainerCallback

    from train_lora import latest_valid_checkpoint
    from train_lora import train as run_training

    class VolumeCommit(TrainerCallback):
        def on_save(self, args, state, control, **kwargs):
            VOLUME.commit()

    _ensure_layout()
    run_id = support.validate_run_id(request["run_id"])
    resume = bool(request.get("resume"))
    gate = _smoke_gate()
    gpu = _gpu_report()
    env = _environment_report()
    cfg_source = REPO / CONFIG["configs"]["standard"]
    cfg = support.load_yaml(cfg_source)
    if gate["report"].get("base_model") != cfg["base_model"]:
        raise RuntimeError(
            "smoke report was produced for "
            f"{gate['report'].get('base_model')}, but the training config is "
            f"{cfg['base_model']}")
    if (gate["report"].get("standard_config_sha256")
            != support.sha256_file(cfg_source)):
        raise RuntimeError(
            "the Standard training config changed after the smoke test; "
            "re-run --stage smoke")
    dataset = _dataset_identity()
    run_dir, checkpoint_dir = _training_paths(run_id, resume)
    existing = latest_valid_checkpoint(checkpoint_dir)
    if resume and existing is None:
        raise RuntimeError(
            "resume requested but no valid checkpoint exists in "
            f"{checkpoint_dir}")
    if not resume and any(checkpoint_dir.glob("checkpoint-*")):
        raise RuntimeError(
            "fresh run found existing checkpoints; use --resume with the "
            "original --run-id")
    previous = None
    if resume:
        previous = support.read_json(run_dir / "run.json")
        if previous.get("status") == "completed":
            raise RuntimeError(
                f"run {run_id} is already completed; start a new run id "
                "instead of resuming it")
        stamp = int(time.time())
        prior_report = run_dir / "reports" / "training-report.json"
        if prior_report.exists():
            shutil.copy2(
                prior_report,
                run_dir / "reports"
                / f"training-report.prior-{stamp}.json")
        for name in ("config.used.yaml", "config.original.yaml",
                     "selection.json", "training-analysis.json"):
            prior = run_dir / name
            if prior.exists():
                shutil.copy2(
                    prior, run_dir / f"{prior.stem}.prior-{stamp}{prior.suffix}")
    used, used_path, changes = _used_config(
        cfg_source, DATASET_DIR, checkpoint_dir, run_dir / "reports",
        extra={"train.save_total_limit": None})
    versions = env["versions"]
    manifest = _training_manifest(
        run_id, "training", used, used_path, changes, dataset, gpu,
        versions, request.get("git") or {}, gate,
        resume, str(existing) if existing else None,
        previous.get("run_id") if previous else None)
    manifest["environment"] = {
        "versions": versions,
        "pip_freeze": env.get("pip_freeze"),
        "caches": env.get("caches"),
    }
    manifest["model_identity"] = (
        (gate["report"].get("tests") or {}).get("D_model"))
    if previous:
        manifest["resume_history"] = previous.get("resume_history", []) + [{
            "resumed_at": _now(),
            "original_run_id": previous.get("run_id"),
            "checkpoint": str(latest_valid_checkpoint(checkpoint_dir)),
            "config_sha256": support.sha256_file(used_path),
            "dataset_manifest_sha256": dataset["manifest_sha256"],
            "model_id": cfg["base_model"],
        }]
    _write_run_state(run_dir, manifest)
    print("TRAINING MANIFEST", json.dumps({
        "run_id": run_id,
        "resume": resume,
        "resume_from_checkpoint": manifest["resume_from_checkpoint"],
        "base_model": cfg["base_model"],
        "dataset_manifest_sha256": dataset["manifest_sha256"],
        "gpu": gpu["name"],
        "config_overrides": changes,
        "training": {
            key: used["train"].get(key) for key in (
                "epochs", "max_steps", "per_device_train_batch_size",
                "gradient_accumulation_steps", "learning_rate",
                "lr_scheduler", "warmup_ratio", "weight_decay",
                "save_steps", "eval_steps", "save_total_limit", "seed")
        },
        "lora": used["lora"],
        "max_seq_length": used["max_seq_length"],
    }, indent=2), flush=True)
    os.environ["HAVYN_CODE_COMMIT"] = str(
        (request.get("git") or {}).get("commit") or "")
    started = time.time()
    try:
        report = run_training(
            str(used_path), resume, None, None, False, [VolumeCommit()])
        duration = time.time() - started
        if report.get("status") != "completed":
            raise RuntimeError(
                f"training did not complete: {report.get('status')} "
                f"{report.get('reason', '')}")
        dataset_after = _dataset_identity()
        if dataset_after["manifest_sha256"] != dataset["manifest_sha256"]:
            raise RuntimeError("dataset manifest changed during training")
        selection = support.select_best_checkpoint(checkpoint_dir)
        support.write_json(run_dir / "selection.json", selection)
        if selection["status"] != "SELECTED":
            raise RuntimeError(
                "no checkpoint met the selection criterion: "
                + str(selection.get("reason")))
        final_state = support.read_json(
            Path(selection["final"]["path"]) / "trainer_state.json")
        assessment = support.assess_training(final_state)
        support.write_json(run_dir / "training-analysis.json", assessment)
        promotion_allowed = assessment["status"] == "stable"
        manifest.update({
            "status": "completed",
            "finished": _now(),
            "duration_seconds": round(duration, 2),
            "estimated_gpu_usd": round(
                duration / 3600 * float(request.get("gpu_rate", 0.0)), 4),
            "actual_gpu": gpu,
            "training_report": {
                key: report.get(key) for key in (
                    "global_step", "final_loss", "best_metric",
                    "log_history", "training_plan", "resume_from_checkpoint",
                    "precision", "effective_batch_size", "train_examples",
                    "validation_examples", "dataset_stats")
            },
            "best_checkpoint": selection["best"]["name"],
            "best_checkpoint_path": selection["best"]["path"],
            "best_eval_loss": selection["best"]["best_eval_loss"],
            "final_checkpoint": selection["final"]["name"],
            "selection": selection,
            "assessment": assessment,
            "promotion_allowed": promotion_allowed,
            "promotion_blockers": (
                [] if promotion_allowed else [assessment["status"]]),
            "checkpoint_dir": str(checkpoint_dir),
        })
        _write_run_state(run_dir, manifest)
        support.write_json(MANIFESTS_ROOT / f"{run_id}.json", manifest)
        print("TRAINING RESULT", json.dumps({
            "status": manifest["status"],
            "run_id": run_id,
            "global_step": manifest["training_report"]["global_step"],
            "best_checkpoint": manifest["best_checkpoint"],
            "best_eval_loss": manifest["best_eval_loss"],
            "final_checkpoint": manifest["final_checkpoint"],
            "assessment": manifest["assessment"],
            "promotion_allowed": manifest["promotion_allowed"],
            "promotion_blockers": manifest["promotion_blockers"],
            "duration_seconds": manifest["duration_seconds"],
            "estimated_gpu_usd": manifest["estimated_gpu_usd"],
        }, indent=2), flush=True)
        VOLUME.commit()
        return manifest
    except BaseException as exc:
        manifest.update({
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
            "finished": _now(),
            "duration_seconds": round(time.time() - started, 2),
        })
        _write_run_state(run_dir, manifest)
        VOLUME.commit()
        raise


def _materialize_for_run(run_id: str) -> dict:
    from transformers import AutoTokenizer

    support.validate_run_id(run_id)
    run_dir = RUNS_ROOT / run_id
    selection_path = run_dir / "selection.json"
    if not selection_path.exists():
        raise FileNotFoundError(f"no selection.json for run {run_id}")
    selection = support.read_json(selection_path)
    if selection.get("status") != "SELECTED":
        raise RuntimeError(
            f"run {run_id} has no selected checkpoint: "
            + str(selection.get("reason")))
    run_manifest = support.read_json(run_dir / "run.json")
    if run_manifest.get("kind") != "training":
        raise RuntimeError(
            f"run {run_id} is {run_manifest.get('kind')!r}, not a full "
            "training run; a smoke adapter is never promoted")
    if not run_manifest.get("promotion_allowed", False):
        raise RuntimeError(
            f"run {run_id} is not promotion-eligible: "
            + ", ".join(run_manifest.get("promotion_blockers") or
                        ["no promotion evidence"]))
    best = selection["best"]
    destination = MODELS_ROOT / f"havyn-standard-{run_id}"
    marker = destination / "SELECTED.json"
    if destination.exists():
        if marker.exists():
            existing = support.read_json(marker)
            if (existing.get("run_id") == run_id
                    and existing.get("best_checkpoint") == best["name"]):
                return existing
        raise FileExistsError(
            f"model dir exists with different content: {destination}")
    destination.mkdir(parents=True)
    source = Path(best["path"])
    copied = []
    for path in sorted(source.iterdir()):
        if not path.is_file():
            continue
        if (path.name.startswith("adapter_")
                or path.name.startswith("tokenizer")
                or path.name in {"special_tokens_map.json",
                                 "chat_template.jinja", "vocab.json",
                                 "merges.txt", "added_tokens.json"}):
            shutil.copy2(path, destination / path.name)
            copied.append(path.name)
    if not any(name.startswith("adapter_model") for name in copied):
        raise RuntimeError("best checkpoint contains no adapter weights")
    if "tokenizer_config.json" not in copied:
        run_config = support.read_yaml(run_dir / "config.used.yaml")
        tokenizer = AutoTokenizer.from_pretrained(
            run_config["base_model"], trust_remote_code=True)
        tokenizer.save_pretrained(destination)
        copied.extend(["tokenizer_config.json", "tokenizer.json"])
    selected = {
        "run_id": run_id,
        "kind": "selected-adapter",
        "status": "SELECTED",
        "created": _now(),
        "best_checkpoint": best["name"],
        "best_step": best["global_step"],
        "best_eval_loss": best["best_eval_loss"],
        "final_checkpoint": selection["final"]["name"],
        "final_eval_loss": selection["final"]["best_eval_loss"],
        "selection_criterion": selection["criterion"],
        "checkpoint_path": best["path"],
        "adapter_path": str(destination),
        "base_model": run_manifest.get("base_model"),
        "dataset_manifest_sha256": run_manifest["dataset"]["manifest_sha256"],
        "config_sha256": run_manifest["config_sha256"],
        "gpu": run_manifest.get("actual_gpu") or run_manifest.get("gpu"),
        "versions": run_manifest.get("versions"),
        "files": sorted(copied),
    }
    support.write_json(marker, selected)
    support.write_json(
        MANIFESTS_ROOT / f"selected-{run_id}.json", selected)
    return selected


@app.function(
    image=IMAGE,
    volumes={MOUNT: VOLUME},
    timeout=CONFIG["runtime"]["function_timeout_seconds"],
    startup_timeout=CONFIG["runtime"]["startup_timeout_seconds"],
    max_containers=1,
    retries=0,
)
def run_select(request: dict) -> dict:
    _ensure_layout()
    selected = _materialize_for_run(
        support.validate_run_id(request["run_id"]))
    VOLUME.commit()
    return selected


def _gate_blockers(gates: dict) -> list[str]:
    blockers: list[str] = []
    for name, gate in gates.items():
        if name == "diagnosis_class" and isinstance(gate, dict):
            for label, detail in gate.items():
                if isinstance(detail, dict) and detail.get("status") in (
                        "FAIL", "NOT VERIFIED"):
                    blockers.append(f"diagnosis_class/{label}")
            continue
        if isinstance(gate, dict) and gate.get("status") in (
                "FAIL", "NOT VERIFIED"):
            blockers.append(name)
    return sorted(set(blockers))


def _evaluate_generator(generate, records: list[dict]) -> tuple[dict, dict, dict]:
    from evaluate_model import compute_gates, evaluate

    generations: dict[str, str] = {}

    def tracked(record: dict) -> str:
        text = generate(record)
        record_id = record.get("id") or ""
        generations[record_id] = text
        return text

    results = evaluate(records, tracked)
    results["gold_scores"] = []
    results["live"] = {
        "ran": False,
        "reason": "Packet Tracer is not reachable from Modal; run the live gate on Windows",
    }
    summary = support.summarize_scores(records, results, generations)
    gates = compute_gates(results)
    return results, summary, {"gates": gates, "generations": generations}


@app.function(
    image=IMAGE,
    volumes={MOUNT: VOLUME},
    gpu=CONFIG["gpu"]["default"],
    timeout=CONFIG["runtime"]["function_timeout_seconds"],
    startup_timeout=CONFIG["runtime"]["startup_timeout_seconds"],
    max_containers=1,
    retries=0,
)
def run_eval(request: dict) -> dict:
    import torch

    from evaluate_model import make_gpu_generator
    from format_examples import format_split

    _ensure_layout()
    run_id = support.validate_run_id(request["run_id"])
    eval_id = support.validate_run_id(
        request.get("eval_id") or support.make_run_id(None, "eval"))
    run_dir = RUNS_ROOT / run_id
    if not (run_dir / "run.json").exists():
        raise FileNotFoundError(f"unknown run: {run_id}")
    output = EVAL_REPORTS / eval_id
    if output.exists():
        raise FileExistsError(
            f"evaluation output already exists: {output}")
    selected = _materialize_for_run(run_id)
    report = {
        "stage": "evaluation",
        "run_id": run_id,
        "eval_id": eval_id,
        "started": _now(),
        "status": "RUNNING",
        "adapter_path": selected["adapter_path"],
        "best_checkpoint": selected["best_checkpoint"],
        "best_eval_loss": selected["best_eval_loss"],
        "final_checkpoint": selected.get("final_checkpoint"),
        "final_eval_loss": selected.get("final_eval_loss"),
        "disable_thinking": bool(
            request.get("disable_thinking",
                        CONFIG["evaluation"]["disable_thinking"])),
        "max_new_tokens": int(
            request.get("max_new_tokens",
                        CONFIG["evaluation"]["max_new_tokens"])),
    }
    started = time.time()
    max_new_tokens = int(
        request.get("max_new_tokens",
                    CONFIG["evaluation"]["max_new_tokens"]))
    disable_thinking = bool(
        request.get("disable_thinking",
                    CONFIG["evaluation"]["disable_thinking"]))
    standard_config = str(REPO / CONFIG["configs"]["standard"])
    records, skipped = format_split(DATASET_DIR / "heldout.jsonl")
    if skipped:
        raise RuntimeError(f"heldout rows failed to format: {skipped}")
    if not records:
        raise RuntimeError("heldout split formatted to zero records")
    try:
        trained_generate = make_gpu_generator(
            selected["base_model"], standard_config,
            adapter_dir=selected["adapter_path"], backend="transformers",
            disable_thinking=disable_thinking,
            max_new_tokens=max_new_tokens)
        results, summary, extra = _evaluate_generator(
            trained_generate, records)
        report["trained"] = {
            "summary": summary,
            "gates": extra["gates"],
        }
        generation_rows = [
            {"prompt_id": key, "generation": value}
            for key, value in sorted(extra["generations"].items())]
        del trained_generate
        gc.collect()
        torch.cuda.empty_cache()
        baseline_generations: list[dict] = []
        if bool(request.get("baseline",
                            CONFIG["evaluation"]["baseline"])):
            baseline_generate = make_gpu_generator(
                selected["base_model"], standard_config,
                adapter_dir=None, backend="transformers",
                disable_thinking=disable_thinking,
                max_new_tokens=max_new_tokens)
            baseline_results, baseline_summary, baseline_extra = (
                _evaluate_generator(baseline_generate, records))
            baseline_generations = [
                {"prompt_id": key, "generation": value}
                for key, value in sorted(
                    baseline_extra["generations"].items())]
            support.write_json(output / "heldout-scores-baseline.json", {
                key: baseline_results.get(key) for key in (
                    "intent_outputs", "diagnosis_scores",
                    "clarification_scores", "enhancement_scores")})
            report["baseline"] = {"summary": baseline_summary}
            trained_accuracy = summary.get("overall_accuracy")
            baseline_accuracy = baseline_summary.get("overall_accuracy")
            report["delta"] = {
                "trained_overall": trained_accuracy,
                "baseline_overall": baseline_accuracy,
                "absolute": (
                    round(trained_accuracy - baseline_accuracy, 4)
                    if trained_accuracy is not None
                    and baseline_accuracy is not None else None),
                "per_task": {
                    task: (
                        round(bucket["accuracy"] - base_accuracy, 4)
                        if (base_accuracy := (
                            baseline_summary["per_task"].get(task) or {})
                            .get("accuracy")) is not None else None)
                    for task, bucket in summary["per_task"].items()
                    if bucket["accuracy"] is not None
                },
            }
            report["regression"] = {
                "overall": bool(
                    report["delta"]["absolute"] is not None
                    and report["delta"]["absolute"] < 0),
                "tasks": sorted(
                    task for task, value
                    in report["delta"]["per_task"].items()
                    if value is not None and value < 0),
            }
        report["status"] = "completed"
        report["finished"] = _now()
        report["duration_seconds"] = round(time.time() - started, 2)
        report["heldout_records"] = len(records)
        report["heldout_skipped"] = skipped
        report["release_gate"] = extra["gates"]["release"]
        report["release_blockers"] = _gate_blockers(extra["gates"])
        support.write_json(output / "evaluation-report.json", report)
        support.write_json(output / "heldout-summary.json", summary)
        support.write_json(output / "heldout-scores.json", {
            key: results.get(key) for key in (
                "intent_outputs", "diagnosis_scores",
                "clarification_scores", "enhancement_scores")})
        support.write_json(output / "heldout-gates.json", extra["gates"])
        with (output / "generations.jsonl").open("w", encoding="utf-8") as handle:
            for row in generation_rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        if baseline_generations:
            with (output / "generations-baseline.jsonl").open(
                    "w", encoding="utf-8") as handle:
                for row in baseline_generations:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        support.write_json(
            MANIFESTS_ROOT / f"evaluation-{eval_id}.json", {
                key: report.get(key) for key in (
                    "run_id", "eval_id", "status", "adapter_path",
                    "best_checkpoint", "best_eval_loss", "final_checkpoint",
                    "final_eval_loss", "heldout_records", "release_gate",
                    "release_blockers", "trained", "baseline", "delta",
                    "regression")
            })
        print("EVALUATION RESULT", json.dumps({
            "run_id": run_id,
            "eval_id": eval_id,
            "status": report["status"],
            "best_checkpoint": report["best_checkpoint"],
            "final_checkpoint": report.get("final_checkpoint"),
            "heldout": summary,
            "baseline": (report.get("baseline") or {}).get("summary"),
            "delta": report.get("delta"),
            "regression": report.get("regression"),
            "release_gate": report["release_gate"],
            "release_blockers": report["release_blockers"],
            "duration_seconds": report["duration_seconds"],
        }, indent=2), flush=True)
        VOLUME.commit()
        return report
    except BaseException as exc:
        report.update({
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
            "finished": _now(),
            "duration_seconds": round(time.time() - started, 2),
        })
        support.write_json(output / "evaluation-report.json", report)
        VOLUME.commit()
        raise


def _local_dataset_sync() -> dict:
    local_dir = REPO / CONFIG["dataset"]["repo_dir"]
    report = support.verify_dataset(
        local_dir, CONFIG["dataset"]["expected_counts"],
        CONFIG["dataset"]["frozen_manifest"])
    if not report["ok"]:
        raise RuntimeError(
            "local frozen dataset failed verification: "
            + "; ".join(report["breaks"]))
    remote_manifest = (PurePosixPath(CONFIG["dataset"]["volume_dir"])
                       / CONFIG["dataset"]["frozen_manifest"])
    local_manifest_bytes = (local_dir / CONFIG["dataset"]["frozen_manifest"]
                            ).read_bytes()
    local_manifest_sha = support.sha256_bytes(local_manifest_bytes)
    remote_bytes = None
    try:
        remote_bytes = b"".join(VOLUME.read_file(str(remote_manifest)))
    except Exception:
        remote_bytes = None
    if remote_bytes is not None:
        if support.sha256_bytes(remote_bytes) != local_manifest_sha:
            raise RuntimeError(
                "the Modal Volume already holds a different frozen dataset "
                "manifest; refusing to overwrite it")
        return {"uploaded": False, "local": report,
                "remote_manifest_sha256": local_manifest_sha}
    with VOLUME.batch_upload(force=False) as batch:
        for path in sorted(local_dir.iterdir()):
            if path.is_file():
                batch.put_file(
                    str(path),
                    str(PurePosixPath(CONFIG["dataset"]["volume_dir"])
                        / path.name))
    VOLUME.commit()
    uploaded = b"".join(VOLUME.read_file(str(remote_manifest)))
    if support.sha256_bytes(uploaded) != local_manifest_sha:
        raise RuntimeError("dataset upload did not persist the frozen manifest")
    return {"uploaded": True, "local": report,
            "remote_manifest_sha256": local_manifest_sha}


def _write_local_receipt(run_id: str, stage: str, payload: dict) -> Path:
    support.validate_run_id(run_id)
    receipt = REPO / "havyn-stage5" / "runs" / run_id / f"{stage}-receipt.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    small = {key: value for key, value in payload.items()
             if key not in {"traceback", "generations"}}
    if len(json.dumps(small, default=str)) > 1_500_000:
        small = {"run_id": run_id, "stage": stage,
                 "status": payload.get("status"),
                 "note": "payload too large for a local receipt"}
    support.write_json(receipt, small)
    return receipt


def _gpu_rate(gpu: str) -> float:
    return float(CONFIG["gpu"]["hourly_usd_estimate"].get(gpu, 0.0))


def _print_summary(stage: str, payload: dict) -> None:
    print("=" * 72)
    print(f"HAVYN STAGE 5 MODAL {stage.upper()}")
    print("=" * 72)
    print("status:", payload.get("status"))
    for key in ("run_id", "duration_seconds", "estimated_gpu_usd",
                "error", "release_gate", "release_blockers"):
        if key in payload:
            print(f"{key}: {payload[key]}")
    if stage == "smoke":
        for name, test in (payload.get("tests") or {}).items():
            if isinstance(test, dict) and "status" in test:
                print(f"{name}: {test['status']}")
    if stage == "train":
        for key in ("best_checkpoint", "best_eval_loss", "checkpoint_dir"):
            print(f"{key}: {payload.get(key)}")
    if stage == "eval":
        trained = (payload.get("trained") or {}).get("summary") or {}
        print("heldout_overall_accuracy:", trained.get("overall_accuracy"))
        for task, bucket in (trained.get("per_task") or {}).items():
            print(f"  {task}: {bucket.get('accuracy')} "
                  f"({bucket.get('passed')}/{bucket.get('examples')})")
        delta = payload.get("delta") or {}
        print("baseline_overall_accuracy:", delta.get("baseline_overall"))
        print("delta_absolute:", delta.get("absolute"))
        print("regression:", payload.get("regression"))


@app.local_entrypoint()
def main(stage: str = "smoke", run_id: str = "", resume: bool = False,
         gpu: str = "", hf_secret: str = "", baseline: bool = True,
         max_new_tokens: int = 0) -> None:
    selected_gpu = support.validate_gpu(
        gpu or CONFIG["gpu"]["default"], CONFIG["gpu"]["allowed"])
    rate = _gpu_rate(selected_gpu)
    git = support.git_info(REPO)
    secrets = [modal.Secret.from_name(hf_secret)] if hf_secret else []
    if stage == "smoke":
        sync = _local_dataset_sync()
        smoke_id = run_id or support.make_run_id(
            git.get("commit"), "smoke")
        result = run_smoke.with_options(
            gpu=selected_gpu, secrets=secrets).remote({
                "run_id": smoke_id,
                "gpu": selected_gpu,
                "gpu_rate": rate,
                "git": git,
            })
        receipt = _write_local_receipt(smoke_id, "smoke", result)
        _print_summary("smoke", result)
        print("local_receipt:", receipt)
        print("dataset_sync:", sync["uploaded"])
        if result.get("status") != "PASS":
            raise SystemExit(1)
        return
    if stage == "train":
        if resume and not run_id:
            raise SystemExit("--resume requires the original --run-id")
        train_id = run_id or support.make_run_id(
            git.get("commit"), "train")
        _local_dataset_sync()
        result = run_train.with_options(
            gpu=selected_gpu, secrets=secrets).remote({
                "run_id": train_id,
                "resume": resume,
                "gpu": selected_gpu,
                "gpu_rate": rate,
                "git": git,
            })
        receipt = _write_local_receipt(train_id, "train", result)
        _print_summary("train", result)
        print("local_receipt:", receipt)
        if result.get("status") != "completed":
            raise SystemExit(1)
        return
    if stage == "select":
        if not run_id:
            raise SystemExit("--stage select requires --run-id")
        result = run_select.with_options(secrets=secrets).remote(
            {"run_id": run_id})
        receipt = _write_local_receipt(run_id, "select", result)
        _print_summary("select", result)
        print("adapter_path:", result.get("adapter_path"))
        print("local_receipt:", receipt)
        return
    if stage == "eval":
        if not run_id:
            raise SystemExit("--stage eval requires --run-id")
        _local_dataset_sync()
        eval_id = support.make_run_id(git.get("commit"), "eval")
        payload = {
            "run_id": run_id,
            "eval_id": eval_id,
            "max_new_tokens": max_new_tokens
            or CONFIG["evaluation"]["max_new_tokens"],
            "disable_thinking": CONFIG["evaluation"]["disable_thinking"],
            "baseline": baseline,
        }
        result = run_eval.with_options(
            gpu=selected_gpu, secrets=secrets).remote(payload)
        receipt = _write_local_receipt(run_id, f"eval-{eval_id}", result)
        _print_summary("eval", result)
        print("local_receipt:", receipt)
        if result.get("status") != "completed":
            raise SystemExit(1)
        return
    if stage == "full":
        sync = _local_dataset_sync()
        smoke_id = support.make_run_id(git.get("commit"), "smoke")
        smoke = run_smoke.with_options(
            gpu=selected_gpu, secrets=secrets).remote({
                "run_id": smoke_id, "gpu": selected_gpu, "gpu_rate": rate,
                "git": git})
        _print_summary("smoke", smoke)
        if smoke.get("status") != "PASS":
            raise SystemExit(1)
        train_id = support.make_run_id(git.get("commit"), "train")
        trained = run_train.with_options(
            gpu=selected_gpu, secrets=secrets).remote({
                "run_id": train_id, "resume": False, "gpu": selected_gpu,
                "gpu_rate": rate, "git": git})
        _print_summary("train", trained)
        if trained.get("status") != "completed":
            raise SystemExit(1)
        eval_id = support.make_run_id(git.get("commit"), "eval")
        evaluated = run_eval.with_options(
            gpu=selected_gpu, secrets=secrets).remote({
                "run_id": train_id,
                "eval_id": eval_id,
                "max_new_tokens": max_new_tokens
                or CONFIG["evaluation"]["max_new_tokens"],
                "disable_thinking": CONFIG["evaluation"]["disable_thinking"],
                "baseline": baseline,
            })
        _print_summary("eval", evaluated)
        print("dataset_sync:", sync["uploaded"])
        if evaluated.get("status") != "completed":
            raise SystemExit(1)
        return
    raise SystemExit(
        f"unknown stage {stage!r}; expected smoke, train, select, eval, full")


if __name__ == "__main__":
    print("run with: modal run havyn-stage5/modal_app.py --stage smoke")
