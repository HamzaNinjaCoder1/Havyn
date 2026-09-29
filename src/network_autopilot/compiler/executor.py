"""The DAG executor: safety gate -> adapter -> receipts -> observed state.

Contract:
- every mutating batch passes the SafetyGate first (READ_ONLY rejects,
  DRY_RUN plans, APPROVAL_REQUIRED waits, AUTONOMOUS consumes budget);
- operations run in the DAG's deterministic topological order;
- a failed operation BLOCKS its dependents â€” they are recorded, never tried;
- create operations are idempotent: the executor observes first and REUSES an
  existing device instead of blindly recreating it;
- every executed operation leaves a receipt in the evidence chain;
- after mutations the executor re-observes the workspace into the shared
  ActualNetworkState. A receipt is "the command was accepted"; the
  observation is "what is actually visible" â€” both are kept, never merged.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..adapter.base import PacketTracerAdapter
from ..core.actual_state import ActualNetworkState
from ..core.ids import utc_now_iso
from ..core.receipts import CheckResult, OperationReceipt, OperationResult
from ..core.requirements import FlowOutcome, FlowResult
from ..core.safety import SafetyGate, SafetyMode
from ..evidence.store import EvidenceStore
from ..serial import read_serial_clock_evidence
from .checks import CheckEvaluator
from .dag import OpStatus, OperationDag
from .operations import MutationClassification
from .port_reservation import PortReservationSystem

_FLOW_TYPES = ("verify_flow",)

#: S1.2/F-2: operations that PROBE the network instead of changing it. Their
#: dependency gate waives unmet upstreams (see execute()): a blocked config
#: op means the network may be half-built, which is exactly what a probe
#: should report rather than hide behind.
_PROBE_TYPES = frozenset({"verify_flow", "wait_dhcp"})

#: ...but only in modes where execution actually happens. A session that
#: mutates nothing has nothing to probe: every mutating dep is a policy
#: refusal, not a failure, and a "dry" run must not start pinging live PTs.
_NON_EXECUTING_MODES = frozenset({SafetyMode.READ_ONLY, SafetyMode.DRY_RUN})


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
    #: Wall seconds spent per operation_type, measured with a monotonic clock
    #: around each dispatch (bulk runs count once for the whole run). The
    #: receipts' started_at/finished_at are stamped at construction and are
    #: always equal, so this is the only honest family breakdown (S1.5).
    op_seconds: dict[str, float] = field(default_factory=dict)

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
            "op_seconds": dict(sorted(self.op_seconds.items())),
            "state_devices": sorted(self.state.devices),
        }


_DHCP_POLL_INTERVAL_S = 0.4


def _client_has_address(state: ActualNetworkState, device: str) -> bool:
    """Does this device currently answer on at least one interface?

    `0.0.0.0` is PT's placeholder (every PC exposes a Bluetooth NIC reading
    it), not a lease — counted as an answer, the DHCP gate passed on poll 1
    for leaseless clients and the flows probed them three seconds later
    (build-ee447a04376b, A-001/A-002 UNKNOWN). Same filter as the ping
    resolver: a real address only.
    """
    observation = state.devices.get(device)
    if observation is None:
        return False
    return any(
        port.ip.is_known
        and bool(port.ip.value)
        and str(port.ip.value) != "0.0.0.0"
        for port in observation.ports.values()
    )


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
        # idempotency_key -> operation_id that already succeeded with it. The
        # compiler stamps every op with one; enforcing it here makes the key
        # load-bearing instead of decorative (P2-4): the same logical operation
        # can never execute twice in a run, and the receipts carry the key so a
        # re-run can reconcile against it instead of blindly re-sending CLI.
        self.executed_keys: dict[str, str] = {}
        # The DAG currently being executed (set by execute()). The
        # precondition/postcondition evaluators read link endpoints from it.
        self._dag: OperationDag | None = None

    # -- public API -----------------------------------------------------------

    def execute(self, dag: OperationDag, *, filename_hint: str | None = None) -> ExecutionResult:
        result = ExecutionResult(dag=dag, state=self.state)
        self._dag = dag
        topo = list(dag.topo_order())
        index = 0
        while index < len(topo):
            op_id = topo[index]
            node = dag.nodes[op_id]
            if node.status is not OpStatus.PENDING:
                index += 1
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
                waived = (
                    op.operation_type in _PROBE_TYPES
                    and self.gate.mode not in _NON_EXECUTING_MODES
                )
                if waived:
                    # S1.2/F-2: a read-only probe must report the half-built
                    # network, not hide behind an unrelated config op's
                    # blockage - that cascade turned 11 real flows into
                    # UNKNOWN in the reference run (flows-unknown.md §2).
                    # The probe runs; its own result carries the truth.
                    # READ_ONLY/DRY_RUN stay exempt: that build never
                    # executed, so probing a network nobody tried to build
                    # is noise (and a live ping storm in a "dry" run).
                    if self.evidence is not None:
                        self.evidence.record(
                            "probe_waived_dependencies",
                            {"operation_id": op_id, "unmet": list(unmet)},
                            component="executor",
                        )
                else:
                    node.status = OpStatus.BLOCKED
                    node.note = f"dependencies not satisfied: {unmet}"
                    index += 1
                    continue

            # Fast path: a run of independent operations of the same kind is
            # sent to PT as ONE command. Creating twenty hosts, cabling forty
            # links or addressing twenty PCs one round-trip each is the single
            # largest cost in a large build, and these operations have no
            # dependencies among themselves, so serialising them buys nothing.
            bulk = self._bulk_handler(op.operation_type)
            if bulk is not None:
                t_op = time.monotonic()
                consumed = self._try_bulk(topo, index, dag, result, op.operation_type, bulk)
                self._time_op(result, op.operation_type, t_op)
                if consumed:
                    index += consumed
                    continue

            t_op = time.monotonic()
            try:
                self._execute_one(op, node, result)
            except Exception as exc:  # adapter crash must not kill the run
                dag.mark_failed(op_id, f"executor exception: {exc}")
                self._record_failure_receipt(node, op_id, f"executor exception: {exc}")
            finally:
                self._time_op(result, op.operation_type, t_op)
            index += 1

        # The DAG is the single source of truth for outcomes; the result lists
        # are derived from it so mark_failed's transitive blocking is included.
        result.failed = [op_id for op_id in topo if dag.nodes[op_id].status is OpStatus.FAILED]
        result.blocked = [op_id for op_id in topo if dag.nodes[op_id].status is OpStatus.BLOCKED]
        self.state.observed_at = utc_now_iso()
        return result

    @staticmethod
    def _time_op(result: ExecutionResult, op_type: str, started: float) -> None:
        """Attribute monotonic wall time since ``started`` to ``op_type``."""
        elapsed = time.monotonic() - started
        result.op_seconds[op_type] = result.op_seconds.get(op_type, 0.0) + elapsed

    # The minimum run length that makes batching worth it. Below this the
    # serial path is already cheap and a batch would add a read-back for no
    # gain; above it the shared round-trip is a large win.
    _BULK_MIN_RUN = 4

    def _bulk_handler(self, op_type: str):
        """The adapter's bulk method for this operation type, or None.

        A run is only batched when the operation carries no dependency on
        another operation in the same run, because batching merges the
        transport, not the semantics: ordering and fail-closed per-op
        behaviour are preserved.
        """
        return {
            "create_device": getattr(self.adapter, "create_devices_bulk", None),
            "create_link": getattr(self.adapter, "create_links_bulk", None),
            "configure_host": getattr(self.adapter, "configure_hosts_bulk", None),
        }.get(op_type)

    def _try_bulk(self, topo, index, dag, result, op_type: str, bulk) -> int:
        """Run a leading group of independent same-kind ops in one PT command.

        Returns how many topological entries were consumed (0 when the fast
        path does not apply, so the caller falls back to the per-op path).
        The per-item receipts, safety-gate decisions and read-backs match the
        serial path; only the transport is shared.
        """
        run: list = []
        position = index
        while position < len(topo):
            node = dag.nodes[topo[position]]
            op = node.operation
            if op.operation_type != op_type or node.status is not OpStatus.PENDING:
                break
            # Every dependency must already be satisfied, and must not be
            # another member of this run (which would create an ordering the
            # batch would lose). At this point in the loop all earlier ops have
            # run, so satisfied deps are the common case.
            if any(
                dag.nodes[dep].status not in (OpStatus.SUCCEEDED, OpStatus.SKIPPED)
                for dep in op.dependencies
            ):
                break
            run.append((topo[position], node, op))
            position += 1

        if len(run) < self._BULK_MIN_RUN or bulk is None:
            return 0

        # Authorize every item first: a build must not mutate anything the
        # safety gate would refuse, batched or not.
        authorized: list[tuple] = []
        refused: list[tuple] = []
        for op_id, node, op in run:
            if self._authorize(op):
                authorized.append((op_id, node, op))
            else:
                node.status = OpStatus.BLOCKED
                node.note = "safety gate did not authorize execution"
                result.blocked.append(op_id)
                refused.append((op_id, node, op))
        if len(authorized) < self._BULK_MIN_RUN:
            for op_id, node, op in refused:
                node.status = OpStatus.PENDING
                result.blocked.remove(op_id)
            return 0

        specs = [dict(op.inputs) for _, _, op in authorized]
        try:
            receipts = bulk(specs)
        except Exception:
            for op_id, node, op in authorized:
                node.status = OpStatus.PENDING
            return 0

        # Match each receipt back to its operation. The adapter keys receipts
        # by the natural identity of the item (device name, link id), so the
        # DAG id is restored afterwards and the result lists stay keyed by op.
        key_field = {"create_device": "name",
                     "configure_host": "device",
                     "create_link": "link_id"}[op_type]
        by_item = {}
        for r in receipts:
            for op_id, node, op in authorized:
                if r.operation_id.endswith(str(op.inputs.get(key_field, ""))):
                    by_item[op.inputs[key_field]] = r
                    break

        for op_id, node, op in authorized:
            receipt = by_item.get(op.inputs.get(key_field))
            if receipt is None:
                self._execute_one(op, node, result)  # unusual; do it properly
                continue
            receipt.operation_id = op_id  # keep the DAG's id as the identity
            self._record(receipt)
            node.receipt = receipt
            if receipt.executed_ok and receipt.result is OperationResult.SUCCESS:
                node.status = OpStatus.SUCCEEDED
            else:
                self._fail(node, op_id, result,
                           receipt.error or f"{op_type} failed")
        return len(run)

    # -- dispatch -------------------------------------------------------------

    def _execute_one(self, op, node, result: ExecutionResult) -> None:
        dispatch = {
            "create_device": self._op_create_device,
            "create_link": self._op_create_link,
            "install_module": self._op_install_module,
            "install_wireless_radio": self._op_install_wireless_radio,
            "associate_wireless": self._op_associate_wireless,
            "configure_ios": self._op_configure_ios,
            "configure_gateway_lan": self._op_configure_gateway_lan,
            "configure_host": self._op_configure_host,
            "verify_flow": self._op_verify_flow,
            "wait_dhcp": self._op_wait_dhcp,
            "verify_state": self._op_verify_state,
            "save_project": self._op_save_project,
            "delete_device": self._op_delete_device,
            "delete_link": self._op_delete_link,
            "rollback": self._op_rollback,
        }
        handler = dispatch.get(op.operation_type)
        if handler is None:
            self._fail(node, op.operation_id, result, f"unknown operation type {op.operation_type!r}")
            return
        # Every mutating op must clear the idempotency guard first (P2-4).
        if op.classification is not MutationClassification.OBSERVE:
            if not self._idempotency_guard(op, node, result):
                return
            # Preconditions are a hard gate (P2-2): fail-closed. A check that
            # cannot be answered TRUE blocks the operation instead of letting
            # it mutate on an assumption.
            if op.preconditions:
                evaluator = self._check_evaluator()
                holds, outcomes = evaluator.all_hold(op.preconditions)
                if not holds:
                    node.status = OpStatus.BLOCKED
                    failed = [o.to_dict() for o in outcomes if o.passed is not True]
                    node.note = f"preconditions not satisfied: {failed}"
                    result.blocked.append(op.operation_id)
                    self.gate.budget.refund(1)
                    if self.evidence is not None:
                        self.evidence.record(
                            "precondition_blocked",
                            {"operation_id": op.operation_id, "preconditions": failed},
                            component="executor",
                        )
                    return
        handler(op, node, result)
        # Record the key only when the op actually succeeded, so a failed op
        # can legitimately be retried under the same key.
        if node.status is OpStatus.SUCCEEDED:
            self._remember_key(op)
            # Postconditions decide whether the mutation is CONFIRMED (P2-2):
            # a receipt that ran is not evidence that the intended state now
            # holds â€” the read-back is.
            self._evaluate_postconditions(op, node)

    def _authorize(self, op) -> bool:
        """Pass the operation through the safety gate. Returns execute?

        For link operations, also enforce port reservations (Â§9/Â§16): a port
        already reserved by another in-flight operation is a hard stop.
        """
        permission = self.gate.authorize([op.to_dict()])
        if not permission.execute:
            return False
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

    def _note_receipt(
        self,
        node,
        operation: str,
        *,
        requested: dict,
        postcondition: str,
    ) -> None:
        """Record a read-back-confirmed operation that has no adapter receipt.

        The wireless operations answer from PT state rather than from a
        mutation receipt, but the evidence trail must be the same shape as
        every other operation's. The postcondition carries the observed fact
        as an Observable, because that is what a receipt means here: this is
        what PT was seen to be, not what we asked for.
        """
        from ..core.observability import Observable

        receipt = OperationReceipt(
            operation_id=node.operation.operation_id,
            operation=operation,
            requested=requested,
            result=OperationResult.SUCCESS,
            postconditions=[
                CheckResult.of(
                    operation,
                    "observed",
                    Observable.known(postcondition, source="executor:read-back"),
                )
            ],
            started_at=utc_now_iso(),
            finished_at=utc_now_iso(),
        )
        self._record(receipt)
        node.receipt = receipt

    def _idempotency_guard(self, op, node, result: ExecutionResult) -> bool:
        """Enforce an operation's idempotency key (P2-4).

        Returns True when the operation may proceed. A key that already
        succeeded in this run is a reused no-op â€” recorded SKIPPED, not
        re-executed â€” so a repeated build never duplicates its own work.
        """
        key = getattr(op, "idempotency_key", None)
        if not key:
            return True
        previous = self.executed_keys.get(key)
        if previous and previous != op.operation_id:
            node.status = OpStatus.SKIPPED
            node.note = f"idempotency key {key!r} already satisfied by {previous}"
            result.reused.append(op.operation_id)
            self._record(
                OperationReceipt(
                    operation_id=op.operation_id,
                    operation=op.operation_type,
                    requested=op.inputs,
                    result=OperationResult.NOT_EXECUTED,
                    started_at=utc_now_iso(),
                    finished_at=utc_now_iso(),
                    evidence={
                        "decision": "idempotency key already executed",
                        "idempotency_key": key,
                        "previous_operation": previous,
                    },
                )
            )
            return False
        return True

    def _remember_key(self, op) -> None:
        key = getattr(op, "idempotency_key", None)
        if key:
            self.executed_keys.setdefault(key, op.operation_id)

    def _check_evaluator(self) -> CheckEvaluator:
        return CheckEvaluator(
            self.adapter, self.state, self._link_endpoints_of(self._dag)
        )

    @staticmethod
    def _link_endpoints_of(dag: OperationDag | None) -> dict[str, tuple[str, str, str, str]]:
        endpoints: dict[str, tuple[str, str, str, str]] = {}
        if dag is None:
            return endpoints
        for node in dag.nodes.values():
            op = node.operation
            if op.operation_type == "create_link":
                inputs = op.inputs
                endpoints[inputs["link_id"]] = (
                    inputs["a_device"], inputs["a_port"],
                    inputs["b_device"], inputs["b_port"],
                )
        return endpoints

    def _evaluate_postconditions(self, op, node) -> None:
        """Turn an executed op into a confirmed one only if read-back holds.

        Mutating the receipt in place keeps the DAG's receipt honest: it is
        CONFIRMED exactly when the postconditions were observable and TRUE.
        An unobservable postcondition leaves it unconfirmed â€” never a pass.
        """
        if not op.expected_postconditions or node.receipt is None:
            return
        evaluator = self._check_evaluator()
        outcomes = [evaluator.evaluate(c) for c in op.expected_postconditions]
        confirmed = all(o.passed is True for o in outcomes)
        unknown = any(o.passed is None for o in outcomes)
        receipt = node.receipt
        evidence = dict(receipt.evidence or {})
        evidence["postcondition_results"] = [o.to_dict() for o in outcomes]
        evidence["confirmed_by_read_back"] = confirmed
        if unknown:
            evidence["postcondition_note"] = (
                "at least one postcondition could not be observed â€” receipt "
                "stays unconfirmed, verification decides"
            )
        # Receipts are immutable records of what happened; we replace the
        # reference with an updated copy rather than rewriting history in place.
        node.receipt = OperationReceipt(
            operation_id=receipt.operation_id,
            operation=receipt.operation,
            requested=receipt.requested,
            executed=receipt.executed,
            result=receipt.result,
            preconditions=receipt.preconditions,
            postconditions=receipt.postconditions,
            evidence=evidence,
            started_at=receipt.started_at,
            finished_at=receipt.finished_at,
            error=receipt.error,
        )
        if self.evidence is not None:
            self.evidence.record(
                "postcondition_evaluation",
                {
                    "operation_id": op.operation_id,
                    "confirmed": confirmed,
                    "results": [o.to_dict() for o in outcomes],
                },
                component="executor",
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
            # IDEMPOTENT REUSE: observe â†’ compare â†’ reconcile â†’ reuse.
            # Never recreate a device that already exists.
            self.gate.budget.refund(1)
            reconciliation = self._reconcile_device(op)
            node.status = OpStatus.SKIPPED
            node.note = "device already exists; reused instead of recreated"
            if reconciliation:
                node.note += f"; reconciled: {len(reconciliation)} field(s) checked"
            result.reused.append(op.operation_id)
            if self.evidence is not None:
                self.evidence.record(
                    "device_reconciliation",
                    {
                        "operation_id": op.operation_id,
                        "device": name,
                        "reconciliation": reconciliation,
                    },
                    component="executor",
                )
            self._record(
                OperationReceipt(
                    operation_id=op.operation_id,
                    operation="create_device",
                    requested=op.inputs,
                    result=OperationResult.NOT_EXECUTED,
                    started_at=utc_now_iso(),
                    finished_at=utc_now_iso(),
                    evidence={
                        "decision": "reused existing device",
                        "reconciliation": reconciliation,
                    },
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

    def _reconcile_device(self, op) -> list[dict]:
        """Reuse is observe -> diff -> reconcile -> confirm, not just observe.

        A device that already exists may carry config the spec no longer asks
        for (an operator's leftover, a crashed earlier build). The normal
        configure push only SETS what the spec wants, so an extra address
        survives the build and the device never equals the spec (F5). Here the
        observed addresses are diffed against the addresses this very DAG's
        config operations ask for; the extras are removed through the safety
        gate and the removal is confirmed by read-back. Everything the spec
        does want is left to the normal configure op.
        """
        name = op.inputs["name"]
        obs = self.adapter.inspect_device(name)
        expected_model = op.inputs.get("model")
        reconciliation: list[dict] = []

        # Compare model
        actual_model = obs.model.value if obs.model.is_known else None
        if expected_model and actual_model != expected_model:
            reconciliation.append({
                "field": "model",
                "actual": actual_model,
                "expected": expected_model,
                "needs_mutation": True,
            })

        expected_ips = self._expected_addresses_for(name)
        extras: list[dict] = []
        for port_name, port_obs in sorted(obs.ports.items()):
            if not (port_obs.ip.is_known and port_obs.ip.value):
                continue
            ip = str(port_obs.ip.value)
            if ip == "0.0.0.0":  # PT's Bluetooth placeholder, not a real address
                continue
            if ip not in expected_ips:
                extras.append({"port": port_name, "ip": ip})

        if extras:
            reconciliation.append({
                "field": "extra_addresses",
                "actual": extras,
                "expected": sorted(expected_ips),
                "needs_mutation": True,
            })
            self._reconcile_remove_extras(name, extras, reconciliation)

        # Control-plane read-back is reported, never acted on here: routes and
        # VLANs are re-pushed by their own configure op, and guessing a removal
        # set from a partial read is exactly the speculation this project bans.
        if hasattr(obs, "routes") and obs.routes.is_known and obs.routes.value:
            actual_routes = obs.routes.value if isinstance(obs.routes.value, list) else [obs.routes.value]
            reconciliation.append({
                "field": "routes",
                "actual": actual_routes,
                "needs_mutation": False,
            })
        if hasattr(obs, "vlans") and obs.vlans.is_known and obs.vlans.value:
            actual_vlans = obs.vlans.value if isinstance(obs.vlans.value, list) else [obs.vlans.value]
            reconciliation.append({
                "field": "vlans",
                "actual": actual_vlans,
                "needs_mutation": False,
            })

        return reconciliation

    def _expected_addresses_for(self, device: str) -> set[str]:
        """Every address the spec asks for on `device`, read off this DAG.

        Sourced from the compiled configure ops themselves, so the reconcile
        compares against exactly what the build is about to push — not a
        parallel re-derivation that could drift from the compiler.
        """
        expected: set[str] = set()
        if self._dag is None:
            return expected
        for node in self._dag.nodes.values():
            op = node.operation
            inputs = op.inputs
            if op.operation_type == "configure_ios" and inputs.get("device") == device:
                for command in inputs.get("commands") or []:
                    stripped = command.strip()
                    if stripped.lower().startswith("ip address "):
                        parts = stripped.split()
                        if len(parts) >= 3:
                            expected.add(parts[2])
            elif op.operation_type == "configure_host" and inputs.get("device") == device:
                ip = inputs.get("ip")
                if ip:
                    expected.add(str(ip))
            elif (
                op.operation_type == "configure_gateway_lan"
                and inputs.get("device") == device
            ):
                ip = inputs.get("ip")
                if ip:
                    expected.add(str(ip))
        return expected

    def _reconcile_remove_extras(
        self, device: str, extras: list[dict], reconciliation: list[dict]
    ) -> None:
        """Remove stale addresses through the gate, then confirm by read-back."""
        commands: list[str] = []
        for entry in extras:
            commands.extend([f"interface {entry['port']}", "no ip address", "exit"])
        permission = self.gate.authorize([
            {
                "operation_type": "configure_ios",
                "device": device,
                "commands": commands,
            }
        ])
        if not permission.execute:
            reconciliation.append({
                "field": "removal_refused",
                "detail": "safety gate did not authorize the reconcile removal",
                "ports": [e["port"] for e in extras],
                "needs_mutation": True,
            })
            return
        receipt = self.adapter.configure_ios(device, commands)
        self._record(receipt)
        after = self.adapter.inspect_device(device)
        removed: list[dict] = []
        remaining: list[dict] = []
        for entry in extras:
            port_obs = after.ports.get(entry["port"])
            still_there = (
                port_obs is not None
                and port_obs.ip.is_known
                and port_obs.ip.value
                and str(port_obs.ip.value) == entry["ip"]
            )
            (remaining if still_there else removed).append(entry)
        reconciliation.append({
            "field": "removal_readback",
            "removed": removed,
            "remaining": remaining,
            "needs_mutation": False,
        })

    def _op_install_wireless_radio(self, op, node, result: ExecutionResult) -> None:
        """Put a wireless NIC in a host so it can associate at all."""
        if not self._authorize(op):
            node.status = OpStatus.BLOCKED
            node.note = "safety gate did not authorize execution"
            result.blocked.append(op.operation_id)
            return
        device = op.inputs["device"]
        answer = self.adapter.ensure_wireless_radio(
            device,
            module=op.inputs.get("module", "PT-LAPTOP-NM-1W"),
            slot=op.inputs.get("slot", "0"),
            interface=op.inputs.get("interface", "Wireless0"),
        )
        if answer.get("installed"):
            node.status = OpStatus.SUCCEEDED
            node.note = f"wireless NIC {answer.get('module')} installed"
            self._note_receipt(
                node,
                "install_wireless_radio",
                requested={"device": device, **answer},
                postcondition=(
                    f"wireless_interface_visible={op.inputs.get('interface')}"
                ),
            )
        else:
            self._fail(
                node, op.operation_id, result,
                answer.get("error") or f"wireless radio not installed: {answer.get('answer')}",
            )

    def _op_associate_wireless(self, op, node, result: ExecutionResult) -> None:
        """Associate a wireless client and prove it from PT's own state.

        Packet Tracer performs the association itself; this operation's job is
        to give it the radio, let it settle, and read the `Antenna` link back.
        A missing antenna is a FAILED build - the alternative is a topology
        that reports success and carries no traffic.
        """
        if not self._authorize(op):
            node.status = OpStatus.BLOCKED
            node.note = "safety gate did not authorize execution"
            result.blocked.append(op.operation_id)
            return
        from ..wireless import WirelessAssociationError, associate_wireless_client

        client = op.inputs["client"]
        access_point = op.inputs["access_point"]
        try:
            association = associate_wireless_client(
                self.adapter,
                client,
                access_point,
                interface=op.inputs.get("client_interface", "Wireless0"),
                channel=op.inputs.get("channel"),
                settle_seconds=float(op.inputs.get("settle_seconds", 1.0)),
                timeout_seconds=float(op.inputs.get("timeout_seconds", 20.0)),
            )
        except WirelessAssociationError as exc:
            self._fail(node, op.operation_id, result, str(exc))
            return
        node.status = OpStatus.SUCCEEDED
        node.note = (
            f"{association.client}:{association.client_interface} associated to "
            f"{association.access_point}:{association.ap_interface} "
            f"({association.link_class})"
        )
        self._note_receipt(
            node,
            "associate_wireless",
            requested={"client": client, "access_point": access_point},
            postcondition=(
                f"wireless_associated={association.access_point} "
                f"link_class={association.link_class} port_up={association.port_up}"
            ),
        )

    def _op_delete_device(self, op, node, result: ExecutionResult) -> None:
        if not self._authorize(op):
            node.status = OpStatus.BLOCKED
            node.note = "safety gate did not authorize execution"
            result.blocked.append(op.operation_id)
            return
        name = op.inputs["name"]
        receipt = self.adapter.delete_device(name, operation_id=op.operation_id)
        self._record(receipt)
        node.receipt = receipt
        if receipt.executed_ok and receipt.result is OperationResult.SUCCESS:
            node.status = OpStatus.SUCCEEDED
        else:
            self._fail(node, op.operation_id, result, receipt.error or "delete_device failed")

    def _op_delete_link(self, op, node, result: ExecutionResult) -> None:
        if not self._authorize(op):
            node.status = OpStatus.BLOCKED
            node.note = "safety gate did not authorize execution"
            result.blocked.append(op.operation_id)
            return
        a_device = op.inputs["a_device"]
        a_port = op.inputs.get("a_port", "")
        b_device = op.inputs["b_device"]
        b_port = op.inputs.get("b_port", "")
        receipt = self.adapter.delete_link(
            a_device, a_port, b_device, b_port, operation_id=op.operation_id,
        )
        self._record(receipt)
        node.receipt = receipt
        if receipt.executed_ok and receipt.result is OperationResult.SUCCESS:
            node.status = OpStatus.SUCCEEDED
        else:
            self._fail(node, op.operation_id, result, receipt.error or "delete_link failed")

    def _op_rollback(self, op, node, result: ExecutionResult) -> None:
        if not self._authorize(op):
            node.status = OpStatus.BLOCKED
            node.note = "safety gate did not authorize execution"
            result.blocked.append(op.operation_id)
            return
        device = op.inputs["device"]
        prior_ip = op.inputs.get("prior_ip")
        prior_mask = op.inputs.get("prior_mask")
        prior_gateway = op.inputs.get("prior_gateway")
        mutations: list[OperationReceipt] = []
        if prior_ip is not None and prior_mask is not None:
            receipt = self.adapter.configure_host(
                device,
                dhcp=False,
                ip=prior_ip,
                mask=prior_mask,
                gateway=prior_gateway,
                operation_id=op.operation_id,
            )
            self._record(receipt)
            mutations.append(receipt)
            if not (receipt.executed_ok and receipt.result is OperationResult.SUCCESS):
                self._fail(node, op.operation_id, result, receipt.error or "rollback configure_host failed")
                return
        node.status = OpStatus.SUCCEEDED
        node.note = f"rollback applied for {device}"
        node.receipt = OperationReceipt(
            operation_id=op.operation_id,
            operation="rollback",
            requested=op.inputs,
            executed={"mutations": [m.to_dict() for m in mutations]},
            result=OperationResult.SUCCESS,
            started_at=utc_now_iso(),
            finished_at=utc_now_iso(),
            evidence={"mutations": [m.to_dict() for m in mutations]},
        )
        self._record(node.receipt)

    def _op_create_link(self, op, node, result: ExecutionResult) -> None:
        if not self._authorize(op):
            node.status = OpStatus.BLOCKED
            node.note = "safety gate did not authorize execution"
            result.blocked.append(op.operation_id)
            return
        a_device, a_port = op.inputs["a_device"], op.inputs.get("a_port")
        b_device, b_port = op.inputs["b_device"], op.inputs.get("b_port")
        endpoints = ((a_device, a_port), (b_device, b_port))
        media = op.inputs.get("media", "auto")
        # Reserve both ports up front (P2-1): a link that loses the race for a
        # port must not cable onto an interface another operation took.
        for dev, iface in endpoints:
            if not iface:
                continue
            outcome = self.reservation_system.reserve_single(
                dev, iface, media, operation_id=op.operation_id
            )
            if not outcome.success:
                node.status = OpStatus.BLOCKED
                node.note = f"port reservation refused: {outcome.reason}"
                result.blocked.append(op.operation_id)
                return
        if a_port and b_port:
            exists = self.adapter.link_exists(a_device, a_port, b_device, b_port)
            if exists.is_known and exists.value:
                # Idempotent reuse: nothing was cabled, so return the budget
                # the gate reserved for this link.
                self.gate.budget.refund(1)
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
            # Release the reservation: the link did not take the ports.
            for dev, iface in endpoints:
                if iface:
                    self.reservation_system.release(dev, iface)
            self._fail(node, op.operation_id, result, receipt.error or "link failed")

    def _op_install_module(self, op, node, result: ExecutionResult) -> None:
        if not self._authorize(op):
            node.status = OpStatus.BLOCKED
            node.note = "safety gate did not authorize execution"
            result.blocked.append(op.operation_id)
            return
        device = op.inputs["device"]
        slot = op.inputs["slot"]
        module = op.inputs["module"]
        expected_port = op.inputs.get("expected_port") or ""
        receipt = self.adapter.install_module(
            device, slot, module, expected_port, operation_id=op.operation_id
        )
        self._record(receipt)
        node.receipt = receipt
        if receipt.executed_ok:
            node.status = OpStatus.SUCCEEDED
        else:
            self._fail(node, op.operation_id, result, receipt.error or "module install failed")

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
        # IOS acceptance may be unobservable (receipt unconfirmed) â€” that is
        # still a SUCCESS for DAG purposes; verification will judge reality.
        if receipt.executed_ok:
            node.status = OpStatus.SUCCEEDED
            if not receipt.confirmed:
                node.note = (
                    "executed; IOS acceptance not observable â€” judged by verification"
                )
            self._prove_serial_clocks(op, node, result)
        else:
            self._fail(node, op.operation_id, result, receipt.error or "configuration failed")

    def _prove_serial_clocks(self, op, node, result: ExecutionResult) -> None:
        """Check a back-to-back serial link's DCE/DTE roles against PT itself.

        A serial link only carries traffic when exactly one end is clocked
        (`_probe_serial5.py`), and PT will not tell the user which end is
        wrong. Reading `show controllers` back and comparing it with what was
        compiled turns that into a receipt fact: a proven mismatch fails the op
        with the PT line attached, and an unreadable console stays UNKNOWN
        rather than claiming success.
        """
        entries = op.inputs.get("serial_clocks") or []
        if not entries:
            return
        evidence = read_serial_clock_evidence(self.adapter, list(entries))
        if self.evidence is not None:
            self.evidence.record(
                "serial_clock",
                {
                    "device": op.inputs.get("device"),
                    "endpoints": evidence,
                },
                component="executor",
            )
        failed = [e for e in evidence if e["status"] == "fail"]
        if failed:
            self._fail(node, op.operation_id, result, failed[0]["detail"])
            return
        unknown = [e for e in evidence if e["status"] == "unknown"]
        if unknown:
            node.note = (
                f"serial clock roles not observable on {unknown[0]['device']}"
                f":{unknown[0]['interface']} â€” judged by verification"
            )

    def _op_configure_gateway_lan(self, op, node, result: ExecutionResult) -> None:
        """#10: the Linksys LAN address travels the port API, not the CLI.

        Same receipt discipline as configure_ios: the adapter read-back flips
        a push that did not land to FAILED, so this handler only records what
        the receipt actually says.
        """
        if not self._authorize(op):
            node.status = OpStatus.BLOCKED
            node.note = "safety gate did not authorize execution"
            result.blocked.append(op.operation_id)
            return
        receipt = self.adapter.configure_gateway_lan(
            op.inputs["device"],
            op.inputs["ip"],
            op.inputs["mask"],
            operation_id=op.operation_id,
        )
        self._record(receipt)
        node.receipt = receipt
        if receipt.executed_ok:
            node.status = OpStatus.SUCCEEDED
            if not receipt.confirmed:
                node.note = (
                    "executed; gateway LAN address not observable â€” "
                    "judged by verification"
                )
        else:
            self._fail(
                node,
                op.operation_id,
                result,
                receipt.error or "gateway LAN configuration failed",
            )

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
        # The criterion's declared protocol reaches the adapter so it can
        # choose a real probe â€” and honestly report when it has none (P1-5).
        transport = str(op.inputs.get("transport") or "icmp")
        port = op.inputs.get("port")
        flow = self.adapter.run_flow_test(
            source, destination, transport=transport, port=port,
            destination_ip=op.inputs.get("destination_ip"),
        )
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
        note = f"flow observed: {flow.outcome.value}"
        node.note = f"{node.note}; {note}" if node.note else note

    def _op_wait_dhcp(self, op, node, result: ExecutionResult) -> None:
        """#26: bounded convergence wait so flows do not probe a leaseless host.

        The wait is OBSERVE-class: it reads, never mutates, and it never fails
        the DAG. If the window closes with a client still unaddressed the gate
        says so in its note and the flow reports UNKNOWN honestly afterwards -
        a timeout is information, not a broken build.
        """
        pending = [str(d) for d in (op.inputs.get("devices") or [])]
        if self.adapter.name == "null":
            # The structural double has no data plane to converge on, so
            # polling it would only burn the window.
            node.status = OpStatus.SUCCEEDED
            node.note = "structural double: nothing to wait for"
            return
        timeout = float(op.inputs.get("timeout_s") or 0.0)
        deadline = time.time() + max(timeout, 0.0)
        polls = 0
        while pending:
            observed = self.adapter.observe_state(pending)
            self.state.merge(observed)
            self.state.observed_at = utc_now_iso()
            polls += 1
            pending = [d for d in pending if not _client_has_address(observed, d)]
            if not pending or time.time() >= deadline:
                break
            time.sleep(_DHCP_POLL_INTERVAL_S)
        if self.evidence is not None:
            self.evidence.record(
                "dhcp_convergence",
                {"pending": pending, "polls": polls, "timeout_s": timeout},
                component="executor",
            )
        node.status = OpStatus.SUCCEEDED
        note = (
            "all DHCP clients have an address"
            if not pending
            else "still leaseless after {}s: {}".format(timeout, ", ".join(pending))
        )
        node.note = f"{node.note}; {note}" if node.note else note

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
        self._record_failure_receipt(node, op_id, note)

    def _record_failure_receipt(self, node, op_id: str, note: str) -> None:
        """Persist a failure's reason where the evidence can answer for it.

        `node.note` alone is not evidence: it never reaches evidence.jsonl, so
        build-68648f705ccb's radio failure was reconstructable only from the
        blocked ids around it (rule 9: refuse BY NAME). A failure without a
        prior receipt gets a FAILED one carrying the reason; an op that already
        recorded its adapter receipt keeps exactly one record.
        """
        if node.receipt is not None or self.evidence is None:
            return
        self._record(
            OperationReceipt(
                operation_id=op_id,
                operation=node.operation.operation_type,
                requested=node.operation.inputs,
                result=OperationResult.FAILED,
                error=note,
                started_at=utc_now_iso(),
                finished_at=utc_now_iso(),
            )
        )
