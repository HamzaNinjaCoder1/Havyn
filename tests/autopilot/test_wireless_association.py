"""Wireless association: production capability, verified from real PT state.

Live evidence behind this module, measured on a running PT:

  * Packet Tracer associates a wireless client AUTOMATICALLY. As soon as a
    laptop with a wireless NIC and an access point share the workspace, PT
    creates a link whose class is `Antenna` - that object IS the association.
    Every probe saw it within ~1-3 s, with no explicit call.
  * `port.getLink()` on the client's `Wireless0` returns that `Antenna` link,
    and `link.getPort().getOwnerDevice()` names the AP - so the association is
    observable, not inferred.
  * Traffic needs two things the earlier rounds missed: the client must have a
    real address (DHCP lease or static on the radio), and the AP must be one
    that bridges. Measured matrix, laptop )) AP --- PC:
        AccessPoint-PT    cross -> blocked     straight -> blocked
        AccessPoint-PT-A  cross -> blocked     straight -> blocked
        AccessPoint-PT-N  cross -> REACHABLE   straight -> blocked
        AccessPoint-PT-AC cross -> REACHABLE   straight -> REACHABLE
  * The laptop's radio needs the PT-LAPTOP-NM-1W module (a plain addModule on
    an occupied slot returns false; the working sequence removes the wired NIC
    first, exactly like the extension's swapLaptopToWireless).
  * A home gateway / wireless router bridges too, and its DHCP pool gives the
    client a lease (192.168.0.100 observed), so both wired and DHCP-fed
    wireless clients are covered.

These tests pin the production behaviour: the compiler emits the radio +
association operations, the association is verified from observed link state,
and a client PT cannot associate (an IoE thing) is refused with a clear error.
"""

from __future__ import annotations

import pytest

from src.network_autopilot import wireless
from src.network_autopilot.adapter.packet_tracer import js_commands as js
from src.network_autopilot.compiler import CompileError, compile_spec
from src.network_autopilot.core.network_spec import NetworkSpec
from src.network_autopilot.ipam import plan_addressing
from src.network_autopilot.pt.catalog.cables import infer_cable


# --------------------------------------------------------------------------
# JS builders
# --------------------------------------------------------------------------

def test_radio_installer_removes_the_wired_nic_first():
    code = js.js_install_wireless_radio("lab-lap", "PT-LAPTOP-NM-1W", "0", "Wireless0")
    assert "removeModule" in code, "the wired NIC occupies the slot; add alone fails"
    assert "addModule" in code
    assert "PT-LAPTOP-NM-1W" in code
    assert '"lab-lap"' in code


def test_radio_installer_quotes_device_names():
    hostile = 'lap"); alert(1);//'
    code = js.js_install_wireless_radio(hostile, "PT-LAPTOP-NM-1W", "0", "Wireless0")
    assert json_escaped(code, hostile), "device names go through json.dumps"


def json_escaped(code: str, value: str) -> bool:
    import json
    return json.dumps(value) in code


def test_association_read_returns_the_antenna_peer():
    code = js.js_wireless_association("lab-lap", "Wireless0")
    assert "getLink" in code
    assert "getClassName" in code
    assert "Antenna" in code
    assert '"lab-lap"' in code and '"Wireless0"' in code


# --------------------------------------------------------------------------
# Compiler: a wireless link becomes radio + association operations
# --------------------------------------------------------------------------

def _wifi_spec(client_model: str = "Laptop-PT", ap_model: str = "AccessPoint-PT-AC") -> dict:
    """Laptop )) AP --- PC, the shape that was proven live.

    The access point carries no address: PT's APs have no CLI (their port
    object answers no configuration), it is a layer-2 bridge, and the two
    hosts share the subnet across it.
    """
    return {
        "name": "wifi-lab",
        "devices": [
            {"id": "lap", "role": "laptop", "model_hint": client_model},
            {"id": "ap", "role": "access_point", "model_hint": ap_model},
            {"id": "pc", "role": "pc"},
        ],
        "links": [
            {"id": "l-wifi", "a": {"device": "lap"}, "b": {"device": "ap"},
             "media": "wireless"},
            {"id": "l-wired", "a": {"device": "ap"}, "b": {"device": "pc"}},
        ],
        "subnets": [{"id": "wifi", "cidr": "192.168.10.0/24"}],
        "ip_assignments": [
            {"device": "lap", "subnet": "wifi", "ip": "192.168.10.20"},
            {"device": "pc", "subnet": "wifi", "ip": "192.168.10.10"},
        ],
        "routing": {"mode": "connected"},
    }


