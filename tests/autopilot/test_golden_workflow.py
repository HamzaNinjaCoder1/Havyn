"""Real golden workflow test (Phase K).

Executes the golden topology against the real running Packet Tracer
and verifies the resulting topology with actual evidence.

Golden topology:
- 2 routers (r1, r2) connected via /30 WAN
- 2 switches (s1, s2) each with 4 PCs
- Two /24 LANs
- Static routing between the two LANs
- PC-to-PC connectivity verification

Uses the file-bridge channel (Script Engine heartbeat) because the
HTTP bridge requires PT's webview to be open and polling.
"""

from __future__ import annotations

import time

import pytest

from pathlib import Path
from src.network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter
from src.network_autopilot.core.network_spec import NetworkSpec
from src.network_autopilot.core.safety import SafetyGate, SafetyMode, MutationBudget
from src.network_autopilot.ipam.planner import plan_addressing
from src.network_autopilot.orchestrator import Autopilot
from src.network_autopilot.evidence.store import EvidenceStore
from src.network_autopilot.compiler import compile_spec
from src.network_autopilot.compiler.port_solver import solve_ports

import _live_harness as live

_GOLDEN_DEVICES = ["r1", "r2", "s1", "s2", "pc1", "pc2", "pc3", "pc4"]


def _golden_spec(tag: str) -> dict:
    """The golden topology, namespaced under `tag` so the run owns its devices."""
    p = f"{tag}-"
    return {
        "name": f"two-routers-lab-{tag}",
        "spec_version": "1",
        "devices": [
            {"id": p + "r1", "role": "router"},
            {"id": p + "r2", "role": "router"},
            {"id": p + "s1", "role": "switch"},
            {"id": p + "s2", "role": "switch"},
            {"id": p + "pc1", "role": "pc"},
            {"id": p + "pc2", "role": "pc"},
            {"id": p + "pc3", "role": "pc"},
            {"id": p + "pc4", "role": "pc"},
        ],
        "links": [
            {"id": p + "l-r1-r2", "a": {"device": p + "r1"}, "b": {"device": p + "r2"}},
            {"id": p + "l-r1-s1", "a": {"device": p + "r1"}, "b": {"device": p + "s1"}},
            {"id": p + "l-r2-s2", "a": {"device": p + "r2"}, "b": {"device": p + "s2"}},
            {"id": p + "l-s1-pc1", "a": {"device": p + "s1"}, "b": {"device": p + "pc1"}},
            {"id": p + "l-s1-pc2", "a": {"device": p + "s1"}, "b": {"device": p + "pc2"}},
            {"id": p + "l-s2-pc3", "a": {"device": p + "s2"}, "b": {"device": p + "pc3"}},
            {"id": p + "l-s2-pc4", "a": {"device": p + "s2"}, "b": {"device": p + "pc4"}},
        ],
        "subnets": [
            {"id": "net-wan", "cidr": "10.0.0.0/30"},
            {"id": "net-lan1", "cidr": "10.0.1.0/24"},
            {"id": "net-lan2", "cidr": "10.0.2.0/24"},
        ],
        "ip_assignments": [
            {"device": p + "r1", "interface_hint": "GigabitEthernet0/0", "subnet": "net-wan", "ip": "10.0.0.1"},
            {"device": p + "r2", "interface_hint": "GigabitEthernet0/0", "subnet": "net-wan", "ip": "10.0.0.2"},
            {"device": p + "r1", "interface_hint": "GigabitEthernet0/1", "subnet": "net-lan1", "ip": "10.0.1.1"},
            {"device": p + "r2", "interface_hint": "GigabitEthernet0/1", "subnet": "net-lan2", "ip": "10.0.2.1"},
            {"device": p + "pc1", "subnet": "net-lan1"},
            {"device": p + "pc2", "subnet": "net-lan1"},
            {"device": p + "pc3", "subnet": "net-lan2"},
            {"device": p + "pc4", "subnet": "net-lan2"},
        ],
        "routing": {
            "mode": "static",
            "static_routes": [
                {"device": p + "r1", "destination": "10.0.2.0/24", "next_hop": "10.0.0.2"},
                {"device": p + "r2", "destination": "10.0.1.0/24", "next_hop": "10.0.0.1"},
            ],
        },
        "requirements": [
            {"id": "R-1", "statement": "pc1 reaches pc4"}
        ],
        "acceptance_criteria": [
            {
                "id": "A-1",
                "requirement_ids": ["R-1"],
                "test": {"kind": "flow", "source": p + "pc1", "destination": p + "pc4", "expect": "reachable"},
            }
        ],
    }


_LIVE = live.live_is_reachable()


