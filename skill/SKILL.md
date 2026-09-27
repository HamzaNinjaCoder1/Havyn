---
name: packet-tracer
description: >-
  Use when building, automating, or debugging Cisco Packet Tracer networks through the
  `packet-tracer` MCP (the `pt_*` tools) — planning topologies from natural language,
  live-deploying into a running Packet Tracer, adding devices / links / modules, applying
   ACL / NAT, exporting configs, or writing raw Script-Engine JS. This is
  the authoritative reference for the tool catalog, the mandatory discover→plan→validate→deploy
  workflow, the EXACT PT Script-Engine API (so you never invent method names), and the known
  rough edges. Load it before driving the MCP so you act from verified facts, not guesses.
---

# Havyn — operating guide

A Model Context Protocol server (`python -m network_autopilot`) that does **closed-loop
network engineering** for *Cisco Packet Tracer*: you write a typed `NetworkSpec`, and a
deterministic pipeline compiles it, gates it, executes it against a live PT, and then
**verifies the result with real traffic**.

You never hand-write operations, Script Engine batches or IOS CLI. You describe the
network; the engine decides the ports, the media, the config and the order.

## ⛔ Prime directive: never invent a PT fact

The single biggest failure mode for a model driving Packet Tracer is **guessing** — a
model name, a port name, a slot id, a cable type, a module name, or a Script-Engine API
method. Every one of these is **discoverable**, and a wrong guess either fails preflight
or (for raw JS) **freezes Packet Tracer** (see the modal-freeze rule below).

Rules:
1. If you did not read it from a tool result or the **Verified Script-Engine API** table
   below, **you do not know it** — look it up first.
2. Write **roles** in a spec (`"role": "router"`), not model names, unless the model is
   stated in the requirement. The catalog resolves the role to a real model.
3. Run `net_spec_validate` → `net_plan` → `net_preflight` before `net_build`. Preflight is a
   **hard gate**: it refuses a bad port, cable, module or address by name, before any
   mutation.
4. After a build, verify with `net_verify`, and diff with `net_diff`. A green build is not
   a verified network.
5. When something is not in the catalog, say so. The engine refuses by name rather than
   half-configuring — do not work around a refusal by inventing config.

## The bridge (mental model)

```
LLM ──▶ MCP server ──▶ HTTP bridge :54321 ──▶ MCP Control Center (PT extension) ──▶ PT Script Engine
                       └─ or file mailbox ──▶ Script Engine polling
```

- The MCP queues JS; the PT extension polls `/next` and runs each command in PT's
  **Script Engine**, posting results back.
- **`XMLHttpRequest` does NOT exist in the Script Engine** (only in the extension webview).
  That's why all I/O goes through the bridge.
- Confirm the link with **`pt_status`**. If it reports not connected, the user opens
  **Extensions → MCP BUILDER** in PT (or dismisses a stuck error modal).

## Mandatory workflow

- New topology, stepwise (preferred): `net_spec_validate` → `net_plan` → `net_preflight`
  → `net_compile` → `net_build` → `net_verify`.
- New topology, from a spec file: `python build_havyn.py <spec>.json <safety_mode>`.
- Review before committing: `net_build` with `safety_mode="dry_run"` prints the whole plan
  and mutates nothing.
- Inspect a live topology: `pt_status` → `net_observe` → `net_diff`.

## Tool catalog (14)

**Engine status:** `autopilot_status` (version, channel health), `pt_status` (PT heartbeat),
`pt_launch` (start PT and wait for the extension to poll).

**Spec lifecycle — offline, no Packet Tracer needed, run these first:**
`net_spec_validate`, `net_plan` (IPAM), `net_preflight` (the hard gate), `net_compile`
(operation DAG).

**Live:** `net_build` (execute + verify), `net_observe` (live ports, addresses, VLAN/trunk
state), `net_verify` (acceptance + per-criterion verdicts), `net_diff` (spec vs observed),
`associate_wireless_client` (module swap + SSID join).

**Layout:** `pt_auto_layout`, `pt_find_free_position`.

`safety_mode` on `net_build`: `read_only`, `dry_run`, `approval_required`, `autonomous`.

## Verdicts are three-valued — and UNKNOWN is not a pass

