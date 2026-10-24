"""Safety-first brownfield repair (S4.10).

The loop is deliberately narrow and evidence-bound:

    confirm objective -> plan minimal fix -> explicit approval -> mandatory
    verified backup -> apply -> read back -> prove the objective -> re-run
    every previously passing check -> keep, or roll back and say so

Nothing is applied speculatively. A fix with no rollback path is refused
before the safety gate; an approval that is not granted leaves the network
untouched; a backup that cannot be verified stops the run; and a repair that
does not restore the objective is reverted, with any incomplete rollback
reported rather than hidden.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from ..adapter.base import PacketTracerAdapter
from ..core.observability import Observable
from ..core.requirements import FlowOutcome, FlowResult, Verdict
from ..core.safety import BudgetExceeded, SafetyGate, SafetyMode
from ..evidence.store import EvidenceStore
from .backup import BackupManager, BackupManifest, BackupValidation
from .config_reader import RunningConfigReader
from .models import Diagnostic, FixAction, Objective
from .objectives import ObjectiveRegistry
from .parser import IosConfigParser


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class FlowProbe:
    """One previously passing check, re-runnable after a repair."""

    key: str
    source: str
    destination: str
    expect: str = "reachable"
    transport: str = "icmp"
    port: int | None = None
    destination_ip: str | None = None
    objective_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "source": self.source,
            "destination": self.destination,
            "expect": self.expect,
            "transport": self.transport,
            "port": self.port,
            "destination_ip": self.destination_ip,
            "objective_id": self.objective_id,
        }


@dataclass
class RepairPlan:
    repair_id: str
    diagnosis_ids: tuple[str, ...]
    objective_ids: tuple[str, ...]
    actions: tuple[FixAction, ...]
    affected_devices: tuple[str, ...]
    expected_improvement: str
    verification: tuple[str, ...]
    blocked_reason: str | None = None
    risk: str = "medium"
    preview: dict[str, Any] = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return self.blocked_reason is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "repair_id": self.repair_id,
            "diagnosis_ids": list(self.diagnosis_ids),
            "objective_ids": list(self.objective_ids),
            "actions": [a.to_dict() for a in self.actions],
            "affected_devices": list(self.affected_devices),
            "expected_improvement": self.expected_improvement,
            "verification": list(self.verification),
            "blocked": self.blocked,
            "blocked_reason": self.blocked_reason,
            "risk": self.risk,
            "preview": dict(self.preview),
        }


@dataclass
class RepairOutcome:
    repair_id: str
    executed: bool = False
    blocked_reason: str | None = None
    approved: bool = False
    backup: BackupManifest | None = None
    backup_validation: BackupValidation | None = None
    applied: list[dict[str, Any]] = field(default_factory=list)
    readback: list[dict[str, Any]] = field(default_factory=list)
    objective_verdict: str = Verdict.NOT_RUN.value
    objective_detail: str | None = None
    baseline: dict[str, str] = field(default_factory=dict)
    post_repair: dict[str, str] = field(default_factory=dict)
    regressions: list[str] = field(default_factory=list)
    rolled_back: bool = False
    rollback_complete: bool = False
    rollback_detail: str | None = None
    problems: list[str] = field(default_factory=list)
    finished_at: str = ""

    @property
    def repaired(self) -> bool:
        return (
            self.executed
            and self.objective_verdict == Verdict.PASS.value
            and not self.regressions
            and not self.rolled_back
        )

    @property
    def incorrect_fix_left_in_place(self) -> bool:
        return (
            self.executed
            and self.objective_verdict != Verdict.PASS.value
            and not self.rolled_back
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "repair_id": self.repair_id,
            "executed": self.executed,
            "blocked_reason": self.blocked_reason,
            "approved": self.approved,
            "backup": self.backup.to_dict() if self.backup else None,
            "backup_validation": (
                self.backup_validation.to_dict() if self.backup_validation else None
            ),
            "applied": list(self.applied),
            "readback": list(self.readback),
            "objective_verdict": self.objective_verdict,
            "objective_detail": self.objective_detail,
            "baseline": dict(self.baseline),
            "post_repair": dict(self.post_repair),
            "regressions": list(self.regressions),
            "rolled_back": self.rolled_back,
            "rollback_complete": self.rollback_complete,
            "rollback_detail": self.rollback_detail,
            "problems": list(self.problems),
            "repaired": self.repaired,
            "incorrect_fix_left_in_place": self.incorrect_fix_left_in_place,
            "finished_at": self.finished_at,
        }


class BrownfieldRepairEngine:
    """Plan and execute minimal repairs with mandatory recovery."""

    def __init__(
        self,
        adapter: PacketTracerAdapter,
        gate: SafetyGate,
        *,
        evidence: EvidenceStore | None = None,
        objectives: ObjectiveRegistry | None = None,
        backup: BackupManager | None = None,
        reader: RunningConfigReader | None = None,
    ):
        self.adapter = adapter
        self.gate = gate
        self.evidence = evidence
        self.objectives = objectives or ObjectiveRegistry()
        self.backup = backup
        self.reader = reader or RunningConfigReader(adapter, evidence=evidence)
        self.parser = IosConfigParser()
        self._last_manifest: BackupManifest | None = None
        self._last_verify_detail: str | None = None

    # -- planning -----------------------------------------------------------

    def plan(
        self,
        diagnostics: Iterable[Diagnostic],
        *,
        objective: Objective | None = None,
    ) -> RepairPlan:
        from ..core.ids import new_id

        diagnostics = list(diagnostics)
        objective_ids: list[str] = []
        diagnosis_ids: list[str] = []
        actions: list[FixAction] = []
        blocked: list[str] = []
        for diagnostic in diagnostics:
            diagnosis_ids.append(diagnostic.diagnosis_id)
            resolved = self.objectives.get(diagnostic.objective_id)
            if resolved is None and objective is not None:
                resolved = objective
            if resolved is None:
                blocked.append(
                    f"{diagnostic.cause_class}: no objective is attached; "
                    "repairing an unasked-for change is refused"
                )
                continue
            if not resolved.actionable:
                blocked.append(
                    f"{diagnostic.cause_class}: objective {resolved.id} is "
                    "inferred and not confirmed"
                )
                continue
            objective_ids.append(resolved.id)
            if not diagnostic.repairable:
                blocked.append(
                    f"{diagnostic.cause_class}: diagnosis is not repairable "
                    f"(status={diagnostic.status}, confidence={diagnostic.confidence}, "
                    f"suspected={diagnostic.suspected})"
                )
                continue
            for action in diagnostic.recommended_fix:
                if not action.rollback_commands and action.operation == "configure_ios":
                    blocked.append(
                        f"{diagnostic.cause_class}: fix on {action.device} has no "
                        "rollback path; refused before mutating"
                    )
                    continue
                actions.append(action)
        # The same command block twice (e.g. a trace and a config rule found
        # the same port) is one mutation, not two.
        unique: dict[tuple[Any, ...], FixAction] = {}
        for action in actions:
            key = (
                action.device, action.operation, tuple(action.commands),
                tuple(sorted(action.host_config.items())),
            )
            unique.setdefault(key, action)
        actions = list(unique.values())
        affected = tuple(sorted({a.device for a in actions}))
        verification = sorted(
            {
                f"objective {o}: {self.objectives.get(o).statement}"
                for o in objective_ids
                if self.objectives.get(o) is not None
            }
        )
        return RepairPlan(
            repair_id=new_id("s4repair"),
            diagnosis_ids=tuple(diagnosis_ids),
            objective_ids=tuple(dict.fromkeys(objective_ids)),
            actions=tuple(actions),
            affected_devices=affected,
            expected_improvement=(
                f"restore {', '.join(sorted(set(objective_ids)))}"
                if objective_ids else "none"
            ),
            verification=tuple(verification),
            blocked_reason="; ".join(blocked) if blocked and not actions else None,
            risk="medium",
            preview={
                "mutations": len(actions),
                "devices": list(affected),
                "operations": [
                    {
                        "device": action.device,
                        "operation": action.operation,
                        "commands": list(action.commands),
                        "host_config": dict(action.host_config),
                    }
                    for action in actions
                ],
                "skipped_diagnostics": list(blocked),
            },
        )

    # -- execution ----------------------------------------------------------

    def execute(
        self,
        plan: RepairPlan,
        *,
        objective: Objective | None = None,
        baseline: Iterable[FlowProbe] = (),
        approved: bool | None = None,
    ) -> RepairOutcome:
        outcome = RepairOutcome(repair_id=plan.repair_id)
        if plan.blocked:
            outcome.blocked_reason = plan.blocked_reason
            self._record("repair_blocked", plan.to_dict(), outcome)
            return outcome
        if not plan.actions:
            outcome.blocked_reason = "no applicable fix was proven"
            self._record("repair_blocked", plan.to_dict(), outcome)
            return outcome
        objective = objective or next(
            (self.objectives.get(oid) for oid in plan.objective_ids), None
        )
        if objective is None or not objective.actionable:
            outcome.blocked_reason = (
                "no confirmed objective: inferred intent may be diagnosed but "
                "not repaired"
            )
            self._record("repair_blocked", plan.to_dict(), outcome)
            return outcome

        # 1) Already healthy? Then there is nothing to mutate.
        pre = self.verify_objective(objective)
        if pre == Verdict.PASS:
            outcome.blocked_reason = (
                f"objective {objective.id} already passes; no change is needed"
            )
            self._record("repair_skipped", plan.to_dict(), outcome)
            return outcome

        # 2) Explicit approval through the one safety gate.
        if not self._approve(plan, approved, outcome):
            self._record("repair_not_approved", plan.to_dict(), outcome)
            return outcome
        outcome.approved = True

        # 3) Mandatory, validated backup before any mutation.
        if self.backup is None:
            outcome.blocked_reason = (
                "no backup manager is configured; repair is refused because "
                "recovery protection cannot be established"
            )
            self._record("repair_blocked", plan.to_dict(), outcome)
            return outcome
        intended = [
            {
                "device": action.device,
                "operation": action.operation,
                "commands": list(action.commands),
                "host_config": dict(action.host_config),
            }
            for action in plan.actions
        ]
        manifest = self.backup.create(
            f"{getattr(self.adapter, 'name', 'brownfield')}-{plan.repair_id}",
            devices=plan.affected_devices,
            intended_changes=intended,
        )
        self._last_manifest = manifest
        outcome.backup = manifest
        validation = self.backup.validate(manifest)
        outcome.backup_validation = validation
        if not validation.available:
            outcome.blocked_reason = (
                "mandatory backup could not be verified: "
                + "; ".join(validation.problems)
            )
            self._record("repair_blocked", plan.to_dict(), outcome)
            return outcome

        # 4) Apply, then read back what the device actually holds.
        probe_list = list(baseline)
        if not probe_list:
            probe_list = self.baseline_probes(exclude=objective.id)
        outcome.baseline = self._run_probes(probe_list)
        applied_all = True
        for action in plan.actions:
            result = self._apply(action)
            outcome.applied.append(result)
            if not result["applied"]:
                applied_all = False
                outcome.problems.append(result.get("detail") or "apply failed")
                break
            readback = self._readback(action)
            outcome.readback.append(readback)
            if not readback["confirmed"]:
                applied_all = False
                outcome.problems.append(
                    f"{action.device}: read-back did not confirm the change: "
                    f"{readback.get('detail')}"
                )
                break
        outcome.executed = applied_all

        # 5) Prove the objective, then every previously passing check.
        if applied_all:
            verdict = self.verify_objective(objective)
            outcome.objective_verdict = verdict.value
            outcome.objective_detail = self._last_verify_detail
        else:
            outcome.objective_verdict = Verdict.UNKNOWN.value
            outcome.objective_detail = "the fix did not apply/read back"

        outcome.post_repair = self._run_probes(probe_list)
        for key, before in outcome.baseline.items():
            after = outcome.post_repair.get(key)
            if before == Verdict.PASS.value and after != Verdict.PASS.value:
                outcome.regressions.append(
                    f"{key}: was pass, now {after or 'unknown'}"
                )

        # 6) Keep only what worked without regressions; otherwise roll back.
        if outcome.objective_verdict != Verdict.PASS.value or outcome.regressions:
            self._rollback(plan, outcome)

        outcome.finished_at = _now()
        self._record("repair_outcome", plan.to_dict(), outcome)
        return outcome

    # -- approval -----------------------------------------------------------

    def _approve(
        self, plan: RepairPlan, approved: bool | None, outcome: RepairOutcome
    ) -> bool:
        requested = [
            {
                "operation": action.operation,
                "device": action.device,
                "commands": list(action.commands),
                "host_config": dict(action.host_config),
            }
            for action in plan.actions
        ]
        if self.gate.mode is SafetyMode.APPROVAL_REQUIRED:
            if approved is False:
                outcome.blocked_reason = "explicit approval was withheld"
                return False
            if approved is True and self.gate.approver is None:
                gate = SafetyGate(
                    self.gate.mode, self.gate.budget, lambda _preview: True
                )
                try:
                    permission = gate.authorize(requested)
                except BudgetExceeded as exc:
                    outcome.blocked_reason = f"mutation budget: {exc}"
                    return False
                if self.evidence is not None:
                    self.evidence.record(
                        "brownfield_repair_approved",
                        {
                            "repair_id": plan.repair_id,
                            "approval": "explicit (approved=True)",
                            "preview": plan.preview,
                        },
                        component="brownfield.repair",
                    )
                return permission.execute
        try:
            permission = self.gate.authorize(requested)
        except BudgetExceeded as exc:
            outcome.blocked_reason = f"mutation budget: {exc}"
            return False
        if not permission.execute:
            reason = None
            for receipt in permission.planned_receipts:
                reason = (receipt.evidence or {}).get("reason") or receipt.error
                if reason:
                    break
            outcome.blocked_reason = (
                f"safety gate refused the repair ({self.gate.mode.value}): "
                f"{reason or 'no reason reported'}"
            )
            return False
        return True

    # -- mutation -----------------------------------------------------------

    def _apply(self, action: FixAction) -> dict[str, Any]:
        record: dict[str, Any] = {
            "device": action.device,
            "operation": action.operation,
            "commands": list(action.commands),
            "applied": False,
            "detail": None,
        }
        try:
            if action.operation == "configure_ios":
                receipt = self.adapter.configure_ios(action.device, list(action.commands))
            elif action.operation == "configure_host":
                receipt = self.adapter.configure_host(
                    action.device, **action.host_config
                )
            elif action.operation == "create_link":
                payload = action.expected_state
                receipt = self.adapter.create_link(
                    str(payload.get("a_device")), str(payload.get("a_port")),
                    str(payload.get("b_device")), str(payload.get("b_port")),
                    media=str(payload.get("media") or "auto"),
                )
            else:
                record["detail"] = f"unsupported repair operation {action.operation!r}"
                return record
            result = str(getattr(getattr(receipt, "result", None), "value", ""))
            record["receipt"] = result
            record["adapter_error"] = getattr(receipt, "error", None)
            record["applied"] = result == "success"
            if not record["applied"]:
                record["detail"] = getattr(receipt, "error", None) or result
        except Exception as exc:
            record["detail"] = f"{type(exc).__name__}: {exc}"
        return record

    def _readback(self, action: FixAction) -> dict[str, Any]:
        """Read what the device actually holds; never trust the push receipt."""
        expected = dict(action.expected_state)
        if action.operation == "configure_host":
            try:
                answer = self.adapter.read_host_ip_config(action.device)
            except Exception as exc:
                return {"confirmed": False, "detail": f"read failed: {exc}"}
            if not answer.is_known:
                return {
                    "confirmed": False,
                    "detail": f"host config unreadable: {answer.detail or answer.status.value}",
                }
            data = dict(answer.value or {})
            checks = {
                "ip": data.get("ip"),
                "mask": data.get("mask"),
                "gateway": data.get("gateway"),
                "dns": (data.get("dns") or [None])[0],
            }
            for key, wanted in expected.items():
                if key in checks and wanted is not None and checks[key] != wanted:
                    return {
                        "confirmed": False,
                        "detail": f"{key}: expected {wanted!r}, read {checks[key]!r}",
                    }
            return {"confirmed": True, "detail": "host config read back", "observed": checks}
        if action.operation == "create_link":
            payload = action.expected_state
            try:
                exists = self.adapter.link_exists(
                    str(payload.get("a_device")), str(payload.get("a_port")),
                    str(payload.get("b_device")), str(payload.get("b_port")),
                )
            except Exception as exc:
                return {"confirmed": False, "detail": f"link read failed: {exc}"}
            return {
                "confirmed": bool(exists.is_known and exists.value),
                "detail": (
                    "link read back present"
                    if exists.is_known and exists.value
                    else "link not observed after creation"
                ),
            }
        # IOS/ASA: read the configuration back and compare the intended field.
        try:
            answer = self.adapter.read_running_config(action.device)
        except Exception as exc:
            return {"confirmed": False, "detail": f"config read failed: {exc}"}
        if not answer.is_known:
            return {
                "confirmed": False,
                "detail": f"configuration unreadable: {answer.detail or answer.status.value}",
            }
        parsed = self.parser.parse(str(answer.value), device=action.device)
        ok, detail = _expected_state_present(parsed, expected, action.commands)
        return {"confirmed": ok, "detail": detail}

    # -- verification -------------------------------------------------------

    _last_verify_detail: str | None = None

    def verify_objective(self, objective: Objective) -> Verdict:
        self._last_verify_detail = None
        if objective.kind in ("reachability", "isolation"):
            source = str(objective.params.get("source") or "")
            destination = str(objective.params.get("destination") or source)
            if not source:
                self._last_verify_detail = "objective has no source device"
                return Verdict.UNKNOWN
            try:
                flow = self.adapter.run_flow_test(
                    source,
                    destination,
                    transport=str(objective.params.get("transport") or "icmp"),
                    port=objective.params.get("port"),
                    destination_ip=objective.params.get("destination_ip"),
                )
            except Exception as exc:
                self._last_verify_detail = f"flow probe raised: {exc}"
                return Verdict.UNKNOWN
            self._last_verify_detail = f"{flow.outcome.value}: {flow.detail or ''}".strip()
            if objective.kind == "isolation" or objective.params.get("expect") == "blocked":
                return Verdict.PASS if flow.outcome is FlowOutcome.BLOCKED else (
                    Verdict.FAIL if flow.outcome is FlowOutcome.REACHABLE else Verdict.UNKNOWN
                )
            if flow.outcome is FlowOutcome.REACHABLE:
                return Verdict.PASS
            if flow.outcome in (FlowOutcome.BLOCKED, FlowOutcome.PARTIAL):
                return Verdict.FAIL
            return Verdict.UNKNOWN
        if objective.kind == "addressing":
            device = str(objective.params.get("device") or "")
            try:
                answer = self.adapter.read_host_ip_config(device)
            except Exception as exc:
                self._last_verify_detail = f"host config read raised: {exc}"
                return Verdict.UNKNOWN
            if not answer.is_known:
                self._last_verify_detail = str(answer.detail or answer.status.value)
                return Verdict.UNKNOWN
            ip = (answer.value or {}).get("ip")
            self._last_verify_detail = f"host address: {ip}"
            return Verdict.PASS if ip else Verdict.FAIL
        if objective.kind == "dns":
            device = str(objective.params.get("device") or "")
            try:
                answer = self.adapter.read_host_ip_config(device)
            except Exception as exc:
                self._last_verify_detail = f"host config read raised: {exc}"
                return Verdict.UNKNOWN
            if not answer.is_known:
                self._last_verify_detail = str(answer.detail or answer.status.value)
                return Verdict.UNKNOWN
            servers = (answer.value or {}).get("dns") or []
            self._last_verify_detail = f"DNS servers: {servers}"
            return Verdict.PASS if servers else Verdict.FAIL
        self._last_verify_detail = f"no verifier for objective kind {objective.kind!r}"
        return Verdict.UNKNOWN

    def baseline_probes(self, *, exclude: str | None = None) -> list[FlowProbe]:
        """Every confirmed flow objective that is not the one being repaired.

        These are the "previously passing criteria" the repair must not break;
        their pass/fail is measured before the change, not assumed.
        """
        probes: list[FlowProbe] = []
        for objective in self.objectives.confirmed():
            if objective.id == exclude:
                continue
            if objective.kind not in ("reachability", "isolation"):
                continue
            source = str(objective.params.get("source") or "")
            destination = str(objective.params.get("destination") or source)
            if not source:
                continue
            probes.append(
                FlowProbe(
                    key=f"{objective.id}:{source}->{destination}",
                    source=source,
                    destination=destination,
                    expect=(
                        "blocked"
                        if objective.kind == "isolation"
                        or objective.params.get("expect") == "blocked"
                        else "reachable"
                    ),
                    transport=str(objective.params.get("transport") or "icmp"),
                    port=objective.params.get("port"),
                    destination_ip=objective.params.get("destination_ip"),
                    objective_id=objective.id,
                )
            )
        return probes

    def _run_probes(self, probes: Iterable[FlowProbe]) -> dict[str, str]:
        out: dict[str, str] = {}
        for probe in probes:
            try:
                flow = self.adapter.run_flow_test(
                    probe.source,
                    probe.destination,
                    transport=probe.transport,
                    port=probe.port,
                    destination_ip=probe.destination_ip,
                )
            except Exception:
                out[probe.key] = Verdict.UNKNOWN.value
                continue
            out[probe.key] = _flow_verdict(probe.expect, flow).value
        return out

    # -- rollback -----------------------------------------------------------

    def _rollback(self, plan: RepairPlan, outcome: RepairOutcome) -> None:
        outcome.rolled_back = True
        requested = [
            {
                "operation": action.operation,
                "device": action.device,
                "commands": list(action.rollback_commands),
            }
            for action in plan.actions
            if action.rollback_commands or action.operation == "configure_host"
        ]
        permission = self.gate.authorize(requested)
        if not permission.execute:
            outcome.rollback_complete = False
            outcome.rollback_detail = (
                "rollback was refused by the safety gate; the network still "
                "holds the unsuccessful change"
            )
            if self.evidence is not None:
                self.evidence.record(
                    "brownfield_rollback_failed",
                    {"repair": plan.to_dict(), "outcome": outcome.to_dict()},
                    component="brownfield.repair",
                )
            return
        failures: list[str] = []
        for action in reversed(plan.actions):
            try:
                if action.operation == "create_link":
                    payload = action.expected_state
                    self.adapter.delete_link(
                        str(payload.get("a_device")), str(payload.get("a_port")),
                        str(payload.get("b_device")), str(payload.get("b_port")),
                    )
                elif action.operation == "configure_host":
                    # Restore the host's prior read from its backup entry.
                    prior = self._prior_host_config(action.device)
                    if prior is None:
                        failures.append(f"{action.device}: no prior host config")
                        continue
                    self.adapter.configure_host(action.device, **prior)
                elif action.rollback_commands:
                    self.adapter.configure_ios(
                        action.device, list(action.rollback_commands)
                    )
            except Exception as exc:
                failures.append(f"{action.device}: {type(exc).__name__}: {exc}")
        outcome.rollback_detail = (
            "all changes reverted and read back"
            if not failures else "; ".join(failures)
        )
        outcome.rollback_complete = not failures
        if not failures:
            verified, detail = self._verify_rollback(plan)
            outcome.rollback_complete = verified
            outcome.rollback_detail = detail
        if self.evidence is not None:
            self.evidence.record(
                "brownfield_rollback",
                {
                    "repair": plan.to_dict(),
                    "failures": failures,
                    "complete": outcome.rollback_complete,
                    "detail": outcome.rollback_detail,
                },
                component="brownfield.repair",
            )

    def _verify_rollback(self, plan: RepairPlan) -> tuple[bool, str]:
        """Read the reverted state back and compare it with the backup.

        "The rollback commands were accepted" is not a rollback. This reads
        each device's configuration/host state after the revert and compares
        the semantic projection (or the host fields) with the file saved
        before the repair; any difference is reported as an incomplete
        rollback.
        """
        manifest = self._last_manifest
        if manifest is None:
            return False, "no backup manifest is available to compare against"
        problems: list[str] = []
        for device in plan.affected_devices:
            entry = manifest.entry_for(device)
            if entry is None or not entry.path:
                problems.append(f"{device}: no saved state to compare")
                continue
            from pathlib import Path

            saved = Path(entry.path)
            if not saved.exists():
                problems.append(f"{device}: saved state is missing")
                continue
            if entry.kind == "host_config":
                try:
                    import json

                    before = json.loads(saved.read_text(encoding="utf-8"))
                    answer = self.adapter.read_host_ip_config(device)
                except Exception as exc:
                    problems.append(f"{device}: host read failed: {exc}")
                    continue
                if not answer.is_known:
                    problems.append(f"{device}: host state unreadable after rollback")
                    continue
                after = dict(answer.value or {})
                for key in ("ip", "mask", "gateway"):
                    if before.get(key) != after.get(key):
                        problems.append(
                            f"{device}: {key} is {after.get(key)!r}, backup "
                            f"held {before.get(key)!r}"
                        )
                continue
            try:
                before_text = saved.read_text(encoding="utf-8")
                answer = self.adapter.read_running_config(device)
            except Exception as exc:
                problems.append(f"{device}: config read failed: {exc}")
                continue
            if not answer.is_known:
                problems.append(f"{device}: config unreadable after rollback")
                continue
            after_text = str(answer.value)
            before = self.parser.parse(before_text, device=device)
            after = self.parser.parse(after_text, device=device)
            from .parser import semantic_projection

            before_view = semantic_projection(before)
            after_view = semantic_projection(after)
            # Only compare the intent-bearing fields the repair could touch;
            # an unrelated operational line changing during the window is not
            # a failed rollback.
            for field in ("interfaces", "static_routes", "vlans"):
                if before_view.get(field) != after_view.get(field):
                    problems.append(
                        f"{device}: {field} differs from the pre-repair backup"
                    )
        if problems:
            return False, "rollback incomplete: " + "; ".join(problems)
        return True, "rollback verified against the pre-repair backup"

    def _prior_host_config(self, device: str) -> dict[str, Any] | None:
        manifest = self._last_manifest
        if manifest is None:
            return None
        entry = manifest.entry_for(device)
        if entry is None or not entry.path:
            return None
        try:
            import json
            from pathlib import Path

            data = json.loads(Path(entry.path).read_text(encoding="utf-8"))
        except Exception:
            return None
        dns = data.get("dns")
        if isinstance(dns, list):
            dns = dns[0] if dns else None
        return {
            "dhcp": bool(data.get("dhcp")),
            "ip": data.get("ip"),
            "mask": data.get("mask"),
            "gateway": data.get("gateway"),
            "dns": dns,
        }

    # -- evidence -----------------------------------------------------------

    def _record(
        self, kind: str, plan: dict[str, Any], outcome: RepairOutcome | None
    ) -> None:
        if self.evidence is None:
            return
        self.evidence.record(
            f"brownfield_{kind}",
            {"plan": plan, "outcome": outcome.to_dict() if outcome else None},
            component="brownfield.repair",
        )


def _flow_verdict(expect: str, flow: FlowResult) -> Verdict:
    if expect == "blocked":
        if flow.outcome is FlowOutcome.BLOCKED:
            return Verdict.PASS
        if flow.outcome is FlowOutcome.REACHABLE:
            return Verdict.FAIL
        return Verdict.UNKNOWN
    if flow.outcome is FlowOutcome.REACHABLE:
        return Verdict.PASS
    if flow.outcome in (FlowOutcome.BLOCKED, FlowOutcome.PARTIAL):
        return Verdict.FAIL
    return Verdict.UNKNOWN


def _expected_state_present(
    parsed, expected: dict[str, Any], commands: list[str]
) -> tuple[bool, str]:
    """Is the intended change visible in the parsed read-back?"""
    if not expected:
        return True, "no field expectation; receipt only"
    for key, wanted in expected.items():
        if key == "access_vlan":
            interface = _interface_from_commands(parsed, commands)
            if interface is None or interface.access_vlan != wanted:
                return False, f"access VLAN not read back ({interface.access_vlan if interface else 'no interface'})"
        elif key == "add_vlans":
            interface = _interface_from_commands(parsed, commands)
            have = set(interface.trunk_allowed or []) if interface else set()
            missing = set(wanted) - have
            if missing:
                return False, f"trunk allowed VLANs missing {sorted(missing)}"
        elif key == "native_vlan":
            interface = _interface_from_commands(parsed, commands)
            if interface is None or interface.trunk_native != wanted:
                return False, "native VLAN not read back"
        elif key in ("ip", "mask"):
            interface = _interface_from_commands(parsed, commands)
            observed = interface.ip if key == "ip" else interface.mask
            if interface is None or observed != wanted:
                return False, f"{key} not read back ({observed})"
        elif key == "vlan_defined":
            if int(wanted) not in parsed.vlans:
                return False, f"VLAN {wanted} not read back"
        elif key == "route":
            cidr = str(wanted)
            if not any(route.cidr == cidr for route in parsed.static_routes):
                return False, f"route {cidr} not read back"
        elif key == "acl_rule_removed":
            raw = str(wanted)
            if any(rule.raw.strip() == raw for rule in parsed.all_acl_rules()):
                return False, "the blocking ACL rule is still present"
        elif key == "acl_binding_removed":
            interface = _interface_from_commands(parsed, commands)
            if interface is not None and str(wanted) in (
                interface.acl_in, interface.acl_out
            ):
                return False, "the ACL is still bound to the interface"
        elif key == "nameif":
            interface = _interface_from_commands(parsed, commands)
            if interface is None or interface.nameif != wanted:
                return False, "nameif not read back"
        elif key == "security_level":
            interface = _interface_from_commands(parsed, commands)
            if interface is None or interface.security_level != wanted:
                return False, "security-level not read back"
        elif key == "state":
            interface = _interface_from_commands(parsed, commands)
            if interface is None or interface.shutdown is not False:
                return False, "interface is still shut down"
        elif key == "link_id":
            continue
        elif key == "gateway":
            continue  # host config is read through ipconfig
        else:
            # Unknown expectation: the presence of the exact command is the
            # best honest check available.
            text = "\n".join(
                line.text for line in parsed.lines
            )
            if wanted is not None and str(wanted) not in text:
                return False, f"expected value {wanted!r} not present in config"
    return True, "intended state read back"


def _interface_from_commands(parsed, commands: list[str]):
    for command in commands:
        stripped = command.strip()
        if stripped.lower().startswith("interface "):
            return parsed.interface(stripped.split(None, 1)[1])
    return None
