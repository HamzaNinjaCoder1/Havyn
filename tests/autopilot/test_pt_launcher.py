"""On-demand Packet Tracer launching — the engine must never be blocked by a
closed GUI.

The Script Engine only heartbeats while PT runs. Before pt_launcher, a closed PT
meant the live channel was simply unavailable. Now `ensure_packet_tracer` spawns
the documented executable and waits (bounded) for the heartbeat, so an agent can
open PT itself.
"""

from __future__ import annotations

import sys
import time

import pytest

from src.network_autopilot.adapter.packet_tracer import pt_launcher
from src.network_autopilot.adapter.packet_tracer.pt_launcher import (
    LaunchResult,
    ensure_packet_tracer,
    find_packet_tracer,
)


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch):
    # PT_EXE_PATH from the ambient machine must not leak into these scenarios.
    monkeypatch.delenv("PT_EXE_PATH", raising=False)


#: CI runners do not have Packet Tracer. Discovery and the real-GUI tests
#: only mean something on a machine that does.
_PT_INSTALLED = find_packet_tracer() is not None
_NEEDS_PT = pytest.mark.skipif(
    not _PT_INSTALLED, reason="Packet Tracer is not installed on this machine"
)


def _fake_exe(tmp_path):
    """A path find_packet_tracer can return without a real install."""
    exe = tmp_path / "PacketTracer.exe"
    exe.write_text("stub", encoding="utf-8")
    return exe


# -- discovery ----------------------------------------------------------------


@_NEEDS_PT
def test_find_packet_tracer_locates_real_install():
    """This machine has PT installed, so discovery must actually find it."""
    exe = find_packet_tracer()
    assert exe is not None
    assert exe.is_file()
    assert exe.name.lower() == "packettracer.exe"


def test_find_packet_tracer_respects_env_override(tmp_path, monkeypatch):
    fake = tmp_path / "PacketTracer.exe"
    fake.write_text("stub", encoding="utf-8")
    monkeypatch.setenv("PT_EXE_PATH", str(fake))
    assert find_packet_tracer() == fake


@_NEEDS_PT
def test_find_packet_tracer_ignores_stale_env_override(tmp_path, monkeypatch):
    """A bogus PT_EXE_PATH must not hide a real install."""
    monkeypatch.setenv("PT_EXE_PATH", str(tmp_path / "does-not-exist.exe"))
    exe = find_packet_tracer()
    # Falls through to the real install on this machine rather than returning None.
    assert exe is not None and exe.is_file()


def test_find_packet_tracer_returns_none_when_missing(monkeypatch):
    monkeypatch.setattr(pt_launcher, "_from_registry", lambda: None)
    monkeypatch.setattr(pt_launcher, "_from_glob", lambda: None)
    assert find_packet_tracer() is None


def test_discovery_survives_a_raising_registry(monkeypatch):
    """A corrupt registry key must not break discovery (falls back to glob)."""
    monkeypatch.setattr(pt_launcher, "_from_registry", lambda: (_ for _ in ()).throw(OSError(13)))
    monkeypatch.setattr(pt_launcher, "_from_glob", lambda: None)
    assert find_packet_tracer() is None  # no exception propagated


# -- ensure_packet_tracer -----------------------------------------------------


class _Heartbeat:
    """Flip the heartbeat after N polls so the launch wait is deterministic."""

    def __init__(self, fresh_after: int | None = None):
        self.fresh_after = fresh_after
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        if self.fresh_after is None:
            return False
        return self.calls >= self.fresh_after


def test_ensure_is_idempotent_when_already_alive():
    """A fresh heartbeat must never spawn a second GUI."""
    spawns: list = []

    result = ensure_packet_tracer(
        alive=lambda: True, spawn=True, spawner=lambda exe: spawns.append(exe),
    )

    assert result.status == LaunchResult.ALIVE
    assert result.usable and not result.spawned
    assert spawns == []


def test_ensure_spawns_and_waits_for_heartbeat(tmp_path, monkeypatch):
    """Not alive -> spawn once -> heartbeat goes fresh -> LAUNCHED."""
    hb = _Heartbeat(fresh_after=2)
    spawns: list = []
    monkeypatch.setattr(pt_launcher, "find_packet_tracer",
                        lambda: _fake_exe(tmp_path))

    result = ensure_packet_tracer(
        alive=hb, spawn=True, timeout=30.0, poll=0.001,
        spawner=lambda exe: spawns.append(exe),
    )

    assert result.status == LaunchResult.LAUNCHED
    assert result.usable and result.spawned
    assert len(spawns) == 1
    assert hb.calls >= 2


def test_ensure_bounded_timeout_never_hangs(tmp_path, monkeypatch):
    """Heartbeat never freshens: the deadline must be honoured, not ignored."""
    monkeypatch.setattr(pt_launcher, "find_packet_tracer",
                        lambda: _fake_exe(tmp_path))
    started = time.time()
    result = ensure_packet_tracer(
        alive=_Heartbeat(), spawn=True, timeout=1.0, poll=0.05,
        spawner=lambda exe: None,
    )
    elapsed = time.time() - started

    assert result.status == LaunchResult.TIMEOUT
    assert not result.usable and result.spawned
    # Returns roughly on time instead of waiting for the default 120s.
    assert elapsed < 5.0


