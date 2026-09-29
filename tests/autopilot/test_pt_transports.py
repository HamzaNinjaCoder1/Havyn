"""The Autopilot's own runtime transports.

These pin the contracts the rest of the engine relies on, and — just as
importantly — the contract with the Packet Tracer extension, which is a fixed
consumer we do not control:

* ``pt/token.py`` — the shared secret both channels authenticate with. Same
  path and same minimum strength as the extension expects, because the
  extension reads the file directly and has no way to be told otherwise.
* ``pt/mailbox.py`` — the file protocol the Script Engine implements:
  ``req_<tag>.js`` in, ``res_<tag>.txt`` out, ``alive.txt`` heartbeat.
* ``pt/http_bridge.py`` — the loopback HTTP bridge the webview polls, with
  token authentication and request-id result correlation.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request

import pytest

from src.network_autopilot.pt import mailbox as mb
from src.network_autopilot.pt import token as tk
from src.network_autopilot.pt.http_bridge import (
    DEFAULT_PORT,
    MAX_BATCH_COMMANDS,
    PTCommandBridge,
    next_rid,
    report_result_js,
)


# --------------------------------------------------------------------------
# token
# --------------------------------------------------------------------------


class TestToken:
    def test_fingerprint_is_stable_and_short(self):
        a = tk.token_fingerprint("s" * 40)
        assert a == tk.token_fingerprint("s" * 40)
        assert len(a) == 16
        assert a != tk.token_fingerprint("t" * 40)

    def test_strength_gate(self):
        assert tk.is_valid("a" * 32)
        assert not tk.is_valid("a" * 31)          # too short
        assert not tk.is_valid("")                 # empty
        assert not tk.is_valid("!" * 40)           # illegal character

    def test_generated_token_is_usable(self):
        value = tk.get_bridge_token(refresh=True)
        assert tk.is_valid(value)

    def test_env_override_is_validated_not_trusted(self, monkeypatch):
        # The override exists for tests and CI, so a weak value there would
        # quietly remove the only defence against a hostile web page.
        monkeypatch.setenv(tk._ENV_VAR, "short")
        with pytest.raises(tk.BridgeTokenError):
            tk.get_bridge_token(refresh=True)
        monkeypatch.setenv(tk._ENV_VAR, "k" * 40)
        assert tk.get_bridge_token() == "k" * 40

    def test_second_creator_does_not_overwrite(self, tmp_path):
        """O_EXCL, not rename: with rename two servers starting together would
        end up holding different tokens and only one client would pair."""
        target = tmp_path / "bridge_token"
        first = tk._create_token(target)
        assert first is not None
        assert tk._create_token(target) is None
        assert target.read_text() == first

    def test_token_dir_is_local_not_roaming(self):
        if os.name == "nt":
            # %LOCALAPPDATA%, not %APPDATA%: a loopback secret must not roam
            # to a file server with a domain profile.
            assert "Roaming" not in str(tk.token_dir())


# --------------------------------------------------------------------------
# runtime dir rename (S1.18 / F16)
# --------------------------------------------------------------------------


class TestHavynDirs:
    """Product is Havyn: runtime data lives under Havyn/, the old
    packet-tracer-mcp/ dir is read for one-time migration and then left
    alone so old installs keep working."""

    @pytest.fixture(autouse=True)
    def _isolated(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        monkeypatch.delenv(tk._ENV_VAR, raising=False)
        tk.reset_cache()
        yield tmp_path
        tk.reset_cache()

    def _old_token_file(self, tmp_path, value: str = "a" * 40):
        old = tmp_path / "packet-tracer-mcp"
        old.mkdir(parents=True, exist_ok=True)
        (old / "bridge_token").write_text(value, encoding="utf-8")
        return old

    def test_token_dir_is_havyn(self, tmp_path):
        assert tk.token_dir() == tmp_path / "Havyn"

    def test_old_token_migrates_on_first_use(self, tmp_path):
        """Old install's token is copied to the new dir once; the old file
        stays so the old extension builds keep working."""
        self._old_token_file(tmp_path)

        assert tk.get_bridge_token() == "a" * 40
        assert (tmp_path / "Havyn" / "bridge_token").read_text(
            encoding="utf-8"
        ) == "a" * 40
        assert (tmp_path / "packet-tracer-mcp" / "bridge_token").exists()

    def test_new_token_wins_when_both_exist(self, tmp_path):
        self._old_token_file(tmp_path, "a" * 40)
        new = tmp_path / "Havyn"
        new.mkdir(parents=True, exist_ok=True)
        (new / "bridge_token").write_text("b" * 40, encoding="utf-8")

        assert tk.get_bridge_token() == "b" * 40
        assert (tmp_path / "packet-tracer-mcp" / "bridge_token").read_text(
            encoding="utf-8"
        ) == "a" * 40

    def test_fresh_install_creates_only_the_new_dir(self, tmp_path):
        value = tk.get_bridge_token()

        assert tk.is_valid(value)
        assert (tmp_path / "Havyn" / "bridge_token").exists()
        assert not (tmp_path / "packet-tracer-mcp").exists()

    def test_extension_searches_the_new_dir_before_the_old(self):
        """Both halves must agree on the order. main.js cannot be executed
        from a test, so its candidate list is pinned by reading it, next to
        the engine's own order - a rename that updated one side only would
        leave the engine and the extension looking in different places."""
        from pathlib import Path

        js = (
            Path(__file__).resolve().parents[2]
            / "EXTENSION" / "script-engine" / "main.js"
        ).read_text(encoding="utf-8")
        new = js.index("AppData/Local/Havyn/bridge_token")
        old = js.index("AppData/Local/packet-tracer-mcp/bridge_token")
        assert new < old, "the extension must try Havyn/ before packet-tracer-mcp/"

        candidates = tk._candidate_dirs()
        assert [c.name for c in candidates] == ["Havyn", "packet-tracer-mcp"]


# --------------------------------------------------------------------------
# mailbox
# --------------------------------------------------------------------------


class _FakeScriptEngine:
    """Stands in for PT's Script Engine: polls, executes, writes a reply."""

    def __init__(self, directory, reply=lambda js: "OK"):
        self.dir = directory
        self.reply = reply
        self.seen: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                for entry in sorted(os.listdir(self.dir)):
                    if not entry.startswith("req_") or not entry.endswith(".js"):
                        continue
                    name = entry[4:-3]
                    js = open(
                        os.path.join(self.dir, entry), encoding="utf-8"
                    ).read()
                    self.seen.append(js)
                    result = self.reply(js)
                    open(
                        os.path.join(self.dir, f"res_{name}.txt"),
                        "w", encoding="utf-8", newline="",
                    ).write(result)
                    os.remove(os.path.join(self.dir, entry))
            except OSError:
                pass
            time.sleep(0.02)

    def touch_heartbeat(self):
        open(
            os.path.join(self.dir, "alive.txt"), "w", encoding="utf-8"
        ).write(str(int(time.time() * 1000)))

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2)


