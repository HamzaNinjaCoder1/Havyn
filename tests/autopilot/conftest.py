"""S1.15 / F12: the --require-live flag.

Live tests skip themselves when Packet Tracer is unreachable, which keeps
the offline suite honest — but a release cut on a machine with a dead PT
would then "pass" without proving anything live. `--require-live` flips
that: with the flag set, an unreachable PT FAILS every live test instead of
skipping it, fast (one bounded reachability check, no test body runs).

Without the flag nothing changes: skips stay skips.
"""

from __future__ import annotations

import pytest

from tests.autopilot import _live_harness as live

_LIVE_TEST_SUBSTRINGS = ("test_live_", "test_scale_live")


def _is_live_item(node) -> bool:
    """A collected item that drives the real Packet Tracer."""
    return any(s in node.nodeid for s in _LIVE_TEST_SUBSTRINGS)


def pytest_addoption(parser):
    parser.addoption(
        "--require-live",
        action="store_true",
        default=False,
        help=(
            "fail (do not skip) live Packet Tracer tests when PT is "
            "unreachable; release runs must prove live, not skip live"
        ),
    )


def pytest_collection_modifyitems(items, config):
    """Strip the unreachable-PT skips so the guard below fails loudly."""
    if not config.getoption("require_live", False):
        return
    for item in items:
        if not _is_live_item(item):
            continue
        node = item
        seen: set[int] = set()
        while node is not None and id(node) not in seen:
            seen.add(id(node))
            own = getattr(node, "own_markers", None)
            if own is not None:
                node.own_markers = [
                    m for m in own if m.name not in ("skip", "skipif")
                ]
            node = getattr(node, "parent", None)


@pytest.fixture(autouse=True)
def _require_live_guard(request):
    _guard(request)


def _guard(request):
    """Fail a live test fast when PT is unreachable under --require-live."""
    if not request.config.getoption("require_live", False):
        return
    if not _is_live_item(request.node):
        return
    if not live.live_is_reachable():
        pytest.fail(
            "--require-live: Packet Tracer is not reachable on any channel "
            "(HTTP bridge or Script Engine file mailbox); refusing to skip "
            "this live test",
            pytrace=False,
        )