def _compile(raw: dict):
    spec = NetworkSpec.model_validate(raw)
    return compile_spec(spec, plan_addressing(spec))


def test_wireless_link_compiles_to_a_radio_and_an_association_op():
    result = _compile(_wifi_spec())
    types = {
        nid: n.operation.operation_type
        for nid, n in result.dag.nodes.items()
    }
    assert "install_wireless_radio" in types.values(), types
    assert "associate_wireless" in types.values(), types
    # the wireless link itself is NOT a physical create_link
    assert not any(
        n.operation.operation_type == "create_link"
        and n.operation.inputs.get("link_id") == "l-wifi"
        for n in result.dag.nodes.values()
    )
    # ...but the AP's wired side is a real cable
    wired = [
        n.operation.inputs
        for n in result.dag.nodes.values()
        if n.operation.operation_type == "create_link"
    ]
    assert any(inputs["link_id"] == "l-wired" for inputs in wired)


def test_association_waits_for_both_devices_and_the_radio():
    result = _compile(_wifi_spec())
    op = next(
        n.operation for n in result.dag.nodes.values()
        if n.operation.operation_type == "associate_wireless"
    )
    deps = set(op.dependencies)
    assert "op-create-lap" in deps and "op-create-ap" in deps, deps
    radio = next(
        nid for nid, n in result.dag.nodes.items()
        if n.operation.operation_type == "install_wireless_radio"
    )
    assert radio in deps, "the association must follow the radio install"


def test_wireless_radio_is_installed_for_tablet_too():
    result = _compile(_wifi_spec(client_model="TabletPC-PT"))
    radio = next(
        n.operation for n in result.dag.nodes.values()
        if n.operation.operation_type == "install_wireless_radio"
    )
    assert radio.inputs["device"] == "lap"
    assert radio.inputs["interface"] == "Wireless0"


def test_iot_thing_is_refused_as_a_wireless_client():
    """IoE things never form an Antenna link in PT; say so at compile time."""
    with pytest.raises(CompileError, match="Solar Panel"):
        _compile(_wifi_spec(client_model="Solar Panel"))


def _wifi_spec_with_criterion(include_wireless_link: bool) -> dict:
    raw = _wifi_spec()
    if not include_wireless_link:
        raw["links"] = [
            link for link in raw["links"] if link.get("media") != "wireless"
        ]
    raw["requirements"] = [
        {"id": "R-001", "statement": "the laptop associates", "kind": "connectivity"}
    ]
    raw["acceptance_criteria"] = [
        {
            "id": "A-001",
            "requirement_ids": ["R-001"],
            "title": "lap associates to ap",
            "test": {
                "kind": "wireless",
                "client": "lap",
                "access_point": "ap",
                "interface": "Wireless0",
                "expect": "associated",
            },
        }
    ]
    return raw


def test_a_wireless_criterion_without_a_declared_link_is_refused():
    """Failing-first (F-6/A-021/A-022): the reference run declared a
    Wireless0 association on a spec with ZERO wireless links; the build ran
    to completion and the criteria FAILED late with "no Wireless0
    interface". A topology that never declares the association can never
    satisfy it — refuse before any mutation, naming the criterion."""
    with pytest.raises(CompileError, match="A-001"):
        _compile(_wifi_spec_with_criterion(include_wireless_link=False))


def test_the_declared_wireless_criterion_compiles_with_its_link():
    """Pin: declaring the wireless link is exactly what satisfies the guard —
    the same criterion with its link in place compiles clean."""
    result = _compile(_wifi_spec_with_criterion(include_wireless_link=True))
    assert any(
        n.operation.operation_type == "associate_wireless"
        for n in result.dag.nodes.values()
    )


