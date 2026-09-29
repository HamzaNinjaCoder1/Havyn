"""Exit codes for the one-shot builder (S1.3, F3).

The bug this pins: `build_havyn.py` returned 0 as soon as the build *ran*,
so `OVERALL: NOT SUCCESS` still looked green to scripts and CI. Each code in
the contract now has a test that fails on the old behaviour:

- NOT SUCCESS -> 2   (old: 0)
- preflight   -> 3   (old: 1, shared with every other error)
- unreachable -> 4   (old: 3, which now means preflight)
"""

from __future__ import annotations

import json

import pytest

import build_havyn
from network_autopilot.core.build_errors import PreflightBlocked
from network_autopilot.core.network_spec import NetworkSpec
from network_autopilot.core.observability import Observable
from network_autopilot.core.safety import MutationBudget, SafetyGate, SafetyMode

MINIMAL_SPEC = {
    "name": "exit-code-lab",
    "devices": [
        {"id": "r1", "role": "router"},
        {"id": "p1", "role": "pc"},
    ],
    "links": [
        {
            "id": "l1",
            "a": {"device": "r1", "interface_hint": "GigabitEthernet0/1"},
            "b": {"device": "p1", "interface_hint": "FastEthernet0"},
        }
    ],
    "subnets": [{"id": "net1", "cidr": "192.168.1.0/24"}],
    "ip_assignments": [
        {
            "device": "r1",
            "subnet": "net1",
            "ip": "192.168.1.1",
            "interface_hint": "GigabitEthernet0/1",
        },
        {"device": "p1", "subnet": "net1"},
    ],
}


@pytest.fixture
def spec_file(tmp_path):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(MINIMAL_SPEC), encoding="utf-8")
    return str(path)


class _Probe:
    def __init__(self, reachable: bool) -> None:
        self.reachable = reachable
        self.detail = Observable.known("fake channel", source="test")


def _fake_adapter(reachable: bool):
    class _Adapter:
        def __init__(self, **kwargs) -> None:
            pass

        def ping(self, timeout: float = 5.0) -> _Probe:
            return _Probe(reachable)

    return _Adapter


class _Report:
    """Just the surface build_havyn reads: verdict properties + human_text."""

    def __init__(self, *, success: bool, planned_only: bool = False) -> None:
        self.success = success
        self.planned_only = planned_only

    def human_text(self) -> str:
        if self.planned_only:
            return "OVERALL:        PLAN OK (DRY_RUN - nothing was executed)"
        return f"OVERALL:        {'SUCCESS' if self.success else 'NOT SUCCESS'}"


def _fake_autopilot(report, error=None):
    class _Autopilot:
        def __init__(self, adapter, gate, evidence, keep_evidence=20) -> None:
            pass

        def build(self, spec, *, resume_from=None):
            return report, error

    return _Autopilot


def test_missing_spec_exits_two(tmp_path, capsys):
    """A missing spec is bad usage; argparse shares its code 2 with NOT SUCCESS."""
    assert build_havyn.main([str(tmp_path / "nope.json")]) == 2


def test_pt_unreachable_exits_four(monkeypatch, spec_file):
    monkeypatch.setattr(build_havyn, "BridgeAdapter", _fake_adapter(False))
    assert build_havyn.main([spec_file]) == 4, "unreachable PT is its own code"


def test_preflight_block_exits_three(monkeypatch, spec_file):
    monkeypatch.setattr(build_havyn, "BridgeAdapter", _fake_adapter(True))
    monkeypatch.setattr(
        build_havyn,
        "Autopilot",
        _fake_autopilot(None, PreflightBlocked("preflight blocked execution: collision: r1")),
    )
    assert build_havyn.main([spec_file]) == 3, (
        "the safety gate refusing is not the same as a crash"
    )


def test_other_build_error_exits_one(monkeypatch, spec_file):
    """An untyped error is an engine-side failure: code 1, never a refusal."""
    monkeypatch.setattr(build_havyn, "BridgeAdapter", _fake_adapter(True))
    monkeypatch.setattr(
        build_havyn,
        "Autopilot",
        _fake_autopilot(None, "internal failure: TypeError: 'NoneType'"),
    )
    assert build_havyn.main([spec_file]) == 1


