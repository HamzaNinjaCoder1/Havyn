# Colab + Hugging Face readiness

Date: 2026-10-29. Preparation only: no GPU was used, no training was started,
no model weights were downloaded, and nothing was uploaded to Hugging Face.

## Headline

| Area | Readiness | What is true |
| --- | --- | --- |
| Colab pipeline preparation | **90%** | scripts, notebooks, gates, dry-run, preflight, storage and resume paths exist and were exercised offline; not executed in Colab |
| Hugging Face readiness | **80%** | auth helper, smoke test, private dataset snapshot, publish gate (default off); public Hub access verified; token path untested because no token exists here |
| Model selection | **0% measured** | 4 Standard + 4 Lite candidates registered; no winner declared; bake-off NOT RUN |
| Training readiness | **NOT VERIFIED** | preflight is implemented but cannot pass on this CPU-only machine; it is designed to pass on a GPU runtime after `requirements-colab.txt` |
| Live Packet Tracer | **NOT RUN** | Colab cannot reach a local Packet Tracer; this gate stays on the Windows machine |

## 1. Audit of the existing pipeline

| Component | State | Action taken |
| --- | --- | --- |
| `train_lora.py` | worked, but the transformers fallback passed `load_in_4bit` (invalid) and the report lacked provenance | fixed `BitsAndBytesConfig`, added code/config/dataset-freeze pins, checkpoint path, weight-aware VRAM reserve |
| `format_examples.py` | silently skipped 1,176 English intent rows and trained on invented `source_excerpt` text | renders a faithful request from the intent, always shows the clarification draft, de-duplicates identical chat rows |
| `assemble_dataset.py` / `freeze_dataset.py` | family split, leakage checks, freeze | unchanged logic; prompt-key bug fixed earlier; training refuses unfrozen data |
| `evaluate_model.py` | gates and live-gate guard | unchanged; live gate still refuses without Packet Tracer |
| `benchmark_harness.py` | bake-off harness had the same invalid `load_in_4bit` call and measured global peak VRAM | fixed `BitsAndBytesConfig`, per-candidate peak reset, per-model peak recorded |
| `export_gguf.py` | hash/manifest/card half works offline | unchanged; publish moved to a separate, double-gated script |
| `improvement_loop.py` | classify → plan → delta | unchanged; exercised in dry-run |
| Notebooks 01–11 | setup, data, training, eval, export | env-cell order fixed, duplicate setup cell removed, beginner dashboard added, DRY_RUN/GPU gates added, Drive paths fixed |
| Configs | train/bake-off YAML | added `configs/models.yaml` registry and `configs/colab.yaml`; added `model_weight_gb` reserves |
| Requirements | only repo `pyproject.toml` | added `requirements-colab.txt` (torch deliberately unpinned) |

## 2. Ready

- Repo clone/update, editable install, offline test suite (`colab_setup.py`).
- Frozen dataset (2,332 rows) with `FROZEN.json`; training refuses any other state.
- Preflight (`colab_preflight.py`) covering GPU, CUDA, VRAM, Python, packages,
  token presence, git, freeze, registry, disk, publish safety.
- Dashboard (`colab_dashboard.py`) with the ten beginner facts.
- Offline DRY_RUN (`dry_run_check.py`) for every stage; all stages pass here.
- HF auth helper, cheap smoke test (public Hub verified), private dataset
  snapshot with provenance, SHA-256, Gold exclusion, licence and card.
- Model registry with licence, size, context, VRAM, backend and limitations.
- Teacher/review configured through `TEACHER_MODEL_ID` / `REVIEW_MODEL_ID`.
- Resumable training: Drive checkpoints, `save_total_limit: 3`, `--resume`,
  newest-checkpoint selection, provenance in the report.
- Publish is off by default and requires two switches.

## 3. Needs modification by the owner

- Owner Gold: 0/100 adopted, so the evaluation and live gates have no real
  prompts yet. The pipeline is ready; the data is not.
- Cross-family review: package ready, model not run; `REVIEW_MODEL_ID` must be
  a family different from the teacher and the fine-tune base.
- The Stage 3/4 engine is committed separately as `d47588c` with sanitized lab
  credentials; pins are clean (`src-tree:5e437ba66b14`).
- Gated models (Llama 3.1/3.2, Gemma 2) require accepting their licences on the
  Hugging Face account behind `HF_TOKEN`.

## 4. Windows-specific vs Linux/Colab

