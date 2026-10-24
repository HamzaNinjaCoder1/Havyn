"""Mandatory verified pre-fix backup (S4.9).

No repair starts until recovery protection is established and validated:

1. the Packet Tracer project is saved to a dedicated backup directory and the
   file is read back and hashed;
2. every device the repair will modify has its running configuration saved
   (IOS/ASA), or its host configuration is recorded;
3. a manifest records the locations, hashes, sizes, timestamp and the intended
   change;
4. `validate()` re-checks the files on disk.

If the project backup cannot be proven to exist, or a configuration the repair
will touch cannot be saved, the backup reports unavailable and the repair
engine refuses to mutate. That refusal is the feature: a fix you cannot undo
is a lab you can lose.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..adapter.base import PacketTracerAdapter
from ..evidence.store import EvidenceStore
from .config_reader import RunningConfigReader, safe_component
from .models import ConfigExtract, ConfigStatus, Platform


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class BackupEntry:
    device: str
    kind: str                      # running_config | host_config
    status: str
    path: str | None = None
    sha256: str | None = None
    size: int | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "kind": self.kind,
            "status": self.status,
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "detail": self.detail,
        }


@dataclass
class BackupManifest:
    backup_id: str
    created_at: str
    project_name: str
    backup_dir: str
    project_path: str | None = None
    project_sha256: str | None = None
    project_size: int | None = None
    entries: list[BackupEntry] = field(default_factory=list)
    intended_changes: list[dict[str, Any]] = field(default_factory=list)
    adapter: str = ""
    problems: list[str] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return self.project_path is not None and self.project_sha256 is not None

    def entry_for(self, device: str) -> BackupEntry | None:
        for entry in self.entries:
            if entry.device == device:
                return entry
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "backup_id": self.backup_id,
            "created_at": self.created_at,
            "project_name": self.project_name,
            "backup_dir": self.backup_dir,
            "project_path": self.project_path,
            "project_sha256": self.project_sha256,
            "project_size": self.project_size,
            "entries": [e.to_dict() for e in self.entries],
            "intended_changes": list(self.intended_changes),
            "adapter": self.adapter,
            "available": self.available,
            "problems": list(self.problems),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BackupManifest":
        manifest = cls(
            backup_id=str(data.get("backup_id") or ""),
            created_at=str(data.get("created_at") or ""),
            project_name=str(data.get("project_name") or ""),
            backup_dir=str(data.get("backup_dir") or ""),
            project_path=data.get("project_path"),
            project_sha256=data.get("project_sha256"),
            project_size=data.get("project_size"),
            intended_changes=list(data.get("intended_changes") or []),
            adapter=str(data.get("adapter") or ""),
            problems=list(data.get("problems") or []),
        )
        for raw in data.get("entries") or []:
            manifest.entries.append(
                BackupEntry(
                    device=str(raw.get("device")),
                    kind=str(raw.get("kind")),
                    status=str(raw.get("status")),
                    path=raw.get("path"),
                    sha256=raw.get("sha256"),
                    size=raw.get("size"),
                    detail=raw.get("detail"),
                )
            )
        return manifest


@dataclass(frozen=True)
class BackupValidation:
    available: bool
    problems: tuple[str, ...]
    manifest: BackupManifest | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "problems": list(self.problems),
            "manifest": self.manifest.to_dict() if self.manifest else None,
        }


class BackupManager:
    """Create and validate the recovery point a repair depends on."""

    def __init__(
        self,
        adapter: PacketTracerAdapter,
        *,
        root: Path | str,
        evidence: EvidenceStore | None = None,
        reader: RunningConfigReader | None = None,
    ):
        self.adapter = adapter
        self.root = Path(root)
        self.evidence = evidence
        self.reader = reader or RunningConfigReader(adapter, evidence=evidence)

    def create(
        self,
        project_name: str,
        *,
        devices: Iterable[str] = (),
        intended_changes: Iterable[dict[str, Any]] = (),
        build_id: str | None = None,
    ) -> BackupManifest:
        created = _now()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        short = hashlib.sha1(
            f"{project_name}:{created}".encode("utf-8")
        ).hexdigest()[:8]
        backup_id = f"bak-{stamp}-{short}"
        directory = self.root / safe_component(
            build_id or "brownfield"
        ) / safe_component(backup_id)
        directory.mkdir(parents=True, exist_ok=True)
        manifest = BackupManifest(
            backup_id=backup_id,
            created_at=created,
            project_name=project_name,
            backup_dir=str(directory),
            intended_changes=[dict(item) for item in intended_changes],
            adapter=getattr(self.adapter, "name", type(self.adapter).__name__),
        )

        # 1) The project itself.
        filename = safe_component(project_name, fallback="brownfield") + ".pkt"
        saved_path: Path | None = None
        try:
            try:
                receipt = self.adapter.save_project(filename, directory=str(directory))
            except TypeError:
                receipt = self.adapter.save_project(filename)
            executed = getattr(receipt, "executed", None)
            if isinstance(executed, dict) and executed.get("saved_path"):
                candidate = Path(str(executed["saved_path"]))
                if candidate.exists():
                    saved_path = candidate
            if saved_path is None:
                candidate = directory / filename
                if candidate.exists():
                    saved_path = candidate
            result = getattr(receipt, "result", None)
            ok = str(getattr(result, "value", result)) in ("success", "OperationResult.SUCCESS")
            if not ok:
                manifest.problems.append(
                    "project save did not succeed: "
                    f"{getattr(receipt, 'error', None) or getattr(result, 'value', result)}"
                )
        except Exception as exc:
            manifest.problems.append(f"project save raised: {type(exc).__name__}: {exc}")
        if saved_path is not None:
            try:
                manifest.project_path = str(saved_path)
                manifest.project_size = saved_path.stat().st_size
                manifest.project_sha256 = _sha256_file(saved_path)
                if manifest.project_size <= 0:
                    manifest.problems.append("saved project file is empty")
                    manifest.project_sha256 = None
            except OSError as exc:
                manifest.problems.append(f"could not read saved project: {exc}")
        else:
            manifest.problems.append(
                f"saved project file was not found under {directory}"
            )

        # 2) Working configurations for everything the repair will touch.
        for device in sorted(set(devices)):
            entry = self._save_device_config(directory, device)
            manifest.entries.append(entry)
            if entry.status not in ("known", "partial", "host_known"):
                manifest.problems.append(
                    f"{device}: configuration for rollback is not available "
                    f"({entry.status}: {entry.detail or 'no detail'})"
                )

        # 3) The manifest itself, written last so its presence is a fact.
        manifest_path = directory / "backup-manifest.json"
        manifest_path.write_text(
            json.dumps(manifest.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
            newline="\n",
        )
        self._record(manifest)
        return manifest

    def _save_device_config(self, directory: Path, device: str) -> BackupEntry:
        platform = Platform.UNKNOWN
        try:
            answer = getattr(self.adapter, "read_host_ip_config", lambda _d: None)(device)
        except Exception:
            answer = None
        # Host configuration is the rollback source for a host.
        if answer is not None and getattr(answer, "is_known", False):
            payload = json.dumps(answer.value or {}, indent=2, sort_keys=True)
            path = directory / "configs" / (safe_component(device) + ".host.json")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(payload, encoding="utf-8", newline="\n")
            return BackupEntry(
                device=device,
                kind="host_config",
                status="host_known",
                path=str(path),
                sha256=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                size=len(payload.encode("utf-8")),
            )
        extract: ConfigExtract = self.reader.read(device, platform=platform, parse=False)
        if extract.readable:
            path = directory / "configs" / (safe_component(device) + ".txt")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(extract.raw, encoding="utf-8", newline="\n")
            return BackupEntry(
                device=device,
                kind="running_config",
                status=extract.status.value,
                path=str(path),
                sha256=extract.sha256,
                size=len(extract.raw.encode("utf-8")),
            )
        return BackupEntry(
            device=device,
            kind="running_config",
            status=extract.status.value,
            detail=extract.detail or "configuration was not readable",
        )

    def validate(self, manifest: BackupManifest) -> BackupValidation:
        problems: list[str] = []
        if manifest.project_path is None or manifest.project_sha256 is None:
            problems.append("no verified project backup")
        else:
            path = Path(manifest.project_path)
            if not path.exists():
                problems.append(f"project backup is missing: {path}")
            else:
                try:
                    actual = _sha256_file(path)
                except OSError as exc:
                    problems.append(f"project backup is unreadable: {exc}")
                else:
                    if actual != manifest.project_sha256:
                        problems.append(
                            "project backup hash does not match the manifest"
                        )
        for entry in manifest.entries:
            if not entry.path:
                if entry.status not in ("unsupported", "unknown"):
                    problems.append(
                        f"{entry.device}: config backup absent ({entry.status})"
                    )
                continue
            path = Path(entry.path)
            if not path.exists():
                problems.append(f"{entry.device}: config backup is missing: {path}")
                continue
            if entry.sha256:
                actual = _sha256_file(path)
                if actual != entry.sha256:
                    problems.append(
                        f"{entry.device}: config backup hash does not match"
                    )
        validation = BackupValidation(
            available=not problems, problems=tuple(problems), manifest=manifest
        )
        if self.evidence is not None:
            self.evidence.record(
                "brownfield_backup_validated",
                {"manifest": manifest.to_dict(), "validation": validation.to_dict()},
                component="brownfield.backup",
            )
        return validation

    def _record(self, manifest: BackupManifest) -> None:
        if self.evidence is None:
            return
        self.evidence.record(
            "brownfield_backup_created",
            manifest.to_dict(),
            component="brownfield.backup",
        )
