"""Stage 4 objectives, tracer, fault rules and report tests (S4.5-S4.8)."""

from __future__ import annotations

import pytest

from network_autopilot.brownfield.faults import FaultDetector, ExpectedNetwork
from network_autopilot.brownfield.importer import BrownfieldImporter
from network_autopilot.brownfield.intent import AsIsIntentBuilder
from network_autopilot.brownfield.models import Diagnostic
from network_autopilot.brownfield.objectives import ObjectiveRegistry
from network_autopilot.brownfield.report import DiagnosisReport
from network_autopilot.brownfield.tracer import PathTracer
from network_autopilot.core.observability import Observable

from tests.autopilot import s4_helpers as h


def _setup(name: str = "T02-two-vlan-office.json"):
    spec = h.load_spec(name)
    adapter, imported = h.import_simulated(spec)
    intent = AsIsIntentBuilder().build(imported)
    expected = ExpectedNetwork.from_spec(intent.spec)
    objectives = h.objectives_for(spec)
    detector = FaultDetector(
        imported.inventory, imported.corpus, objectives, expected=expected
    )
    return spec, adapter, imported, intent, expected, objectives, detector


def test_clean_network_trace_is_config_consistent() -> None:
    spec, adapter, imported, *_ = _setup()
    trace = PathTracer(imported.inventory, imported.corpus).trace("pc1", "pc3")
    assert trace.status == "config_consistent"
    assert trace.path == ["pc1", "s1", "r1", "s1", "pc3"]
    assert trace.first_failing_hop is None


def test_explicit_user_requirement_is_confirmed_and_actionable() -> None:
    registry = ObjectiveRegistry()
    objective = registry.add_user_requirement("PC5 must communicate with Server1")
    assert objective.source == "explicit"
    assert objective.confirmed and objective.actionable
    isolation = registry.add_user_requirement("VLAN 20 must remain isolated from VLAN 30")
    assert isolation.kind == "isolation"
    with pytest.raises(ValueError):
        registry.add_user_requirement("make the network better")


def test_inferred_objective_requires_confirmation() -> None:
    _, _, imported, _, _, objectives, detector = _setup()
    inferred = objectives.infer_from_inventory(imported.inventory)
    pending = [o for o in inferred if not o.actionable]
    assert pending, "the imported hosts should produce inferred objectives"
    diagnostics = detector.diagnose(pending[0])
    assert diagnostics
    assert diagnostics[0].status == "UNKNOWN - ADDITIONAL VERIFICATION REQUIRED"
    assert not diagnostics[0].repairable
    objectives.confirm(pending[0].id)
    assert objectives.get(pending[0].id).actionable


def test_wrong_access_vlan_names_first_failing_hop_and_minimal_fix() -> None:
    spec, adapter, imported, intent, expected, objectives, _ = _setup()
    switch = adapter.network.devices["s1"]
    access_port = next(
        i for i in switch.interfaces.values()
        if i.linked and i.access_vlan == 10
    )
    access_port.access_vlan = 999
    switch.vlans[999] = "WRONG"
    fresh = BrownfieldImporter(adapter).import_network()
    detector = FaultDetector(
        fresh.inventory, fresh.corpus, objectives, expected=expected
    )
    diagnostics = detector.diagnose(
        next(o for o in objectives.confirmed() if o.id == "OBJ-A-1")
    )
    wrong = [d for d in diagnostics if d.cause_class == "wrong-access-vlan"]
    assert wrong, [d.cause_class for d in diagnostics]
    diagnosis = wrong[0]
    assert diagnosis.layer == "L2"
    assert diagnosis.confidence == "high"
    assert diagnosis.affected_device == "s1"
    assert diagnosis.affected_interface == access_port.name
    assert diagnosis.repairable
    assert diagnosis.recommended_fix[0].commands == (
        f"interface {access_port.name}",
        "switchport access vlan 10",
        "exit",
    )


def test_multiple_simultaneous_faults_are_all_reported() -> None:
    spec, adapter, imported, intent, expected, objectives, _ = _setup()
    switch = adapter.network.devices["s1"]
    access_ports = [
        i for i in switch.interfaces.values()
        if i.linked and i.access_vlan is not None
    ]
    access_ports[0].access_vlan = 999
    switch.vlans[999] = "WRONG"
    # A second, unrelated L1 fault on another cabled port.
    trunk = next(i for i in switch.interfaces.values() if i.switchport_mode == "trunk")
    trunk.shutdown = True
    fresh = BrownfieldImporter(adapter).import_network()
    detector = FaultDetector(
        fresh.inventory, fresh.corpus, objectives, expected=expected
    )
    diagnostics = detector.diagnose_all_confirmed()
    classes = {d.cause_class for d in diagnostics}
    assert "wrong-access-vlan" in classes
    assert "port-shutdown" in classes