class TestMailbox:
    def test_round_trip_against_a_simulated_engine(self, tmp_path):
        engine = _FakeScriptEngine(tmp_path, reply=lambda js: "OK:" + js.strip())
        try:
            channel = mb.FileMailbox(tmp_path)
            assert channel.send_and_wait("doThing()", timeout=5) == "OK:doThing()"
        finally:
            engine.stop()

    def test_alive_depends_on_a_fresh_heartbeat(self, tmp_path):
        channel = mb.FileMailbox(tmp_path)
        assert channel.pt_alive() is False       # no heartbeat at all
        engine = _FakeScriptEngine(tmp_path)
        try:
            engine.touch_heartbeat()
            assert channel.pt_alive() is True
            # A stale heartbeat must read as dead: that is what distinguishes
            # "PT is not open" from "PT is open".
            old = time.time() - (mb.HEARTBEAT_FRESH_S + 5)
            os.utime(tmp_path / "alive.txt", (old, old))
            assert channel.pt_alive() is False
        finally:
            engine.stop()

    def test_newlines_survive_byte_for_byte(self, tmp_path):
        """On Windows text mode turns \\n into \\r\\n, and a real CR/LF inside a
        JS string literal is a syntax error — so multi-line IOS config has to
        reach the engine unchanged."""
        channel = mb.FileMailbox(tmp_path)
        command = "configureIosDevice('R1', [\n 'line vty 0 4',\n])"
        channel.send(command)
        request = next(p for p in tmp_path.glob("req_*.js"))
        assert request.read_bytes() == command.encode("utf-8")

    def test_mailbox_meets_a_stale_extension_in_the_old_dir(
        self, tmp_path, monkeypatch):
        """S1.18 live find: an extension started before the rename keeps
        heartbeating into packet-tracer-mcp/bridge while the engine defaults
        to Havyn/bridge. The mailbox must meet the fresh heartbeat, not go
        dark insisting on the new dir."""
        new = tmp_path / "Havyn"
        old = tmp_path / "packet-tracer-mcp"
        (old / "bridge").mkdir(parents=True)
        (old / "bridge" / "alive.txt").write_text(
            str(int(time.time() * 1000)), encoding="utf-8")
        monkeypatch.setattr(tk, "_candidate_dirs", lambda: [new, old])

        assert mb.mailbox_dir() == old / "bridge"
        assert mb.FileMailbox().pt_alive() is True

    def test_mailbox_prefers_the_new_dir_when_nothing_beats_it(
        self, tmp_path, monkeypatch):
        new = tmp_path / "Havyn"
        old = tmp_path / "packet-tracer-mcp"
        old.mkdir(parents=True)
        monkeypatch.setattr(tk, "_candidate_dirs", lambda: [new, old])

        assert mb.mailbox_dir() == new / "bridge"

    def test_timeout_withdraws_the_request(self, tmp_path):
        """A request left queued gets executed late, and then the retry runs it
        a second time."""
        channel = mb.FileMailbox(tmp_path)
        assert channel.send_and_wait("neverAnswered()", timeout=0.4) is None
        assert list(tmp_path.glob("req_*.js")) == []

    def test_reply_older_than_the_request_is_not_ours(self, tmp_path):
        """A leftover reply from a previous run (recycled pid+counter) must
        never be reported as this operation's answer."""
        channel = mb.FileMailbox(tmp_path)
        tag = channel._next_tag()
        stale = tmp_path / f"res_{tag}.txt"
        stale.write_text("STALE", encoding="utf-8")
        old = time.time() - 60
        os.utime(stale, (old, old))
        assert channel.send_and_wait("go()", timeout=1.0) is None

    def test_send_is_fire_and_forget(self, tmp_path):
        channel = mb.FileMailbox(tmp_path)
        assert channel.send("fire()") is True
        assert len(list(tmp_path.glob("req_*.js"))) == 1


