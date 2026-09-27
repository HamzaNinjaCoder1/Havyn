"""Shared harness for live Packet Tracer tests.

Two guarantees every live test must offer:

1. BOUNDED WAITS — a live test must never hang the suite. PT detection and
   every bridge call carry an explicit deadline.
2. NO WORKSPACE POLLUTION — every device a test creates is tracked and
   removed on teardown, and pre-existing user devices are never touched.

The harness namespaces all test devices (`q<short>-...`) so they cannot
collide with a real user topology, and deletes exactly the names it created.

Namespacing is a property of the tests that use this harness, not of the
engine: a build run through `net_build()` or the MCP tools creates the raw ids
its spec names (`r1`, `pc1`, `redge`). Cleanup that only ever looks for the
`q<short>-` prefix therefore deletes nothing after such a build, which is how
devices survived into the user's workspace. Every cleanup or leak check must
start from the ids in the spec - `owned_device_names()` below - and accept both
the raw and the namespaced form.
"""

from __future__ import annotations

import time
import uuid

# A live test that cannot finish within this wall clock is treated as a
# failure, not an infinite hang.
LIVE_TEST_DEADLINE_SECONDS = 600


def http_bridge_listening(port: int = 54321, timeout: float = 2.0) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def file_channel_alive(timeout: float = 2.0) -> bool:
    try:
        from network_autopilot.pt.mailbox import FileMailbox as FileBridge

        return FileBridge().pt_alive()
    except Exception:
        return False


def live_is_reachable() -> bool:
    """True only if PT answers on some channel within a bounded time."""
    return http_bridge_listening() or file_channel_alive()


def run_tag() -> str:
    """A short unique prefix so every run owns its own device namespace."""
    return f"q{uuid.uuid4().hex[:6]}"


def make_adapter():
    """A live adapter on the file channel (or hosted HTTP when available)."""
    from src.network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter
    from network_autopilot.pt.mailbox import FileMailbox as FileBridge

    return BridgeAdapter(host_bridge=False, file_bridge=FileBridge())


def delete_devices(names: list[str], timeout: float = 240.0) -> dict[str, str]:
    """Best-effort removal of exactly the named devices.

    Returns per-name outcomes. This is the ONLY cleanup path: it never deletes
    anything it was not handed, so a user's own devices are untouched.

    The budget starts here, NOT at the caller's deadline. Cleanup runs in a
    test's ``finally`` block after a full build has already consumed most of
    the suite's wall clock; sharing that deadline made every delete report
    "timeout" and leaked real devices into the user's workspace. A leaked
    device is worse than a slow test, so teardown always gets a fresh budget.
    """
    outcomes: dict[str, str] = {}
    if not names:
        return outcomes
    # A single failed adapter construction must not skip cleanup entirely: that
    # is how a device outlives its test and then captures a later run's
    # wireless client. Retry construction on its own budget.
    adapter = None
    for _ in range(3):
        try:
            adapter = make_adapter()
            break
        except Exception as exc:  # transient channel hiccup
            time.sleep(2.0)
    if adapter is None:
        return {name: "no-channel" for name in names}
    # PT is still busy executing the build this cleanup follows; give the
    # channel a moment to drain before the first delete.
    time.sleep(2.0)
    deadline = time.time() + timeout
    # One round-trip for the whole set. Deleting device-by-device overran the
    # budget on a sixty-device build and leaked the rest into the workspace,
    # which made every later PT run slow enough to time out.
    try:
        for name, outcome in adapter.delete_devices_bulk(names).items():
            outcomes[name] = "success" if outcome == "deleted" else outcome
    except Exception as exc:  # cleanup must never raise into the test
        outcomes.update({name: f"error:{type(exc).__name__}" for name in names})
    for attempt in range(2):
        outstanding = [n for n in names if outcomes.get(n) != "success"]
        if not outstanding:
            break
        for name in outstanding:
            if time.time() > deadline:
                outcomes.setdefault(name, "timeout")
                continue
            try:
                receipt = adapter.delete_device(name)
                outcomes[name] = receipt.result.value
            except Exception as exc:  # cleanup must never raise into the test
                outcomes[name] = f"error:{type(exc).__name__}"
    return outcomes


def list_owned_devices(prefix: str) -> list[str]:
    """Every live device whose name starts with this run's namespace."""
    return [name for name in _live_names() if name.startswith(prefix)]


def owned_device_names(spec_ids, prefix: str | None = None) -> list[str]:
    """Every name a build of these spec ids may have created (#40).

    A namespaced run creates ``<prefix><id>``, a raw run creates ``<id>``. A
    cleanup that only knows one of the two silently deletes nothing, so both
    are always handed to the delete path; they are distinct exact names, never
    a wildcard, so a user device that happens to share a prefix is untouched.
    """
    ids = {str(i) for i in spec_ids}
    names = set(ids)
    if prefix:
        names |= {f"{prefix}{i}" for i in ids}
    return sorted(names)


def delete_spec_devices(spec_ids, prefix: str | None = None, timeout: float = 240.0):
    """Teardown that starts from the spec's ids instead of a name prefix (#40).

    Both forms are deleted because a build may have run either way; names that
    are not in the workspace come back as a normal not-found outcome.
    """
    return delete_devices(owned_device_names(spec_ids, prefix), timeout)


def leaked_spec_devices(spec_ids, prefix: str | None = None) -> list[str]:
    """Owned names that are still in the workspace after teardown.

    An empty list means the test cleaned up after itself; anything else is a
    leak that will slow every later Packet Tracer run.
    """
    present = set(_live_names())
    return sorted(present & set(owned_device_names(spec_ids, prefix)))


def _live_names() -> list[str]:
    try:
        adapter = make_adapter()
        devices = adapter._live_devices()
        if not devices.is_known:
            return []
        return [str(d.get("name") or "") for d in devices.value]
    except Exception:
        return []
