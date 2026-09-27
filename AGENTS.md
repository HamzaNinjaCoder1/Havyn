# AGENTS.md

Notes for coding agents working in this repo. Read this before changing anything
under `src/`.

## What this is

Havyn: closed-loop network engineering for Cisco Packet Tracer. It takes a typed
`NetworkSpec`, compiles it into a deterministic operation DAG, executes that DAG
against a live Packet Tracer, and verifies the result with fresh read-back and
real traffic. It ships its own MCP server, its own Packet Tracer transport and
its own device catalog — the engine is self-contained.

## Build and test

```bash
pip install -e ".[test]"
python -m pytest          # from the repo root, no PT required
```

Live tests under `tests/autopilot/test_live_*.py` skip themselves when Packet
Tracer is not reachable. There is no linter or formatter configured. Match the
surrounding style: type hints on public functions, `from __future__ import
annotations` at the top, comments in Spanish or English following whatever the
file already uses.

## Layout

| Path | What lives there |
| --- | --- |
| `src/network_autopilot/core/` | `NetworkSpec`, `Observable`, receipts, safety, actual-state models |
| `src/network_autopilot/compiler/` | Compiler, operation DAG, executor, port solver, preflight, layout, policy |
| `src/network_autopilot/ipam/` | Deterministic addressing plan and validator |
| `src/network_autopilot/adapter/` | Adapter contract, null adapter, `packet_tracer/` live adapter + JS builders |
| `src/network_autopilot/pt/` | Transport (`http_bridge`, `mailbox`, `token`) and the device/module/cable catalog |
| `src/network_autopilot/verification/`, `acceptance/`, `diagnosis/`, `diff/` | Verdicts, evidence-based diff and diagnosis |
| `src/network_autopilot/repair/` | Bounded diagnose → plan → gate → execute → re-verify loop |
| `src/network_autopilot/mcp/server.py` | The MCP tool surface (`python -m network_autopilot`) |
| `EXTENSION/script-engine/` | Script-engine side of the extension. `main.js` is ours (tracked); the rest are PTBuilder reference copies (gitignored) |
| `EXTENSION/webview/` | The MCP Control Center webview (`index.html` + `interface.js`) |
| `build_havyn.py` | One-shot build entry point: `python build_havyn.py <spec> <safety_mode>` |

`src/network_autopilot/mcp/server.py` keeps its handlers thin — every tool
delegates to the engine. If you write a helper worth testing, put it in the
module the engine already owns (for example `core/ids.py` or `shared` helpers
inside the layer that uses them), never in a closure inside `register_tools`-style
registration.

### Network Autopilot (Phase 1-3)

The `src/network_autopilot/` package implements the closed-loop engine:

- **Compiler** (`compiler/`): `compile_spec()` produces an `OperationDag`.
  `net_build()` adds preflight as a HARD gate. `solve_ports()` resolves
  ports/media/modules. `PortReservationSystem` prevents double-use of ports.
- **Executor** (`compiler/executor.py`): `DagExecutor` runs the DAG through
  the `SafetyGate`, checks port reservations before link operations, and
  produces receipts with read-back postconditions.
- **Preflight** (`compiler/preflight.py`): `PreflightValidator` checks port
  compatibility, module availability, and cable rules before execution.
  If preflight fails, `net_build()` blocks execution immediately.
- **Per-criterion verification** (`verification/per_criterion.py`): Every
  acceptance criterion gets its own `CriterionVerification` with its own
  evidence chain. UNKNOWN is never converted to PASS.
- **Port/Media/Module solver** (`compiler/port_solver.py`): Resolves every
  link endpoint to a concrete port + media + module using the upstream
  catalog. Checks compatibility, module requirements, and cable rules.
- **Port reservation** (`compiler/port_reservation.py`): Prevents two
  operations from consuming the same physical/interface resource.

## Phase 3 Implementation

### Port/Media/Module solver (§9)
- `solve_ports(spec, plan)` returns a `PortPlan` with `PortResolution` for each endpoint
- Checks port existence, media compatibility, module requirements
- `infer_cable()` uses `CABLE_RULES` from the upstream catalog
- `ModuleRequirement` tracks modules that must be installed before links

### Preflight hard gate (§G)
- `PreflightValidator.run_preflight()` checks all pre-execution conditions
- `net_build()` calls `compile_spec()` then checks `preflight.can_execute`
- If preflight fails: returns `(None, error_string)` — never executes
- `PreflightCheck` has categories: port, module, reservation, link, device

### Per-criterion verification (§H)
- `PerCriterionVerifier.verify()` generates `CriterionVerification` per criterion
- Each has its own `verification_id` (CV- prefix), evidence, and verdict
- Flow criteria: REACHABLE→PASS, PARTIAL→FAIL, BLOCKED→FAIL, UNKNOWN→UNKNOWN
- Structural/config/VLAN criteria: PASS only if observed, UNKNOWN if not observable
- `VerificationReport` includes `per_criterion` field

