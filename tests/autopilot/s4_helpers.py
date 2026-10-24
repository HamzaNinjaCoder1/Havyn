"""Shared fixtures for Stage 4 tests (no live Packet Tracer required)."""

from __future__ import annotations

from pathlib import Path

from network_autopilot.brownfield.faults import ExpectedNetwork, FaultDetector
from network_autopilot.brownfield.importer import BrownfieldImporter, ImportOptions
from network_autopilot.brownfield.intent import AsIsIntentBuilder
from network_autopilot.brownfield.objectives import ObjectiveRegistry
from network_autopilot.brownfield.simulated import SimulatedAdapter
from network_autopilot.core.network_spec import NetworkSpec

LIBRARY = Path(__file__).resolve().parents[2] / "library"


def load_spec(name: str) -> NetworkSpec:
    return NetworkSpec.from_json((LIBRARY / name).read_text(encoding="utf-8"))


def import_simulated(spec: NetworkSpec):
    adapter = SimulatedAdapter(spec)
    imported = BrownfieldImporter(
        adapter, options=ImportOptions(read_stp=True)
    ).import_network()
    return adapter, imported


def expected_for(imported) -> ExpectedNetwork:
    intent = AsIsIntentBuilder().build(imported)
    return ExpectedNetwork.from_spec(intent.spec)


def objectives_for(spec: NetworkSpec) -> ObjectiveRegistry:
    registry = ObjectiveRegistry()
    registry.add_explicit_from_spec(spec)
    return registry


def detector_for(adapter, imported, spec, expected=None) -> tuple[FaultDetector, ObjectiveRegistry]:
    objectives = objectives_for(spec)
    detector = FaultDetector(
        imported.inventory,
        imported.corpus,
        objectives,
        expected=expected or expected_for(imported),
    )
    return detector, objectives


def first_flow(spec: NetworkSpec):
    for criterion in spec.acceptance_criteria:
        if criterion.test.kind == "flow":
            return criterion.test
    return None
