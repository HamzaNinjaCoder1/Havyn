"""Explicit-observation values: KNOWN / UNKNOWN / NOT_OBSERVED / UNSUPPORTED / STALE.

The Autopilot's core principle is that "configured" is not "verified" and
"command sent" is not "command succeeded". Actual state reported by Packet
Tracer must never silently collapse missing values, unsupported reads and
never-queried fields into one falsy blob — each of those states demands a
different next action (re-query, report a gap, skip a check, refresh). Every
observed field in `ActualNetworkState` is therefore an `Observable`, and code
that needs a value must ask for it explicitly via `require()`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class ObservationStatus(str, Enum):
    """Why we do or don't know a value."""

    KNOWN = "known"                  # observed this session; value is authoritative
    UNKNOWN = "unknown"              # queried, but the runtime could not answer
    NOT_OBSERVED = "not_observed"    # never queried
    UNSUPPORTED = "unsupported"      # the runtime cannot expose this at all
    STALE = "stale"                  # observed earlier; may have changed since


class UnknownValueError(LookupError):
    """Raised when code requires a KNOWN value and gets anything else."""


@dataclass(frozen=True)
class Observable(Generic[T]):
    """A value with an explicit epistemic status.

    `None` is never used to mean "unknown": absence of knowledge is a status,
    not a value. This is what makes UNKNOWN able to survive serialization,
    diffs and reports without quietly becoming a fact.
    """

    status: ObservationStatus = ObservationStatus.NOT_OBSERVED
    value: T | None = None
    detail: str | None = None
    source: str | None = None
    observed_at: str | None = None

    @classmethod
    def known(
        cls, value: T, *, source: str | None = None, observed_at: str | None = None
    ) -> "Observable[T]":
        return cls(ObservationStatus.KNOWN, value, None, source, observed_at)

    @classmethod
    def unknown(cls, detail: str | None = None, *, source: str | None = None) -> "Observable[T]":
        return cls(ObservationStatus.UNKNOWN, None, detail, source, None)

    @classmethod
    def not_observed(cls) -> "Observable[T]":
        return cls()

    @classmethod
    def unsupported(cls, detail: str | None = None) -> "Observable[T]":
        return cls(ObservationStatus.UNSUPPORTED, None, detail, None, None)

    @classmethod
    def stale(cls, value: T, detail: str | None = None) -> "Observable[T]":
        """A previously observed value that may have changed."""
        return cls(ObservationStatus.STALE, value, detail, None, None)

    @classmethod
    def from_raw(cls, value: Any, *, source: str | None = None) -> "Observable":
        """Convert a raw runtime answer.

        A null/empty read is UNKNOWN, never a fact — an empty string is not an
        IP address, and treating it as one is exactly the silent-false-OK class
        of bug this type exists to prevent.
        """
        if value is None or (isinstance(value, str) and value == ""):
            return cls.unknown("runtime returned an empty value", source=source)
        return cls.known(value, source=source)

    @property
    def is_known(self) -> bool:
        return self.status is ObservationStatus.KNOWN

    def require(self) -> T:
        """Return the value, or raise if it is not KNOWN.

        Fail-closed on purpose: callers that cannot proceed without a fact
        must not be handed a None that looks like one.
        """
        if not self.is_known:
            raise UnknownValueError(
                f"value is {self.status.value}: {self.detail or 'no detail'}"
            )
        return self.value  # type: ignore[return-value]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "value": self.value,
            "detail": self.detail,
            "source": self.source,
            "observed_at": self.observed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Observable":
        return cls(
            status=ObservationStatus(data.get("status", "not_observed")),
            value=data.get("value"),
            detail=data.get("detail"),
            source=data.get("source"),
            observed_at=data.get("observed_at"),
        )
