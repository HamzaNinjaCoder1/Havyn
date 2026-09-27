"""HTTP command bridge between Python and Packet Tracer.

Packet Tracer cannot be driven directly, so this module runs a small local
HTTP server and the extension's webview polls it for work. The extension
executes what it receives and posts the outcome back.

Security posture, and it is worth stating plainly because it is the whole
reason the token exists:

* Binding to 127.0.0.1 is not a control. A ``POST /queue`` with
  ``Content-Type: text/plain`` is a CORS-"simple" request, so any web page
  open in any browser on this host can enqueue JavaScript, and PT runs it
  through ``new Function()``. Loopback prevents the attacker from *reading*
  the answer, which is irrelevant — the injection never needed to read one.
* So every endpoint except ``/ping`` requires the shared token. ``/ping`` stays
  open only so we can tell "something owns this port" from "our bridge is
  here", and it reveals nothing but a one-way fingerprint of the token.
* The ``Host`` header is checked too. It is derived from the URL the client
  dialled, so a DNS-rebinding request arrives as ``Host: evil.com:port`` and is
  refused. Packet Tracer always sends ``127.0.0.1:<port>``, so this cannot
  reject the real client.
* The extension obtains the token by reading the token file through PT's own
  filesystem API — nothing is paired, and the secret is never served over
  HTTP.

Result correlation: every command carries a request id, injected inside the
JavaScript, and PT hands it straight back when posting. Without that, results
queued up globally and a slow answer was consumed by the *next* operation — PT
returned real data, from the wrong device. The mailbox channel correlated by
filename; this is the same idea over HTTP.
"""

from __future__ import annotations

import hmac
import http.server
import itertools
import json
import os
import threading
import time
from http.server import ThreadingHTTPServer
from queue import Empty, Full, Queue
from urllib.parse import parse_qs, urlparse

from .token import get_bridge_token, token_fingerprint

# The port the extension is configured to poll.
DEFAULT_PORT = 54321

# 1 MiB. Real commands are a few KB; this ceiling only stops abuse.
MAX_BODY_BYTES = 1 << 20

# Bounded queue: if PT wedges, pending work must not grow without limit.
MAX_QUEUE_ITEMS = 1000

# Ceiling on how long ``/result`` may hold a connection. The caller chooses its
# own wait (it knows how slow its operation is) but not without limit, since
# every waiting request occupies a thread. Real callers ask for up to 45s.
MAX_RESULT_WAIT_SECONDS = 60.0

# A result nobody collects is dropped after this long. Without it, the result
# table trades the correlation bug for a slow leak.
RESULT_TTL_SECONDS = 60.0

# Hard ceiling on that table, for when more orphaned results arrive than the
# TTL sweeps.
MAX_RESULT_ITEMS = 256

# ``/next`` holds the request this long waiting for work instead of answering
# immediately. That both cuts latency (a command leaves the moment it is
# queued rather than on the next 500ms tick) and stops the idle drip of empty
# requests.
NEXT_LONGPOLL_SECONDS = 2.0

# Commands handed over per response. PT runs the whole batch in one runCode.
# Measured on PT 9.0: 10 devices + links + IOS config ran in ~100ms with no
# spacing needed, because every command already carries its own try/catch.
MAX_BATCH_COMMANDS = 200

# How recently a poll must have happened for the bridge to count as connected.
_CLIENT_FRESH_S = 10.0

# How long a failed authentication stays interesting in a status report. This
# is what separates "PT is not open" from "PT is open and we are rejecting it"
# — two situations that otherwise look identical.
_UNAUTH_FRESH_S = 30.0

_rid_counter = itertools.count(1)


def next_rid() -> str:
    """A process-unique operation id.

    The pid keeps separate MCP processes sharing a bridge apart; the counter
    keeps operations apart within one. Deliberately free of characters that
    would need escaping in a query string.
    """
    return f"{os.getpid()}-{next(_rid_counter)}"


