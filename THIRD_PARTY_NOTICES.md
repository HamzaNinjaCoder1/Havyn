# Third-Party Notices

Havyn incorporates components from the following third-party sources. We are
grateful for them and keep this file accurate.

## 1. MCP-Packet-Tracer (historical foundation)

- **Project:** [MCP-Packet-Tracer](https://github.com/Mats2208/MCP-Packet-Tracer)
  (PyPI: `packet-tracer-mcp`)
- **Author:** Mateo ([@Mats2208](https://github.com/Mats2208))
- **License:** [MIT](https://github.com/Mats2208/MCP-Packet-Tracer/blob/main/LICENSE)
  — Copyright (c) 2026 Mateo
- **Status:** the upstream package is **no longer part of this repository.** The
  MIT licence permitted its use as a starting point; its code has been removed.
  What is retained here is the *knowledge* it helped produce, and the
  attribution for it:

  - the **Packet Tracer extension runtime** (`EXTENSION/`) that Havyn drives,
    together with the API surface and device behaviour that had to be
    measured rather than assumed;
  - the on-disk **token and mailbox contract** (`packet-tracer-mcp` state
    directory, bridge endpoints) that the extension resolves by name and cannot
    be configured otherwise — see `INDEPENDENCE.md`.

  Havyn's engine (`src/network_autopilot/`) implements its own catalog, port and
  media solver, module solver, ping parser, canvas layout, HTTP command bridge,
  file mailbox and bridge token. No upstream Python source is present in this
  repository.

### Honest record of what the initial implementation replaced

The `network_autopilot.pt` package was written for this project against
documented Packet Tracer behaviour and the interface contracts the engine
requires. Two caveats are worth stating plainly:

- Some catalog entries were informed by data the upstream project had probed
  from a running Packet Tracer. Device model names, port names and slot
  numbering are facts about PT and the hardware, not creative expression.
- The two transports must match a fixed external consumer. The Packet Tracer
  extension is that consumer, and it has no configuration channel, so the HTTP
  bridge serves the same endpoints and the mailbox uses the same on-disk
  filenames in the same directory. That is protocol compatibility, which the
  MIT licence expressly permits.

## 2. PTBuilder (inspiration credit)

- **Project:** [PTBuilder](https://github.com/kimmknight/PTBuilder)
- **Author:** Kim Knight ([@kimmknight](https://github.com/kimmknight))
- PTBuilder pioneered driving Packet Tracer's Script Engine from JavaScript and
  is the reference for the Script-Engine helper layer in the extension runtime.
  That credit is carried over because Havyn reuses that extension runtime.
- **No PTBuilder-derived code is copied into the Havyn engine.** PTBuilder
  reference copies are not committed to this repository
  (see `.gitignore`: only `main.js` is tracked under `EXTENSION/script-engine/`),
  and the engine does not depend on them.

## 3. Runtime dependencies

| Package | License | Use |
| --- | --- | --- |
| [Model Context Protocol Python SDK](https://github.com/modelcontextprotocol/python-sdk) (`mcp[cli]`) | MIT | MCP server surface |
| [Pydantic](https://docs.pydantic.dev) | MIT | typed spec models |
| [pytest](https://pytest.org) | MIT | testing |

## 4. Trademarks

Cisco Packet Tracer is a product of Cisco Systems, Inc. Havyn is an
independent automation tool and is not affiliated with, endorsed by, or
sponsored by Cisco. All product names and trademarks are the property of their
respective owners.
