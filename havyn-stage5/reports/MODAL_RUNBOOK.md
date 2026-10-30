# Havyn Stage 5 Modal runbook

This runbook replaces the Colab training half for Qwen3-8B LoRA. The frozen
dataset, training recipe and evaluation gates are unchanged. The local Windows
machine is control-plane only: no model weights, Hugging Face cache, checkpoint
or adapter is written to `C:` or `D:`.

## What runs where

| Location | Responsibility |
| --- | --- |
| Local Windows PC | git, configs, manifests, dataset hash verification, `modal run`, small JSON receipts |
| GitHub | code, configs, manifests (no weights) |
| Modal Volume `havyn-stage5` | frozen dataset copy, Hugging Face cache, base model, checkpoints, adapters, evaluation artifacts |
| Google Drive | independent dataset backup (unchanged, not used by this job) |

## Prerequisites

```bash
python -m pip install -e ".[modal]"
python -m modal token new
```

`modal token new` opens a browser and writes `~/.modal.toml`. On this machine
the `modal.exe` entry point is not on `PATH`, so the commands below use
`python -m modal`. `modal run ...` is equivalent once the Scripts directory is
on `PATH`.

The base model `Qwen/Qwen3-8B` is public, so no Hugging Face token is required
for the Standard run. If a gated model is added later, create a Modal secret
named `huggingface` containing `HF_TOKEN` and pass `--hf-secret huggingface`.

## Stage 0: local preflight (no GPU, no auth needed)

```bash
python havyn-stage5/scripts/modal_preflight.py
```

Verifies the frozen dataset hashes/counts, config hashes, preserved
model/LoRA recipe, pinned container requirements, cloud-only cache paths, the
Modal CLI version, the app definition and the runbook. Exit code 2 with
`next_action: python -m modal token new` means every local check passed and
only authentication is missing.

## Stage 1: smoke tests A-F

```bash
python -m modal run havyn-stage5/modal_app.py --stage smoke
```

The local entrypoint verifies the frozen dataset, uploads it once to the Modal
Volume if absent, and refuses to overwrite a different remote copy. The remote
function then runs, in one GPU container:

| Test | Checks |
| --- | --- |
| A GPU | CUDA, device name, capability, VRAM, BF16 support, BF16 matmul |
| B environment | Python, PyTorch, Transformers, TRL, PEFT, Accelerate, Datasets, bitsandbytes, safetensors; cache paths under `/data`; full `pip freeze` |
| C dataset | SHA-256 against `FROZEN.json`, 1,989/223/120 rows, formatting, tokenizer lengths |
| D model | Qwen3-8B identity, parameter count, dtype, 4-bit NF4 quantization, device, peak VRAM, tokenizer identity, chat template hash |
| E LoRA | trainable parameters, frozen base, target-module matches, rank/alpha/dropout |
| F tiny training | 2 optimizer steps, forward/backward, optimizer, checkpoint, validation loss, logging, adapter file |

Only `status: PASS` unlocks training. The report is written to
`/data/havyn-stage5/reports/smoke/<run-id>/smoke-report.json` and
`/data/havyn-stage5/manifests/smoke-<run-id>.json`.

Default GPU: `A10G` (24 GB, Ampere, BF16). Allowed: `L4`, `A10G`, `L40S`,
`A100-40GB`. H100 is deliberately not allowed. Override with `--gpu L4`.

## Stage 2: full training

```bash
python -m modal run havyn-stage5/modal_app.py --stage train
```

The run ID is generated from UTC time, git SHA and `train`. The job refuses to
start without a passing smoke report for the same dataset manifest and base
model. It then trains the unchanged `train-standard.yaml` recipe: Qwen3-8B,
4-bit NF4, r=32, alpha=32, dropout 0, 7 target modules, sequence length 2048,
3 epochs, effective batch 16, LR 2e-4 cosine, warmup 5%, bf16, seed 20261028.

One Modal-only override is recorded in every manifest: `save_total_limit` is
set to `null` so no checkpoint is deleted automatically. The source recipe's
value (`3`) is unchanged for Colab.