def report_result_js(port: int = DEFAULT_PORT, token: str = "", rid: str = "") -> str:
    """JavaScript that defines ``reportResult()`` and posts the value back.

    Defined inline with every command so it shares runCode's scope. It routes
    through the webview's XMLHttpRequest because Packet Tracer's Script Engine
    has none of its own.

    The request id travels inside this snippet, so PT echoes it back on its
    own — the extension never assembles the URL, it only runs what it is given.
    That is what let correlation be added without redistributing the built
    extension. The token precedes the rid on purpose: a test asserts the literal
    ``/result?t=<token>`` appears.
    """
    single = chr(39)
    double = chr(34)
    backslash = chr(92)
    return (
        "function reportResult(d){"
        "var s=String(d)"
        f".replace(/{backslash}{backslash}/g,{single}{backslash}{backslash}{backslash}{backslash}{single})"
        f".replace(/{single}/g,{double}{backslash}{backslash}{single}{double})"
        f".replace(/{backslash}n/g,{single}{backslash}{backslash}n{single});"
        "window.webview.evaluateJavaScriptAsync("
        f"{single}var x=new XMLHttpRequest();"
        f"x.open({backslash}{single}POST{backslash}{single},"
        f"{backslash}{single}http://127.0.0.1:{port}/result?t={token}&rid={rid}"
        f"{backslash}{single},true);"
        f"x.setRequestHeader({backslash}{single}Content-Type{backslash}{single},"
        f"{backslash}{single}text/plain{backslash}{single});"
        f"x.send({backslash}{single}{backslash}{single}+s+{backslash}{single});{single}"
        ")}"
    )


