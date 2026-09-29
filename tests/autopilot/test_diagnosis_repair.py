"""Diagnosis + bounded repair: evidence-backed causes, budgets, no faking."""

from __future__ import annotations

from src.network_autopilot.adapter import NullAdapter
from src.network_autopilot.compiler import compile_spec
from src.network_autopilot.core.safety import MutationBudget, SafetyGate, SafetyMode
from src.network_autopilot.diagnosis import diagnose
from src.network_autopilot.ipam import plan_addressing
from src.network_autopilot.repair import RepairEngine
from src.network_autopilot.verification import VerificationEngine
from tests.autopilot.test_verification_diff import _observe_full, _golden
from src.network_autopilot.core.network_spec import NetworkSpec
from src.network_autopilot.core.requirements import (
    AcceptanceReport,
    FlowOutcome,
    FlowResult,
    Verdict,
)


def _diagnose_clean():
    spec, plan, dag = _golden()
    adapter = NullAdapter()
    _execution, state = _observe_full(adapter, spec, dag)
    report = VerificationEngine(adapter).verify(
        spec, plan, state, _execution.flow_results,
        link_endpoints=_endpoints(dag), observations_complete=True,
    )
    return spec, plan, adapter, report, state


def _endpoints(dag):
    out = {}
    for node in dag.nodes.values():
        if node.operation.operation_type == "create_link":
            i = node.operation.inputs
            out[i["link_id"]] = (i["a_device"], i["a_port"], i["b_device"], i["b_port"])
    return out


def test_clean_build_has_no_repairable_diagnoses():
    spec, plan, adapter, report, state = _diagnose_clean()
    diagnoses = diagnose(spec, report, state)
    # only connectivity UNKNOWNs (no data plane) -> no repairable causes
    assert all(not d.repairable for d in diagnoses)


def test_missing_device_diagnosed_with_create_repair():
    spec, plan, dag = _golden()
    adapter = NullAdapter()
    # build everything EXCEPT pc4
    from src.network_autopilot.compiler.executor import DagExecutor
    from src.network_autopilot.compiler.dag import OpStatus

    executor = DagExecutor(adapter, SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=100)))
    for op_id in list(dag.topo_order()):
        node = dag.nodes[op_id]
        if node.operation.operation_id == "op-create-pc4":
            node.status = OpStatus.SKIPPED  # pretend it was never created
            continue
    execution = executor.execute(dag)
    # pc4 creation was skipped -> dependents blocked; force-fake not needed.
    # Now diagnose on the observed state.
    state = executor.state
    state.merge(adapter.observe_state([d.id for d in spec.devices]))
    report = VerificationEngine(adapter).verify(
        spec, plan, state, {},
        link_endpoints=_endpoints(dag), observations_complete=True,
    )
    diagnoses = diagnose(spec, report, state)
    pc4_diags = [d for d in diagnoses if "pc4" in d.affected_resources and d.repairable]
    assert pc4_diags, [d.to_dict() for d in diagnoses]
    d = pc4_diags[0]
    assert d.confidence == "high"
    assert d.proposed_repair[0]["operation_type"] == "create_device"


def test_repair_executes_and_budget_blocks_further_attempts():
    spec, plan, dag = _golden()
    adapter = NullAdapter()
    _execution, state = _observe_full(adapter, spec, dag)
    report = VerificationEngine(adapter).verify(
        spec, plan, state, {}, link_endpoints=_endpoints(dag), observations_complete=True,
    )
    # a clean build yields no repairable diagnoses (see
    # test_clean_build_has_no_repairable_diagnoses), so this test drives the
    # RepairEngine with a synthetic evidence-backed diagnosis instead.
    engine = RepairEngine(
        adapter,
        SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=100)),
        max_attempts=1,
    )
    # Simulate a repairable diagnosis: device absent
    from src.network_autopilot.diagnosis.engine import Diagnosis

    fake = Diagnosis(
        diagnosis_id="diag-test",
        failed_tests=("VT-struct-device-pc9",),
        root_cause="device 'pc9' is absent from the workspace",
        evidence=("observation complete; not seen",),
        confidence="high",
        affected_resources=("pc9",),
        proposed_repair=(
            {"operation_type": "create_device", "inputs": {"name": "pc9", "role": "pc"}, "reason": "missing"},
        ),
        uncertainty=None,
    )
    repair_plan = engine.plan_repair(fake, plan)
    assert not repair_plan.blocked
    outcome = engine.execute(repair_plan)
    assert outcome.executed
    assert adapter.device_exists("pc9").value is True  # really created
    # budget: only 1 attempt allowed
    outcome2 = engine.execute(repair_plan)
    assert not outcome2.executed
    assert "budget" in (outcome2.blocked_reason or "")


