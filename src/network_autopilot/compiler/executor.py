"""The DAG executor: safety gate -> adapter -> receipts -> observed state.

Contract:
- every mutating batch passes the SafetyGate first (READ_ONLY rejects,
  DRY_RUN plans, APPROVAL_REQUIRED waits, AUTONOMOUS consumes budget);
- operations run in the DAG's deterministic topological order;
- a failed operation BLOCKS its dependents — they are recorded, never tried;
- create operations are idempotent: the executor observes first and REUSES an
  existing device instead of blindly recreating it;
- every executed operation leaves a receipt in the evidence chain;
- after mutations the executor re-observes the workspace into the shared
  ActualNetworkState. A receipt is "the command was accepted"; the
  observation is "what is actually visible" — both are kept, never merged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..adapter.base import PacketTracerAdapter
from ..core.actual_state import ActualNetworkState
from ..core.ids import utc_now_iso
from ..core.receipts import OperationReceipt, OperationResult
from ..core.requirements import FlowOutcome, FlowResult
from ..core.safety import SafetyGate
from ..evidence.store import EvidenceStore
from .dag import OpStatus, OperationDag
from .operations import MutationClassification
from .port_reservation import PortReservationSystem

_FLOW_TYPES = ("verify_flow",)


@dataclass
class ExecutionResult:
    dag: OperationDag
    state: ActualNetworkState
    flow_results: dict[tuple[str, str], FlowResult] = field(default_factory=dict)
    receipts: list[OperationReceipt] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    reused: list[str] = field(default_factory=list)
    preflight_blocked: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dag_summary": self.dag.summary(),
            "flow_results": {
                f"{src}->{dst}": {
                    "outcome": r.outcome.value,
                    "detail": r.detail,
                    "evidence": r.evidence,
                }
                for (src, dst), r in self.flow_results.items()
            },
            "receipts": [r.to_dict() for r in self.receipts],
            "blocked": list(self.blocked),
            "failed": list(self.failed),
            "reused": list(self.reused),
            "preflight_blocked": list(self.preflight_blocked),
            "state_devices": sorted(self.state.devices),
        }


class DagExecutor:
    def __init__(
        self,
        adapter: PacketTracerAdapter,
        gate: SafetyGate,
        evidence: EvidenceStore | None = None,
        reservation_system: PortReservationSystem | None = None,
    ):
        self.adapter = adapter
        self.gate = gate
        self.evidence = evidence
        self.reservation_system = reservation_system or PortReservationSystem()
        self.state = ActualNetworkState(
            source=f"executor:{adapter.name}", observed_at=utc_now_iso()
        )

    # -- public API -----------------------------------------------------------

    def execute(self, dag: OperationDag, *, filename_hint: str | None = None) -> ExecutionResult:
        result = ExecutionResult(dag=dag, state=self.state)
        topo = list(dag.topo_order())
        for op_id in topo:
            node = dag.nodes[op_id]
            if node.status is not OpStatus.PENDING:
                continue  # already FAILED/BLOCKED/SKIPPED
            op = node.operation

            # dependency gate: anything not SUCCEEDED (and not SKIPPED) blocks
            unmet = [
                dep
                for dep in op.dependencies
                if dag.nodes[dep].status
                not in (OpStatus.SUCCEEDED, OpStatus.SKIPPED)
            ]
            if unmet:
                node.status = OpStatus.BLOCKED
                node.note = f"dependencies not satisfied: {unmet}"
                continue

            try:
                self._execute_one(op, node, result)
            except Exception as exc:  # adapter crash must not kill the run
                dag.mark_failed(op_id, f"executor exception: {exc}")
                continue

        # The DAG is the single source of truth for outcomes; the result lists
        # are derived from it so mark_failed's transitive blocking is included.
        result.failed = [op_id for op_id in topo if dag.nodes[op_id].status is OpStatus.FAILED]
        result.blocked = [op_id for op_id in topo if dag.nodes[op_id].status is OpStatus.BLOCKED]
        self.state.observed_at = utc_now_iso()
        return result

    # -- dispatch -------------------------------------------------------------

    def _execute_one(self, op, node, result: ExecutionResult) -> None:
        dispatch = {
            "create_device": self._op_create_device,
            "create_link": self._op_create_link,
            "configure_ios": self._op_configure_ios,
            "configure_host": self._op_configure_host,
            "verify_flow": self._op_verify_flow,
            "verify_state": self._op_verify_state,
            "save_project": self._op_save_project,
        }
        handler = dispatch.get(op.operation_type)
        if handler is None:
            dag.mark_failed(op.operation_id, f"unknown operation type {op.operation_type!r}")
            return
        handler(op, node, result)

    def _authorize(self, op) -> bool:
        """Pass the operation through the safety gate. Returns execute?

        For link operations, also check port reservations (§9).
        """
        permission = self.gate.authorize([op.to_dict()])
        if not permission.execute:
            return False
        # Port reservation check for links (§9)
        if op.operation_type == "create_link":
            a_dev = op.inputs.get("a_device", "")
            a_port = op.inputs.get("a_port", "")
            b_dev = op.inputs.get("b_device", "")
            b_port = op.inputs.get("b_port", "")
            for dev, iface in ((a_dev, a_port), (b_dev, b_port)):
                if iface and self.reservation_system.is_reserved(dev, iface):
                    existing = self.reservation_system.get_reservation(dev, iface)
                    if existing and existing.operation_id != op.operation_id:
                        return False
        return True

    def _record(self, receipt: OperationReceipt) -> None:
        if self.evidence is not None:
            self.evidence.record(
                "operation_receipt", receipt.to_dict(), component="executor"
            )

    # -- operation handlers ----------------------------------------------------

    def _op_create_device(self, op, node, result: ExecutionResult) -> None:
        if not self._authorize(op):
            node.status = OpStatus.BLOCKED
            node.note = "safety gate did not authorize execution"
            result.blocked.append(op.operation_id)
            return
        name = op.inputs["name"]
        exists = self.adapter.device_exists(name)
        if exists.is_known and exists.value:
            # IDEMPOTENT REUSE: observe → compare → reuse. Never recreate.
            node.status = OpStatus.SKIPPED
            node.note = "device already exists; reused instead of recreated"
            result.reused.append(op.operation_id)
            self._record(
                OperationReceipt(
                    operation_id=op.operation_id,
                    operation="create_device",
                    requested=op.inputs,
                    result=OperationResult.NOT_EXECUTED,
                    started_at=utc_now_iso(),
                    finished_at=utc_now_iso(),
                    evidence={"decision": "reused existing device"},
                )
            )
            return
        receipt = self.adapter.create_device(
            name,
            op.inputs["role"],
            model_hint=op.inputs.get("model"),
            position=tuple(op.inputs["position"]) if op.inputs.get("position") else None,
            operation_id=op.operation_id,
        )
        self._record(receipt)
        node.receipt = receipt
        if receipt.confirmed or (receipt.executed_ok and receipt.result is OperationResult.SUCCESS):
            node.status = OpStatus.SUCCEEDED
        else:
            self._fail(node, op.operation_id, result, receipt.error or "create failed")

    def _op_create_link(self, op, node, result: ExecutionResult) -> None:
        if not self._authorize(op):
            node.status = OpStatus.BLOCKED
            node.note = "safety gate did not authorize execution"
            result.blocked.append(op.operation_id)
            return
        a_device, a_port = op.inputs["a_device"], op.inputs.get("a_port")
        b_device, b_port = op.inputs["b_device"], op.inputs.get("b_port")
        if a_port and b_port:
            exists = self.adapter.link_exists(a_device, a_port, b_device, b_port)
            if exists.is_known and exists.value:
                node.status = OpStatus.SKIPPED
                node.note = "link already present; reused instead of recreated"
                result.reused.append(op.operation_id)
                self._record(
                    OperationReceipt(
                        operation_id=op.operation_id,
                        operation="create_link",
                        requested=op.inputs,
                        result=OperationResult.NOT_EXECUTED,
                        started_at=utc_now_iso(),
                        finished_at=utc_now_iso(),
                        evidence={"decision": "reused existing link"},
                    )
                )
                return
        receipt = self.adapter.create_link(
            a_device, a_port, b_device, b_port,
            media=op.inputs.get("media", "auto"),
            operation_id=op.operation_id,
        )
        self._record(receipt)
        node.receipt = receipt
        if receipt.executed_ok:
            node.status = OpStatus.SUCCEEDED
        else:
            self._fail(node, op.operation_id, result, receipt.error or "link failed")

    def _op_configure_ios(self, op, node, result: ExecutionResult) -> None:
        if not self._authorize(op):
            node.status = OpStatus.BLOCKED
            node.note = "safety gate did not authorize execution"
            result.blocked.append(op.operation_id)
            return
        receipt = self.adapter.configure_ios(
            op.inputs["device"], op.inputs["commands"], operation_id=op.operation_id
        )
        self._record(receipt)
        node.receipt = receipt
        # IOS acceptance may be unobservable (receipt unconfirmed) — that is
        # still a SUCCESS for DAG purposes; verification will judge reality.
        if receipt.executed_ok:
            node.status = OpStatus.SUCCEEDED
            if not receipt.confirmed:
                node.note = (
                    "executed; IOS acceptance not observable — judged by verification"
                )
        else:
            self._fail(node, op.operation_id, result, receipt.error or "configuration failed")

    def _op_configure_host(self, op, node, result: ExecutionResult) -> None:
        if not self._authorize(op):
            node.status = OpStatus.BLOCKED
            node.note = "safety gate did not authorize execution"
            result.blocked.append(op.operation_id)
            return
        receipt = self.adapter.configure_host(
            op.inputs["device"],
            dhcp=op.inputs.get("dhcp", False),
            ip=op.inputs.get("ip"),
            mask=op.inputs.get("mask"),
            gateway=op.inputs.get("gateway"),
            operation_id=op.operation_id,
        )
        self._record(receipt)
        node.receipt = receipt
        if receipt.executed_ok:
            node.status = OpStatus.SUCCEEDED
        else:
            self._fail(node, op.operation_id, result, receipt.error or "host config failed")

    def _op_verify_flow(self, op, node, result: ExecutionResult) -> None:
        source = op.inputs["source"]
        destination = op.inputs["destination"]
        flow = self.adapter.run_flow_test(source, destination)
        result.flow_results[(source, destination)] = flow
        if self.evidence is not None:
            self.evidence.record(
                "flow_result",
                {
                    "criterion_id": op.inputs.get("criterion_id"),
                    "source": source,
                    "destination": destination,
                    "expect": op.inputs.get("expect"),
                    "outcome": flow.outcome.value,
                    "detail": flow.detail,
                    "evidence": flow.evidence,
                },
                component="executor",
            )
        node.status = OpStatus.SUCCEEDED
        node.note = f"flow observed: {flow.outcome.value}"

    def _op_verify_state(self, op, node, result: ExecutionResult) -> None:
        names = op.inputs.get("device_names") or []
        observed = self.adapter.observe_state(names)
        self.state.merge(observed)
        self.state.observed_at = utc_now_iso()
        if self.evidence is not None:
            self.evidence.record(
                "observation",
                {"state": self.state.to_dict(), "unknowns": self.state.unknown_summary()},
                component="executor",
            )
        node.status = OpStatus.SUCCEEDED
        node.note = f"observed {len(self.state.devices)} device(s)"

    def _op_save_project(self, op, node, result: ExecutionResult) -> None:
        if not self._authorize(op):
            node.status = OpStatus.BLOCKED
            node.note = "safety gate did not authorize execution"
            result.blocked.append(op.operation_id)
            return
        receipt = self.adapter.save_project(
            op.inputs["filename"], operation_id=op.operation_id
        )
        self._record(receipt)
        node.receipt = receipt
        if receipt.executed_ok:
            node.status = OpStatus.SUCCEEDED
        else:
            self._fail(node, op.operation_id, result, receipt.error or "save failed")

    def _fail(self, node, op_id: str, result: ExecutionResult, note: str) -> None:
        node.status = OpStatus.FAILED
        node.note = note
        result.failed.append(op_id)
