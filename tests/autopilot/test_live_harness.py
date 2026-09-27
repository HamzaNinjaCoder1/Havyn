"""Cleanup by the ids the spec actually used (#40).

A build through `net_build()` creates raw spec ids (`r1`, `pc1`), while the
harness namespaced tests create `q<short>-r1`. A cleanup that only knows one
of the two forms deletes nothing, and the device outlives its test in the
user's workspace.
"""

from __future__ import annotations

from tests.autopilot import _live_harness as live


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