| Windows-specific | Linux/Colab |
| --- | --- |
| Packet Tracer, the bridge and the live gate | all training/evaluation scripts |
| the bridge timing test flake | pathlib-based paths, `/content` roots |
| local `D:\` checkout | Drive at `/content/drive/MyDrive/havyn-stage5` |
| available RAM/VRAM detection differs | CUDA detection via `torch.cuda` |

No script under `havyn-stage5/scripts/` hard-codes `C:\` or `D:\`. The Colab
paths are environment/default values, not concatenated user input.

## 5. What requires what

| Requirement | Needed for |
| --- | --- |
| GPU + CUDA (≥12 GB Standard, ≥6 GB Lite) | bake-off, training, evaluation, GGUF, cross-family review |
| ≥24 GB for the teacher/review models at 4-bit | teacher prompt generation, independent review |
| `HF_TOKEN` | gated models, any private download/upload, whoami |
| `HF_USERNAME` (optional) | naming the private dataset/model repository |
| `GITHUB_TOKEN` (optional) | cloning a private fork; the public repo needs none |
| Packet Tracer (Windows) | the 50-prompt live gate only |

## 6. Model candidates (no winner declared)

Standard (7–9B): `Qwen/Qwen3-8B`, `meta-llama/Meta-Llama-3.1-8B-Instruct`,
`mistralai/Mistral-7B-Instruct-v0.3`, `google/gemma-2-9b-it`.

Lite (2–4B): `Qwen/Qwen3-4B`, `microsoft/Phi-3.5-mini-instruct`,
`meta-llama/Llama-3.2-3B-Instruct`, `google/gemma-2-2b-it`.

Teacher: `Qwen/Qwen3-32B` via `TEACHER_MODEL_ID`.
Review: `mistralai/Mistral-Small-24B-Instruct-2501` via `REVIEW_MODEL_ID`
(different family from the teacher). Both are `configured_not_run`.

Every candidate is `candidate_not_measured`; the bake-off decides, and
`configs/bakeoff.yaml` must not change after results are recorded.

## 7. Files created or modified in this pass

Created: `scripts/merge_lora.py`, `configs/models.yaml`, `configs/colab.yaml`,
`requirements-colab.txt`, `scripts/hf_auth.py`, `scripts/hf_smoke_test.py`,
`scripts/prepare_hf_dataset.py`, `scripts/hf_publish.py`,
`scripts/colab_preflight.py`, `scripts/colab_dashboard.py`,
`scripts/colab_ux.py`, `scripts/dry_run_check.py`, `scripts/merge_lora.py`,
`datasets/hf-dataset-card-template.md`, `notebooks/README.md`,
`reports/COLAB_RUNBOOK.md`, this report.

Modified: `train_lora.py`, `benchmark_harness.py` (engine verification, hallucination rate, per-language and repeatability metrics), `format_examples.py`,
`generate_prompts.py`, `roundtrip_verify.py`, all 11 notebooks,
`configs/train-standard.yaml`, `configs/train-lite.yaml`, `.gitignore`.

Generated locally (gitignored): `datasets/hf-export/havyn-sft-20261029-74d529e2/`
(7 files, 8.9 MB), with a SHA-256 table in the card, manifest and Gold exclusion.

## 8. Exact remaining user actions

1. **USER ACTION REQUIRED** — Colab: Runtime → Change runtime type → GPU
   (L4/A100). Standard needs ≥12 GB; teacher/review need ≥24 GB.
2. **USER ACTION REQUIRED** — Colab Secrets: add `HF_TOKEN` (read token) and
   optionally `HF_USERNAME`; enable notebook access. Accept the Llama/Gemma
   licences on the Hugging Face account if those candidates are used.
3. Run notebook 01, then 02, then 03 (bake-off). Record results before
   choosing a winner.
4. Run 04 → 05 → 06 → 07 → 08 only if regenerating data; they end with
   assemble/validate/freeze.
5. Run 09 standard, then 09 lite. Keep Drive mounted so checkpoints survive.
6. Run 10 evaluation; only a PASS gate may proceed to GGUF export.
7. Run the live gate on the Windows machine with Packet Tracer open.
8. **USER ACTION REQUIRED** — publish only if you decide to: set
   `HF_PUBLISH=true` and pass `--publish`; repositories are created private.

## 9. GPU, storage and cost

- GPU: none consumed during preparation. Bake-off needs one 12–16 GB GPU for
  the Standard candidates; training needs 12–16 GB Standard / 6–8 GB Lite;
  teacher+review need ~24 GB each.
- Storage (planning estimates, `colab_dashboard.py`): dataset 0.01 GB,
  downloads Standard ~5 GB, Lite ~2.5 GB, checkpoints ~6 GB, artifacts ~4 GB.
  Minimum free ~17.5 GB; with teacher+review ~49.5 GB. Recommend ≥60 GB free.
- Drive that should survive runtime termination: `datasets/`, `checkpoints/`,
  `reports/`, `evaluation/`, `artifacts/`, manifests. Base-model caches can be
  re-downloaded.
- Paid services: none required. Colab free GPU may be too small/slow; Colab Pro
  is optional. Private HF repositories are free.

## 10. What was actually verified here

| Check | Result |
| --- | --- |
| Offline test suite | 1,197 passed / 32 skipped (before this pass; re-run after changes) |
| `dry_run_check.py` all stages | PASS (imports, engine, bake-off inputs, dataset, format, preflight, train, evaluate, export, improve, cross-family package) |
| `colab_preflight.py` | structural checks pass; packages missing on this CPU machine (expected), GPU absent (expected) |
| `hf_smoke_test.py --dry-run` | PASS |
| `hf_smoke_test.py` (public Hub) | PASS: tiny model metadata resolved, tiny tokenizer loaded (~1 MB); no weights downloaded |
| `hf_auth.py` | token absent; token path untested |
| `prepare_hf_dataset.py` | snapshot built and hashed; 1,989/223/120 rows; provenance synthetic; 0 Gold |
| `hf_publish.py` | upload disabled with no flag and with only `HF_PUBLISH=true` |
| Colab execution | **not run** — this machine is not Colab |
| GPU training | **not run** |
| Model selection | **not measured** |

The first real Colab run starts with:

```bash
!git clone https://github.com/HamzaNinjaCoder1/MCP-Packet-Tracer.git /content/MCP-Packet-Tracer
%cd /content/MCP-Packet-Tracer
!pip install -q -e .[test]
!pip install -q -r havyn-stage5/requirements-colab.txt
!python havyn-stage5/scripts/colab_preflight.py --require-gpu --require-hf
!python havyn-stage5/scripts/hf_smoke_test.py
```

Then open `notebooks/01-environment-setup.ipynb` and follow the runbook.


## 11. Final parallel-setup report (the 15 required items)

1. **Colab readiness:** 90% prepared, **0% executed** (not opened in Colab).
2. **Hugging Face readiness:** 80% (public connectivity/tokenizer verified; token path and any upload untested).
3. **Standard candidates:** Qwen3-8B, Llama-3.1-8B-Instruct, Mistral-7B-Instruct-v0.3, Gemma-2-9B-it.
4. **Lite candidates:** Qwen3-4B, Phi-3.5-mini-instruct, Llama-3.2-3B-Instruct, Gemma-2-2b-it (2.6B quality floor).
5. **Teacher:** `Qwen/Qwen3-32B`, `TEACHER_MODEL_ID`, `configured_not_run`.
6. **Independent reviewer:** `Mistral-Small-24B-Instruct-2501`, `REVIEW_MODEL_ID`, different family, `configured_not_run`.
7. **Files/notebooks:** 15 new files, 11 notebooks updated, 2 training configs, 2 test files, `.gitignore`; full list in section 7.
8. **User actions:** add `HF_TOKEN` (+ optional `HF_USERNAME`); select a GPU runtime; accept Llama/Gemma licences; run 01→11; run the live gate on Windows; publish only on explicit decision.
9. **GPU:** ≥12 GB Standard, ≥6 GB Lite, ≥24 GB teacher/review. None consumed.
10. **Storage:** dataset 0.01 GB, downloads ~7.5 GB, checkpoints ~6 GB, artifacts ~4 GB; ~17.5 GB minimum, ~49.5 GB with teacher/review; recommend ≥60 GB.
11. **Secrets:** `HF_TOKEN` required for gated models/uploads; `HF_USERNAME` and `GITHUB_TOKEN` optional; never printed or committed.
12. **Paid service:** none required; Colab Pro optional; private HF repos free.
13. **Model downloaded:** no weights; only the ~1 MB smoke-test tokenizer.
14. **GPU consumed:** none.
15. **First real commands:** the clone/install/preflight block in section 10, then notebook 01 and `reports/COLAB_RUNBOOK.md`.

Claims deliberately not made: training ready, model selected, Hugging Face
configured with a token, or Colab verified. None of those is true yet.