`net_verify` reports **PASS / FAIL / UNKNOWN** per criterion. `UNKNOWN` means the property
could not be observed through PT. It is never upgraded to PASS.

- `REACHABLE` — every ping returned.
- `PARTIAL` — some loss. Normal on a first ping (ARP resolves hop by hop), so the engine
  retries before concluding; a persistent PARTIAL is a real fault, not a pass.
- `BLOCKED` — no replies.

The only data-plane probe verified against PT is **ICMP echo**. A criterion that declares
`tcp`/`udp` comes back `UNKNOWN` with the reason — an echo reply is not proof of a TCP
session, and the engine will not pretend otherwise.

## Advanced builds (verified live)
- **Inter-VLAN / router-on-a-stick:** subinterfaces with `encapsulation dot1Q`, one `/24` +
  DHCP pool per VLAN, trunk uplink. 2960 omits trunk `encapsulation` (dot1q-only); 3560
  emits it.
- **IPv6:** routers get `ipv6 address` via CLI + `ipv6 unicast-routing`; hosts use **SLAAC**.
  Static host IPv6 is NOT settable via the PT API (`addIpv6Address` fails on HostPort) —
  SLAAC is the path.
- **IPv6:** routers get `ipv6 address` via CLI + `ipv6 unicast-routing`; hosts use
  `ipv6 unicast-routing`; hosts use **SLAAC** (`configurePcIpv6` = enable + auto-config). Static host
  IPv6 is NOT settable via the PT API (`addIpv6Address` fails on HostPort) — SLAAC is the path.
- **WiFi laptops:** `wireless_laptops=True` swaps each Laptop-PT NIC to `PT-LAPTOP-NM-1W` (slot `"0"`)
  → `Wireless0`, and adds **one `AccessPoint-PT` per LAN**, each wired to that LAN's own switch.
  ⚠️ **Wireless addressing is NOT deterministic.** Laptops auto-associate on the default SSID and
  PT exposes **no SSID API** (verified: neither the AP nor its port has `setSsid`), so with more
  than one AP a laptop can associate to *any* of them and take a DHCP lease from **another LAN's
  pool**. Measured on PT 9.0.1: LT10, sitting right next to its own LAN's AP, still associated to
  the LAN-1 AP and got `192.168.0.13`; only with that AP powered off did it take `192.168.4.25`.
  The planner now emits a `WIRELESS_AMBIGUOUS_ASSOCIATION` **warning** when ≥2 APs coexist.
  Always confirm with `pt_inspect_ports`; for deterministic addressing use `wireless_laptops=False`.

## Verified PT Script-Engine API (for raw JS)

