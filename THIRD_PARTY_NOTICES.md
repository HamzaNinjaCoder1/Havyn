# Third-Party Notices

This project (AI Network Autopilot) incorporates components from the following
third-party sources. We are grateful for them and keep this file accurate.

## 1. MCP-Packet-Tracer (upstream foundation)

- **Project:** [MCP-Packet-Tracer](https://github.com/Mats2208/MCP-Packet-Tracer)
  (PyPI: `packet-tracer-mcp`)
- **Author:** Mateo ([@Mats2208](https://github.com/Mats2208))
- **License:** [MIT](https://github.com/Mats2208/MCP-Packet-Tracer/blob/main/LICENSE)
  — Copyright (c) 2026 Mateo - Packet Tracer MCP Server
- **What we retain and how:** the low-level Packet Tracer transport and device
  knowledge from that repository are used behind an isolated adapter boundary
  (`src/network_autopilot/adapter/upstream/`). Retained components include:
  - `PTCommandBridge` HTTP command bridge and its request/result correlation
    (`live_bridge.py`)
  - the per-machine bridge token security model (`bridge_token.py`)
  - the file-mailbox bridge channel concept (`file_bridge.py`)
  - the device / module / cable catalog data (`infrastructure/catalog/`)
  - shared path-safety and ping-classification utilities (`shared/utils.py`)
  - verified Packet Tracer IPC/JS command patterns (extension script engine and
    tool registry)
- The upstream `src/packet_tracer_mcp/` package is kept intact and unmodified in
  this repository. All higher-level engineering — the NetworkSpec schema, the
  IP/subnet planning, the port/cable solver, the compiler, verification,
  diagnosis, repair, evidence and safety systems — is written independently for
  AI Network Autopilot. See `PROVENANCE.md` for the full classification.

## 2. PTBuilder (inspiration credit, carried over from upstream)

- **Project:** [PTBuilder](https://github.com/kimmknight/PTBuilder)
- **Author:** Kim Knight ([@kimmknight](https://github.com/kimmknight))
- Upstream credits PTBuilder as the pioneer of driving Packet Tracer's Script
  Engine from JavaScript and as the starting point / reference for its
  Script-Engine helper layer. That credit is carried over here because we reuse
  the upstream extension runtime in which those helpers live.
- **No PTBuilder-derived code is copied into the AI Network Autopilot engine.**
  PTBuilder-derived reference material is not part of this repository, and the
  product-specific logic of this project does not depend on it.

## 3. Runtime dependencies

| Package | License | Use |
| --- | --- | --- |
| [Model Context Protocol Python SDK](https://github.com/modelcontextprotocol/python-sdk) (`mcp[cli]`) | MIT | MCP server surface |
| [Pydantic](https://docs.pydantic.dev) | MIT | typed models |
| [pytest](https://pytest.org) | MIT | testing |

Cisco Packet Tracer is a product of Cisco Systems, Inc. This project is an
independent automation tool and is not affiliated with, endorsed by, or
sponsored by Cisco.
