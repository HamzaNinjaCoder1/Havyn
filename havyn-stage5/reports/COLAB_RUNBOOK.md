# Havyn Colab runbook

This is the exact operator sequence. Steps marked **USER ACTION REQUIRED** are
the only ones the tooling cannot do for you. Nothing is published and no GPU
is used until the relevant step says so.

Recommended first pass: set `DRY_RUN=true` and run 01 → 03 → 09 → 10 → 11. Every
expensive notebook then validates its stage without a GPU or model download.

## A. Open Colab — **USER ACTION REQUIRED**

Open <https://colab.research.google.com> and create a new notebook.

## B. Select a GPU runtime — **USER ACTION REQUIRED**

Runtime → Change runtime type → Hardware accelerator → GPU.

- Standard training / bake-off: ≥12 GB (L4 or better).
- Lite training: ≥6 GB.
- Teacher or independent review: ≥24 GB (A100 recommended).

If the runtime has no GPU, `colab_preflight.py --require-gpu` stops before any
work.

## C. Add secrets — **USER ACTION REQUIRED**

Left sidebar key icon → Secrets:

| Secret | Required | Purpose |
| --- | --- | --- |
| `HF_TOKEN` | yes for gated models/uploads | Hugging Face read token |
| `HF_USERNAME` | optional | repository owner name |
| `GITHUB_TOKEN` | optional | only for a private fork |

Enable notebook access for each. Never paste a token into a cell. If a token is
missing, `hf_auth.py` prints the three-line instruction and never the token.
Accept the Llama and Gemma licences on the Hugging Face account if those
candidates are used.

## D. Clone and install

```python
!git clone https://github.com/HamzaNinjaCoder1/MCP-Packet-Tracer.git /content/MCP-Packet-Tracer
%cd /content/MCP-Packet-Tracer
!pip install -q -e .[test]
!pip install -q -r havyn-stage5/requirements-colab.txt
```

Expected: editable install, no dependency conflicts. Failure: the preflight
prints the missing package; re-run the requirements install.

## E. Run setup (notebook 01)

Open `notebooks/01-environment-setup.ipynb` and run all cells.

Expected: offline suite passes, `env-report.json` written to Drive/runtime,
the dashboard prints GPU, VRAM, Python, commit, dataset version, model
configuration, HF status (token hidden), next step and storage estimate.

Failure: stop. The notebook asserts on setup errors by design.

## F. Run engine validation (notebook 02)

Expected: schema/lint/compile tests pass. No GPU work.

## G. Model connectivity test

```bash
python havyn-stage5/scripts/hf_smoke_test.py
```

Expected: `PASS`; tiny metadata resolved and tiny tokenizer loaded. This
downloads ~1 MB, no model weights. If a token is present, it also checks
`whoami`; a rejected token fails loudly. Gated-model access is reported when
`--registry-model` is passed.

## H. Run the bake-off (notebook 03)

**This cell uses a GPU and downloads several GB per candidate.**

```bash
python havyn-stage5/scripts/build_bench_prompts.py \
  --valid havyn-stage5/datasets/havyn-intents-v1/valid.jsonl \
  --target 120 --out havyn-stage5/datasets/bench-prompts-v1.jsonl
```

Then run the notebook cell. Expected outputs in Drive:
`<run>/<model>-generations.jsonl`, `<model>-summary.json`, `comparison.md`.
No winner is selected by the tooling; record the measured table before
changing `configs/bakeoff.yaml`.

## I. Verify/freeze the dataset

```bash
python havyn-stage5/scripts/freeze_dataset.py havyn-stage5/datasets/havyn-train-v1 --check
python havyn-stage5/scripts/validate_training_split.py
python havyn-stage5/scripts/prepare_hf_dataset.py --dry-run
```

Expected: `frozen: true`, `passed: true`, and the export plan with
train/validation/heldout counts. If regeneration (notebooks 04–08) ran, the
freeze is invalidated; notebook 08 ends with assemble → validate → freeze.

## J. Training preflight

```bash
python havyn-stage5/scripts/colab_preflight.py --require-gpu --require-hf
```

Expected: `PREFLIGHT PASS`. It checks GPU, CUDA, VRAM, Python, packages, token,
git, freeze, registry, disk and publish safety. Failure means do not train.

## K. Standard training (notebook 09)

**This cell starts hours of GPU training.**

