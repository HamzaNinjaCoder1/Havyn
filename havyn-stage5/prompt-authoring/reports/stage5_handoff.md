# Stage-5 execution handoff

Snapshot taken 2026-10-29. Checkpoint: `e8503cdfb32a83817a2fbc84ad4245057acf75a2`.
This handoff describes what is done, what is locally runnable, and what is
blocked. It does **not** claim the Stage-5 pipeline has been executed end to
end.

## Snapshot

| Item | State |
| --- | --- |
| Prompt-authoring pool | 56 candidates: 34 buildable + 22 refusal |
| Candidate engine check | **56/56** in the working tree, 0 failures (clean `e8503cd` = 48/56; see below) |
| Post-fix semantic status | **ACCEPT 48 / REVIEW 8 / REJECT 0** (same-base re-check, not cross-family) |
| Duplicate control | 0 exact / 0 normalized / 0 intent-level / 0 MinHash ≥0.90 |
| VLAN-pin audit | 21 multi-VLAN sites, 50 host groups, 10 servers, **0 unpinned** |
| Coverage | exact 1 / label-only 14 / feature-match 4 / extension-cell 15 / refusal 22 |
| Owner Gold | **0 / 100** records exist (only a template) |
| External Gold | **0 / 200** records exist |
| Cross-family review | **not done** |
| Synthetic training data | 2,263 rows: train 1,776 / validation 250 / heldout 237 |
| Dataset freeze | **not frozen** (`FROZEN.json` absent) |
| Certificates | 108/108 cells, 61/61 fault injectors, 5/5 mutations at `06c57ce`; `--check` says **STALE** vs `e8503cd` |
| Gold template validation | 1/1 engine-verified; leak check clean against the training set |
| Offline test suite | Run 1: **1,176 passed / 32 skipped / 0 failed** (202.66 s). Post-commit rerun: **1,175 passed / 1 failed / 32 skipped** (293.91 s). The single failure is a pre-existing timing-sensitive bridge test, not Stage 5; see "Known test flake". |
| Live Packet Tracer | **not run** |

---

## A. Completed

1. **Engine and Stage-5 pipeline code exist.** The closed-loop engine, intent
   schema/lint/expander, Stage-5 scripts, configs and 11 notebooks are in the
   tree. The full offline suite passed 1,176 with 32 skipped on the first run; a
   post-commit rerun hit one pre-existing timing-sensitive bridge test (see
   "Known test flake" below).
2. **Capability-gap evidence is engine-executed offline.** 14/14 probes matched
   (`capability-gap-probes.json`): guest isolation, guest-VLAN lint,
   DMZ/multi-site, ASA 5505 DMZ, ASA single-VLAN, ASA multi-site, VLAN outside
   block, server DHCP, wireless cell size, camera kind, bonded access, no
   inter-VLAN ACL, no IoT IP placement, one internet block.
3. **Refusal evidence is classified.** `refusal_source_verification.md`
   distinguishes capability probes (executed), source traces, candidate-level
   checks (null-intent only) and live PT (not run); two wrong cross-references
   (oc-048 under ASA multi-site, oc-054 under DMZ multi-site) are corrected.
4. **Prompt-authoring pool is verified.** 56 candidates pass the deterministic
   chain in the working tree; 0 duplicates; 0 unpinned groups; language
   30 en / 18 roman-urdu / 8 mixed; difficulty 4/8/19/16/9.
5. **The 16 checkpoint REVIEWs were re-checked.** 8 moved to ACCEPT after their
   flagged defects were fixed (`oc-004, 005, 006, 030, 044, 048, 049, 056`);
   8 remain REVIEW for owner decisions (`oc-007, 012, 013, 028, 035, 040, 053,
   054`). Objective fixes: oc-004's "NAT at each" over-claim, oc-056's open
   "may ask" menu, and the invented `camerapanel` PC in oc-027/oc-054.
6. **The owner-Gold plan is actionable.** 100 slots, candidate-backed vs
   coverage-driven, language/difficulty/cell/rewrite/status/action per row, the
   8 exact owner questions, and the provenance block.
