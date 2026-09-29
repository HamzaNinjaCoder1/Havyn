"""S1.18 / F16 live: the renamed runtime dir against a real Packet Tracer.

The offline tests pin the candidate order, the one-time migration and the
mailbox fallback against a temp dir. What only the real machine can show:

* the engine's runtime dir really is the new ``Havyn/`` dir;
* the engine's mailbox resolves to a real candidate and reaches PT's live
  heartbeat there. The running extension may still be heartbeating into the
  pre-rename ``packet-tracer-mcp/`` dir (it resolved its paths at load), and
  meeting it there is exactly the "old install still works" promise;
* reading the token never rewrites or removes a pre-rename install's token.

The extension half (main.js) cannot be executed from a test; its candidate
order is pinned offline in test_pt_transports.py, and the live half is the
engine meeting whichever dir the running extension actually chose.
"""

from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "tests/autopilot")
import _live_harness as live

from network_autopilot.pt import mailbox as mb
from network_autopilot.pt import token as tk

pytestmark = pytest.mark.skipif(
    not live.live_is_reachable(),
    reason=(
        "LIVE TEST BLOCKED: Packet Tracer is not reachable on either channel "
        "(HTTP bridge on 127.0.0.1:54321, or the Script Engine file mailbox). "
        "Open PT with the MCP Control Center extension and re-run."
    ),
)


def test_engine_reaches_pt_through_the_renamed_runtime_dir():
    candidates = tk._candidate_dirs()
    assert candidates, "there must be at least one runtime dir"
    assert tk.token_dir() == candidates[0]
    assert candidates[0].name in ("Havyn", ".havyn"), candidates[0]

    resolved = mb.mailbox_dir()
    assert any(resolved == c / "bridge" for c in candidates), resolved
    assert mb.FileMailbox().pt_alive() is True, (
        f"the engine's mailbox {resolved} did not meet PT's live heartbeat; "
        f"candidates={candidates}"
    )
    assert tk.is_valid(tk.get_bridge_token())


def test_reading_the_token_never_disturbs_a_pre_rename_install():
    candidates = tk._candidate_dirs()
    if len(candidates) < 2:
        pytest.skip("this platform has no pre-rename candidate dir")
    old_token = candidates[1] / "bridge_token"
    before = old_token.read_bytes() if old_token.exists() else None

    assert tk.is_valid(tk.get_bridge_token())

    if before is not None:
        assert old_token.read_bytes() == before, (
            "reading the token must copy, never move or rewrite a "
            "pre-rename install's file"
        )
