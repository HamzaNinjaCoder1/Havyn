"""configure_ios_bulk: many devices, ONE push / ONE settle / ONE snapshot.

Measured on the reference build (43 devices): configure_ios was 236 s for 13
devices. Each serial call pays a fresh topology snapshot read — measured
2.7–66.4 s on the idle canvas — plus its own 2.5 s settle, and a switch pays a
SECOND fresh read for the VLAN table. The devices' configs are independent, so
the transport can carry them together.

Only the transport. Every semantic of ``configure_ios`` has to survive the
batch or the batch is a lie:

* an absent device is refused by name and never pushed (precondition);
* addresses are read back — a known snapshot without the address is FAILED,
  bounded by the same ``_IOS_CONFIG_ATTEMPTS`` as the serial path;
* the channel failing is UNKNOWN with the channel's detail — never a verdict
  that blames the device (rule 7);
* VLAN and route read-backs still run per device (rule 8).
"""

from __future__ import annotations

import json
import re
import time

import pytest

from src.network_autopilot.adapter.packet_tracer import bridge_adapter as bridge_mod
from src.network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter
from src.network_autopilot.core.observability import Observable
from src.network_autopilot.core.receipts import OperationResult

TEST_TOKEN = "test-token-long-enough-0123456789abcdef"

_ROUTER_CMDS = [
    "interface GigabitEthernet0/0",
    "ip address 10.0.0.1 255.255.255.252",
    "no shutdown",
    "exit",
    "ip route 0.0.0.0 0.0.0.0 10.0.0.2",
]
_ROUTER2_CMDS = [
    "interface GigabitEthernet0/0",
    "ip address 10.0.0.5 255.255.255.252",
    "no shutdown",
    "exit",
    "ip route 10.20.0.0 255.255.0.0 10.0.0.6",
]
_SWITCH_CMDS = [
    "vlan 10",
    "name users",
    "exit",
    "interface FastEthernet0/1",
    "switchport mode access",
    "switchport access vlan 10",
]

_R1_SNAP = {"name": "r1", "model": "2911",
            "ports": [{"name": "GigabitEthernet0/0", "ip": "10.0.0.1"}]}
_R2_SNAP = {"name": "r2", "model": "2911",
            "ports": [{"name": "GigabitEthernet0/0", "ip": "10.0.0.5"}]}
_R2_NO_ADDR = {"name": "r2", "model": "2911", "ports": [{"name": "GigabitEthernet0/0"}]}

_R1_ROUTES = Observable.known([
    {"network": "0.0.0.0", "prefix": 0, "next_hop": "10.0.0.2"},
])
_R2_ROUTES = Observable.known([
    {"network": "10.20.0.0", "prefix": 16, "next_hop": "10.0.0.6"},
])


def _snap(*devices) -> Observable[list[dict]]:
    return Observable.known(list(devices))


