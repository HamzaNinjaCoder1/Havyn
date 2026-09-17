"""Safety modes: the gate is the only way through."""

from __future__ import annotations

import pytest

from src.network_autopilot.core.receipts import OperationResult
from src.network_autopilot.core.safety import (
    BudgetExceeded,
    MutationBudget,
    SafetyGate,
    SafetyMode,
    SafetyRejection,
)

OP = {"operation": "create_device", "name": "R1", "role": "router"}


def test_read_only_blocks_mutations():
    gate = SafetyGate(SafetyMode.READ_ONLY)
    with pytest.raises(SafetyRejection):
        gate.authorize([OP])


def test_dry_run_plans_without_executing():
    gate = SafetyGate(SafetyMode.DRY_RUN)
    permission = gate.authorize([OP, dict(OP, name="R2")])
    assert not permission.execute
    assert all(r.result is OperationResult.NOT_EXECUTED for r in permission.planned_receipts)
    assert permission.planned_receipts[0].operation == "create_device"


def test_approval_required_blocks_without_approver():
    gate = SafetyGate(SafetyMode.APPROVAL_REQUIRED)
    with pytest.raises(SafetyRejection):
        gate.authorize([OP])


def test_approval_required_passes_with_consent_and_shows_blast_radius():
    seen: dict = {}

    def approver(preview):
        seen.update(preview.to_dict())
        return True

    gate = SafetyGate(SafetyMode.APPROVAL_REQUIRED, approver=approver)
    ops = [
        OP,
        {"operation": "create_link", "a_device": "R1", "a_port": "Gi0/0", "b_device": "SW1", "b_port": "Fa0/1"},
    ]
    permission = gate.authorize(ops)
    assert permission.execute
    assert seen["mutation_count"] == 2
    assert seen["blast_radius"]["devices"] == 2  # distinct: R1, SW1
    assert seen["blast_radius"]["links"] == 2    # a_port + b_port entries


def test_denial_blocks():
    gate = SafetyGate(SafetyMode.APPROVAL_REQUIRED, approver=lambda preview: False)
    with pytest.raises(SafetyRejection):
        gate.authorize([OP])


def test_autonomous_enforces_mutation_budget():
    gate = SafetyGate(SafetyMode.AUTONOMOUS, budget=MutationBudget(max_mutations=2))
    gate.authorize([OP])
    gate.authorize([OP])
    with pytest.raises(BudgetExceeded):
        gate.authorize([OP])


def test_empty_batch_is_always_fine():
    for mode in SafetyMode:
        assert SafetyGate(mode).authorize([]).execute
