"""Archive and remove old build evidence folders, keeping the newest N.

Every build writes an `autopilot_evidence/build-<id>/` folder. Hundreds of them
fill the disk with JSONL that nobody reads twice, so this keeps the recent ones
readable and compresses the rest into a single archive that is still
restorable.

    python scripts/evidence_prune.py --keep 3 --dry-run
    python scripts/evidence_prune.py --keep 3

The archive is **deterministic**: fixed member order and a fixed timestamp, so
pruning the same input twice produces byte-identical zips. That is what makes an
archive trustworthy - you can hash it and compare months later.

Nothing is deleted before its archive has been written *and* re-read, so an
interrupted run loses nothing: the worst case is a stale archive that can be
deleted by hand.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

#: Where pruned evidence is compressed to, relative to the evidence root.
ARCHIVE_NAME = "archive-{label}.zip"

#: Fixed ZIP timestamp (1980-01-01) so archives are byte-reproducible.
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


@dataclass
class PruneReport:
    """What was kept, what was archived, and what the archive is worth."""

    keep: list[str] = field(default_factory=list)
    archived: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    archive_path: str | None = None
    archive_sha256: str | None = None
    dry_run: bool = False

    def as_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "keep": self.keep,
            "archived": self.archived,
            "removed": self.removed,
            "failed": self.failed,
            "archive_path": self.archive_path,
            "archive_sha256": self.archive_sha256,
        }


def select(
    directories: list[Path], keep: int
) -> tuple[list[Path], list[Path]]:
    """Split build folders into ``(newest_keep, older_prune)``.

    Newest wins by modification time, falling back to the name so a copied tree
    with clobbered mtimes still prunes deterministically. ``keep <= 0`` prunes
    everything, which is a legitimate way to archive the lot.
    """
    ordered = sorted(directories, key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    if keep < 0:
        raise ValueError("keep must be >= 0")
    return ordered[:keep], ordered[keep:]


def _iter_files(root: Path):
    """Every file under ``root``, as paths relative to it, in sorted order."""
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path, path.relative_to(root).as_posix()


def write_archive(sources: list[Path], destination: Path) -> tuple[str, int]:
    """Zip ``sources`` into ``destination``; return ``(sha256, bytes)``.

    The archive is written to a temporary sibling and moved into place, so a
    partial write can never be mistaken for a finished archive. Each build keeps
    its own top-level folder inside the zip, which is what makes extraction
    unambiguous later.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".partial")
    digest = hashlib.sha256()
    total = 0

    with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for source in sorted(sources, key=lambda p: p.name):
            for path, arcname in _iter_files(source):
                member = f"{source.name}/{arcname}"
                info = zipfile.ZipInfo(member, date_time=_ZIP_EPOCH)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                zf.writestr(info, path.read_bytes())
    total = partial.stat().st_size
    partial.replace(destination)

    with open(destination, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest(), total


def prune(
    evidence_root: Path,
    *,
    keep: int = 3,
    label: str | None = None,
    dry_run: bool = False,
) -> PruneReport:
    """Archive and remove every build folder under ``evidence_root`` but the newest."""
    report = PruneReport(dry_run=dry_run)
    evidence_root = Path(evidence_root)
    if not evidence_root.is_dir():
        return report

    builds = [p for p in evidence_root.iterdir() if p.is_dir() and p.name.startswith("build-")]
    kept, prune_list = select(builds, keep)
    report.keep = [p.name for p in kept]
    report.archived = [p.name for p in prune_list]
    if dry_run or not prune_list:
        return report

    stamp = label or _timestamp()
    destination = evidence_root / ARCHIVE_NAME.format(label=stamp)
    try:
        digest, _size = write_archive(prune_list, destination)
    except Exception as exc:
        report.failed["archive"] = f"{type(exc).__name__}: {exc}"
        return report

    # The archive is on disk and readable: only now is it safe to unlink the
    # originals. Re-read it first so a truncated zip cannot delete the evidence.
    try:
        with zipfile.ZipFile(destination) as zf:
            broken = zf.testzip()
        if broken is not None:
            raise OSError(f"archive member unreadable: {broken}")
    except Exception as exc:
        report.failed["archive"] = f"verify failed, nothing removed: {exc}"
        return report

    for path in prune_list:
        try:
            _rmtree(path)
            report.removed.append(path.name)
        except Exception as exc:
            report.failed[path.name] = f"{type(exc).__name__}: {exc}"

    report.archive_path = str(destination)
    report.archive_sha256 = digest
    manifest = {
        "archived_from": sorted(p.name for p in prune_list),
        "kept": sorted(p.name for p in kept),
        "sha256": digest,
    }
    (evidence_root / f"archive-{stamp}.manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    return report


def _rmtree(path: Path) -> None:
    """Remove a directory tree, read-only files and all."""
    import shutil

    def _onerror(func, target, _exc):
        import os

        os.chmod(target, 0o700)
        func(target)

    shutil.rmtree(path, onerror=_onerror)


def _timestamp() -> str:
    import datetime

    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="evidence_prune",
        description="Archive and remove old autopilot evidence folders.",
    )
    parser.add_argument("--root", default="autopilot_evidence", help="evidence root directory")
    parser.add_argument("--keep", type=int, default=3, help="how many newest build-* folders to keep")
    parser.add_argument("--label", default=None, help="archive label (default: timestamp)")
    parser.add_argument("--dry-run", action="store_true", help="report only, change nothing")
    args = parser.parse_args(argv)

    report = prune(Path(args.root), keep=args.keep, label=args.label, dry_run=args.dry_run)

    if report.dry_run:
        print(f"DRY RUN - keep {len(report.keep)}: {report.keep}")
        print(f"DRY RUN - would archive {len(report.archived)} folder(s)")
        for name in report.archived[:10]:
            print(f"  - {name}")
        if len(report.archived) > 10:
            print(f"  ... and {len(report.archived) - 10} more")
        return 0

    print(f"kept {len(report.keep)}: {report.keep}")
    print(f"archived {len(report.archived)} folder(s) -> {report.archive_path}")
    print(f"removed {len(report.removed)} folder(s)")
    if report.archive_sha256:
        print(f"archive sha256: {report.archive_sha256}")
    if report.failed:
        print("failures:")
        for name, why in sorted(report.failed.items()):
            print(f"  ! {name}: {why}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
