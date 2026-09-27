"""Bounded repair: diagnosis -> preview -> gate -> execute -> re-verify.

Hard limits (per §22): max_attempts repairs total, each with a capped number
of mutation operations, all through the Phase-1 SafetyGate. Rollback restores
the prior observed values of anything the repair changed (where the adapter
can write them back). When the diagnosis has no evidence-backed root cause,
repair is BLOCKED and says exactly why — nothing is mutated speculatively.

Re-verification has two halves (#15): read-back proves the symptom (the
address is there again), and — for a firewall-class repair — an actual flow
probe proves the function (traffic crosses). A firewall whose address reads
back while it forwards nothing is NOT reported as repaired.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..adapter.base import PacketTracerAdapter
from ..compiler.dag import OpStatus, OperationDag
from ..compiler.executor import DagExecutor
from ..compiler.operations import Check, MutationClassification, NetworkOperation, Risk, key_for
from ..core.ids import new_id, utc_now_iso
from ..core.network_spec import FlowTestSpec
from ..core.requirements import FlowOutcome, FlowResult, Verdict, aggregate_verdicts
from ..core.safety import MutationBudget, SafetyGate, SafetyMode
from ..diagnosis.engine import Diagnosis
from ..evidence.store import EvidenceStore
from ..ipam.planner import AddressPlan
from ..verification.engine import _flow_verdict

MAX_REPAIR_ATTEMPTS = 3
MAX_REPAIR_MUTATIONS = 5


@dataclass
class RepairPlan:
    repair_id: str
    diagnosis_id: str
    operations: tuple[NetworkOperation, ...]
    affected_resources: tuple[str, ...]
    expected_improvement: str
    risk: Risk
    rollback_strategy: str
    verification_criteria: tuple[str, ...]      # test_ids that must pass after
    blocked_reason: str | None = None

    @property
    def blocked(self) -> bool:
        return self.blocked_reason is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "repair_id": self.repair_id,
            "diagnosis_id": self.diagnosis_id,
            "operations": [op.to_dict() for op in self.operations],
            "affected_resources": list(self.affected_resources),
            "expected_improvement": self.expected_improvement,
            "risk": self.risk.value,
            "rollback_strategy": self.rollback_strategy,
            "verification_criteria": list(self.verification_criteria),
            "blocked": self.blocked,
            "blocked_reason": self.blocked_reason,
        }


@dataclass
class RepairOutcome:
    repair_id: str
    attempt: int
    executed: bool
    blocked_reason: str | None
    dag_summary: dict[str, int] = field(default_factory=dict)
    rolled_back: bool = False
    rollback_reason: str | None = None
    reobserved: bool = False
    # #15: what the re-verification actually proved about the FUNCTION.
    # None: this repair class has no functional bar (nothing to prove with
    # traffic). Otherwise a Verdict value: "pass" only when every probed flow
    # behaved as the spec expects, "fail" when one did not, "unknown" when no
    # flow could be probed at all — unknown never counts as repaired.
    function_verdict: str | None = None
    function_detail: str | None = None
    function_flows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def repaired(self) -> bool:
        """Reported as repaired only when what was broken is proven fixed.

        `executed` says the mutations ran and `reobserved` says state was read
        back — both are about the symptom (#15). For a firewall-class repair
        the function must also be shown by traffic: a FAIL (still forwarding
        nothing) or an UNKNOWN (nothing could be probed) keeps this False.
        """
        if not self.executed:
            return False
        if self.function_verdict is None:
            return True
        return self.function_verdict == Verdict.PASS.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "repair_id": self.repair_id,
            "attempt": self.attempt,
            "executed": self.executed,
            "blocked_reason": self.blocked_reason,
            "dag_summary": dict(self.dag_summary),
            "rolled_back": self.rolled_back,
            "rollback_reason": self.rollback_reason,
            "reobserved": self.reobserved,
            "function_verdict": self.function_verdict,
            "function_detail": self.function_detail,
            "function_flows": list(self.function_flows),
            "repaired": self.repaired,
        }


class RepairEngine:
    def __init__(
        self,
        adapter: PacketTracerAdapter,
        gate: SafetyGate,
        evidence: EvidenceStore | None = None,
        *,
        max_attempts: int = MAX_REPAIR_ATTEMPTS,
        max_mutations: int = MAX_REPAIR_MUTATIONS,
        spec: Any | None = None,
    ):
        # #29: a `source` proposal is rendered against this spec, so the repair
        # pushes the same config the build would have pushed.
        self._spec = spec
        self.adapter = adapter
        self.gate = gate
        self.evidence = evidence
        self.max_attempts = max_attempts
        self.max_mutations = max_mutations
        self.attempts = 0
        self._prior_values: dict[str, dict[str, Any]] = {}

    # -- planning -----------------------------------------------------------

    def plan_repair(
        self,
        diagnosis: Diagnosis,
        plan: AddressPlan | None = None,
    ) -> RepairPlan:
        if not diagnosis.repairable:
            reason = (
                f"REPAIR BLOCKED: confidence={diagnosis.confidence} "
                f"({'no proposed repair' if not diagnosis.proposed_repair else 'insufficient evidence'}); "
                f"{diagnosis.uncertainty or 'no evidence-backed root cause'}"
            )
            return RepairPlan(
                repair_id=new_id("repair"),
                diagnosis_id=diagnosis.diagnosis_id,
                operations=(),
                affected_resources=diagnosis.affected_resources,
                expected_improvement="none (blocked)",
                risk=Risk.LOW,
                rollback_strategy="n/a",
                verification_criteria=(),
                blocked_reason=reason,
            )
        if self.attempts >= self.max_attempts:
            return RepairPlan(
                repair_id=new_id("repair"),
                diagnosis_id=diagnosis.diagnosis_id,
                operations=(),
                affected_resources=diagnosis.affected_resources,
                expected_improvement="none (budget)",
                risk=Risk.LOW,
                rollback_strategy="n/a",
                verification_criteria=(),
                blocked_reason=(
                    f"REPAIR BLOCKED: attempt budget exhausted "
                    f"({self.attempts}/{self.max_attempts})"
                ),
            )

        operations: list[NetworkOperation] = []
        affected: set[str] = set()
        for index, proposal in enumerate(diagnosis.proposed_repair):
            op = self._materialize(proposal, plan, diagnosis, index)
            if op is None:
                return RepairPlan(
                    repair_id=new_id("repair"),
                    diagnosis_id=diagnosis.diagnosis_id,
                    operations=(),
                    affected_resources=diagnosis.affected_resources,
                    expected_improvement="none (unsupported)",
                    risk=Risk.LOW,
                    rollback_strategy="n/a",
                    verification_criteria=(),
                    blocked_reason=(
                        f"REPAIR BLOCKED: unsupported repair operation "
                        f"{proposal.get('operation_type')!r}"
                    ),
                )
            operations.append(op)
            affected.update(op.blast_radius)

        if len(operations) > self.max_mutations:
            return RepairPlan(
                repair_id=new_id("repair"),
                diagnosis_id=diagnosis.diagnosis_id,
                operations=(),
                affected_resources=diagnosis.affected_resources,
                expected_improvement="none (budget)",
                risk=Risk.MEDIUM,
                rollback_strategy="n/a",
                verification_criteria=(),
                blocked_reason=(
                    f"REPAIR BLOCKED: repair needs {len(operations)} mutations, "
                    f"budget allows {self.max_mutations}"
                ),
            )

        return RepairPlan(
            repair_id=new_id("repair"),
            diagnosis_id=diagnosis.diagnosis_id,
            operations=tuple(operations),
            affected_resources=tuple(sorted(affected)),
            expected_improvement=f"resolves: {diagnosis.root_cause}",
            risk=Risk.MEDIUM,
            rollback_strategy=(
                "restore prior observed values for changed fields where the "
                "adapter supports writing them; otherwise stop and report"
            ),
            verification_criteria=diagnosis.failed_tests,
        )

    def _materialize(
        self, proposal: dict[str, Any], plan: AddressPlan | None,
        diagnosis: Diagnosis, index: int,
    ) -> NetworkOperation | None:
        op_type = proposal.get("operation_type")
        inputs = dict(proposal.get("inputs") or {})
        op_id = f"op-repair-{diagnosis.diagnosis_id[-6:]}-{index}"
        if op_type == "create_device":
            return NetworkOperation(
                operation_id=op_id,
                operation_type="create_device",
                inputs=inputs,
                preconditions=(Check("device_absent", inputs["name"], "absent"),),
                expected_postconditions=(Check("device_present", inputs["name"], "present"),),
                classification=MutationClassification.CREATE,
                risk=Risk.LOW,
                blast_radius=(inputs["name"],),
                idempotency_key=key_for("create_device", inputs["name"], inputs.get("model")),
            )
        if op_type == "create_link":
            if not (inputs.get("a_port") and inputs.get("b_port")):
                return None
            return NetworkOperation(
                operation_id=op_id,
                operation_type="create_link",
                inputs=inputs,
                preconditions=(
                    Check("device_present", inputs["a_device"], "present"),
                    Check("device_present", inputs["b_device"], "present"),
                ),
                expected_postconditions=(Check("link_present", inputs["link_id"], "present"),),
                classification=MutationClassification.LINK,
                risk=Risk.LOW,
                blast_radius=(inputs["a_device"], inputs["b_device"]),
                idempotency_key=key_for(
                    "create_link",
                    tuple(sorted([(inputs["a_device"], inputs["a_port"]),
                                  (inputs["b_device"], inputs["b_port"])])),
                ),
            )
        if op_type == "reconfigure_address":
            if plan is None:
                return None
            completed = self._complete_address_inputs(inputs, plan)
            if completed is None:
                return None
            self._snapshot_prior(completed)
            if completed.get("_gateway"):
                operation_type = "configure_gateway_lan"
            elif completed.get("_host"):
                operation_type = "configure_host"
            else:
                operation_type = "configure_ios"
            return NetworkOperation(
                operation_id=op_id,
                operation_type=operation_type,
                inputs=completed["inputs"],
                preconditions=(Check("device_present", completed["inputs"]["device"], "present"),),
                classification=MutationClassification.CONFIGURE,
                risk=Risk.MEDIUM,
                blast_radius=(completed["inputs"]["device"],),
                idempotency_key=key_for("repair_address", completed["inputs"]),
            )
        if op_type == "ios_command":
            rendered = self._render_config(inputs, plan)
            if rendered is None:
                return None
            inputs = {**inputs, "commands": rendered}
            self._snapshot_prior({"device": inputs["device"]})
            return NetworkOperation(
                operation_id=op_id,
                operation_type="configure_ios",
                inputs=inputs,
                preconditions=(Check("device_present", inputs["device"], "present"),),
                classification=MutationClassification.CONFIGURE,
                risk=Risk.MEDIUM,
                blast_radius=(inputs["device"],),
                idempotency_key=key_for("repair_ios", inputs["device"], inputs["commands"]),
            )
        return None

    def _render_config(
        self, inputs: dict[str, Any], plan: AddressPlan | None
    ) -> list[str] | None:
        """Turn a `source` proposal into the real CLI, from the compiler (#29).

        A diagnosis must not invent CLI. The switch VLAN block and the ASA
        block are the compiler's business, so the proposal names the source and
        this re-compiles the spec and lifts the device's own `configure_ios`
        commands out of the DAG - the exact block the build would have pushed,
        so a repair cannot drift from a build. `None` means there is nothing to
        push, and the caller blocks the repair rather than sending an empty
        block that would look like a successful no-op.
        """
        source = inputs.get("source")
        if not source:
            return list(inputs.get("commands") or [])
        spec = getattr(self, "_spec", None)
        if spec is None or plan is None:
            return None
        device_id = str(inputs.get("device") or "")
        try:
            from ..compiler.compiler import compile_spec

            compiled = compile_spec(spec, plan)
        except Exception as exc:
            if self.evidence is not None:
                self.evidence.record(
                    "repair_render_failed",
                    {"device": device_id, "source": source, "error": str(exc)},
                    component="repair",
                )
            return None
        for node in compiled.dag.nodes.values():
            op = node.operation
            if op.operation_type != "configure_ios":
                continue
            if str(op.inputs.get("device")) != device_id:
                continue
            commands = [str(c) for c in (op.inputs.get("commands") or [])]
            if commands:
                return commands
        return None

    def _complete_address_inputs(
        self, inputs: dict[str, Any], plan: AddressPlan
    ) -> dict[str, Any] | None:
        device = inputs["device"]
        iface = inputs.get("interface")
        # #29: a DHCP client has no address to write, so the repair is a fresh
        # DHCP request - not a static address the spec never asked for.
        dhcp_assignment = next(
            (
                a for a in plan.assignments
                if a.device == device and getattr(a, "dhcp", False)
            ),
            None,
        )
        if dhcp_assignment is not None:
            return {
                "_host": True,
                "inputs": {
                    "device": device,
                    "dhcp": True,
                    "interface": iface,
                },
            }
        assignment = next(
            (
                a for a in plan.assignments
                if a.device == device and a.ip is not None
                and (iface is None or a.interface_hint == iface)
            ),
            None,
        )
        if assignment is None and iface == "FastEthernet0":
            # Host devices have no interface hint in the plan; verification
            # reports the single host NIC as "FastEthernet0" (see the
            # verification engine). Fall back to the device's hint-less
            # assignment so host addressing is actually repairable.
            assignment = next(
                (
                    a for a in plan.assignments
                    if a.device == device and a.ip is not None
                    and a.interface_hint is None
                ),
                None,
            )
        if assignment is None:
            return None
        allocation = plan.allocation_by_subnet(assignment.subnet_id)
        if allocation is None:
            return None
        if assignment.interface_hint and "." in assignment.interface_hint:
            return None  # subinterface repair unsupported in Phase 2
        # #10: PT's Linksys takes no CLI (configureIosDevice answers `true`
        # and applies nothing - live probe) and configurePcIp is unproven on
        # it, so its LAN address moves through the port API - the same push
        # the build's configure_gateway_lan op uses.
        if (
            self._spec is not None
            and _role_of(self._spec, device) == "wireless_router"
        ):
            return {
                "_gateway": True,
                "inputs": {
                    "device": device,
                    "ip": assignment.ip,
                    "mask": allocation.netmask,
                },
            }
        is_router = iface is not None and iface != "FastEthernet0" and assignment.interface_hint != "FastEthernet0"
        # Router interface vs host NIC: use configure_ios for anything that is
        # not the single host NIC.
        if is_router:
            return {
                "_host": False,
                "inputs": {
                    "device": device,
                    "commands": [
                        f"interface {assignment.interface_hint}",
                        f"ip address {assignment.ip} {allocation.netmask}",
                        "no shutdown",
                    ],
                },
            }
        return {
            "_host": True,
            "inputs": {
                "device": device,
                "dhcp": False,
                "ip": assignment.ip,
                "mask": allocation.netmask,
                "gateway": assignment.gateway,
            },
        }

    def _snapshot_prior(self, inputs: dict[str, Any]) -> None:
        """Capture the values we can restore if the repair regresses."""
        device = inputs.get("device")
        if not device or device in self._prior_values:
            return
        observation = self.adapter.inspect_device(device)
        snapshot: dict[str, Any] = {"device": device, "ports": {}}
        for name, port in observation.ports.items():
            snapshot["ports"][name] = {
                "ip": port.ip.value if port.ip.is_known else None,
                "mask": port.mask.value if port.mask.is_known else None,
                "gateway": port.gateway.value if port.gateway.is_known else None,
            }
        self._prior_values[device] = snapshot

    # -- execution ------------------------------------------------------------

    def execute(self, repair_plan: RepairPlan) -> RepairOutcome:
        outcome = RepairOutcome(
            repair_id=repair_plan.repair_id,
            attempt=self.attempts + 1,
            executed=False,
            blocked_reason=repair_plan.blocked_reason,
        )
        if repair_plan.blocked:
            if self.evidence is not None:
                self.evidence.record("repair_blocked", repair_plan.to_dict(), component="repair")
            return outcome
        if self.attempts >= self.max_attempts:
            outcome.blocked_reason = (
                f"repair attempt budget exhausted ({self.attempts}/{self.max_attempts})"
            )
            return outcome

        self.attempts += 1
        dag = OperationDag()
        for op in repair_plan.operations:
            dag.add(op)
        dag.validate()

        executor = DagExecutor(self.adapter, self.gate, self.evidence)
        result = executor.execute(dag)
        outcome.dag_summary = dag.summary()
        outcome.reobserved = True
        outcome.executed = dag.summary()["failed"] == 0 and dag.summary()["blocked"] == 0

        if outcome.executed:
            # #15: read-back only proves the symptom; a firewall-class repair
            # must also show the traffic it is responsible for actually flows.
            self._verify_function(repair_plan, outcome)

        if self.evidence is not None:
            self.evidence.record(
                "repair_attempt",
                {"plan": repair_plan.to_dict(), "outcome": outcome.to_dict()},
                component="repair",
            )

        # What was proven is on the outcome: read-back for the symptom and,
        # for a firewall-class repair, the probed flows for the function.
        # `repaired` only turns True once that function check passes (§45:
        # prove it); the caller still re-verifies the report as a whole.
        return outcome

    # -- function verification (#15) ------------------------------------------

    def _firewall_of(self, repair_plan: RepairPlan) -> str | None:
        """Which firewall this plan mutates, or None when it is not one.

        Two honest signals: the role the spec gives a device the plan touches,
        and the `firewall_config` source stamp #29 leaves on the operation
        inputs of a proposal that re-pushes a compiled ASA block (the stamp
        survives materialisation, so it is still visible on the operation).
        """
        devices: list[str] = []
        for op in repair_plan.operations:
            device = str(op.inputs.get("device") or op.inputs.get("name") or "")
            if op.inputs.get("source") == "firewall_config":
                return device or "<unknown>"
            if device:
                devices.append(device)
        devices.extend(repair_plan.affected_resources)
        spec = self._spec
        if spec is None:
            return None
        for device_id in dict.fromkeys(devices):
            if _role_of(spec, device_id) == "firewall":
                return device_id.partition(":")[0]
        return None

    @staticmethod
    def _function_criteria(spec: Any, firewall: str) -> list[Any]:
        """Flow criteria whose traffic has to cross the repaired firewall.

        Linkage, in order of strength:
        1. the firewall is an endpoint of the flow;
        2. the endpoints sit on different segments and the firewall owns an
           address on one of them — traffic between segments has to traverse
           the box that connects them;
        3. no honest linkage: every flow criterion of the spec, bounded by
           their count, because an unlinked flow that still fails is
           information, not a reason to stay quiet.

        Policies are deliberately NOT used to select criteria: PT only
        enforces wildcard ACLs (see compiler/firewall_policy.py), so a policy
        that `apply_to` the firewall names no particular flow.
        """
        flows = [
            criterion for criterion in spec.acceptance_criteria
            if isinstance(criterion.test, FlowTestSpec)
        ]
        endpoint = [
            c for c in flows if firewall in (c.test.source, c.test.destination)
        ]
        if endpoint:
            return endpoint
        firewall_subnets = {
            a.subnet for a in spec.ip_assignments if a.device == firewall
        }
        if not firewall_subnets:
            return flows

        def segments(device: str) -> set[str]:
            return {a.subnet for a in spec.ip_assignments if a.device == device}

        crossing: list[Any] = []
        for criterion in flows:
            source = segments(criterion.test.source)
            destination = segments(criterion.test.destination)
            if not source or not destination or source == destination:
                continue  # unknown endpoint, or both on the same segment
            if (source | destination) & firewall_subnets:
                crossing.append(criterion)
        return crossing or flows

    def _verify_function(
        self, repair_plan: RepairPlan, outcome: RepairOutcome
    ) -> None:
        """Probe the traffic a firewall-class repair is meant to restore (#15).

        Runs only after the mutations executed: read-back already proved the
        symptom, and this proves the function with the same adapter flow
        machinery the build and the drift check use. When no criterion can be
        probed the outcome says UNKNOWN — unverified, never repaired.
        """
        firewall = self._firewall_of(repair_plan)
        if firewall is None:
            return  # not a firewall-class repair: no functional bar to add
        spec = self._spec
        criteria = self._function_criteria(spec, firewall) if spec is not None else []
        if not criteria:
            outcome.function_verdict = Verdict.UNKNOWN.value
            outcome.function_detail = (
                "no flow criterion can prove the firewall forwards traffic; "
                "the repair stays unverified (#15)"
            )
            self._record_function_check(outcome, firewall, [])
            return

        verdicts: list[Verdict] = []
        for criterion in criteria:
            test = criterion.test
            try:
                flow = self.adapter.run_flow_test(
                    test.source,
                    test.destination,
                    transport=test.transport,
                    port=test.port,
                    destination_ip=getattr(test, "destination_ip", None),
                )
            except Exception as exc:  # a dead channel must not fake a pass
                flow = FlowResult(
                    source=test.source,
                    destination=test.destination,
                    transport=test.transport,
                    port=test.port,
                    outcome=FlowOutcome.UNKNOWN,
                    detail=f"probe could not run: {exc}",
                )
            verdict = _flow_verdict(test.expect, flow.outcome)
            verdicts.append(verdict)
            outcome.function_flows.append(
                {
                    "criterion_id": criterion.id,
                    "source": test.source,
                    "destination": test.destination,
                    "expect": test.expect,
                    "outcome": flow.outcome.value,
                    "verdict": verdict.value,
                    "detail": flow.detail,
                }
            )
        overall = aggregate_verdicts(verdicts)
        outcome.function_verdict = overall.value
        not_passing = [
            f["criterion_id"]
            for f in outcome.function_flows
            if f["verdict"] != Verdict.PASS.value
        ]
        probed = len(outcome.function_flows)
        if overall is Verdict.PASS:
            outcome.function_detail = (
                f"{probed} flow probe(s) re-run after the repair: traffic "
                "behaves as the spec expects"
            )
        elif overall is Verdict.FAIL:
            outcome.function_detail = (
                "traffic still wrong after the repair: "
                + ", ".join(not_passing)
            )
        else:
            outcome.function_detail = (
                "flow probes inconclusive for: " + ", ".join(not_passing)
            )
        self._record_function_check(outcome, firewall, outcome.function_flows)

    def _record_function_check(
        self, outcome: RepairOutcome, firewall: str, flows: list[dict[str, Any]]
    ) -> None:
        if self.evidence is None:
            return
        self.evidence.record(
            "repair_function_check",
            {
                "repair_id": outcome.repair_id,
                "firewall": firewall,
                "verdict": outcome.function_verdict,
                "detail": outcome.function_detail,
                "flows": list(flows),
            },
            component="repair",
        )

    def rollback(self, repair_plan: RepairPlan) -> RepairOutcome:
        """Restore prior observed values where the adapter can write them."""
        outcome = RepairOutcome(
            repair_id=repair_plan.repair_id,
            attempt=self.attempts,
            executed=False,
            blocked_reason=None,
        )
        if not self._prior_values:
            outcome.blocked_reason = "rollback unavailable: no prior values were captured"
            if self.evidence is not None:
                self.evidence.record("rollback_failed", outcome.to_dict(), component="repair")
            return outcome
        try:
            for device, snapshot in self._prior_values.items():
                # #10: a Linksys cannot be restored through configure_host
                # (configurePcIp is unproven on it) - its prior Vlan1 address
                # goes back through the port API, like every other gateway
                # write. A port whose mask was never read stays untouched:
                # pushing half an address would be worse than not restoring.
                gateway = (
                    self._spec is not None
                    and _role_of(self._spec, device) == "wireless_router"
                )
                for _port, values in snapshot["ports"].items():
                    if values["ip"] is None:
                        continue
                    if gateway:
                        if values["mask"] is None:
                            continue
                        self.adapter.configure_gateway_lan(
                            device, values["ip"], values["mask"]
                        )
                    else:
                        self.adapter.configure_host(
                            device,
                            dhcp=False,
                            ip=values["ip"],
                            mask=values["mask"],
                            gateway=values["gateway"],
                        )
            outcome.rolled_back = True
        except Exception as exc:
            outcome.blocked_reason = f"rollback failed: {exc}"
        if self.evidence is not None:
            self.evidence.record(
                "rollback", outcome.to_dict(), component="repair"
            )
        return outcome


def _role_of(spec: Any, device_id: str) -> str:
    """The spec's role for a device, tolerating a `device:port` resource id."""
    device = spec.device_by_id(device_id.partition(":")[0])
    return str(getattr(getattr(device, "role", None), "value", ""))
