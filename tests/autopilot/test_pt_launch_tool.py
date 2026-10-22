"""MCP-level Packet Tracer lifecycle tools: pt_launch, pt_status, and the
auto_launch path on the bridge tools.

The agent-facing contract these protect: 'Packet Tracer must already be open'
was the last manual step. Now an agent can open PT itself, and every bridge
tool either proceeds against the live GUI or returns a receipt explaining why
it could not. auto_launch stays opt-in everywhere so a closed PT is never
silently worked around.
"""

from __future__ import annotations

import json

import pytest

from src.network_autopilot.adapter.base import AdapterInfo
from src.network_autopilot.adapter.packet_tracer.pt_launcher import (
    LaunchResult,
    find_packet_tracer,
)
from src.network_autopilot.core.observability import Observable
from src.network_autopilot.mcp import server
from src.network_autopilot.pt.mailbox import FileMailbox

#: The status/launch assertions below describe a machine with a running PT.
#: CI has neither the install nor the heartbeat, so they skip there.
_PT_READY = find_packet_tracer() is not None and FileMailbox().pt_alive()
_NEEDS_LIVE_PT = pytest.mark.skipif(
    not _PT_READY, reason="Packet Tracer is not installed/running on this machine"
)


class _StubState:
    def to_dict(self):
        return {"ok": True, "observed": "live workspace"}


class FakeBridge:
    """Minimal stand-in so the server tools never touch a real GUI."""

    def __init__(self, reachable=False, launch_result=None, *, auto_launch=False, **_kw):
        self._reachable = reachable
        self._launch_result = launch_result
        self.auto_launch = auto_launch
        self.launch_calls = 0

    def ping(self, timeout=5.0):
        return AdapterInfo("fake", "0", self._reachable,
                           Observable.known("up" if self._reachable else "down"))

    def launch(self, timeout=None):
        self.launch_calls += 1
        result = self._launch_result or LaunchResult(LaunchResult.LAUNCHED, exe="fake")
        if result.usable:
            self._reachable = True
        return result

    def observe_state(self, names=None):
        assert self._reachable, "observe must only run against a live channel"
        return _StubState()


# -- _live_or_blocked helper -------------------------------------------------


def test_helper_passes_through_when_reachable():
    bridge = FakeBridge(reachable=True)
    assert server._live_or_blocked(bridge, auto_launch=False) is None


def test_helper_blocks_when_unreachable_and_no_auto_launch():
    bridge = FakeBridge(reachable=False)
    out = server._live_or_blocked(bridge, auto_launch=False)

    assert out["status"] == "BLOCKED"
    assert bridge.launch_calls == 0, "must never open a GUI the caller did not ask for"


def test_helper_launches_then_proceeds():
    bridge = FakeBridge(reachable=False, launch_result=LaunchResult(LaunchResult.LAUNCHED))

    assert server._live_or_blocked(bridge, auto_launch=True) is None
    assert bridge.launch_calls == 1
    assert bridge.ping().reachable, "the launch made the channel live"


def test_helper_reports_a_failed_launch_as_a_receipt():
    bridge = FakeBridge(
        reachable=False,
        launch_result=LaunchResult(LaunchResult.TIMEOUT, exe="C:/PT/PacketTracer.exe",
                                   detail="heartbeat never freshened"),
    )
    out = server._live_or_blocked(bridge, auto_launch=True)

    assert out["status"] == "BLOCKED"
    assert out["launch"]["status"] == "timeout"
    assert out["launch"]["exe"] == "C:/PT/PacketTracer.exe"
    assert "heartbeat never freshened" in out["detail"]


# -- pt_status / pt_launch (real machine, read-only) -------------------------


@_NEEDS_LIVE_PT
def test_pt_status_reports_install_and_channel():
    """PT is installed and running on this machine; the tool must say so."""
    status = server.pt_status()

    assert set(status) >= {"installed", "exe", "process_running", "bridge_reachable", "detail"}
    assert status["installed"] is True
    assert status["exe"] is not None
    # detail is always populated so an agent can act on it, whatever the state.
    assert status["detail"]


@_NEEDS_LIVE_PT
def test_pt_launch_is_idempotent_and_never_spawns_a_live_pt(monkeypatch):
    """An already-heartbeating PT must be reported, not re-opened."""
    from src.network_autopilot.adapter.packet_tracer import pt_launcher

    monkeypatch.setattr(pt_launcher, "_spawn_default",
                        lambda exe: pytest.fail("live PT must never be spawned"))

    out = server.pt_launch(timeout_s=60.0)

    assert out["status"] == LaunchResult.ALIVE
    assert out["usable"] is True
    assert out["spawned"] is False
    assert isinstance(out["elapsed_s"], float)


# -- bridge tools honor auto_launch -----------------------------------------


@pytest.fixture
def fake_bridge(monkeypatch):
    """Swap the real BridgeAdapter for a fake across the whole server module.

    Returns a holder the test configures: set `launch_result` to make the
    auto-launch succeed, fail, or report a missing install.
    """
    import src.network_autopilot.adapter.packet_tracer.bridge_adapter as ba

    holder = {"launch_result": None, "bridge": None}

    def factory(*args, **kwargs):
        bridge = FakeBridge(reachable=False, launch_result=holder["launch_result"], **kwargs)
        holder["bridge"] = bridge
        return bridge

    monkeypatch.setattr(ba, "BridgeAdapter", factory)
    return holder


def test_net_observe_blocked_by_default_when_unreachable(fake_bridge):
    out = server.net_observe("[]", adapter="bridge")

    assert out["status"] == "BLOCKED"
    assert fake_bridge["bridge"].launch_calls == 0


def test_net_observe_auto_launch_proceeds_against_live_gui(fake_bridge):
    """auto_launch=True turns a BLOCKED observation into a live one."""
    fake_bridge["launch_result"] = LaunchResult(LaunchResult.LAUNCHED)

    out = server.net_observe("[]", adapter="bridge", auto_launch=True)

    assert fake_bridge["bridge"].launch_calls == 1
    assert out["ok"] is True


def test_net_observe_reports_launch_receipt_when_pt_still_down(fake_bridge):
    fake_bridge["launch_result"] = LaunchResult(LaunchResult.MISSING,
                                                detail="no install found")

    out = server.net_observe("[]", adapter="bridge", auto_launch=True)

    assert out["status"] == "BLOCKED"
    assert out["launch"]["status"] == "missing"
    assert fake_bridge["bridge"].launch_calls == 1
