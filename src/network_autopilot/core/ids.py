"""Stable identifiers, idempotency keys and build fingerprints.

Internal object IDs are deliberately decoupled from Packet Tracer display
names: PT names are mutable (a rename changes them), while our references must
stay stable across builds, evidence records and reports.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone

_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def utc_now_iso() -> str:
    """UTC timestamp for evidence and observation records."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_id(value: str, kind: str = "id") -> str:
    """Validate a stable internal id (lowercase alnum/-/_ , starts with a letter).

    Internal ids end up in evidence files, JS command payloads and report
    anchors, so they are constrained by design instead of sanitized after
    the fact.
    """
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise ValueError(
            f"invalid {kind}: {value!r} (expected lowercase letter first, then "
            f"[a-z0-9_-], max 64 chars)"
        )
    return value


def new_id(prefix: str) -> str:
    """A fresh unique id like 'op-1f3a...'. Not stable across runs by design."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def canonical_json(obj: object) -> str:
    """Deterministic JSON text used for hashing and idempotency keys."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def idempotency_key(*parts: object) -> str:
    """Stable key for "the same request twice" detection.

    Re-running a build whose requested operations hash to the same key should
    be recognizable as a repeat, not a fresh mutation stream.
    """
    return hashlib.sha256(canonical_json(list(parts)).encode("utf-8")).hexdigest()[:16]


def build_fingerprint(
    spec_hash: str, components: dict[str, str], layout_seed: int = 0
) -> dict[str, str]:
    """Deterministic fingerprint of a build context.

    Two builds of the same NetworkSpec with the same compiler version,
    capability registry, Packet Tracer version and adapter version should
    produce the same fingerprint — that is what makes a build reproducible
    claim checkable instead of aspirational.
    """
    digest = hashlib.sha256(
        canonical_json(
            {"spec": spec_hash, "components": components, "layout_seed": layout_seed}
        ).encode("utf-8")
    ).hexdigest()[:32]
    return {
        "fingerprint": digest,
        "spec_hash": spec_hash,
        "components": dict(sorted(components.items())),
        "layout_seed": str(layout_seed),
    }