class _PT:
    """Fake transport answering by intent.

    Records what the adapter did — pushes (and which devices each push
    carried), fresh snapshot reads, settles — so a test can assert the
    batching actually happened, in the right order, exactly once.
    """

    def __init__(
        self,
        *,
        present: set[str] | None = None,
        snapshots: list[Observable] | None = None,
        push_answers: list[Observable] | None = None,
        omit_rows: set[str] | None = None,
        routes: dict[str, Observable] | None = None,
        vlans: dict[str, Observable] | None = None,
    ) -> None:
        self.present = present if present is not None else {"r1", "r2"}
        self.snapshots = snapshots or [_snap(_R1_SNAP, _R2_SNAP)]
        self.push_answers = push_answers  # None => every push answers rows
        self.omit_rows = omit_rows or set()
        self.routes = routes or {"r1": _R1_ROUTES, "r2": _R2_ROUTES}
        self.vlans = vlans or {}
        self.log: list[str] = []            # "push" / "settle" / "read"
        self.pushes: list[list[str]] = []   # devices carried by each push
        self.state = {"push": 0, "read": 0}
        self.vlan_reads: list[str] = []
        self.route_reads: list[str] = []

    def adapter(self, monkeypatch) -> BridgeAdapter:
        adapter = BridgeAdapter(port=59999, token=TEST_TOKEN, host_bridge=False)
        monkeypatch.setattr(time, "sleep",
                            lambda s: self.log.append("settle"), raising=False)
        adapter.device_exists = (  # type: ignore[method-assign]
            lambda name: Observable.known(name in self.present)
        )

        def fake_send(expression, *, statements=None, wait=None):
            text = " ".join(statements or []) + " " + str(expression)
            if "__mcpCfgResults" in text:
                self.log.append("push")
                pushed = re.findall(r'configureIosDevice\("([^"]+)"', text)
                self.pushes.append(pushed)
                self.state["push"] += 1
                if self.push_answers is not None:
                    answer = self.push_answers[
                        min(self.state["push"] - 1, len(self.push_answers) - 1)
                    ]
                    if not answer.is_known:
                        return answer
                rows = [
                    {"device": d, "ok": True}
                    for d in pushed if d not in self.omit_rows
                ]
                return Observable.known(json.dumps(rows))
            # The route-retry re-push is a plain configureIosDevice expression.
            if "configureIosDevice" in text:
                self.log.append("push")
                self.pushes.append(re.findall(r'configureIosDevice\("([^"]+)"', text))
                return Observable.known("true")
            return Observable.known("OK")

        adapter.send_expression = fake_send  # type: ignore[method-assign]

        def fake_live(*, fresh=True):
            if fresh:
                self.log.append("read")
                obs = self.snapshots[
                    min(self.state["read"], len(self.snapshots) - 1)
                ]
                self.state["read"] += 1
                return obs
            return self.snapshots[0]

        monkeypatch.setattr(adapter, "_live_devices", fake_live)
        monkeypatch.setattr(
            adapter, "read_routing_table",
            lambda device: (self.route_reads.append(device),
                            self.routes.get(device, Observable.known([])))[1],
        )
        monkeypatch.setattr(
            adapter, "read_vlans",
            lambda device: (self.vlan_reads.append(device),
                            self.vlans.get(device, Observable.known([])))[1],
        )
        return adapter


def _spec(device: str, commands: list[str]) -> dict:
    return {"device": device, "commands": commands}


# ---------------------------------------------------------------------------
# The batching itself: one push, one settle, one shared snapshot read
# ---------------------------------------------------------------------------


def test_bulk_configure_is_one_push_one_settle_one_shared_snapshot(monkeypatch):
    """The whole point of the batch (rule 10: claim exactly what runs).

    Serial configure_ios pays, per device: push + settle + a FRESH topology
    snapshot (measured 2.7–66.4 s each). Two devices must cost exactly one of
    each — not two.
    """
    pt = _PT()
    adapter = pt.adapter(monkeypatch)

    receipts = adapter.configure_ios_bulk([
        _spec("r1", _ROUTER_CMDS),
        _spec("r2", _ROUTER2_CMDS),
    ])

    assert len(receipts) == 2
    for receipt in receipts:
        assert receipt.result is OperationResult.SUCCESS, receipt.error
        assert receipt.confirmed, receipt.postconditions
    assert pt.log == ["push", "settle", "read"], (
        "exactly one push, one settle, one snapshot read — serial configure "
        f"pays those per device; got {pt.log}"
    )
    assert pt.pushes[0] == ["r1", "r2"], (
        f"one JS push must carry every device, got {pt.pushes}"
    )


def test_bulk_configure_reads_back_every_address_from_the_shared_snapshot(monkeypatch):
    """The batch's read-back is the same truth the serial one asks for: a
    device whose address is absent from the known snapshot FAILS while the
    device that took its address stays green."""
    pt = _PT(snapshots=[_snap(_R1_SNAP, _R2_NO_ADDR)])
    adapter = pt.adapter(monkeypatch)

    receipts = adapter.configure_ios_bulk([
        _spec("r1", _ROUTER_CMDS),
        _spec("r2", _ROUTER2_CMDS),
    ])
    by_device = {r.requested["device"]: r for r in receipts}

    assert by_device["r1"].result is OperationResult.SUCCESS, by_device["r1"].error
    r2 = by_device["r2"]
    assert r2.result is OperationResult.FAILED, r2.result
    assert "not observed after" in (r2.error or ""), r2.error
    addr = next(p for p in r2.postconditions
                if p.name == "configured_addresses_present")
    assert addr.passed is False


