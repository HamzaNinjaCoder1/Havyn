"""S1.4: the workspace collision check (blueprint F4).

"Same naam ke purane devices build ko bigaad dete hain." Before this check,
`Autopilot.build` would silently reuse whatever device already answered to a
spec device name - a leftover from another topology, another test run, or a
crashed build - and the report would claim that device was the spec's.

Blueprint contract pinned here:
  * intersection of PT device names and spec device names -> build refuses
    to start, naming every colliding device;
  * an unreadable workspace FAILS CLOSED - unknown is never "no collision"
    (AGENTS.md rule 7);
  * `--clean-workspace` runs the S0.4 cleanup first, then the build runs;
  * callers that never read a workspace (lint, net_preflight, repair's
    direct `compile_spec`) keep the old behaviour: no check, no new params.

Every test fails until the check is implemented - see the mutation note at
the bottom of test_preflight_blocks_on_a_name_already_in_the_workspace.
"""

from __future__ import annotations

import json

import pytest

import build_havyn
from network_autopilot.core.build_errors import PreflightBlocked
from network_autopilot.core.network_spec import NetworkSpec
from network_autopilot.core.observability import Observable
from network_autopilot.core.safety import MutationBudget, SafetyGate, SafetyMode
from network_autopilot.ipam import plan_addressing
from network_autopilot.orchestrator import Autopilot

MINIMAL_SPEC = {
    "name": "collision-lab",
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
    "subnets": [{"id": "net1", "cidr": "192.168.60.0/24"}],
    "ip_assignments": [
        {
            "device": "r1",
            "subnet": "net1",
            "ip": "192.168.60.1",
            "interface_hint": "GigabitEthernet0/1",
        },
        {"device": "p1", "subnet": "net1"},
    ],
}


def _spec() -> NetworkSpec:
    return NetworkSpec.model_validate(MINIMAL_SPEC)


def _autopilot(adapter):
    from pathlib import Path
    import tempfile

    from network_autopilot.evidence.store import EvidenceStore

    return Autopilot(
        adapter,
        SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=100)),
        EvidenceStore(root=Path(tempfile.mkdtemp(prefix="collision-evidence-"))),
    )


# -- preflight level ---------------------------------------------------------


def test_preflight_blocks_on_a_name_already_in_the_workspace(tmp_path):
    from network_autopilot.compiler.preflight import run_preflight

    spec = _spec()
    plan = plan_addressing(spec)
    existing = Observable.known(["r1", "some-other-box"], source="fake-pt")

    result = run_preflight(spec, plan, workspace_names=existing)

    assert result.can_execute is False, "a colliding name must block the build"
    collisions = [e for e in result.errors if "workspace collision" in e]
    assert collisions, result.errors
    assert "r1" in collisions[0], collisions[0]
    assert "some-other-box" not in collisions[0], "only spec names can collide"
    workspace_checks = [c for c in result.checks if c.category == "workspace"]
    assert workspace_checks and workspace_checks[0].status.value == "fail"
    assert "r1" in workspace_checks[0].evidence


def test_preflight_fails_closed_when_the_workspace_cannot_be_read():
    """Unknown is never PASS: with no read there is no intersection to show,
    so the build must stop rather than assume the canvas is empty."""
    from network_autopilot.compiler.preflight import run_preflight

    spec = _spec()
    plan = plan_addressing(spec)
    unreadable = Observable[list[str]].unknown("mailbox channel gone")

    result = run_preflight(spec, plan, workspace_names=unreadable)

    assert result.can_execute is False
    assert any("unreadable" in e for e in result.errors), result.errors


def test_preflight_without_a_workspace_read_keeps_the_old_behaviour():
    """lint, net_preflight and repair call preflight without a workspace;
    they must keep working exactly as before (no adapter, no new required
    params, no phantom collision)."""
    from network_autopilot.compiler.preflight import run_preflight

    spec = _spec()
    plan = plan_addressing(spec)

    result = run_preflight(spec, plan)

    assert result.can_execute is True, result.errors
    assert not [c for c in result.checks if c.category == "workspace"]


# -- build level -------------------------------------------------------------


