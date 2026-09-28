"""Offline tests for the maintenance scripts.

Both scripts touch a live Packet Tracer canvas or the filesystem, so the logic
that can be wrong in a way nobody notices - "does it delete the user's own
device", "does it delete evidence before the archive is readable" - is split
into pure functions and tested here with a fake adapter and a temp tree. No
Packet Tracer, no network, no sleeps.
"""

from __future__ import annotations

import importlib
import zipfile
from pathlib import Path

import pytest

from network_autopilot.adapter.null_adapter import NullAdapter
from network_autopilot.core.observability import Observable, ObservationStatus

pt_clean = importlib.import_module("scripts.pt_clean_workspace")
evidence_prune = importlib.import_module("scripts.evidence_prune")


class FakeWorkspace:
    """In-memory stand-in for the live adapter's delete/list surface."""

    def __init__(self, names: list[str], *, bulk_fails: list[str] | None = None) -> None:
        self.names = list(names)
        self.bulk_fails = list(bulk_fails or [])
        self.bulk_calls: list[list[str]] = []
        self.single_calls: list[str] = []
        self.readable = True

    def list_device_names(self):
        if not self.readable:
            return Observable(
                status=ObservationStatus.UNKNOWN, value=None,
                detail="channel not ready", source="fake",
            )
        return Observable.known(list(self.names), source="fake")

    def delete_devices_bulk(self, names: list[str]) -> dict[str, str]:
        self.bulk_calls.append(list(names))
        out: dict[str, str] = {}
        for name in names:
            if name in self.bulk_fails:
                out[name] = "still-present"
                continue
            self.names.remove(name)
            out[name] = "deleted"
        return out

    def delete_device(self, name: str):
        self.single_calls.append(name)
        if name in self.names:
            self.names.remove(name)
        class _R:
            class result:
                value = "success"
        return _R()


# -- pt_clean_workspace ------------------------------------------------------


def test_power_distribution_devices_are_never_deleted():
    """The user's own environment devices must survive every run."""
    drop, keep = pt_clean.plan(
        ["Power Distribution Device0", "Power Distribution Device12", "r1", "pc1"]
    )
    assert keep == ["Power Distribution Device0", "Power Distribution Device12"]
    assert drop == ["pc1", "r1"]


def test_protection_is_a_prefix_allowlist_not_a_deny_list():
    """A device family the script has never heard of must still be deleted.

    A deny list would leak anything new; the allowlist is what makes "the canvas
    holds only what I kept" a guarantee rather than a hope.
    """
    drop, keep = pt_clean.plan(["IoT Server", "Meraki MX65W", "Solar Panel"])
    assert drop == ["IoT Server", "Meraki MX65W", "Solar Panel"]
    assert keep == []


def test_protection_matching_ignores_case_but_still_anchors_the_prefix():
    assert pt_clean.plan(["power distribution device7"])[1] == ["power distribution device7"]
    assert pt_clean.plan(["My Power Distribution Device"])[0] == ["My Power Distribution Device"]


def test_extra_keep_prefixes_replace_the_default():
    drop, keep = pt_clean.plan(
        ["Router1", "Power Distribution Device0", "Switch7"],
        protected_prefixes=("Router",),
    )
    assert keep == ["Router1"]
    assert drop == ["Power Distribution Device0", "Switch7"]


def test_clean_removes_everything_but_the_protected_devices():
    ws = FakeWorkspace(["r1", "pc1", "sw1", "Power Distribution Device0"])
    report = pt_clean.clean(ws)
    assert sorted(report.deleted) == ["pc1", "r1", "sw1"]
    assert report.failed == {}
    assert report.remaining == ["Power Distribution Device0"]


def test_clean_retries_a_device_the_batch_left_behind():
    """A batch that reports "still present" must not be believed."""
    ws = FakeWorkspace(["r1", "pc1"], bulk_fails=["r1"])
    report = pt_clean.clean(ws)
    assert ws.single_calls == ["r1"]
    assert sorted(report.deleted) == ["pc1", "r1"]
    assert report.remaining == []