def test_ensure_spawn_failure_is_reported_not_raised(tmp_path, monkeypatch):
    def boom(_exe):
        raise OSError(5, "Access is denied")

    monkeypatch.setattr(pt_launcher, "find_packet_tracer",
                        lambda: _fake_exe(tmp_path))
    result = ensure_packet_tracer(
        alive=lambda: False, spawn=True, spawner=boom,
    )

    assert result.status == LaunchResult.SPAWN_FAILED
    assert not result.usable and not result.spawned
    assert "Access is denied" in result.detail


def test_ensure_disabled_when_spawn_false():
    """Observe-only callers must never start a GUI."""
    result = ensure_packet_tracer(alive=lambda: False, spawn=False)

    assert result.status == LaunchResult.DISABLED
    assert not result.usable and not result.spawned


def test_ensure_missing_when_no_exe(monkeypatch):
    monkeypatch.setattr(pt_launcher, "find_packet_tracer", lambda: None)
    result = ensure_packet_tracer(alive=lambda: False, spawn=True)

    assert result.status == LaunchResult.MISSING
    assert not result.usable and not result.spawned
    assert result.exe is None


def test_ensure_uses_supplied_exe_without_search(monkeypatch):
    """An explicit exe path is used verbatim; discovery is skipped."""
    searched: list = []
    monkeypatch.setattr(pt_launcher, "find_packet_tracer",
                        lambda: searched.append(1) or None)

    result = ensure_packet_tracer(
        alive=_Heartbeat(fresh_after=2), exe="/opt/pt/PacketTracer.exe",
        timeout=5.0, poll=0.001, spawner=lambda exe: None,
    )

    assert result.status == LaunchResult.LAUNCHED
    assert result.exe == "/opt/pt/PacketTracer.exe"
    assert searched == []  # discovery never ran


# -- BridgeAdapter integration ----------------------------------------------

# The adapter under test must never touch the real machine: an empty temp
# mailbox gives it no heartbeat, and port 59999 hosts nothing.
_TEST_PORT = 59999


def _isolated_adapter(tmp_path, auto_launch=False):
    from src.network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter
    from src.network_autopilot.adapter.packet_tracer.bridge_adapter import FileBridge

    mailbox = tmp_path / "mailbox"
    mailbox.mkdir()
    return BridgeAdapter(
        port=_TEST_PORT, token="test-token", host_bridge=False,
        file_bridge=FileBridge(directory=mailbox), auto_launch=auto_launch,
    )


def test_auto_launch_opens_pt_when_no_channel(tmp_path, monkeypatch):
    """No live channel + auto_launch -> the adapter opens PT itself."""
    import src.network_autopilot.adapter.packet_tracer.bridge_adapter as ba

    adapter = _isolated_adapter(tmp_path, auto_launch=True)
    launches: list = []

    def fake_ensure(*, alive, timeout, **_kw):
        launches.append(alive)
        # Simulate PT coming up: the mailbox heartbeat goes fresh, which is
        # exactly what a real launch does to alive.txt.
        (adapter._file_bridge.dir / "alive.txt").write_text("up", encoding="utf-8")
        return LaunchResult(LaunchResult.LAUNCHED, exe="C:/PT/PacketTracer.exe")

    monkeypatch.setattr(ba, "ensure_packet_tracer", fake_ensure)

    # Before: nothing is live.
    assert adapter._pick_channel() == ""
    answer = adapter.send_expression("'x'", wait=0.2)

    # The launch was attempted (the precondition the agent could not satisfy
    # on its own) and afterwards the file channel is usable.
    assert len(launches) == 1
    assert adapter.last_launch.status == LaunchResult.LAUNCHED
    assert adapter._pick_channel() == "file"
    # No real PT drains the temp mailbox, so the send stays UNKNOWN — but the
    # reason is "no answer", never "no live channel".
    assert not answer.is_known
    assert "no live channel" not in (answer.detail or "")


def test_auto_launch_off_by_default_never_spawns(tmp_path, monkeypatch):
    """Default (auto_launch=False): a closed PT is reported, never worked around."""
    import src.network_autopilot.adapter.packet_tracer.bridge_adapter as ba

    adapter = _isolated_adapter(tmp_path)
    monkeypatch.setattr(ba, "ensure_packet_tracer",
                        lambda *a, **k: pytest.fail("launch must not happen"))

    answer = adapter.send_expression("'x'", wait=0.2)

    assert not answer.is_known
    assert "no live channel" in (answer.detail or "")
    assert adapter.last_launch is None


def test_launch_method_is_idempotent_when_alive(tmp_path, monkeypatch):
    """launch() on an already-live PT reports ALIVE and never spawns."""
    adapter = _isolated_adapter(tmp_path)
    (adapter._file_bridge.dir / "alive.txt").write_text("up", encoding="utf-8")
    # Prove the spawn path is unreachable: a fresh heartbeat must short-circuit
    # inside ensure_packet_tracer before any process is started.
    monkeypatch.setattr(pt_launcher, "_spawn_default",
                        lambda exe: pytest.fail("alive PT must not spawn"))

    result = adapter.launch()

    assert result.status == LaunchResult.ALIVE
    assert result.usable and not result.spawned
    assert adapter._pick_channel() == "file"


def test_launch_reports_missing_install(tmp_path, monkeypatch):
    """A launch with no install on disk is a MISSING receipt, not an exception."""
    adapter = _isolated_adapter(tmp_path)
    monkeypatch.setattr(pt_launcher, "find_packet_tracer", lambda: None)

    result = adapter.launch(timeout=0.2)

    assert result.status == LaunchResult.MISSING
    assert not result.usable

