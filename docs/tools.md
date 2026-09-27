# MCP Tools

Havyn exposes a small, deliberate tool surface. The point is not to give the
model many ways to poke Packet Tracer — it is to give it one **typed spec** and a
deterministic pipeline. Every tool below is a thin handler over the engine; the
LLM never authors an operation by hand.

Start the server with `python -m network_autopilot`. Tools that touch a running
Packet Tracer require the [live bridge](live-deploy.md) to be connected.

## Engine status

| Tool | What it does |
| --- | --- |
| `autopilot_status` | Engine version, channel health and bridge reachability. |
| `pt_status` | Packet Tracer / Script Engine heartbeat. |
| `pt_launch` | Start Packet Tracer and wait for the extension to poll. |

## Spec lifecycle (offline — no Packet Tracer required)

These run against the null adapter, so they are safe to call at any time and are
what an agent should use to reason about a topology before committing to it.

| Tool | What it does |
| --- | --- |
| `net_spec_validate` | Validate the spec against the typed `NetworkSpec` model. |
| `net_plan` | Deterministic IPAM plan: subnets, allocations, conflicts. |
| `net_preflight` | Port / media / module / address compatibility gate. |
| `net_compile` | Compile the spec into the operation DAG and report the plan. |

## Live operations

| Tool | What it does |
| --- | --- |
| `net_build` | Full build: execute the DAG through the safety gate, then verify. |
| `net_observe` | Read live state from Packet Tracer: ports, addresses, VLAN/trunk. |
| `net_verify` | Run acceptance criteria and per-criterion verdicts against the canvas. |
| `net_diff` | Semantic diff of the spec versus what is actually there. |
| `associate_wireless_client` | Install a wireless module and join an SSID. |

## Layout

| Tool | What it does |
| --- | --- |
| `pt_auto_layout` | Deterministic, non-overlapping placement (`full` / `rows` / `zones`). |
| `pt_find_free_position` | A free canvas position for a new device. |

## Typical sequence

```text
net_spec_validate -> net_plan -> net_preflight -> net_compile
                                          |
                                          v
                              net_build (autonomous)
                                          |
                                          v
                              net_verify -> net_diff
```

`net_build` takes a `safety_mode`:

| Mode | Behaviour |
| --- | --- |
| `read_only` | observe and report; no mutation at all |
| `dry_run` | compile and print the plan; nothing is pushed |
| `approval_required` | stop before each mutation class for approval |
| `autonomous` | execute the whole plan inside the mutation budget |

## Raw Script Engine access

`network_autopilot/pt/` also exposes verified Script Engine builders
(`js_commands.py`) used internally by the adapter — device creation, cabling,
`configureIosDevice`, port-level address and mask, DHCP flags, and inspection
calls. They are **not** MCP tools: they are the engine's own, tested vocabulary
for Packet Tracer, and the adapter composes them into the operations the DAG
declares. An agent that needs raw access reaches it through raw-JS debugging on
the bridge, not by inventing Script Engine code.

!!! warning "Trust model"
    Anything that executes JavaScript inside Packet Tracer does so by design.
    The bridge token stops a browser page from reaching the loopback port, and the
    file channel is confined to a user-owned directory. This is a single-user
    local desktop tool — run it on a machine and with topologies you trust.
    See [SECURITY.md](https://github.com/HamzaNinjaCoder1/Havyn/blob/main/SECURITY.md).
