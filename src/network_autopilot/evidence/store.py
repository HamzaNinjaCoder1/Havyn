"""Append-only, timestamped, provenance-tagged evidence store.

Every significant step records one line of JSON here: requests, specs,
receipts, observations, verdicts, repairs, rollbacks. Records are redacted at
write time, so reports can be shared without leaking device credentials.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.ids import new_id, utc_now_iso
from .redaction import redact


@dataclass(frozen=True)
class EvidenceRecord:
    record_id: str
    ts: str
    actor: str
    component: str
    kind: str
    payload: dict[str, Any]
    provenance: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "ts": self.ts,
            "actor": self.actor,
            "component": self.component,
            "kind": self.kind,
            "payload": self.payload,
            "provenance": self.provenance,
        }


@dataclass
class EvidenceStore:
    """One JSONL file per build under `root/<build_id>/evidence.jsonl`."""

    root: Path
    actor: str = "network_autopilot"
    build_id: str = field(default_factory=lambda: new_id("build"))
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _seq: int = field(default=0, repr=False)

    @property
    def path(self) -> Path:
        return self.root / self.build_id / "evidence.jsonl"

    def record(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        component: str = "core",
        provenance: dict[str, Any] | None = None,
    ) -> EvidenceRecord:
        record = EvidenceRecord(
            record_id=new_id("rec"),
            ts=utc_now_iso(),
            actor=self.actor,
            component=component,
            kind=kind,
            payload=redact(payload),
            provenance={
                "store_build_id": self.build_id,
                **(provenance or {}),
            },
        )
        with self._lock:
            self._seq += 1
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
        return record

    def read_all(self) -> list[EvidenceRecord]:
        if not self.path.exists():
            return []
        out: list[EvidenceRecord] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            data = json.loads(line)
            out.append(
                EvidenceRecord(
                    record_id=data["record_id"],
                    ts=data["ts"],
                    actor=data["actor"],
                    component=data["component"],
                    kind=data["kind"],
                    payload=data["payload"],
                    provenance=data.get("provenance", {}),
                )
            )
        return out

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.read_all():
            counts[record.kind] = counts.get(record.kind, 0) + 1
        return counts
