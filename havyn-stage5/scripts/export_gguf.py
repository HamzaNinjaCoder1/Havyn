"""S5.21 GGUF Q4_K_M export with SHA-256 manifests and model cards.

GPU path (Colab): Unsloth ``save_pretrained_gguf`` for the q4_k_m quant; if
Unsloth is unavailable, llama.cpp's convert/quantize path is used instead.
The hashing/manifest/card half is pure and offline-testable.

Usage (see notebooks/10-evaluation-export.ipynb):
  python export_gguf.py --model checkpoints/standard --quant q4_k_m \
      --out "$HAVYN_DRIVE/artifacts" --role standard

Release rule: the exported file's SHA-256 is written next to it and the
export is only released after the S5.19 gate report for the same model says
GO (checked when --gate-report is passed; otherwise the manifest records
that no gate was attached and the release stays honest about it).
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
for p in (str(REPO), str(REPO / "havyn-stage5" / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def write_manifest(model_file: Path, out_dir: Path, quant: str, role: str,
                   base_model: str, gate_report: Path | None) -> dict:
    manifest = {
        "artifact": model_file.name,
        "sha256": sha256_file(model_file),
        "size_bytes": model_file.stat().st_size,
        "quant": quant,
        "role": role,
        "base_model": base_model,
        "engine_commit": _git_commit(),
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if gate_report and Path(gate_report).exists():
        gates = json.loads(Path(gate_report).read_text(encoding="utf-8"))
        manifest["gate_release"] = gates.get("gates", {}).get("release")
        manifest["gate_report"] = str(gate_report)
    else:
        manifest["gate_release"] = None
        manifest["gate_report"] = None
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


MODEL_CARD_TEMPLATE = """# Havyn {role} model ({quant} GGUF)

- Base model: {base_model}
- Fine-tune: LoRA SFT on havyn-train-v1 (S5.17/S5.18 recipe)
- Quantisation: {quant}
- SHA-256: {sha256}
- Exported: {created}

## What it does

Converts natural-language networking requests into Havyn Intent Schema v1
JSON, asks clarification questions when information is missing, diagnoses
failed verification evidence and produces structured enhancement patches.
It NEVER invents low-level detail: the deterministic engine owns addressing,
ports, compilation and verification; this model is only the intent layer.

## Provenance and limits

- Gate release: {gate_release}
- Training report: {training_report}
- The live Packet Tracer gate (S5.20) result: {live_note}

## Refusals

When a request needs a feature Packet Tracer cannot do (guest isolation,
L3-switch DHCP, ASA 5505 DMZ, IoT registration, custom SSID), the model
refuses by name instead of half-configuring.
"""


def write_model_card(out_dir: Path, manifest: dict,
                     training_report: Path | None) -> Path:
    live_note = ("reported in the gate report"
                 if manifest.get("gate_release") == "GO"
                 else "NOT VERIFIED - no passing S5.20 gate is attached")
    loss = None
    if training_report and Path(training_report).exists():
        loss = json.loads(Path(training_report).read_text(
            encoding="utf-8")).get("final_loss")
    card = MODEL_CARD_TEMPLATE.format(
        role=manifest["role"], quant=manifest["quant"],
        base_model=manifest["base_model"], sha256=manifest["sha256"],
        created=manifest["created"],
        gate_release=manifest.get("gate_release") or "none attached",
        training_report=loss if loss is not None else "see Drive report dir",
        live_note=live_note)
    path = out_dir / f"MODEL-CARD-{manifest['role']}.md"
    path.write_text(card, encoding="utf-8")
    return path


def _git_commit() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            capture_output=True, text=True, check=True,
            cwd=str(REPO)).stdout.strip()
    except Exception:
        return None


def export_gguf(model_dir: Path, quant: str, out_dir: Path) -> Path:
    """GPU half. Unsloth first, llama.cpp fallback. NOT verifiable offline
    (rule 10) - the Colab run produces the artifact this function describes."""
    model_dir = model_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        from unsloth import FastLanguageModel
        model, tokenizer = FastLanguageModel.from_pretrained(
            model_name=str(model_dir), max_seq_length=4096, load_in_4bit=True)
        model.save_pretrained_gguf(
            str(out_dir), tokenizer,
            quantization_method=quant)
    except ImportError:
        convert_script = Path("llama.cpp/convert_hf_to_gguf.py")
        if not convert_script.exists():
            raise SystemExit(
                "neither unsloth nor llama.cpp/convert_hf_to_gguf.py is "
                "available; export runs on Colab")
        f16 = out_dir / f"{model_dir.name}-f16.gguf"
        subprocess.run(
            [sys.executable, str(convert_script), str(model_dir),
             "--outfile", str(f16), "--outtype", "f16"],
            check=True)
        quantize = Path("llama.cpp/quantize")
        target = out_dir / f"{model_dir.name}-{quant}.gguf"
        subprocess.run(
            [str(quantize), str(f16), str(target), quant.upper()], check=True)
        f16.unlink()
    produced = sorted(out_dir.glob("*.gguf"), key=lambda p: p.stat().st_mtime)
    if not produced:
        raise SystemExit("export produced no .gguf file")
    return produced[-1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=None,
                        help="adapter/merged model directory")
    parser.add_argument("--quant", default="q4_k_m")
    parser.add_argument("--out", default="havyn-stage5/artifacts")
    parser.add_argument("--role", default="standard", choices=["standard", "lite"])
    parser.add_argument("--base-model", default="Qwen/Qwen3-8B")
    parser.add_argument("--gate-report",
                        default="havyn-stage5/evaluation/gate-report.json")
    parser.add_argument("--training-report", default=None)
    parser.add_argument("--manifest-only", action="store_true",
                        help="hash + manifest + card for an existing gguf "
                             "(offline path; no model export)")
    args = parser.parse_args()
    if not args.model and not args.manifest_only:
        parser.error("--model is required unless --manifest-only")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.manifest_only:
        existing = sorted(out_dir.glob("*.gguf"))
        if not existing:
            raise SystemExit("--manifest-only needs a .gguf in --out")
        model_file = existing[-1]
    else:
        model_file = export_gguf(Path(args.model), args.quant, out_dir)

    manifest = write_manifest(
        model_file, out_dir, args.quant, args.role, args.base_model,
        Path(args.gate_report) if args.gate_report else None)
    card = write_model_card(
        out_dir, manifest,
        Path(args.training_report) if args.training_report else None)
    print(json.dumps({
        "artifact": str(model_file), "sha256": manifest["sha256"],
        "model_card": str(card), "gate_release": manifest["gate_release"],
    }, indent=2))


if __name__ == "__main__":
    main()
