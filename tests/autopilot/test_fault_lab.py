"""Fault-injection lab (S2.5), offline: the harness's own honesty rules.

The live half of this harness is proven against Packet Tracer (its evidence
is `fault_lab_results/fault_lab.json`). What can be pinned offline — and what
actually decides whether a live run means anything — is when the driver is
allowed to say a fault was verified at all:

* a fault that does not break the intended behaviour is NEVER verified, even
  though its injection "succeeded";
* an injection the adapter refused stops the experiment before any claim;
* the revert still runs when the break failed, because leaving a fault in
  place would contaminate every later fault in the run;
* nothing is injected into a lab that was not known-good first;
* a fake adapter cannot break anything, so the honest run reports that.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

from network_autopilot.core.receipts import OperationReceipt, OperationResult
from network_autopilot.core.requirements import FlowOutcome, FlowResult

fault_lab = importlib.import_module("scripts.fault_lab")


class FakeAdapter:
    """A live adapter whose probe outcome and receipt verdict are scripted.

    ``broken_after`` is the probe outcome once the fault has been applied, so
    a test can stage "the injection landed but the path still works" — the
    case that must never read as a verified fault.
    """

    def __init__(self, *, healthy="reachable", broken_after="blocked",
                 inject_ok=True, revert_ok=True):
        self.healthy = healthy
        self.broken_after = broken_after
        self.inject_ok = inject_ok
        self.revert_ok = revert_ok
        self.applied = False
        self.calls: list[str] = []

    # -- mutations ---------------------------------------------------------
    def _receipt(self, ok: bool, op_id: str) -> OperationReceipt:
        return OperationReceipt(
            operation_id=op_id,
            operation="configure_ios",
            requested={},
            result=OperationResult.SUCCESS if ok else OperationResult.FAILED,
            error=None if ok else "PT refused the line",
        )

    def configure_ios(self, device, commands, operation_id=None):
        op_id = operation_id or "op"
        if "revert" in op_id:
            self.calls.append(f"revert:{device}")
            self.applied = False
            return self._receipt(self.revert_ok, op_id)
        self.calls.append(f"inject:{device}")
        self.applied = True
        return self._receipt(self.inject_ok, op_id)

    def configure_host(self, device, *, dhcp, ip=None, mask=None,
                       gateway=None, dns=None, operation_id=None):
        return self.configure_ios(device, [], operation_id=operation_id)

    # -- reads -------------------------------------------------------------
    def run_flow_test(self, source, destination, *, transport="icmp", **kw):
        outcome = self.broken_after if self.applied else self.healthy
        return FlowResult(
            source=source, destination=destination, transport=transport,
            port=None, outcome=FlowOutcome(outcome), detail="scripted",
        )


class StubAutopilot:
    """`inspect_diagnosable` with a scripted cause list."""

    def __init__(self, causes=("interface(s) down on the path",), error=None,
                 raises=None):
        self.causes = list(causes)
        self.error = error
        self.raises = raises
        self.calls = 0

    def inspect_diagnosable(self, spec):
        self.calls += 1
        if self.raises is not None:
            raise self.raises

        class _D:
            def __init__(self, root_cause):
                self.root_cause = root_cause

        return (None, None, None, tuple(_D(c) for c in self.causes), self.error)


def _action(**over):
    """A minimal fault whose expected cause matches the stub's."""
    defaults = dict(
        label="port-shutdown",
        description="scripted",
        expected_cause="interface(s) down on the path",
        apply=lambda a: "success",
        revert=lambda a: "success",
    )
    defaults.update(over)
    return fault_lab.FaultAction(**defaults)


def _live_action(adapter, **over):
    return _action(
        apply=lambda a: fault_lab._push_ios(a, "r1", ["shutdown"], "inject"),
        revert=lambda a: fault_lab._push_ios(a, "r1", ["no shutdown"], "revert"),
        **over,
    )


def test_a_real_fault_is_verified_through_all_six_steps():
    adapter = FakeAdapter()
    autopilot = StubAutopilot()

    outcome = fault_lab.run_fault(adapter, autopilot, object(),
                                  _live_action(adapter))

    assert outcome.verified is True
    assert outcome.failure is None
    assert outcome.control_before == "reachable"
    assert outcome.observed_broken == "blocked"
    assert outcome.caused == ["interface(s) down on the path"]
    assert outcome.control_after == "reachable"
    assert adapter.applied is False, "the lab was left with the fault in place"
    assert autopilot.calls == 1


def test_a_fault_that_does_not_break_anything_is_never_verified():
    """The core rule: a written label is not an injected fault.

    The injection is accepted by the device and the flow still forwards —
    which is exactly the shape a wrong CLI line or a stale port name takes.
    """
    adapter = FakeAdapter(broken_after="reachable")
    outcome = fault_lab.run_fault(adapter, StubAutopilot(), object(),
                                  _live_action(adapter))

    assert outcome.verified is False
    assert "did not break the intended behaviour" in outcome.failure
    assert outcome.caused == [], "no diagnosis is claimed for a fault that did not land"
    # ... and the revert still ran, so the NEXT fault starts from a clean lab
    assert adapter.calls[-1] == "revert:r1"
    assert adapter.applied is False


def test_a_refused_injection_stops_the_experiment_before_any_claim():
    adapter = FakeAdapter(inject_ok=False)
    outcome = fault_lab.run_fault(adapter, StubAutopilot(), object(),
                                  _live_action(adapter))

    assert outcome.verified is False
    assert "injection did not take effect" in outcome.failure
    assert outcome.observed_broken == "", "no probe was run after a failed inject"
    assert outcome.control_after == ""


