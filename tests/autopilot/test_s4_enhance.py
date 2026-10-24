"""Stage 4 enhancement tests (S4.11)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from network_autopilot.brownfield.backup import BackupManager
from network_autopilot.brownfield.enhance import BrownfieldEnhancer, EnhancementPlanner
from network_autopilot.brownfield.simulated import SimulatedAdapter
from network_autopilot.core.network_spec import NetworkSpec
from network_autopilot.core.safety import MutationBudget, SafetyGate, SafetyMode

from tests.autopilot import s4_helpers as h


def _raw(name: str) -> dict:
    return json.loads((h.LIBRARY / name).read_text(encoding="utf-8"))


def test_planner_reports_structured_additions() -> None:
    raw = _raw("T01-small-office.json")
    current = NetworkSpec.model_validate(raw)
    desired_raw = copy.deepcopy(raw)
    desired_raw["devices"].append({"id": "pc6", "role": "pc"})
    desired_raw["links"].append(
        {
            "id": "l-s1-pc6",
            "a": {"device": "s1", "interface_hint": "FastEthernet0/7"},
            "b": {"device": "pc6", "interface_hint": "FastEthernet0"},
        }
    )
    desired_raw["ip_assignments"].append(
        {"device": "pc6", "subnet": "lan", "ip": "192.168.1.60"}
    )
    desired = NetworkSpec.model_validate(desired_raw)
    plan = EnhancementPlanner().plan(current, desired)
    assert not plan.blocked
    kinds = {(change.section, change.kind) for change in plan.changes}
    assert ("devices", "add") in kinds
    assert ("links", "add") in kinds
    assert ("ip_assignments", "add") in kinds
    assert plan.operations
    assert "pc6" in plan.affected_devices


def test_planner_detects_ip_overlap() -> None:
    raw = _raw("T01-small-office.json")
    current = NetworkSpec.model_validate(raw)
    desired_raw = copy.deepcopy(raw)
    desired_raw["subnets"].append({"id": "overlap", "cidr": "192.168.1.128/25"})
    desired = NetworkSpec.model_validate(desired_raw)
    plan = EnhancementPlanner().plan(current, desired)
    assert any(c.code == "ip-overlap" for c in plan.conflicts)
    assert plan.blocked


def test_planner_detects_vlan_and_name_collisions() -> None:
    raw = _raw("T02-two-vlan-office.json")
    current = NetworkSpec.model_validate(raw)
    desired_raw = copy.deepcopy(raw)
    desired_raw["vlans"][0]["name"] = "SOMETHING-ELSE"
    desired_raw["devices"][0]["role"] = "switch"
    desired = NetworkSpec.model_validate(desired_raw)
    plan = EnhancementPlanner().plan(current, desired)
    codes = {c.code for c in plan.conflicts}
    assert "vlan-name-collision" in codes
    assert "naming-conflict" in codes


def test_planner_detects_insufficient_interface_capacity() -> None:
    raw = _raw("T01-small-office.json")
    current = NetworkSpec.model_validate(raw)
    desired_raw = copy.deepcopy(raw)
    for index in range(30):
        host = f"pcextra{index}"
        desired_raw["devices"].append({"id": host, "role": "pc"})
        desired_raw["links"].append(
            {
                "id": f"l-s1-{host}",
                "a": {"device": "s1"},
                "b": {"device": host},
            }
        )
    desired = NetworkSpec.model_validate(desired_raw)
    plan = EnhancementPlanner().plan(current, desired)
    assert any(
        c.code == "insufficient-interface-capacity" for c in plan.conflicts
    )


def test_enhancement_applies_and_preserves_original_criteria(tmp_path: Path) -> None:
    raw = _raw("T01-small-office.json")
    current = NetworkSpec.model_validate(raw)
    desired_raw = copy.deepcopy(raw)
    desired_raw["name"] = "T01 enhanced"
    desired_raw["devices"].append({"id": "pc6", "role": "pc"})
    desired_raw["links"].append(
        {
            "id": "l-s1-pc6",
            "a": {"device": "s1", "interface_hint": "FastEthernet0/7"},
            "b": {"device": "pc6", "interface_hint": "FastEthernet0"},
        }
    )
    desired_raw["ip_assignments"].append(
        {"device": "pc6", "subnet": "lan", "ip": "192.168.1.60"}
    )
    desired = NetworkSpec.model_validate(desired_raw)
    plan = EnhancementPlanner().plan(current, desired)
    assert not plan.blocked
    adapter = SimulatedAdapter(current)
    enhancer = BrownfieldEnhancer(
        adapter,
        SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=200)),
        backup=BackupManager(adapter, root=tmp_path),
    )
    outcome = enhancer.apply(plan, approved=True)
    assert outcome.executed
    assert outcome.preserved, outcome.regressions
    assert outcome.success, (outcome.problems, outcome.original_before,
                             outcome.original_after)
    # Only the changed subset was pushed, not the whole network.
    assert len(outcome.applied_operations) < 20
