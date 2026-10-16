"""Channel probing: an absent HTTP bridge must not be re-paid on every command.

The channel probe consults the HTTP bridge first, and a refused loopback
connect measures 2.03 s on this machine (plus 0.36 s of getaddrinfo). The
positive channel cache lives only 4 s, so on a host where no bridge is ever
listening — the reference build's 435 sends were ALL on the file channel,
none on HTTP — the adapter paid ~2.4 s before most commands just to re-learn
that HTTP is absent. That was the single largest fixed cost of the 20-minute
reference build (build-dcca6bd4ab7f).

These tests pin the negative cache: the absence is remembered, the verdict is
unchanged, and a bridge that appears is still picked up.
"""

from __future__ import annotations

import time

from src.network_autopilot.adapter.packet_tracer.bridge_adapter import (
    BridgeAdapter,
    FileBridge,
)


def _fresh_mailbox(tmp_path):
    mailbox = tmp_path / "mailbox"
    mailbox.mkdir()
    # A fresh heartbeat is what makes the file channel a live alternative, so
    # the probe has somewhere to land when HTTP is ruled out.
    (mailbox / "alive.txt").write_text("fresh", encoding="utf-8")
    return mailbox


def _adapter(tmp_path, *, http_reachable: bool = False):
    """An adapter whose HTTP answer and probe count we control exactly."""
    mailbox = _fresh_mailbox(tmp_path)
    adapter = BridgeAdapter(
        host_bridge=False,
        token="test-token",
        file_bridge=FileBridge(directory=mailbox),
    )
    calls = {"n": 0}

    def counting(timeout: float = 5.0):
        calls["n"] += 1
        # Stand in for the measured outcomes without paying the real probe:
        # a refused connect costs 2 s, and a reachable bridge cannot exist in
        # a unit test at all.
        from src.network_autopilot.adapter.base import AdapterInfo
        from src.network_autopilot.core.observability import Observable

        if http_reachable:
            return AdapterInfo(
                adapter.name, adapter.version, True,
                Observable.known("http bridge authenticated"),
            )
        return AdapterInfo(
            adapter.name, adapter.version, False,
            Observable.known("http bridge not reachable: refused"),
        )

    adapter._http_ping = counting
    adapter.probe_calls = calls
    return adapter


def test_an_absent_http_bridge_is_probed_once_not_per_command(tmp_path):
    """N probes must cost ONE HTTP round trip, not N."""
    adapter = _adapter(tmp_path, http_reachable=False)

    for _ in range(20):
        assert adapter._probe_channels() == "file"

    assert adapter.probe_calls["n"] == 1, (
        f"re-probed the absent bridge {adapter.probe_calls['n']} times; "
        "each one costs a refused connect on the user's machine"
    )


def test_the_verdict_is_unchanged_by_the_caching(tmp_path):
    """Caching must not turn a dead bridge into a live channel."""
    adapter = _adapter(tmp_path, http_reachable=False)
    assert adapter._probe_channels() == "file"

    # Kill the file heartbeat: with both channels gone the answer is still "",
    # and the remembered HTTP absence must not be mistaken for a live HTTP.
    import os

    alive = adapter._file_bridge.dir / "alive.txt"
    old = alive.stat().st_mtime - 600
    os.utime(alive, (old, old))

    adapter._channel = None
    adapter._channel_cached_at = None
    assert adapter._probe_channels() == ""


def test_a_bridge_that_appears_is_picked_up_after_the_window(tmp_path):
    """Remembering an absence must not mean ignoring the bridge forever."""
    adapter = _adapter(tmp_path, http_reachable=False)
    assert adapter._probe_channels() == "file"
    before = adapter.probe_calls["n"]

    # The bridge comes up. Within the window the memory is trusted...
    adapter._http_ping = lambda timeout=5.0: _reachable(adapter)
    assert adapter._probe_channels() == "file"
    assert adapter.probe_calls["n"] == before, "memory was still in force"

    # ...and once it lapses the probe runs again and finds the bridge.
    adapter._http_absent_until = 0.0
    assert adapter._probe_channels() == "http"


def test_a_failed_send_forgets_the_remembered_absence(tmp_path):
    """A send failure is exactly when the other channel deserves a re-look."""
    adapter = _adapter(tmp_path, http_reachable=False)
    assert adapter._probe_channels() == "file"
    assert adapter._http_absent_until > time.time()

    adapter._invalidate_channel()

    assert adapter._http_absent_until == 0.0
    assert adapter._channel is None and adapter._channel_cached_at is None


def test_http_still_wins_when_it_actually_answers(tmp_path):
    """The upstream preference order is preserved: HTTP first, file second."""
    adapter = _adapter(tmp_path, http_reachable=True)
    assert adapter._probe_channels() == "http"
    assert adapter._http_absent_until == 0.0, "a live bridge must never be cached as absent"


def _reachable(adapter):
    from src.network_autopilot.adapter.base import AdapterInfo
    from src.network_autopilot.core.observability import Observable

    return AdapterInfo(
        adapter.name, adapter.version, True,
        Observable.known("http bridge authenticated"),
    )
