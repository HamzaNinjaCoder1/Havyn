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

## Rules that are not negotiable

1. **Never build JavaScript with raw f-strings.** Use `json.dumps` for each
   field. PT runs it through `new Function()`, so an unescaped device name is
   arbitrary code execution. `ptbuilder_generator.py` shows the correct pattern.
2. **Never build a path by concatenation.** Use `safe_name_component()` then
   `resolve_within()` from `shared/utils.py`.
3. **Never add an unauthenticated bridge endpoint.** Everything except `/ping`
   requires the token; see `bridge_token.py` for why loopback alone is not a
   control. (The file-bridge channel needs no token — the mailbox lives under a
   user-ACL'd `%LOCALAPPDATA%` dir a browser page can't reach.)
4. **Don't validate in the models.** Validation belongs in `domain/rules/` and
   returns `ValidationResult`, so the use case decides whether to proceed.
5. **A bug fix needs a test that fails without it.** Write the failing test
   first; if it passes before your change, it isn't testing the bug.
6. **Never guess a PT API signature.** If a method isn't already used somewhere
   in this repo, confirm it against Cisco's reference before writing code on top
   of it — PT answers a wrong call with a bare `Invalid arguments for IPC call
   "X"`, so a guess fails without telling you why.

## Working with the bridge

The bridge only really works with Packet Tracer open, so most verification is
offline. `tests/test_bridge_security.py` drives a real `PTCommandBridge` on an
ephemeral port — follow that pattern rather than mocking HTTP.

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