def test_repair_blocked_without_evidence():
    from src.network_autopilot.diagnosis.engine import Diagnosis

    adapter = NullAdapter()
    engine = RepairEngine(adapter, SafetyGate(SafetyMode.AUTONOMOUS))
    no_evidence = Diagnosis(
        diagnosis_id="diag-weak",
        failed_tests=("VT-flow-A-1",),
        root_cause="connectivity failed but no state anomaly was observed",
        evidence=("flow observed: blocked",),
        confidence="unknown",
        affected_resources=("pc1", "pc4"),
        proposed_repair=(),
        uncertainty="no evidence-backed root cause",
    )
    repair_plan = engine.plan_repair(no_evidence)
    assert repair_plan.blocked
    assert "REPAIR BLOCKED" in repair_plan.blocked_reason
    outcome = engine.execute(repair_plan)
    assert not outcome.executed  # nothing was mutated


def test_repair_via_safety_gate_read_only_is_blocked():
    from src.network_autopilot.diagnosis.engine import Diagnosis

    adapter = NullAdapter()
    engine = RepairEngine(adapter, SafetyGate(SafetyMode.READ_ONLY))
    diag = Diagnosis(
        diagnosis_id="diag-x",
        failed_tests=("VT-struct-device-pc9",),
        root_cause="device 'pc9' absent",
        evidence=(),
        confidence="high",
        affected_resources=("pc9",),
        proposed_repair=(
            {"operation_type": "create_device", "inputs": {"name": "pc9", "role": "pc"}, "reason": "missing"},
        ),
        uncertainty=None,
    )
    repair_plan = engine.plan_repair(diag)
    outcome = engine.execute(repair_plan)
    # the gate refused every mutation -> the repair did NOT execute
    assert not outcome.executed
    assert adapter.device_exists("pc9").value is False


# ---------------------------------------------------------------------------
# Bug #29 - repair classes for L2, DHCP and ASA
# ---------------------------------------------------------------------------

_L2_SPEC = {
    "name": "l2-lab",
    "devices": [
        {"id": "s1", "role": "switch"},
        {"id": "pc1", "role": "pc"},
    ],
    "vlans": [{"vlan_id": 10, "name": "users"}],
    "links": [
        {"id": "l-hosts",
         "a": {"device": "s1", "interface_hint": "FastEthernet0/1", "vlan": 10},
         "b": {"device": "pc1"}},
    ],
    "subnets": [{"id": "lan", "cidr": "10.10.0.0/24", "vlan_id": 10}],
    "ip_assignments": [{"device": "pc1", "subnet": "lan", "ip": "10.10.0.10"}],
}

_ASA_SPEC = {
    "name": "asa-lab",
    "devices": [
        {"id": "fw", "role": "firewall", "model_hint": "5506-X"},
        {"id": "pc1", "role": "pc"},
    ],
    "links": [
        {"id": "l1",
         "a": {"device": "fw", "interface_hint": "GigabitEthernet1/1"},
         "b": {"device": "pc1"}},
    ],
    "subnets": [{"id": "lan", "cidr": "10.20.0.0/24"}],
    "ip_assignments": [
        {"device": "fw", "subnet": "lan", "ip": "10.20.0.1",
         "interface_hint": "GigabitEthernet1/1"},
        {"device": "pc1", "subnet": "lan"},
    ],
}

_DHCP_SPEC = {
    "name": "dhcp-lab",
    "devices": [
        {"id": "r1", "role": "router"},
        {"id": "pc1", "role": "pc"},
    ],
    "links": [
        {"id": "l1",
         "a": {"device": "r1", "interface_hint": "GigabitEthernet0/1"},
         "b": {"device": "pc1"}},
    ],
    "subnets": [{"id": "lan", "cidr": "10.30.0.0/24", "dhcp": True}],
    "ip_assignments": [
        {"device": "r1", "subnet": "lan", "ip": "10.30.0.1",
         "interface_hint": "GigabitEthernet0/1"},
        {"device": "pc1", "subnet": "lan"},
    ],
}


