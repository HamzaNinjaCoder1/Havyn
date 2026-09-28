"""The end-to-end autopilot: spec -> plan -> compile -> execute -> verify.

One pipeline for both the offline contract environment (NullAdapter) and the
real Packet Tracer bridge â€” only the adapter differs, by design.

The orchestrator records every stage into the evidence store and produces the
BuildReport. It never upgrades a verdict: if verification could not check
something, the report says UNKNOWN, and overall success requires every test to
have actually passed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .adapter.base import PacketTracerAdapter
from .acceptance.evaluator import evaluate_spec
from .compiler.compiler import CompileError, compile_spec
from .compiler.dag import OperationDag
from .compiler.executor import DagExecutor
from .compiler.firewall_policy import PolicyCompileError
from .core.build_errors import PreflightBlocked
from .core.network_spec import FlowTestSpec, NetworkSpec
from .core.observability import Observable
from .core.actual_state import LinkObservation
from .core.requirements import FlowOutcome, FlowResult
from .core.safety import MutationBudget, SafetyGate, SafetyMode
from .diff.engine import diff_expected_vs_actual
from .evidence.store import EvidenceStore
from .ipam.planner import PlanningFailure, plan_addressing
from .ipam.validator import has_errors, validate_plan
from .repair.engine import RepairEngine
from .verification.engine import VerificationEngine, VerificationReport


#: operation_type -> phase family for the in-execute breakdown (S1.5).
#: The blueprint wants create / cable / configure / flows separated; anything
#: not named here (read-back sweeps, save, future types) lands in
#: ``execute_other`` so the table never hides time it cannot explain.
_OP_FAMILIES: dict[str, str] = {
    "create_device": "create",
    "install_module": "create",
    "install_wireless_radio": "create",
    "create_link": "cable",
    "associate_wireless": "cable",
    "configure_host": "configure",
    "configure_ios": "configure",
    "configure_gateway_lan": "configure",
    "verify_flow": "flows",
    "wait_dhcp": "flows",
    "verify_state": "execute_other",
    "save_project": "execute_other",
}

#: (key, note) rows of the report's phase table, in print order. The family
#: rows are subsets of ``execute``; the note says so instead of letting the
#: reader add them into a wrong total.
_PHASE_TABLE: tuple[tuple[str, str], ...] = (
    ("preflight", "plan + lint + compile + preflight"),
    ("create", "devices/modules (within execute)"),
    ("cable", "links/association (within execute)"),
    ("configure", "device config (within execute)"),
    ("flows", "flow probes + DHCP wait (within execute)"),
    ("execute_other", "read-backs, save, other ops (within execute)"),
    ("execute", "wall clock of the whole executor"),
    ("verify", "fresh observation + acceptance"),
    ("repair", "diagnose + repair + re-verify"),
    ("total", "whole build"),
)


def _families_from_op_seconds(op_seconds: dict[str, float]) -> dict[str, float]:
    """Group the executor's per-operation_type seconds into phase families.

    ``op_seconds`` is ``ExecutionResult.op_seconds``, measured with a
    monotonic clock around each dispatch. Receipt timestamps cannot be used
    here: ``OperationReceipt.started_at``/``finished_at`` are both stamped
    when the receipt is constructed, so every receipt reports 0 (archived
    reference run: 123/123 receipts equal). Every family key is always
    present so the report table has a stable shape.
    """
    totals = {family: 0.0 for family in set(_OP_FAMILIES.values())}
    totals.setdefault("execute_other", 0.0)
    for op_type, seconds in op_seconds.items():
        family = _OP_FAMILIES.get(op_type, "execute_other")
        totals[family] = totals.get(family, 0.0) + float(seconds)
    return totals


def _populate_link_observations(
    adapter: PacketTracerAdapter,
    link_endpoints: dict[str, tuple[str, str, str, str]],
    state: Any,  # ActualNetworkState
) -> None:
    """Record adapter-observed link existence into the state's link list so the
    semantic diff judges real links, not the absence of observations."""
    existing = {
        l.ref_id.value for l in state.links if l.ref_id.is_known
    }
    for link_id, (a_dev, a_port, b_dev, b_port) in link_endpoints.items():
        if link_id in existing:
            continue
        observed = adapter.link_exists(a_dev, a_port, b_dev, b_port)
        if observed.is_known and observed.value:
            state.links.append(
                LinkObservation(
                    ref_id=Observable.known(link_id, source=observed.source),
                    endpoint_a=Observable.known(f"{a_dev}:{a_port}"),
                    endpoint_b=Observable.known(f"{b_dev}:{b_port}"),
                )
            )


#: Functional layers, in report order (#23). `structural` device/link
#: existence is deliberately absent: that is the headline this section exists
#: to keep honest.
LAYER_LABELS = {
    "l2": "L2 (VLAN/trunk)",
    "l3": "L3 (routing/DHCP)",
    "security": "Security (ASA)",
    "data_plane": "Data plane (flows)",
}


def _layer_of(test: Any, roles: dict[str, str]) -> str:
    """Which functional layer one acceptance criterion actually exercises."""
    kind = getattr(test, "kind", "")
    if kind == "flow":
        return "data_plane"
    if kind == "wireless":
        return "l2"
    if kind == "config":
        if getattr(test, "field", "") == "vlan":
            return "l2"
        role = roles.get(str(getattr(test, "device", "") or ""), "")
        return "security" if role == "firewall" else "l3"
    check = str(getattr(test, "check", "") or "")
    if check in ("vlan_defined", "trunk_configured"):
        return "l2"
    if check in ("ip_assigned", "gateway_assigned"):
        return "l3"
    # device_exists / link_exists: the structural headline, already reported.
    return ""


def functional_layers(spec: Any, acceptance: Any) -> dict[str, dict[str, int]]:
    """Split the acceptance criteria by the layer they test, with their verdicts.

    `structural 87/87 PASS` only means the devices and cables are there. This
    shows what the build actually proved: a spec with no flow criterion has
    proved no data plane and one with no VLAN criterion has proved no L2,
    whatever the structural total says (#23).
    """
    roles = {
        str(d.id): str(getattr(d.role, "value", d.role)) for d in spec.devices
    }
    verdicts = {r.criterion_id: r.verdict.value for r in acceptance.results}
    buckets: dict[str, list[str]] = {key: [] for key in LAYER_LABELS}
    for criterion in spec.acceptance_criteria:
        layer = _layer_of(criterion.test, roles)
        if layer:
            buckets[layer].append(str(criterion.id))
    out: dict[str, dict[str, int]] = {}
    for key, ids in buckets.items():
        counts = {"total": len(ids), "pass": 0, "fail": 0, "unknown": 0, "not_run": 0}
        for criterion_id in ids:
            verdict = verdicts.get(criterion_id, "not_run")
            counts[verdict] = counts.get(verdict, 0) + 1
        out[key] = counts
    return out


@dataclass
class BuildReport:
    build_id: str
    spec_name: str
    spec_hash: str
    fingerprint: dict[str, str]
    compile_unsupported: list[str]
    compile_notes: list[str]
    execution: dict[str, Any]
    verification: VerificationReport
    diff: dict[str, Any]
    repairs: list[dict[str, Any]]
    reused: list[str]
    project_file: str | None
    evidence_path: str | None
    drift_unknown_count: int = 0
    safety_mode: str = SafetyMode.AUTONOMOUS.value
    layers: dict[str, dict[str, int]] = field(default_factory=dict)
    #: Seconds per phase (S1.5): wall segments (preflight/execute/verify/
    #: repair/total) plus the receipt-derived families inside execute.
    phase_timing: dict[str, float] = field(default_factory=dict)

    @property
    def planned_only(self) -> bool:
        """True when the gate planned every mutation and executed none."""
        return self.safety_mode == SafetyMode.DRY_RUN.value

    @property
    def success(self) -> bool:
        """Success = the build ran clean AND every verification test passed.

        A dry run never succeeds: it changed nothing, so there is no build to
        have succeeded (#28). Reads that happen to agree with the workspace
        say nothing about a run that touched nothing.
        """
        if self.planned_only:
            return False
        summary = self.verification.summary
        return (
            not self.compile_unsupported
            and not self.execution.get("failed")
            and not self.execution.get("blocked")
            and summary.get("pass", 0) == len(self.verification.tests)
            and summary.get("fail", 0) == 0
            and summary.get("unknown", 0) == 0
            and self.diff.get("drift_count", 1) == 0
        )

    def _overall_line(self) -> str:
        if self.planned_only:
            # "NOT SUCCESS" reads as a broken network and "SUCCESS" as a
            # completed build; a dry run is neither.
            return "PLAN OK (DRY_RUN \u2014 nothing was executed)"
        return "SUCCESS" if self.success else "NOT SUCCESS"

    def human_text(self) -> str:
        v = self.verification.summary
        lines = [
            "AI NETWORK AUTOPILOT â€” BUILD REPORT",
            "",
            f"Build:          {self.build_id}",
            f"Spec:           {self.spec_name} ({self.spec_hash[:12]})",
            f"Fingerprint:    {self.fingerprint.get('fingerprint', 'n/a')}",
            "",
            "Verification:",
            f"  structural:   {self._level_summary('structural')}",
            f"  addressing:   {self._level_summary('addressing')}",
            f"  connectivity: {self._level_summary('connectivity')}",
            f"  TOTAL:        {v.get('pass', 0)}/{len(self.verification.tests)} PASS"
            + (f"  ({v.get('fail', 0)} FAIL, {v.get('unknown', 0)} UNKNOWN)" if v.get('fail') or v.get('unknown') else ""),
            "",
            "",
            *self._layer_lines(),
            "",
            f"Acceptance:     {self._acceptance_line()}",
            f"Drift:          {self._drift_line()}",
            f"Repairs:        {self._repairs_line()}",
            f"Reused:         {len(self.reused)} operation(s) (idempotent)",
            f"Project:        {self.project_file or 'not saved'}",
            f"Evidence:       {self.evidence_path or 'n/a'}",
            "",
            f"OVERALL:        {self._overall_line()}",
        ]
        if self.compile_unsupported:
            lines.append(f"Unsupported:    {', '.join(self.compile_unsupported)}")
        lines.extend(self._phase_lines())
        return "\n".join(lines)

    def _phase_lines(self) -> list[str]:
        """The phase-wise time table, printed at the end of the report."""
        if not self.phase_timing:
            return []
        lines = ["", "Phase timing:"]
        for key, note in _PHASE_TABLE:
            if key not in self.phase_timing:
                continue
            lines.append(f"  {key + ':':<16}{self.phase_timing[key]:9.1f}s   {note}")
        return lines

    def _layer_lines(self) -> list[str]:
        """One line per functional layer, with an explicit zero (#23).

        "0 criteria" is the point: it says the layer was not exercised at all,
        which no PASS total can convey.
        """
        if not self.layers:
            return ["Functional layers: not computed"]
        lines = ["Functional layers:"]
        for key, label in LAYER_LABELS.items():
            lines.append(f"  {label + ':':<22}{self._layer_line(key)}")
        return lines

    def _layer_line(self, key: str) -> str:
        counts = self.layers.get(key) or {}
        total = counts.get("total", 0)
        if not total:
            return "0 criteria â€” not exercised"
        passed = counts.get("pass", 0)
        parts = [f"{passed}/{total} PASS"]
        for verdict, label in (("fail", "FAIL"), ("unknown", "UNKNOWN"),
                               ("not_run", "NOT_RUN")):
            if counts.get(verdict):
                parts.append(f"{counts[verdict]} {label}")
        return "  ".join(parts)

    def _level_summary(self, level: str) -> str:
        tests = self.verification.by_level(level)
        passed = sum(1 for t in tests if t.verdict.value == "pass")
        extra = ""
        fails = [t for t in tests if t.verdict.value != "pass"]
        if fails:
            counts: dict[str, int] = {}
            for t in fails:
                counts[t.verdict.value.upper()] = counts.get(t.verdict.value.upper(), 0) + 1
            extra = "  (" + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) + ")"
        return f"{passed}/{len(tests)} PASS{extra}"

    def _acceptance_line(self) -> str:
        a = self.verification.acceptance
        if a.satisfied is True:
            return f"{a.summary.get('pass', 0)}/{len(a.results)} criteria PASS â€” requirements satisfied"
        if a.satisfied is False:
            return f"requirements NOT satisfied ({a.summary})"
        return f"requirements UNKNOWN â€” {a.summary}"

    def _drift_line(self) -> str:
        if self.diff.get("drift_count", 0) == 0 and self.diff.get("unknown_count", 0) == 0:
            return "NONE"
        parts = [f"{self.diff.get('drift_count', 0)} drift"]
        if self.diff.get("unknown_count"):
            parts.append(f"{self.diff['unknown_count']} unknown")
        return ", ".join(parts)

    def _repairs_line(self) -> str:
        if not self.repairs:
            return "0"
        executed = sum(1 for r in self.repairs if r.get("outcome", {}).get("executed"))
        reverified = sum(
            1 for r in self.repairs if r.get("outcome", {}).get("executed") and r.get("re_verified")
        )
        parts = [f"{len(self.repairs)} planned, {executed} executed"]
        if executed:
            parts.append(f"{reverified} re-verified")
        else:
            parts.append("all blocked (no speculative repair)")
        return ", ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "build_id": self.build_id,
            "spec_name": self.spec_name,
            "spec_hash": self.spec_hash,
            "fingerprint": self.fingerprint,
            "compile_unsupported": self.compile_unsupported,
            "compile_notes": self.compile_notes,
            "execution": self.execution,
            "verification": self.verification.to_dict(),
            "diff": self.diff,
            "repairs": self.repairs,
            "reused": self.reused,
            "project_file": self.project_file,
            "evidence_path": self.evidence_path,
            "success": self.success,
            "functional_layers": self.layers,
            "phase_timing": self.phase_timing,
        }


class Autopilot:
    """The closed-loop engine facade."""

    def __init__(
        self,
        adapter: PacketTracerAdapter,
        gate: SafetyGate,
        evidence: EvidenceStore | None = None,
    ):
        self.adapter = adapter
        self.gate = gate
        self.evidence = evidence

    # -- shared pipeline stages ---------------------------------------------

    def _link_endpoints(self, dag: OperationDag) -> dict[str, tuple[str, str, str, str]]:
        endpoints: dict[str, tuple[str, str, str, str]] = {}
        for node in dag.nodes.values():
            op = node.operation
            if op.operation_type == "create_link":
                inputs = op.inputs
                endpoints[inputs["link_id"]] = (
                    inputs["a_device"], inputs["a_port"], inputs["b_device"], inputs["b_port"],
                )
        return endpoints

    def _verify_and_diff(self, spec, plan, dag, state, flow_results, compiled=None):
        link_endpoints = self._link_endpoints(dag)
        _populate_link_observations(self.adapter, link_endpoints, state)
        # The real interface each address assignment was compiled onto.
        # Without this map an assignment with no interface_hint is verified
        # against a nonexistent interface and success becomes unreachable (P1-2).
        assignment_ports = getattr(compiled, "assignment_ports", None) or {}
        # Verification requires OBSERVED model evidence for a structural PASS
        # (P1-4): a device present in state with an unknown model is UNKNOWN,
        # never a pass.
        verification_observations_complete = all(
            state.devices.get(d.id) is not None
            and state.devices[d.id].model.is_known
            for d in spec.devices
        )
        # The DIFF has a different bar: absence is itself an observable fact.
        # When the adapter enumerated the workspace and answered for every spec
        # device (present in devices OR recorded as confirmed-absent in
        # metadata), a missing device is a real REMOVE â€” drift â€” not UNKNOWN.
        # Without this, an empty workspace reported zero drift and hid a
        # completely missing topology.
        diff_observations_complete = all(
            d.id in state.devices
            or f"absent:{d.id}" in state.metadata
            for d in spec.devices
        )
        engine = VerificationEngine(self.adapter)
        report = engine.verify(
            spec, plan, state, flow_results,
            link_endpoints=link_endpoints,
            observations_complete=verification_observations_complete,
            assignment_ports=assignment_ports,
        )
        diff = diff_expected_vs_actual(
            spec, plan, state, observations_complete=diff_observations_complete,
            assignment_ports=assignment_ports,
        )
        return report, diff

    def _collect_flows(self, spec: NetworkSpec) -> dict[tuple[str, str], FlowResult]:
        """Run one data-plane probe per distinct flow criterion.

        A probe sends packets; it never edits the workspace, so it belongs on
        the read-only drift check as well. Before this, `inspect()` passed an
        empty flow_results (#19) and every connectivity criterion came back
        UNKNOWN with "no flow result recorded" however healthy the network
        was - the read-only path could never confirm that traffic forwards.

        A probe that raises is recorded as UNKNOWN, not as a crash: the
        structural and addressing half of the report is still worth returning.
        """
        flows: dict[tuple[str, str], FlowResult] = {}
        for criterion in spec.acceptance_criteria:
            test = criterion.test
            if not isinstance(test, FlowTestSpec):
                continue
            key = (test.source, test.destination)
            if key in flows:
                continue
            try:
                flows[key] = self.adapter.run_flow_test(
                    test.source,
                    test.destination,
                    transport=test.transport,
                    port=test.port,
                    destination_ip=getattr(test, "destination_ip", None),
                )
            except Exception as exc:  # a dead channel must not sink the report
                flows[key] = FlowResult(
                    source=test.source,
                    destination=test.destination,
                    transport=test.transport,
                    port=test.port,
                    outcome=FlowOutcome.UNKNOWN,
                    detail=f"probe could not run: {exc}",
                )
            if self.evidence is not None:
                self.evidence.record(
                    "flow_result",
                    {
                        "criterion_id": criterion.id,
                        "source": key[0],
                        "destination": key[1],
                        "transport": test.transport,
                        "outcome": flows[key].outcome.value,
                        "detail": flows[key].detail,
                    },
                    component="orchestrator",
                )
        return flows

    def inspect(
        self, spec: NetworkSpec
    ) -> tuple[VerificationReport | None, dict | None, str | None]:
        """Observe + verify + diff WITHOUT mutating anything.

        Returns (report, diff, error). This is the drift-check path: "does the
        workspace still match the design?" â€” pure reads through the adapter.
        """
        try:
            plan = plan_addressing(spec)
        except Exception as exc:
            return None, None, f"addressing failed: {exc}"
        issues = validate_plan(plan, spec)
        if has_errors(issues):
            return None, None, "addressing validation failed: " + "; ".join(
                f"{i.resource}: {i.problem}" for i in issues if i.severity == "error"
            )
        self.adapter.ping()
        try:
            compiled = compile_spec(spec, plan, packet_tracer_version="unknown")
        except Exception as exc:
            return None, None, f"compilation refused: {exc}"
        # Probe first, then observe: the state should reflect whatever the
        # probes did to link/ARP state, not the other way round.
        flows = self._collect_flows(spec)
        state = self.adapter.observe_state([d.id for d in spec.devices])
        report, diff = self._verify_and_diff(spec, plan, compiled.dag, state, flows, compiled)
        if self.evidence is not None:
            self.evidence.record("verification", report.to_dict(), component="orchestrator")
            self.evidence.record("diff", diff.to_dict(), component="orchestrator")
        return report, diff, None

    def build(self, spec: NetworkSpec) -> tuple[BuildReport | None, str | None]:
        """Run the full pipeline. Returns (report, error) â€” error blocks the build."""
        evidence = self.evidence
        phases: dict[str, float] = {}
        t_build = time.monotonic()
        if evidence is not None:
            evidence.record(
                "build_request",
                {"spec_json": spec.to_json(), "spec_hash": spec.spec_hash()},
                component="orchestrator",
            )

        # 1. IPAM
        try:
            plan = plan_addressing(spec)
        except PlanningFailure as exc:
            # A named refusal of the spec's addressing, raised before anything
            # runs: same class of stop as preflight (S1.3 exit codes).
            return None, PreflightBlocked(f"addressing failed: {exc}")
        except Exception as exc:
            return None, f"addressing failed: {exc}"
        issues = validate_plan(plan, spec)
        if has_errors(issues):
            if evidence is not None:
                evidence.record("addressing_invalid", {"issues": [i.to_dict() for i in issues]}, component="orchestrator")
            return None, PreflightBlocked(
                "addressing validation failed: "
                + "; ".join(
                    f"{i.resource}: {i.problem}" for i in issues if i.severity == "error"
                )
            )
        if evidence is not None:
            evidence.record("addressing_planned", plan.to_dict(), component="orchestrator")

        # 2. Compile + preflight (preflight is a HARD gate â€” Â§G)
        from .compiler import net_build
        try:
            # adapter forwarded for the S1.4 workspace collision check.
            compiled, compile_error = net_build(spec, plan, adapter=self.adapter)
        except (CompileError, PolicyCompileError) as exc:
            # The compiler refused the spec by name (port preflight, policy,
            # out-of-scope feature) - pre-mutation, so it shares preflight's
            # exit code. Anything else escaping here is a bug: keep it
            # untyped so the CLI reports a plain build error.
            return None, PreflightBlocked(f"compilation refused: {exc}")
        except Exception as exc:
            return None, f"compilation refused: {exc}"
        if compile_error:
            if evidence is not None:
                evidence.record("preflight_failed", {"error": compile_error}, component="orchestrator")
            # Typed (still a str) so the CLI can give preflight its own exit
            # code (S1.3). net_build already prefixes its own preflight
            # message, so do not double it; the lint path arrives bare.
            message = (
                compile_error
                if str(compile_error).startswith("preflight blocked execution")
                else f"preflight blocked execution: {compile_error}"
            )
            return None, PreflightBlocked(message)
        if compiled is None:
            return None, "compilation returned no result"
        if evidence is not None:
            evidence.record(
                "compiled",
                {
                    "fingerprint": compiled.fingerprint,
                    "notes": compiled.notes,
                    "operations": len(compiled.dag.nodes),
                    "preflight_passed": compiled.preflight.passed if compiled.preflight else True,
                },
                component="orchestrator",
            )
        phases["preflight"] = time.monotonic() - t_build

        # 3. Execute through the safety gate
        # Size the budget to the actual plan. A hardcoded ceiling here would
        # abort any non-trivial build (the old fixed 25 killed 12-device labs);
        # for_operations() scales with the DAG while staying globally capped.
        op_count = len(compiled.dag.nodes)
        if self.gate.budget.max_mutations < op_count:
            self.gate.budget.max_mutations = MutationBudget.for_operations(
                op_count
            ).max_mutations
        executor = DagExecutor(self.adapter, self.gate, evidence)
        t_execute = time.monotonic()
        execution = executor.execute(compiled.dag)
        phases["execute"] = time.monotonic() - t_execute
        # Family breakdown inside execute (create/cable/configure/flows vs
        # the rest) from the executor's monotonic clock â€” S1.5.
        phases.update(_families_from_op_seconds(execution.op_seconds))
        if evidence is not None:
            evidence.record("execution_summary", execution.to_dict(), component="orchestrator")

        # 4-5. Verify (against a fresh observation sweep of all spec devices)
        t_verify = time.monotonic()
        fresh = self.adapter.observe_state([d.id for d in spec.devices])
        merged_state = executor.state
        merged_state.merge(fresh)
        report, diff = self._verify_and_diff(
            spec, plan, compiled.dag, merged_state, execution.flow_results, compiled
        )
        phases["verify"] = time.monotonic() - t_verify

        # 7. Diagnose + repair (bounded) when needed. Diagnosis is for
        # FAILURES and drift: an UNKNOWN test means the flow could not be
        # observed, so there is no evidence-backed cause to diagnose â€”
        # attempting repairs on it would be speculation.
        t_repair = time.monotonic()
        repairs: list[dict[str, Any]] = []
        if report.summary.get("fail", 0) > 0 or diff.drift_count:
            from .diagnosis.engine import diagnose

            diagnoses = diagnose(spec, report, merged_state)
            repair_engine = RepairEngine(self.adapter, self.gate, evidence, spec=spec)
            for diagnosis in diagnoses:
                repair_plan = repair_engine.plan_repair(diagnosis, plan)
                outcome = repair_engine.execute(repair_plan)
                # S1.2/F-1: the repair's own probes are the freshest
                # observation of the data plane. Without this merge they die
                # in repair_function_check records and the re-verify reads
                # the stale DAG-era result (flows-unknown.md Â§3: three real
                # PASSes discarded 42s before UNKNOWN was printed).
                for flow in outcome.function_flow_results:
                    execution.flow_results[
                        (flow.source, flow.destination)
                    ] = flow
                repairs.append(
                    {
                        "diagnosis": diagnosis.to_dict(),
                        "plan": repair_plan.to_dict(),
                        "outcome": outcome.to_dict(),
                        "re_verified": None,  # filled below when executed
                    }
                )
            # re-observe + re-verify after any executed repair
            if any(r["outcome"].get("executed") for r in repairs):
                fresh2 = self.adapter.observe_state([d.id for d in spec.devices])
                merged_state.merge(fresh2)
                report, diff = self._verify_and_diff(
                    spec, plan, compiled.dag, merged_state, execution.flow_results, compiled
                )
                for r in repairs:
                    if r["outcome"].get("executed"):
                        # #15: a repair is only re-verified when its function
                        # check passed - an address that reads back while the
                        # firewall still drops traffic is not a repair.
                        r["re_verified"] = (
                            report.to_dict()["summary"]
                            if r["outcome"].get("repaired")
                            else None
                        )

        phases["repair"] = time.monotonic() - t_repair
        phases["total"] = time.monotonic() - t_build
        if evidence is not None:
            evidence.record("verification", report.to_dict(), component="orchestrator")
            evidence.record("diff", diff.to_dict(), component="orchestrator")
            evidence.record("phase_timing", phases, component="orchestrator")

        build = BuildReport(
            build_id=evidence.build_id if evidence else "no-evidence",
            spec_name=spec.name,
            spec_hash=spec.spec_hash(),
            fingerprint=compiled.fingerprint,
            compile_unsupported=compiled.unsupported_features,
            compile_notes=compiled.notes,
            execution=execution.to_dict(),
            verification=report,
            diff=diff.to_dict(),
            repairs=repairs,
            reused=execution.reused,
            project_file=(
                compiled.dag.nodes["op-save"].operation.inputs.get("filename")
                if "op-save" in compiled.dag.nodes
                else None
            ),
            evidence_path=str(evidence.path) if evidence else None,
            safety_mode=getattr(self.gate, "mode", SafetyMode.AUTONOMOUS).value,
            layers=functional_layers(spec, report.acceptance),
            phase_timing=phases,
        )
        build.drift_unknown_count = diff.unknown_count
        if evidence is not None:
            evidence.record("build_result", build.to_dict(), component="orchestrator")
        return build, None
