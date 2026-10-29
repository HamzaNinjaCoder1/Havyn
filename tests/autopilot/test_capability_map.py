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
        evidence = row.evidence.lower()
        # Live proof is either a live test or a verbatim capture written by a
        # probe/lab under probes/live or probes/raw (the latter is where the
        # stage3_* scripts write console output).
        assert (
            "test_live" in evidence
            or "live" in evidence
            or "probes/raw/" in evidence
            or "probes/live/" in evidence
        ), row.id
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


# -- evidence-derived status (S2.6) --------------------------------------


def _run(topology: str, *, mode: str = "live", status: str = "pass",
         run_id: str = "run-1") -> dict:
    criteria = (
        {"pass": 10, "fail": 0, "unknown": 0, "total": 10}
        if status == "pass"
        else {"pass": 8, "fail": 2, "unknown": 0, "total": 10}
    )
    return {
        "topology_id": topology,
        "case_id": topology,
        "run_id": run_id,
        "mode": mode,
        "status": status,
        "criteria": criteria,
        "elapsed_s": 120.0,
        "evidence_dir": f"evidence/{run_id}",
        "finished_at": "2026-10-22T00:00:00+00:00",
    }


def _write_history(root: Path, *runs: dict) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "history.jsonl").write_text(
        "".join(json.dumps(run) + "\n" for run in runs), encoding="utf-8")
    return root


def _row(data: dict, feature_id: str) -> dict:
    return next(f for f in data["features"] if f["id"] == feature_id)


def test_a_complete_live_pass_promotes_an_unverified_capability(tmp_path):
    runs = capability_map.load_live_results(
        _write_history(tmp_path / "results", _run("T09")))
    data = capability_map.build_map(capability_map.REGISTRY(), runs)

    ospf = _row(data, "ospf-backbone")
    assert ospf["status"] == "Verified"
    assert ospf["derived_from_run"] is True
    assert "run-1" in ospf["evidence"]
    assert data["runs"]["T09"]["status"] == "pass"


def test_an_offline_pass_never_promotes_a_capability(tmp_path):
    runs = capability_map.load_live_results(
        _write_history(tmp_path / "results", _run("T09", mode="offline")))
    data = capability_map.build_map(capability_map.REGISTRY(), runs)
    assert _row(data, "ospf-backbone")["status"] == "Unverified"


def test_an_incomplete_live_pass_is_not_promoted(tmp_path):
    run = _run("T09")
    run["criteria"] = {"pass": 9, "fail": 0, "unknown": 1, "total": 10}
    runs = capability_map.load_live_results(
        _write_history(tmp_path / "results", run))
    data = capability_map.build_map(capability_map.REGISTRY(), runs)
    ospf = _row(data, "ospf-backbone")
    assert ospf["status"] == "Unverified"
    assert ospf["latest_run"]["status"] == "pass", (
        "the run is still reported beside the row, it just proves less"
    )


def test_a_failed_live_run_is_recorded_but_does_not_promote(tmp_path):
    runs = capability_map.load_live_results(
        _write_history(tmp_path / "results", _run("T10", status="fail")))
    data = capability_map.build_map(capability_map.REGISTRY(), runs)
    nat = _row(data, "nat-pat")
    assert nat["status"] == "Unverified"
    assert nat["latest_run"]["status"] == "fail"

    text = capability_map.render_markdown(data)
    assert "Latest live runner evidence" in text
    assert "T10" in text
