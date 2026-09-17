"""The operation DAG: dependency-ordered execution with cycle detection.

Ordering is deterministic: Kahn's algorithm over insertion order — at every
step the ready node with the lowest insertion index runs first, so the same
compiled DAG always executes identically. A failed operation BLOCKS its
dependents (they are skipped, not attempted); it never silently lets a
dependent run against a missing foundation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterator

from .operations import NetworkOperation


class DagError(ValueError):
    """The DAG is malformed: cycle, missing dependency, or duplicate id."""


class OpStatus(str, Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"      # a dependency failed; never attempted
    SKIPPED = "skipped"      # idempotent no-op: already satisfied


@dataclass
class DagNode:
    operation: NetworkOperation
    status: OpStatus = OpStatus.PENDING
    receipt: Any = None              # OperationReceipt once executed/skipped
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation.to_dict(),
            "status": self.status.value,
            "note": self.note,
        }


@dataclass
class OperationDag:
    """A validated, deterministic execution graph."""

    nodes: dict[str, DagNode] = field(default_factory=dict)
    _order: list[str] = field(default_factory=list)

    def add(self, operation: NetworkOperation) -> None:
        if operation.operation_id in self.nodes:
            raise DagError(f"duplicate operation id: {operation.operation_id}")
        self.nodes[operation.operation_id] = DagNode(operation)
        self._order.append(operation.operation_id)

    def validate(self) -> None:
        for node in self.nodes.values():
            for dep in node.operation.dependencies:
                if dep not in self.nodes:
                    raise DagError(
                        f"operation {node.operation.operation_id!r} depends on "
                        f"unknown operation {dep!r}"
                    )
        self._assert_acyclic()

    def _assert_acyclic(self) -> None:
        indegree = {op_id: 0 for op_id in self._order}
        dependents: dict[str, list[str]] = {op_id: [] for op_id in self._order}
        for op_id in self._order:
            for dep in self.nodes[op_id].operation.dependencies:
                indegree[op_id] += 1
                dependents[dep].append(op_id)
        ready = [op_id for op_id in self._order if indegree[op_id] == 0]
        visited = 0
        while ready:
            current = ready.pop(0)
            visited += 1
            for dependent in dependents[current]:
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    ready.append(dependent)
        if visited != len(self._order):
            cyclic = sorted(op_id for op_id in self._order if indegree[op_id] > 0)
            raise DagError(f"dependency cycle detected among: {cyclic}")

    def topo_order(self) -> Iterator[str]:
        """Deterministic topological order (Kahn, insertion-index tiebreak)."""
        self.validate()
        indegree = {op_id: 0 for op_id in self._order}
        dependents: dict[str, list[str]] = {op_id: [] for op_id in self._order}
        for op_id in self._order:
            for dep in self.nodes[op_id].operation.dependencies:
                indegree[op_id] += 1
                dependents[dep].append(op_id)
        # ready list kept sorted by insertion index for determinism
        ready = sorted(
            (op_id for op_id in self._order if indegree[op_id] == 0),
            key=self._order.index,
        )
        emitted: list[str] = []
        while ready:
            current = ready.pop(0)
            emitted.append(current)
            for dependent in dependents[current]:
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    ready.append(dependent)
            ready.sort(key=self._order.index)
        return iter(emitted)

    def dependents_of(self, op_id: str) -> list[str]:
        return [
            other
            for other in self._order
            if op_id in self.nodes[other].operation.dependencies
        ]

    def mark_failed(self, op_id: str, note: str) -> list[str]:
        """Fail an operation and BLOCK every transitive dependent."""
        self.nodes[op_id].status = OpStatus.FAILED
        self.nodes[op_id].note = note
        newly_blocked: list[str] = []
        stack = list(self.dependents_of(op_id))
        while stack:
            current = stack.pop(0)
            if self.nodes[current].status is OpStatus.PENDING:
                self.nodes[current].status = OpStatus.BLOCKED
                self.nodes[current].note = f"blocked by failed dependency {op_id!r}"
                newly_blocked.append(current)
                stack.extend(self.dependents_of(current))
        return newly_blocked

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {status.value: 0 for status in OpStatus}
        for node in self.nodes.values():
            counts[node.status.value] += 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "operations": [self.nodes[op_id].to_dict() for op_id in self._order],
            "summary": self.summary(),
        }