def test_access_point_cable_to_a_host_is_a_cross_cable():
    """Measured: only a cross cable brings the AP's wired port up."""
    assert infer_cable("accesspoint", "pc") == "cross"
    assert infer_cable("pc", "accesspoint") == "cross"


# --------------------------------------------------------------------------
# Runtime: association is read from PT, never assumed
# --------------------------------------------------------------------------

class _FakePort:
    def __init__(self, name, link_class=None, peer=None, up=True):
        self.name = name
        self.link_class = link_class
        self.peer = peer
        self.up = up


class _FakeAdapter:
    """Adapter stub exposing the methods the wireless module needs."""

    def __init__(self, ports_by_device, radio_installs=True, model="AccessPoint-PT-AC"):
        self._ports = ports_by_device
        self._radio_installs = radio_installs
        self._model = model
        self.swapped: list[str] = []
        self.channels: list[tuple[str, int]] = []
        self.cycled: list[str] = []

    def ensure_wireless_radio(
        self, device, *, module="PT-LAPTOP-NM-1W", slot="0", interface="Wireless0"
    ):
        if not self._radio_installs:
            return {"installed": False, "answer": "false"}
        self.swapped.append(device)
        self._ports.setdefault(device, {})[interface] = _FakePort(
            interface, "Antenna", "ap", up=True
        )
        return {"installed": True, "device": device, "module": module}

    def wireless_association(self, device, interface="Wireless0"):
        from src.network_autopilot.wireless import WirelessAssociation

        port = self._ports.get(device, {}).get(interface)
        if port is None or port.link_class is None:
            return None
        return WirelessAssociation(
            client=device,
            access_point=port.peer or "",
            client_interface=interface,
            ap_interface=interface,
            link_class=port.link_class,
            port_up=port.up,
            evidence={"port_up": port.up},
        )

    def inspect_device(self, device):
        from src.network_autopilot.core.actual_state import DeviceObservation
        from src.network_autopilot.core.observability import Observable

        observation = DeviceObservation(name=device)
        observation.model = Observable.known(self._model, source="test")
        for name in self._ports.get(device, {}):
            observation.port(name)
        return observation

    def set_wireless_channel(self, device, port, channel):
        self.channels.append((device, channel))
        return {"ok": True, "device": device, "port": port, "channel": channel}

    def power_cycle(self, device):
        self.cycled.append(device)
        return {"ok": True, "device": device}


def test_associate_wireless_client_reports_the_antenna_evidence():
    adapter = _FakeAdapter({
        "lap": {"Wireless0": _FakePort("Wireless0", "Antenna", "ap", up=True)},
    })
    association = wireless.associate_wireless_client(adapter, "lap", "ap")
    assert association.associated is True
    assert association.link_class == "Antenna"
    assert association.access_point == "ap"
    assert association.client == "lap"
    assert association.evidence["port_up"] is True


def test_associate_wireless_client_raises_when_pt_shows_no_association():
    adapter = _FakeAdapter({}, radio_installs=False)
    with pytest.raises(wireless.WirelessAssociationError) as exc:
        wireless.associate_wireless_client(adapter, "lap", "ap", timeout_seconds=0.2)
    assert "lap" in str(exc.value)


def test_associate_wireless_client_installs_the_radio_first():
    adapter = _FakeAdapter({})
    wireless.associate_wireless_client(adapter, "lap", "ap", settle_seconds=0.0)
    assert "lap" in adapter.swapped, "the radio must be installed first"


def test_association_puts_both_radios_on_one_channel_and_rescans():
    """The deterministic path: same channel, then re-scan, then verify.

    On PT's default channel a fresh client joins whatever radio it finds
    first - including antenna objects from deleted devices - so the requested
    access point has to be selected by channel and confirmed by read-back.
    """
    adapter = _FakeAdapter({
        "ap": {"Port 0": _FakePort("Port 0"), "Port 1": _FakePort("Port 1")},
    })
    wireless.associate_wireless_client(adapter, "lap", "ap", settle_seconds=0.0)
    assert [c for c in adapter.channels if c[0] == "ap"], "the AP radio needs a channel"
    assert ("lap", wireless.DEFAULT_CHANNEL) in adapter.channels
    assert "lap" in adapter.cycled, "the client must re-scan after the channel move"


