"""S1.2 / F-1 + F-2: the flow-verification fix, failing-first (docs/reports/flows-unknown.md).

Two stacked defects, each pinned by one test:

* **F-2 (cascade):** one config op that does not succeed blocked
  `op-wait-dhcp`, which blocked every `op-flow-*` — the reference run's 11
  flows never probed anything and verification printed UNKNOWN with
  "no flow result recorded". A read-only probe must still report the live
  data plane when an unrelated config op failed.
* **F-1 (plumbing):** the repair engine's own `run_flow_test` observations
  (3 real PASSes in the reference run) were written into
  `repair_function_check` records and discarded; verification only ever read
  `execution.flow_results`. The freshest probe must reach the re-verify.
"""

from __future__ import annotations

from src.network_autopilot.adapter import NullAdapter
from src.network_autopilot.core.network_spec import NetworkSpec
from src.network_autopilot.core.receipts import OperationResult
from src.network_autopilot.core.requirements import FlowOutcome, FlowResult, Verdict
from src.network_autopilot.core.safety import MutationBudget, SafetyGate, SafetyMode
from src.network_autopilot.evidence.store import EvidenceStore
from src.network_autopilot.orchestrator import Autopilot
from tests.autopilot.test_diagnosis_repair import _FIREWALL_FLOW_SPEC

# r1's config op failing is the reference run's cascade (op-config-risp /
# op-config-fwhq precondition-blocked): wait-dhcp fans into every config op
# and every flow fans into wait-dhcp.
_CASCADE_SPEC = {
    "name": "flow-cascade",
    "devices": [
        {"id": "r1", "role": "router"},
        {"id": "pc1", "role": "pc"},
    ],
    "links": [{"id": "l1", "a": {"device": "r1"}, "b": {"device": "pc1"}}],
    "subnets": [{"id": "lan", "cidr": "192.168.10.0/24", "dhcp": True}],
    "ip_assignments": [
        {"device": "r1", "interface_hint": "GigabitEthernet0/0",
         "subnet": "lan", "ip": "192.168.10.1"},
        {"device": "pc1", "subnet": "lan"},
    ],
    "acceptance_criteria": [
        {"id": "A-1", "requirement_ids": [],
         "test": {"kind": "flow", "source": "pc1", "destination": "r1",
                  "expect": "reachable"}},
    ],
}


def _autopilot(tmp_path, adapter):
    evidence = EvidenceStore(root=tmp_path / "evidence")
    return (
        Autopilot(
            adapter,
            SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=100)),
            evidence,
        ),
        evidence,
    )


class _BrokenRouterLivePlane(NullAdapter):
    """r1's config fails (the scripted channel outage) while the data plane
    answers every probe — the exact split the reference run had at 17:20:
    half the config blocked, three flows actually reachable."""

    def configure_ios(self, device, commands, operation_id=None):
        if device == "r1":
            return self._receipt(
                "configure_ios",
                {"device": device, "command_count": len(commands)},
                operation_id,
                OperationResult.REJECTED,
                error="channel outage: no live channel (scripted)",
            )
        return super().configure_ios(device, commands, operation_id=operation_id)

    def run_flow_test(
        self, source, destination, *, transport="icmp", port=None,
        destination_ip=None,
    ):
        return FlowResult(
            source=source,
            destination=destination,
            transport=transport,
            port=port,
            outcome=FlowOutcome.REACHABLE,
            detail="scripted ping ok",
        )


def test_flows_still_probe_when_an_unrelated_config_op_fails(tmp_path):
    """F-2: the blocked cascade must not swallow the probes.

    Before the fix: op-config-r1 FAILS -> op-wait-dhcp BLOCKED ->
    op-flow-A-1 BLOCKED -> execution.flow_results == {} -> the criterion is
    UNKNOWN with 'no flow result recorded' although the plane answers."""
    adapter = _BrokenRouterLivePlane()
    autopilot, _evidence = _autopilot(tmp_path, adapter)
    spec = NetworkSpec.model_validate(_CASCADE_SPEC)

    build, error = autopilot.build(spec)

    assert error is None and build is not None
    # the config failure itself is still real and still reported
    assert "op-config-r1" in build.execution["failed"], build.execution["failed"]
    # ...but the probe ran against the live plane instead of being blocked
    flows = build.execution["flow_results"]
    assert "pc1->r1" in flows, f"flow op never ran: {flows}"
    assert flows["pc1->r1"]["outcome"] == FlowOutcome.REACHABLE.value
    connectivity = build.verification.by_level("connectivity")
    assert connectivity, build.verification.summary
    assert any(t.verdict is Verdict.PASS for t in connectivity), [
        (t.test_id, t.verdict, t.observed_result) for t in connectivity
    ]