def _report_with(test_id, level, target, *, observed, verdict=Verdict.FAIL):
    from src.network_autopilot.core.ids import utc_now_iso
    from src.network_autopilot.verification.engine import (
        VerificationReport,
        VerificationTest,
    )

    test = VerificationTest(
        test_id=test_id,
        level=level,
        source_requirement=None,
        source_acceptance_criterion=None,
        source=None,
        target=target,
        expected_result="as specified",
        observed_result=observed,
        evidence=(f"observed {observed}",),
        timestamp=utc_now_iso(),
        verdict=verdict,
    )
    return VerificationReport(
        tests=(test,),
        summary={verdict.value: 1},
        acceptance=AcceptanceReport((), (), {}, None),
        observations_complete=True,
    )


def _state_with(known_ips: dict[str, str]):
    from src.network_autopilot.core.actual_state import (
        ActualNetworkState,
        DeviceObservation,
        PortObservation,
    )
    from src.network_autopilot.core.ids import utc_now_iso
    from src.network_autopilot.core.observability import Observable

    state = ActualNetworkState(source="test", observed_at=utc_now_iso())
    for name, ip in known_ips.items():
        device = DeviceObservation(name=name)
        device.ports["GigabitEthernet0/1"] = PortObservation(
            name="GigabitEthernet0/1",
            ip=Observable.known(ip, source="test"),
        )
        state.observe_device(device)
    return state


def test_an_l2_vlan_failure_repairs_by_re_pushing_the_switch_block():
    """#29: a switch whose VLAN table does not match had no repair class at
    all, so the run planned repairs and executed none."""
    spec = NetworkSpec.model_validate(_L2_SPEC)
    report = _report_with(
        "VT-struct-vlan-10", "structural", "10",
        observed="vlan not present",
    )
    state = _state_with({"s1": "", "pc1": "10.10.0.10"})
    diagnoses = diagnose(spec, report, state)
    repairable = [d for d in diagnoses if d.repairable]
    assert repairable, [d.to_dict() for d in diagnoses]
    proposal = repairable[0].proposed_repair[0]
    assert proposal["operation_type"] == "ios_command"
    assert proposal["inputs"]["device"] == "s1"
    assert proposal["inputs"]["source"] == "switch_vlan_config"


def test_the_l2_repair_pushes_the_compilers_own_switch_block():
    """The repair must not invent CLI: it lifts the block from the compiler, so
    it cannot drift from what a build would have sent."""
    spec = NetworkSpec.model_validate(_L2_SPEC)
    plan = plan_addressing(spec)
    report = _report_with(
        "VT-struct-vlan-10", "structural", "10", observed="vlan not present"
    )
    state = _state_with({"s1": "", "pc1": "10.10.0.10"})
    diagnosis = [d for d in diagnose(spec, report, state) if d.repairable][0]
    engine = RepairEngine(
        NullAdapter(),
        SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=100)),
        spec=spec,
    )
    repair_plan = engine.plan_repair(diagnosis, plan)
    assert not repair_plan.blocked_reason, repair_plan.blocked_reason
    op = repair_plan.operations[0]
    assert op.operation_type == "configure_ios"
    assert " switchport access vlan 10" in op.inputs["commands"], op.inputs


def test_a_firewall_config_failure_repairs_by_re_pushing_its_asa_block():
    spec = NetworkSpec.model_validate(_ASA_SPEC)
    report = _report_with(
        "VT-addr-fw", "addressing", "fw:GigabitEthernet1/1",
        observed="no ip configured",
    )
    state = _state_with({"fw": "10.20.0.1", "pc1": ""})
    # two rules see this test: the generic addressing one (re-push the address)
    # and the security one (re-push the whole ASA block). The security rule is
    # the one that exists because an ASA with an address can still forward
    # nothing, so the test names that proposal explicitly.
    proposals = [
        p
        for d in diagnose(spec, report, state) if d.repairable
        for p in d.proposed_repair
        if p["inputs"].get("device") == "fw"
    ]
    sources = {p["inputs"].get("source") for p in proposals}
    assert "firewall_config" in sources, sources


def test_a_dhcp_client_with_no_observed_server_is_left_blocked():
    """#29: re-pushing a client config when the pool was never observed to be
    configured changes nothing, so the repair must stay blocked."""
    spec = NetworkSpec.model_validate(_DHCP_SPEC)
    report = _report_with(
        "VT-addr-pc1", "addressing", "pc1:FastEthernet0",
        observed="no ip configured",
    )
    # nothing observed at all: no server address, so the pool is unknown
    state = _state_with({"pc1": ""})
    diagnoses = diagnose(spec, report, state)
    dhcp = [d for d in diagnoses if "pc1" in d.affected_resources]
    assert dhcp, [d.to_dict() for d in diagnoses]
    for d in dhcp:
        assert d.proposed_repair == ()
        assert not d.repairable
        assert "REPAIR BLOCKED" in (d.uncertainty or "")


