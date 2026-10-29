# Havyn Stage 5 notebooks

The repository keeps its established numbering. The requested one-click flow
maps onto it as follows; notebooks were not renamed.

| Requested step | Notebook / command | Notes |
| --- | --- | --- |
| 01 setup | `01-environment-setup.ipynb` | clones, installs, runs the offline suite, prints the dashboard |
| 02 engine validation | `02-engine-validation.ipynb` | schema/lint/compile tests; no GPU |
| 03 model bake-off | `03-base-model-benchmark.ipynb` | builds 125 bench prompts, runs `configs/bakeoff.yaml`; GPU |
| 04 dataset generation | `04-dataset-generation.ipynb` | intents + template prompts; CPU |
| 04 dataset validation | `05-dataset-verification.ipynb` | independent-family round-trip; GPU + `REVIEW_MODEL_ID` |
| 05 format dataset | `08-question-data.ipynb` (last cell) | `assemble_dataset.py` → `validate_training_split.py` → `freeze_dataset.py` |
| 05 diagnosis/enhancement data | `06-diagnosis-data.ipynb`, `07-enhancement-data.ipynb` | CPU |
| 06 training preflight | `09-training.ipynb` first training cell (`gate("train")`) or `scripts/colab_preflight.py` | fails before GPU if anything is missing |
| 07 standard training | `09-training.ipynb` standard cell | Qwen3-8B, Drive checkpoints, resumable |
| 08 lite training | `09-training.ipynb` lite cell | Qwen3-4B |
| 09 evaluation | `10-evaluation-export.ipynb` evaluation cell | gates: JSON 100%, lint ≥95%, Gold ≥90%, diagnosis ≥85% |
| 10 live gate and export | `10-evaluation-export.ipynb` merge → evaluate → export cells + `evaluate_model.py --live` | merge adapter first; live gate needs Packet Tracer and owner Gold |
| 11 improvement loop | `11-improvement-loop.ipynb` | classify → delta → reassemble → retrain |

## DRY_RUN

Set `DRY_RUN=true` in the environment before opening a notebook. Every
expensive notebook then runs `scripts/dry_run_check.py --stage <stage>` instead
of the GPU operation. That check exercises imports, configuration, paths,
dataset loading, formatting, model resolution, evaluator wiring and output
directories. It does not download weights, use a token, or touch a GPU.

```
DRY_RUN=true python havyn-stage5/scripts/dry_run_check.py
DRY_RUN=true python havyn-stage5/scripts/dry_run_check.py --stage train,evaluate
```

The data-generation notebooks skip their generation cells under `DRY_RUN`
instead of rewriting the frozen dataset.

## Gates and warnings

- `colab_ux.gate(stage)` runs `colab_preflight.py --require-gpu` before any
  expensive cell and stops on failure. In `DRY_RUN` it runs the offline check.
- Every expensive cell has a markdown warning and prints a runtime banner.
- `train_lora.py` refuses an unfrozen or byte-changed dataset.
- `evaluate_model.py` never converts NOT VERIFIED into PASS.
- `hf_publish.py` requires **both** `HF_PUBLISH=true` and `--publish`; the
  default is no upload.

## Secrets

Use Colab Secrets (`HF_TOKEN`, optional `HF_USERNAME`, `GITHUB_TOKEN`). The
notebooks read them through `hf_auth.py`; nothing prints or writes the token.
Never put a token in a cell, a Drive file, or Git.

## First run

Follow `../reports/COLAB_RUNBOOK.md`. It marks every **USER ACTION REQUIRED**
step and the exact command for each stage.
