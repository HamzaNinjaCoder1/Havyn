"""Overnight runner (S2.2): queue, clean-gate, results, crash-resume.

All offline on the NullAdapter (the data plane is unobservable there, so a
tiny spec honestly reports fail — what matters here is the runner
machinery: one JSON per case, resume by id, stop on a dirty workspace,
prune + upload cadence).
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

havyn_runner = importlib.import_module("scripts.havyn_runner")

from network_autopilot.adapter import NullAdapter
from network_autopilot.core.observability import Observable
from network_autopilot.core.receipts import OperationReceipt, OperationResult

TINY_SPEC = {
    "name": "runner-tiny",
    "devices": [
        {"id": "r1", "role": "router"},
        {"id": "p1", "role": "pc"},
    ],
    "links": [{"id": "l1", "a": {"device": "r1"}, "b": {"device": "p1"}}],
    "subnets": [{"id": "net1", "cidr": "192.168.200.0/24"}],
    "ip_assignments": [
        {"device": "r1", "subnet": "net1", "ip": "192.168.200.1"},
        {"device": "p1", "subnet": "net1"},
    ],
    "requirements": [{"id": "R-1", "statement": "p1 reaches r1"}],
    "acceptance_criteria": [{
        "id": "A-1",
        "requirement_ids": ["R-1"],
        "test": {"kind": "flow", "source": "p1", "destination": "r1",
                 "expect": "reachable"},
    }],
}


def _write_spec(tmp_path: Path, name: str = "tiny.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps(TINY_SPEC), encoding="utf-8")
    return str(path)


def _queue(cases) -> list[dict]:
    return list(cases)


class _DirtyAdapter(NullAdapter):
    """A workspace whose ghost device survives every delete."""

    def list_device_names(self):
        return Observable.known(["ghost"], source="fake")

    def device_exists(self, name):
        return Observable.known(True, source="fake")

    def delete_device(self, name, operation_id=None):
        return OperationReceipt(
            operation_id=operation_id or "op-del",
            operation="delete_device",
            requested={"name": name},
            result=OperationResult.FAILED,
            error="stuck, says the fake canvas",
        )

    def delete_devices_bulk(self, names):
        return {n: "still-present" for n in names}


def test_runner_writes_result_json_per_case(tmp_path):
    spec = _write_spec(tmp_path)
    summary = havyn_runner.run_queue(
        _queue([{"id": "T", "spec": spec}]),
        NullAdapter,
        tmp_path / "results",
        tmp_path / "evidence",
    )

    result = json.loads((tmp_path / "results" / "T.json").read_text(
        encoding="utf-8"))
    assert result["case_id"] == "T"
    assert result["status"] == "fail", result  # null cannot observe flows
    assert result["criteria"]["total"] >= 1
    assert result["build_id"], result
    assert result["finished_at"], result
    assert (tmp_path / "results" / "summary.json").exists()
    assert summary.ran == 1 and summary.failed == 1


def test_runner_skips_completed_cases(tmp_path):
    """Crash-resume: a finished case is never rebuilt."""
    spec = _write_spec(tmp_path)
    done = {"case_id": "T1", "spec": spec, "status": "pass",
            "criteria": {}, "elapsed_s": 1.0, "build_id": "build-old",
            "detail": None, "finished_at": "then"}
    results = tmp_path / "results"
    results.mkdir()
    before = results / "T1.json"
    before.write_text(json.dumps(done), encoding="utf-8")

    summary = havyn_runner.run_queue(
        _queue([{"id": "T1", "spec": spec}, {"id": "T2", "spec": spec}]),
        NullAdapter,
        results,
        tmp_path / "evidence",
    )

    assert json.loads(before.read_text(encoding="utf-8")) == done
    assert (results / "T2.json").exists()
    assert summary.resumed == 1 and summary.ran == 1


def test_runner_stops_when_workspace_cannot_be_cleaned(tmp_path):
    """A dirty canvas stops the run: never build on top of leftovers."""
    spec = _write_spec(tmp_path)
    summary = havyn_runner.run_queue(
        _queue([{"id": "T1", "spec": spec}, {"id": "T2", "spec": spec}]),
        _DirtyAdapter,
        tmp_path / "results",
        tmp_path / "evidence",
    )

    first = json.loads((tmp_path / "results" / "T1.json").read_text(
        encoding="utf-8"))
    assert first["status"] == "blocked", first
    assert "workspace" in (first["detail"] or "")
    assert not (tmp_path / "results" / "T2.json").exists()
    assert summary.stopped, summary.to_dict()
    assert summary.ran == 1


def test_failed_build_does_not_stop_the_queue(tmp_path):
    """One bad case is recorded; the queue marches on."""
    spec = _write_spec(tmp_path)
    summary = havyn_runner.run_queue(
        _queue([{"id": "BAD", "spec": str(tmp_path / "missing.json")},
                {"id": "T", "spec": spec}]),
        NullAdapter,
        tmp_path / "results",
        tmp_path / "evidence",
    )

    bad = json.loads((tmp_path / "results" / "BAD.json").read_text(
        encoding="utf-8"))
    assert bad["status"] == "error", bad
    assert (tmp_path / "results" / "T.json").exists()
    assert summary.ran == 2 and summary.failed == 2


def test_runner_prunes_and_uploads_on_cadence(tmp_path):
    """Every prune_every cases: evidence pruned, uploader called."""
    uploads: list[str] = []

    def fake_uploader(results_dir):
        uploads.append(str(results_dir))
        return {"uploaded": True, "detail": "fake remote"}

    spec = _write_spec(tmp_path)
    summary = havyn_runner.run_queue(
        _queue([{"id": "T1", "spec": spec}, {"id": "T2", "spec": spec}]),
        NullAdapter,
        tmp_path / "results",
        tmp_path / "evidence",
        keep_evidence=1,
        prune_every=1,
        uploader=fake_uploader,
    )

    assert len(uploads) == 2, uploads
    assert len(summary.uploads) == 2
    assert all(u["uploaded"] for u in summary.uploads)
    assert list((tmp_path / "evidence").glob("archive-*.zip")), (
        "pruning must archive the first case's evidence"
    )