These are the **real** signatures (verified against the MCP's runtime patches and live testing).
If a method is not here, do **not** assume it exists.

**Globals**
- `getDevices(filter)` → **Array of NAME STRINGS**, not device objects (filter `""` = all,
  `"router"` = routers). Calling `.getName()` / `.getPorts()` on an element throws
  `TypeError: Property 'getName' of object R1 is not a function` — the element *is* `"R1"`.
  To get the object, feed each name to `ipc.network().getDevice(name)`.
- `allModuleTypes[name]` → module-type handle (passed to `addModule`)
- `reportResult(data)` → exists **only when `wait_result=True`**; POSTs the result back
- ❌ there is **no global `getDevice(...)`**; ❌ no `XMLHttpRequest` in the Script Engine

**Network / device** — `var d = ipc.network().getDevice("R1");`  // ← correct singular lookup, may be null
- `ipc.appWindow().getActiveWorkspace().getLogicalWorkspace()` → `lw`
  - `lw.addDevice(type, model, x, y)` → autoName · `lw.createLink(d1,p1,d2,p2,cableEnumInt)`
- `d.getPorts()` → **Array of port-name strings** — use `.length`, `[i]`, `.join(",")`.
  ❌ never `.size()`, `.at(i)`, `.getName()` on it (TypeError → modal → freeze).
- `d.getPort(name)` → Port | null · `d.getPower()/setPower(bool)/skipBoot()/setName(name)`
- `d.moveToLocation(x, y)` → reposiciona en el canvas lógico. Es lo que usa `pt_move_device`;
   en un solo envio de JS podes reacomodar decenas de dispositivos sin una llamada por cada uno.
- `d.addModule(slot, allModuleTypes[model], modelName)` → bool  (**slot is a STRING**)
- `d.getProcess("AclProcess")` → AclProcess | null (routers) · `d.enterCommand(cmd, mode)`
- `d.getCommandLine()` → console handle with `getOutput()`, `enterCommand(cmd)`, `getPrompt()`.
  Use **this** for console work: `getCommandPrompt()` exists ONLY on hosts and throws
  `TypeError` on any router. PCs expose both, so `getCommandLine()` covers both worlds.
  ⚠️ A router deployed by the MCP was never touched by console, so it sits at
  `Would you like to enter the initial configuration dialog? [yes/no]:` — a `ping` sent
  there is eaten as the yes/no answer. Prime it first: answer `no`, then send an empty
  command to clear `Press RETURN to get started.`
- `d.setDhcpFlag(bool)`, `d.setDefaultGateway(ip)`

**Port** — `var p = d.getPort("GigabitEthernet0/0");`
- `p.getLink()` → link | null (null = free) · `p.setIpSubnetMask(ip, mask)` · `p.setDefaultGateway(ip)`
- `p.setDnsServerIp(ip)` · `p.setAclInID(id)`/`p.setAclOutID(id)` (pass `""` to clear)

**AclProcess** — `var ap = d.getProcess("AclProcess");`
- `ap.addAcl(name)` · `ap.getAcl(name)` → acl|null · `ap.removeAcl(name)`
- `acl.addStatement(str)` → bool · `acl.getCommandCount()` → int

**Cable enum ints (for `lw.createLink`)**: straight 8100 · cross 8101 · roll 8102 · fiber 8103 ·
phone 8104 · cable 8105 · serial 8106 · auto 8107 · console 8108 · wireless 8109 · coaxial 8110 ·
octal 8111 · cellular 8112 · usb 8113 · custom_io 8114.

### 🔒 ALWAYS wrap raw JS in try/catch
```js
try { var d = ipc.network().getDevice("R1"); reportResult("ports="+d.getPorts().join(",")); }
catch (e) { reportResult("ERR: " + e); }
```
An **uncaught** error pops a modal `QMessageBox` in PT ("An error occurred… ReferenceError…") that is
**modal** and freezes the webview polling loop — the bridge goes "NOT connected" until a human clicks
**OK**. A **caught** error never does. (The server also guards commands now, but wrap anyway — it's
free insurance and keeps results clean.)

## Validation: read `plan.errors` before you deploy

`net_compile` / `net_build` return the refusal list and the warnings;
`pt_validate_plan` returns the same as typed codes. **An empty `errors[]` is now
meaningful** — it did not used to be. The validator only checked per-device facts,
so a topology split into islands passed with `valid: true`, deployed, and failed
silently. It now walks the graph.

| Code | What it means | What to do |
|---|---|---|
| `TOPOLOGY_DISCONNECTED` | The cabled devices form more than one component — something has no path to the rest | Usually the hub ran out of ports (a 2911 has 3 Gigabit). Use a bigger model, add a module, or fewer routers. **Do not deploy**: the isolated part cannot route |
| `OSPF_NO_NETWORKS` | An OSPF process with zero `network` statements | That router has no addressed, linked interface. Fix the links first |
| `OSPF_INVALID_ROUTER_ID` | `router-id 0.0.0.0`, which IOS rejects | Same root cause as above |
| `WIRELESS_AMBIGUOUS_ASSOCIATION` | **warning** — two or more APs share the default SSID | Not a blocker. Confirm with `pt_inspect_ports` which subnet each wireless host actually landed in |
| `IP_CONFLICT` / `INVALID_IP_ADDRESS` | Duplicate or malformed address | Re-address |
| `DHCP_GATEWAY_MISMATCH` | **warning** — the pool gateway is on no interface of that router | Check the pool against the router's interfaces |

Wireless hosts are excluded from the connectivity graph on purpose: they carry no
cable by design, so counting them as islands would flag every wireless topology
as broken.

## Big topologies: when the spec does not fit through a tool parameter

A large spec is thousands of lines of JSON, so passing it through a tool
parameter is impractical. Build it **locally** — it is the same code path the
MCP tools use, so nothing is lost:

```python
from network_autopilot.core.network_spec import NetworkSpec
from network_autopilot.core.safety import MutationBudget, SafetyGate, SafetyMode
from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter
from network_autopilot.orchestrator import Autopilot

spec = NetworkSpec.from_json("my-network.spec.json")   # devices, links, subnets, criteria
gate = SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget())
build, error = Autopilot(BridgeAdapter(), gate).build(spec)
if error:
    raise SystemExit(error)                 # preflight refusals land here, by name
print(build.human_text())                   # per-criterion verdicts
```

Or straight from the command line:

```bash
python build_havyn.py my-network.spec.json autonomous
```

The spec carries `routing`, `ospf`, `nat`, `ipv6`, VLAN/port configuration and
`policies` (ACLs) as typed fields — the compiler decides what each one emits and
**refuses by name** anything PT cannot do, instead of half-configuring it. There
is no need to hand-write Script Engine batches: the executor already sequences
the operations, gates them through `SafetyGate`, and confirms each one by
read-back. Raise `MutationBudget` for a topology larger than the default 25
mutations.

Verified: 88 devices, 88 links, 282 JS statements in 12 batches over the file
bridge. One `try/catch` per statement so a single failure does not take the batch
down. A plan can carry `ospf_configs`, `eigrp_configs` and `rip_configs` at the
same time — and one router can appear in two of them, which is how you build a
redistribution boundary. **`redistribute` itself is not in the plan model**: push
it as extra CLI with `configureIosDevice(name, cli)`.

## Common mistakes → corrections (do not repeat these)

| ❌ Wrong | ✅ Right | Why |
|---|---|---|
| `getDevice("R1")` | `ipc.network().getDevice("R1")` (or `getDevices("")` to list) | no global `getDevice` → ReferenceError → freeze |
| `d.getPorts().size()` / `.at(i)` / `.getName()` | `d.getPorts()[i]` (string array) | getPorts returns strings |
| adding a module | `slot="0"` (a **string**) | an int slot silently fails (`===` compare) |
| cable `"crossover"` | `"cross"` | alias works but `cross` is canonical |
| invent model `"Cisco4500"` | use a **role** in the spec, or read the catalog | use real catalog names |
| invent module `"NM-4T"` | read `pt/catalog/modules.py` first | use real module names |
| raw JS unwrapped | wrap in `try/catch` + `reportResult` | uncaught error freezes the bridge |
| trust an install "timeout" = failure | verify with `net_observe` | it often succeeds despite a timeout |

## Cables, ports, IP conventions

- **Cables (15)**: `straight, cross, roll, serial, fiber, console, phone, cable, coaxial, auto, wireless,
     octal, cellular, usb, custom_io`. Omit the link's `media` to infer (router-to-router &
  switch↔switch → `cross`; router↔switch, switch↔host → `straight`).
