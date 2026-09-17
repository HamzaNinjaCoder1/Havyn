"""Network operations: the typed units the compiler emits and the executor runs.

An operation carries everything needed to judge it later: inputs, dependencies,
pre/postconditions, classification, risk, blast radius, retry policy and an
idempotency key. Nothing here knows about Packet Tracer — the adapter contract
is the only execution surface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..core.ids import canonical_json, idempotency_key

OPERATION_SCHEMA = 2


class MutationClassification(str, Enum):
    OBSERVE = "observe"        # reads state; never mutates
    CREATE = "create"          # brings new resources into existence
    LINK = "link"              # connects existing resources
    CONFIGURE = "configure"    # changes configuration of existing resources
    PERSIST = "persist"        # saves state
    CONTROL = "control"        # workflow-only (e.g. verify batches)


class Risk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class Check:
    """A structured precondition/postcondition the executor can evaluate."""

    kind: str                     # e.g. device_absent, device_present, port_free
    target: str
    expected: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "target": self.target, "expected": self.expected}


@dataclass(frozen=True)
class NetworkOperation:
    operation_id: str
    operation_type: str
    inputs: dict[str, Any] = field(default_factory=dict)
    dependencies: tuple[str, ...] = ()
    preconditions: tuple[Check, ...] = ()
    expected_postconditions: tuple[Check, ...] = ()
    classification: MutationClassification = MutationClassification.CREATE
    risk: Risk = Risk.LOW
    blast_radius: tuple[str, ...] = ()
    retry_policy: int = 0
    idempotency_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "operation_type": self.operation_type,
            "inputs": self.inputs,
            "dependencies": list(self.dependencies),
            "preconditions": [c.to_dict() for c in self.preconditions],
            "expected_postconditions": [c.to_dict() for c in self.expected_postconditions],
            "classification": self.classification.value,
            "risk": self.risk.value,
            "blast_radius": list(self.blast_radius),
            "retry_policy": self.retry_policy,
            "idempotency_key": self.idempotency_key,
            "schema": OPERATION_SCHEMA,
        }


def key_for(kind: str, *parts: Any) -> str:
    """Stable idempotency key for a semantically-identified operation."""
    return idempotency_key(kind, *[canonical_json(p) for p in parts])