class _RepairTurnsThePlaneOn(NullAdapter):
    """The plane is silent until a repair executes, then it answers.

    fw's own config is a silent no-op — the command is accepted but the
    address is never observable, which is exactly the reference run's
    'config galat' shape and the evidence a firewall diagnosis needs. Only a
    repair operation ('op-repair-...') flips the plane switch, so the first
    verification sees a real FAIL and the repair's function check is the only
    source of a PASS."""

    def __init__(self) -> None:
        super().__init__()
        self.repair_seen = False
        self.flow_calls: list[tuple[str, str]] = []

    def _flip(self, operation_id) -> None:
        if operation_id and "repair" in str(operation_id):
            self.repair_seen = True

    def configure_ios(self, device, commands, operation_id=None):
        self._flip(operation_id)
        if device == "fw":
            return self._receipt(
                "configure_ios",
                {"device": device, "command_count": len(commands)},
                operation_id,
                OperationResult.SUCCESS,
                executed={"recorded": len(commands)},
            )
        return super().configure_ios(device, commands, operation_id=operation_id)

    def configure_gateway_lan(self, device, ip, mask, operation_id=None):
        self._flip(operation_id)
        return super().configure_gateway_lan(
            device, ip, mask, operation_id=operation_id
        )

    def configure_host(
        self, device, *, dhcp, ip=None, mask=None, gateway=None, dns=None,
        operation_id=None,
    ):
        self._flip(operation_id)
        return super().configure_host(
            device, dhcp=dhcp, ip=ip, mask=mask, gateway=gateway, dns=dns,
            operation_id=operation_id,
        )

    def run_flow_test(
        self, source, destination, *, transport="icmp", port=None,
        destination_ip=None,
    ):
        self.flow_calls.append((source, destination))
        outcome = (
            FlowOutcome.REACHABLE if self.repair_seen else FlowOutcome.BLOCKED
        )
        return FlowResult(
            source=source,
            destination=destination,
            transport=transport,
            port=port,
            outcome=outcome,
            detail="scripted plane: " + ("answers" if self.repair_seen else "silent"),
        )


def test_repair_flow_observations_reach_the_final_report(tmp_path):
    """F-1: the repair engine probes with the real adapter — its verdict must
    be what the post-repair re-verify reads.

    Before the fix the re-verify read the DAG-era BLOCKED observation, so a
    repair whose own probes proved 'reachable' was still reported as FAIL."""
    adapter = _RepairTurnsThePlaneOn()
    autopilot, evidence = _autopilot(tmp_path, adapter)
    spec = NetworkSpec.model_validate(_FIREWALL_FLOW_SPEC)

    build, error = autopilot.build(spec)

    assert error is None and build is not None
    # a firewall-class repair executed and its function check really probed
    executed = [r for r in build.repairs if r["outcome"].get("executed")]
    assert executed, [r["plan"].get("blocked_reason") for r in build.repairs]
    assert adapter.repair_seen, "no repair operation reached the adapter"
    assert adapter.flow_calls, "the function check never probed the plane"
    probed = [
        r for r in executed if r["outcome"].get("function_verdict") == "pass"
    ]
    assert probed, [
        (r["outcome"].get("function_verdict"), r["outcome"].get("function_detail"))
        for r in executed
    ]
    # F-1: execution.flow_results now carries the repair's fresh observation
    # (it still holds the DAG-era BLOCKED one without the merge)
    flows = build.execution["flow_results"]
    assert flows.get("pc1->srv", {}).get("outcome") == FlowOutcome.REACHABLE.value, flows
    # ...and the final report says the same thing
    connectivity = build.verification.by_level("connectivity")
    assert connectivity, build.verification.summary
    assert any(t.verdict is Verdict.PASS for t in connectivity), [
        (t.test_id, t.verdict, t.observed_result) for t in connectivity
    ]
