# Project Independence

Havyn is a self-contained project. Its runtime uses only code packaged in this
repository plus the declared Python dependencies. It does not clone, download,
or import code from a remote Git repository at runtime.

The engine (`src/network_autopilot/`) is the whole product. It carries its own
Packet Tracer transport — authenticated loopback HTTP bridge, Script Engine file
mailbox and bridge token, all in `network_autopilot.pt` — and its own device,
module and cable catalog. It imports nothing outside the standard library, `mcp`
and `pydantic`.

This boundary is enforced by a test rather than by convention:
`tests/independence_blocker.py` is a pytest plugin that fails collection if any
module imported during the autopilot suite resolves to a name on its blocklist.

The project-owned live adapter is `network_autopilot.adapter.packet_tracer`.

## Runtime data directory

The bridge token and the Script Engine mailbox live under a
`packet-tracer-mcp` directory in the platform state location:

| Platform | Path |
| --- | --- |
| Windows | `%LOCALAPPDATA%\packet-tracer-mcp\` |
| Linux | `$XDG_STATE_HOME/packet-tracer-mcp\`, else `~/.local/state/packet-tracer-mcp/` |
| macOS | `~/.packet-tracer-mcp/` |

The directory name is a **wire and on-disk compatibility constraint**, not a
vestige of a removed package: the Packet Tracer extension resolves the token
and the mailbox by this exact path (`EXTENSION/script-engine/main.js`), and the
extension has no configuration channel to change it. Renaming it would strand
every already-installed extension, so the name is kept deliberately and
documented here.

`%LOCALAPPDATA%` is used rather than `%APPDATA%` because these are
machine-local secrets and state, not roaming user documents.