def test_bulk_configure_repushes_only_the_device_that_missed_its_address(monkeypatch):
    """Bounded recovery with the serial semantics: only the device that did
    not take its address is re-pushed (idempotent), settled, and re-read —
    the healthy device is neither re-pushed nor re-read."""
    pt = _PT(snapshots=[
        _snap(_R1_SNAP, _R2_NO_ADDR),   # first read: r2 missed
        _snap(_R1_SNAP, _R2_SNAP),      # after the re-push: applied
    ])
    adapter = pt.adapter(monkeypatch)

    receipts = adapter.configure_ios_bulk([
        _spec("r1", _ROUTER_CMDS),
        _spec("r2", _ROUTER2_CMDS),
    ])
    by_device = {r.requested["device"]: r for r in receipts}

    assert by_device["r1"].result is OperationResult.SUCCESS
    assert by_device["r2"].result is OperationResult.SUCCESS, by_device["r2"].error
    assert pt.pushes[1] == ["r2"], (
        f"the recovery re-pushes only the missing device, got {pt.pushes}"
    )
    assert by_device["r2"].evidence.get("attempts") == "2"
    assert by_device["r1"].evidence.get("attempts") == "1"


def test_bulk_configure_missing_address_fails_after_the_same_bounded_attempts(monkeypatch):
    """No weakening: the serial path re-sends at most ``_IOS_CONFIG_ATTEMPTS``
    times and then reports the address FAILED. The batch must do the same —
    no fourth push, no silent green."""
    pt = _PT(snapshots=[_snap(_R1_SNAP, _R2_NO_ADDR)])  # never takes the address
    adapter = pt.adapter(monkeypatch)

    receipts = adapter.configure_ios_bulk([
        _spec("r1", _ROUTER_CMDS),
        _spec("r2", _ROUTER2_CMDS),
    ])
    by_device = {r.requested["device"]: r for r in receipts}

    r2 = by_device["r2"]
    assert r2.result is OperationResult.FAILED
    assert f"not observed after {bridge_mod._IOS_CONFIG_ATTEMPTS} attempt(s)" in (
        r2.error or ""
    ), r2.error
    assert by_device["r1"].result is OperationResult.SUCCESS
    assert len(pt.pushes) == bridge_mod._IOS_CONFIG_ATTEMPTS, (
        f"bounded re-push rounds, got {len(pt.pushes)}: {pt.pushes}"
    )
    for recovery in pt.pushes[1:]:
        assert recovery == ["r2"], (
            f"recovery rounds touch only the missing device, got {recovery}"
        )


# ---------------------------------------------------------------------------
# Honest verdicts (rule 7): the channel failing is UNKNOWN, never a device lie
# ---------------------------------------------------------------------------


def test_bulk_configure_dark_channel_is_unknown_and_never_blames_the_device(monkeypatch):
    """The batch that never reached PT (all sends dark) is UNKNOWN with the
    channel's detail, after the SAME bounded re-send the serial path performs.
    A receipt that says 'address not observed' for a push that never left the
    machine blames the device for our channel."""
    dark = Observable[str].unknown(
        "no live channel: the file mailbox heartbeat is stale"
    )
    pt = _PT(push_answers=[dark])
    adapter = pt.adapter(monkeypatch)

    receipts = adapter.configure_ios_bulk([
        _spec("r1", _ROUTER_CMDS),
        _spec("r2", _ROUTER2_CMDS),
    ])

    assert len(receipts) == 2
    for receipt in receipts:
        assert receipt.result is OperationResult.UNKNOWN, receipt.result
        assert "never reached PT" in (receipt.error or ""), receipt.error
        assert "not observed" not in (receipt.error or ""), receipt.error
        assert not receipt.confirmed
    assert len(pt.pushes) == bridge_mod._IOS_CONFIG_ATTEMPTS, (
        "the dark channel gets the serial path's bounded re-send — exactly "
        f"{bridge_mod._IOS_CONFIG_ATTEMPTS} pushes, got {len(pt.pushes)}"
    )
    assert pt.log.count("read") == 0, (
        "nothing was pushed, so nothing is read back — a snapshot read here "
        "would be theatre"
    )