class PTCommandBridge:
    """HTTP bridge between Python and Packet Tracer's extension."""

    def __init__(self, port: int = DEFAULT_PORT, token: str | None = None):
        self.port = port
        self.token = token or get_bridge_token()
        self.token_id = token_fingerprint(self.token)
        self._queue: Queue[str] = Queue(maxsize=MAX_QUEUE_ITEMS)
        # Keyed by request id rather than queued: with a queue, a late result
        # nobody collected would sit at the head and be handed to whichever
        # operation asked next.
        self._results: dict[str, tuple[str, float]] = {}
        self._results_cv = threading.Condition()
        self._result_ttl = RESULT_TTL_SECONDS
        self._server = None
        self._thread = None
        self._last_poll_time: float = 0.0
        # Diagnostics: "PT is absent" and "PT is present and we rejected it"
        # look the same from the outside unless we count the rejections.
        self._unauth_count: int = 0
        self._unauth_last: float = 0.0
        self._unauth_paths: set[str] = set()
        # Whatever PT actually sends on its first authenticated poll. This is
        # the only honest way to learn the webview's Origin without guessing.
        self._client_headers: dict[str, str] = {}

    # -- state ----------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        if self._last_poll_time == 0:
            return False
        return time.time() - self._last_poll_time < _CLIENT_FRESH_S

    @property
    def saw_recent_unauthorized(self) -> bool:
        """True if something called in without the token recently."""
        return self._unauth_last > 0 and (time.time() - self._unauth_last) < _UNAUTH_FRESH_S

    def status_dict(self) -> dict:
        ago = time.time() - self._last_poll_time
        return {
            "connected": self._last_poll_time > 0 and ago < _CLIENT_FRESH_S,
            "last_poll_ago": round(ago, 1) if self._last_poll_time else None,
            "unauth_recent": self.saw_recent_unauthorized,
            "unauth_count": self._unauth_count,
            "unauth_paths": sorted(self._unauth_paths),
            "client_headers": dict(self._client_headers),
            "token_id": self.token_id,
        }

    # -- correlated results ----------------------------------------------

    def _purge_results_locked(self) -> None:
        """Drop results nobody came back for. Caller holds the lock."""
        now = time.time()
        for rid in [r for r, (_, ts) in self._results.items() if now - ts > self._result_ttl]:
            del self._results[rid]
        # Hard ceiling in case more orphans arrive than the TTL clears: the
        # oldest go first.
        if len(self._results) >= MAX_RESULT_ITEMS:
            oldest = sorted(self._results.items(), key=lambda kv: kv[1][1])
            for rid, _ in oldest[: len(self._results) - MAX_RESULT_ITEMS + 1]:
                del self._results[rid]

    def put_result(self, rid: str, body: str) -> None:
        """Record the result for ``rid`` and wake anything waiting on it."""
        with self._results_cv:
            self._purge_results_locked()
            self._results[rid] = (body, time.time())
            self._results_cv.notify_all()

    def take_result(self, rid: str, wait: float) -> str | None:
        """Wait for and consume ``rid``'s result; ``None`` if it never arrived.

        Keyed by id: taking "whatever result is next" is how a real reply from
        device B gets reported as device A's.
        """
        deadline = time.time() + min(max(wait, 0.0), MAX_RESULT_WAIT_SECONDS)
        with self._results_cv:
            while True:
                hit = self._results.pop(rid, None)
                if hit is not None:
                    return hit[0]
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._results_cv.wait(remaining)

    def drain_commands(self) -> list[str]:
        """Block for one command, then take everything else already queued.

        Delivering one every 500ms made a 40-command topology take tens of
        seconds. PT executes the whole batch in a single runCode, and each
        command already has its own try/catch.
        """
        commands: list[str] = []
        try:
            commands.append(self._queue.get(timeout=NEXT_LONGPOLL_SECONDS))
        except Empty:
            return commands
        while len(commands) < MAX_BATCH_COMMANDS:
            try:
                commands.append(self._queue.get_nowait())
            except Empty:
                break
        return commands

    # -- server ---------------------------------------------------------

    def start(self):
        """Bind the loopback server and start serving in the background."""
        bridge = self

        class Handler(http.server.BaseHTTPRequestHandler):
            # -- request helpers --

            def _parse(self):
                """Split path from query string.

                Without this, comparing ``self.path`` literally sends every
                ``?t=...`` request down the 404 branch — the latent bug that
                broke the token before it was ever checked.
                """
                parsed = urlparse(self.path)
                return parsed.path, parse_qs(parsed.query)

            def _host_ok(self) -> bool:
                """Reject a request whose Host is not this loopback service."""
                host = (self.headers.get("Host") or "").strip().lower()
                return host in (
                    f"127.0.0.1:{bridge.port}",
                    f"localhost:{bridge.port}",
                    f"[::1]:{bridge.port}",
                )

            def _token_from(self, qs: dict) -> str:
                return (qs.get("t", [""])[0]) or self.headers.get("X-PT-Token", "")

            def _authorized(self, qs: dict) -> bool:
                if not self._host_ok():
                    return False
                return hmac.compare_digest(self._token_from(qs), bridge.token)

            def _note_unauth(self, path: str) -> None:
                bridge._unauth_count += 1
                bridge._unauth_last = time.time()
                bridge._unauth_paths.add(path)

            def _remember_client(self) -> None:
                if bridge._client_headers:
                    return
                for header in ("Origin", "Sec-Fetch-Site", "Sec-Fetch-Mode", "User-Agent"):
                    value = self.headers.get(header)
                    if value:
                        bridge._client_headers[header] = value

            def _read_body(self) -> str | None:
                """Read a length-capped body; ``None`` if it must be refused."""
                try:
                    length = int(self.headers.get("Content-Length", 0) or 0)
                except ValueError:
                    self._deny(400)
                    return None
                if length < 0 or length > MAX_BODY_BYTES:
                    self._deny(413)
                    return None
                if not length:
                    return ""
                return self.rfile.read(length).decode("utf-8", "replace")

            def _deny(self, code: int = 401, path: str = "") -> None:
                if code == 401 and path:
                    self._note_unauth(path)
                # Drain a small body before answering. Closing while the client
                # is still writing makes it see a connection reset instead of
                # the status code (WinError 10053 on Windows). Large bodies are
                # deliberately NOT read — refusing to read them is the point of
                # the 413.
                try:
                    length = int(self.headers.get("Content-Length", 0) or 0)
                except ValueError:
                    length = 0
                if 0 < length <= 65536:
                    try:
                        self.rfile.read(length)
                    except OSError:
                        pass
                self.close_connection = True
                self.send_response(code)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                # No CORS headers on a refusal: nothing legitimate reads an
                # error, and leaving them off means an attacking page cannot
                # even learn which check failed.
                self.end_headers()

            # -- routes --

            def do_GET(self):
                path, qs = self._parse()

                if path == "/ping":
                    # Unauthenticated on purpose: needed to learn that the port
                    # is ours before we know the token. Reveals only a
                    # non-invertible fingerprint.
                    self._respond(200, json.dumps({
                        "service": "pt-mcp-bridge",
                        "proto": 1,
                        "id": bridge.token_id,
                    }))
                    return

                if not self._authorized(qs):
                    self._deny(401, path)
                    return

                if path == "/next":
                    self._remember_client()
                    # Record the poll BEFORE the long-poll wait: otherwise the
                    # bridge believes itself disconnected for as long as the
                    # queue is empty.
                    bridge._last_poll_time = time.time()
                    self._respond(200, "\n".join(bridge.drain_commands()))
                elif path == "/status":
                    self._respond(200, json.dumps(bridge.status_dict()))
                elif path == "/result":
                    # The caller sets the wait — it knows how long its own
                    # operation takes. A fixed 9s here used to cut short
                    # anything slower (26 of 36 calls, up to 45s) and report
                    # them as failures on a 204.
                    rid = (qs.get("rid", [""])[0] or "").strip()
                    if not rid:
                        self._respond(204, "")
                        return
                    try:
                        wait = float(qs.get("wait", ["9"])[0])
                    except ValueError:
                        wait = 9.0
                    result = bridge.take_result(rid, wait)
                    if result is None:
                        self._respond(204, "")
                    else:
                        self._respond(200, result)
                else:
                    self._deny(404)

            def do_POST(self):
                path, qs = self._parse()

                if not self._authorized(qs):
                    self._deny(401, path)
                    return

                body = self._read_body()
                if body is None:
                    return

                if path == "/result":
                    # A result with no id cannot be attributed to any
                    # operation, so it is dropped. Storing it "just in case" is
                    # precisely how it used to contaminate the next operation.
                    rid = (qs.get("rid", [""])[0] or "").strip()
                    if not rid:
                        self._respond(200, "discarded")
                        return
                    bridge.put_result(rid, body)
                    self._respond(200, "ok")
                elif path == "/queue":
                    if body:
                        try:
                            bridge._queue.put_nowait(body)
                        except Full:
                            self._deny(503)
                            return
                    self._respond(200, "queued")
                else:
                    self._deny(404)

            def do_OPTIONS(self):
                self.send_response(200)
                self._cors_headers()
                self.end_headers()

            def _respond(self, code, body):
                self.send_response(code)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self._cors_headers()
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))

            def _cors_headers(self):
                # Permissive on success because the webview depends on CORS to
                # read them, and we do not yet know what Origin it sends (it is
                # captured in _remember_client so we can tighten later). CORS
                # never stopped the request being SENT — that was the actual
                # hole, and the token is what closes it, not this header.
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-PT-Token")

            def log_message(self, format, *args):
                pass  # keep the console clean

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        # port=0 asks for an ephemeral port; the real one has to be read back so
        # Host validation and the client bootstrap target the right address.
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        """Shut the server down."""
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # There is intentionally no send()/send_and_wait() here: the MCP adapter
    # talks to the bridge over HTTP (POST /queue, GET /result) rather than
    # calling methods on an instance that another process may have started.
