"""Stage 4 backup and repair tests (S4.9/S4.10)."""

from __future__ import annotations

import json
from pathlib import Path

from network_autopilot.brownfield.backup import BackupManager
from network_autopilot.brownfield.faults import FaultDetector
from network_autopilot.brownfield.importer import BrownfieldImporter
from network_autopilot.brownfield.intent import AsIsIntentBuilder
from network_autopilot.brownfield.repair import (
    BrownfieldRepairEngine,
    RepairPlan,
)
from network_autopilot.brownfield.simulated import SimulatedAdapter
from network_autopilot.core.receipts import OperationResult
from network_autopilot.core.safety import MutationBudget, SafetyGate, SafetyMode

from tests.autopilot import s4_helpers as h


def _setup_fault():
    spec = h.load_spec("T02-two-vlan-office.json")
    adapter, imported = h.import_simulated(spec)
    expected = h.expected_for(imported)
    objectives = h.objectives_for(spec)
    # Shut the switch port towards pc3 (VLAN 20); pc1 -> pc2 still works.
    switch = adapter.network.devices["s1"]
    port = next(
        i for i in switch.interfaces.values()
        if i.linked and i.access_vlan == 20
    )
    port.shutdown = True
    fresh = BrownfieldImporter(adapter).import_network()
    detector = FaultDetector(
        fresh.inventory, fresh.corpus, objectives, expected=expected
    )
    objective = next(
        o for o in objectives.confirmed()
        if o.params.get("destination") == "pc3"
    )
    diagnostics = detector.diagnose(objective)
    diagnosis = next(d for d in diagnostics if d.cause_class == "port-shutdown")
    return spec, adapter, objectives, objective, diagnosis, port


def _engine(adapter, objectives, tmp_path: Path) -> BrownfieldRepairEngine:
    gate = SafetyGate(
        SafetyMode.AUTONOMOUS,
        MutationBudget(max_mutations=100, max_repair_mutations=10),
    )
    return BrownfieldRepairEngine(
        adapter,
        gate,
        objectives=objectives,
        backup=BackupManager(adapter, root=tmp_path),
    )


def test_mandatory_backup_creates_project_config_and_manifest(tmp_path: Path) -> None:
    spec = h.load_spec("T02-two-vlan-office.json")
    adapter = SimulatedAdapter(spec)
    manager = BackupManager(adapter, root=tmp_path)
    manifest = manager.create(
        "lab", devices=["s1"], intended_changes=[{"device": "s1"}]
    )
    assert manifest.available
    assert Path(manifest.project_path or "").exists()
    entry = manifest.entry_for("s1")
    assert entry is not None and entry.path and Path(entry.path).exists()
    validation = manager.validate(manifest)
    assert validation.available, validation.problems
    # Tampering is detected.
    Path(manifest.project_path).write_text("corrupt", encoding="utf-8")
    assert not manager.validate(manifest).available


def test_correct_repair_succeeds_and_preserves_other_flow(tmp_path: Path) -> None:
    spec, adapter, objectives, objective, diagnosis, port = _setup_fault()
    engine = _engine(adapter, objectives, tmp_path)
    plan = engine.plan([diagnosis], objective=objective)
    assert not plan.blocked
    outcome = engine.execute(plan, objective=objective, approved=True)
    assert outcome.executed
    assert outcome.objective_verdict == "pass"
    assert outcome.repaired
    assert outcome.backup is not None and outcome.backup_validation.available
    assert outcome.rollback_complete is False  # no rollback was needed
    # The other confirmed flow (pc1 -> pc2) passed before and still passes.
    key = next(k for k in outcome.baseline if "pc2" in k)
    assert outcome.baseline[key] == "pass"
    assert outcome.post_repair[key] == "pass"
    assert not adapter.network.devices["s1"].interfaces[port.name].shutdown


def test_unsuccessful_repair_is_rolled_back_and_verified(tmp_path: Path) -> None:
    spec, adapter, objectives, objective, diagnosis, port = _setup_fault()
    engine = _engine(adapter, objectives, tmp_path)
    switch = adapter.network.devices["s1"]
    other = next(
        i for i in switch.interfaces.values()
        if i.linked and i is not port and i.access_vlan is not None
    )
    wrong_fix = __import__(
        "network_autopilot.brownfield.models", fromlist=["FixAction"]
    ).FixAction(
        device="s1",
        commands=(
            f"interface {other.name}",
            "switchport access vlan 20",
            "exit",
        ),
        reason="deliberately wrong repair for the rollback test",
        rollback_commands=(
            f"interface {other.name}",
            "switchport access vlan 10",
            "exit",
        ),
    )
    plan = RepairPlan(
        repair_id="test-wrong-repair",
        diagnosis_ids=(diagnosis.diagnosis_id,),
        objective_ids=(objective.id,),
        actions=(wrong_fix,),
        affected_devices=("s1",),
        expected_improvement="none (deliberately wrong)",
        verification=(objective.statement,),
    )
    outcome = engine.execute(plan, objective=objective, approved=True)
    assert outcome.executed
    assert outcome.rolled_back
    assert outcome.rollback_complete, outcome.rollback_detail
    assert not outcome.incorrect_fix_left_in_place
    assert not outcome.repaired
    # The original fault is still present (the rollback restores pre-repair
    # state, it does not silently "fix" the real fault).
    assert switch.interfaces[port.name].shutdown is True


def test_missing_approval_prevents_any_modification(tmp_path: Path) -> None:
    spec, adapter, objectives, objective, diagnosis, port = _setup_fault()
    gate = SafetyGate(
        SafetyMode.APPROVAL_REQUIRED,
        MutationBudget(max_mutations=100),
        approver=None,
    )
    engine = BrownfieldRepairEngine(
        adapter, gate, objectives=objectives,
        backup=BackupManager(adapter, root=tmp_path),
    )
    plan = engine.plan([diagnosis], objective=objective)
    outcome = engine.execute(plan, objective=objective, approved=False)
    assert not outcome.executed
    assert "approval" in (outcome.blocked_reason or "").lower()
    assert outcome.backup is None
    assert adapter.network.devices["s1"].interfaces[port.name].shutdown is True


def test_backup_failure_prevents_modification(tmp_path: Path) -> None:
    class NoSaveAdapter(SimulatedAdapter):
        def save_project(self, filename, directory="", operation_id=None):
            return self._receipt(
                "save_project", {"filename": filename},
                OperationResult.REJECTED, "disk refused",
            )

    spec = h.load_spec("T02-two-vlan-office.json")
    base = SimulatedAdapter(spec)
    adapter = NoSaveAdapter(spec)
    adapter.network = base.network
    objectives = h.objectives_for(spec)
    expected = h.expected_for(
        BrownfieldImporter(base).import_network()
    )
    switch = adapter.network.devices["s1"]
    port = next(
        i for i in switch.interfaces.values()
        if i.linked and i.access_vlan == 20
    )
    port.shutdown = True
    fresh = BrownfieldImporter(adapter).import_network()
    detector = FaultDetector(
        fresh.inventory, fresh.corpus, objectives, expected=expected
    )
    objective = next(
        o for o in objectives.confirmed()
        if o.params.get("destination") == "pc3"
    )
    diagnosis = next(
        d for d in detector.diagnose(objective)
        if d.cause_class == "port-shutdown"
    )
    engine = _engine(adapter, objectives, tmp_path)
    plan = engine.plan([diagnosis], objective=objective)
    outcome = engine.execute(plan, objective=objective, approved=True)
    assert not outcome.executed
    assert "backup" in (outcome.blocked_reason or "").lower()
    assert port.shutdown is True