def test_a_dhcp_client_with_an_observed_server_is_repairable():
    spec = NetworkSpec.model_validate(_DHCP_SPEC)
    report = _report_with(
        "VT-addr-pc1", "addressing", "pc1:FastEthernet0",
        observed="no ip configured",
    )
    # the router is observed with an address, so the pool has somewhere to live
    state = _state_with({"r1": "10.30.0.1", "pc1": ""})
    plan = plan_addressing(spec)
    engine = RepairEngine(
        NullAdapter(),
        SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=100)),
        spec=spec,
    )
    dhcp = [
        d for d in diagnose(spec, report, state)
        if "pc1" in d.affected_resources and d.repairable
    ]
    assert dhcp, "with an observed server the client repair is evidence-backed"
    repair_plan = engine.plan_repair(dhcp[0], plan)
    assert not repair_plan.blocked_reason, repair_plan.blocked_reason
    assert repair_plan.operations[0].inputs.get("dhcp") is True


def test_a_repair_with_no_spec_cannot_render_a_source_block():
    """Without the spec the engine cannot re-render, and it blocks rather than
    sending an empty block that would look like a successful no-op."""
    from src.network_autopilot.diagnosis.engine import Diagnosis

    spec = NetworkSpec.model_validate(_L2_SPEC)
    engine = RepairEngine(
        NullAdapter(),
        SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=100)),
    )
    diagnosis = Diagnosis(
        diagnosis_id="diag-x",
        failed_tests=("VT-struct-vlan-10",),
        root_cause="switch state wrong",
        evidence=("observed",),
        confidence="medium",
        affected_resources=("s1",),
        proposed_repair=(
            {
                "operation_type": "ios_command",
                "inputs": {"device": "s1", "source": "switch_vlan_config"},
                "reason": "re-push",
            },
        ),
        uncertainty=None,
    )
    repair_plan = engine.plan_repair(diagnosis, plan_addressing(spec))
    assert repair_plan.operations == ()
    assert "unsupported" in (repair_plan.blocked_reason or "")


# ---------------------------------------------------------------------------
# Bug #15 - a firewall repair must be verified by FUNCTION (traffic), not by
# an address that merely reads back
# ---------------------------------------------------------------------------

_FIREWALL_FLOW_SPEC = {
    "name": "asa-forwarding-lab",
    "devices": [
        {"id": "fw", "role": "firewall", "model_hint": "5506-X"},
        {"id": "pc1", "role": "pc"},
        {"id": "srv", "role": "server"},
    ],
    "links": [
        {"id": "l-inside",
         "a": {"device": "fw", "interface_hint": "GigabitEthernet1/1"},
         "b": {"device": "pc1"}},
        {"id": "l-outside",
         "a": {"device": "fw", "interface_hint": "GigabitEthernet1/2"},
         "b": {"device": "srv"}},
    ],
    "subnets": [
        {"id": "inside", "cidr": "10.40.0.0/24"},
        {"id": "outside", "cidr": "10.50.0.0/24"},
    ],
    "ip_assignments": [
        {"device": "fw", "subnet": "inside", "ip": "10.40.0.1",
         "interface_hint": "GigabitEthernet1/1"},
        {"device": "fw", "subnet": "outside", "ip": "10.50.0.1",
         "interface_hint": "GigabitEthernet1/2"},
        {"device": "pc1", "subnet": "inside", "ip": "10.40.0.10"},
        {"device": "srv", "subnet": "outside", "ip": "10.50.0.10"},
    ],
    "acceptance_criteria": [
        {"id": "A-inside-outside", "requirement_ids": [],
         "test": {"kind": "flow", "source": "pc1", "destination": "srv",
                  "expect": "reachable"}},
    ],
}

# The same lab with nothing that could prove forwarding (#15).
_FIREWALL_NO_FLOW_SPEC = {
    key: value
    for key, value in _FIREWALL_FLOW_SPEC.items()
    if key != "acceptance_criteria"
}