7. **Cross-family input package is generated.** 56 records, SHA-256
   `c0baaa4482c0792f4cf03475a2dd2f1b3fc1157fc1f96799d5c439013662d0dd`, with
   Model A's rationale fields excluded. Protocol: `cross-family-review-protocol.md`.
8. **Reproducibility dependency is documented.** `reproducibility_dependency.md`
   names the exact `ai/` consumers, why `ai/` is Stage-5-only, and the 8-candidate
   Stage-3 dependency that blocks a clean-clone 56/56.
9. **Synthetic data exists.** intents 1,944 valid / 0 failed; prompts 147;
   roundtrip 147 (ACCEPT 77 / REVIEW 70 / REJECT 0, dry-run); diagnosis 72;
   enhancement 60; clarification 40; train split 2,263 rows with 0 leaks.

## B. Locally executable, but resource- or data-dependent

These run on CPU here, but are gated by data/engine state; they are not blocked
by hardware.

| Task | Command | Proves | Does not prove |
| --- | --- | --- | --- |
| Re-certify after the engine commit | `python havyn-stage5/scripts/certify.py` | cells / fault injectors / mutations compile at that commit | live PT behaviour |
| Check certificates | `python havyn-stage5/scripts/certify.py --check` | certificates match the engine commit | correctness of a stale certificate |
| Re-assemble splits | `python havyn-stage5/scripts/assemble_dataset.py` | family-aware split, leakage audit | model quality |
| Freeze training data | `python havyn-stage5/scripts/freeze_dataset.py` | hashes pin the splits | that the data is good |
| Check freeze | `python havyn-stage5/scripts/freeze_dataset.py --check` | no byte changed since freeze | gold-set quality |
| Validate Gold | `python havyn-stage5/scripts/validate_gold.py --leak-check havyn-stage5/datasets/havyn-train-v1` | every Gold record builds; no Gold prompt leaks into training | live PT behaviour |
| Score a bake-off run | `benchmark_harness.py` scoring half (see notebook 03) | valid JSON / schema / lint / field scores on pre-generated outputs | generation quality without a GPU |
| Score evaluation outputs | `python havyn-stage5/scripts/evaluate_model.py --generations-file <file> --gold ...` | gate metrics on pre-generated outputs | generation without a GPU |
| Hash/manifest half of GGUF | `python havyn-stage5/scripts/export_gguf.py --help` / offline helpers | SHA-256, manifest, model card | the quantised file itself |
| Environment report | `python havyn-stage5/scripts/env_report.py` | what the runtime actually has | GPU training success |

Known local data issues to fix before freeze:

- **The `prompt` task has 0 training rows.** The audit splits 147 prompt
  examples into 2 families: validation 59, heldout 88, train 0. Training with
  `prompt` in `task_tags` would contribute nothing. Fix by generating more
  prompt families (at least one family must stay in train) or by removing
  `prompt` from the training tags until that exists.
- **Certificates are stale by commit.** `certify.py --check` reports
  `06c57ce → e8503cd` for all three families. Re-run after the engine commit is
  final; do not reuse the old certificates.
- **The prompt/roundtrip manifests do not record an engine commit.** The intent,
  diagnosis, enhancement and clarification manifests do. Add the engine commit
  when regenerating.

## C. Requires Hamza / human contributors

| Task | Why it cannot be automated | Exact next action |
| --- | --- | --- |
| 8 REVIEW decisions | They are policy/design choices, not text defects | Answer the 8 questions in `owner_gold_authoring_plan.md`; then adopt or drop |
| 48 ACCEPT rewrites | Owner authorship is the point of `gold-001..100` | Rewrite each prompt in your own words; confirm intent; adopt with the provenance block |
| 44 open Gold slots | No candidate exists | Author prompts for `gold-057..100` from scratch; validate with `validate_gold.py` |
| 200 external examples | Must be real people, not AI | Recruit ≥8 contributors; use `external-collection/contributor-guide.md`; consent + ledger; second-person fidelity read; engine check |
| Cross-family review | Needs a different model family/provider | Run `cross-family-review-protocol.md` on the 56-record package; commit the raw review |
| Commit the Stage 3/4 engine work | This work is forbidden to touch it | Decide where that commit lands; then re-certify and re-run the clean-clone check |
| Approve provenance extension | Dataset policy | Approve `provenance` fields / `ai_assisted` rule in `datasets/gold-v1/README.md` |
| Fix the 0-train prompt task | Dataset design decision | Add prompt families or drop `prompt` from training tags before freeze |