def test_association_to_a_different_ap_is_not_accepted():
    adapter = _FakeAdapter(
        {"lap": {"Wireless0": _FakePort("Wireless0", "Antenna", "other-ap", up=True)}},
        radio_installs=False,
    )
    with pytest.raises(wireless.WirelessAssociationError):
        wireless.associate_wireless_client(adapter, "lap", "ap", timeout_seconds=0.2)


def test_association_without_resolved_peer_is_not_accepted():
    adapter = _FakeAdapter(
        {"lap": {"Wireless0": _FakePort("Wireless0", "Antenna", peer="", up=True)}},
        radio_installs=False,
    )
    with pytest.raises(wireless.WirelessAssociationError):
        wireless.associate_wireless_client(adapter, "lap", "ap", timeout_seconds=0.2)


# --------------------------------------------------------------------------
# Verification: a criterion passes only on PT's own antenna evidence
# --------------------------------------------------------------------------

def _wireless_criterion(expect: str = "associated") -> dict:
    return {
        "id": "A-wifi",
        "requirement_ids": ["R-1"],
        "test": {"kind": "wireless", "client": "lap", "access_point": "ap",
                 "expect": expect},
    }


def _state_with_association(link_class, peers):
    from src.network_autopilot.core.actual_state import (
        ActualNetworkState, DeviceObservation, PortObservation,
    )
    from src.network_autopilot.core.observability import Observable

    state = ActualNetworkState(source="test", observed_at="2026-01-01T00:00:00+00:00")
    device = DeviceObservation(name="lap")
    port = PortObservation(name="Wireless0")
    port.state = Observable.known("up", source="test")
    if link_class is not None:
        port.link_class = Observable.known(link_class, source="test")
    if peers is not None:
        port.peers = Observable.known(peers, source="test")
    device.ports["Wireless0"] = port
    state.observe_device(device)
    return state


def _verify(criterion: dict):
    from src.network_autopilot.core.network_spec import NetworkSpec
    from src.network_autopilot.verification.per_criterion import PerCriterionVerifier

    spec = NetworkSpec.model_validate({
        "name": "wifi-verify",
        "devices": [{"id": "lap", "role": "laptop"}],
        "requirements": [{"id": "R-1", "statement": "laptop joins the AP"}],
        "acceptance_criteria": [criterion],
    })
    report = PerCriterionVerifier().verify(
        spec, _state_with_association("Antenna", ["ap"]), {}
    )
    return report.verifications[0]


def test_wireless_criterion_passes_on_a_real_antenna_link():
    result = _verify(_wireless_criterion())
    assert result.verdict.value == "pass", result.evidence
    assert any("Antenna" in str(e) for e in result.evidence)


def test_wireless_criterion_fails_without_an_antenna_link():
    result = _verify(_wireless_criterion())
    state = _state_with_association(None, None)
    from src.network_autopilot.core.network_spec import NetworkSpec
    from src.network_autopilot.verification.per_criterion import PerCriterionVerifier

    spec = NetworkSpec.model_validate({
        "name": "wifi-verify",
        "devices": [{"id": "lap", "role": "laptop"}],
        "requirements": [{"id": "R-1", "statement": "laptop joins the AP"}],
        "acceptance_criteria": [_wireless_criterion()],
    })
    report = PerCriterionVerifier().verify(spec, state, {})
    assert report.verifications[0].verdict.value == "unknown", (
        "no link type observed is UNKNOWN, never a silent pass"
    )


def test_wireless_criterion_fails_on_a_cable_instead_of_an_antenna():
    from src.network_autopilot.core.network_spec import NetworkSpec
    from src.network_autopilot.verification.per_criterion import PerCriterionVerifier

    spec = NetworkSpec.model_validate({
        "name": "wifi-verify",
        "devices": [{"id": "lap", "role": "laptop"}],
        "requirements": [{"id": "R-1", "statement": "laptop joins the AP"}],
        "acceptance_criteria": [_wireless_criterion()],
    })
    report = PerCriterionVerifier().verify(
        spec, _state_with_association("Cable", ["ap"]), {}
    )
    assert report.verifications[0].verdict.value == "fail", (
        "a wired link is not a wireless association"
    )
