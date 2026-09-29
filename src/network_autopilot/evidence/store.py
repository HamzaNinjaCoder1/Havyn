"""Append-only, timestamped, provenance-tagged evidence store.

Every significant step records one line of JSON here: requests, specs,
receipts, observations, verdicts, repairs, rollbacks. Records are redacted at
write time, so reports can be shared without leaking device credentials.
"""

from __future__ import annotations

import hashlib
import json
import threading
import zipfile
from collections.abc import Collection
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


#: S1.17/F15: archived evidence zips kept per evidence root. Older ones are
#: deleted at the end of a build so the folder stays bounded on a laptop.
KEEP_ARCHIVES = 2

#: Fixed ZIP timestamp so archives are byte-reproducible (same convention as
#: the manual scripts/evidence_prune.py tool).
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


def prune_evidence(
    root: Path | str,
    *,
    keep: int = 20,
    exclude: Collection[str] = (),
) -> dict[str, Any]:
    """Archive and remove old build folders, then cap old archives.

    Keeps the newest `keep` build dirs live (plus every `exclude`d name,
    which is how the running build survives its own retention pass).
    Older dirs are zipped deterministically and removed only after the zip
    verifies; a failed archive removes nothing (fail closed). Finally, only
    the newest KEEP_ARCHIVES zips (+ manifests) are kept.

    Returns a summary dict with kept/archived/removed/removed_archives,
    archive_path/archive_sha256, and failed (empty when all went well).
    """
    if keep < 0:
        raise ValueError("keep must be >= 0")
    summary: dict[str, Any] = {
        "kept": [],
        "archived": [],
        "removed": [],
        "removed_archives": [],
        "archive_path": None,
        "archive_sha256": None,
        "failed": {},
    }
    root = Path(root)
    if not root.is_dir():
        return summary
    # `keep` counts live builds INCLUDING the running one (it is the newest,
    # so it always survives on mtime alone); `exclude` is absolute deletion
    # protection by name, so the running build can never eat itself even
    # with keep=0 or a skewed clock.
    all_dirs = [
        p for p in root.iterdir()
        if p.is_dir() and p.name.startswith("build-")
    ]
    ordered = sorted(
        all_dirs, key=lambda p: (p.stat().st_mtime, p.name), reverse=True
    )
    keep_names = [p.name for p in ordered[:keep]]
    excluded_present = sorted({p.name for p in all_dirs if p.name in exclude})
    summary["kept"] = sorted(set(keep_names) | set(excluded_present))
    prune_list = [p for p in ordered[keep:] if p.name not in exclude]
    summary["archived"] = sorted(p.name for p in prune_list)
    if prune_list:
        import datetime

        label = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        destination = root / f"archive-{label}.zip"
        try:
            digest = _write_archive(prune_list, destination)
        except Exception as exc:
            summary["failed"]["archive"] = f"{type(exc).__name__}: {exc}"
            return summary
        try:
            with zipfile.ZipFile(destination) as zf:
                broken = zf.testzip()
            if broken is not None:
                raise OSError(f"archive member unreadable: {broken}")
        except Exception as exc:
            summary["failed"]["archive"] = f"verify failed, nothing removed: {exc}"
            return summary
        for path in prune_list:
            try:
                _rmtree(path)
                summary["removed"].append(path.name)
            except Exception as exc:
                summary["failed"][path.name] = f"{type(exc).__name__}: {exc}"
        summary["removed"] = sorted(summary["removed"])
        summary["archive_path"] = str(destination)
        summary["archive_sha256"] = digest
        (root / f"archive-{label}.manifest.json").write_text(
            json.dumps(
                {
                    "archived_from": summary["archived"],
                    "kept": summary["kept"],
                    "sha256": digest,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    _cap_archives(root, summary)
    return summary


def _write_archive(sources: list[Path], destination: Path) -> str:
    """Zip `sources` deterministically; return the sha256 hex digest."""
    members: list[tuple[Path, str]] = []
    for source in sources:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                members.append(
                    (path, f"{source.name}/{path.relative_to(source).as_posix()}")
                )
    members.sort(key=lambda item: item[1])
    digest = hashlib.sha256()
    partial = destination.with_name(destination.name + ".partial")
    with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, member in members:
            info = zipfile.ZipInfo(member, date_time=_ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            with path.open("rb") as fh:
                zf.writestr(info, fh.read())
    with partial.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    partial.replace(destination)
    return digest.hexdigest()


def _cap_archives(root: Path, summary: dict[str, Any]) -> None:
    """Delete archive zips (+ manifests) beyond the newest KEEP_ARCHIVES."""
    archives = sorted(root.glob("archive-*.zip"), key=lambda p: p.name)
    for stale in archives[:-KEEP_ARCHIVES] if len(archives) > KEEP_ARCHIVES else []:
        try:
            stale.unlink()
            manifest = stale.with_name(stale.stem + ".manifest.json")
            if manifest.exists():
                manifest.unlink()
            summary["removed_archives"].append(stale.name)
        except Exception as exc:
            summary["failed"][stale.name] = f"{type(exc).__name__}: {exc}"
    summary["removed_archives"] = sorted(summary["removed_archives"])


def _rmtree(path: Path) -> None:
    """Remove a directory tree, read-only files and all."""
    import os
    import shutil

    def _onerror(func, target, _exc):
        os.chmod(target, 0o700)
        func(target)

    shutil.rmtree(path, onerror=_onerror)


def completed_ops_from_evidence(root: Path | str, build_id: str) -> dict[str, str]:
    """Confirmed operation ids from a prior build's evidence, for --resume.

    Only receipts that are *confirmed* (executed AND read back AND every
    postcondition passed) count: resuming over an unconfirmed op would bless
    work that was never proven. Returns {operation_id: build_id}. An
    unknown build or a build with nothing confirmed is an empty dict — the
    caller fails closed on that rather than silently rebuilding.
    """
    store = EvidenceStore(root=Path(root), build_id=build_id)
    out: dict[str, str] = {}
    for record in store.read_all():
        if record.kind != "operation_receipt":
            continue
        payload = record.payload or {}
        if payload.get("confirmed") is True and payload.get("operation_id"):
            out[str(payload["operation_id"])] = build_id
    return out
