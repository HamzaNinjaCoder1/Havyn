"""AI Network Autopilot MCP server.

Runs as its own stdio server (`python -m network_autopilot`). It is
self-contained: the engine, its catalogs and both runtime transports
(`network_autopilot.pt`) are this project's own, so nothing here imports
anything beyond the standard library, `mcp` and `pydantic`. The host LLM
does the natural-language understanding; everything this server exposes
enforces the typed, deterministic discipline of the engine.

Handlers stay thin: every tool delegates to the engine (IPAM, compiler,
orchestrator, verification). The LLM never writes operations by hand — it
writes a NetworkSpec, and the deterministic pipeline does the rest.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .. import __version__
from ..adapter import NullAdapter
from ..compiler.compiler import compile_spec
from ..core.network_spec import NetworkSpec
from ..core.safety import MutationBudget, SafetyGate, SafetyMode
from ..ipam.planner import plan_addressing
from ..ipam.validator import has_errors, validate_plan
from ..orchestrator import Autopilot
from ..evidence.store import EvidenceStore

mcp = FastMCP("network-autopilot")

_DEFAULT_EVIDENCE_DIR = "autopilot_evidence"


def _parse_spec(spec_json: str) -> tuple[NetworkSpec | None, dict | None]:
    try:
        return NetworkSpec.from_json(spec_json), None
    except ValueError as exc:
        return None, {"valid": False, "error": str(exc)}


def _make_autopilot(
    adapter: str, safety_mode: str, evidence_dir: str, auto_launch: bool = False,
) -> tuple[Autopilot | None, dict | None]:
    """Build the engine facade; adapter='bridge' requires no live check here —
    callers report channel status honestly from ping()."""
    mode = SafetyMode(safety_mode)
    gate = SafetyGate(mode, MutationBudget())
    evidence = EvidenceStore(root=Path(evidence_dir))
    if adapter == "null":
        return Autopilot(NullAdapter(), gate, evidence), None
    if adapter == "bridge":
        from ..adapter.packet_tracer.bridge_adapter import BridgeAdapter

        bridge = BridgeAdapter(host_bridge=True, evidence=evidence,
                               auto_launch=auto_launch)
        return Autopilot(bridge, gate, evidence), None
    return None, {"error": f"unknown adapter {adapter!r}: use 'null' or 'bridge'"}


def _live_or_blocked(adapter_obj, auto_launch: bool):
    """Probe the live channel, launching PT on demand when allowed.

    Returns None when a channel is usable (the caller proceeds), else a BLOCKED
    dict the caller returns verbatim. auto_launch defaults to False everywhere:
    a closed PT stays a hard BLOCKED rather than silently opening a GUI. When a
    launch is attempted and still fails, the receipt says exactly why.
    """
    probe = adapter_obj.ping(timeout=3.0)
    if probe.reachable:
        return None
    if not auto_launch:
        return {
            "status": "BLOCKED",
            "detail": probe.detail.value or probe.detail.status.value,
        }
    launch = adapter_obj.launch()
    if launch.usable:
        return None
    return {
        "status": "BLOCKED",
        "detail": (
            f"Packet Tracer auto-launch did not produce a live channel "
            f"({launch.status}: {launch.detail})"
        ),
        "launch": {
            "status": launch.status,
            "exe": launch.exe,
            "elapsed_s": round(launch.elapsed_s, 2),
            "detail": launch.detail,
        },
    }


def _plan(spec: NetworkSpec) -> tuple[Any | None, dict | None]:
    try:
        plan = plan_addressing(spec)
    except Exception as exc:
        return None, {"error": f"addressing failed: {exc}"}
    issues = validate_plan(plan, spec)
    return plan, {
        "issues": [i.to_dict() for i in issues],
        "has_errors": has_errors(issues),
    }


@mcp.tool()
def autopilot_status() -> dict:
    """Report engine version, safety modes and live bridge availability.

    Bridge reachability here is an observation, not a promise: the detail
    string says exactly what was (or was not) seen.
    """
    budget = MutationBudget()
    info: dict = {
        "autopilot_version": __version__,
        "safety_modes": [mode.value for mode in SafetyMode],
        "default_mutation_budget": {
            "max_mutations": budget.max_mutations,
            "max_repair_attempts": budget.max_repair_attempts,
            "max_repair_mutations": budget.max_repair_mutations,
        },
    }
    try:
        from ..adapter.packet_tracer.bridge_adapter import BridgeAdapter

        adapter = BridgeAdapter(host_bridge=False)  # pure client: a status tool binds nothing
        probe = adapter.ping(timeout=2.0)
        info["bridge"] = {
            "adapter": probe.adapter_name,
            "reachable": probe.reachable,
            "detail": probe.detail.value if probe.detail.is_known else probe.detail.status.value,
        }
    except Exception as exc:  # status must never crash the session
        info["bridge"] = {
            "adapter": "packet-tracer-bridge",
            "reachable": False,
            "detail": f"unavailable: {exc}",
        }
    return info


@mcp.tool()
def net_spec_validate(spec_json: str) -> dict:
    """Typed validation of a NetworkSpec JSON document.

    This is the gate between LLM output and the engine: free-form or
    under-specified documents are rejected here with the exact reason, and
    valid ones get their content hash (the build-fingerprint seed).
    """
    try:
        spec = NetworkSpec.from_json(spec_json)
    except ValueError as exc:
        return {"valid": False, "error": str(exc)}
    return {
        "valid": True,
        "name": spec.name,
        "spec_hash": spec.spec_hash(),
        "counts": {
            "requirements": len(spec.requirements),
            "acceptance_criteria": len(spec.acceptance_criteria),
            "devices": len(spec.devices),
            "links": len(spec.links),
            "vlans": len(spec.vlans),
            "subnets": len(spec.subnets),
            "ip_assignments": len(spec.ip_assignments),
            "policies": len(spec.policies),
        },
        "requirements": [r.id for r in spec.requirements],
        "acceptance_criteria": [c.id for c in spec.acceptance_criteria],
    }


@mcp.tool()
def net_plan(spec_json: str) -> dict:
    """Deterministic IPAM: size and allocate every subnet, then validate.

    The LLM never does subnet arithmetic. Returns the full AddressPlan
    (subnets, gateways, ranges, per-device assignments) plus every validation
    issue with severity — errors are errors, not warnings.
    """
    spec, err = _parse_spec(spec_json)
    if err:
        return err
    plan, report = _plan(spec)
    if plan is None:
        return report or {"error": "planning failed"}
    return {
        "spec_hash": spec.spec_hash(),
        "plan": plan.to_dict(),
        "validation": report,
    }


@mcp.tool()
def net_preflight(spec_json: str) -> dict:
    """Everything that can be checked BEFORE touching Packet Tracer.

    Typed validation -> IPAM -> plan validation -> compilation. Compilation
    refuses out-of-scope features BY NAME, so the response lists exactly which
    requested features are unsupported. No mutation happens here.
    """
    spec, err = _parse_spec(spec_json)
    if err:
        return err
    plan, report = _plan(spec)
    if plan is None:
        return report or {"error": "planning failed"}
    if report and report["has_errors"]:
        return {"preflight": "FAILED", "validation": report}
    try:
        compiled = compile_spec(spec, plan, packet_tracer_version="unknown")
    except Exception as exc:
        return {"preflight": "FAILED", "compile_error": str(exc)}
    return {
        "preflight": "PASS" if not compiled.unsupported_features else "PASS_WITH_GAPS",
        "fingerprint": compiled.fingerprint,
        "unsupported_features": compiled.unsupported_features,
        "notes": compiled.notes,
        "operation_count": len(compiled.dag.nodes),
    }


@mcp.tool()
def net_compile(spec_json: str) -> dict:
    """Compile a NetworkSpec into the deterministic operation DAG.

    Shows every operation the executor will run, with dependencies — the
    artifact the safety gate and executor consume. Pure function of
    (spec, plan): the same input always produces the same DAG.
    """
    spec, err = _parse_spec(spec_json)
    if err:
        return err
    plan, report = _plan(spec)
    if plan is None:
        return report or {"error": "planning failed"}
    if report and report["has_errors"]:
        return {"error": "addressing validation failed", "validation": report}
    try:
        compiled = compile_spec(spec, plan, packet_tracer_version="unknown")
    except Exception as exc:
        return {"error": f"compilation refused: {exc}"}
    ops = []
    for node in compiled.dag.topo_order():
        op = compiled.dag.nodes[node].operation
        ops.append(
            {
                "operation_id": op.operation_id,
                "type": op.operation_type,
                "dependencies": list(op.dependencies),
                "risk": op.risk.value,
            }
        )
    return {
        "fingerprint": compiled.fingerprint,
        "unsupported_features": compiled.unsupported_features,
        "notes": compiled.notes,
        "operations": ops,
    }


@mcp.tool()
def net_observe(
    names_json: str = "[]", adapter: str = "bridge", auto_launch: bool = False,
) -> dict:
    """Observe the CURRENT workspace state through the adapter.

    Returns the ActualNetworkState with every field's observability status —
    KNOWN values carry evidence; UNKNOWN/NOT_OBSERVED fields say why. With the
    'bridge' adapter this is real Packet Tracer state; if no live channel
    exists the response says so instead of returning invented state.

    auto_launch=True opens Packet Tracer itself when it is closed, instead of
    answering BLOCKED — the observation then proceeds against the live GUI.
    """
    names = json.loads(names_json) if names_json else []
    if adapter == "bridge":
        from ..adapter.packet_tracer.bridge_adapter import BridgeAdapter

        bridge = BridgeAdapter(host_bridge=False, auto_launch=auto_launch)
        blocked = _live_or_blocked(bridge, auto_launch)
        if blocked is not None:
            return blocked
        state = bridge.observe_state(names or None)
    elif adapter == "null":
        state = NullAdapter().observe_state(names or None)
    else:
        return {"error": f"unknown adapter {adapter!r}: use 'null' or 'bridge'"}
    return state.to_dict()


@mcp.tool()
def net_verify(spec_json: str, adapter: str = "bridge", auto_launch: bool = False) -> dict:
    """Verify the CURRENT workspace against the spec — without mutating.

    Structural, addressing and connectivity verification against a fresh
    observation sweep. UNKNOWN never becomes PASS: a test without evidence
    says UNKNOWN. adapter='bridge' needs the live channel and says BLOCKED
    otherwise, unless auto_launch=True opens Packet Tracer first.
    """
    spec, err = _parse_spec(spec_json)
    if err:
        return err
    autopilot, aerr = _make_autopilot(adapter, "read_only", _DEFAULT_EVIDENCE_DIR,
                                      auto_launch=auto_launch)
    if aerr:
        return aerr
    if adapter == "bridge":
        blocked = _live_or_blocked(autopilot.adapter, auto_launch)
        if blocked is not None:
            return blocked
    report, _diff, error = autopilot.inspect(spec)
    if error:
        return {"status": "FAILED", "error": error}
    return report.to_dict()  # type: ignore[union-attr]


@mcp.tool()
def net_diff(spec_json: str, adapter: str = "bridge", auto_launch: bool = False) -> dict:
    """Semantic diff: expected (spec+IPAM) vs actual (observed workspace).

    MATCH/ADD/REMOVE/CHANGE/UNKNOWN per field. 'not observed' is never
    silently equated with 'missing'. The drift-check path: does the workspace
    still match the design? auto_launch=True opens Packet Tracer when closed.
    """
    spec, err = _parse_spec(spec_json)
    if err:
        return err
    autopilot, aerr = _make_autopilot(adapter, "read_only", _DEFAULT_EVIDENCE_DIR,
                                      auto_launch=auto_launch)
    if aerr:
        return aerr
    if adapter == "bridge":
        blocked = _live_or_blocked(autopilot.adapter, auto_launch)
        if blocked is not None:
            return blocked
    _report, diff, error = autopilot.inspect(spec)
    if error:
        return {"status": "FAILED", "error": error}
    return diff.to_dict()


@mcp.tool()
def net_build(
    spec_json: str,
    adapter: str = "null",
    safety_mode: str = "dry_run",
    evidence_dir: str = _DEFAULT_EVIDENCE_DIR,
    auto_launch: bool = False,
) -> dict:
    """Run the full autopilot pipeline: plan -> compile -> execute -> verify.

    safety_mode: 'dry_run' (default; plans everything, mutates nothing),
    'read_only', 'approval_required' (waits), or 'autonomous' (executes within
    the mutation budget). adapter='bridge' drives the REAL Packet Tracer and
    reports BLOCKED with the channel status when none is reachable.

    auto_launch=True removes the last manual step: when Packet Tracer is
    closed, the tool opens it and waits (bounded) for the Script Engine
    heartbeat instead of answering BLOCKED. It never starts a GUI that is
    already running, and a launch that does not produce a heartbeat still
    reports BLOCKED with the reason.

    The response carries the full machine-readable build report and the
    human-readable one; every PASS in it is backed by recorded evidence.
    """
    spec, err = _parse_spec(spec_json)
    if err:
        return err
    autopilot, aerr = _make_autopilot(adapter, safety_mode, evidence_dir,
                                      auto_launch=auto_launch)
    if aerr:
        return aerr
    if adapter == "bridge":
        blocked = _live_or_blocked(autopilot.adapter, auto_launch)
        if blocked is not None:
            return blocked
    build, error = autopilot.build(spec)
    if error:
        return {"status": "FAILED", "error": error}
    return {  # type: ignore[union-attr]
        "report": build.to_dict(),
        "human_text": build.human_text(),
    }


@mcp.tool()
def associate_wireless_client(
    client: str,
    access_point: str,
    adapter: str = "null",
    auto_launch: bool = False,
    settle_seconds: float = 1.0,
    timeout_seconds: float = 20.0,
) -> dict:
    """Associate a wireless client with an access point, in real PT.

    Installs the client's wireless NIC if it does not have one (PT-LAPTOP-NM-1W
    and friends, replacing the wired NIC that occupies the module slot), lets
    Packet Tracer settle, and then reads the association back from PT itself:
    the antenna link on the client's radio must name this access point. The
    answer carries the observed evidence (link class, peer device/interface,
    port state) and a failure is reported as a failure - a client that merely
    has a Wireless0 has NOT associated.
    """
    from ..wireless import (
        WirelessAssociationError,
        associate_wireless_client as _associate,
        list_associations_for,
    )

    autopilot, aerr = _make_autopilot(adapter, "read_only", _DEFAULT_EVIDENCE_DIR,
                                      auto_launch=auto_launch)
    if aerr:
        return aerr
    if adapter == "bridge":
        blocked = _live_or_blocked(autopilot.adapter, auto_launch)
        if blocked is not None:
            return blocked
    try:
        association = _associate(
            autopilot.adapter, client, access_point,
            settle_seconds=settle_seconds, timeout_seconds=timeout_seconds,
        )
    except WirelessAssociationError as exc:
        return {
            "status": "FAILED",
            "associated": False,
            "error": str(exc),
            "associations": list_associations_for(autopilot.adapter),
        }
    return {
        "status": "OK",
        "associated": True,
        "association": association.to_dict(),
        "associations": list_associations_for(autopilot.adapter),
    }


# -- Packet Tracer lifecycle ------------------------------------------------


@mcp.tool()
def pt_launch(timeout_s: float = 120.0) -> dict:
    """Open Packet Tracer and wait until the Script Engine is heartbeating.

    Removes 'PT must already be open' from the loop: an agent calls this first
    and every bridge tool afterwards sees a live channel. Idempotent — an
    already-live PT is reported and nothing is spawned.

    Returns a receipt: status is 'alive' (nothing to do), 'launched' (opened
    and heartbeating), or a reason the channel is still unusable ('timeout',
    'spawn_failed', 'missing'). Never touches a PT API or the bridge token; it
    only spawns the documented executable and reads the existing heartbeat.
    """
    from ..adapter.packet_tracer.bridge_adapter import BridgeAdapter

    bridge = BridgeAdapter(host_bridge=False)  # client: binds no port
    result = bridge.launch(timeout=timeout_s)
    return {
        "status": result.status,
        "usable": result.usable,
        "spawned": result.spawned,
        "exe": result.exe,
        "elapsed_s": round(result.elapsed_s, 2),
        "detail": result.detail,
    }


@mcp.tool()
def pt_status() -> dict:
    """Is Packet Tracer installed and the Script Engine alive right now?

    Pure observation — opens nothing, sends no bridge command. The cheap
    pre-flight an agent runs to decide between proceeding and pt_launch().
    """
    from ..adapter.packet_tracer.bridge_adapter import BridgeAdapter
    from ..adapter.packet_tracer.pt_launcher import (
        find_packet_tracer,
        packet_tracer_running,
    )

    exe = find_packet_tracer()
    bridge = BridgeAdapter(host_bridge=False)
    probe = bridge.ping(timeout=3.0)
    return {
        "installed": exe is not None,
        "exe": str(exe) if exe is not None else None,
        "process_running": packet_tracer_running(),
        "bridge_reachable": probe.reachable,
        "detail": probe.detail.value or probe.detail.status.value,
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()


# -- Canvas auto-layout (newly authored for this project) --------------------
#
# The two tools below live here rather than in the upstream MCP registry: they
# are written for this engine and depend only on its own layout module and its
# adapter boundary.


def _layout_adapter(auto_launch: bool):
    from ..adapter.packet_tracer.bridge_adapter import BridgeAdapter

    bridge = BridgeAdapter(host_bridge=False, auto_launch=auto_launch)
    blocked = _live_or_blocked(bridge, auto_launch)
    if blocked is not None:
        return None, blocked
    return bridge, None


@mcp.tool()
def pt_auto_layout(mode: str = "full", auto_launch: bool = False) -> dict:
    """Intelligently arrange every device on the Packet Tracer canvas.

    Reads the current topology (devices + links), computes a clean
    hierarchical layout, and moves each device once. Guarantees:

    - No device overlaps or hides another, ever (rectangle collision model).
    - Devices align to a grid in role rows: WAN/cloud on top, routers,
      switches, access points and end hosts below.
    - Neighbours in the topology are drawn near each other, ordered to minimise
      crossing links; disjoint networks stay in separate blocks.

    mode:
    - "full" (default): recompute the whole layout from the topology.
    - "overlap": the smallest change possible — only devices actually stacked
      on top of another device move, everything else stays put.

    Needs a live Packet Tracer channel and reports BLOCKED otherwise, unless
    auto_launch=True opens Packet Tracer first.
    """
    from ..pt import layout as auto_layout
    from ..pt.live_layout import apply_moves, snapshot_topology

    mode_norm = (mode or "full").strip().lower()
    if mode_norm not in ("full", "overlap"):
        return {"error": 'mode must be "full" or "overlap".'}

    bridge, blocked = _layout_adapter(auto_launch)
    if bridge is None:
        return blocked

    snapshot = snapshot_topology(bridge)
    if snapshot is None:
        return {"error": "could not read the current topology from PT"}
    devices, links = snapshot
    if not devices:
        return {"status": "OK", "detail": "the canvas has no devices to arrange"}

    old_pos = {name: (int(x), int(y)) for name, _, x, y in devices}
    cats = {name: cat for name, cat, _, _ in devices}

    if mode_norm == "full":
        target = auto_layout.compute_layout(
            [(name, cat) for name, cat, _, _ in devices], links,
        ).positions
    else:
        target = auto_layout.resolve_overlaps(
            {name: (x, y) for name, _, x, y in devices}, cats,
        )

    moves = [
        (name, int(nx), int(ny))
        for name, (nx, ny) in target.items()
        if name in old_pos and old_pos[name] != (nx, ny)
    ]
    if not moves:
        return {
            "status": "OK",
            "detail": f"nothing to move — {len(devices)} device(s) already tidy",
        }

    applied, skipped = apply_moves(bridge, moves)
    return {
        "status": "OK",
        "mode": mode_norm,
        "moved": applied,
        "skipped": skipped,
        "moves": [
            {"device": n, "from": list(old_pos[n]), "to": [int(x), int(y)]}
            for n, x, y in moves
        ],
    }


@mcp.tool()
def pt_find_free_position(
    category: str, want_x: int = 0, want_y: int = 0, auto_launch: bool = False,
) -> dict:
    """Ask the layout engine where a new device can go without landing on an
    existing one.

    Parameters:
    - category: device category (router, switch, pc, server, laptop,
      accesspoint, cloud, ...) or a model name (2911, 2960-24TT).
    - want_x / want_y: preferred point; the engine returns the free grid slot
      closest to it.

    Needs a live channel; reports BLOCKED when PT is unreachable unless
    auto_launch=True opens it first.
    """
    from ..pt import layout as auto_layout
    from ..pt.live_layout import snapshot_topology

    bridge, blocked = _layout_adapter(auto_launch)
    if bridge is None:
        return blocked

    snapshot = snapshot_topology(bridge)
    if snapshot is None:
        return {"error": "could not read the current topology from PT"}
    devices, _links = snapshot

    x, y = auto_layout.find_free_position(
        devices, category, want_x=want_x or None, want_y=want_y or None,
    )
    return {
        "status": "OK",
        "category": category,
        "position": [int(x), int(y)],
        "devices_on_canvas": len(devices),
    }