class _FlowProbeAdapter(NullAdapter):
    """NullAdapter with a scripted data plane.

    The structural double honestly has no flows; this one answers every probe
    with a scripted outcome so the post-repair function check can be driven
    offline (#15).
    """

    def __init__(self, outcome: FlowOutcome) -> None:
        super().__init__()
        self.scripted_outcome = outcome
        self.flow_calls: list[tuple[str, str]] = []

    def run_flow_test(
        self, source, destination, *, transport="icmp", port=None,
        destination_ip=None,
    ):
        self.flow_calls.append((source, destination))
        return FlowResult(
            source=source,
            destination=destination,
            transport=transport,
            port=port,
            outcome=self.scripted_outcome,
            detail="scripted probe",
        )


def _firewall_repair_plan(spec_raw, adapter, observed_state):
    """Diagnose a firewall addressing failure and plan its ASA re-push."""
    spec = NetworkSpec.model_validate(spec_raw)
    plan = plan_addressing(spec)
    report = _report_with(
        "VT-addr-fw", "addressing", "fw:GigabitEthernet1/1",
        observed="no ip configured",
    )
    state = _state_with(observed_state)
    diagnoses = diagnose(spec, report, state)
    firewall_repairs = [
        d for d in diagnoses
        if d.repairable
        and any(p["inputs"].get("source") == "firewall_config"
                for p in d.proposed_repair)
    ]
    assert firewall_repairs, [d.to_dict() for d in diagnoses]
    for device in spec.devices:
        adapter.create_device(device.id, device.role.value, device.model_hint)
    engine = RepairEngine(
        adapter,
        SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=100)),
        spec=spec,
    )
    repair_plan = engine.plan_repair(firewall_repairs[0], plan)
    assert not repair_plan.blocked_reason, repair_plan.blocked_reason
    return engine, repair_plan


def test_a_firewall_repair_is_not_repaired_while_traffic_still_fails():
    """#15: the ASA block read back its address, so the symptom is gone - but
    the firewall still forwards nothing, so nothing was repaired."""
    adapter = _FlowProbeAdapter(FlowOutcome.BLOCKED)
    engine, repair_plan = _firewall_repair_plan(
        _FIREWALL_FLOW_SPEC,
        adapter,
        {"fw": "10.40.0.1", "pc1": "10.40.0.10", "srv": "10.50.0.10"},
    )
    outcome = engine.execute(repair_plan)

    assert outcome.executed
    # the symptom really is gone: the address reads back from the device
    read_back = adapter.inspect_device("fw").ports["GigabitEthernet1/1"]
    assert read_back.ip.value == "10.40.0.1"
    # ...so a symptom-only check would declare victory here (#15)
    assert adapter.flow_calls, "the re-verification must probe traffic (#15)"
    assert outcome.function_verdict == Verdict.FAIL.value
    assert not outcome.repaired


def test_a_firewall_repair_is_repaired_when_the_flow_now_passes():
    adapter = _FlowProbeAdapter(FlowOutcome.REACHABLE)
    engine, repair_plan = _firewall_repair_plan(
        _FIREWALL_FLOW_SPEC,
        adapter,
        {"fw": "10.40.0.1", "pc1": "10.40.0.10", "srv": "10.50.0.10"},
    )
    outcome = engine.execute(repair_plan)

    assert outcome.executed
    # the criterion whose path crosses the firewall is the one that is probed
    assert adapter.flow_calls == [("pc1", "srv")]
    assert outcome.function_flows[0]["criterion_id"] == "A-inside-outside"
    assert outcome.function_verdict == Verdict.PASS.value
    assert outcome.repaired


def test_a_firewall_repair_with_no_flow_criterion_stays_unverified():
    """No criterion can prove the firewall forwards, so the repair is reported
    unverified - never repaired (#15)."""
    adapter = _FlowProbeAdapter(FlowOutcome.REACHABLE)
    engine, repair_plan = _firewall_repair_plan(
        _FIREWALL_NO_FLOW_SPEC,
        adapter,
        {"fw": "10.40.0.1", "pc1": "10.40.0.10", "srv": "10.50.0.10"},
    )
    outcome = engine.execute(repair_plan)

    assert outcome.executed
    assert adapter.flow_calls == []  # nothing to probe honestly
    assert outcome.function_verdict == Verdict.UNKNOWN.value
    assert not outcome.repaired
    assert "flow" in (outcome.function_detail or "")


# ---------------------------------------------------------------------------
# Bug #10 - a Linksys address repair must use the port API, not the dead CLI
# ---------------------------------------------------------------------------

