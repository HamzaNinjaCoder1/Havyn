"""File-mailbox transport to Packet Tracer's Script Engine.

Why this channel exists alongside HTTP: the HTTP bridge is polled by the
extension's *webview*, which only lives while that window is open. Close it
and Packet Tracer stops executing commands even though the extension is still
installed. The Script Engine is different — it runs whenever PT is open, even
headless, and it has a timer and filesystem access — but it has no
XMLHttpRequest, so it cannot speak HTTP. A directory of files is the protocol
it can speak.

These channels are complements, not alternatives: HTTP while the window is
open, the mailbox when it is closed. Choosing between them per request (never
both) is the adapter's job; this module is only the transport.

Wire protocol, one file per request, written atomically::

    Python   -- writes req_<tag>.js (tmp then rename) -->  Script Engine
    Python   <- reads and removes res_<tag>.txt        --  executes, writes res
    Script Engine touches alive.txt every tick, so its heartbeat says it is up

The tag is ``<pid>_<counter>``, which keeps concurrent servers sharing one
mailbox from colliding, and makes a leftover file attributable to the process
that wrote it.

Trust model: the mailbox sits under the user's own local app-data directory,
which a browser page cannot write to. So unlike the HTTP channel this one has
no cross-origin exposure to defend against — it relies on the same assumption
the rest of the design already makes, that the local user is the user.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from .token import token_dir

# The mailbox lives next to the token so both share one place to grant access.
_MAILBOX_SUBDIR = "bridge"

# How long a heartbeat stays meaningful. PT's Script Engine ticks several times
# a second, so anything older than this means the engine is gone.
HEARTBEAT_FRESH_S = 6.0

# How often a response file is polled while waiting for it.
_POLL_INTERVAL_S = 0.1

# Windows can briefly refuse to replace a file another handle has open; the
# Script Engine opens each request as it polls. Bounded, so a genuinely
# locked file still fails instead of hanging the build.
_REPLACE_RETRIES = 50
_REPLACE_DELAY = 0.02


def _heartbeat_fresh(directory: Path) -> bool:
    """True while the Script Engine is refreshing this dir's heartbeat."""
    try:
        age = time.time() - (directory / "alive.txt").stat().st_mtime
    except OSError:
        return False
    return age < HEARTBEAT_FRESH_S


def mailbox_dir() -> Path:
    """Where to talk to the Script Engine: the new dir first, then the old.

    S1.18 compat: the extension resolves its mailbox the same way, but it
    resolves once at load — an extension started before the rename (or
    before migration) keeps heartbeating into the pre-rename dir. Meeting
    it where its heartbeat is fresh is what reconnects, not what forks:
    only one dir is ever used per call.
    """
    from .token import _candidate_dirs

    for candidate in _candidate_dirs():
        if _heartbeat_fresh(candidate / _MAILBOX_SUBDIR):
            return candidate / _MAILBOX_SUBDIR
    return token_dir() / _MAILBOX_SUBDIR


def ensure_mailbox_dir() -> Path:
    path = mailbox_dir()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


class FileMailbox:
    """The Python side of the file mailbox.

    Holds almost no state — a sequence counter — because the real state is the
    files on disk, which is what lets a restarted process pick up cleanly.
    """

    def __init__(self, directory: Path | None = None):
        self.dir = Path(directory) if directory else mailbox_dir()
        # Only a default-dir mailbox follows a fresh heartbeat elsewhere: a
        # caller-supplied directory (tests, alternate config) is explicit and
        # stays put.
        self._follow_heartbeat = directory is None
        self._seq = 0

    def _ensure_dir(self) -> None:
        # Always self.dir, never the module default: a caller-supplied
        # directory (tests, alternate config) has to be both created and
        # written to consistently.
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    # -- Script Engine liveness -----------------------------------------

    def pt_alive(self) -> bool:
        """True while the Script Engine is refreshing its heartbeat."""
        if _heartbeat_fresh(self.dir):
            return True
        # The extension may have restarted onto the other dir (pre/post
        # rename); follow a fresh heartbeat rather than going dark. Explicit
        # directories never follow.
        if self._follow_heartbeat:
            resolved = mailbox_dir()
            if resolved != self.dir and _heartbeat_fresh(resolved):
                self.dir = resolved
                return True
        return False

    # -- request/response ------------------------------------------------

    def _next_tag(self) -> str:
        """Per-process monotonic tag; the pid separates concurrent servers."""
        self._seq += 1
        return f"{os.getpid()}_{self._seq:06d}"

    def _write_atomic(self, path: Path, text: str) -> None:
        """Publish a file so a reader never observes it half-written.

        Written as bytes, not text: on Windows text mode turns ``\\n`` into
        ``\\r\\n``, and a real CR/LF inside a JavaScript string literal is a
        syntax error. Multi-line IOS CLI configuration has to reach the Script
        Engine byte-for-byte as generated.
        """
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(text.encode("utf-8"))
        # Windows refuses os.replace while another handle has the destination
        # open. The Script Engine legitimately opens the request to read it,
        # so retry the replace briefly instead of losing the command (a
        # PermissionError here became a silent send_and_wait timeout).
        for attempt in range(_REPLACE_RETRIES):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt + 1 >= _REPLACE_RETRIES:
                    raise
                time.sleep(_REPLACE_DELAY)

    def send(self, js_code: str) -> bool:
        """Queue a command without waiting for a reply."""
        try:
            self._ensure_dir()
            tag = self._next_tag()
            self._write_atomic(self.dir / f"req_{tag}.js", js_code)
            return True
        except OSError:
            return False

    def send_and_wait(self, js_code: str, timeout: float = 12.0) -> str | None:
        """Queue a command and wait for the Script Engine's reply.

        ``None`` on timeout, which callers must treat as "no evidence" rather
        than as failure or success.
        """
        try:
            self._ensure_dir()
        except OSError:
            return None

        tag = self._next_tag()
        request = self.dir / f"req_{tag}.js"
        response = self.dir / f"res_{tag}.txt"
        try:
            self._write_atomic(request, js_code)
        except OSError:
            return None

        # A response file that predates this request belongs to an earlier run
        # — a recycled pid+counter after a crash — and reading it as this
        # operation's answer would be reporting someone else's data as ours.
        sent_at = time.time()
        deadline = sent_at + timeout
        while time.time() < deadline:
            try:
                if response.exists():
                    if response.stat().st_mtime < sent_at:
                        # Leftover from a previous request; keep waiting for
                        # the answer to *this* one.
                        time.sleep(_POLL_INTERVAL_S)
                        continue
                    body = response.read_text(encoding="utf-8")
                    response.unlink(missing_ok=True)
                    return body
            except OSError:
                pass
            time.sleep(_POLL_INTERVAL_S)

        # Timed out: withdraw the request. Leaving it queued "in case it gets
        # processed late" is how a retried mutation ends up running twice — we
        # lose patience, re-send, and the Script Engine then handles both.
        # Autopilot operations are idempotent, but a doubled command still
        # dirties the workspace, so in-flight work is capped at what we are
        # actually waiting on.
        for path in (request, response):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        return None


# Kept as the historical name for this transport. Probes and callers reach for
# it, and the rename to FileMailbox is cosmetic — the mailbox it talks to is
# the same.
FileBridge = FileMailbox