## D. Requires GPU / live Packet Tracer

| Task | Where | Gate |
| --- | --- | --- |
| Base-model bake-off generation | Colab GPU, `configs/bakeoff.yaml` (5 candidates, 12 GB needs 4-bit) | comparison table; no winner selected yet |
| LoRA training (standard Qwen3-8B) | Colab GPU, `configs/train-standard.yaml` | loss/checkpoint report; resumable from Drive |
| LoRA training (lite Qwen3-4B) | Colab GPU, `configs/train-lite.yaml` | same |
| Evaluation generation | Colab GPU, `evaluate_model.py` | schema 100%, lint ≥95%, gold ≥90%, diagnosis ≥85% |
| Live Packet Tracer gate | Live PT + 50 Gold prompts | `live_pass ≥ 90%`, exact X/Y reported |
| LoRA merge | GPU | merged checkpoint |
| GGUF Q4_K_M export | GPU / llama.cpp | file + SHA-256 + model card |
| Improvement loop retrain half | GPU | one full cycle, honestly recorded |

The thresholds above are from `evaluate_model.py` (S5.19) and are copied, not
invented. A NOT VERIFIED gate blocks release exactly like a FAIL.

## Known test flake (not Stage 5)

`tests/test_bridge_results.py::test_unclaimed_results_are_purged` sets
`bridge._result_ttl = 0.2`, posts five results, and asserts all five are still
present. Under full-suite load the five HTTP posts take longer than 0.2 s, so
the first entries are purged before the assertion (observed `2 == 5`). The test
passes in isolation and passed on the first full run. No Stage-5 code is
involved: neither the bridge nor the test is changed by the Stage-5 commit.
Fix it separately (for example, fill with a long TTL, then lower the TTL for
the purge assertion) rather than inside a Stage-5 commit.

## E. Final release blockers

1. **0/100 owner Gold** records; 8 REVIEW decisions open.
2. **0/200 external Gold**; no contributor has been recruited.
3. **No cross-family review**; same-base Model B does not satisfy this gate.
4. **Clean-clone verification is 48/56**, not 56/56, until the Stage 3/4
   server-service support is committed.
5. **Certificates are stale** vs `e8503cd`; do not train on them.
6. **Dataset is not frozen**, and the `prompt` task has 0 train rows.
7. **No bake-off result**, no trained checkpoint, no evaluation gate report.
8. **No live Packet Tracer gate** (50 Gold prompts, ≥90%).
9. **No merged model, no GGUF**, no model card/manifest, no release GO.
10. **No leak check against real Gold** is possible until Gold exists.
11. **One known offline test flake** (`test_unclaimed_results_are_purged`);
    fix it separately before claiming a fully deterministic CI.

---

## Run order

1. Hamza: answer the 8 REVIEW questions; rewrite/adopt the 48 ACCEPT drafts;
   author the 44 open slots.
2. Run the cross-family review on the frozen 56-record package; resolve REJECTs.
3. Owner: commit the Stage 3/4 engine work; re-run `certify.py`; regenerate
   datasets if the engine changed; re-assemble; fix the 0-train prompt task;
   freeze the training data.
4. Collect the 200 external examples with consent + ledger + second read.
5. Colab: notebook 01 (setup) → 02 (engine validation) → 03 (bake-off) →
   09 (training) → 10 (evaluation + live gate + GGUF) → 11 (improvement loop).
6. Release only when every gate in `evaluate_model.py` is PASS (not NOT
   VERIFIED) and the Gold/external sets are frozen.

## Honest boundaries

- The 56/56 candidate verification was run in the working tree, which contains
  pre-existing uncommitted Stage 3/4 changes. It is not reproducible from the
  Stage-5 commit alone.
- All capability probes are offline engine probes. None is a live Packet
  Tracer run.
- No GPU training, bake-off, evaluation, GGUF export or live PT gate has been
  executed in this work.
- No model result, human authorship, or cross-family verdict is claimed.