- **Exact ports**: 2911 `GigabitEthernet0/0..0/2` but 1941/2901 only `0/0..0/1`;
  ISR4321/4331 `GigabitEthernet0/0/0..`;
  2960/3560 `FastEthernet0/1..0/24` + `GigabitEthernet0/1..0/2`; PC/Laptop/Server `FastEthernet0`;
  HWIC-2T in `"0/x"` → `Serial0/x/0`,`Serial0/x/1`;
  **Cloud-PT has 8 ports**: `Serial0..3`, `Modem4`, `Modem5`, `Ethernet6`, `Coaxial7`.
  ⚠️ A cloud only *forwards* over its serial ports once frame relay is configured, and the MCP has no
  tool for that. For a WAN core that actually passes traffic, link routers to each other with `/30`s and
  hang the cloud off `Ethernet6` as an external stub.
- **IP plan** (`net_plan`): LANs `/24` from `192.168.0.0`, gateway `.1`, hosts from `.2`;
  router↔router `/30` from `10.0.0.0`; DHCP pool per LAN with `.1` excluded; routing
  `static|ospf|eigrp|rip|none` (+ `floating_routes`, `ospf_process_id`, `eigrp_as`).

## Module install by router family

`slot` is a **STRING**. Pick a module whose `compatible_with` includes the model — the MCP **rejects an
incompatible module up front** when the module declares `compatible_with` (HWIC/NIM/built-ins); generic
`PT-*` modules have no declared constraint, so choose sensibly there. Always confirm with
`net_observe` after install; a single module install may report a **timeout yet still succeed**.

