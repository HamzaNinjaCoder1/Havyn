"""Shared secret for the Packet Tracer command channels.

Why a secret is needed at all: the bridge listens on loopback, which is NOT a
security boundary. A ``POST /queue`` carrying ``Content-Type: text/plain`` is
a CORS-"simple" request, so *any* web page open in *any* browser on this
machine can enqueue JavaScript, and Packet Tracer runs whatever it finds there.
Loopback only stops the attacker from reading the reply — and the injection
never needed to read a reply.

What closes it is a value the attacking page cannot guess or derive. That rules
out anything computed from the clock, the hostname, or any other public input:
this project is public, so any formula we can derive is one the attacker can
derive too. Hence a random token, persisted to a per-user file.

The extension inside PT reads the same file directly (it has filesystem access
through its own API), so there is no pairing step and no window in which the
secret is ever served over HTTP.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import time
from pathlib import Path

# Where the secret lives, and the environment variable that overrides it.
_TOKEN_FILENAME = "bridge_token"
_ENV_VAR = "PT_MCP_BRIDGE_TOKEN"

# 32 bytes of entropy, url-safe encoded. Long enough that an offline guess is
# hopeless; restricted charset so the value survives a query string and a
# plain-text file without escaping.
_MIN_LENGTH = 32
_TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9_-]+$")

# State the caller reports on: a rotated token invalidates any already-paired
# client, and an ephemeral one dies with this process. Both are worth saying
# out loud rather than discovering later.
_rotated = False
_ephemeral = False
_cached: str | None = None


class BridgeTokenError(RuntimeError):
    """The configured token cannot be used."""


def token_dir() -> Path:
    """Directory holding the token: per-user, local to the machine.

    On Windows this is ``%LOCALAPPDATA%`` and deliberately not ``%APPDATA%`` —
    the latter roams with a domain profile, and a loopback secret has no
    business travelling to a file server. On POSIX the XDG state directory is
    used.
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "packet-tracer-mcp"
        return Path.home() / ".packet-tracer-mcp"
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "packet-tracer-mcp"
    return Path.home() / ".local" / "state" / "packet-tracer-mcp"


def token_path() -> Path:
    return token_dir() / _TOKEN_FILENAME


def token_fingerprint(token: str) -> str:
    """A non-invertible handle for a token.

    Lets a status report say *which* token a bridge is using without putting
    the token itself on the wire.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def is_valid(token: str) -> bool:
    return len(token) >= _MIN_LENGTH and bool(_TOKEN_SHAPE.match(token))


def _read_token(path: Path) -> str | None:
    """Read and validate the token on disk; ``None`` if unusable.

    A UTF-8 BOM and surrounding whitespace are tolerated: this is a plain text
    file and someone will eventually open it in Notepad.
    """
    try:
        text = path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text if is_valid(text) else None


def _create_token(path: Path) -> str | None:
    """Create the token file exclusively; ``None`` if another process won.

    ``O_EXCL`` rather than write-temp-then-rename: rename is atomic but
    last-writer-wins, so two servers starting together would end up holding
    *different* tokens. With ``O_EXCL`` the loser simply reads the winner's.
    """
    candidate = secrets.token_urlsafe(32)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return None
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(candidate)
        handle.flush()
        os.fsync(handle.fileno())
    return candidate


def get_bridge_token(refresh: bool = False) -> str:
    """Return the bridge token, creating it on first use.

    Deliberately hard to fail: a corrupt file is rotated, and an unwritable
    directory falls back to a per-process token. A server that refuses to
    start is worse than one that announces the token needs re-pairing.
    """
    global _cached, _rotated, _ephemeral

    override = os.environ.get(_ENV_VAR, "").strip()
    if override:
        # An explicit override is a decision the operator made, so it is
        # validated as strictly as the file rather than more loosely. This one
        # is the variable people reach for in tests and CI, and a short value
        # there would quietly remove the only defence against a hostile page.
        if not is_valid(override):
            raise BridgeTokenError(
                f"{_ENV_VAR} is not usable as a token: it needs at least "
                f"{_MIN_LENGTH} characters from [A-Za-z0-9_-], and it is "
                f"{len(override)} long. Fix it, or unset the variable to use "
                f"the on-disk token."
            )
        return override

    if _cached is not None and not refresh:
        return _cached

    path = token_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        _ephemeral = True
        _cached = secrets.token_urlsafe(32)
        return _cached

    for _ in range(10):
        existing = _read_token(path)
        if existing:
            _cached = existing
            return _cached

        if path.exists():
            # Present but unusable: empty, truncated, or hand-edited. Replace
            # it and remember that we did, because any paired client is now
            # holding a secret that no longer matches.
            try:
                path.unlink()
                _rotated = True
            except OSError:
                break

        created = _create_token(path)
        if created:
            _cached = created
            return _cached
        # Lost the race for the file: whoever won has already written it, so
        # read it back on the next turn.
        time.sleep(0.05)

    _ephemeral = True
    _cached = secrets.token_urlsafe(32)
    return _cached


def token_was_rotated() -> bool:
    """True when the stored token was unusable and got regenerated."""
    return _rotated


def token_is_ephemeral() -> bool:
    """True when the token could not be persisted and dies with this process."""
    return _ephemeral


def reset_cache() -> None:
    """Drop cached state. For tests only."""
    global _cached, _rotated, _ephemeral
    _cached = None
    _rotated = False
    _ephemeral = False
