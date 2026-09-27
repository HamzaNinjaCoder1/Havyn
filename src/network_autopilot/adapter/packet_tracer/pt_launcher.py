"""Launch Packet Tracer on demand so the engine is never blocked by a closed GUI.

The Script Engine extension only heartbeats while Packet Tracer is running, so
without this the live channel is unreachable until someone opens PT by hand.
`ensure_packet_tracer()` turns "PT must already be open" into "PT will be opened
for you": it locates the install, spawns the documented executable, and waits
(bounded) for the Script Engine's existing heartbeat to go fresh.

Deliberately does NOT touch any PT API: it only spawns the executable and reads
the existing `alive.txt` heartbeat. No bridge endpoint is added and no token is
involved — launching a GUI does not grant bridge access, which still needs the
authenticated channel from `bridge_adapter.py`.

Env overrides (checked by `find_packet_tracer` in this order):
  PT_EXE_PATH        absolute path to PacketTracer.exe, used verbatim.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

# Polling cadence while waiting for the Script Engine heartbeat after spawn.
_HEARTBEAT_POLL_S = 1.0

# Launching a cold PT + loading the extension can take a while on a slow disk;
# the caller's deadline is the real bound, this is just the default.
DEFAULT_LAUNCH_TIMEOUT_S = 120.0

_PROCESS_NAME = "PacketTracer.exe"


@dataclass(frozen=True)
class LaunchResult:
    """Outcome of an on-demand launch attempt (a receipt, not an exception)."""

    status: str
    exe: str | None = None
    elapsed_s: float = 0.0
    detail: str = ""

    # The statuses form a total order of usefulness for the caller.
    ALIVE = "alive"            # already heartbeating; nothing was spawned
    LAUNCHED = "launched"      # spawned and heartbeat went fresh in time
    TIMEOUT = "timeout"        # spawned, but the heartbeat never freshened
    SPAWN_FAILED = "spawn_failed"  # the executable could not be started
    MISSING = "missing"        # no PacketTracer.exe could be located
    DISABLED = "disabled"      # spawning was not requested and PT was not alive

    @property
    def usable(self) -> bool:
        """True when the Script Engine is heartbeating as a result of this call."""
        return self.status in (LaunchResult.ALIVE, LaunchResult.LAUNCHED)

    @property
    def spawned(self) -> bool:
        """A GUI process was actually started (distinct from finding it alive)."""
        return self.status in (LaunchResult.LAUNCHED, LaunchResult.TIMEOUT)


# -- discovery -------------------------------------------------------------

def find_packet_tracer() -> Path | None:
    """Locate PacketTracer.exe without guessing: env override, registry, then glob.

    Returns the first existing executable, or None when nothing is installed.
    Never raises — a missing install is a reportable result, not an exception.
    """
    override = os.environ.get("PT_EXE_PATH", "").strip()
    if override:
        candidate = Path(override)
        if candidate.is_file():
            return candidate
        # Fall through: a stale PT_EXE_PATH should not hide a real install.

    exe = None
    try:
        exe = _from_registry()
    except OSError:
        # A corrupt or unreadable registry key must not break discovery: fall
        # through to the filesystem scan, which is the authoritative check.
        exe = None
    if exe is not None:
        return exe

    return _from_glob()


def _from_registry() -> Path | None:
    """InstallDir under the Cisco Packet Tracer keys (try both 64/32-bit views)."""
    try:
        import winreg
    except ImportError:  # pragma: no cover - non-Windows dev box
        return None
    keys = (
        r"SOFTWARE\Cisco Systems\PacketTracer",
        r"SOFTWARE\WOW6432Node\Cisco Systems\PacketTracer",
        r"SOFTWARE\Cisco Packet Tracer",
        r"SOFTWARE\WOW6432Node\Cisco Packet Tracer",
    )
    for key in keys:
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, key) as handle:
                    install_dir, _ = winreg.QueryValueEx(handle, "InstallDir")
            except OSError:
                continue
            if not install_dir:
                continue
            # InstallDir may be the bin dir or its parent.
            for base in (Path(install_dir), Path(install_dir) / "bin"):
                exe = base / _PROCESS_NAME
                if exe.is_file():
                    return exe
    return None


def _from_glob() -> Path | None:
    """Scan the usual install roots. Sorted so a newer version wins ties."""
    roots: list[str] = []
    for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA", "SystemDrive"):
        value = os.environ.get(var, "").strip()
        if value:
            roots.append(value)
    # Also probe the raw drive in case the env vars are stripped.
    roots.append(os.environ.get("SystemDrive", "C:") + "\\")

    found: list[Path] = []
    for root in dict.fromkeys(roots):
        base = Path(root)
        if not base.is_dir():
            continue
        # C:\Program Files\Cisco Packet Tracer 9.0.1\bin\PacketTracer.exe
        found.extend(base.glob("Cisco Packet Tracer*/bin/" + _PROCESS_NAME))
        found.extend(base.glob("Packet Tracer*/bin/" + _PROCESS_NAME))
        # LOCALAPPDATA variant: ...\Programs\Cisco Packet Tracer*\bin\...
        found.extend(base.glob("Programs/Cisco Packet Tracer*/bin/" + _PROCESS_NAME))
    if not found:
        return None
    # Highest version last wins when names differ; same name keeps first hit.
    return sorted(found, key=lambda p: p.parent.parent.name)[-1]


def packet_tracer_running() -> bool:
    """True if a PacketTracer.exe process exists. Cheap pre-check, no throw."""
    if os.name != "nt":  # pragma: no cover - non-Windows dev box
        return False
    try:
        proc = subprocess.run(
            ("tasklist", "/FI", f"IMAGENAME eq {_PROCESS_NAME}", "/NH", "/FO", "CSV"),
            capture_output=True,
            timeout=10.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return _PROCESS_NAME.lower() in proc.stdout.decode("utf-8", "replace").lower()


# -- launch ----------------------------------------------------------------

def ensure_packet_tracer(
    *,
    alive,
    exe: Path | None = None,
    timeout: float = DEFAULT_LAUNCH_TIMEOUT_S,
    spawn: bool = True,
    poll: float = _HEARTBEAT_POLL_S,
    spawner=None,
) -> LaunchResult:
    """Guarantee the Script Engine is heartbeating, launching PT when needed.

    `alive` is a zero-arg callable returning whether the heartbeat is fresh
    (FileBridge.pt_alive): passed in so this module owns no transport state and
    tests can flip the heartbeat deterministically. `spawner` is the process
    spawn hook (defaults to a detached subprocess.Popen) so tests never spawn a
    real GUI.

    Idempotent: when the heartbeat is already fresh nothing is spawned and
    ALIVE is returned. When `spawn` is False and PT is not alive the result is
    DISABLED rather than a launch — callers that only want to *observe* never
    start a GUI.
    """
    started = time.time()
    if alive():
        return LaunchResult(LaunchResult.ALIVE, elapsed_s=0.0,
                            detail="Script Engine heartbeat already fresh")

    if not spawn:
        return LaunchResult(LaunchResult.DISABLED, elapsed_s=0.0,
                            detail="auto-launch disabled and PT is not alive")

    resolved = exe if exe is not None else find_packet_tracer()
    if resolved is None:
        return LaunchResult(
            LaunchResult.MISSING, elapsed_s=time.time() - started,
            detail="PacketTracer.exe not found (set PT_EXE_PATH to locate it)",
        )

    spawn_fn = spawner or _spawn_default
    try:
        spawn_fn(resolved)
    except OSError as exc:
        return LaunchResult(
            LaunchResult.SPAWN_FAILED, exe=str(resolved), elapsed_s=time.time() - started,
            detail=f"spawn failed: {exc}",
        )

    # Bounded wait for the extension to load and start heartbeating. A hung PT
    # load must not hang the caller: the deadline is absolute.
    deadline = started + max(timeout, 0.0)
    while time.time() < deadline:
        remaining = deadline - time.time()
        time.sleep(min(poll, remaining) if poll > 0 else 0.0)
        if alive():
            return LaunchResult(
                LaunchResult.LAUNCHED, exe=str(resolved), elapsed_s=time.time() - started,
                detail="Packet Tracer launched; Script Engine heartbeat is fresh",
            )
    return LaunchResult(
        LaunchResult.TIMEOUT, exe=str(resolved), elapsed_s=time.time() - started,
        detail=(
            "Packet Tracer was launched but the Script Engine heartbeat never "
            "went fresh — is the MCP Control Center extension installed and "
            "enabled? (channel still unusable)"
        ),
    )


def _spawn_default(exe: Path) -> None:
    """Start PT detached: the GUI outlives this process and we never wait on it.

    DETACHED_PROCESS so a Ctrl-C on the server does not kill the user's PT.
    """
    flags = 0
    if os.name == "nt":
        # CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS
        flags = 0x00000008 | 0x00000200
    subprocess.Popen(
        [str(exe)],
        close_fds=True,
        creationflags=flags,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
