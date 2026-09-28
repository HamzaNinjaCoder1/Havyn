"""S1.2 live: the engine's own pings must PASS on a small routed lab.

Blueprint done for S1.2: "Engine khud ping chala ke 5/5 runs me PASS likhta
hai." The offline tests prove the cascade fix (F-2) and the repair merge
(F-1) on fakes; this drives the real probe path over the live channel: two
broadcast domains joined by a router, static addressing (so no DHCP wait
noise), and two flow criteria the engine must prove itself.

Deliberately NOT a VLAN-trunk lab: the trunk read-back is a known-broken
criterion shape (flows-unknown.md §6 / F-4, fixed by S1.7), and a live test
for the flow fix must not stand on ground the blueprint has already marked
broken. Two subnets = two broadcast domains = the same cross-domain ping
path a 2-VLAN lab would prove.

Teardown starts from the spec's device ids (both raw and namespaced forms)
and proves nothing leaked.
"""

from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "tests/autopilot")
import _live_harness as live

from src.network_autopilot.core.network_spec import NetworkSpec
from src.network_autopilot.core.requirements import Verdict
from src.network_autopilot.core.safety import MutationBudget, SafetyGate, SafetyMode
from src.network_autopilot.evidence.store import EvidenceStore
from src.network_autopilot.orchestrator import Autopilot

pytestmark = pytest.mark.skipif(
    not live.live_is_reachable(),
    reason=(
        "LIVE TEST BLOCKED: Packet Tracer is not reachable on either channel "
        "(HTTP bridge on 127.0.0.1:54321, or the Script Engine file mailbox). "
        "Open PT with the MCP Control Center extension and re-run."
    ),
)

_SPEC_IDS = ("r1", "pc1", "pc2")


def _lab_spec(tag: str) -> NetworkSpec:
    r1, pc1, pc2 = (f"{tag}-{i}" for i in _SPEC_IDS)
    net_a, net_b = f"{tag}-net-a", f"{tag}-net-b"
    return NetworkSpec.model_validate(
        {
            "name": f"{tag}-flows-lab",
            "devices": [
                {"id": r1, "role": "router"},
                {"id": pc1, "role": "pc"},
                {"id": pc2, "role": "pc"},
            ],
            "links": [
                {
                    "id": f"{tag}-l-a",
                    "a": {"device": r1, "interface_hint": "GigabitEthernet0/0"},
                    "b": {"device": pc1, "interface_hint": "FastEthernet0"},
                },
                {
                    "id": f"{tag}-l-b",
                    "a": {"device": r1, "interface_hint": "GigabitEthernet0/1"},
                    "b": {"device": pc2, "interface_hint": "FastEthernet0"},
                },
            ],
            "subnets": [
                {"id": net_a, "cidr": "10.61.10.0/24"},
                {"id": net_b, "cidr": "10.61.20.0/24"},
            ],
            "ip_assignments": [
                {"device": r1, "interface_hint": "GigabitEthernet0/0",
                 "subnet": net_a, "ip": "10.61.10.1"},
                {"device": r1, "interface_hint": "GigabitEthernet0/1",
                 "subnet": net_b, "ip": "10.61.20.1"},
                {"device": pc1, "subnet": net_a, "ip": "10.61.10.10"},
                {"device": pc2, "subnet": net_b, "ip": "10.61.20.10"},
            ],
            "acceptance_criteria": [
                {"id": "A-1", "requirement_ids": [],
                 "test": {"kind": "flow", "source": pc1, "destination": pc2,
                          "expect": "reachable"}},
                {"id": "A-2", "requirement_ids": [],
                 "test": {"kind": "flow", "source": pc2, "destination": pc1,
                          "expect": "reachable"}},
            ],
        }
    )


def test_engine_pings_cross_the_lab_and_writes_pass(tmp_path):
    tag = live.run_tag()
    spec = _lab_spec(tag)
    # Full ids, no raw fallback: owned_device_names() with a prefix would
    # also hand the untagged "r1"/"pc1"/"pc2" to the delete path, and those
    # may be the user's own devices.
    ids = [f"{tag}-{i}" for i in _SPEC_IDS]
    adapter = live.make_adapter()
    autopilot = Autopilot(
        adapter,
        SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=50)),
        EvidenceStore(root=tmp_path / "evidence"),
    )
    try:
        build, error = autopilot.build(spec)

        assert error is None, error
        assert build is not None
        # the fix under test: the flow ops really ran (no cascade blockage)
        flows = build.execution["flow_results"]
        assert len(flows) == 2, flows
        # and the engine's own probes say the data plane works
        connectivity = build.verification.by_level("connectivity")
        assert len(connectivity) == 2, [
            (t.test_id, t.verdict) for t in connectivity
        ]
        for t in connectivity:
            assert t.verdict is Verdict.PASS, (
                t.test_id, t.verdict, t.observed_result
            )
        assert build.execution["failed"] == [], build.execution["failed"]
    finally:
        outcomes = live.delete_spec_devices(ids)
        leaked = live.leaked_spec_devices(ids)
        assert not leaked, f"leaked devices: {leaked} (outcomes: {outcomes})"
