# Stage-5 reproducibility and the `src/network_autopilot/ai/` dependency

Status: **RESOLVED 2026-10-29.** The Stage 3/4 engine work is committed
separately as `d47588c`. A clean checkout of that engine verifies **56/56**
candidates. The historical finding below is preserved for context.

## Finding

The Stage-5 checkpoint `e8503cdfb32a83817a2fbc84ad4245057acf75a2` is **not
self-contained**:

1. `src/network_autopilot/ai/` is **untracked** at that commit. Every Stage-5
   script, test and the candidate verifier imports it, so a clean clone of
   `e8503cd` cannot run any Stage-5 verification.
2. After restoring `ai/`, a clean `e8503cd` tree verifies **48/56** candidates.
   The other 8 need the **pre-existing, uncommitted Stage 3/4 Server-PT service
   support** in `src/network_autopilot/compiler/compiler.py` (working tree lines
   133-160; HEAD refuses at lines 138-148).

## Exact dependency map

### The package

| File | Role | Stage |
| --- | --- | --- |
| `src/network_autopilot/ai/__init__.py` | "Havyn Stage 5 AI layer" docstring | S5 |
| `src/network_autopilot/ai/intent.py` | Intent Schema v1 ("S5.1") | S5 |
| `src/network_autopilot/ai/lint.py` | structured intent lint | S5 |
| `src/network_autopilot/ai/expander.py` | deterministic intent → `NetworkSpec` ("S5.2") | S5 |

Evidence that the package is Stage-5-only, not Stage 3/4:

- Module docstrings identify S5.1/S5.2 and call it the Stage 5 AI layer.
- Its only consumers are Stage-5 files (table below). No Stage-3/4 module
  imports it. `grep -rn "network_autopilot.ai" src/ | grep -v "/ai/"` returns
  nothing.
- All engine classes it imports (`NetworkSpec`, `PolicySpec`, `AclRuleSpec`,
  `HostDnsSpec`, `StaticRouteSpec`, `WirelessTestSpec`, `RequirementSpec`,
  `DesignDecisionSpec`, …) already exist at `e8503cd`. The package is additive.

### Consumers (all Stage 5)

| Consumer | Import site |
| --- | --- |
| `havyn-stage5/prompt-authoring/owner-candidates/verify_candidates.py` | lines 40-45 |
| `havyn-stage5/prompt-authoring/owner-candidates/candidate_data.py` | source-line citations only (no import) |
| `havyn-stage5/scripts/benchmark_harness.py` | 35-36 |
| `havyn-stage5/scripts/build_bench_prompts.py` | 25 |
| `havyn-stage5/scripts/certify.py` | 38-40 |
| `havyn-stage5/scripts/evaluate_model.py` | 43-44 |
| `havyn-stage5/scripts/generate_clarification.py` | 33-34 |
| `havyn-stage5/scripts/generate_enhancement.py` | 31-33 |
| `havyn-stage5/scripts/generate_intents.py` | 31-33 |
| `havyn-stage5/scripts/generate_prompts.py` | 39 |
| `havyn-stage5/scripts/probe_capability_gaps.py` | 31-36 |
| `havyn-stage5/scripts/roundtrip_verify.py` | 36, 154 |
| `havyn-stage5/scripts/validate_gold.py` | 29-31 |
| `scripts/s5_generate_examples.py` | 13 |
| `tests/autopilot/test_s5_bakeoff_scoring.py` | 24 |
| `tests/autopilot/test_s5_intent_schema.py` | 15-21 |

## Reproducibility test (isolated, working tree untouched)

```bash
TMP=${TMPDIR:-/tmp}/havyn-head-repro
rm -rf "$TMP"; mkdir -p "$TMP"
git archive HEAD | tar -x -C "$TMP"
cp -r src/network_autopilot/ai "$TMP/src/network_autopilot/ai"
cp -r havyn-stage5/prompt-authoring/. "$TMP/havyn-stage5/prompt-authoring/"
cd "$TMP"
python havyn-stage5/prompt-authoring/owner-candidates/verify_candidates.py
```

Observed:

```json
{
  "candidates": 56,
  "engine_verified": 48,
  "failed": 8,
  "refusals_expected": 22
}
```

Failing candidates and the exact shared cause:
`oc-012, oc-013, oc-023, oc-025, oc-041, oc-053, oc-055, oc-056` —
`CompileError: spec requires features outside the compiler scope: Server-PT
services (...)`.

Without copying `ai/`, the same clean tree fails at import:
`ModuleNotFoundError: No module named 'network_autopilot.ai'`.

## Decision

- **Commit `src/network_autopilot/ai/*.py`** in the Stage-5 fix commit. It is
  Stage-5-only, additive, and required by already-committed Stage-5 scripts.
  This is not Stage 3/4 work.
- **Do not commit** `src/network_autopilot/compiler/compiler.py`,
  `spec_lint.py`, `core/network_spec.py`, `stage3_commands.py` or any other
  pre-existing Stage 3/4 change. Those belong to the owner of that work.
- **Do not vendor a private copy** of the server-service compiler logic inside
  Stage 5. That would fork the compiler, create two sources of truth, and make
  Stage-5 output diverge from the engine's real capability. The correct fix is
  to commit the Stage 3/4 work separately.

## Resolution (2026-10-29)

The pre-existing Stage 3/4 engine work is committed separately as `d47588c`
(device/feature coverage with sanitized evidence). It includes the Server-PT
service support, the executor/port-solver changes and the Stage-5 criterion fix
in `ai/expander.py`.

- `src/network_autopilot/ai/` is tracked at `d47588c` (4 files), so a clean
  clone runs every Stage-5 script.
- The candidate pool re-verifies **56/56** at `d47588c` with no `+dirty-src`
  marker (`verification-report.json`).
- The Stage-5 commits are on top of that engine checkpoint; no Stage 3/4 file
  was included in a Stage-5 commit.
- Certificates are pinned to `src-tree:5e437ba66b14`, the committed engine tree.

The historical 48/56 finding above applied to `e8503cd` before the Stage 3/4
checkpoint; it is no longer the current state.

## What remains

Only the owner gates remain: confirm the 8 technical decisions, rewrite/adopt
Gold drafts, and run the GPU/Colab and live-Packet-Tracer stages.
