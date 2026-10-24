"""Safe existing-network enhancement (S4.11).

Enhancement is a diff, not a rebuild. The planner compares the as-is intent
with the desired intent, reports every addition/modification/removal, and
refuses conflicts that would make the result unsafe or ambiguous (IP overlap,
VLAN collision, duplicate address, a name reused for a different device, more
links than the chassis has ports).

Execution is deliberately conservative:

* approval goes through the same `SafetyGate`;
* a verified backup of every device the change touches is mandatory;
* only operations whose idempotency key differs from the current build are
  executed, so unchanged devices are not re-pushed;
* the whole network is reverified afterwards — the desired criteria AND every
  criterion that passed before — and regressions are reported.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..adapter.base import PacketTracerAdapter
from ..compiler.compiler import compile_spec
from ..compiler.dag import OpStatus, OperationDag
from ..compiler.executor import DagExecutor
from ..core.network_spec import (
    AcceptanceCriterionSpec,
    DeviceRole,
    FlowTestSpec,
    NetworkSpec,
    default_model_for_role,
)
from ..core.requirements import FlowOutcome, FlowResult, Verdict
from ..core.safety import SafetyGate, SafetyMode
from ..evidence.store import EvidenceStore
from ..ipam.planner import plan_addressing
from ..pt.catalog import resolve_model
from ..verification.engine import VerificationEngine, VerificationReport
from .backup import BackupManager, BackupManifest, BackupValidation
from .models import CONFIDENCE_RANK


@dataclass(frozen=True)
class Change:
    section: str
    kind: str          # add | remove | modify
    identity: str
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "kind": self.kind,
            "identity": self.identity,
            "before": self.before,
            "after": self.after,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class Conflict:
    code: str
    severity: str      # error | warning
    message: str
    resource: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "resource": self.resource,
        }


@dataclass
class EnhancementPlan:
    current_spec: NetworkSpec
    desired_spec: NetworkSpec
    changes: list[Change] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    operations: list[dict[str, Any]] = field(default_factory=list)
    affected_devices: list[str] = field(default_factory=list)
    blocked_reason: str | None = None
    requires_manual_removal: bool = False
    diff_summary: dict[str, int] = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return self.blocked_reason is not None or any(
            conflict.severity == "error" for conflict in self.conflicts
        )

    @property
    def error_conflicts(self) -> list[Conflict]:
        return [c for c in self.conflicts if c.severity == "error"]

    def preview(self) -> dict[str, Any]:
        return {
            "changes": [c.to_dict() for c in self.changes],
            "conflicts": [c.to_dict() for c in self.conflicts],
            "diff_summary": dict(self.diff_summary),
            "operations": list(self.operations),
            "affected_devices": list(self.affected_devices),
            "requires_manual_removal": self.requires_manual_removal,
            "blocked": self.blocked,
            "blocked_reason": self.blocked_reason,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_spec": self.current_spec.model_dump(mode="json"),
            "desired_spec": self.desired_spec.model_dump(mode="json"),
            **self.preview(),
        }


@dataclass
class EnhancementOutcome:
    executed: bool = False
    blocked_reason: str | None = None
    approved: bool = False
    backup: BackupManifest | None = None
    backup_validation: BackupValidation | None = None
    dag_summary: dict[str, int] = field(default_factory=dict)
    applied_operations: list[str] = field(default_factory=list)
    original_before: dict[str, str] = field(default_factory=dict)
    original_after: dict[str, str] = field(default_factory=dict)
    desired_after: dict[str, str] = field(default_factory=dict)
    preserved: bool = False
    desired_satisfied: bool = False
    success: bool = False
    regressions: list[str] = field(default_factory=list)
    reverted: bool = False
    revert_detail: str | None = None
    original_report: dict[str, Any] | None = None
    desired_report: dict[str, Any] | None = None
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "executed": self.executed,
            "blocked_reason": self.blocked_reason,
            "approved": self.approved,
            "backup": self.backup.to_dict() if self.backup else None,
            "backup_validation": (
                self.backup_validation.to_dict() if self.backup_validation else None
            ),
            "dag_summary": dict(self.dag_summary),
            "applied_operations": list(self.applied_operations),
            "original_before": dict(self.original_before),
            "original_after": dict(self.original_after),
            "desired_after": dict(self.desired_after),
            "preserved": self.preserved,
            "desired_satisfied": self.desired_satisfied,
            "success": self.success,
            "regressions": list(self.regressions),
            "reverted": self.reverted,
            "revert_detail": self.revert_detail,
            "original_report": self.original_report,
            "desired_report": self.desired_report,
            "problems": list(self.problems),
        }


class EnhancementPlanner:
    """Compute the structured difference and the conflicts it exposes."""

    def plan(self, current: NetworkSpec, desired: NetworkSpec) -> EnhancementPlan:
        plan = EnhancementPlan(current_spec=current, desired_spec=desired)
        plan.changes.extend(self._diff_devices(current, desired))
        plan.changes.extend(self._diff_links(current, desired))
        plan.changes.extend(self._diff_subnets(current, desired))
        plan.changes.extend(self._diff_vlans(current, desired))
        plan.changes.extend(self._diff_assignments(current, desired))
        plan.changes.extend(self._diff_routing(current, desired))
        plan.changes.extend(self._diff_features(current, desired))
        plan.conflicts.extend(self._conflicts(current, desired))
        plan.requires_manual_removal = any(
            change.kind == "remove" for change in plan.changes
        )
        counts: dict[str, int] = {}
        for change in plan.changes:
            key = f"{change.section}:{change.kind}"
            counts[key] = counts.get(key, 0) + 1
        plan.diff_summary = dict(sorted(counts.items()))

        # Preview the operations an application would actually run: the
        # desired DAG minus every operation the current build already
        # satisfies by idempotency key. Unchanged devices are not re-pushed.
        try:
            current_compile = compile_spec(current, plan_addressing(current))
            desired_compile = compile_spec(desired, plan_addressing(desired))
        except Exception as exc:
            plan.blocked_reason = f"the desired intent does not compile: {exc}"
            return plan
        current_keys = {
            node.operation.idempotency_key
            for node in current_compile.dag.nodes.values()
        }
        changed_ops = [
            node.operation
            for node in desired_compile.dag.nodes.values()
            if node.operation.idempotency_key not in current_keys
        ]
        plan.operations = [op.to_dict() for op in changed_ops]
        devices: set[str] = set()
        for op in changed_ops:
            for key in ("device", "name", "a_device", "b_device", "source", "destination"):
                if op.inputs.get(key):
                    devices.add(str(op.inputs[key]))
        plan.affected_devices = sorted(devices)
        if plan.requires_manual_removal and not plan.error_conflicts:
            plan.blocked_reason = (
                "the desired intent removes resources; removing configuration "
                "is not applied automatically without an explicit removal plan"
            )
        if plan.blocked_reason is None and not changed_ops:
            plan.blocked_reason = "no executable change was produced"
        return plan

    # -- structured diffs ---------------------------------------------------

    @staticmethod
    def _diff_collection(
        section: str,
        current_items: Iterable[Any],
        desired_items: Iterable[Any],
        identity,
    ) -> list[Change]:
        current = {identity(item): item for item in current_items}
        desired = {identity(item): item for item in desired_items}
        changes: list[Change] = []
        for key in sorted(set(current) | set(desired)):
            before = current.get(key)
            after = desired.get(key)
            b = before.model_dump(mode="json") if before is not None else None
            a = after.model_dump(mode="json") if after is not None else None
            if before is None and after is not None:
                changes.append(Change(section, "add", str(key), None, a))
            elif before is not None and after is None:
                changes.append(Change(section, "remove", str(key), b, None))
            elif b != a:
                changes.append(Change(section, "modify", str(key), b, a))
        return changes

    def _diff_devices(self, current: NetworkSpec, desired: NetworkSpec) -> list[Change]:
        return self._diff_collection(
            "devices", current.devices, desired.devices, lambda d: d.id
        )

    def _diff_links(self, current: NetworkSpec, desired: NetworkSpec) -> list[Change]:
        def identity(link) -> str:
            endpoints = tuple(
                sorted(
                    (
                        (link.a.device, link.a.interface_hint or ""),
                        (link.b.device, link.b.interface_hint or ""),
                    )
                )
            )
            return str(endpoints)

        return self._diff_collection("links", current.links, desired.links, identity)

    def _diff_subnets(self, current: NetworkSpec, desired: NetworkSpec) -> list[Change]:
        return self._diff_collection(
            "subnets", current.subnets, desired.subnets, lambda s: s.cidr or s.id
        )

    def _diff_vlans(self, current: NetworkSpec, desired: NetworkSpec) -> list[Change]:
        return self._diff_collection(
            "vlans", current.vlans, desired.vlans, lambda v: v.vlan_id
        )

    def _diff_assignments(self, current: NetworkSpec, desired: NetworkSpec) -> list[Change]:
        return self._diff_collection(
            "ip_assignments",
            current.ip_assignments,
            desired.ip_assignments,
            lambda a: (a.device, a.interface_hint or "", a.subnet),
        )

    def _diff_routing(self, current: NetworkSpec, desired: NetworkSpec) -> list[Change]:
        changes = self._diff_collection(
            "static_routes",
            current.routing.static_routes,
            desired.routing.static_routes,
            lambda r: (r.device, r.destination, r.next_hop or r.exit_interface or ""),
        )
        if current.routing.mode != desired.routing.mode:
            changes.append(
                Change(
                    "routing", "modify", "mode",
                    {"mode": current.routing.mode}, {"mode": desired.routing.mode},
                )
            )
        return changes

    def _diff_features(self, current: NetworkSpec, desired: NetworkSpec) -> list[Change]:
        changes: list[Change] = []
        for section, before_items, after_items in (
            ("svis", current.switching.svis, desired.switching.svis),
            ("etherchannels", current.switching.etherchannels, desired.switching.etherchannels),
            ("stp", current.switching.stp, desired.switching.stp),
            ("port_security", current.switching.port_security, desired.switching.port_security),
            ("vtp", current.switching.vtp, desired.switching.vtp),
            ("voice_vlans", current.switching.voice_vlans, desired.switching.voice_vlans),
            ("management_ips", current.switching.management_ips, desired.switching.management_ips),
            ("hsrp", current.routing.hsrp, desired.routing.hsrp),
            ("dhcp_pools", current.routing.dhcp_pools, desired.routing.dhcp_pools),
            ("dhcp_relays", current.routing.dhcp_relays, desired.routing.dhcp_relays),
            ("nat", current.routing.nat, desired.routing.nat),
            ("policies", current.policies, desired.policies),
            ("services", current.services, desired.services),
            ("firewall_dhcp", current.firewall.dhcp, desired.firewall.dhcp),
            ("firewall_nat", current.firewall.nat, desired.firewall.nat),
            ("object_groups", current.firewall.object_groups, desired.firewall.object_groups),
            ("wireless", current.wireless_config, desired.wireless_config),
            ("host_dns", current.host_dns, desired.host_dns),
        ):
            changes.extend(
                self._diff_collection(
                    section, before_items, after_items,
                    lambda item: str(item.model_dump(mode="json")),
                )
            )
        return changes

    # -- conflicts ----------------------------------------------------------

    @staticmethod
    def _networks(spec: NetworkSpec) -> list[tuple[str, str, ipaddress.IPv4Network]]:
        out: list[tuple[str, str, ipaddress.IPv4Network]] = []
        for subnet in spec.subnets:
            if not subnet.cidr:
                continue
            try:
                out.append((subnet.id, subnet.cidr, ipaddress.ip_network(subnet.cidr, strict=False)))
            except ValueError:
                continue
        return out

    def _conflicts(self, current: NetworkSpec, desired: NetworkSpec) -> list[Conflict]:
        conflicts: list[Conflict] = []
        current_nets = self._networks(current)
        desired_nets = self._networks(desired)
        removed_cidrs = {
            cidr for _, cidr, _ in current_nets
        } - {cidr for _, cidr, _ in desired_nets}
        # IP overlap against a subnet the enhancement does not replace.
        for subnet_id, cidr, network in desired_nets:
            for other_id, other_cidr, other_network in current_nets:
                if other_cidr in removed_cidrs:
                    continue
                if other_cidr == cidr:
                    continue
                if network.overlaps(other_network):
                    conflicts.append(
                        Conflict(
                            "ip-overlap", "error",
                            f"desired subnet {cidr} ({subnet_id}) overlaps "
                            f"existing {other_cidr} ({other_id})",
                            subnet_id,
                        )
                    )
        # Duplicate addresses inside the desired intent.
        seen: dict[str, str] = {}
        for assignment in desired.ip_assignments:
            if not assignment.ip:
                continue
            if assignment.ip in seen:
                conflicts.append(
                    Conflict(
                        "duplicate-ip", "error",
                        f"desired address {assignment.ip} is assigned to both "
                        f"{seen[assignment.ip]} and {assignment.device}",
                        assignment.ip,
                    )
                )
            else:
                seen[assignment.ip] = assignment.device
        # VLAN collisions: same id, different name; same id, different subnet.
        current_vlans = {v.vlan_id: v for v in current.vlans}
        desired_vlans = {v.vlan_id: v for v in desired.vlans}
        for vlan_id in sorted(set(current_vlans) & set(desired_vlans)):
            if current_vlans[vlan_id].name != desired_vlans[vlan_id].name:
                conflicts.append(
                    Conflict(
                        "vlan-name-collision", "error",
                        f"VLAN {vlan_id} is {current_vlans[vlan_id].name!r} today "
                        f"and {desired_vlans[vlan_id].name!r} in the desired intent",
                        f"vlan:{vlan_id}",
                    )
                )
        current_vlan_subnets = {
            s.vlan_id: s.cidr for s in current.subnets if s.vlan_id is not None
        }
        desired_vlan_subnets = {
            s.vlan_id: s.cidr for s in desired.subnets if s.vlan_id is not None
        }
        for vlan_id in sorted(set(current_vlan_subnets) & set(desired_vlan_subnets)):
            if current_vlan_subnets[vlan_id] != desired_vlan_subnets[vlan_id]:
                conflicts.append(
                    Conflict(
                        "vlan-subnet-collision", "error",
                        f"VLAN {vlan_id} serves {current_vlan_subnets[vlan_id]} "
                        f"today and {desired_vlan_subnets[vlan_id]} in the desired intent",
                        f"vlan:{vlan_id}",
                    )
                )
        # Naming conflict: an existing name reused for a different role/model.
        current_devices = {d.id: d for d in current.devices}
        for device in desired.devices:
            existing = current_devices.get(device.id)
            if existing is None:
                continue
            if (
                existing.role != device.role
                or (existing.model_hint or "") != (device.model_hint or "")
            ):
                conflicts.append(
                    Conflict(
                        "naming-conflict", "error",
                        f"device id {device.id!r} already exists as "
                        f"{existing.role.value}/{existing.model_hint}; the desired "
                        f"intent changes it to {device.role.value}/{device.model_hint}",
                        device.id,
                    )
                )
        # Interface capacity: desired links versus the chassis's onboard ports.
        link_counts: dict[str, int] = {}
        for link in desired.links:
            link_counts[link.a.device] = link_counts.get(link.a.device, 0) + 1
            link_counts[link.b.device] = link_counts.get(link.b.device, 0) + 1
        for device in desired.devices:
            need = link_counts.get(device.id, 0)
            model_name = device.model_hint or default_model_for_role(device.role)
            model = resolve_model(model_name)
            if model is None:
                conflicts.append(
                    Conflict(
                        "unknown-model", "warning",
                        f"cannot check port capacity for {device.id}: model "
                        f"{model_name!r} is not in the catalog",
                        device.id,
                    )
                )
                continue
            capacity = len(model.ports)
            if need > capacity:
                conflicts.append(
                    Conflict(
                        "insufficient-interface-capacity", "error",
                        f"{device.id} needs {need} cabled ports but its "
                        f"{model_name} chassis has {capacity}",
                        device.id,
                    )
                )
        # DHCP pool consistency.
        for pool in desired.routing.dhcp_pools:
            subnet = desired.subnet_by_id(pool.subnet)
            if subnet is None or not subnet.cidr:
                continue
            try:
                network = ipaddress.ip_network(subnet.cidr, strict=False)
                router = ipaddress.ip_address(pool.default_router)
            except ValueError:
                continue
            if router not in network:
                conflicts.append(
                    Conflict(
                        "dhcp-gateway-outside-subnet", "error",
                        f"DHCP pool on {pool.device} uses default router "
                        f"{pool.default_router}, outside {subnet.cidr}",
                        pool.subnet,
                    )
                )
        return conflicts


class BrownfieldEnhancer:
    """Execute an approved enhancement and verify the whole network."""

    def __init__(
        self,
        adapter: PacketTracerAdapter,
        gate: SafetyGate,
        *,
        evidence: EvidenceStore | None = None,
        backup: BackupManager | None = None,
    ):
        self.adapter = adapter
        self.gate = gate
        self.evidence = evidence
        self.backup = backup

    def apply(
        self,
        plan: EnhancementPlan,
        *,
        approved: bool | None = None,
    ) -> EnhancementOutcome:
        outcome = EnhancementOutcome()
        if plan.blocked:
            outcome.blocked_reason = plan.blocked_reason or (
                "enhancement conflicts: "
                + "; ".join(c.message for c in plan.error_conflicts)
            )
            self._record("enhancement_blocked", plan, outcome)
            return outcome
        if self.gate.mode is SafetyMode.APPROVAL_REQUIRED:
            if approved is False:
                outcome.blocked_reason = "explicit approval was withheld"
                return outcome
            if approved is not True and self.gate.approver is None:
                outcome.blocked_reason = (
                    "APPROVAL_REQUIRED: the enhancement preview has not been approved"
                )
                return outcome
        effective_gate = self.gate
        if (
            approved is True
            and self.gate.mode is SafetyMode.APPROVAL_REQUIRED
            and self.gate.approver is None
        ):
            effective_gate = SafetyGate(
                self.gate.mode, self.gate.budget, lambda _preview: True
            )
        outcome.approved = True

        # Baseline: what passes now, before anything changes.
        try:
            before_report = self._verify(plan.current_spec)
            outcome.original_before = _criteria_map(before_report)
            outcome.original_report = before_report.to_dict()
        except Exception as exc:
            outcome.problems.append(f"baseline verification failed: {exc}")
            outcome.original_before = {}

        # Backup every existing device the change touches. A device the
        # enhancement CREATES has no prior state to back up; requiring one
        # would block every legitimate addition.
        if self.backup is not None:
            existing: list[str] = []
            for name in plan.affected_devices:
                try:
                    present = self.adapter.device_exists(name)
                except Exception:
                    present = None
                if present is not None and present.is_known and present.value:
                    existing.append(name)
            manifest = self.backup.create(
                f"enhance-{plan.desired_spec.name}",
                devices=existing,
                intended_changes=plan.operations,
            )
            outcome.backup = manifest
            validation = self.backup.validate(manifest)
            outcome.backup_validation = validation
            if not validation.available:
                outcome.blocked_reason = (
                    "mandatory backup could not be verified: "
                    + "; ".join(validation.problems)
                )
                self._record("enhancement_blocked", plan, outcome)
                return outcome

        # Execute only the operations that differ from the current build.
        current_compile = compile_spec(
            plan.current_spec, plan_addressing(plan.current_spec)
        )
        desired_compile = compile_spec(
            plan.desired_spec, plan_addressing(plan.desired_spec)
        )
        current_keys = {
            node.operation.idempotency_key
            for node in current_compile.dag.nodes.values()
        }
        changed = [
            node.operation
            for node in desired_compile.dag.nodes.values()
            if node.operation.idempotency_key not in current_keys
        ]
        dag = OperationDag()
        ids = {op.operation_id for op in changed}
        for op in changed:
            # Dependencies that already exist are satisfied by the current
            # build; keep only edges inside the changed set.
            dag.add(
                op.__class__(
                    operation_id=op.operation_id,
                    operation_type=op.operation_type,
                    inputs=op.inputs,
                    dependencies=tuple(d for d in op.dependencies if d in ids),
                    preconditions=op.preconditions,
                    expected_postconditions=op.expected_postconditions,
                    classification=op.classification,
                    risk=op.risk,
                    blast_radius=op.blast_radius,
                    retry_policy=op.retry_policy,
                    idempotency_key=op.idempotency_key,
                )
            )
        executor = DagExecutor(self.adapter, effective_gate, self.evidence)
        result = executor.execute(dag)
        outcome.dag_summary = dag.summary()
        outcome.applied_operations = [
            op_id
            for op_id, node in dag.nodes.items()
            if node.status in (OpStatus.SUCCEEDED, OpStatus.SKIPPED)
        ]
        outcome.executed = (
            dag.summary()["failed"] == 0 and dag.summary()["blocked"] == 0
        )

        # Reverify the desired network and every previously passing criterion.
        after_report = None
        try:
            after_report = self._verify(plan.desired_spec)
            outcome.desired_report = after_report.to_dict()
            outcome.desired_after = _criteria_map(after_report)
            outcome.desired_satisfied = after_report.passed
        except Exception as exc:
            outcome.problems.append(f"desired verification failed: {exc}")
        try:
            original_after_report = self._verify(plan.current_spec)
            outcome.original_after = _criteria_map(original_after_report)
        except Exception as exc:
            outcome.problems.append(f"original re-verification failed: {exc}")
        for criterion_id, verdict in outcome.original_before.items():
            if verdict == Verdict.PASS.value and outcome.original_after.get(criterion_id) != Verdict.PASS.value:
                outcome.regressions.append(
                    f"{criterion_id}: was pass, now "
                    f"{outcome.original_after.get(criterion_id, 'unknown')}"
                )
        # `preserved` means: every previously passing criterion still passes.
        outcome.preserved = not outcome.regressions
        before_ids = set(outcome.original_before)
        new_criteria = [
            result.criterion_id
            for result in after_report.acceptance.results
            if result.criterion_id not in before_ids
            and result.verdict != Verdict.PASS
        ] if after_report is not None else []
        if new_criteria:
            outcome.problems.append(
                "new criteria not satisfied: " + ", ".join(new_criteria)
            )
        outcome.success = (
            outcome.executed
            and outcome.preserved
            and not new_criteria
        )

        if not outcome.executed or outcome.regressions:
            self._revert(plan, outcome)
        self._record("enhancement_outcome", plan, outcome)
        return outcome

    # -- verification -------------------------------------------------------

    def _verify(self, spec: NetworkSpec) -> VerificationReport:
        plan = plan_addressing(spec)
        compiled = compile_spec(spec, plan)
        names = [d.id for d in spec.devices]
        state = self.adapter.observe_state(names)
        link_endpoints = {
            node.operation.inputs["link_id"]: (
                node.operation.inputs["a_device"],
                node.operation.inputs["a_port"],
                node.operation.inputs["b_device"],
                node.operation.inputs["b_port"],
            )
            for node in compiled.dag.nodes.values()
            if node.operation.operation_type == "create_link"
        }
        flow_results: dict[tuple[str, str], FlowResult] = {}
        for criterion in spec.acceptance_criteria:
            test = criterion.test
            if not isinstance(test, FlowTestSpec):
                continue
            flow_results[(test.source, test.destination)] = self.adapter.run_flow_test(
                test.source,
                test.destination,
                transport=test.transport,
                port=test.port,
                destination_ip=test.destination_ip,
            )
        observations_complete = all(
            state.devices.get(name) is not None for name in names
        )
        engine = VerificationEngine(self.adapter)
        return engine.verify(
            spec,
            plan,
            state,
            flow_results,
            link_endpoints=link_endpoints,
            observations_complete=observations_complete,
            assignment_ports=compiled.assignment_ports,
        )

    # -- revert -------------------------------------------------------------

    def _revert(self, plan: EnhancementPlan, outcome: EnhancementOutcome) -> None:
        outcome.reverted = True
        current_compile = compile_spec(
            plan.current_spec, plan_addressing(plan.current_spec)
        )
        applied = 0
        for node in current_compile.dag.nodes.values():
            op = node.operation
            if op.operation_type != "configure_ios":
                continue
            device = str(op.inputs.get("device") or "")
            if device not in plan.affected_devices:
                continue
            try:
                receipt = self.adapter.configure_ios(
                    device, [str(c) for c in op.inputs.get("commands") or []]
                )
                if str(getattr(receipt.result, "value", "")) == "success":
                    applied += 1
            except Exception as exc:
                outcome.problems.append(f"revert {device} failed: {exc}")
        outcome.revert_detail = (
            f"re-pushed the current configuration to {applied} device(s); "
            "created devices are not removed automatically"
            if applied else
            "no configuration could be re-pushed; review the backup manifest"
        )

    def _record(
        self, kind: str, plan: EnhancementPlan, outcome: EnhancementOutcome
    ) -> None:
        if self.evidence is None:
            return
        self.evidence.record(
            f"brownfield_{kind}",
            {"plan": plan.preview(), "outcome": outcome.to_dict()},
            component="brownfield.enhance",
        )


def _criteria_map(report: VerificationReport) -> dict[str, str]:
    return {
        result.criterion_id: result.verdict.value
        for result in report.acceptance.results
    }
