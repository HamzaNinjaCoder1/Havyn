"""S1.4 live: a leftover device stops the build before any mutation.

The offline tests prove the check's logic on a fake adapter; this proves the
real channel produces a readable name list and that the refusal happens
against a live Packet Tracer workspace - and that the message names the
device that actually collides, not a guess.

Teardown deletes every name this test could have created (the seeded device
and, should a bug ever let the build start, the spec's second device), then
re-reads the workspace to prove nothing leaked.
"""

from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "tests/autopilot")
import _live_harness as live

from src.network_autopilot.core.build_errors import PreflightBlocked
from src.network_autopilot.core.network_spec import NetworkSpec
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


def test_a_leftover_device_stops_the_live_build_and_is_named(tmp_path):
    adapter = live.make_adapter()
    tag = live.run_tag()
    left = f"{tag}-r1"
    right = f"{tag}-pc1"
    spec = NetworkSpec.model_validate(
        {
            "name": "live-collision-lab",
            "devices": [
                {"id": left, "role": "router"},
                {"id": right, "role": "pc"},
            ],
            "links": [
                {
                    "id": f"{tag}-l1",
                    "a": {"device": left, "interface_hint": "GigabitEthernet0/1"},
                    "b": {"device": right, "interface_hint": "FastEthernet0"},
                }
            ],
            "subnets": [{"id": f"{tag}-net", "cidr": "192.168.77.0/24"}],
            "ip_assignments": [
                {
                    "device": left,
                    "subnet": f"{tag}-net",
                    "ip": "192.168.77.1",
                    "interface_hint": "GigabitEthernet0/1",
                },
                {"device": right, "subnet": f"{tag}-net"},
            ],
        }
    )
    autopilot = Autopilot(
        adapter,
        SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=50)),
        EvidenceStore(root=tmp_path / "evidence"),
    )
    created: list[str] = []
    try:
        receipt = adapter.create_device(
            left, "router", model_hint="2911", position=(1080, 480)
        )
        assert receipt.result.value == "success", receipt.error
        created.append(left)

        report, error = autopilot.build(spec)

        assert report is None, "a colliding workspace must stop the build"
        assert isinstance(error, PreflightBlocked), error
        assert left in str(error), (left, error)
        assert right not in str(error), "only real collisions may be named"
        names = adapter.list_device_names()
        assert names.is_known, "the live workspace must be readable here"
        assert right not in names.value, "the refused build created nothing"
    finally:
        live.delete_devices(sorted({*created, right}))
        leftover = live.leaked_spec_devices([left, right])
        assert not leftover, f"leaked devices: {leftover}"
