"""Capability Map (S2.6): no status without evidence, no invented support."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

capability_map = importlib.import_module("scripts.capability_map")

REPO_ROOT = Path(__file__).parents[2]


def test_every_row_has_a_status_and_a_real_evidence_pointer():
    for feature in capability_map.REGISTRY():
        feature.validate()  # raises on unknown status / missing evidence
        if feature.status in ("Verified", "Refused"):
            assert feature.evidence_path().is_file()


def test_verified_means_a_live_test_and_refused_means_a_reason():
    rows = capability_map.REGISTRY()
    verified = [f for f in rows if f.status == "Verified"]
    refused = [f for f in rows if f.status == "Refused"]
    assert verified and refused
    for row in verified:
        assert "test_live" in row.evidence or "live" in row.evidence.lower(), row.id
    for row in refused:
        assert row.refusal_reason, row.id


def test_unknown_status_is_rejected_not_rendered():
    bad = capability_map.Feature(
        id="x", name="x", families=("router",), status="Proven",
        evidence="tests/autopilot/test_asa_policy.py",
    )
    with pytest.raises(ValueError):
        capability_map.build_map([bad])


def test_missing_evidence_file_is_rejected():
    bad = capability_map.Feature(
        id="x", name="x", families=("router",), status="Verified",
        evidence="tests/autopilot/test_does_not_exist.py",
    )
    with pytest.raises(ValueError):
        bad.validate()


def test_generator_writes_both_artifacts(tmp_path):
    md, js = tmp_path / "map.md", tmp_path / "map.json"
    assert capability_map.main(["--out-docs", str(md), "--out-json", str(js)]) == 0
    data = json.loads(js.read_text(encoding="utf-8"))
    assert data["features"], "an empty map proves nothing"
    text = md.read_text(encoding="utf-8")
    for status in ("Verified", "Unverified", "Refused"):
        assert f"## {status}" in text
    assert "asa-address-scoped" in text
