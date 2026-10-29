# Stage-5 final status

Date: 2026-10-29. Stage-5 commits through `dba7a1b`; the separate Stage 3/4
engine checkpoint is `d47588c`. The final dataset is regenerated, validated
and frozen at that engine.

Status vocabulary is the required one only: COMPLETE, READY, PARTIAL, BLOCKED,
NOT RUN, NOT VERIFIED. "COMPLETE" is used only where the artefact exists and
the checks were executed here.

| Area | Status | Evidence | Blocker |
| --- | --- | --- | --- |
| Candidate pool (56) | COMPLETE | `verify_candidates.py --emit`: 56/56 engine-verified at clean `d47588c` (no dirty marker), 0 failed, 22 refusals; 0 duplicates; 0 unpinned groups | — |
| 8 owner decisions | READY | `owner_decisions.json/.md`: recommendation, basis, engine can/cannot verify, provenance, `owner_confirmation_required: true`; 2 REFUSAL, 5 BUILD, 1 BUILD_AFTER_REWRITE | Hamza has not confirmed any decision; none is recorded as approved |
| Owner-Gold drafts (56) | COMPLETE (drafts) | `accepted-candidates/`: 48 ACCEPT + 8 decision drafts; 56/56 re-verified; provenance `ai_assisted_candidate`, `owner_adoption: pending`, `owner_approved: 0` | Owner rewrite/adoption into `gold-v1` (0/100 adopted) |
| Coverage slots (44) | COMPLETE | `coverage-candidates/`: 44/44 verified, 41 build + 3 capability-gap refusals; 35 distinct cells + 9 language variants; prompts match labels (24 en / 11 roman-urdu / 9 mixed) | Owner adoption into `gold-057..100` |
| External synthetic corpus (200) | COMPLETE | `datasets/external-synthetic-v1`: 160 buildable + 20 refusal + 20 ambiguous, 200/200 engine-checked; per-row `origin: synthetic_ai`, `human_contributor: false`, `consent: not_applicable` | Not human Gold and never labelled as such |
| Real external human Gold (200) | BLOCKED | `external-collection/` guide, template, ledger ready | No real contributors recruited; consent/provenance cannot be fabricated |
| Cross-family review | BLOCKED | 56-record package, SHA-256 `f7a0a1f4…c02bc492`; environment audit in `cross-family-review-report.md` (CPU-only, 8 GB RAM, no Mistral weights/API) | No runnable different-family reviewer locally; executor + notebook 12 prepared, offline precheck 56/56 with 0 issues; run on Colab GPU with Mistral-Small-24B |
| Stage 3/4 dependency | COMPLETE | committed separately as `d47588c` after sanitising lab credentials; 86 Stage 3 tests pass; `stage34_checkpoint_blocker.md` records the resolution | — |
| Dataset assembly | COMPLETE | 2,332 rows: train 1,989 / validation 223 / heldout 120 (0.853/0.096/0.051); per-task counts below; 0 errors, 0 family overlaps, 0 cross-split duplicates | — |
| Dataset freeze | COMPLETE | final `havyn-train-v1/FROZEN.json`; `--check` valid; train SHA-256 `74d529e2…`; regenerated at `d47588c` | — |
| Certificates | COMPLETE | `certify.py --check` valid at clean `src-tree:5e437ba66b14`: cells 108/108, fault injectors 58/58, mutations 5/5 | — |
| Colab preparation | READY | 11 notebooks statically checked: every referenced script exists; 05 requires `INDEPENDENT_MODEL_ID` and passes `--model`; 08 assembles/validates/freezes; 09 copies the frozen set to Drive; 10 uses Drive checkpoint paths | Not executed on a GPU runtime here |
| Base-model bake-off | READY / NOT RUN | `configs/bakeoff.yaml` (5 candidates, metrics), `benchmark_harness.run_bakeoff`, 125 benchmark prompts built (`bench-prompts-v1.jsonl`) | No GPU; no measured results exist and none are invented |
| Training (standard/lite) | NOT RUN | `train_lora.py` refuses unfrozen/changed data; configs and resume recipe ready | No GPU; no checkpoint, loss curve or score exists |
| Model evaluation | NOT VERIFIED | `evaluate_model.py` gates: JSON 100%, lint ≥95%, Gold ≥90%, diagnosis ≥85%, live ≥90%; FAIL/NOT VERIFIED blocks release | Requires a trained model and the owner Gold set |
| Live Packet Tracer gate | NOT RUN | Harness exists; no simulated PASS is recorded | Needs live PT and 50 owner-Gold prompts (0 exist) |
| GGUF / release | NOT RUN | `export_gguf.py` (Q4_K_M, SHA-256, model card) prepared | Requires trained merged weights and a PASS gate report |
| Git | COMPLETE | Stage-5 commits contain only `havyn-stage5/**` and `tests/autopilot/test_s5_*.py`; Stage 3/4 is a separate commit `d47588c` | — |
| Offline tests | COMPLETE | `python -m pytest -q`: 1,197 passed, 32 skipped, 0 failed | Known bridge timing flake still possible under load (separate issue) |
| Colab/HF preparation | READY | `reports/colab_hf_readiness.md`, `reports/COLAB_RUNBOOK.md`, model registry, preflight, dashboard, DRY_RUN for every stage, private HF dataset snapshot, publish default off; public Hub smoke passed | No GPU/token here; not executed in Colab; 0 owner Gold |
| Documentation | COMPLETE | Handoff rewritten to the verified state; this report; candidate/review/capability reports; Colab runbook | — |

## Exact dataset row counts

| Task | Examples | Train | Validation | Heldout | Families |
| --- | --- | --- | --- | --- | --- |
| prompt | 216 | 185 | 21 | 10 | 36 |
| intent | 1,944 | 1,692 | 168 | 84 | 36 |
| diagnosis | 72 | 59 | 4 | 9 | 8 |
| enhancement | 60 | 38 | 19 | 3 | 5 |
| clarification | 40 | 15 | 11 | 14 | 4 |
| **total** | **2,332** | **1,989** | **223** | **120** | — |

Prompt gating: 216/216 `template_construction`. The round-trip run is a
dry-run (`reconstruction == source`) and is recorded as such; it is not used
as evidence. `independent_model_verified: false`.

## Frozen SHA-256

Recorded in `datasets/havyn-train-v1/FROZEN.json` (train, validation, heldout,
audit report, dataset card). `freeze_dataset.py --check` passes.

## What remains for the owner (minimum)

1. Confirm or reject the 8 recommendations in `owner_decisions.json`, then
   rewrite and adopt drafts into `datasets/gold-v1/` (0/100 today).
2. Sanitise the Stage 3/4 evidence and land that checkpoint separately; then
   re-run `certify.py`, `verify_candidates.py --emit`, `assemble_dataset.py`,
   `validate_training_split.py`, `freeze_dataset.py`.
3. Run the cross-family review on a GPU/hosted different family with the
   existing package and protocol.
4. Recruit real contributors for `external-human-v1` (the synthetic corpus is
   not a substitute).
5. Run notebooks 01→11 on a GPU runtime; only then record bake-off, training,
   evaluation, live PT and GGUF results.

Nothing in this report is a claim of GPU training, live Packet Tracer, human
authorship, owner approval or cross-family verification. None of those
happened.
