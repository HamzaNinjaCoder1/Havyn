"""The end-to-end autopilot: spec -> plan -> compile -> execute -> verify.

One pipeline for both the offline contract environment (NullAdapter) and the
real Packet Tracer bridge — only the adapter differs, by design.

The orchestrator records every stage into the evidence store and produces the
BuildReport. It never upgrades a verdict: if verification could not check
something, the report says UNKNOWN, and overall success requires every test to
have actually passed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .adapter.base import PacketTracerAdapter
from .acceptance.evaluator import evaluate_spec
from .compiler.compiler import compile_spec
from .compiler.dag import OperationDag
from .compiler.executor import DagExecutor
from .core.network_spec import NetworkSpec
from .core.observability import Observable
from .core.actual_state import LinkObservation
from .core.safety import SafetyGate
from .diff.engine import diff_expected_vs_actual
from .evidence.store import EvidenceStore
from .ipam.planner import plan_addressing
from .ipam.validator import has_errors, validate_plan
from .repair.engine import RepairEngine
from .verification.engine import VerificationEngine, VerificationReport


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

    @property
    def success(self) -> bool:
        """Success = the build ran clean AND every verification test passed."""
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

    def human_text(self) -> str:
        v = self.verification.summary
        lines = [
            "AI NETWORK AUTOPILOT — BUILD REPORT",
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
            f"Acceptance:     {self._acceptance_line()}",
            f"Drift:          {self._drift_line()}",
            f"Repairs:        {self._repairs_line()}",
            f"Reused:         {len(self.reused)} operation(s) (idempotent)",
            f"Project:        {self.project_file or 'not saved'}",
            f"Evidence:       {self.evidence_path or 'n/a'}",
            "",
            f"OVERALL:        {'SUCCESS' if self.success else 'NOT SUCCESS'}",
        ]
        if self.compile_unsupported:
            lines.append(f"Unsupported:    {', '.join(self.compile_unsupported)}")
        return "\n".join(lines)

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
            return f"{a.summary.get('pass', 0)}/{len(a.results)} criteria PASS — requirements satisfied"
        if a.satisfied is False:
            return f"requirements NOT satisfied ({a.summary})"
        return f"requirements UNKNOWN — {a.summary}"

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

    def _verify_and_diff(self, spec, plan, dag, state, flow_results):
        link_endpoints = self._link_endpoints(dag)
        _populate_link_observations(self.adapter, link_endpoints, state)
        engine = VerificationEngine(self.adapter)
        report = engine.verify(
            spec, plan, state, flow_results,
            link_endpoints=link_endpoints,
            observations_complete=True,  # we observed exactly the spec device set
        )
        diff = diff_expected_vs_actual(spec, plan, state, observations_complete=True)
        return report, diff

    def inspect(
        self, spec: NetworkSpec
    ) -> tuple[VerificationReport | None, dict | None, str | None]:
        """Observe + verify + diff WITHOUT mutating anything.

        Returns (report, diff, error). This is the drift-check path: "does the
        workspace still match the design?" — pure reads through the adapter.
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
        state = self.adapter.observe_state([d.id for d in spec.devices])
        report, diff = self._verify_and_diff(spec, plan, compiled.dag, state, {})
        if self.evidence is not None:
            self.evidence.record("verification", report.to_dict(), component="orchestrator")
            self.evidence.record("diff", diff.to_dict(), component="orchestrator")
        return report, diff, None

    def build(self, spec: NetworkSpec) -> tuple[BuildReport | None, str | None]:
        """Run the full pipeline. Returns (report, error) — error blocks the build."""
        evidence = self.evidence
        if evidence is not None:
            evidence.record(
                "build_request",
                {"spec_json": spec.to_json(), "spec_hash": spec.spec_hash()},
                component="orchestrator",
            )

        # 1. IPAM
        try:
            plan = plan_addressing(spec)
        except Exception as exc:
            return None, f"addressing failed: {exc}"
        issues = validate_plan(plan, spec)
        if has_errors(issues):
            if evidence is not None:
                evidence.record("addressing_invalid", {"issues": [i.to_dict() for i in issues]}, component="orchestrator")
            return None, "addressing validation failed: " + "; ".join(
                f"{i.resource}: {i.problem}" for i in issues if i.severity == "error"
            )
        if evidence is not None:
            evidence.record("addressing_planned", plan.to_dict(), component="orchestrator")

        # 2. Compile
        info = self.adapter.ping()
        try:
            compiled = compile_spec(spec, plan, packet_tracer_version="unknown")
        except Exception as exc:
            return None, f"compilation refused: {exc}"
        if evidence is not None:
            evidence.record(
                "compiled",
                {
                    "fingerprint": compiled.fingerprint,
                    "notes": compiled.notes,
                    "operations": len(compiled.dag.nodes),
                },
                component="orchestrator",
            )

        # 3. Execute through the safety gate
        executor = DagExecutor(self.adapter, self.gate, evidence)
        execution = executor.execute(compiled.dag)
        if evidence is not None:
            evidence.record("execution_summary", execution.to_dict(), component="orchestrator")

        # 4-5. Verify (against a fresh observation sweep of all spec devices)
        fresh = self.adapter.observe_state([d.id for d in spec.devices])
        merged_state = executor.state
        merged_state.merge(fresh)
        report, diff = self._verify_and_diff(
            spec, plan, compiled.dag, merged_state, execution.flow_results
        )

        # 7. Diagnose + repair (bounded) when needed. Diagnosis is for
        # FAILURES and drift: an UNKNOWN test means the flow could not be
        # observed, so there is no evidence-backed cause to diagnose —
        # attempting repairs on it would be speculation.
        repairs: list[dict[str, Any]] = []
        if report.summary.get("fail", 0) > 0 or diff.drift_count:
            from .diagnosis.engine import diagnose

            diagnoses = diagnose(spec, report, merged_state)
            repair_engine = RepairEngine(self.adapter, self.gate, evidence)
            for diagnosis in diagnoses:
                repair_plan = repair_engine.plan_repair(diagnosis, plan)
                outcome = repair_engine.execute(repair_plan)
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
                    spec, plan, compiled.dag, merged_state, execution.flow_results
                )
                for r in repairs:
                    if r["outcome"].get("executed"):
                        r["re_verified"] = report.to_dict()["summary"]

        if evidence is not None:
            evidence.record("verification", report.to_dict(), component="orchestrator")
            evidence.record("diff", diff.to_dict(), component="orchestrator")

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
        )
        build.drift_unknown_count = diff.unknown_count
        if evidence is not None:
            evidence.record("build_result", build.to_dict(), component="orchestrator")
        return build, None
