"""Overnight runner (S2.2): queue, clean-gate, results, crash-resume.

All offline on the NullAdapter (the data plane is unobservable there, so a
tiny spec honestly reports fail — what matters here is the runner
machinery: one JSON per case, resume by id, stop on a dirty workspace,
prune + upload cadence).
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
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


class _ProtectedOnlyAdapter(NullAdapter):
    """A canvas holding only a protected power device.

    ``clean()`` leaves it in place (by design) and reports it in ``remaining``
    — which is the full read-back, not just leftovers. The runner must read
    that as a clean workspace, not as an uncleanable one.
    """

    def list_device_names(self):
        return Observable.known(["Power Distribution Device0"], source="fake")


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


def test_runner_does_not_treat_protected_devices_as_dirt(tmp_path):
    """Protected devices always survive cleaning; that is not a dirty canvas.

    ``clean()`` reports them in ``remaining`` (the whole read-back). A runner
    that blocked on any ``remaining`` would refuse to run on every real
    workspace, which always holds the user's protected power devices.
    """
    spec = _write_spec(tmp_path)
    summary = havyn_runner.run_queue(
        _queue([{"id": "T", "spec": spec}]),
        _ProtectedOnlyAdapter,
        tmp_path / "results",
        tmp_path / "evidence",
    )

    result = json.loads((tmp_path / "results" / "T.json").read_text(
        encoding="utf-8"))
    assert result["status"] != "blocked", result
    assert not summary.stopped, summary.to_dict()
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


def test_runner_runs_as_a_script_from_the_repo_root(tmp_path):
    """The nightly .ps1 invokes ``python scripts/havyn_runner.py``.

    Run that way, ``sys.path[0]`` is ``scripts/`` -- not the repo root -- so the
    sibling import ``scripts.pt_clean_workspace`` used to raise
    ModuleNotFoundError before the first build. This drives the real entry point
    as a subprocess (the only faithful way to reproduce a launch-time sys.path)
    and asserts the case actually produced a result.
    """
    repo = Path(havyn_runner.__file__).resolve().parents[1]
    spec = _write_spec(tmp_path)
    queue = tmp_path / "queue.json"
    queue.write_text(
        json.dumps({"cases": [{"id": "T", "spec": spec}]}), encoding="utf-8"
    )

    proc = subprocess.run(
        [
            sys.executable, str(repo / "scripts" / "havyn_runner.py"),
            str(queue),
            "--adapter", "null",
            "--results-dir", str(tmp_path / "results"),
            "--evidence-dir", str(tmp_path / "evidence"),
        ],
        cwd=repo,
        capture_output=True,
        text=True,
    )

    assert "ModuleNotFoundError" not in proc.stderr, proc.stderr
    assert "No module named 'scripts'" not in proc.stderr, proc.stderr
    assert (tmp_path / "results" / "T.json").exists(), (
        f"runner did not produce a result\nstdout:\n{proc.stdout}\n"
        f"stderr:\n{proc.stderr}"
    )


def test_a_cleaning_crash_is_a_case_error_not_a_dead_queue(tmp_path, monkeypatch):
    """One case's fault must never abandon the rest of the queue.

    The S1.19 exit run died inside run06 with results for run01-run05 on disk
    and nothing for run06: whatever escaped ``run_case`` escaped ``run_queue``
    too and took the process with it, so a ten-case gate could never finish.
    ``run_case`` claims "never raises for case-level faults"; this pins it.
    """
    spec = _write_spec(tmp_path)
    from scripts import pt_clean_workspace

    def _boom(*args, **kwargs):
        raise RuntimeError("channel died mid-clean")

    monkeypatch.setattr(pt_clean_workspace, "clean", _boom)

    summary = havyn_runner.run_queue(
        _queue([{"id": "T1", "spec": spec}, {"id": "T2", "spec": spec}]),
        NullAdapter,
        tmp_path / "results",
        tmp_path / "evidence",
    )

    assert summary.ran == 2, (
        "the queue stopped early: a case-level fault escaped run_case"
    )
    for case_id in ("T1", "T2"):
        result = json.loads(
            (tmp_path / "results" / f"{case_id}.json").read_text(encoding="utf-8")
        )
        assert result["status"] == "error", result
        assert "channel died mid-clean" in (result["detail"] or ""), result
    assert (tmp_path / "results" / "summary.json").exists()


def test_a_crashing_adapter_factory_is_a_case_error_not_a_dead_queue(tmp_path):
    """The factory runs per case, so its failures are per case too."""
    spec = _write_spec(tmp_path)

    def _factory():
        raise RuntimeError("the mailbox directory vanished")

    summary = havyn_runner.run_queue(
        _queue([{"id": "A", "spec": spec}, {"id": "B", "spec": spec}]),
        _factory,
        tmp_path / "results",
        tmp_path / "evidence",
    )

    assert summary.ran == 2, summary.to_dict()
    for case_id in ("A", "B"):
        result = json.loads(
            (tmp_path / "results" / f"{case_id}.json").read_text(encoding="utf-8")
        )
        assert result["status"] == "error", result
        assert "the mailbox directory vanished" in (result["detail"] or ""), result


def test_a_crash_writes_a_result_so_a_restart_resumes_past_it(tmp_path, monkeypatch):
    """A dead case must still leave a result, or restart redoes the work."""
    spec = _write_spec(tmp_path)
    from scripts import pt_clean_workspace

    calls = {"n": 0}

    def _boom(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient transport fault")
        return _real_clean(*args, **kwargs)

    _real_clean = pt_clean_workspace.clean
    monkeypatch.setattr(pt_clean_workspace, "clean", _boom)

    cases = _queue([{"id": "T1", "spec": spec}, {"id": "T2", "spec": spec}])
    havyn_runner.run_queue(cases, NullAdapter, tmp_path / "results",
                           tmp_path / "evidence")

    prior = json.loads((tmp_path / "results" / "T1.json").read_text(encoding="utf-8"))
    assert prior["status"] == "error"

    # A restart must treat that terminal status as done, not rebuild it.
    summary = havyn_runner.run_queue(cases, NullAdapter, tmp_path / "results",
                                     tmp_path / "evidence")
    assert summary.resumed == 2, summary.to_dict()
    assert summary.ran == 0