def test_clean_reports_a_failure_instead_of_claiming_success():
    class Stubborn(FakeWorkspace):
        def delete_device(self, name: str):
            raise RuntimeError("device busy")

    ws = Stubborn(["r1"], bulk_fails=["r1"])
    report = pt_clean.clean(ws)
    assert report.deleted == []
    assert "r1" in report.failed
    assert report.remaining == ["r1"]


def test_dry_run_deletes_nothing_but_still_reports_the_plan():
    ws = FakeWorkspace(["r1", "Power Distribution Device0"])
    report = pt_clean.clean(ws, dry_run=True)
    assert report.to_delete == ["r1"]
    assert report.deleted == []
    assert ws.bulk_calls == [] and ws.single_calls == []
    assert report.remaining == ["r1", "Power Distribution Device0"]


def test_an_unreadable_workspace_is_not_reported_as_all_clear():
    """An UNKNOWN read must never look like 'there was nothing to delete'.

    This is the S0.4 survivor: ``_read_names`` used to fall back to
    ``observed.value or []``, and an UNKNOWN observable has ``value None``, so
    an unreadable canvas came back as an empty list - byte-identical to a clean
    one. The mutant 'UNKNOWN -> []' therefore passed while silently claiming
    all-clear. Now an unreadable read surfaces as REMAINING "unknown" and a
    read_failed flag, and the run must not delete anything in that state.
    """
    ws = FakeWorkspace(["r1"])
    ws.readable = False
    report = pt_clean.clean(ws)
    assert report.to_delete == []
    assert report.remaining == ["unknown"], (
        "an unreadable workspace is not an empty one"
    )
    assert report.read_failed is True
    assert ws.bulk_calls == [] and ws.single_calls == [], (
        "nothing may be deleted when the workspace could not be read"
    )


def test_an_unreadable_workspace_exits_non_zero(monkeypatch):
    """A canvas the script could not read is not a green run for CI either."""
    import network_autopilot.adapter.packet_tracer.bridge_adapter as bridge_mod

    class UnreadableAdapter:
        def ping(self, timeout: float = 5.0):
            class _Probe:
                reachable = True

            return _Probe()

        def list_device_names(self):
            return Observable(
                status=ObservationStatus.UNKNOWN, value=None,
                detail="channel not ready", source="fake",
            )

        def delete_device(self, name: str):
            raise AssertionError("delete must not run on an unreadable canvas")

    monkeypatch.setattr(bridge_mod, "BridgeAdapter", UnreadableAdapter)
    assert pt_clean.main([]) != 0, "read failure must exit non-zero"


def test_a_readable_empty_workspace_is_genuinely_all_clear():
    """The empty case must stay distinguishable from the unreadable one.

    If 'unreadable' and 'empty' both reported ``unknown`` the fix would be
    useless; if both reported ``[]`` we are back to the survivor. Keep both
    honest: readable-empty is a real, verified empty list.
    """
    ws = FakeWorkspace([])
    report = pt_clean.clean(ws)
    assert report.remaining == []
    assert report.read_failed is False
    assert report.to_delete == []


def test_clean_runs_against_the_real_adapter_contract():
    """Guard the Protocol: NullAdapter must already satisfy what clean() calls.

    The live adapter's bulk delete is optional by design, so the null adapter -
    which has no such method - is the real check that the script degrades to
    per-device deletes instead of demanding an online-only capability.
    """
    adapter = NullAdapter()
    for method in ("list_device_names", "delete_device"):
        assert callable(getattr(adapter, method, None)), method
    report = pt_clean.clean(adapter)
    assert report.to_delete == [] and report.failed == {}


# -- evidence_prune ----------------------------------------------------------


def _tree(root: Path, names: list[str]) -> list[Path]:
    made = []
    for i, name in enumerate(names):
        d = root / name
        (d / "nested").mkdir(parents=True)
        (d / "evidence.jsonl").write_text(f'{{"build":"{name}"}}\n', encoding="utf-8")
        (d / "nested" / "receipt.json").write_text(f'{{"n":{i}}}', encoding="utf-8")
        made.append(d)
    return made


def test_select_keeps_the_newest_n(tmp_path):
    root = tmp_path / "evidence"
    dirs = _tree(root, ["build-a", "build-b", "build-c"])
    for i, d in enumerate(dirs):
        import os

        os.utime(d, (1_700_000_000 + i * 100, 1_700_000_000 + i * 100))
    keep, prune = evidence_prune.select(dirs, keep=2)
    assert [p.name for p in keep] == ["build-c", "build-b"]
    assert [p.name for p in prune] == ["build-a"]


def test_select_keeps_nothing_when_keep_is_zero(tmp_path):
    dirs = _tree(tmp_path / "evidence", ["build-a", "build-b"])
    keep, prune = evidence_prune.select(dirs, keep=0)
    assert keep == [] and len(prune) == 2


def test_select_refuses_a_negative_keep(tmp_path):
    dirs = _tree(tmp_path / "evidence", ["build-a"])
    with pytest.raises(ValueError):
        evidence_prune.select(dirs, keep=-1)


def test_prune_archives_older_builds_and_keeps_the_newest(tmp_path):
    root = tmp_path / "evidence"
    dirs = _tree(root, ["build-a", "build-b", "build-c"])
    import os

    for i, d in enumerate(dirs):
        os.utime(d, (1_700_000_000 + i * 100, 1_700_000_000 + i * 100))

    report = evidence_prune.prune(root, keep=1, label="fixed")

    assert report.keep == ["build-c"]
    assert sorted(report.removed) == ["build-a", "build-b"]
    assert not (root / "build-a").exists() and not (root / "build-b").exists()
    assert (root / "build-c" / "evidence.jsonl").exists()

    archive = root / "archive-fixed.zip"
    assert archive.exists() and report.archive_path == str(archive)
    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
        assert "build-a/evidence.jsonl" in names
        assert "build-a/nested/receipt.json" in names
        assert zf.testzip() is None


def test_prune_keeps_the_nested_layout_so_an_archive_is_unambiguous(tmp_path):
    root = tmp_path / "evidence"
    _tree(root, ["build-a", "build-b"])
    evidence_prune.prune(root, keep=0, label="x")
    with zipfile.ZipFile(root / "archive-x.zip") as zf:
        top = {n.split("/")[0] for n in zf.namelist()}
    assert top == {"build-a", "build-b"}


def test_the_archive_is_byte_reproducible(tmp_path):
    """A hash of the archive is only meaningful if a re-run reproduces it.

    This is the S0.4 survivor. The fixed ZIP timestamp is what makes an archive
    hashable months later, but the mutant that builds ``date_time`` from the
    wall clock - or from a file's own mtime - used to slip through: both
    archives were written inside the same two-second zip-clock tick and so came
    out byte-identical anyway, "proving" determinism that was not there.

    Two things now pin the epoch:
    - the two builds get deliberately *different* file mtimes, so a member
      timestamp derived from a file cannot coincide across the pair;
    - every member's ``date_time`` is asserted against the 1980 epoch directly,
      which a wall-clock mutant cannot satisfy no matter how fast the test runs.
    """
    import os

    root = tmp_path / "evidence"
    dirs = _tree(root, ["build-a", "build-b"])
    for i, d in enumerate(dirs):
        stamp = 1_700_000_000 + i * 100_000
        for f in d.rglob("*"):
            if f.is_file():
                os.utime(f, (stamp, stamp))

    sources = sorted(root.iterdir())
    first, _ = evidence_prune.write_archive(sources, root / "one.zip")
    second, _ = evidence_prune.write_archive(sources, root / "two.zip")

    assert first == second
    assert (root / "one.zip").read_bytes() == (root / "two.zip").read_bytes()

    for archive in (root / "one.zip", root / "two.zip"):
        with zipfile.ZipFile(archive) as zf:
            assert zf.namelist(), "the archive must hold something to test"
            # Literal, not evidence_prune._ZIP_EPOCH: comparing against the
            # module's own constant would let a mutant redefine the constant
            # and be measured by it.
            assert all(
                info.date_time == (1980, 1, 1, 0, 0, 0) for info in zf.infolist()
            ), "member timestamps must stay the fixed epoch, not the clock"


def test_prune_removes_nothing_when_the_archive_cannot_be_written(tmp_path, monkeypatch):
    """The ordering that matters: archive, verify, only then unlink."""
    root = tmp_path / "evidence"
    _tree(root, ["build-a", "build-b"])

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(evidence_prune, "write_archive", broken)
    report = evidence_prune.prune(root, keep=0, label="x")

    assert report.removed == []
    assert report.failed == {"archive": "OSError: disk full"}
    assert (root / "build-a").exists() and (root / "build-b").exists()


def test_prune_removes_nothing_when_the_archive_fails_verification(tmp_path, monkeypatch):
    """A truncated zip must not be able to delete the evidence it failed to hold."""
    root = tmp_path / "evidence"
    _tree(root, ["build-a", "build-b"])

    real_zipfile = zipfile.ZipFile

    class Truncating(real_zipfile):
        def testzip(self):
            return "build-a/evidence.jsonl"

    monkeypatch.setattr(evidence_prune.zipfile, "ZipFile", Truncating)
    report = evidence_prune.prune(root, keep=0, label="x")

    assert report.removed == []
    assert "archive" in report.failed
    assert "verify failed" in report.failed["archive"]
    assert (root / "build-a").exists() and (root / "build-b").exists()


def test_prune_reports_a_folder_it_could_not_remove(tmp_path, monkeypatch):
    """A removal that fails is named, not swallowed into a clean-looking report."""
    root = tmp_path / "evidence"
    _tree(root, ["build-a", "build-b"])

    def broken(*args, **kwargs):
        raise OSError("file in use")

    monkeypatch.setattr(evidence_prune, "_rmtree", broken)
    report = evidence_prune.prune(root, keep=0, label="x")

    assert report.removed == []
    assert set(report.failed) == {"build-a", "build-b"}
    assert (root / "build-a").exists()


def test_prune_ignores_folders_that_are_not_builds(tmp_path):
    root = tmp_path / "evidence"
    (root / "my-notes").mkdir(parents=True)
    _tree(root, ["build-a", "build-b"])
    report = evidence_prune.prune(root, keep=0, label="x")
    assert (root / "my-notes").exists()
    assert sorted(report.removed) == ["build-a", "build-b"]


def test_prune_on_a_missing_root_is_a_no_op_not_a_crash(tmp_path):
    report = evidence_prune.prune(tmp_path / "nope", keep=3)
    assert report.keep == [] and report.removed == [] and report.failed == {}


def test_dry_run_archives_nothing(tmp_path):
    root = tmp_path / "evidence"
    _tree(root, ["build-a", "build-b", "build-c"])
    report = evidence_prune.prune(root, keep=1, dry_run=True)
    assert report.keep == ["build-c"]
    assert report.removed == []
    assert not list(root.glob("*.zip"))
    assert all((root / n).exists() for n in ("build-a", "build-b", "build-c"))


def test_a_manifest_records_what_went_where(tmp_path):
    """Later, 'which build is in this zip?' must be answerable without guessing."""
    import json

    root = tmp_path / "evidence"
    _tree(root, ["build-a", "build-b"])
    report = evidence_prune.prune(root, keep=0, label="m1")
    manifest = json.loads((root / "archive-m1.manifest.json").read_text(encoding="utf-8"))
    assert manifest["archived_from"] == ["build-a", "build-b"]
    assert manifest["sha256"] == report.archive_sha256
