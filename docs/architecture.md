# Architecture

Havyn is a single, self-contained engine under `src/network_autopilot/`. Its
layout keeps Packet Tracer details behind an adapter so the compiler, solver and
verification layers stay pure and testable offline.

```text
core/          NetworkSpec, Observable, receipts, safety, actual-state models
ipam/          deterministic addressing plan + validator
compiler/      spec → operation DAG, port/media/module solver, preflight,
               layout gate, policy rendering, executor
adapter/       PacketTracerAdapter contract
  null_adapter.py              deterministic offline adapter (no PT)
  packet_tracer/               the live adapter
    bridge_adapter.py          receipts, read-back, flow tests
    js_commands.py             verified Packet Tracer Script Engine builders
pt/            transport + catalog (no engine knowledge)
  http_bridge.py  mailbox.py  token.py  ping.py  enums.py  constants.py
  layout.py       live_layout.py
  catalog/        devices.py  modules.py  cables.py  aliases.py
verification/   per-criterion verdicts with evidence chains
acceptance/    acceptance evaluator, functional-layer report
diagnosis/      evidence-based diagnosis
diff/           semantic expected-vs-actual diff
repair/         bounded diagnose → plan → gate → execute → re-verify
evidence/       evidence store + redaction
mcp/server.py   the MCP tool surface (thin handlers over the engine)
```

## Request flow

```text
MCP tool call
   │
mcp/server.py                     ← validates args, delegates to the engine
   │
orchestrator.Autopilot            ← build / verify / diff / repair orchestration
   │
compiler/                         ← lint → IPAM → port solver → preflight → layout
   │                                 → operation DAG
   │
compiler/executor.py              ← SafetyGate + port reservations + receipts
   │
adapter.packet_tracer             ← receipts, read-back postconditions
   │
pt/http_bridge.py  or  pt/mailbox.py   ← HTTP (:54321) or file mailbox
   │
MCP Control Center extension → PT Script Engine → Cisco Packet Tracer
```

## The live bridge

Two channels reach the extension; the adapter picks one per command.

**HTTP** — `pt/http_bridge.py` runs a small HTTP server on `127.0.0.1:54321`,
used while the extension window is open:

- `GET /next` — the PT webview polls this for queued commands (token required).
- `POST /queue` — the server enqueues a JS command (token required).
- `POST /result` / `GET /result` — round-trips results back via `reportResult()`.
- `GET /ping` — unauthenticated identity check (fingerprint of the token only).

**File channel** — `pt/mailbox.py`, used when the Control Center window is
closed. The Script Engine polls a mailbox under
`%LOCALAPPDATA%\packet-tracer-mcp\bridge\` (`req_*.js` → execute → `res_*.txt`,
plus an `alive.txt` heartbeat). It needs no token: a browser page cannot write
into a user-ACL'd local directory. The `packet-tracer-mcp` directory name is a
compatibility constraint — the extension resolves it by name and cannot be
configured otherwise (see [INDEPENDENCE.md](https://github.com/HamzaNinjaCoder1/Havyn/blob/main/INDEPENDENCE.md)).

**Auth** — the HTTP bridge requires a per-machine token (`pt/token.py`,
auto-generated under `%LOCALAPPDATA%`). The extension reads it from disk through
the Script Engine — no pairing, no pasting. See [SECURITY.md](https://github.com/HamzaNinjaCoder1/Havyn/blob/main/SECURITY.md).

!!! info "Inspired by PTBuilder"
    The Script-Engine helper layer that runs inside Packet Tracer was inspired by
    [PTBuilder](https://github.com/kimmknight/PTBuilder); the extension itself (the
    MCP Control Center) is this project's own — see [Credits & Attribution](credits.md).

!!! warning "Trust model"
    The Script Engine builders and any raw-JS path execute JavaScript in PT's
    Script Engine **by design**. The token stops a browser page from reaching
    `:54321`, and the file channel is confined to a user-owned directory. This is
    a single-user local desktop tool — run it on a machine and with topologies you
    trust.