# --------------------------------------------------------------------------
# http bridge
# --------------------------------------------------------------------------


@pytest.fixture()
def bridge():
    server = PTCommandBridge(port=0, token="t" * 40)
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _get(server, path, token="t" * 40, wait=None):
    """Build a request against the running bridge.

    ``path`` may carry its own query; the token is added as a parameter rather
    than concatenated blindly so a path that already has a query stays valid.
    """
    query = []
    if token is not None:
        query.append(f"t={token}")
    if wait is not None:
        query.append(f"wait={wait}")
    if "?" in path:
        head, existing = path.split("?", 1)
        parts = [existing] + query
    else:
        head = path
        parts = query
    url = f"http://127.0.0.1:{server.port}{head}"
    if parts:
        url += "?" + "&".join(parts)
    request = urllib.request.Request(url)
    return urllib.request.urlopen(request, timeout=30)


class TestHttpBridge:
    def test_ping_needs_no_token(self, bridge):
        with _get(bridge, "/ping", token=None) as response:
            assert response.status == 200
            body = json.loads(response.read())
        assert body["service"] == "pt-mcp-bridge"
        # only a fingerprint, never the secret
        assert "t" * 40 not in json.dumps(body)

    def test_every_other_route_requires_the_token(self, bridge):
        for path in ("/next", "/status"):
            with pytest.raises(urllib.error.HTTPError) as caught:
                _get(bridge, path, token=None)
            assert caught.value.code == 401

    def test_wrong_token_is_refused_and_counted(self, bridge):
        with pytest.raises(urllib.error.HTTPError) as caught:
            _get(bridge, "/status", token="w" * 40)
        assert caught.value.code == 401
        assert bridge.saw_recent_unauthorized
        assert bridge.status_dict()["unauth_count"] >= 1

    def test_rebinding_host_is_refused(self, bridge):
        """A DNS-rebinding request arrives with a Host we do not serve."""
        url = f"http://127.0.0.1:{bridge.port}/status?t={'t' * 40}"
        request = urllib.request.Request(url)
        request.add_header("Host", "evil.example")
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        assert caught.value.code == 401

    def test_queue_then_next_returns_the_command(self, bridge):
        body = b"doThing()"
        url = f"http://127.0.0.1:{bridge.port}/queue?t={'t' * 40}"
        urllib.request.urlopen(
            urllib.request.Request(url, data=body), timeout=10
        ).read()
        with _get(bridge, "/next") as response:
            assert response.read().decode() == "doThing()"

    def test_next_delivers_a_batch_not_one_at_a_time(self, bridge):
        """Handing out one command per 500ms made a 40-command topology take
        tens of seconds."""
        url = f"http://127.0.0.1:{bridge.port}/queue?t={'t' * 40}"
        for index in range(5):
            urllib.request.urlopen(
                urllib.request.Request(url, data=f"c{index}()".encode()),
                timeout=10,
            ).read()
        with _get(bridge, "/next") as response:
            delivered = response.read().decode().split("\n")
        assert delivered == [f"c{i}()" for i in range(5)]
        assert MAX_BATCH_COMMANDS >= 5

    def test_result_correlates_by_id(self, bridge):
        """A slow answer must not be consumed by the next operation — PT
        returns real data, just from the wrong device. Both answers are posted
        before either is collected, which is exactly the ordering that used to
        hand each operation the other's result."""
        first, second = next_rid(), next_rid()
        assert first != second
        result_url = f"http://127.0.0.1:{bridge.port}/result?t={'t' * 40}"
        urllib.request.urlopen(
            urllib.request.Request(
                f"{result_url}&rid={second}", data=b"WRONG"
            ), timeout=10,
        ).read()
        urllib.request.urlopen(
            urllib.request.Request(
                f"{result_url}&rid={first}", data=b"RIGHT"
            ), timeout=10,
        ).read()

        # Each id gets its own answer, regardless of arrival order.
        with _get(bridge, f"/result?rid={first}", wait=2) as response:
            assert response.read().decode() == "RIGHT"
        with _get(bridge, f"/result?rid={second}", wait=2) as response:
            assert response.read().decode() == "WRONG"
        # Both were consumed: neither leaks into a later operation. 204 is a
        # 2xx, so that is a normal response rather than an exception.
        with _get(bridge, f"/result?rid={first}", wait=1) as response:
            assert response.status == 204
            assert response.read() == b""

    def test_result_wait_is_bounded(self, bridge):
        """An id that never gets an answer must not hold the connection open
        for ever; the wait the caller asked for is the ceiling."""
        started = time.time()
        with _get(bridge, f"/result?rid={next_rid()}", wait=1) as response:
            assert response.status == 204
        assert time.time() - started < 10

    def test_result_without_an_id_is_discarded(self, bridge):
        url = f"http://127.0.0.1:{bridge.port}/result?t={'t' * 40}"
        with urllib.request.urlopen(
            urllib.request.Request(url, data=b"orphan"), timeout=10
        ) as response:
            assert response.read().decode() == "discarded"

    def test_oversized_body_is_never_buffered(self, bridge):
        """An oversized body must be refused, not absorbed.

        The server deliberately does not read a body past the cap, so the
        client may see the 413 or — because we close while it is still writing
        2 MiB — a connection abort. Either is fine; what must never happen is
        the command being queued, because buffering an arbitrary body is the
        denial-of-service we put the cap there to prevent.
        """
        url = f"http://127.0.0.1:{bridge.port}/queue?t={'t' * 40}"
        with pytest.raises((urllib.error.HTTPError, OSError)):
            urllib.request.urlopen(
                urllib.request.Request(url, data=b"x" * (1 << 21)), timeout=10
            )
        # Nothing was enqueued, and /next stays empty.
        with _get(bridge, "/next", wait=0) as response:
            assert response.read() == b""

    def test_body_just_under_the_cap_is_accepted(self, bridge):
        url = f"http://127.0.0.1:{bridge.port}/queue?t={'t' * 40}"
        payload = b"a" * 4096
        urllib.request.urlopen(
            urllib.request.Request(url, data=payload), timeout=10
        ).read()
        with _get(bridge, "/next", wait=1) as response:
            assert response.read() == payload

    def test_status_is_reachable_and_typed(self, bridge):
        with _get(bridge, "/status") as response:
            body = json.loads(response.read())
        assert set(body) >= {"connected", "unauth_recent", "token_id"}

    def test_report_result_js_carries_token_then_id(self):
        js = report_result_js(DEFAULT_PORT, "SECRET", "42-1")
        assert "/result?t=SECRET&rid=42-1" in js
        # the value has to be embedded in a way that survives PT's JS parser
        assert "function reportResult" in js
        assert "\\n" in js        # newlines are escaped, not literal


def test_next_rid_is_unique_and_url_safe():
    ids = {next_rid() for _ in range(50)}
    assert len(ids) == 50
    for value in ids:
        assert all(ch.isalnum() or ch in "-_" for ch in value), value
