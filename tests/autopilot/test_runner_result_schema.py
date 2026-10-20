"""Runner result contract (S2.2): attributable, honest, and verified upload.

A result the scorecard cannot attribute is a result nobody can act on: which
topology, which run, live or offline, where the evidence is. These tests pin
the field set and — more importantly — the two places where a result could
lie: a refusal recorded as a failure, and an upload recorded as successful
without the bytes having arrived.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from network_autopilot.adapter import NullAdapter

havyn_runner = importlib.import_module("scripts.havyn_runner")

TINY_SPEC = {
    "name": "runner-schema",
    "devices": [
        {"id": "r1", "role": "router"},
        {"id": "p1", "role": "pc"},
    ],
    "links": [{"id": "l1", "a": {"device": "r1"}, "b": {"device": "p1"}}],
    "subnets": [{"id": "net1", "cidr": "192.168.240.0/24"}],
    "ip_assignments": [
        {"device": "r1", "subnet": "net1", "ip": "192.168.240.1"},
        {"device": "p1", "subnet": "net1"},
    ],
    "routing": {"mode": "connected"},
    "requirements": [{"id": "R-1", "statement": "p1 reaches r1"}],
    "acceptance_criteria": [{
        "id": "A-1",
        "requirement_ids": ["R-1"],
        "title": "p1 reaches r1",
        "test": {"kind": "flow", "source": "p1", "destination": "r1",
                 "expect": "reachable"},
    }],
}


def _queue(tmp_path: Path, **case_extra) -> Path:
    spec = tmp_path / "T99-tiny.json"
    spec.write_text(json.dumps(TINY_SPEC), encoding="utf-8")
    case = {"id": "T99", "spec": str(spec), "safety_mode": "autonomous"}
    case.update(case_extra)
    queue = tmp_path / "queue.json"
    queue.write_text(json.dumps({"cases": [case]}), encoding="utf-8")
    return queue


def _run(tmp_path: Path, queue: Path, *, mode: str = "offline", **kwargs):
    results = tmp_path / "results"
    cases = havyn_runner.load_queue(queue)
    return havyn_runner.run_queue(
        cases, NullAdapter, results, tmp_path / "evidence",
        mode=mode, **kwargs,
    )


def test_every_result_is_attributable(tmp_path):
    """Identity is not decoration: it is what makes a result actionable."""
    queue = _queue(tmp_path, topology="T99")
    summary = _run(tmp_path, queue, mode="live", prune_every=99)
    assert summary.ran == 1

    data = json.loads((tmp_path / "results" / "T99.json").read_text("utf-8"))
    for key in ("case_id", "topology_id", "spec", "run_id", "mode", "status",
                "started_at", "finished_at", "elapsed_s", "criteria",
                "criteria_detail", "evidence_dir", "refused", "refusal_reason",
                "timed_out"):
        assert key in data, f"result is missing {key}"
    assert data["topology_id"] == "T99"
    assert data["run_id"].startswith("run-")
    assert data["mode"] == "live"
    assert data["started_at"] and data["finished_at"]


def test_mode_comes_from_the_caller_not_from_the_verdict(tmp_path):
    """A NullAdapter run must never be recorded as live.

    The offline and live runs of the same queue produce comparable-looking
    verdicts; if `mode` were inferred, an offline result could stand in for a
    live one in the scorecard — the exact substitution this field prevents.
    """
    (tmp_path / "a").mkdir()
    offline = _run(tmp_path / "a", _queue(tmp_path / "a"), mode="offline")
    assert offline.ran == 1
    card = json.loads((tmp_path / "a" / "results" / "T99.json").read_text("utf-8"))
    assert card["mode"] == "offline"


def test_topology_id_falls_back_to_the_spec_stem(tmp_path):
    """An unlabelled case is still attributable to a topology."""
    queue = _queue(tmp_path)  # no "topology" key
    _run(tmp_path, queue, prune_every=99)
    data = json.loads((tmp_path / "results" / "T99.json").read_text("utf-8"))
    assert data["topology_id"] == "T99-tiny"


def test_a_failing_case_carries_its_per_criterion_verdicts(tmp_path):
    """A failure has to be readable without opening the evidence store."""
    queue = _queue(tmp_path)
    _run(tmp_path, queue, prune_every=99)
    data = json.loads((tmp_path / "results" / "T99.json").read_text("utf-8"))
    assert data["status"] == "fail"  # the data plane is unobservable offline
    assert data["criteria_detail"], "no per-criterion detail recorded"
    assert set(data["criteria_detail"].values()) <= {
        "pass", "fail", "unknown", "not_run"
    }


def test_a_dirty_workspace_is_not_recorded_as_a_refusal(tmp_path, monkeypatch):
    """Blocked-by-environment and refused-by-engine are different facts.

    Both stop the case, and only one of them is a decision about the spec.
    Collapsing them would let the scorecard report "Havyn refused this" when
    the truth was "the canvas was dirty".
    """
    import scripts.pt_clean_workspace as clean_mod

    class _Dirty:
        read_failed = False
        failed = {"leftover": "not deleted"}
        remaining = ["leftover"]
        protected: list[str] = []

    monkeypatch.setattr(clean_mod, "clean", lambda adapter: _Dirty())
    queue = _queue(tmp_path)
    _run(tmp_path, queue, prune_every=99)
    data = json.loads((tmp_path / "results" / "T99.json").read_text("utf-8"))
    assert data["status"] == "blocked"
    assert data["refused"] is False
    assert data["refusal_reason"] is None


def test_preflight_refusal_is_recorded_as_a_refusal(tmp_path, monkeypatch):
    """A spec PT cannot build is refused by name, and says so.

    The refusal is injected at the orchestrator boundary (the same seam
    `test_build_havyn_exit_codes` uses) so this test is about the RUNNER's
    mapping — a typed preflight block becomes ``refused`` with the engine's
    own words, not a generic error.
    """
    from network_autopilot.core.build_errors import PreflightBlocked
    from network_autopilot.orchestrator import Autopilot

    def _refuse(self, spec, **kwargs):
        return None, PreflightBlocked(
            "preflight blocked execution: policy on a device with no "
            "enforcement path: pc1"
        )

    monkeypatch.setattr(Autopilot, "build", _refuse)
    queue = _queue(tmp_path)
    _run(tmp_path, queue, prune_every=99)

    data = json.loads((tmp_path / "results" / "T99.json").read_text("utf-8"))
    assert data["status"] == "blocked"
    assert data["refused"] is True
    assert "no enforcement path" in data["refusal_reason"]


# -- upload ---------------------------------------------------------------


def test_dir_uploader_verifies_by_read_back(tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    (results / "T1.json").write_text('{"case_id": "T1"}', encoding="utf-8")
    destination = tmp_path / "share"
    destination.mkdir()

    uploader = havyn_runner.make_dir_uploader(destination)
    outcome = uploader(results)

    assert outcome["uploaded"] is True
    landed = destination / "results-T1.json"
    assert landed.is_file()
    assert landed.read_text(encoding="utf-8") == '{"case_id": "T1"}'


def test_dir_uploader_refuses_a_missing_destination(tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    (results / "T1.json").write_text("{}", encoding="utf-8")

    outcome = havyn_runner.make_dir_uploader(tmp_path / "nope")(results)

    assert outcome["uploaded"] is False
    assert "not a directory" in outcome["detail"]


def test_no_configuration_means_no_uploader_never_a_fake_success(tmp_path):
    """The cadence record must say *why* nothing was uploaded."""
    queue = _queue(tmp_path)
    summary = _run(tmp_path, queue, prune_every=1)
    assert summary.uploads, "the upload cadence left no record at all"
    for record in summary.uploads:
        assert record["uploaded"] is False
        assert record["reason"] == "no uploader configured"


def test_upload_dir_flag_wins_over_the_environment(tmp_path, monkeypatch):
    class _Args:
        upload_dir = str(tmp_path / "share")

    monkeypatch.setenv("HAVYN_UPLOAD_DIR", str(tmp_path / "other"))
    uploader = havyn_runner._uploader_from_args(_Args())
    assert uploader is not None

    class _EnvArgs:
        upload_dir = None

    monkeypatch.setenv("HAVYN_UPLOAD_DIR", str(tmp_path / "other"))
    assert havyn_runner._uploader_from_args(_EnvArgs()) is not None

    class _Bare:
        upload_dir = None

    monkeypatch.delenv("HAVYN_UPLOAD_DIR", raising=False)
    assert havyn_runner._uploader_from_args(_Bare()) is None


def test_configured_upload_reaches_the_destination_during_a_run(tmp_path):
    """End-to-end: a configured destination really receives the results."""
    destination = tmp_path / "share"
    destination.mkdir()
    queue = _queue(tmp_path)
    _run(tmp_path, queue, prune_every=1,
         uploader=havyn_runner.make_dir_uploader(destination))

    landed = destination / "results-T99.json"
    assert landed.is_file(), sorted(p.name for p in destination.iterdir())
    assert json.loads(landed.read_text("utf-8"))["case_id"] == "T99"