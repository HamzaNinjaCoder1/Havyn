"""Bounded repair: diagnosis -> preview -> gate -> execute -> re-verify.

Hard limits (per §22): max_attempts repairs total, each with a capped number
of mutation operations, all through the Phase-1 SafetyGate. Rollback restores
the prior observed values of anything the repair changed (where the adapter
can write them back). When the diagnosis has no evidence-backed root cause,
repair is BLOCKED and says exactly why — nothing is mutated speculatively.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..adapter.base import PacketTracerAdapter
from ..compiler.dag import OpStatus, OperationDag
from ..compiler.executor import DagExecutor
from ..compiler.operations import Check, MutationClassification, NetworkOperation, Risk, key_for
from ..core.ids import new_id, utc_now_iso
from ..core.safety import MutationBudget, SafetyGate, SafetyMode
from ..diagnosis.engine import Diagnosis
from ..evidence.store import EvidenceStore
from ..ipam.planner import AddressPlan

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
    ):
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
            return NetworkOperation(
                operation_id=op_id,
                operation_type="configure_host" if completed.get("_host") else "configure_ios",
                inputs=completed["inputs"],
                preconditions=(Check("device_present", completed["inputs"]["device"], "present"),),
                classification=MutationClassification.CONFIGURE,
                risk=Risk.MEDIUM,
                blast_radius=(completed["inputs"]["device"],),
                idempotency_key=key_for("repair_address", completed["inputs"]),
            )
        if op_type == "ios_command":
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

    def _complete_address_inputs(
        self, inputs: dict[str, Any], plan: AddressPlan
    ) -> dict[str, Any] | None:
        device = inputs["device"]
        iface = inputs.get("interface")
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

        if self.evidence is not None:
            self.evidence.record(
                "repair_attempt",
                {"plan": repair_plan.to_dict(), "outcome": outcome.to_dict()},
                component="repair",
            )

        # success is NOT claimed here: the caller must re-verify and only then
        # declare the repair effective (§45: prove it).
        return outcome

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
                for _port, values in snapshot["ports"].items():
                    if values["ip"] is not None:
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