def test_not_success_verdict_exits_two_never_zero(monkeypatch, spec_file):
    """The S1.3 bug: a completed build with a failing verdict used to exit 0."""
    monkeypatch.setattr(build_havyn, "BridgeAdapter", _fake_adapter(True))
    monkeypatch.setattr(
        build_havyn, "Autopilot", _fake_autopilot(_Report(success=False))
    )
    assert build_havyn.main([spec_file]) == 2, (
        "NOT SUCCESS must never exit 0"
    )


def test_success_verdict_exits_zero(monkeypatch, spec_file):
    monkeypatch.setattr(build_havyn, "BridgeAdapter", _fake_adapter(True))
    monkeypatch.setattr(
        build_havyn, "Autopilot", _fake_autopilot(_Report(success=True))
    )
    assert build_havyn.main([spec_file]) == 0


def test_dry_run_plan_ok_exits_zero(monkeypatch, spec_file):
    """PLAN OK is neither SUCCESS nor a failure: the run did what was asked."""
    monkeypatch.setattr(build_havyn, "BridgeAdapter", _fake_adapter(True))
    monkeypatch.setattr(
        build_havyn,
        "Autopilot",
        _fake_autopilot(_Report(success=False, planned_only=True)),
    )
    assert build_havyn.main([spec_file]) == 0


def test_help_documents_every_exit_code(capsys):
    with pytest.raises(SystemExit) as exc:
        build_havyn.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "exit codes:" in out
    for fragment in (
        "0  SUCCESS",
        "1  unexpected build failure",
        "2  NOT SUCCESS",
        "3  refused before start",
        "4  Packet Tracer unreachable",
    ):
        assert fragment in out, fragment


# -- the orchestrator side: typed errors, single prefix ----------------------


def test_preflight_block_is_returned_typed_without_double_prefix(
    monkeypatch, tmp_path
):
    """build_havyn can only pick exit code 3 if the error says so by type.

    The orchestrator wraps *every* net_build error as a preflight block (the
    lint path arrives bare), and net_build's own message already carries the
    prefix - so the typed result must not double it.
    """
    import network_autopilot.compiler as compiler_mod
    from network_autopilot.evidence.store import EvidenceStore
    from network_autopilot.orchestrator import Autopilot
    from network_autopilot.adapter.null_adapter import NullAdapter

    def fake_net_build(spec, plan, *args, **kwargs):
        return None, "preflight blocked execution: collision: r1"

    monkeypatch.setattr(compiler_mod, "net_build", fake_net_build)
    autopilot = Autopilot(
        NullAdapter(),
        SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=100)),
        EvidenceStore(root=tmp_path / "evidence"),
    )
    report, error = autopilot.build(NetworkSpec.model_validate(MINIMAL_SPEC))

    assert report is None
    assert isinstance(error, PreflightBlocked)
    assert isinstance(error, str), "typed errors stay strings for every caller"
    assert error.startswith("preflight blocked execution: ")
    assert error.count("preflight blocked execution") == 1


def test_lint_problems_come_back_as_preflight_blocks(monkeypatch, tmp_path):
    """The lint path arrives bare from net_build; the orchestrator still has
    to give it the preflight type - that is what the CLI's exit 3 keys on."""
    import network_autopilot.compiler as compiler_mod
    from network_autopilot.evidence.store import EvidenceStore
    from network_autopilot.orchestrator import Autopilot
    from network_autopilot.adapter.null_adapter import NullAdapter

    def fake_net_build(spec, plan, *args, **kwargs):
        return None, "A-001: flow test targets an undeclared device"

    monkeypatch.setattr(compiler_mod, "net_build", fake_net_build)
    autopilot = Autopilot(
        NullAdapter(),
        SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=100)),
        EvidenceStore(root=tmp_path / "evidence"),
    )
    _report, error = autopilot.build(NetworkSpec.model_validate(MINIMAL_SPEC))

    assert isinstance(error, PreflightBlocked)
    assert str(error).startswith("preflight blocked execution: ")