Outputs:

```text
/data/havyn-stage5/
  datasets/havyn-train-v1/
  hf-cache/                    Hugging Face weights and tokenizer cache
  checkpoints/<run-id>/checkpoint-*/
  runs/<run-id>/run.json
  runs/<run-id>/config.original.yaml
  runs/<run-id>/config.used.yaml
  runs/<run-id>/selection.json
  runs/<run-id>/training-analysis.json
  runs/<run-id>/reports/training-report.json
  models/havyn-standard-<run-id>/
  manifests/<run-id>.json
  reports/evaluation/<eval-id>/
```

`selection.json` picks the checkpoint with the lowest validation loss at its
own training step, never the later checkpoint that merely inherited an earlier
eval number. `training-analysis.json` records the overfitting/underfitting
assessment. A run is promotion-eligible only when that assessment is
`stable`: a validation-loss regression after its minimum, no measurable
improvement, or fewer than two validation evaluations blocks promotion with
`promotion_allowed: false`. The final adapter is copied to
`models/havyn-standard-<run-id>/` with `SELECTED.json` recording the run ID,
step, eval loss, dataset hash, config hash, GPU and software versions.
Existing directories are never overwritten.

## Resume after interruption or preemption

```bash
python -m modal run havyn-stage5/modal_app.py --stage train --resume --run-id <original-run-id>
```

Resume requires the original run ID. The script restores the newest valid
checkpoint including optimizer and scheduler state, records the original run
ID, checkpoint path, dataset hash, config hash and model identity in
`resume_history`, and never reports a resumed run as fresh. A completed run
cannot be resumed; start a new run ID instead.

## Stage 3: evaluation

```bash
python -m modal run havyn-stage5/modal_app.py --stage eval --run-id <run-id>
```

Evaluation loads the selected adapter on the 4-bit base, disables Qwen3
thinking, generates the 120 held-out records, and scores:

- JSON/schema validity and lint pass rate
- intent and prompt field accuracy
- diagnosis verdict, cause and repair correctness per class
- clarification and enhancement behavior, including named refusals
- hallucinated PASS outputs where the evidence names a failure
- the same held-out set on the unadapted base model as a baseline
- absolute and per-task improvement over that baseline

Each evaluation gets its own `eval-id`; previous evaluation directories are
never overwritten. Gold is empty (0 owner records) and Packet Tracer is not
reachable from Modal, so the release gate reports `BLOCKED` with
`gold_score` and `live_pass` as `NOT VERIFIED`. That is the honest result, not
a training failure. The live gate still runs on the Windows machine with
Packet Tracer open.

## One-command sequence

```bash
python -m modal run havyn-stage5/modal_app.py --stage full
```

Runs smoke, then training, then evaluation, stopping at the first non-passing
stage. Use it only after the smoke stage has already passed once.

## Cost control

Every function exits when it finishes; no idle GPU container is left running.
Smoke includes one model download into the Volume cache. Training and
evaluation reuse that cache. Planning figures at USD/hour, from
`configs/modal.yaml`: L4 0.80, A10G 1.10, L40S 1.95, A100-40GB 2.10. Each run
records `duration_seconds` and `estimated_gpu_usd`; confirm actual spend in the
Modal dashboard.

## Failure behavior

| Symptom | Meaning | Action |
| --- | --- | --- |
| `Token missing` | no Modal credentials | run `python -m modal token new` |
| `dataset verification failed` | local bytes differ from `FROZEN.json` | stop; do not regenerate silently |
| `Volume already holds a different frozen dataset manifest` | remote copy differs | inspect before replacing; the job refuses to overwrite |
| `no smoke report found` | training started before smoke | run `--stage smoke` |
| `latest smoke report is FAIL` | a smoke test failed | read the named test in `smoke-report.json` |
| `resume requested but no valid checkpoint` | wrong run ID or empty checkpoint dir | use the original run ID |
| `run ... is already completed` | resume of a finished run | start a new run ID |
| `BF16 is required` or VRAM gate | GPU cannot run this recipe | use `L4`/`A10G` or larger |
| `4-bit was requested but no quantized modules loaded` | bitsandbytes/CUDA mismatch | fix the image, do not train |
| `no checkpoint met the selection criterion` | no eval loss or adapter file | inspect checkpoints; do not pick the final one by default |
| `run ... is not promotion-eligible` | validation loss regressed, did not improve, or had fewer than two eval points | inspect `training-analysis.json`; do not ship an overfit or unmeasured adapter |

