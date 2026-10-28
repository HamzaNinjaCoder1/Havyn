"""Generate the S5.3 Colab notebooks into havyn-stage5/notebooks/.

Notebooks are generated, not hand-maintained: every one starts from the same
setup cell (repo clone, editable install, offline test gate, Drive mount, env
report) and then calls into the versioned scripts in havyn-stage5/scripts/.
Regenerate after editing:  python havyn-stage5/scripts/make_notebooks.py
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "notebooks"

SETUP_CELL = """\
# Havyn Stage 5 - environment setup and verification gate.
# Every notebook runs this first: no notebook trains or generates data on an
# unverified environment.
import sys, shutil, os
if not shutil.which("git"):
    raise RuntimeError("git missing")
!git clone --depth 1 https://github.com/HamzaNinjaCoder1/MCP-Packet-Tracer.git /content/MCP-Packet-Tracer 2>/dev/null || git -C /content/MCP-Packet-Tracer pull --ff-only
import sys
sys.path.insert(0, "/content/MCP-Packet-Tracer/havyn-stage5/scripts")
os.chdir("/content/MCP-Packet-Tracer")
from colab_setup import setup, save_report
env = setup(run_tests=True, use_drive=True)
assert not env.errors, f"environment verification failed: {env.errors}"
assert env.tests_passed, "offline suite failed - STOP, do not continue"
from env_report import build_report, recommend_config
report = build_report(repo="/content/MCP-Packet-Tracer", dataset_version="v0")
import json, pathlib
out_dir = pathlib.Path(env.drive_dir or "/content/havyn-artifacts")
out_dir.mkdir(parents=True, exist_ok=True)
(out_dir / "env-report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(recommend_config(report.get("gpu", {})), indent=2))
print(json.dumps(report, indent=2)[:2000])
"""


def cell(source: str, kind: str = "code") -> dict:
    return {
        "cell_type": kind,
        "metadata": {},
        "source": source.splitlines(keepends=True),
        **({"outputs": [], "execution_count": None} if kind == "code" else {}),
    }


def nb(title: str, summary: str, cells: list[dict]) -> dict:
    head = [
        cell(f"# {title}\n\n{summary}", kind="markdown"),
        cell(SETUP_CELL),
    ]
    return {
        "cells": head + cells,
        "metadata": {
            "colab": {"provenance": []},
            "kernelspec": {"display_name": "Python 3", "name": "python3"},
            "language_info": {"name": "python"},
            "accelerator": "GPU",
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


NOTEBOOKS: dict[str, tuple[str, list[dict]]] = {
    "01-environment-setup": (
        "Environment setup",
        "Clones the repo, installs the package, runs the offline test suite "
        "and records the environment report (GPU, CUDA, versions, commit).",
        [
            cell("env = setup(run_tests=True, use_drive=True)\nassert not env.errors"),
        ],
    ),
    "02-engine-validation": (
        "Engine validation",
        "Runs the complete offline engine chain (schema, lint, expander, "
        "IPAM, compiler) over the manual example set and reports failures.",
        [
            cell(
                "!python -m pytest tests/autopilot/test_s5_intent_schema.py -q\n"
                "!python -m pytest tests -q -x --ignore=tests/autopilot/test_live_preflight.py"
            ),
        ],
    ),
    "03-base-model-benchmark": (
        "Base-model bake-off (S5.4)",
        "Runs identical prompts through every candidate in configs/bakeoff.yaml "
        "and writes per-model summaries plus the comparison table. No "
        "fine-tuning happens here.",
        [
            cell(
                "!python havyn-stage5/scripts/build_bench_prompts.py "
                "--valid havyn-stage5/datasets/havyn-intents-v1/valid.jsonl "
                "--target 120 "
                "--out havyn-stage5/datasets/bench-prompts-v1.jsonl"
            ),
            cell(
                "from benchmark_harness import run_bakeoff\n"
                "table = run_bakeoff(\n"
                "    config_path='havyn-stage5/configs/bakeoff.yaml',\n"
                "    prompts='havyn-stage5/datasets/bench-prompts-v1.jsonl',\n"
                "    out_dir=str(pathlib.Path(env.drive_dir) / 'bakeoff'),\n"
                ")\nprint(table.read_text())"
            ),
        ],
    ),
    "04-dataset-generation": (
        "Intent dataset generation (S5.6/S5.8)",
        "Generates intents across the coverage matrix and chains every one "
        "through schema validation, intent lint, expansion, spec lint and "
        "compilation. Fixed seed; failures are categorised, never hidden.",
        [
            cell(
                "!PYTHONPATH=. python havyn-stage5/scripts/generate_intents.py "
                "--out havyn-stage5/datasets/havyn-intents-v1"
            ),
            cell(
                "import json\n"
                "cov = json.load(open('havyn-stage5/datasets/havyn-intents-v1/coverage-report.json'))\n"
                "print(json.dumps(cov, indent=2)[:3000])"
            ),
        ],
    ),
    "05-dataset-verification": (
        "Dataset verification (S5.10)",
        "Round-trip verification: an independent model family reconstructs "
        "intents from prompts; field-by-field comparison routes examples into "
        "ACCEPT / REVIEW queues.",
        [
            cell(
                "# Requires INDEPENDENT_MODEL_ID via Colab secrets:\n"
                "#   from google.colab import userdata; os.environ['HF_TOKEN'] = userdata.get('HF_TOKEN')\n"
                "#   os.environ['INDEPENDENT_MODEL_ID'] = userdata.get('INDEPENDENT_MODEL_ID')\n"
                "!PYTHONPATH=havyn-stage5/scripts:. python havyn-stage5/scripts/roundtrip_verify.py \\\n"
                "  --prompts havyn-stage5/datasets/havyn-prompts-v1/prompts.jsonl \\\n"
                "  --out havyn-stage5/datasets/havyn-roundtrip-v1"
            ),
        ],
    ),
    "06-diagnosis-data": (
        "Diagnosis data generation (S5.11)",
        "Generates diagnosis examples from certified fault injectors, balanced "
        "across fault classes with healthy / UNKNOWN / multi-fault quotas.",
        [
            cell(
                "!PYTHONPATH=havyn-stage5/scripts:. python havyn-stage5/scripts/generate_diagnosis.py \\\n"
                "  --count 2000 --out havyn-stage5/datasets/havyn-diagnosis-v1"
            ),
        ],
    ),
    "07-enhancement-data": (
        "Enhancement data generation (S5.12)",
        "Before/mutation/after triples through the compiler, labelled as "
        "structured patches; ~10% impossible requests whose correct output is "
        "a named refusal.",
        [
            cell(
                "!PYTHONPATH=havyn-stage5/scripts:. python havyn-stage5/scripts/generate_enhancement.py \\\n"
                "  --count 1000 --out havyn-stage5/datasets/havyn-enhancement-v1"
            ),
        ],
    ),
    "08-question-data": (
        "Clarification question data (S5.13)",
        "Removes required fields from valid intents; the model must ask only "
        "what is genuinely missing. ~40% of examples need no question at all.",
        [
            cell(
                "!PYTHONPATH=havyn-stage5/scripts:. python havyn-stage5/scripts/generate_clarification.py \\\n"
                "  --count 500 --out havyn-stage5/datasets/havyn-clarification-v1"
            ),
        ],
    ),
    "09-training": (
        "Fine-tuning (S5.17/S5.18)",
        "Unsloth + TRL LoRA fine-tuning for the Standard (Qwen3-8B) and Lite "
        "(Qwen3-4B) models. Checkpoints land on Drive and training is "
        "resumable - never assume one uninterrupted session.",
        [
            cell(
                "!pip install -q unsloth\n"
                "# No secrets in the notebook: read tokens via Colab secrets only.\n"
                "#   from google.colab import userdata; os.environ['HF_TOKEN'] = userdata.get('HF_TOKEN')"
            ),
            cell(
                "!PYTHONPATH=havyn-stage5/scripts:. python havyn-stage5/scripts/train_lora.py \\\n"
                "  --config havyn-stage5/configs/train-standard.yaml --resume\n"
                "# the config's checkpoint_dir already points at Drive; rerun\n"
                "# with train-lite.yaml for the Lite model"
            ),
        ],
    ),
    "10-evaluation-export": (
        "Evaluation and export (S5.19/S5.21)",
        "Runs the evaluation gates (schema/lint/gold/diagnosis thresholds), "
        "then exports GGUF Q4_K_M with SHA-256 manifests and model cards.",
        [
            cell(
                "!PYTHONPATH=havyn-stage5/scripts:. python havyn-stage5/scripts/evaluate_model.py \\\n"
                "  --model checkpoints/standard --gold havyn-stage5/datasets/gold-v1 \\\n"
                "  --out havyn-stage5/evaluation"
            ),
            cell(
                "!PYTHONPATH=havyn-stage5/scripts:. python havyn-stage5/scripts/export_gguf.py \\\n"
                "  --model checkpoints/standard --quant q4_k_m \\\n"
                "  --out \"$HAVYN_DRIVE/artifacts\""
            ),
        ],
    ),
    "11-improvement-loop": (
        "Continuous improvement loop (S5.22)",
        "Classifies every failed gate and example from the gate report, "
        "builds delta training examples from engine-verified failures, and "
        "plans the reassemble -> retrain -> re-evaluate cycle.",
        [
            cell(
                "!PYTHONPATH=havyn-stage5/scripts:. python havyn-stage5/scripts/improvement_loop.py \\\n"
                "  --evaluation havyn-stage5/evaluation/gate-report.json \\\n"
                "  --build-delta"
            ),
            cell(
                "# after the delta exists, reassemble and retrain:\n"
                "#   python havyn-stage5/scripts/assemble_dataset.py\n"
                "#   python havyn-stage5/scripts/train_lora.py --config ... --resume"
            ),
        ],
    ),
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for stem, (title, summary, cells) in NOTEBOOKS.items():
        path = OUT / f"{stem}.ipynb"
        path.write_text(
            json.dumps(nb(title, summary, cells), indent=1, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