| Router family | Module type | Slot (string) | Ports added | Status |
|---|---|---|---|---|
| ISR G2 — 2911/2901 | HWIC (`HWIC-2T`, `HWIC-1GE-SFP`) | `"0/0".."0/3"` | `Serial0/x/0`,`Serial0/x/1` | ✅ verified 2911 |
| ISR G2 — **1941** | HWIC | **only `"0/0"`, `"0/1"`** — it has 2 slots, not 4 | `Serial0/x/0`,`Serial0/x/1` | ✅ verified 1941 (PT 9.0.1) |
| ISR 4000 — ISR4321/4331 | NIM (`NIM-2T`, `NIM-ES2-4`) | **`"0/1"`, `"0/2"`** | `Serial0/1/0`,`Serial0/1/1` | ✅ verified ISR4321 & ISR4331 |
| 2811 / 2620XM / 2621XM | NM (`NM-4A/S`, `NM-2FE2W`,…) | **`"1"`** | `Serial1/0..1/3` | ✅ verified 2811 |
| Router-PT (generic) | NM (`NM-*`, `PT-ROUTER-NM-*`) | `"1"` | ⚠️ non-standard ids (e.g. `Serial2/0`) | installs, odd port names |

> Some docstrings say NIM slots are `"0"`/`"1"` - **that is
> wrong**; the working slot is **`"0/1"`** (chassis/subslot). HWIC = `"0/x"`, NM = `"1"`, NIM = `"0/1"`.
> The install *mechanism* (`addModule`) is identical across routers — only the **slot string** differs
> by family. Always confirm the result with `net_observe`.

Install several modules in one operation (one power-cycle); individual installs
power-cycle the device and can exceed the wait window (and often report a timeout despite succeeding).

## Known rough edges (verified by benchmark against PT 9.0.1)

- **Module compatibility is enforced for modules that declare `compatible_with`** (HWIC/NIM/built-ins
  reject a wrong model); generic `PT-*` modules carry no constraint, so still pick sensibly.
- **A single module install can report a timeout but still succeed** - verify ports, don't blindly retry.
  (A batch install reports `installed` per module, see round 2 below.)
- **`three_router_triangle` closes the ring (R3↔R1)** and `hub_spoke` wires R1→every spoke — the
  orchestrator honors the template shape (was a flat chain before). ⚠️ **`hub_spoke` is limited by the
  hub's port count**: a 2911 has 3 Gigabit ports, so it cannot serve 5 spokes plus its own LAN. Asking
  for more no longer fails silently — the plan comes back with `TOPOLOGY_DISCONNECTED`. Pick a router
  with more ports, add a module, or use `multi_lan` (a chain needs only 3 ports per router).
- **`pt://capabilities` is derived from the live tool registry** (`supported_live.nat/acl/modules/…`) — it
  can no longer drift; trust it *and* the tools.
- **An operation reported as executed is not proof** - host IPs may lag a few seconds.
- **Invalid `routing=` raises a raw exception**; invalid `router_model` returns a degenerate plan with an
  embedded `errors[]` — always check `plan.errors` before deploying.
- A harmless phantom `Power Distribution Device` can appear after deploy (off-canvas).

### ☠️ Never iterate a native PT object with `for...in`

```js
var d = lw.getEllipseItemData(id);
for (var k in d) { ... }   // ← CRASHES Packet Tracer. The whole app dies.
```

`try/catch` does **not** save you: this is not a JS exception, it is the script engine
blowing up and taking the process with it. Verified the hard way against PT 9.0.1 — the
app closed with "Cisco Packet Tracer quit unexpectedly".

Inspect native objects by calling **known getters** and stringifying the result, never by
enumerating keys. If you don't know the shape, probe one accessor at a time.

### Drawing IS possible — `drawCircle` takes SEVEN arguments

```js
lw.drawCircle(x, y, ignored, diameter, 0, 0, 0)   // x,y = TOP-LEFT corner
```

Returns an ellipse id and really draws. With 3–6 arguments it throws, which is why it was
believed unusable — same failure mode as `setHideDevLabel` (needs 2, not 1): the call was
fine, the **arity** was wrong. The old note that "the size argument controls stacking order"
came from passing the size in slot 3; **slot 4 is the diameter** and slot 3 is ignored.