def test_bulk_configure_absent_device_is_refused_by_name_and_never_pushed(monkeypatch):
    """Refuse by name, never half-configure (rule 9): the missing device gets
    the serial path's precondition receipt and its config never enters any
    push; the present device is still configured."""
    pt = _PT(present={"r1"})
    adapter = pt.adapter(monkeypatch)

    receipts = adapter.configure_ios_bulk([
        _spec("ghost", _ROUTER_CMDS),
        _spec("r1", _ROUTER_CMDS),
    ])
    by_device = {r.requested["device"]: r for r in receipts}

    ghost = by_device["ghost"]
    assert ghost.result is OperationResult.REJECTED, ghost.result
    assert ghost.error and "device missing or unknown" in ghost.error
    pushed = {d for batch in pt.pushes for d in batch}
    assert "ghost" not in pushed, f"a refused device was pushed anyway: {pt.pushes}"
    assert by_device["r1"].result is OperationResult.SUCCESS, by_device["r1"].error


def test_bulk_configure_names_pt_that_answered_without_a_row(monkeypatch):
    """PT ran the batch and said nothing about r2 — that is PT's omission,
    reported for what it is, not an address verdict."""
    pt = _PT(omit_rows={"r2"})
    adapter = pt.adapter(monkeypatch)

    receipts = adapter.configure_ios_bulk([
        _spec("r1", _ROUTER_CMDS),
        _spec("r2", _ROUTER2_CMDS),
    ])
    by_device = {r.requested["device"]: r for r in receipts}

    assert by_device["r1"].result is OperationResult.SUCCESS
    r2 = by_device["r2"]
    assert r2.result is OperationResult.FAILED, r2.result
    assert "without a result for this device" in (r2.error or ""), r2.error


# ---------------------------------------------------------------------------
# The read-backs survive the batch (rule 8): routes and VLANs, per device
# ---------------------------------------------------------------------------


def test_bulk_configure_still_reads_routes_and_vlans_per_device(monkeypatch):
    """Batching merges the PUSH, not the proof: the router's `show ip route`
    and the switch's `show vlan brief` still run — each for exactly the
    devices that have something to read."""
    pt = _PT(
        present={"r1", "s1"},
        snapshots=[_snap(
            _R1_SNAP,
            {"name": "s1", "model": "2960",
             "ports": [{"name": "FastEthernet0/1"}]},
        )],
        routes={"r1": _R1_ROUTES},
        vlans={"s1": Observable.known([
            {"id": 10, "name": "users", "ports": ["Fa0/1"]},
        ])},
    )
    adapter = pt.adapter(monkeypatch)

    receipts = adapter.configure_ios_bulk([
        _spec("r1", _ROUTER_CMDS),
        _spec("s1", _SWITCH_CMDS),
    ])
    by_device = {r.requested["device"]: r for r in receipts}

    assert pt.route_reads == ["r1"], (
        f"the router's route table is read back, got {pt.route_reads}"
    )
    assert pt.vlan_reads == ["s1"], (
        f"the switch's vlan table is read back (and only the switch), got {pt.vlan_reads}"
    )
    names_r1 = [p.name for p in by_device["r1"].postconditions]
    names_s1 = [p.name for p in by_device["s1"].postconditions]
    assert "configured_routes_present" in names_r1, names_r1
    assert "configured_vlans_present" in names_s1, names_s1
    for receipt in receipts:
        assert receipt.result is OperationResult.SUCCESS, receipt.error