### Safety/idempotency/repair (§I)
- `SafetyGate` enforces READ_ONLY/DRY_RUN/APPROVAL_REQUIRED/AUTONOMOUS modes
- `MutationBudget` caps mutations (25 default) and repair attempts (3 default)
- `RepairEngine` does bounded repair: diagnose → plan → gate → execute → re-verify
- `DagExecutor` reuses existing devices (idempotent), blocks dependents on failure
- Receipts require read-back postconditions for confirmation

## The 10 rules that do not bend

These are not style preferences. Each one exists because breaking it produced a
specific, documented failure. If a change seems to need one of them broken, the
change is wrong — not the rule.

1. **Never build JavaScript with raw f-strings.** Use `json.dumps` for every
   interpolated value. PT runs the result through `new Function()`, so one
   unescaped device name is arbitrary code execution inside the user's PT.
   `js_commands.py` is the reference pattern: `_q()` each field, then build.

2. **Never build a filesystem path by concatenating user input.** Sanitise the
   component first, then join. `EvidenceStore` derives its directory from a
   generated `build_id`; anything that reaches a path from a spec or a device
   name must be normalised (strip separators, reject `..`) before it is joined
   to a root.

3. **Never add an unauthenticated bridge endpoint.** Everything except `/ping`
   requires the per-machine token; see `pt/token.py` for why loopback alone is
   not an access control. The file-mailbox channel needs no token because the
   mailbox lives under a user-ACL'd `%LOCALAPPDATA%` directory a browser page
   cannot write to — that is the whole argument, so do not move it somewhere a
   page can reach.

4. **Never validate inside a model.** A Pydantic model describes shape, not
   policy. Semantic checks belong in `compiler/spec_lint.py` or
   `compiler/preflight.py` and return a structured result, so the caller decides
   whether to proceed. A model that raises on a policy question makes the policy
   unoverridable and untestable.

5. **A bug fix needs a test that fails without it.** Write the test first. If it
   passes on the unfixed code, it is not testing the bug — prove it by mutating
   the fix back out and watching it go red.

6. **Never guess a PT API or CLI signature.** If a method is not already used
   somewhere in this repo, confirm it against Cisco's reference *or* a live
   probe before building on it. PT answers a wrong call with a bare
   `Invalid arguments for IPC call "X"` and no hint of what was expected.

7. **UNKNOWN is never PASS.** Not on a config criterion, not on a flow, not in a
   report. If a property could not be observed, the verdict is `UNKNOWN` with
   the reason. This is the product's core claim: "configured" does not mean
   "verified", and a green build that lied about an unobservable property is
   worse than a build that reports the gap.

8. **A receipt without read-back is not a confirmation.** `OperationReceipt`
   distinguishes executed from confirmed for exactly this reason. PT reports
   `true` for a script it aborted on, so acceptance of a push proves nothing —
   push, then read the state back, and fail the operation if it is not there.

9. **Refuse by name, never half-configure.** If PT cannot do what the spec asks
   (a static route on a Linksys, an address-scoped ASA rule, a feature with no
   API), the compiler raises with the reason *before* any mutation. A build that
   quietly skips half a requirement is the failure mode this project exists to
   prevent; a refusal is a feature, not a gap.

10. **Never claim more verification than you ran.** Live tests skip
    themselves when PT is unreachable, and anything touching PT's webview (CORS,
    the `this-sm:` origin, whether the Script Engine can reach an API) cannot be
    verified from a test at all. Say "this half is unverified and here is why".
    An offline green suite proves internal consistency, not live correctness.

## Working with the bridge

The bridge only really works with Packet Tracer open, so most verification is
offline. `tests/test_bridge_results.py` and `tests/test_bridge_token.py` drive
the real bridge on an ephemeral port — follow that pattern rather than mocking
HTTP.

Set `PT_MCP_BRIDGE_TOKEN` to avoid touching the user's real token file.

Anything involving PT's webview (CORS behaviour, the `this-sm:` origin, whether
the script engine can reach an API) **cannot be verified from tests**. Say so
explicitly instead of assuming; don't claim a change is verified when only the
offline half was.

## Gotchas

- PT's `executeCode()` **strips newlines** from source that is *pasted* into the
  Builder Code Editor, so any such snippet must be a single line without `//`
  comments. (The compiled `.pts` files — `main.js` etc. — are not pasted and can
  be normal multi-line JS.)
- An uncaught error inside `runCode` opens a modal that freezes the webview and
  kills the polling loop. That is why fire-and-forget commands are wrapped in
  `try{...}catch{}`.
- `%LOCALAPPDATA%`, not `%APPDATA%`, for machine-local secrets — the latter
  roams.
