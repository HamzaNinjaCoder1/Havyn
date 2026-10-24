"""The standardized Stage 4 diagnosis report (S4.8).

One schema for every diagnosis, whatever layer produced it. The fields the
brief requires are all here — root cause, device, interface, VLAN/subnet,
evidence, confidence, recommended fix, impact and verification requirements —
and the schema is validated, so a report cannot omit the evidence behind a
claim or present a guess as a diagnosis.

Two properties are structural, not stylistic:

* a diagnostic with no evidence is `UNKNOWN - ADDITIONAL VERIFICATION
  REQUIRED`, never `DIAGNOSED`;
* an actionable (repairable) diagnostic must reference a confirmed objective;
  the validator flags any report where that is not true.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from .models import CAUSE_CLASSES, CONFIDENCE_RANK, Diagnostic, Objective


@dataclass(frozen=True)
class ReportIssue:
    code: str
    message: str
    diagnosis_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "diagnosis_id": self.diagnosis_id,
        }


@dataclass
class DiagnosisReport:
    network: str
    generated_at: str
    diagnostics: list[Diagnostic] = field(default_factory=list)
    objectives: list[Objective] = field(default_factory=list)
    observations: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        network: str,
        diagnostics: Iterable[Diagnostic],
        *,
        objectives: Iterable[Objective] = (),
        observations: dict[str, Any] | None = None,
    ) -> "DiagnosisReport":
        return cls(
            network=network,
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            diagnostics=list(diagnostics),
            objectives=list(objectives),
            observations=dict(observations or {}),
        )

    # -- invariants ---------------------------------------------------------

    def validate(self) -> list[ReportIssue]:
        issues: list[ReportIssue] = []
        seen: set[str] = set()
        for diagnostic in self.diagnostics:
            if diagnostic.diagnosis_id in seen:
                issues.append(
                    ReportIssue(
                        "duplicate-diagnosis-id",
                        "the same diagnosis id appears twice",
                        diagnostic.diagnosis_id,
                    )
                )
            seen.add(diagnostic.diagnosis_id)
            if diagnostic.cause_class not in CAUSE_CLASSES:
                issues.append(
                    ReportIssue(
                        "unknown-cause-class",
                        f"cause class {diagnostic.cause_class!r} is not in the "
                        "shared vocabulary",
                        diagnostic.diagnosis_id,
                    )
                )
            if diagnostic.confidence not in CONFIDENCE_RANK:
                issues.append(
                    ReportIssue(
                        "invalid-confidence",
                        f"confidence {diagnostic.confidence!r} is not one of "
                        f"{sorted(CONFIDENCE_RANK)}",
                        diagnostic.diagnosis_id,
                    )
                )
            if diagnostic.status == "DIAGNOSED" and not diagnostic.evidence:
                issues.append(
                    ReportIssue(
                        "diagnosis-without-evidence",
                        "a DIAGNOSED record has no evidence",
                        diagnostic.diagnosis_id,
                    )
                )
            if diagnostic.repairable and not diagnostic.objective_confirmed:
                issues.append(
                    ReportIssue(
                        "repair-without-confirmed-objective",
                        "an actionable repair does not reference a confirmed "
                        "objective",
                        diagnostic.diagnosis_id,
                    )
                )
            if not diagnostic.impact:
                issues.append(
                    ReportIssue(
                        "missing-impact",
                        "diagnosis does not state the impact",
                        diagnostic.diagnosis_id,
                    )
                )
            if not diagnostic.verification_requirements:
                issues.append(
                    ReportIssue(
                        "missing-verification-requirements",
                        "diagnosis does not state how the fix will be verified",
                        diagnostic.diagnosis_id,
                    )
                )
        return issues

    def issues_for(self, diagnosis_id: str) -> list[ReportIssue]:
        return [i for i in self.validate() if i.diagnosis_id == diagnosis_id]

    # -- roll-ups -----------------------------------------------------------

    @property
    def diagnosed(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.status == "DIAGNOSED"]

    @property
    def unknown(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.status != "DIAGNOSED"]

    @property
    def root_causes(self) -> list[Diagnostic]:
        """One record per (cause, device, interface): multiple faults stay."""
        return self.diagnostics

    def summary(self) -> dict[str, Any]:
        by_layer: dict[str, int] = {}
        by_cause: dict[str, int] = {}
        by_confidence: dict[str, int] = {}
        for diagnostic in self.diagnostics:
            by_layer[diagnostic.layer] = by_layer.get(diagnostic.layer, 0) + 1
            by_cause[diagnostic.cause_class] = by_cause.get(diagnostic.cause_class, 0) + 1
            by_confidence[diagnostic.confidence] = (
                by_confidence.get(diagnostic.confidence, 0) + 1
            )
        return {
            "total": len(self.diagnostics),
            "diagnosed": len(self.diagnosed),
            "unknown": len(self.unknown),
            "repairable": sum(1 for d in self.diagnostics if d.repairable),
            "actionable": sum(
                1
                for d in self.diagnostics
                if d.repairable and d.objective_confirmed
            ),
            "by_layer": dict(sorted(by_layer.items())),
            "by_cause": dict(sorted(by_cause.items())),
            "by_confidence": dict(sorted(by_confidence.items())),
            "schema_issues": len(self.validate()),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "network": self.network,
            "generated_at": self.generated_at,
            "summary": self.summary(),
            "diagnostics": [d.to_dict() for d in self.diagnostics],
            "objectives": [o.to_dict() for o in self.objectives],
            "observations": dict(self.observations),
            "issues": [i.to_dict() for i in self.validate()],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    def to_markdown(self) -> str:
        summary = self.summary()
        lines = [
            f"# Brownfield diagnosis — {self.network}",
            "",
            f"_generated {self.generated_at}_",
            "",
            f"Diagnoses **{summary['total']}** · diagnosed **{summary['diagnosed']}** · "
            f"unknown **{summary['unknown']}** · repairable **{summary['repairable']}** · "
            f"schema issues **{summary['schema_issues']}**",
            "",
        ]
        if not self.diagnostics:
            lines.extend(
                [
                    "No fault was proven from the imported evidence.",
                    "",
                    "This is a statement about evidence, not about the network: "
                    "an unobserved path is not a healthy path.",
                ]
            )
            return "\n".join(lines)
        lines.extend(
            [
                "| # | layer | cause | device | port | vlan | confidence | status |",
                "| --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for index, diagnostic in enumerate(self.diagnostics, start=1):
            lines.append(
                f"| {index} | {diagnostic.layer} | `{diagnostic.cause_class}` | "
                f"{diagnostic.affected_device or '-'} | "
                f"{diagnostic.affected_interface or '-'} | "
                f"{diagnostic.vlan if diagnostic.vlan is not None else '-'} | "
                f"{diagnostic.confidence} | {diagnostic.status} |"
            )
        lines.append("")
        for index, diagnostic in enumerate(self.diagnostics, start=1):
            lines.extend(
                [
                    f"## {index}. {diagnostic.root_cause}",
                    "",
                    f"- **Cause class:** `{diagnostic.cause_class}`",
                    f"- **Layer:** {diagnostic.layer}",
                    f"- **Device:** {diagnostic.affected_device or 'unknown'}",
                    f"- **Interface:** {diagnostic.affected_interface or 'n/a'}",
                    f"- **VLAN / subnet:** "
                    f"{diagnostic.vlan if diagnostic.vlan is not None else 'n/a'}"
                    f" / {diagnostic.subnet or 'n/a'}",
                    f"- **Confidence:** {diagnostic.confidence}",
                    f"- **Status:** {diagnostic.status}",
                    f"- **Objective:** "
                    + (
                        f"`{diagnostic.objective_id}`"
                        + (" (confirmed)" if diagnostic.objective_confirmed else " (not confirmed)")
                        if diagnostic.objective_id else "none"
                    ),
                    f"- **Impact:** {diagnostic.impact}",
                    "",
                    "**Evidence**",
                    "",
                ]
            )
            for item in diagnostic.evidence:
                lines.append(f"- {item}")
            lines.append("")
            if diagnostic.recommended_fix:
                lines.extend(["**Recommended minimal fix**", ""])
                for fix in diagnostic.recommended_fix:
                    lines.append(
                        f"- `{fix.device}` ({fix.operation}): "
                        + ("; ".join(f"`{c}`" for c in fix.commands)
                           if fix.commands else "host configuration update")
                    )
                    if fix.rollback_commands:
                        lines.append(
                            "  - rollback: "
                            + "; ".join(f"`{c}`" for c in fix.rollback_commands)
                        )
                lines.append("")
            else:
                lines.extend(
                    [
                        "**Recommended minimal fix**",
                        "",
                        "- none: " + (diagnostic.uncertainty or "no safe fix is proven"),
                        "",
                    ]
                )
            lines.extend(
                [
                    "**Verification requirements**",
                    "",
                    *[f"- {item}" for item in diagnostic.verification_requirements],
                    "",
                ]
            )
        issues = self.validate()
        if issues:
            lines.extend(["## Schema issues", ""])
            lines.extend(f"- `{i.code}` ({i.diagnosis_id}): {i.message}" for i in issues)
            lines.append("")
        return "\n".join(lines)