_LINKSYS_SPEC = {
    "name": "wr-lab",
    "devices": [
        {"id": "wr", "role": "wireless_router", "model_hint": "Linksys-WRT300N"},
        {"id": "pc1", "role": "pc"},
    ],
    "links": [{"id": "l1", "a": {"device": "wr"}, "b": {"device": "pc1"}}],
    "subnets": [{"id": "lan", "cidr": "192.168.0.0/24"}],
    "ip_assignments": [
        {"device": "wr", "subnet": "lan", "ip": "192.168.0.1"},
        {"device": "pc1", "subnet": "lan", "ip": "192.168.0.50"},
    ],
}


def _linksys_repair_engine(mode: SafetyMode = SafetyMode.AUTONOMOUS):
    from src.network_autopilot.diagnosis.engine import Diagnosis

    spec = NetworkSpec.model_validate(_LINKSYS_SPEC)
    plan = plan_addressing(spec)
    engine = RepairEngine(
        NullAdapter(),
        SafetyGate(mode, MutationBudget(max_mutations=100)),
        spec=spec,
    )
    diagnosis = Diagnosis(
        diagnosis_id="diag-wr-addr",
        failed_tests=("VT-addr-wr",),
        root_cause="the gateway LAN address does not match the plan",
        evidence=("observed 192.168.0.100 on Vlan1; planned 192.168.0.1",),
        confidence="high",
        affected_resources=("wr",),
        proposed_repair=(
            {"operation_type": "reconfigure_address",
             "inputs": {"device": "wr"}, "reason": "address drift"},
        ),
        uncertainty=None,
    )
    return engine, plan, diagnosis


def test_address_repair_on_a_linksys_materialises_the_port_api_op():
    """#10: PT's Linksys answers configureIosDevice with `true` and applies
    nothing (live probe), so a reconfigure_address repair on a
    wireless_router must materialise configure_gateway_lan — the same
    port-API push the build uses — never a dead configure_ios block."""
    engine, plan, diagnosis = _linksys_repair_engine()
    repair_plan = engine.plan_repair(diagnosis, plan)
    assert not repair_plan.blocked, repair_plan.blocked_reason
    op = repair_plan.operations[0]
    assert op.operation_type == "configure_gateway_lan"
    assert op.inputs == {
        "device": "wr",
        "ip": "192.168.0.1",
        "mask": "255.255.255.0",
    }


def test_rollback_of_a_linksys_address_pushes_the_port_api_too():
    """#10: rollback restored every prior address through configure_host —
    configurePcIp is unproven on a Linksys. The prior Vlan1 address must go
    back through the port API, same as every other gateway write."""
    engine, plan, diagnosis = _linksys_repair_engine()
    engine._prior_values["wr"] = {
        "device": "wr",
        "ports": {
            "Vlan1": {"ip": "192.168.0.1", "mask": "255.255.255.0", "gateway": None}
        },
    }
    pushed: list[tuple] = []

    def _capture(device, ip, mask, operation_id=None):
        pushed.append((device, ip, mask))
        return None

    engine.adapter.configure_gateway_lan = _capture  # type: ignore[method-assign]
    repair_plan = engine.plan_repair(diagnosis, plan)
    outcome = engine.rollback(repair_plan)

    assert outcome.rolled_back, outcome.blocked_reason
    assert pushed == [("wr", "192.168.0.1", "255.255.255.0")]


def test_rollback_in_read_only_mode_never_touches_the_adapter():
    """S1.11 / F8: rollback is a mutation path like any other. In READ_ONLY
    mode the safety gate must stop it BEFORE the adapter is written to —
    the whole point of the gate is that no mutation path bypasses it."""
    engine, plan, diagnosis = _linksys_repair_engine(SafetyMode.READ_ONLY)
    engine._prior_values["wr"] = {
        "device": "wr",
        "ports": {
            "Vlan1": {"ip": "192.168.0.1", "mask": "255.255.255.0", "gateway": None}
        },
    }
    pushed: list[tuple] = []

    def _capture(device, ip, mask, operation_id=None):
        pushed.append((device, ip, mask))
        return None

    engine.adapter.configure_gateway_lan = _capture  # type: ignore[method-assign]
    repair_plan = engine.plan_repair(diagnosis, plan)
    outcome = engine.rollback(repair_plan)

    assert not outcome.rolled_back, "READ_ONLY rollback must not report success"
    assert pushed == [], f"adapter was written in READ_ONLY mode: {pushed}"
    assert outcome.blocked_reason, "a blocked rollback must say why"
    assert "READ_ONLY" in outcome.blocked_reason, outcome.blocked_reason