## Local/container boundary

The local Windows process never calls `Volume.commit()`; that API is valid only
inside a container that mounts the Volume. Dataset synchronization uses
`Volume.batch_upload`, whose `VolumePutFiles` call persists the files directly.
Upload and read paths are volume-relative (`havyn-stage5/...`), not container
paths (`/data/havyn-stage5/...`), so the files appear under the mounted `/data`
inside the container. The app source is mounted at `/root/havyn` with
`copy=False` and `include_source=False`, which keeps the repository's `data/`
directory out of `/data`. `XDG_CACHE_HOME` is set inside the container after
mount, never in the image environment, because an image-time value would create
`/data/cache` before the Volume could mount.

## Offline verification already performed

- Frozen dataset hashes pass; rows are 1,989 / 223 / 120; split validation
  passes with zero family overlaps and zero cross-split prompt duplicates.
- Formatting produces 4,492 balanced training examples from 1,981 unique raw
  rows (8 exact duplicates removed), 223 validation examples.
- The pinned container stack (torch 2.9.1, transformers 4.57.6, trl 1.17.0,
  peft 0.21.2, accelerate 1.15.0, datasets 4.8.5) completed a real 2-step LoRA
  training run on a tiny Qwen3 architecture on CPU, producing checkpoint-1,
  checkpoint-2, validation losses and an adapter file.
- New tests: 33 Modal-support tests pass. Final full offline suite: 1,240
  passed, 32 skipped. The known bridge timing flake
  (`test_unclaimed_results_are_purged`) failed intermittently in earlier runs
  and passes in isolation; the final run is clean.
- `modal_preflight.py`: **PASS**, all checks including Modal authentication.
- No Qwen weights or Hugging Face model cache were downloaded to this machine.

## Modal smoke result (2026-10-30)

`python -m modal run havyn-stage5/modal_app.py --stage smoke` returned
`status: PASS` on the final production code, run
`20261030T142027Z-e8190733-smoke`, 68.7 s, estimated USD 0.021.

| Test | Actual result |
| --- | --- |
| A GPU | NVIDIA A10, 22.06 GB total / 21.83 GB free, compute 8.6, BF16 matmul OK, CUDA 12.8, torch 2.9.1+cu128 |
| B environment | Python 3.11.12; torch 2.9.1, transformers 4.57.6, trl 1.17.0, peft 0.21.2, accelerate 1.15.0, datasets 4.8.5, bitsandbytes 0.50.2; all caches under `/data` |
| C dataset | 1,989 / 223 / 120 rows and all five SHA-256 hashes match `FROZEN.json`; 32-example token sample mean 1,324 tokens, max 7,525; 3 exceed the 2,048 training length and are truncated by the unchanged recipe |
| D model | `Qwen/Qwen3-8B`, revision `b968826d9c46dd6066d109eabc6255188de91218`, model_type `qwen3`, 8,190,735,360 logical parameters (4,717,851,648 packed 4-bit), 252 quantized tensors, NF4 double-quant, BF16, cuda:0, peak 7.05 GB, tokenizer `Qwen/Qwen3-8B` |
| E LoRA | 87,293,952 trainable of 8,278,029,312 (1.0545%), base frozen, 504 LoRA tensors, r=32/alpha=32/dropout=0, 36 matches per target module |
| F tiny training | 2 optimizer steps, train loss 2.4997, eval loss 2.4788 -> 2.2509, checkpoint-1 and checkpoint-2 written to the Volume, best checkpoint-2 selected, 35.2 s |

The full training stage was **not** started. The smoke gate is now satisfied,
but `--stage train` remains a separate, explicitly requested action.