def test_compiler_refusal_is_typed_as_a_preflight_block(monkeypatch, tmp_path):
    """A port-preflight collision raises CompileError out of net_build and
    used to exit 1. It stops the build before any mutation, so it is a
    refusal (code 3), not a crash - the live probe that found this was
    `build_havyn` on a two-links-one-port spec."""
    import network_autopilot.compiler as compiler_mod
    from network_autopilot.adapter.null_adapter import NullAdapter
    from network_autopilot.compiler.compiler import CompileError
    from network_autopilot.evidence.store import EvidenceStore
    from network_autopilot.orchestrator import Autopilot

    def fake_net_build(spec, plan, *args, **kwargs):
        raise CompileError(
            "port preflight failed: link l2 r1:GigabitEthernet0/1 - "
            "already_used: interface is reserved by another link"
        )

    monkeypatch.setattr(compiler_mod, "net_build", fake_net_build)
    autopilot = Autopilot(
        NullAdapter(),
        SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=100)),
        EvidenceStore(root=tmp_path / "evidence"),
    )
    _report, error = autopilot.build(NetworkSpec.model_validate(MINIMAL_SPEC))

    assert isinstance(error, PreflightBlocked)
    assert str(error).startswith("compilation refused: ")


def test_unexpected_compiler_crash_stays_a_plain_error(monkeypatch, tmp_path):
    """Only *named* refusals get code 3. A RuntimeError escaping the compiler
    is a bug and must keep its untyped message (exit 1), or CI could never
    tell a bad spec from a broken engine."""
    import network_autopilot.compiler as compiler_mod
    from network_autopilot.adapter.null_adapter import NullAdapter
    from network_autopilot.evidence.store import EvidenceStore
    from network_autopilot.orchestrator import Autopilot

    def fake_net_build(spec, plan, *args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(compiler_mod, "net_build", fake_net_build)
    autopilot = Autopilot(
        NullAdapter(),
        SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=100)),
        EvidenceStore(root=tmp_path / "evidence"),
    )
    _report, error = autopilot.build(NetworkSpec.model_validate(MINIMAL_SPEC))

    assert error == "compilation refused: boom"
    assert not isinstance(error, PreflightBlocked)


def test_addressing_refusal_is_typed_as_a_preflight_block(monkeypatch, tmp_path):
    """PlanningFailure is IPAM refusing the spec by name - also pre-mutation,
    also code 3. An unexpected exception from the planner stays plain."""
    import network_autopilot.orchestrator as orch
    from network_autopilot.adapter.null_adapter import NullAdapter
    from network_autopilot.evidence.store import EvidenceStore
    from network_autopilot.ipam.planner import PlanningFailure
    from network_autopilot.orchestrator import Autopilot

    def fake_plan(spec):
        raise PlanningFailure("net1", "invalid cidr", "not an IPv4 prefix")

    monkeypatch.setattr(orch, "plan_addressing", fake_plan)
    autopilot = Autopilot(
        NullAdapter(),
        SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=100)),
        EvidenceStore(root=tmp_path / "evidence"),
    )
    _report, error = autopilot.build(NetworkSpec.model_validate(MINIMAL_SPEC))

    assert isinstance(error, PreflightBlocked)
    assert str(error).startswith("addressing failed: ")


def test_resume_flag_reaches_the_build(monkeypatch, spec_file):
    """--resume <build-id> must reach Autopilot.build as resume_from (S1.14):
    only the remainder runs, and the flag is what selects it."""
    seen: dict = {}

    class _Autopilot:
        def __init__(self, adapter, gate, evidence, keep_evidence=20) -> None:
            pass

        def build(self, spec, *, resume_from=None):
            seen["resume_from"] = resume_from
            return _Report(success=True), None

    monkeypatch.setattr(build_havyn, "BridgeAdapter", _fake_adapter(True))
    monkeypatch.setattr(build_havyn, "Autopilot", _Autopilot)
    assert (
        build_havyn.main([spec_file, "autonomous", "--resume", "build-prior"])
        == 0
    )
    assert seen["resume_from"] == "build-prior"