```bash
python havyn-stage5/scripts/train_lora.py \
  --config havyn-stage5/configs/train-standard.yaml --resume \
  --dataset-dir "$HAVYN_DRIVE/datasets/havyn-train-v1" \
  --output-dir "$HAVYN_DRIVE/checkpoints/standard"
```

Expected: `training-report.json` with base model, config SHA, code commit,
dataset freeze hash, seed, VRAM adjustment, latest checkpoint. Re-running with
`--resume` continues from the newest checkpoint. The script refuses an
unfrozen/changed dataset and refuses to load weights that do not fit.

## L. Lite training

Same command with `train-lite.yaml` and `checkpoints/lite`. Run after Standard
or in a separate runtime.

## M. Evaluation (notebook 10)

```bash
python havyn-stage5/scripts/evaluate_model.py \
  --model "$HAVYN_DRIVE/checkpoints/standard" \
  --gold havyn-stage5/datasets/gold-v1 \
  --out "$HAVYN_DRIVE/evaluation"
```

Gates: JSON 100%, lint ≥95%, Gold ≥90%, diagnosis ≥85% per class, live ≥90%.
FAIL and NOT VERIFIED both block release. With 0 owner Gold records, the Gold
gate is NOT VERIFIED, not passed.

## N. Live Packet Tracer gate — **USER ACTION REQUIRED on Windows**

Colab cannot reach a local Packet Tracer. On the machine with PT open:

```bash
python havyn-stage5/scripts/evaluate_model.py --model <merged-or-adapter> \
  --gold havyn-stage5/datasets/gold-v1 --live --out havyn-stage5/evaluation
```

The run reports exact X/Y and how many prompts succeeded; no PASS is recorded
without real build + traffic verification.

## O1. Merge the adapter (notebook 10)

Training saves an adapter, not a standalone model. Merge before evaluation and
export:

```bash
python havyn-stage5/scripts/merge_lora.py   --base-model Qwen/Qwen3-8B   --adapter "$HAVYN_DRIVE/checkpoints/standard/adapter"   --out "$HAVYN_DRIVE/merged/standard"
```

Expected: merged weights plus `merge-manifest.json` with SHA-256 per file.
Use the merged path in steps M and O.

## O. GGUF export (notebook 10)

```bash
python havyn-stage5/scripts/export_gguf.py \
  --model "$HAVYN_DRIVE/merged/standard" --quant q4_k_m \
  --out "$HAVYN_DRIVE/artifacts" \
  --gate-report "$HAVYN_DRIVE/evaluation/gate-report.json"
```

Expected: GGUF file, `manifest.json` with SHA-256, model card. If the gate is
not GO, the card says NOT VERIFIED.

## P. Verify hashes

```bash
python - <<'PY'
import hashlib, json, pathlib
art = pathlib.Path("<artifacts>")
manifest = json.loads((art / "manifest.json").read_text())
model = art / manifest["artifact"]
actual = hashlib.sha256(model.read_bytes()).hexdigest()
print("manifest:", manifest["sha256"])
print("actual  :", actual)
assert actual == manifest["sha256"], "hash mismatch"
print("OK")
PY
```

## Q. Optional Hugging Face upload — **USER ACTION REQUIRED**

Default is **no upload**. To publish (after explicit authorisation):

```bash
export HF_PUBLISH=true
python havyn-stage5/scripts/hf_publish.py --kind dataset \
  --repo-id <user>/<name> --path havyn-stage5/datasets/hf-export/<version> --publish
python havyn-stage5/scripts/hf_publish.py --kind model \
  --repo-id <user>/<name> --path <artifacts> \
  --gate-report <evaluation>/gate-report.json --publish
```

Both `HF_PUBLISH=true` and `--publish` are required. Repositories are created
private. A model upload without a GO gate is refused unless
`--allow-unverified` is passed, which writes `NOT_VERIFIED.md`.

## Failure behavior

| Symptom | Meaning | Action |
| --- | --- | --- |
| `HF_TOKEN is not set` | secret missing | add it in Colab Secrets |
| `token rejected` | expired/insufficient token | rotate the token |
| `PREFLIGHT FAIL` | missing package/GPU/VRAM/freeze | fix the named check; do not train |
| `refusing to train on an unfrozen ... dataset` | regenerate/freeze first | run 08's final cell |
| `not enough free VRAM ...` | weights do not fit | larger GPU or Lite role |
| `release: BLOCKED` | an evaluation gate failed | improve data/model; never edit the gate |
| live gate `ran: false` | Packet Tracer not reachable | open PT and re-run on Windows |

No result in this pipeline is converted from NOT VERIFIED to PASS.