def test_build_refuses_to_start_when_names_collide_and_names_the_devices():
    from network_autopilot.adapter.null_adapter import NullAdapter

    adapter = NullAdapter()
    adapter.create_device(name="r1", role="router")  # leftover from an old run
    autopilot = _autopilot(adapter)

    report, error = autopilot.build(_spec())

    assert report is None, "the build must not start"
    assert isinstance(error, PreflightBlocked), error
    assert "workspace collision" in str(error)
    assert "r1" in str(error), "the message must say WHICH names collide"
    assert "p1" not in adapter.list_device_names().value, (
        "nothing may be created once the build is refused"
    )


def test_after_cleaning_the_workspace_the_build_runs():
    """The blueprint's done criterion: collision stops the build, and the
    S0.4 cleanup unblocks it."""
    from scripts.pt_clean_workspace import clean
    from network_autopilot.adapter.null_adapter import NullAdapter

    adapter = NullAdapter()
    adapter.create_device(name="r1", role="router")
    autopilot = _autopilot(adapter)

    _report, error = autopilot.build(_spec())
    assert isinstance(error, PreflightBlocked)

    report = clean(adapter)
    assert report.failed == {} and report.read_failed is False
    assert adapter.list_device_names().value == []

    build, error = autopilot.build(_spec())
    assert error is None, error
    assert build is not None
    assert "p1" in adapter.list_device_names().value, "the build really ran"


def test_build_fails_closed_when_the_workspace_read_is_unknown():
    from network_autopilot.adapter.null_adapter import NullAdapter

    adapter = NullAdapter()
    adapter.list_device_names = lambda: Observable[list[str]].unknown(
        "channel down"
    )
    autopilot = _autopilot(adapter)

    report, error = autopilot.build(_spec())

    assert report is None
    assert isinstance(error, PreflightBlocked), error
    assert "unreadable" in str(error)


# -- build_havyn --clean-workspace -------------------------------------------


@pytest.fixture
def spec_file(tmp_path):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(MINIMAL_SPEC), encoding="utf-8")
    return str(path)


class _Report:
    success = True
    planned_only = False

    def human_text(self) -> str:
        return "OVERALL:        SUCCESS"


def test_clean_workspace_flag_wipes_the_workspace_before_building(
    monkeypatch, spec_file
):
    from network_autopilot.adapter.null_adapter import NullAdapter

    captured: dict = {}

    def make_adapter(**kwargs):
        adapter = NullAdapter()
        adapter.create_device(name="old-leftover", role="pc")
        captured["adapter"] = adapter
        return adapter

    class _Autopilot:
        def __init__(self, adapter, gate, evidence) -> None:
            captured["built_with"] = adapter

        def build(self, spec):
            return _Report(), None

    monkeypatch.setattr(build_havyn, "BridgeAdapter", make_adapter)
    monkeypatch.setattr(build_havyn, "Autopilot", _Autopilot)

    code = build_havyn.main([spec_file, "--clean-workspace"])

    assert code == 0
    adapter = captured["adapter"]
    assert adapter.list_device_names().value == [], (
        "the S0.4 cleanup must run before the build starts"
    )
    assert captured["built_with"] is adapter


def test_clean_workspace_exits_one_when_the_workspace_cannot_be_read(
    monkeypatch, spec_file
):
    """clean() refuses to report an unreadable workspace as clean; the CLI
    must propagate that instead of building blind."""
    from network_autopilot.adapter.null_adapter import NullAdapter

    captured: dict = {}

    def make_adapter(**kwargs):
        adapter = NullAdapter()
        adapter.list_device_names = lambda: Observable[list[str]].unknown(
            "no channel"
        )
        captured["adapter"] = adapter
        return adapter

    class _Autopilot:
        def __init__(self, adapter, gate, evidence) -> None:
            captured["build_called"] = True

        def build(self, spec):  # pragma: no cover - must never run
            raise AssertionError("the build must not start on an unreadable wipe")

    monkeypatch.setattr(build_havyn, "BridgeAdapter", make_adapter)
    monkeypatch.setattr(build_havyn, "Autopilot", _Autopilot)

    code = build_havyn.main([spec_file, "--clean-workspace"])

    assert code == 1
    assert "build_called" not in captured