def test_nothing_is_injected_into_a_lab_that_is_not_known_good():
    adapter = FakeAdapter(healthy="blocked")
    outcome = fault_lab.run_fault(adapter, StubAutopilot(), object(),
                                  _live_action(adapter))

    assert outcome.verified is False
    assert "not known-good before the fault" in outcome.failure
    assert adapter.calls == [], "a fault was applied to an already-broken lab"


def test_a_missing_diagnosis_fails_the_fault_even_though_it_broke():
    """Breaking the flow is necessary, not sufficient: the run also asks
    whether Havyn can name what happened."""
    adapter = FakeAdapter()
    outcome = fault_lab.run_fault(adapter, StubAutopilot(causes=("something else",)),
                                  object(), _live_action(adapter))

    assert outcome.observed_broken == "blocked"
    assert outcome.verified is False
    assert "did not name" in outcome.failure


def test_an_unnamed_cause_is_the_expected_result_for_a_wrong_gateway():
    """A wrong gateway is deliberately indistinguishable from other L3
    faults, so that fault must not be failed for reporting the unknown."""
    adapter = FakeAdapter()
    outcome = fault_lab.run_fault(
        adapter,
        StubAutopilot(causes=("connectivity failed but no state anomaly was observed",)),
        object(), _live_action(adapter, expect_named_cause=False),
    )
    assert outcome.verified is True


def test_a_lab_that_does_not_come_back_after_the_revert_fails():
    """A revert that does not restore the lab is a failed fault, even though
    every other step passed — the run is only as clean as its last probe."""

    def _apply(a):
        a.applied = True
        return "success"

    def _revert(a):
        # The fault is taken back, but the path stays down: something else
        # was broken and left behind.
        a.applied = False
        a.healthy = "blocked"
        return "success"

    adapter = FakeAdapter()
    outcome = fault_lab.run_fault(
        adapter, StubAutopilot(), object(),
        _action(apply=_apply, revert=_revert),
    )
    assert outcome.observed_broken == "blocked"
    assert outcome.control_after == "blocked"
    assert outcome.verified is False
    assert "did not come back after the revert" in outcome.failure


def test_diagnosis_is_run_over_the_same_pass_as_the_failing_report():
    """One pass: the cause explains the verdict it was recorded against."""
    adapter = FakeAdapter()
    autopilot = StubAutopilot()
    fault_lab.run_fault(adapter, autopilot, object(), _live_action(adapter))
    assert autopilot.calls == 1


def test_the_five_blueprint_faults_are_all_bound_to_the_lab():
    labels = [f.label for f in fault_lab.build_faults()]
    # The labels are stable: the scorecard and the capability map join on
    # them, so a rename here silently breaks every downstream join.
    assert labels == [
        "port-shutdown", "route-missing", "wrong-access-vlan",
        "trunk-vlan-not-allowed", "wrong-gateway",
    ]
    assert set(labels) == {
        "port-shutdown", "route-missing", "wrong-access-vlan",
        "trunk-vlan-not-allowed", "wrong-gateway",
    }
    for fault in fault_lab.build_faults():
        assert fault.description and fault.expected_cause


def test_the_lab_spec_compiles_and_names_what_each_fault_attacks():
    """The lab is the experiment's control: it has to be buildable offline
    before any live run, and its rendered config is what the fault commands
    are read off."""
    from network_autopilot.compiler import compile_spec
    from network_autopilot.compiler.spec_lint import format_issues, lint_spec
    from network_autopilot.core.network_spec import NetworkSpec
    from network_autopilot.ipam import plan_addressing

    spec = NetworkSpec.model_validate(
        json.loads(Path(fault_lab.DEFAULT_LAB).read_text(encoding="utf-8"))
    )
    assert lint_spec(spec) == [], format_issues(lint_spec(spec))
    compiled = compile_spec(spec, plan_addressing(spec))

    rendered = {
        node.operation.inputs.get("device"): "\n".join(
            node.operation.inputs.get("commands") or []
        )
        for node in compiled.dag.nodes.values()
        if node.operation.operation_type == "configure_ios"
    }
    # Every target the faults name must exist in the compiler's own output,
    # or the live run pushes CLI at an interface that is not there.
    assert "GigabitEthernet0/1" in rendered["r1"]
    assert "ip route 10.0.30.0 255.255.255.0 10.0.0.2" in rendered["r1"]
    assert "switchport access vlan 10" in rendered["s1"]
    assert "switchport trunk allowed vlan 10" in rendered["s1"]


def test_the_null_adapter_run_reports_that_nothing_broke(tmp_path, capsys):
    """`--adapter null` is the honest control: a fake adapter cannot break a
    real flow, so the harness must say so rather than invent a break."""
    lab = tmp_path / "lab.json"
    lab.write_text(Path(fault_lab.DEFAULT_LAB).read_text(encoding="utf-8"),
                   encoding="utf-8")

    code = fault_lab.main([
        "--spec", str(lab), "--adapter", "null",
        "--results-dir", str(tmp_path / "out"),
    ])

    # The lab cannot even be built on the NullAdapter (its data plane is
    # unobservable), so the run stops BEFORE injecting anything - the
    # strongest form of "it did not pretend".
    assert code == fault_lab.EXIT_ERROR
    payload = json.loads(capsys.readouterr().out)
    assert payload["lab"] == "not known-good"
    assert not (tmp_path / "out" / "fault_lab.json").exists()