"""Safety modes and mutation budgets.

Four modes, enforced at one gate that every mutating path must pass:

- READ_ONLY: mutations are rejected outright.
- DRY_RUN: mutations are planned (receipts with NOT_EXECUTED), nothing executes.
- APPROVAL_REQUIRED: a preview is produced and an approver must accept it.
- AUTONOMOUS: executes within the mutation budget; repairs additionally obey
  the repair budget (max attempts, max mutations per repair).

Budgets are counted at the gate, so they cannot be bypassed by an operation
that "forgets" to check.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import Enum

from .receipts import OperationReceipt, OperationResult


class SafetyMode(str, Enum):
    READ_ONLY = "read_only"
    DRY_RUN = "dry_run"
    APPROVAL_REQUIRED = "approval_required"
    AUTONOMOUS = "autonomous"


class SafetyRejection(RuntimeError):
    """A mutation was not allowed under the active safety mode."""


class BudgetExceeded(RuntimeError):
    """The mutation (or repair) budget ran out."""


@dataclass
class MutationBudget:
    """How many mutations a run may make.

    The default is deliberately generous-but-finite; callers that know the DAG
    size must use `for_operations()` so the budget scales with the work and is
    never an arbitrary ceiling that aborts a legitimate build.
    """
    max_mutations: int = 25
    max_repair_attempts: int = 3
    max_repair_mutations: int = 5
    consumed: int = 0

    # A global ceiling so a runaway or adversarial spec cannot mutate forever,
    # no matter how big the DAG is. Well above any realistic lab.
    GLOBAL_CEILING: int = 4000

    @classmethod
    def for_operations(cls, op_count: int, *, base: int = 40, headroom: int = 3) -> "MutationBudget":
        """Budget sized by the actual compiled plan.

        base + (ops * headroom): enough for the build itself, for bounded
        repair, and for re-execution after a re-verify, but still capped by
        GLOBAL_CEILING. Replaces the hardcoded 25 that aborted 12-device labs.
        """
        needed = base + max(0, op_count) * headroom
        return cls(max_mutations=min(needed, cls.GLOBAL_CEILING))

    def can_consume(self, count: int = 1) -> bool:
        return self.consumed + count <= self.max_mutations

    def consume(self, count: int = 1) -> None:
        if not self.can_consume(count):
            raise BudgetExceeded(
                f"mutation budget exhausted: {self.consumed}/{self.max_mutations} used, "
                f"{count} more requested"
            )
        self.consumed += count

    def refund(self, count: int = 1) -> None:
        """Return budget for an operation that did not mutate after all.

        Authorization is optimistic: budget is reserved before execution, so a
        rejected or skipped op must give it back or a large build slowly
        starves on reuse.
        """
        self.consumed = max(0, self.consumed - max(0, count))


@dataclass(frozen=True)
class MutationPreview:
    """What an approver sees before APPROVAL_REQUIRED executes anything."""

    operations: tuple[dict, ...]
    mutation_count: int
    affected_devices: tuple[str, ...]
    affected_links: tuple[str, ...]

    @property
    def blast_radius(self) -> dict[str, int]:
        return {
            "operations": self.mutation_count,
            "devices": len(self.affected_devices),
            "links": len(self.affected_links),
        }

    def to_dict(self) -> dict:
        return {
            "operations": list(self.operations),
            "mutation_count": self.mutation_count,
            "affected_devices": list(self.affected_devices),
            "affected_links": list(self.affected_links),
            "blast_radius": self.blast_radius,
        }


@dataclass
class ExecutionPermission:
    """The gate's decision for a batch of operations."""

    execute: bool
    planned_receipts: list[OperationReceipt] = field(default_factory=list)


Approver = Callable[[MutationPreview], bool]


def _affected(op: dict) -> tuple[list[str], list[str]]:
    devices = [
        str(op[k])
        for k in ("device", "name", "old_name", "new_name", "a_device", "b_device", "source", "destination")
        if k in op
    ]
    links = [str(op[k]) for k in ("link_id", "a_port", "b_port") if k in op]
    return devices, links


class SafetyGate:
    """The single checkpoint every mutation passes through."""

    def __init__(
        self,
        mode: SafetyMode,
        budget: MutationBudget | None = None,
        approver: Approver | None = None,
    ):
        self.mode = mode
        self.budget = budget or MutationBudget()
        self.approver = approver

    def authorize(self, operations: Iterable[dict]) -> ExecutionPermission:
        """Decide what may happen to `operations` (list of requested-op dicts).

        A denial is a DECISION, never an exception: raising here would make
        the executor's blanket `except` record the operation as FAILED and
        consume budget for a mutation that never happened (P1-10). Denials
        return execute=False so the DAG records the op as BLOCKED and the
        caller can refund any reserved budget.
        """
        ops = [dict(op) for op in operations]
        if not ops:
            return ExecutionPermission(execute=True)

        if self.mode is SafetyMode.READ_ONLY:
            return ExecutionPermission(
                execute=False,
                planned_receipts=[
                    self._denied_receipt(op, "READ_ONLY mode rejects every mutation")
                    for op in ops
                ],
            )

        if self.mode is SafetyMode.DRY_RUN:
            return ExecutionPermission(
                execute=False,
                planned_receipts=[
                    self._planned_receipt(op) for op in ops
                ],
            )

        devices: list[str] = []
        links: list[str] = []
        for op in ops:
            d, l = _affected(op)
            devices.extend(d)
            links.extend(l)
        preview = MutationPreview(
            operations=tuple(ops),
            mutation_count=len(ops),
            affected_devices=tuple(sorted(set(devices))),
            affected_links=tuple(sorted(set(links))),
        )

        if self.mode is SafetyMode.APPROVAL_REQUIRED:
            if self.approver is None or not self.approver(preview):
                return ExecutionPermission(
                    execute=False,
                    planned_receipts=[
                        self._denied_receipt(
                            op, "APPROVAL_REQUIRED mode: no approver accepted the preview"
                        )
                        for op in ops
                    ],
                )

        self.budget.consume(len(ops))
        return ExecutionPermission(execute=True)

    @staticmethod
    def _denied_receipt(op: dict, reason: str) -> OperationReceipt:
        return OperationReceipt(
            operation_id=op.get("operation_id", "denied"),
            operation=str(op.get("operation", "unknown")),
            requested=op,
            result=OperationResult.NOT_EXECUTED,
            evidence={"decision": "denied by safety gate", "reason": reason},
        )

    @staticmethod
    def _planned_receipt(op: dict) -> OperationReceipt:
        return OperationReceipt(
            operation_id=op.get("operation_id", "planned"),
            operation=str(op.get("operation", "unknown")),
            requested=op,
            result=OperationResult.NOT_EXECUTED,
        )
