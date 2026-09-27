"""Live contract tests — the real Packet Tracer, the real bridge.

These tests are only meaningful against a running Packet Tracer with the MCP
Control Center extension installed. They run when either live channel is up:
the HTTP bridge (extension webview polling) or the file mailbox (Script Engine
heartbeat). When neither is reachable they report LIVE TEST BLOCKED and skip —
they are never satisfied by offline behavior, and they are never fake-passed.

§30: every device a test creates is registered and removed on teardown, so the
user's real workspace is never polluted. A leaked device FAILS the run rather
than being silently left behind.
"""

from __future__ import annotations

import time

import pytest

from src.network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter
from src.network_autopilot.core.receipts import OperationResult
from src.network_autopilot.core.requirements import FlowOutcome

import _live_harness as live

pytestmark = pytest.mark.skipif(
    not live.live_is_reachable(),
    reason=(
        "LIVE TEST BLOCKED: Packet Tracer is not reachable through any channel "
        "(HTTP bridge on 127.0.0.1:54321 or the Script Engine file mailbox). "
        "Open Packet Tracer with the MCP Control Center extension installed, "
        "then re-run."
    ),
)


@pytest.fixture
def owned():
    """Devices created by one test; removed verbatim on teardown."""
    created: list[str] = []

    def _track(name: str) -> str:
        if name not in created:
            created.append(name)
        return name

    yield _track
    outcomes = live.delete_devices(created)
    leaked = [n for n, s in outcomes.items() if s != "success"]
    if leaked:
        pytest.fail(f"live test leaked devices into the workspace: {leaked} ({outcomes})")


def _adapter() -> BridgeAdapter:
    # Uses the real per-machine token: these tests talk to the real bridge.
    return BridgeAdapter(host_bridge=False)


def _unique(tracker, prefix: str) -> str:
    return tracker(f"{prefix}-{live.run_tag()}")


def test_live_bridge_is_ours_and_polling():
    info = _adapter().ping(timeout=5.0)
    # The module skipif already proved SOME channel is alive, so ping() must
    # agree no matter which one: an authenticated HTTP bridge reports
    # reachable, and the Script Engine file mailbox reports reachable by
    # design (PT is open with the extension polling it). A not-reachable
    # verdict here would mean ping() contradicts the gate that let us run.
    assert info.reachable, info.detail.value or info.detail.status.value


def test_live_topology_readback_enumerates_devices():
    names = _adapter().list_device_names()
    assert names.is_known, names.detail or names.status.value
    assert isinstance(names.value, list)


def test_live_create_device_confirmed_by_readback(owned):
    adapter = _adapter()
    name = _unique(owned, "ap-live")
    receipt = adapter.create_device(name, "router", model_hint="2911", position=(820, 120))
    assert receipt.result is OperationResult.SUCCESS, receipt.error
    assert receipt.confirmed, receipt.to_dict()
    exists = adapter.device_exists(name)
    assert exists.is_known and exists.value


def test_live_deep_inspection_reads_model_and_ports(owned):
    adapter = _adapter()
    name = _unique(owned, "ap-insp")
    receipt = adapter.create_device(name, "router", model_hint="2911", position=(840, 140))
    assert receipt.result is OperationResult.SUCCESS, receipt.error
    observation = adapter.inspect_device(name)
    assert observation.model.is_known, observation.to_dict()
    assert observation.ports, "a router always has ports; none were read back"


def test_live_delete_device_is_idempotent(owned):
    """§29: the delete controller removes a device and a repeat is a no-op."""
    adapter = _adapter()
    name = _unique(owned, "ap-del")
    assert adapter.create_device(name, "router", model_hint="2911").executed_ok
    first = adapter.delete_device(name)
    assert first.result is OperationResult.SUCCESS, first.error
    assert first.confirmed, first.to_dict()
    # Second delete: already absent must be a CONFIRMED success, not an error.
    second = adapter.delete_device(name)
    assert second.result is OperationResult.SUCCESS, second.error
    # The tracker will also try to delete it on teardown — that must be safe.
    exists = adapter.device_exists(name)
    assert exists.is_known and not exists.value


def test_live_end_to_end_pc_to_pc_ping(owned):
    """Two real PCs, one real link, real static IPs, one REAL ping.

    This is the smallest complete data-plane proof the bridge can produce:
    create -> link -> configure -> flow test, every step read back.
    """
    adapter = _adapter()
    tag = live.run_tag()
    a, b = _unique(owned, f"ap-pa-{tag}"), _unique(owned, f"ap-pb-{tag}")
    for name, y in ((a, 200), (b, 260)):
        r = adapter.create_device(name, "pc", position=(860, y))
        assert r.result is OperationResult.SUCCESS, r.error
    link = adapter.create_link(a, "FastEthernet0", b, "FastEthernet0")
    assert link.executed_ok, link.error
    assert link.confirmed, link.to_dict()  # both endpoints read back a link
    for name, ip in ((a, "192.168.77.10"), (b, "192.168.77.11")):
        r = adapter.configure_host(name, dhcp=False, ip=ip, mask="255.255.255.0")
        assert r.executed_ok, r.error
    # give the addressing a moment to settle, then REALLY ping
    time.sleep(1.0)
    flow = adapter.run_flow_test(a, b, timeout=45.0)
    assert flow.outcome is FlowOutcome.REACHABLE, (
        flow.outcome.value, flow.detail, flow.evidence
    )
    # real PC console stat format: 'Packets: Sent = 4, Received = 4, ...'
    assert "Received = 4" in flow.evidence.get("ping_stat", "")


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
