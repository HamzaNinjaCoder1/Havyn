# Quick Start

Once your [MCP client is connected](installation.md), just talk to your AI.

## Example prompt

> *"Build a network with 2 routers, 2 switches, 4 PCs, DHCP and static routing."*

The LLM calls `net_build`, which runs the whole pipeline - plan, validate,
generate → explain → estimate — and returns a complete summary:

```text
Devices (8):  R1, R2 (2911), SW1, SW2 (2960-24TT), PC1..PC4 (PC-PT)
Links   (7):  R1<->R2 (cross), R1<->SW1 (straight), R2<->SW2 (straight),
              SW1<->PC1, SW1<->PC2, SW2<->PC3, SW2<->PC4 (straight)

IP Plan:
  LAN 1:  192.168.0.0/24 -- R1 Gig0/1: .1, PC1: .2, PC2: .3
  LAN 2:  192.168.1.0/24 -- R2 Gig0/1: .1, PC3: .2, PC4: .3
  Link:   10.0.0.0/30    -- R1 Gig0/0: .1, R2 Gig0/0: .2

DHCP:   Pools on R1 and R2
Routes: Bidirectional static routes
```

## The recommended flow

For a **new topology**:

```text
net_spec_validate  ->  net_plan  ->  net_preflight  ->  net_build
```

…or the one-shot shortcut:

```text
python build_havyn.py my-network.spec.json autonomous
```

To **edit a topology already open in PT**:

```text
pt_status  ->  net_observe  ->  net_diff  ->  net_build
                                        pt_rename_device / pt_move_device /
                                        pt_delete_link / pt_delete_device
```

To **check that it actually works** after deploying:

```text
net_verify              # real pings, per-criterion verdicts
net_diff                 # spec vs observed
build.human_text()       # functional-layer report
```

!!! tip "Preflight before you build"
    `net_preflight` refuses a bad port, cable, module or address **by name**,
    so the engine uses **real** model names, ports and cables
    from the catalog instead of guessing. See the [Tool reference](tools.md).

## Three ways to get a topology into Packet Tracer

1. **`net_build`** - compiles the spec, gates it, and executes it against a
   PT via the bridge. See [Live Deploy Setup](live-deploy.md). *(Best experience.)*
2. **`dry_run` first** - `net_build` with `safety_mode="dry_run"` prints the whole
   clipboard; paste it into PT's Builder Code Editor and click Run.
3. **Export to disk** (`pt_export`) — writes the PTBuilder script, per-device IOS
   configs and the plan JSON to `projects/<name>/` for reuse.

Ready for real-time? Continue to **[Live Deploy Setup](live-deploy.md)**.
