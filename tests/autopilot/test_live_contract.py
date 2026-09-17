"""Live contract tests — the real Packet Tracer, the real bridge.

These tests are only meaningful against a running Packet Tracer with the MCP
Control Center extension installed. They run when either live channel is up:
the HTTP bridge (extension webview polling) or the file mailbox (Script Engine
heartbeat). When neither is reachable they report LIVE TEST BLOCKED with the
observed state of BOTH channels and skip — they are never satisfied by offline
behavior, and they are never fake-passed.

Note: the mutation tests leave their devices in the open workspace on purpose;
they are the evidence that the round trip really happened.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
import uuid

import pytest

from packet_tracer_mcp.infrastructure.execution.file_bridge import FileBridge
from packet_tracer_mcp.infrastructure.execution.live_bridge import DEFAULT_PORT

from src.network_autopilot.adapter.upstream.bridge_adapter import BridgeAdapter
from src.network_autopilot.core.receipts import OperationResult
from src.network_autopilot.core.requirements import FlowOutcome


def _http_bridge_listening() -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{DEFAULT_PORT}/ping", timeout=2.0
        ) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _file_channel_alive() -> bool:
    try:
        return FileBridge().pt_alive()
    except OSError:
        return False


_HTTP_UP = _http_bridge_listening()
_FILE_UP = _file_channel_alive()

pytestmark = pytest.mark.skipif(
    not (_HTTP_UP or _FILE_UP),
    reason=(
        "LIVE TEST BLOCKED: Packet Tracer is not reachable through any channel — "
        f"HTTP bridge on 127.0.0.1:{DEFAULT_PORT}: "
        f"{'listening' if _HTTP_UP else 'nothing listening'}; "
        f"file mailbox heartbeat: {'fresh' if _FILE_UP else 'stale/absent'}. "
        "Open Packet Tracer with the MCP Control Center extension installed "
        "(the Script Engine alone is enough for the file channel), then re-run."
    ),
)


def _adapter() -> BridgeAdapter:
    # Uses the real per-machine token: these tests talk to the real bridge.
    return BridgeAdapter(host_bridge=False)


def test_live_bridge_is_ours_and_polling():
    info = _adapter().ping(timeout=5.0)
    # On the file channel ping() reports not-reachable by design; the guard
    # above already proved SOME channel is alive. Assert honestly per channel.
    if _HTTP_UP:
        assert info.reachable, info.detail.value or info.detail.status.value
    else:
        assert not info.reachable  # file channel: HTTP down, that is expected


def test_live_topology_readback_enumerates_devices():
    names = _adapter().list_device_names()
    assert names.is_known, names.detail or names.status.value
    assert isinstance(names.value, list)


def test_live_create_device_confirmed_by_readback():
    adapter = _adapter()
    name = f"ap-live-{uuid.uuid4().hex[:6]}"
    receipt = adapter.create_device(name, "router", model_hint="2911", position=(820, 120))
    assert receipt.result is OperationResult.SUCCESS, receipt.error
    assert receipt.confirmed, receipt.to_dict()
    exists = adapter.device_exists(name)
    assert exists.is_known and exists.value


def test_live_deep_inspection_reads_model_and_ports():
    adapter = _adapter()
    name = f"ap-insp-{uuid.uuid4().hex[:6]}"
    receipt = adapter.create_device(name, "router", model_hint="2911", position=(840, 140))
    assert receipt.result is OperationResult.SUCCESS, receipt.error
    observation = adapter.inspect_device(name)
    assert observation.model.is_known, observation.to_dict()
    assert observation.ports, "a router always has ports; none were read back"


def test_live_end_to_end_pc_to_pc_ping():
    """Two real PCs, one real link, real static IPs, one REAL ping.

    This is the smallest complete data-plane proof the bridge can produce:
    create -> link -> configure -> flow test, every step read back.
    """
    adapter = _adapter()
    tag = uuid.uuid4().hex[:6]
    a, b = f"ap-pa-{tag}", f"ap-pb-{tag}"
    for name, y in ((a, 200), (b, 260)):
        r = adapter.create_device(name, "pc", position=(860, y))
        assert r.result is OperationResult.SUCCESS, r.error
    link = adapter.create_link(a, "FastEthernet0", b, "FastEthernet0")
    assert link.executed_ok, link.error
    for name, ip in ((a, "192.168.77.10"), (b, "192.168.77.11")):
        r = adapter.configure_host(name, dhcp=False, ip=ip, mask="255.255.255.0")
        assert r.executed_ok, r.error
    # give the addressing a moment to settle, then REALLY ping
    time.sleep(1.0)
    flow = adapter.run_flow_test(a, b, timeout=30.0)
    assert flow.outcome is FlowOutcome.REACHABLE, flow.to_dict()
    assert "100 percent" in (flow.evidence.get("ping_stat") or "")


def test_live_save_project_lands_on_disk(tmp_path):
    adapter = _adapter()
    receipt = adapter.save_project("ap-live.pkt", directory=str(tmp_path))
    assert receipt.result is OperationResult.SUCCESS, receipt.error
    assert receipt.confirmed, receipt.to_dict()
    assert (tmp_path / "ap-live.pkt").exists()


def test_live_reopen_project():
    """Honest block, not a fake pass: no verified open/read-back JS exists yet.

    reopen stays REPORTED as unsupported until the open path has the same
    in-band read-back proof the save path has.
    """
    receipt = _adapter().open_project("whatever.pkt")
    assert receipt.result is OperationResult.NOT_EXECUTED
    assert "not wired" in (receipt.error or "")