def test_acl_block_is_a_security_diagnosis_with_evidence() -> None:
    spec, adapter, imported, intent, expected, objectives, _ = _setup()
    router = adapter.network.devices["r1"]
    port = next(i for i in router.interfaces.values() if i.linked)
    router.acls.setdefault("BLOCK", []).append("deny ip any any")
    port.acl_in = "BLOCK"
    fresh = BrownfieldImporter(adapter).import_network()
    detector = FaultDetector(
        fresh.inventory, fresh.corpus, objectives, expected=expected
    )
    diagnostics = detector.diagnose(
        next(
            o for o in objectives.confirmed()
            if o.params.get("destination") == "pc3"
        )
    )
    acl = next(d for d in diagnostics if d.cause_class == "acl-block")
    assert acl.layer == "security"
    assert acl.confidence == "high"
    assert any("deny ip any any" in item for item in acl.evidence)
    assert acl.recommended_fix


def test_report_schema_enforces_evidence_and_confirmation() -> None:
    unknown = Diagnostic(
        diagnosis_id="d-unknown",
        cause_class="unknown",
        layer="L3",
        root_cause="not enough evidence",
        evidence=(),
        confidence="unknown",
        impact="cannot be stated",
        verification_requirements=("more evidence",),
    )
    from network_autopilot.brownfield.models import FixAction

    guessed = Diagnostic(
        diagnosis_id="d-guess",
        cause_class="wrong-access-vlan",
        layer="L2",
        root_cause="looks wrong",
        evidence=("a hunch",),
        confidence="high",
        recommended_fix=(FixAction(device="s1", commands=("interface Fa0/1",)),),
        impact="traffic may be dropped",
        verification_requirements=("re-run the flow",),
        objective_id="OBJ-x",
        objective_confirmed=False,
    )
    report = DiagnosisReport.build("lab", [unknown, guessed])
    assert unknown.status == "UNKNOWN - ADDITIONAL VERIFICATION REQUIRED"
    codes = {issue.code for issue in report.validate()}
    assert "repair-without-confirmed-objective" in codes
    markdown = report.to_markdown()
    assert "UNKNOWN - ADDITIONAL VERIFICATION REQUIRED" in markdown
    assert "Recommended minimal fix" in markdown


def test_stp_alternate_path_is_recognised() -> None:
    from network_autopilot.core.network_spec import NetworkSpec
    from network_autopilot.brownfield.simulated import SimulatedAdapter
    from network_autopilot.brownfield.importer import BrownfieldImporter

    raw = {
        "name": "stp-loop",
        "devices": [
            {"id": "s1", "role": "switch"},
            {"id": "s2", "role": "switch"},
            {"id": "pc1", "role": "pc"},
            {"id": "pc2", "role": "pc"},
        ],
        "links": [
            {"id": "l-a", "a": {"device": "s1", "interface_hint": "FastEthernet0/1"},
             "b": {"device": "s2", "interface_hint": "FastEthernet0/1"}},
            {"id": "l-b", "a": {"device": "s1", "interface_hint": "FastEthernet0/2"},
             "b": {"device": "s2", "interface_hint": "FastEthernet0/2"}},
            {"id": "l-pc1", "a": {"device": "s1", "interface_hint": "FastEthernet0/3"},
             "b": {"device": "pc1", "interface_hint": "FastEthernet0"}},
            {"id": "l-pc2", "a": {"device": "s2", "interface_hint": "FastEthernet0/3"},
             "b": {"device": "pc2", "interface_hint": "FastEthernet0"}},
        ],
        "subnets": [{"id": "lan", "cidr": "10.245.0.0/24"}],
        "ip_assignments": [
            {"device": "pc1", "subnet": "lan", "ip": "10.245.0.10"},
            {"device": "pc2", "subnet": "lan", "ip": "10.245.0.11"},
        ],
    }
    spec = NetworkSpec.model_validate(raw)
    adapter = SimulatedAdapter(spec)
    imported = BrownfieldImporter(adapter).import_network()
    tracer = PathTracer(imported.inventory, imported.corpus)
    assert tracer._has_alternate_path("s1", "s2", "FastEthernet0/1") is True
    assert tracer._has_alternate_path("s1", "pc1", "FastEthernet0/3") is False
