# Provenance — what is upstream, what is ours

Havyn is an independently engineered product built on top of a
clearly bounded foundation. This document is the authoritative map of which
code comes from where. The rule it enforces: **never represent retained
upstream code as newly authored, and never hide what was retained.**

## INDEPENDENCE STATUS — migration complete, upstream removed

`src/network_autopilot/` no longer imports any upstream package, and the
upstream package is no longer part of this repository. The engine is
self-contained: engine, catalogs, and **both runtime transports** are this
project's own. The proof is not a claim but a test — see
`tests/independence_blocker.py`, a meta-path finder that makes the blocklisted
names unimportable. The entire autopilot suite, live tests included, passes
with that blocker active.

| Upstream dependency | Replacement in `network_autopilot/` | Status |
| --- | --- | --- |
| `domain/services/auto_layout.py` | `pt/layout.py` | Replaced |
| `shared/utils.py` (ping classification) | `pt/ping.py` | Replaced |
| `shared/enums.py` (PortSpeed) | `pt/enums.py` | Replaced |
| `shared/constants.py` (DEFAULT_DNS) | `pt/constants.py` | Replaced |
| `infrastructure/catalog/devices.py` | `pt/catalog/devices.py` | Replaced |
| `infrastructure/catalog/aliases.py` | `pt/catalog/aliases.py` | Replaced |
| `infrastructure/catalog/modules.py` | `pt/catalog/modules.py` | Replaced |
| `infrastructure/catalog/cables.py` | `pt/catalog/cables.py` | Replaced |
| `infrastructure/execution/live_bridge.py` | `pt/http_bridge.py` | **Replaced** |
| `infrastructure/execution/bridge_token.py` | `pt/token.py` | **Replaced** |
| `infrastructure/execution/file_bridge.py` | `pt/mailbox.py` | **Replaced** |

Each transport replacement preserves the observable contract rather than
reinventing it, because the Packet Tracer extension is a fixed consumer:

- `pt/http_bridge.py` speaks the same endpoints the extension polls (`/ping`,
  `/next`, `/queue`, `/result`, `/status`), the same `reportResult` snippet
  shape, and the same request-id correlation; it keeps the token check, the
  `Host` check and the request-size caps.
- `pt/mailbox.py` uses the same on-disk protocol the extension's Script Engine
  implements — `req_<tag>.js` in, `res_<tag>.txt` out, `alive.txt` heartbeat —
  in the same directory, because the extension reads the paths from the token
  file and has no configuration channel.
- `pt/token.py` stores the shared secret at the same per-user path the
  extension reads, honours the same environment override, and enforces the
  same minimum strength.

## THIRD-PARTY FOUNDATION (MIT-licensed, attributed, no longer vendored)

The engine was originally grown alongside
[MCP-Packet-Tracer](https://github.com/Mats2208/MCP-Packet-Tracer) by Mateo
(@Mats2208), MIT licensed (see `THIRD_PARTY_NOTICES.md` and `LICENSE`). That
package's Python code is **no longer vendored in this repository**; every
replacement listed in the table above lives in `src/network_autopilot/`.

Two things derived from that foundation remain, and both are attributed:

1. The **Packet Tracer extension runtime** (`EXTENSION/`) that Havyn drives,
   including the measured API surface and device behaviour.
2. The **on-disk token and mailbox contract** (`packet-tracer-mcp` state
   directory, bridge endpoints). The extension resolves those paths by name
   and has no configuration channel, so the names are a compatibility
   constraint rather than a code dependency — see `INDEPENDENCE.md`.

## OUR ENGINEERING (written for this product)

Package `src/network_autopilot/`:

- `core/` — the explicit `Observable` model, stable IDs and build fingerprints,
  the canonical versioned `NetworkSpec`, requirement/acceptance verdict
  semantics (PASS / FAIL / UNKNOWN, UNKNOWN-never-PASS), `ActualNetworkState`,
  operation receipts, safety modes and mutation budgets
- `adapter/` — the `PacketTracerAdapter` abstraction, a deterministic offline
  adapter, and `adapter/packet_tracer/` — the project-owned adapter boundary
  containing the transport client and verified Packet Tracer command builders
- `compiler/` — the network compiler and operation DAG, the port/media/module
  solver, preflight gate, port reservation, canvas layout gate and executor
- `pt/` — the Packet Tracer integration layer written for this project:
  - `layout.py` — the auto-layout engine (rectangle collision model, role
    tiering, barycenter ordering, guaranteed de-overlap)
  - `ping.py` — PT console ping parsing into ok/partial/none
  - `enums.py`, `constants.py` — port-speed families and fixed PT values
  - `catalog/` — device, module and cable knowledge with the port naming and
    slot-rewriting rules PT actually uses
  - `http_bridge.py` — the loopback HTTP command bridge, with token
    authentication, `Host` validation, body caps and request-id correlation
  - `mailbox.py` — the file mailbox the Script Engine polls when the
    extension window is closed
  - `token.py` — the shared secret both channels authenticate with
  - `live_layout.py` — bridge plumbing behind the live layout tools
- `evidence/`, `acceptance/`, `verification/`, `diagnosis/`, `diff/`,
  `repair/`, `ipam/`, `mcp/` — evidence store and redaction, acceptance
  evaluator, multi-level verification, packet forensics, semantic diff,
  evidence-based diagnosis, bounded repair, deterministic IPAM/VLSM, and the
  Autopilot's own MCP server

## UNIQUE PRODUCT FEATURES (all new code)

Requirement analysis traceability, deterministic IPAM/VLSM, port/media/module
solver, network compiler + operation DAG, semantic expected-vs-actual diff,
multi-level verification, packet forensics normalization, evidence-based
diagnosis, bounded auto-repair with rollback, fault-injection lab mode, build
fingerprints, evidence-backed final reports, topology auto-layout, and the
runtime transport layer that drives Packet Tracer.

## Rules this repository follows

1. `network_autopilot/` must import nothing outside the standard library, `mcp`
   and `pydantic`. This is enforced by `tests/independence_blocker.py`, which
   the autopilot suite runs under: it makes the blocklisted names unimportable
   and the suite still passes, live tests included.
2. No PTBuilder-derived code is copied into the engine.
3. A dependency is only recorded as replaced above once the new implementation
   passes the existing regression suite. Each one is additionally checked
   against live Packet Tracer where the behaviour is observable there.
4. Upstream Packet Tracer API knowledge (verified IPC/JS patterns) is reused as
   *knowledge*, consistent with the MIT license, with attribution preserved
   above. Nothing removed from this repository is represented as part of this
   product, and nothing newly authored is presented as upstream's.