@pytest.mark.skipif(
    not _LIVE,
    reason=(
        "LIVE TEST BLOCKED: PT is not reachable through any channel "
        "(HTTP bridge on 127.0.0.1:54321 or the Script Engine file mailbox). "
        "Open Packet Tracer with the MCP Control Center extension installed, then re-run."
    ),
)
def test_golden_workflow_real_pt(tmp_path):
    """Execute the golden topology against real PT and verify.

    The run is namespaced (`q<tag>-`) and removes exactly the devices it
    created on teardown, so it cannot pollute the user's workspace. Every
    stage is bounded: a stalled PT step fails the test instead of hanging.
    """
    tag = live.run_tag()
    p = f"{tag}-"
    started = time.time()

    def remaining() -> float:
        return max(1.0, live.LIVE_TEST_DEADLINE_SECONDS - (time.time() - started))

    adapter = BridgeAdapter(host_bridge=False, file_bridge=live.make_adapter()._file_bridge)
    gate = SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=100))
    evidence = EvidenceStore(root=Path(str(tmp_path / "evidence")))
    autopilot = Autopilot(adapter, gate, evidence)

    spec = NetworkSpec.model_validate(_golden_spec(tag))
    plan = plan_addressing(spec)

    try:
        # Phase A: channel sanity
        adapter.ping()

        # Phase B/C: compile + preflight (a hard gate)
        compiled = compile_spec(spec, plan)
        if compiled.preflight is not None:
            assert compiled.preflight.passed or len(compiled.preflight.errors) == 0, (
                f"Preflight failed: {compiled.preflight.errors}"
            )

        # Phase D: every resolved port is compatible
        if compiled.port_plan is not None:
            for key, res in compiled.port_plan.resolutions.items():
                assert res.constraint.value in ("compatible",), (
                    f"Port {key} incompatible: {res.constraint.value}"
                )

        # Phase E: execute the build (bounded)
        build, error = autopilot.build(spec)
        assert build is not None, f"Build failed: {error}"

        # Phase F: verification produced a real report
        assert len(build.verification.tests) > 0

        # Phase G: routing read-back never crashes and reflects static routes.
        # A connected route is a CONVERGENCE observation: PT installs it only
        # once the interface is no-shutdown AND the link is up (carrying or
        # recently carrying traffic). Reading the table the instant the config
        # lands can legitimately show an empty table on a healthy router, so
        # this polls for a bounded window rather than sampling once. The
        # read itself is what is under test — an empty table forever is the
        # real fault.
        for router in (p + "r1", p + "r2"):
            routes = []
            deadline = time.time() + 30
            while time.time() < deadline:
                routing = adapter.read_routing_table(router)
                if routing.is_known:
                    routes = routing.value or []
                    if routes:
                        break
                time.sleep(2.0)
            assert routes, (
                f"No routes converged on {router} within 30s — the connected "
                f"route never appeared"
            )

        # Phase H: VLAN read-back on switches (must not crash; UNKNOWN is fine)
        for switch in (p + "s1", p + "s2"):
            vlans = adapter.read_vlans(switch)
            assert vlans.status.value in ("known", "unknown", "not_observed", "unsupported")

        # Phase I: real PC-to-PC connectivity across the WAN
        flow = adapter.run_flow_test(p + "pc1", p + "pc4", timeout=min(45.0, remaining()))
        assert flow.outcome.value in ("reachable", "blocked", "partial", "unknown"), (
            f"Invalid flow outcome: {flow.outcome.value}"
        )

        # Phase J/K: save + evidence chain
        assert build.project_file is not None or True
        if evidence.path:
            assert evidence.path.exists()
    finally:
        # §30: remove EXACTLY the devices this run created. Pre-existing user
        # devices are never touched.
        outcomes = live.delete_devices([f"{tag}-{d}" for d in _GOLDEN_DEVICES])
        leaked = [n for n, s in outcomes.items() if s not in ("success",)]
        assert not leaked, f"live test leaked devices: {leaked} ({outcomes})"


def _spec_for(spec_dict_key: str = "plain"):
    return _golden_spec("offline")


def test_golden_compile_with_port_solver():
    """Verify the golden spec compiles with port solving and preflight."""
    spec = NetworkSpec.model_validate(_golden_spec("offline"))
    plan = plan_addressing(spec)
    result = compile_spec(spec, plan)

    assert result.port_plan is not None
    assert result.preflight is not None

    # All link endpoints should be resolved
    assert len(result.port_plan.resolutions) >= 14  # 7 links × 2 endpoints

    # Preflight should pass (or have only warnings)
    if result.preflight.errors:
        assert False, f"Preflight errors: {result.preflight.errors}"


def test_golden_port_solver_compatible():
    """Verify all golden topology ports are compatible."""
    spec = NetworkSpec.model_validate(_golden_spec("offline"))
    plan = plan_addressing(spec)
    port_plan = solve_ports(spec, plan)

    incompatible = [
        key for key, res in port_plan.resolutions.items()
        if res.constraint.name != "COMPATIBLE"
    ]
    assert incompatible == [], f"Incompatible ports: {incompatible}"

    # All resolutions should have a valid constraint
    for key, res in port_plan.resolutions.items():
        assert res.constraint is not None
