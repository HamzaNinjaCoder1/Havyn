"""Cleanup by the ids the spec actually used (#40).

A build through `net_build()` creates raw spec ids (`r1`, `pc1`), while the
harness namespaced tests create `q<short>-r1`. A cleanup that only knows one
of the two forms deletes nothing, and the device outlives its test in the
user's workspace.
"""

from __future__ import annotations

import pytest
from _pytest.outcomes import Failed

from tests.autopilot import _live_harness as live
from tests.autopilot import conftest as live_conftest


def test_both_naming_conventions_are_covered():
    assert live.owned_device_names(["r1", "pc1"], "q123456") == [
        "pc1", "q123456pc1", "q123456r1", "r1",
    ]
    assert live.owned_device_names(["r1", "pc1"]) == ["pc1", "r1"]
    assert live.owned_device_names([]) == []


def test_teardown_deletes_the_raw_id_a_non_namespaced_build_created(monkeypatch):
    """The bug: cleanup only ever asked for `q<short>-r1`, so a build that
    created plain `r1` was never removed."""
    handed: list[list[str]] = []

    def fake_delete(names, timeout=240.0):
        handed.append(list(names))
        return {n: "success" for n in names}

    monkeypatch.setattr(live, "delete_devices", fake_delete)
    live.delete_spec_devices(["r1"], prefix="q123456")
    assert handed == [["q123456r1", "r1"]], handed


def test_leak_check_reports_only_names_that_are_really_still_there(monkeypatch):
    monkeypatch.setattr(
        live, "_live_names", lambda: ["q123456r1", "r1", "someone-elses-pc"]
    )
    assert live.leaked_spec_devices(["r1"], "q123456") == ["q123456r1", "r1"]
    monkeypatch.setattr(live, "_live_names", lambda: ["someone-elses-pc"])
    assert live.leaked_spec_devices(["r1"], "q123456") == []


# -- S1.15 / F12: --require-live -------------------------------------------


class _FakeMark:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeNode:
    def __init__(self, nodeid: str, marks=(), parent=None) -> None:
        self.nodeid = nodeid
        self.own_markers = list(marks)
        self.parent = parent


class _FakeConfig:
    def __init__(self, require_live: bool) -> None:
        self._flag = require_live

    def getoption(self, name: str, default=False):
        return self._flag if name == "require_live" else default


class _FakeRequest:
    def __init__(self, node, config) -> None:
        self.node = node
        self.config = config


def _live_item():
    module = _FakeNode(
        "tests/autopilot/test_live_policy.py",
        marks=(_FakeMark("skipif"),),
    )
    return _FakeNode(
        "tests/autopilot/test_live_policy.py::test_x",
        marks=(_FakeMark("skipif"), _FakeMark("parametrize")),
        parent=module,
    )


def test_require_live_strips_unreachable_skips():
    """With the flag, skip/skipif marks come off live items at collection so
    the guard below fails loudly instead of silently skipping."""
    item = _live_item()
    live_conftest.pytest_collection_modifyitems([item], _FakeConfig(True))

    node: _FakeNode | None = item
    while node is not None:
        assert [m.name for m in node.own_markers] == (
            [] if node is item.parent else ["parametrize"]
        ), [m.name for m in node.own_markers]
        node = node.parent


def test_without_the_flag_skips_are_kept():
    item = _live_item()
    offline = _FakeNode("tests/autopilot/test_scale.py::test_x")
    live_conftest.pytest_collection_modifyitems([item, offline], _FakeConfig(False))

    assert [m.name for m in item.own_markers] == ["skipif", "parametrize"]
    assert [m.name for m in item.parent.own_markers] == ["skipif"]


def test_require_live_guard_fails_fast_when_pt_is_dead(monkeypatch):
    """PT unreachable + flag = immediate failure, no test body runs."""
    monkeypatch.setattr(live, "live_is_reachable", lambda: False)
    request = _FakeRequest(_live_item(), _FakeConfig(True))
    with pytest.raises(Failed, match="--require-live"):
        live_conftest._guard(request)


def test_require_live_guard_passes_when_pt_is_up(monkeypatch):
    monkeypatch.setattr(live, "live_is_reachable", lambda: True)
    request = _FakeRequest(_live_item(), _FakeConfig(True))
    assert live_conftest._guard(request) is None


def test_require_live_guard_ignores_offline_tests(monkeypatch):
    """Offline tests never trip the guard, even with a dead PT + the flag."""
    monkeypatch.setattr(live, "live_is_reachable", lambda: False)
    offline = _FakeNode("tests/autopilot/test_scale.py::test_x")
    request = _FakeRequest(offline, _FakeConfig(True))
    assert live_conftest._guard(request) is None


def test_live_item_classification():
    assert live_conftest._is_live_item(
        _FakeNode("tests/autopilot/test_live_policy.py::test_x")
    )
    assert live_conftest._is_live_item(
        _FakeNode("tests/autopilot/test_scale_live.py::test_x")
    )
    assert not live_conftest._is_live_item(
        _FakeNode("tests/autopilot/test_scale.py::test_x")
    )
