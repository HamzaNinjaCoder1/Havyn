"""S1.7 — trunk read-back from what PT actually prints (F-4/F-5).

Measured live (build-ee447a04376b and the S1.1 raw capture): PT's
`show vlan brief` OMITS trunk ports from every membership list, so the
postcondition demanding allowed VLANs on the trunk port could never pass —
three VLAN ops failed on it and their dependents were blocked. The router
side of a router-on-a-stick has no `show vlan brief` at all; its topology
read DOES carry the subinterfaces (live probe: rhq GigabitEthernet0/0.10/.20/
.30 with addresses), which is the honest evidence for that trunk.
"""

from __future__ import annotations

import json

from src.network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter
from src.network_autopilot.core.observability import Observable
from src.network_autopilot.core.receipts import OperationResult

TEST_TOKEN = "test-token-long-enough-0123456789abcdef"

# PT-real `show vlan brief` for a switch whose Fa0/2 is a trunk: the port is
# in NO membership list (S1.1 capture: Fa0/1, Fa0/2, Gig0/1 absent; unused
# ports sit in VLAN 1).
TRUNK_PORT_ABSENT_FROM_TABLE = """VLAN Name                             Status    Ports
---- -------------------------------- --------- -------------------------------
1    default                          active    Fa0/1
10   users                            active    Fa0/3
20   voice                            active    Fa0/4
1002 fddi-default                     act/unsup
"""

_TRUNK_PUSH = [
    "hostname s1",
    "vlan 10", " name users", " exit",
    "vlan 20", " name voice", " exit",
    "interface FastEthernet0/2",
    " switchport mode trunk",
    " switchport trunk allowed vlan 10,20",
    " exit",
]


def _switch_with_table(monkeypatch, table: str) -> BridgeAdapter:
    from src.network_autopilot.adapter.packet_tracer.js_commands import (
        parse_show_vlan_brief,
    )

    adapter = BridgeAdapter(port=59999, token=TEST_TOKEN, host_bridge=False)
    monkeypatch.setattr(
        adapter,
        "read_vlans",
        lambda device: Observable[list].known(parse_show_vlan_brief(table)),
    )

    match = {
        "name": "s1",
        "model": "2960-24TT",
        "ports": [
            {"name": "FastEthernet0/2", "up": True, "ip": "", "mask": "",
             "linkClass": "Cable", "peers": ["rhq"]},
        ],
    }

    def fake_send(expression, *, statements=None, wait=None):
        text = " ".join(statements or []) + " " + str(expression)
        if "JSON.stringify" in text and "devices" in text:
            return Observable.known(json.dumps({"devices": [match]}))
        return Observable.known("OK")

    adapter.send_expression = fake_send  # type: ignore[method-assign]
    return adapter


def test_a_trunk_absent_from_every_membership_list_confirms(monkeypatch):
    """Failing-first (F-4): PT prints trunks NOWHERE in `show vlan brief`,
    so absence is the evidence, not a contradiction of the push."""
    adapter = _switch_with_table(monkeypatch, TRUNK_PORT_ABSENT_FROM_TABLE)
    receipt = adapter.configure_ios("s1", _TRUNK_PUSH)
    check = next(
        c for c in receipt.postconditions if c.name == "configured_vlans_present"
    )
    assert check.passed is True, json.dumps(check.actual.value)
    assert receipt.result is OperationResult.SUCCESS, receipt.error


def test_a_trunk_push_that_lands_in_one_access_vlan_still_fails(monkeypatch):
    """The other half stays honest: a port that IS listed under a single VLAN
    is behaving as access — the push did not take effect."""
    table = """VLAN Name                             Status    Ports
---- -------------------------------- --------- -------------------------------
1    default                          active    Fa0/1
10   users                            active    Fa0/2, Fa0/3
1002 fddi-default                     act/unsup
"""
    adapter = _switch_with_table(monkeypatch, table)
    receipt = adapter.configure_ios("s1", _TRUNK_PUSH)
    check = next(
        c for c in receipt.postconditions if c.name == "configured_vlans_present"
    )
    assert check.passed is False
    assert receipt.result is OperationResult.FAILED
    assert "FastEthernet0/2" in json.dumps(check.actual.value)


_RHQ_TOPOLOGY = {
    "devices": [
        {
            "name": "rhq",
            "model": "2911",
            "ports": [
                {"name": "GigabitEthernet0/0", "ip": "0.0.0.0",
                 "mask": "0.0.0.0", "up": True, "linked": True},
                {"name": "GigabitEthernet0/0.10", "ip": "10.10.10.1",
                 "mask": "255.255.255.0", "up": True, "linked": False},
                {"name": "GigabitEthernet0/0.20", "ip": "10.10.20.1",
                 "mask": "255.255.255.0", "up": True, "linked": False},
                {"name": "GigabitEthernet0/0.30", "ip": "10.10.30.1",
                 "mask": "255.255.255.0", "up": True, "linked": False},
                {"name": "GigabitEthernet0/1", "ip": "10.0.0.6",
                 "mask": "255.255.255.252", "up": True, "linked": True},
            ],
        }
    ]
}


def _router_adapter() -> BridgeAdapter:
    adapter = BridgeAdapter(port=59999, token=TEST_TOKEN, host_bridge=False)

    def fake_send(expression, *, statements=None, wait=None):
        text = " ".join(statements or []) + " " + str(expression)
        if "JSON.stringify" in text and "devices" in text:
            return Observable.known(json.dumps(_RHQ_TOPOLOGY))
        return Observable.known("OK")

    adapter.send_expression = fake_send  # type: ignore[method-assign]
    return adapter


def test_a_router_on_a_stick_trunk_reads_from_its_subinterfaces():
    """Failing-first (F-5): A-015 targeted rhq:GigabitEthernet0/0 and was
    permanently UNKNOWN ('trunk state not determinable') because routers have
    no `show vlan brief`. The topology read lists the ROAS subinterfaces —
    that IS the trunk evidence."""
    adapter = _router_adapter()
    obs = adapter.inspect_device("rhq")
    parent = obs.ports["GigabitEthernet0/0"]
    assert parent.trunk.is_known and parent.trunk.value is True, (
        parent.trunk.detail or parent.trunk.status.value
    )


def test_a_router_port_without_subinterfaces_reads_as_not_a_trunk():
    """Honest counterpart: Gi0/1 is a plain routed link, so it is known NOT
    to be a trunk — a criterion on it gets FAIL, never a silent UNKNOWN."""
    adapter = _router_adapter()
    obs = adapter.inspect_device("rhq")
    plain = obs.ports["GigabitEthernet0/1"]
    assert plain.trunk.is_known and plain.trunk.value is False, (
        plain.trunk.detail or plain.trunk.status.value
    )
