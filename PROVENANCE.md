# Provenance — what is upstream, what is ours

AI Network Autopilot is an independently engineered product built on top of a
clearly bounded foundation. This document is the authoritative map of which
code comes from where. The rule it enforces: **never represent retained
upstream code as newly authored, and never hide what was retained.**

## UPSTREAM FOUNDATION (retained, MIT-licensed, attributed)

Package `src/packet_tracer_mcp/` — kept intact and unmodified. It is the
low-level Packet Tracer integration layer of
[MCP-Packet-Tracer](https://github.com/Mats2208/MCP-Packet-Tracer) by Mateo
(@Mats2208), MIT licensed (see `THIRD_PARTY_NOTICES.md` and `LICENSE`).

- HTTP command bridge + result correlation (`infrastructure/execution/live_bridge.py`)
- Bridge token security (`infrastructure/execution/bridge_token.py`)
- File-mailbox bridge (`infrastructure/execution/file_bridge.py`)
- Device/module/cable catalogs (`infrastructure/catalog/`)
- Path safety / ping classification utilities (`shared/utils.py`)
- The Packet Tracer extension runtime (`EXTENSION/`) — upstream's own build,
  whose Script-Engine helper layer upstream credits as PTBuilder-inspired.
- The 61 MCP tools, generators, planners and validators of the upstream server
  remain available exactly as they were. Nothing of the upstream product was
  removed or renamed.

## OUR NEW ENGINEERING (written independently for this product)

Package `src/network_autopilot/` — the AI Network Autopilot engine. All of the
following is new code authored for this product:

- `core/observability.py` — the explicit `Observable` model
  (KNOWN / UNKNOWN / NOT_OBSERVED / UNSUPPORTED / STALE)
- `core/ids.py` — stable IDs, idempotency keys, build fingerprints
- `core/network_spec.py` — the canonical, versioned, typed `NetworkSpec`
- `core/requirements.py` — requirement/acceptance verdict semantics
  (PASS / FAIL / UNKNOWN / NOT_RUN, UNKNOWN-never-PASS)
- `core/actual_state.py` — `ActualNetworkState` observation model
- `core/receipts.py` — operation receipts with preconditions/postconditions
- `core/safety.py` — safety modes and mutation budgets
- `adapter/base.py` — the `PacketTracerAdapter` abstraction
- `adapter/null_adapter.py` — deterministic offline adapter
- `adapter/upstream/` — the *only* module allowed to import the upstream
  package; small wrappers + JS command builders (which independently apply the
  `json.dumps` quoting discipline)
- `evidence/` — evidence store and secret redaction
- `acceptance/` — acceptance-criterion evaluator
- `mcp/` — the Autopilot's own MCP server surface

## UNIQUE PRODUCT FEATURES (roadmap, all new code)

Requirement analysis traceability, deterministic IPAM/VLSM, port/media/module
solver, network compiler + operation DAG, semantic expected-vs-actual diff,
multi-level verification, packet forensics normalization, evidence-based
diagnosis, bounded auto-repair with rollback, fault-injection lab mode, build
fingerprints, and evidence-backed final reports.

## Rules this repository follows

1. Only `network_autopilot/adapter/upstream/` may import `packet_tracer_mcp`.
2. No PTBuilder-derived code is copied into the Autopilot engine.
3. Upstream *product logic* (its IP planner, topology diff, auto-fixer,
   validator, etc.) is treated as reference knowledge about Packet Tracer
   behavior — the Autopilot's equivalents are implemented independently.
4. Upstream Packet Tracer API knowledge (verified IPC/JS patterns) is reused as
   *knowledge*, consistent with the MIT license, with attribution preserved
   above.
