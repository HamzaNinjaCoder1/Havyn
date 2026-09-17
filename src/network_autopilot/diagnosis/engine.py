"""Evidence-based diagnosis: failed verification -> candidate root causes.

Rules:
- every candidate root cause is backed by named evidence;
- confidence reflects how much evidence supports the cause — a single weak
  signal is LOW, corroborated signals are HIGH;
- when evidence is insufficient, the diagnosis says so and proposes NO repair
  (speculative mutation is how labs get destroyed);
- the engine never mutates anything: it only produces diagnoses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.actual_state import ActualNetworkState
from ..core.ids import new_id
from ..core.network_spec import NetworkSpec
from ..core.requirements import Verdict
from ..verification.engine import VerificationReport

_EMPTY_READ = "runtime returned an empty value"


@dataclass(frozen=True)
class Diagnosis:
    diagnosis_id: str
    failed_tests: tuple[str, ...]
    root_cause: str
    evidence: tuple[str, ...]
    confidence: str                  # high | medium | low | unknown
    affected_resources: tuple[str, ...]
    proposed_repair: tuple[dict[str, Any], ...]  # operation dicts; empty = no repair
    uncertainty: str | None

    @property
    def repairable(self) -> bool:
        return bool(self.proposed_repair) and self.confidence in ("high", "medium")

    def to_dict(self) -> dict[str, Any]:
        return {
            "diagnosis_id": self.diagnosis_id,
            "failed_tests": list(self.failed_tests),
            "root_cause": self.root_cause,
            "evidence": list(self.evidence),
            "confidence": self.confidence,
            "affected_resources": list(self.affected_resources),
            "proposed_repair": list(self.proposed_repair),
            "uncertainty": self.uncertainty,
            "repairable": self.repairable,
        }


def diagnose(
    spec: NetworkSpec,
    report: VerificationReport,
    state: ActualNetworkState,
) -> tuple[Diagnosis, ...]:
    """Diagnose every failing/unknown area the evidence can explain."""
    diagnoses: list[Diagnosis] = []

    # 1. missing devices (structural failures with complete observation)
    for test in report.tests:
        if test.level == "structural" and test.target.startswith(("device:",)) is False and test.verdict is Verdict.FAIL:
            if test.test_id.startswith("VT-struct-device-"):
                device_id = test.target
                diagnoses.append(
                    Diagnosis(
                        diagnosis_id=new_id("diag"),
                        failed_tests=(test.test_id,),
                        root_cause=f"device {device_id!r} is absent from the workspace",
                        evidence=(str(test.observed_result), *test.evidence),
                        confidence="high",
                        affected_resources=(device_id,),
                        proposed_repair=(
                            {
                                "operation_type": "create_device",
                                "inputs": _device_inputs(spec, device_id),
                                "reason": "expected device missing",
                            },
                        ),
                        uncertainty=None,
                    )
                )
            elif test.test_id.startswith("VT-struct-link-"):
                diagnoses.append(
                    Diagnosis(
                        diagnosis_id=new_id("diag"),
                        failed_tests=(test.test_id,),
                        root_cause=f"link {test.target!r} is not present on its ports",
                        evidence=(str(test.observed_result), *test.evidence),
                        confidence="high",
                        affected_resources=(test.target,),
                        proposed_repair=(
                            {
                                "operation_type": "create_link",
                                "inputs": _link_inputs(spec, test.target),
                                "reason": "expected link missing",
                            },
                        ),
                        uncertainty=None,
                    )
                )

    # 2. addressing failures with observed evidence
    for test in report.tests:
        if test.level != "addressing" or test.verdict is not Verdict.FAIL:
            continue
        device_id, _, iface = test.target.partition(":")
        evidence = [str(test.observed_result), *test.evidence]
        if test.observed_result == "no ip configured":
            root_cause = f"{device_id} {iface} has no IP configured"
            confidence = "high"
            uncertainty = None
        elif test.observed_result and "/" in str(test.observed_result):
            root_cause = f"{device_id} {iface} has wrong addressing"
            confidence = "high"
            uncertainty = None
        else:
            root_cause = f"{device_id} {iface} addressing is wrong"
            confidence = "medium"
            uncertainty = "observed value could not be fully read back"
        diagnoses.append(
            Diagnosis(
                diagnosis_id=new_id("diag"),
                failed_tests=(test.test_id,),
                root_cause=root_cause,
                evidence=tuple(evidence),
                confidence=confidence,
                affected_resources=(device_id,),
                proposed_repair=(
                    {
                        "operation_type": "reconfigure_address",
                        "inputs": _address_inputs(spec, device_id, iface),
                        "reason": "correct the observed addressing",
                    },
                ),
                uncertainty=uncertainty,
            )
        )

    # 3. connectivity failures: correlate flow evidence with state
    for test in report.tests:
        if test.level != "connectivity" or test.verdict is Verdict.PASS:
            continue
        candidates: list[Diagnosis] = []
        source, target = test.source or "?", test.target
        evidence: list[str] = [f"flow {source}->{target} observed: {test.observed_result}"]
        # endpoint absence is a direct, evidence-backed cause
        for endpoint in (source, target):
            device = state.devices.get(endpoint)
            if device is None:
                candidates.append(
                    Diagnosis(
                        diagnosis_id=new_id("diag"),
                        failed_tests=(test.test_id,),
                        root_cause=f"endpoint {endpoint!r} is absent from the observed workspace",
                        evidence=(*evidence, f"{endpoint} not in observed state"),
                        confidence="medium" if not report.observations_complete else "high",
                        affected_resources=(endpoint,),
                        proposed_repair=(),
                        uncertainty=(
                            "endpoint absence inferred from a possibly-partial observation"
                            if not report.observations_complete
                            else None
                        ),
                    )
                )
            else:
                down = [
                    f"{endpoint}:{p.name}"
                    for p in device.ports.values()
                    if p.state.is_known and p.state.value == "down"
                ]
                if down:
                    candidates.append(
                        Diagnosis(
                            diagnosis_id=new_id("diag"),
                            failed_tests=(test.test_id,),
                            root_cause=f"interface(s) down on the path: {', '.join(down)}",
                            evidence=(*evidence, f"observed state=down on {', '.join(down)}"),
                            confidence="high",
                            affected_resources=(endpoint, *down),
                            proposed_repair=(
                                {
                                    "operation_type": "ios_command",
                                    "inputs": {
                                        "device": endpoint,
                                        "commands": [
                                            f"interface {p.split(':')[1]}",
                                            "no shutdown",
                                        ],
                                    },
                                    "reason": "interface administratively/physically down",
                                },
                            ),
                            uncertainty=None,
                        )
                    )
        if candidates:
            diagnoses.extend(candidates)
        else:
            # no state signal correlates with the failure: say so, propose nothing
            diagnoses.append(
                Diagnosis(
                    diagnosis_id=new_id("diag"),
                    failed_tests=(test.test_id,),
                    root_cause="connectivity failed but no state anomaly was observed",
                    evidence=(*evidence, *test.evidence),
                    confidence="unknown",
                    affected_resources=(source, target),
                    proposed_repair=(),
                    uncertainty=(
                        "No correlating evidence (devices present, interfaces up, "
                        "addressing as expected). Possible causes outside observed "
                        "state: routing, cable media, service config. REPAIR BLOCKED: "
                        "no evidence-backed root cause; do not mutate speculatively."
                    ),
                )
            )

    return diagnoses


def _device_inputs(spec: NetworkSpec, device_id: str) -> dict[str, Any]:
    device = spec.device_by_id(device_id)
    role = device.role.value if device else "pc"
    model = device.model_hint if device else None
    return {"name": device_id, "role": role, "model": model or None}


def _link_inputs(spec: NetworkSpec, link_id: str) -> dict[str, Any]:
    link = next((l for l in spec.links if l.id == link_id), None)
    if link is None:
        return {"link_id": link_id}
    return {
        "link_id": link_id,
        "a_device": link.a.device,
        "a_port": link.a.interface_hint,
        "b_device": link.b.device,
        "b_port": link.b.interface_hint,
        "media": link.media,
    }


def _address_inputs(spec: NetworkSpec, device_id: str, iface: str) -> dict[str, Any]:
    # Inputs are completed by the repair engine, which has the AddressPlan.
    return {"device": device_id, "interface": iface, "needs_plan": True}