Calibrated on PT 9.0.1 at default zoom: the drawn diameter is roughly **0.71 ×** the argument,
so to circle a LAN centred at `cx` use `drawCircle(cx - 173, y, 0, 480, 0,0,0)`. Don't compute
it blind - draw, capture a screenshot, adjust.
drawings too, so redraw circles after clearing notes).

Colours still don't work: the last three arguments accept values but the ellipse comes out
with the default outline.

### Verified against PT 9.0.1 (audit round 2)

- **The bridge starts lazily.** After the MCP server restarts (a `/mcp` reconnect), nothing is
  listening on `:54321` until the first `pt_*` call — importing the server deliberately opens no
  socket. So "not connected" right after a reconnect usually means *nobody has called a tool yet*,
  **not** that the extension died: just call `pt_status` and PT resumes polling in seconds.
  Don't ask the user to reopen MCP BUILDER before trying that.
- **`getClassName()` is useless for telling a switch from a router.** PT classifies by behaviour:
  a 3560 answers `"Router"` (it is multilayer) and a 2960 answers `"CiscoDevice"` — neither ever
  says "switch". Use `getModel()` and resolve it against the catalog.
- **`addModule()` returns `false` instead of throwing** when the slot does not exist on that model.
  Check the return value; a silent `false` used to be reported as a successful install. A module
  install now verifies it and reports `installed` / `failed` per module.
- **Renaming to a name that is already taken used to be allowed** and left two devices sharing it,
  with `getDevice(name)` resolving only to one — the other became unreachable by name. Now rejected.

### Verified against PT 9.0.1 (audit round 3)

- **A router deployed by the MCP has never been touched through its console**, so it is still parked at
  `Would you like to enter the initial configuration dialog? [yes/no]:`. A `ping` sent there is eaten as
  the yes/no answer and never runs. Prime it first: send `no`, then an empty command to clear
  `Press RETURN to get started.` (the flow test now does this for you.)
- **An IOS interface with no IP address stays shut down.** The CLI generator only emits `no shutdown`
  for addressed interfaces, so a link you cabled but never addressed shows up as a red triangle on the
  canvas and as a down link in `net_verify`. Give it an address, even a throwaway `/30`.
- **PT does not associate a wireless host with the nearest AP.** Measured: a laptop with its own LAN's
  AP right beside it did a *fresh* association and a *fresh* DHCP and still chose the AP of another LAN,
  taking that LAN's address. Between APs sharing the default SSID the choice is arbitrary, and there is
  no SSID API to force it (neither the AP nor its port has `setSsid`). If you need deterministic
  addressing, use `wireless_laptops=False` and cable the laptops.
- **`PT_MCP_BRIDGE_TOKEN` is validated.** If the server refuses to start with `BridgeTokenError`, the
  variable is set to something shorter than 32 chars or outside `[A-Za-z0-9_-]`. Fix it or unset it so
  the on-disk token is used. It fails loudly on purpose: a bad value there would disable the only real
  defence the bridge has.
- **The layout adapts to the busiest LAN.** Coordinates no longer go negative and LAN clusters no longer
  overlap, so you do not need to reposition devices by hand after `net_build` - only if you want a
  specific arrangement.

## Recipes

- *"2 routers, 2 switches, 4 PCs, DHCP, static"* -> a spec: 2 routers, a switch each, 4 PCs,
  pcs_per_lan=2, dhcp=True, routing="static", deploy=True)`.
- 4 serial ports on R1 (2911) -> a module op `{device:"R1", slot:"0/0", module:"HWIC-2T"}`.
  {device:"R1",slot:"0/1",module:"HWIC-2T"}])`.
- Block telnet to a server on R1 -> a `policies` block in the spec on R1:
     rules: [{action:deny, protocol:tcp, source:any, destination:"192.168.0.10", dest_port:23},
            {action:permit, protocol:ip, source:any, destination:any}]
  binding_interface="GigabitEthernet0/0", binding_direction="in", dry_run=True)` — preview first.
- Read a device's ports safely with a guarded expression, e.g. `try{var d=ipc.network().getDevice("R1");reportResult(d?d.getPorts().join(","):"missing")}catch(e){reportResult("ERR:"+e)}`.
