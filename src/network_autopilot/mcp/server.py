"""AI Network Autopilot MCP server.

Runs as its own stdio server (`python -m network_autopilot`), independent of
the upstream `packet_tracer_mcp` server. The host LLM does the natural-language
understanding; everything this server exposes enforces the typed, deterministic
discipline of the engine.

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
    adapter: str, safety_mode: str, evidence_dir: str
) -> tuple[Autopilot | None, dict | None]:
    """Build the engine facade; adapter='bridge' requires no live check here —
    callers report channel status honestly from ping()."""
    mode = SafetyMode(safety_mode)
    gate = SafetyGate(mode, MutationBudget())
    evidence = EvidenceStore(root=Path(evidence_dir))
    if adapter == "null":
        return Autopilot(NullAdapter(), gate, evidence), None
    if adapter == "bridge":
        from ..adapter.upstream.bridge_adapter import BridgeAdapter

        bridge = BridgeAdapter(host_bridge=True, evidence=evidence)
        return Autopilot(bridge, gate, evidence), None
    return None, {"error": f"unknown adapter {adapter!r}: use 'null' or 'bridge'"}


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
        from ..adapter.upstream.bridge_adapter import BridgeAdapter

        adapter = BridgeAdapter(host_bridge=False)  # pure client: a status tool binds nothing
        probe = adapter.ping(timeout=2.0)
        info["bridge"] = {
            "adapter": probe.adapter_name,
            "reachable": probe.reachable,
            "detail": probe.detail.value if probe.detail.is_known else probe.detail.status.value,
        }
    except Exception as exc:  # status must never crash the session
        info["bridge"] = {
            "adapter": "upstream-bridge",
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
def net_observe(names_json: str = "[]", adapter: str = "bridge") -> dict:
    """Observe the CURRENT workspace state through the adapter.

    Returns the ActualNetworkState with every field's observability status —
    KNOWN values carry evidence; UNKNOWN/NOT_OBSERVED fields say why. With the
    'bridge' adapter this is real Packet Tracer state; if no live channel
    exists the response says so instead of returning invented state.
    """
    names = json.loads(names_json) if names_json else []
    if adapter == "bridge":
        from ..adapter.upstream.bridge_adapter import BridgeAdapter

        bridge = BridgeAdapter(host_bridge=False)
        probe = bridge.ping(timeout=3.0)
        if not probe.reachable:
            return {
                "status": "BLOCKED",
                "detail": probe.detail.value or probe.detail.status.value,
            }
        state = bridge.observe_state(names or None)
    elif adapter == "null":
        state = NullAdapter().observe_state(names or None)
    else:
        return {"error": f"unknown adapter {adapter!r}: use 'null' or 'bridge'"}
    return state.to_dict()


@mcp.tool()
def net_verify(spec_json: str, adapter: str = "bridge") -> dict:
    """Verify the CURRENT workspace against the spec — without mutating.

    Structural, addressing and connectivity verification against a fresh
    observation sweep. UNKNOWN never becomes PASS: a test without evidence
    says UNKNOWN. adapter='bridge' needs the live channel and says BLOCKED
    otherwise.
    """
    spec, err = _parse_spec(spec_json)
    if err:
        return err
    autopilot, aerr = _make_autopilot(adapter, "read_only", _DEFAULT_EVIDENCE_DIR)
    if aerr:
        return aerr
    if adapter == "bridge":
        probe = autopilot.adapter.ping(timeout=3.0)
        if not probe.reachable:
            return {
                "status": "BLOCKED",
                "detail": probe.detail.value or probe.detail.status.value,
            }
    report, _diff, error = autopilot.inspect(spec)
    if error:
        return {"status": "FAILED", "error": error}
    return report.to_dict()  # type: ignore[union-attr]


@mcp.tool()
def net_diff(spec_json: str, adapter: str = "bridge") -> dict:
    """Semantic diff: expected (spec+IPAM) vs actual (observed workspace).

    MATCH/ADD/REMOVE/CHANGE/UNKNOWN per field. 'not observed' is never
    silently equated with 'missing'. The drift-check path: does the workspace
    still match the design?
    """
    spec, err = _parse_spec(spec_json)
    if err:
        return err
    autopilot, aerr = _make_autopilot(adapter, "read_only", _DEFAULT_EVIDENCE_DIR)
    if aerr:
        return aerr
    if adapter == "bridge":
        probe = autopilot.adapter.ping(timeout=3.0)
        if not probe.reachable:
            return {
                "status": "BLOCKED",
                "detail": probe.detail.value or probe.detail.status.value,
            }
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
) -> dict:
    """Run the full autopilot pipeline: plan -> compile -> execute -> verify.

    safety_mode: 'dry_run' (default; plans everything, mutates nothing),
    'read_only', 'approval_required' (waits), or 'autonomous' (executes within
    the mutation budget). adapter='bridge' drives the REAL Packet Tracer and
    reports BLOCKED with the channel status when none is reachable.

    The response carries the full machine-readable build report and the
    human-readable one; every PASS in it is backed by recorded evidence.
    """
    spec, err = _parse_spec(spec_json)
    if err:
        return err
    autopilot, aerr = _make_autopilot(adapter, safety_mode, evidence_dir)
    if aerr:
        return aerr
    if adapter == "bridge":
        probe = autopilot.adapter.ping(timeout=3.0)
        if not probe.reachable:
            return {
                "status": "BLOCKED",
                "detail": probe.detail.value or probe.detail.status.value,
            }
    build, error = autopilot.build(spec)
    if error:
        return {"status": "FAILED", "error": error}
    return {  # type: ignore[union-attr]
        "report": build.to_dict(),
        "human_text": build.human_text(),
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
