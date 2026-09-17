"""Operation receipts: the structured answer to "what actually happened?".

Every mutation returns a receipt. A receipt is SUCCESS only when the operation
was executed; it is *confirmed* only when read-back postconditions also passed.
"JS delivered" or "command sent" is a NOT-confirmed success at best — the
receipt model makes that distinction impossible to skip.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from .observability import Observable


class OperationResult(str, Enum):
    SUCCESS = "success"          # executed; confirmation depends on postconditions
    FAILED = "failed"            # executed and the runtime reported failure
    UNKNOWN = "unknown"          # outcome could not be determined (timeout, no answer)
    NOT_EXECUTED = "not_executed"  # planned only (dry-run)
    REJECTED = "rejected"        # blocked before execution (safety policy or unmet precondition)


@dataclass(frozen=True)
class CheckResult:
    """One precondition or postcondition check.

    `passed` is tri-state: True / False / None (could not determine). A None
    precondition is treated as failed by callers (fail-closed); a None
    postcondition keeps the receipt unconfirmed instead of confirmed.
    """

    name: str
    expected: str
    actual: Observable
    passed: bool | None

    @classmethod
    def of(
        cls,
        name: str,
        expected: str,
        actual: Observable,
        compare: Callable[[Any, str], bool] | None = None,
    ) -> "CheckResult":
        if not actual.is_known:
            return cls(name=name, expected=expected, actual=actual, passed=None)
        if compare is None:
            passed = str(actual.value) == expected
        else:
            passed = bool(compare(actual.value, expected))
        return cls(name=name, expected=expected, actual=actual, passed=passed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "expected": self.expected,
            "actual": self.actual.to_dict(),
            "passed": self.passed,
        }


@dataclass
class OperationReceipt:
    """The evidence-backed result of one adapter operation."""

    operation_id: str
    operation: str
    requested: dict[str, Any]
    executed: dict[str, Any] | None = None
    result: OperationResult = OperationResult.UNKNOWN
    preconditions: list[CheckResult] = field(default_factory=list)
    postconditions: list[CheckResult] = field(default_factory=list)
    evidence: dict[str, str] = field(default_factory=dict)
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None

    @property
    def executed_ok(self) -> bool:
        """Command was sent and the runtime did not report failure."""
        return self.result is OperationResult.SUCCESS

    @property
    def confirmed(self) -> bool:
        """Executed AND read back AND every postcondition actually passed.

        A receipt with no postconditions is by definition unconfirmed —
        "success" without read-back is exactly the claim this product refuses
        to make.
        """
        return (
            self.result is OperationResult.SUCCESS
            and bool(self.postconditions)
            and all(check.passed is True for check in self.postconditions)
        )

    @property
    def precondition_ok(self) -> bool:
        """True when every precondition is known-good (None counts as bad)."""
        return all(check.passed is True for check in self.preconditions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "operation": self.operation,
            "requested": self.requested,
            "executed": self.executed,
            "result": self.result.value,
            "confirmed": self.confirmed,
            "preconditions": [c.to_dict() for c in self.preconditions],
            "postconditions": [c.to_dict() for c in self.postconditions],
            "evidence": dict(self.evidence),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }
