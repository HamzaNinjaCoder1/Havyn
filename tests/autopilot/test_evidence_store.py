"""Evidence store: append-only, timestamped, and redacted at write time."""

from __future__ import annotations

import json
import os
import zipfile

from src.network_autopilot.evidence import EvidenceStore, redact
from src.network_autopilot.evidence.store import prune_evidence


def _aged_build(root, name: str, age_rank: int):
    """A fake old build dir, rank 0 = oldest."""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "evidence.jsonl").write_text('{"kind": "x"}\n', encoding="utf-8")
    stamp = 1_700_000_000 + age_rank * 100
    os.utime(d, (stamp, stamp))
    return d


def test_records_are_written_and_readable(tmp_path):
    store = EvidenceStore(tmp_path)
    store.record("spec_accepted", {"name": "net", "devices": 3}, component="core")
    store.record("operation", {"operation": "create_device"}, component="adapter")
    records = store.read_all()
    assert len(records) == 2
    assert records[0].kind == "spec_accepted"
    assert records[1].ts >= records[0].ts  # timestamps present and ordered
    assert store.summary() == {"spec_accepted": 1, "operation": 1}


def test_secret_keys_are_redacted(tmp_path):
    store = EvidenceStore(tmp_path)
    store.record(
        "config",
        {"password": "hunter2", "enable_secret": "cisco", "name": "R1"},
    )
    payload = store.read_all()[0].payload
    assert payload["password"] == "***REDACTED***"
    assert payload["enable_secret"] == "***REDACTED***"
    assert payload["name"] == "R1"  # non-secret data survives


def test_inline_ios_credentials_are_masked(tmp_path):
    store = EvidenceStore(tmp_path)
    store.record(
        "ios_block",
        {"commands": "enable secret cisco\nusername ops secret s3cret\nhostname R1"},
    )
    payload = store.read_all()[0].payload
    assert "enable secret ***REDACTED***" in payload["commands"]
    assert "username ops secret ***REDACTED***" in payload["commands"]
    assert "hostname R1" in payload["commands"]
    assert "cisco" not in payload["commands"]


def test_records_are_valid_json_lines(tmp_path):
    store = EvidenceStore(tmp_path)
    store.record("a", {"x": 1})
    store.record("b", {"y": [1, 2, {"z": "w"}]})
    lines = store.path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["kind"] == "b"


def test_redact_handles_nested_structures():
    out = redact({"creds": {"token": "abc"}, "list": [{"community_string": "public"}], "keep": 1})
    assert out["creds"]["token"] == "***REDACTED***"
    assert out["list"][0]["community_string"] == "***REDACTED***"
    assert out["keep"] == 1


# -- S1.17 / F15: retention -------------------------------------------------


def test_prune_keeps_the_newest_and_archives_the_rest(tmp_path):
    """Only `keep` newest builds stay live; older ones land in a zip first,
    and only then are removed."""
    root = tmp_path / "evidence"
    for rank, name in enumerate(("build-old-1", "build-old-2", "build-old-3")):
        _aged_build(root, name, rank)

    summary = prune_evidence(root, keep=2)

    assert sorted(summary["kept"]) == ["build-old-2", "build-old-3"]
    assert sorted(summary["removed"]) == ["build-old-1"]
    assert not (root / "build-old-1").exists()
    assert (root / "build-old-2" / "evidence.jsonl").exists()
    archives = list(root.glob("archive-*.zip"))
    assert len(archives) == 1, archives
    with zipfile.ZipFile(archives[0]) as zf:
        assert "build-old-1/evidence.jsonl" in zf.namelist()
        assert zf.testzip() is None


def test_prune_never_touches_the_excluded_build(tmp_path):
    """The running build is excluded by id: even keep=1 cannot eat it, while
    the keep window still applies to everything else."""
    root = tmp_path / "evidence"
    _aged_build(root, "build-old-1", 0)
    _aged_build(root, "build-current", 1)

    summary = prune_evidence(root, keep=1, exclude={"build-current"})

    assert sorted(summary["kept"]) == ["build-current"]
    assert summary["removed"] == ["build-old-1"]
    assert (root / "build-current").exists()
    assert not (root / "build-old-1").exists()


def test_prune_caps_old_archives(tmp_path):
    """Old archive zips accumulate one per pruning run; only the newest two
    survive, with their manifests."""
    root = tmp_path / "evidence"
    root.mkdir(parents=True, exist_ok=True)
    for label in ("20240101-000000", "20240201-000000", "20240301-000000"):
        (root / f"archive-{label}.zip").write_bytes(b"fake-zip")
        (root / f"archive-{label}.manifest.json").write_text("{}", encoding="utf-8")

    summary = prune_evidence(root, keep=20)

    remaining = sorted(p.name for p in root.glob("archive-*.zip"))
    assert remaining == [
        "archive-20240201-000000.zip",
        "archive-20240301-000000.zip",
    ], remaining
    assert not (root / "archive-20240101-000000.zip").exists()
    assert not (root / "archive-20240101-000000.manifest.json").exists()
    assert summary["removed_archives"] == ["archive-20240101-000000.zip"]


def test_prune_on_a_missing_root_is_a_no_op(tmp_path):
    summary = prune_evidence(tmp_path / "nope", keep=20)
    assert summary["kept"] == [] and summary["removed"] == []
