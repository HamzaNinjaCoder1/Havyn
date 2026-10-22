"""Delete every device in a live Packet Tracer workspace except protected ones.

A workspace polluted by earlier runs is not cosmetic: a live build timed out at
48 of 124 devices because the canvas already held enough devices that the
per-device existence read-backs stopped fitting in their budget. This is the
tool that returns the canvas to a known state.

    python scripts/pt_clean_workspace.py --dry-run     # show what would go
    python scripts/pt_clean_workspace.py               # delete it
    python scripts/pt_clean_workspace.py --keep-prefix "Router"   # keep more

The protection rule is a **prefix allowlist**, not a deny list: anything not
recognised as protected is deleted. A deny list would silently grow every time
a new device family appeared, and the whole point is to end up with a canvas
that contains only what the user meant to keep.

`Power Distribution Device*` is protected by default because those are the
user's own physical-environment devices, not engine output.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from typing import Protocol, Sequence

#: Device names starting with any of these are never deleted.
DEFAULT_PROTECTED_PREFIXES: tuple[str, ...] = ("Power Distribution Device",)

#: What REMAINING shows when the canvas could not be read at all. A real device
#: list is never literally this, so "unknown" can never be mistaken for empty.
_UNKNOWN = "unknown"


class DeletableWorkspace(Protocol):
    """The slice of the adapter this script needs.

    Narrow on purpose: a test can satisfy it with a fake, and the script never
    reaches for a private method. ``delete_devices_bulk`` is optional - only the
    live adapter has it - so it is looked up dynamically and the script falls
    back to per-device deletes. That keeps the null adapter usable here instead
    of making the offline contract strictly larger than the online one.
    """

    def list_device_names(self):  # -> Observable[list[str]]
        ...

    def delete_device(self, name: str):  # -> OperationReceipt
        ...


@dataclass
class CleanReport:
    """What the run planned, what it deleted, and what survived."""

    to_delete: list[str] = field(default_factory=list)
    protected: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    remaining: list[str] = field(default_factory=list)
    dry_run: bool = False
    #: The workspace could not be read. An unreadable canvas must never look like
    #: an empty one, so this stays separate from ``failed`` (delete problems) and
    #: forces a non-zero exit.
    read_failed: bool = False

    def as_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "to_delete": self.to_delete,
            "protected": self.protected,
            "deleted": self.deleted,
            "failed": self.failed,
            "remaining": self.remaining,
            "read_failed": self.read_failed,
        }


def plan(
    names: Sequence[str],
    protected_prefixes: Sequence[str] = DEFAULT_PROTECTED_PREFIXES,
) -> tuple[list[str], list[str]]:
    """Split live device names into ``(to_delete, protected)``.

    Pure and side-effect free, so the safety rule is testable without a canvas.
    Comparison is case-insensitive and prefix-anchored: ``"power distribution
    device3"`` is protected, ``"My Power Distribution Device"`` is not.
    """
    keep: list[str] = []
    drop: list[str] = []
    prefixes = tuple(p.lower() for p in protected_prefixes)
    for name in names:
        lowered = str(name).lower()
        if any(lowered.startswith(p) for p in prefixes):
            keep.append(name)
        else:
            drop.append(name)
    return sorted(drop), sorted(keep)


def _read_names(
    workspace: DeletableWorkspace,
    *,
    attempts: int = 3,
    delay: float = 0.5,
) -> list[str] | None:
    """Device names from the workspace, or ``None`` when the read itself failed.

    ``None`` is the whole point: an unreadable workspace used to be reported as
    an empty list, which is byte-identical to "the canvas is clean, all clear".
    The caller treats ``None`` as REMAINING "unknown" and refuses to proceed,
    because "I could not read it" must never look like "nothing to delete".

    A single UNKNOWN is not a verdict. PT's mailbox can be busy for a moment
    right after a build (S1.19 run04 refused to start on a canvas it could read
    two seconds later), so the read is retried a bounded few times before the
    caller is told the workspace is unreadable.
    """
    for attempt in range(max(1, attempts)):
        observed = workspace.list_device_names()
        if getattr(observed, "is_known", False):
            return [str(n) for n in (observed.value or [])]
        if attempt + 1 < attempts:
            time.sleep(delay)
    return None


def clean(
    workspace: DeletableWorkspace,
    *,
    protected_prefixes: Sequence[str] = DEFAULT_PROTECTED_PREFIXES,
    dry_run: bool = False,
) -> CleanReport:
    """Delete every unprotected device, then report what is still there.

    The bulk call is one round-trip because per-device deletes do not scale
    past a few dozen devices. Whatever the batch reports as still present is
    retried through the single-device path, and the *final* answer is the
    read-back - never the call's own claim of success.
    """
    report = CleanReport(dry_run=dry_run)
    names = _read_names(workspace)
    if names is None:
        # Unreadable: report "unknown", never an empty-looking "all clear".
        report.read_failed = True
        report.remaining = [_UNKNOWN]
        return report
    report.to_delete, report.protected = plan(names, protected_prefixes)

    if dry_run or not report.to_delete:
        report.remaining = _final_names(workspace, report)
        return report

    outcomes: dict[str, str] = {}
    bulk = getattr(workspace, "delete_devices_bulk", None)
    if callable(bulk):
        try:
            outcomes = bulk(report.to_delete)
        except Exception as exc:  # a failed batch must not skip the retries
            outcomes = {name: f"error:{type(exc).__name__}" for name in report.to_delete}

    for name in report.to_delete:
        outcome = str(outcomes.get(name, "unknown"))
        if outcome in ("deleted", "success"):
            report.deleted.append(name)
            continue
        try:
            receipt = workspace.delete_device(name)
            result = getattr(receipt, "result", None)
            value = getattr(result, "value", result)
            if str(value) == "success":
                report.deleted.append(name)
            else:
                report.failed[name] = f"retry:{value}"
        except Exception as exc:
            report.failed[name] = f"error:{type(exc).__name__}"

    report.remaining = _final_names(workspace, report)
    return report


def _final_names(workspace: DeletableWorkspace, report: CleanReport) -> list[str]:
    """The post-run read-back, keeping an unreadable canvas visible as UNKNOWN."""
    names = _read_names(workspace)
    if names is None:
        report.read_failed = True
        return [_UNKNOWN]
    return names


def _print_report(report: CleanReport) -> None:
    if report.read_failed:
        print(
            "REMAINING unknown: the workspace could not be read, so nothing was"
            " deleted and the device list is unavailable.",
            file=sys.stderr,
        )
        return
    if report.dry_run:
        print(f"DRY RUN - would delete {len(report.to_delete)} device(s):")
        for name in report.to_delete:
            print(f"  - {name}")
    else:
        print(f"deleted {len(report.deleted)} device(s)")
        if report.failed:
            print(f"failed to delete {len(report.failed)}:")
            for name, why in sorted(report.failed.items()):
                print(f"  ! {name}: {why}")
    print(f"protected {len(report.protected)} device(s)")
    print(f"REMAINING ({len(report.remaining)}): {report.remaining}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pt_clean_workspace",
        description="Delete every Packet Tracer device except protected ones.",
    )
    parser.add_argument(
        "--keep-prefix", action="append", default=None, metavar="PREFIX",
        help=(
            "protected name prefix; repeatable. Defaults to "
            f"{DEFAULT_PROTECTED_PREFIXES!r}. Matching is case-insensitive."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print the plan and the remaining devices without deleting",
    )
    args = parser.parse_args(argv)

    prefixes = tuple(args.keep_prefix) if args.keep_prefix else DEFAULT_PROTECTED_PREFIXES

    from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter

    workspace = BridgeAdapter()
    probe = workspace.ping(timeout=5.0)
    if not probe.reachable:
        print("Packet Tracer is not reachable on either channel.", file=sys.stderr)
        return 3

    report = clean(workspace, protected_prefixes=prefixes, dry_run=args.dry_run)
    _print_report(report)
    # A read failure is not "success with nothing to do": exit non-zero so a
    # caller or CI never reads an unreadable canvas as an all-clear.
    return 1 if (report.failed or report.read_failed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
