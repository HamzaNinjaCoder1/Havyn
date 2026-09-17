"""Evidence store: append-only, timestamped, and redacted at write time."""

from __future__ import annotations

import json

from src.network_autopilot.evidence import EvidenceStore, redact


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
