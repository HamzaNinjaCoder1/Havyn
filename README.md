# Havyn

[![tests](https://github.com/HamzaNinjaCoder1/Havyn/actions/workflows/tests.yml/badge.svg)](https://github.com/HamzaNinjaCoder1/Havyn/actions/workflows/tests.yml)

**Closed-loop network engineering for Cisco Packet Tracer.**

Havyn turns a typed network specification into a validated plan, compiles it
into a deterministic operation DAG, executes that DAG against a live Packet
Tracer, and then *proves* the result with fresh read-back and real traffic.

The core rule is that a command receipt is not proof. Mutations are confirmed
only by fresh Packet Tracer read-back. Unknown state remains `UNKNOWN`.

## Architecture

```text
spec -> NetworkSpec -> IPAM plan -> port/media/module solver -> preflight gate
     -> layout -> compile to DAG -> safety gate -> Packet Tracer execution
     -> read-back -> per-criterion verification -> evidence -> bounded repair
```

`src/network_autopilot/` is the whole engine: the typed spec model, the
compiler, the port solver, IPAM, layout, the execution DAG, safety controls,
verification, the evidence store, and the repair engine. The engine is
**self-contained** — it imports nothing outside the standard library, `mcp`
and `pydantic`, and it carries its own Packet Tracer transport
(`network_autopilot.pt`: authenticated loopback bridge + Script Engine file
mailbox) and its own device catalog.

## Packet Tracer integration

The extension in `EXTENSION/` provides the MCP Control Center and Script
Engine bridge. The Python side starts an authenticated loopback bridge on
`127.0.0.1:54321`. The extension polls it when the Control Center is open. The
Script Engine also provides a file-mailbox channel when the webview is closed,
provided the extension is loaded by Packet Tracer.

Install the extension through Packet Tracer's supported Script Module flow:

1. Open **Extensions -> Scripting -> Configure PT Script Modules**.
2. Add the built extension package for this project.
3. Open **Extensions -> MCP BUILDER**.
4. Start the MCP server and confirm that bridge status reports Packet Tracer
   polling.

## Installation

```bash
pip install -e ".[test]"
python -m network_autopilot
```

Configure the second command in the MCP client of your choice. Offline
planning and compiler tests can use the deterministic null adapter without
Packet Tracer.

## Supported workflow

- topology interpretation and typed network specifications
- exact device inventories with observe-before-create and idempotent reuse
- deterministic graph-aware placement with overlap and crossing validation
- interface reservation and cable/media planning
- IPv4/IPv6 addressing and subnet validation
- IOS host, interface, route, VLAN, and switch configuration generation
- authenticated HTTP and file-mailbox Packet Tracer control
- fresh topology, port, configuration, route, and connectivity read-back
- evidence-backed semantic diff and bounded repair
- real project save checks where Packet Tracer exposes them

Support is model- and Packet Tracer-version-dependent. Unobservable criteria
remain `UNKNOWN` rather than being guessed.

## Running a build

With Packet Tracer open and the extension loaded, the whole pipeline runs from
one command at the repository root:

```bash
python build_havyn.py my-network.spec.json autonomous
```

The second argument is the safety mode: `read_only`, `dry_run`,
`approval_required` or `autonomous`. The build prints the elapsed time, a
per-criterion verdict report grouped into functional layers (L2, L3, security,
data plane) and the evidence directory it wrote.

## MCP tools

`python -m network_autopilot` starts the stdio MCP server. The LLM writes a
`NetworkSpec`; the deterministic pipeline does the rest — the model never
authors operations by hand.

| Tool | Purpose |
| --- | --- |
| `net_spec_validate` | validate a spec against the typed model |
| `net_plan` | IPAM addressing plan |
| `net_preflight` | port/media/module compatibility gate |
| `net_compile` | compile the spec to an operation DAG |
| `net_build` | full build: execute, verify, report |
| `net_observe` | read live Packet Tracer state |
| `net_verify` | verify a spec against the live canvas |
| `net_diff` | semantic diff of spec vs observed state |
| `associate_wireless_client` | install a radio and join an SSID |
| `pt_launch` / `pt_status` | start Packet Tracer and report channel health |
| `pt_auto_layout` / `pt_find_free_position` | deterministic placement |
| `autopilot_status` | engine and channel status |

## Minimal specification

A specification is a `*.spec.json` document validated against
`spec_schema.json` and, at parse time, against `NetworkSpec`. This one puts two
PCs behind a router on a DHCP subnet and asks for one reachable flow:

```json
{
  "name": "branch-office",
  "devices": [
    {"id": "r1", "role": "router"},
    {"id": "s1", "role": "switch"},
    {"id": "pc1", "role": "pc"},
    {"id": "pc2", "role": "pc"}
  ],
  "links": [
    {"id": "l-r1-s1",
     "a": {"device": "r1", "interface_hint": "GigabitEthernet0/1"},
     "b": {"device": "s1", "interface_hint": "FastEthernet0/1"}},
    {"id": "l-s1-pc1",
     "a": {"device": "s1", "interface_hint": "FastEthernet0/2"},
     "b": {"device": "pc1"}},
    {"id": "l-s1-pc2",
     "a": {"device": "s1", "interface_hint": "FastEthernet0/3"},
     "b": {"device": "pc2"}}
  ],
  "subnets": [{"id": "lan", "cidr": "10.0.10.0/24", "dhcp": true}],
  "ip_assignments": [
    {"device": "r1", "subnet": "lan", "ip": "10.0.10.1",
     "interface_hint": "GigabitEthernet0/1"},
    {"device": "pc1", "subnet": "lan"},
    {"device": "pc2", "subnet": "lan"}
  ],
  "acceptance_criteria": [
    {"id": "A-1", "requirement_ids": [],
     "test": {"kind": "flow", "source": "pc1", "destination": "r1",
              "expect": "reachable"}}
  ]
}
```

`acceptance_criteria` entries are objects whose `test` carries the `kind`;
`requirement_ids` stays an array even when it is empty. Device ids are
`r1`/`s1`/`pc1`, not prefixed: they are the names the build creates and the
names a cleanup pass must delete.

Validate a spec before building it:

```bash
python -c "import json; from src.network_autopilot.core.network_spec import NetworkSpec; NetworkSpec.model_validate(json.load(open('my-network.spec.json'))); print('ok')"
```

A wrong `interface_hint` is neither auto-corrected nor reported as an IPC
error. The port solver marks the port absent, preflight refuses the plan, and
`net_build()` returns an error before any Packet Tracer call is made.

## Safety model

The execution DAG enforces preconditions, dependency ordering, mutation
budgets, safety modes, idempotency, and postcondition checks. Preflight is a
hard gate: invalid ports, incompatible media, missing modules, collisions, or
invalid addressing prevent Packet Tracer mutation.

Repair is bounded and evidence-based. Failed operations block unsafe
dependents. The engine does not blindly retry destructive operations.

The mutation budget for a sized build is `min(40 + 3*ops, 4000)` where `ops`
is the compiled DAG size; the unsized default is `25`. Both `net_build()` and
the orchestrator size the budget from the actual plan, so the budget never
aborts a legitimate build and never mutates without a ceiling.

## Testing

Run the offline suite from the repository root:

```bash
python -m pytest
```

Offline tests cover the compiler, solver, layout, safety model, bridge
protocol, verification, and failure handling. They do not prove that a
particular Packet Tracer installation has the extension loaded or that a live
canvas has a desired visual appearance. Live acceptance evidence is therefore
reported separately from unit and offline test results.

## Independence and attribution

Havyn is self-contained: the engine, its device catalog and both runtime
transports live inside `src/network_autopilot/`, and the test suite enforces
that boundary with `tests/independence_blocker.py`. Cisco Packet Tracer is a
Cisco product; this project is independent and is not endorsed by Cisco.
Attribution for retained third-party material is in `PROVENANCE.md` and
`THIRD_PARTY_NOTICES.md`.

## Known limitations

- Packet Tracer must be installed and running for live execution.
- The extension must be loaded in the active Packet Tracer instance.
- Some device configuration and visual properties are not exposed by the
  Script Engine API; those criteria remain `UNKNOWN`.
- Reopen verification depends on what the installed extension exposes for
  project lifecycle control.
