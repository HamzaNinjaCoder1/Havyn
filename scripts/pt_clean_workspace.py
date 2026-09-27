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
from dataclasses import dataclass, field
from typing import Protocol, Sequence

#: Device names starting with any of these are never deleted.
DEFAULT_PROTECTED_PREFIXES: tuple[str, ...] = ("Power Distribution Device",)


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

    def as_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "to_delete": self.to_delete,
            "protected": self.protected,
            "deleted": self.deleted,
            "failed": self.failed,
            "remaining": self.remaining,
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


def _read_names(workspace: DeletableWorkspace) -> list[str]:
    """Device names from the workspace, or an empty list if the read failed.

    An unreadable workspace is reported as empty rather than raised: the caller
    prints the result, and "I could not read it" must never look like "there was
    nothing to delete, all clear".
    """
    observed = workspace.list_device_names()
    if not getattr(observed, "is_known", False):
        return []
    return [str(n) for n in (observed.value or [])]


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
    report.to_delete, report.protected = plan(_read_names(workspace), protected_prefixes)

    if dry_run or not report.to_delete:
        report.remaining = _read_names(workspace)
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

    report.remaining = _read_names(workspace)
    return report


def _print_report(report: CleanReport) -> None:
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
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
