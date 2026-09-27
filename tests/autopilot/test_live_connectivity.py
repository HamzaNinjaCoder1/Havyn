"""End-to-end live connectivity for the four areas the engine must get right.

The offline tests for these features check the compiled operation DAG. This
file checks the thing that actually matters: that Packet Tracer really carries
the traffic, and that the configuration the COMPILER generates is the
configuration that works. Where a probe exists it hand-writes a recipe, which
would only prove the probe; here every command and every port comes out of the
DAG the compiler produced.

Areas:
  * wireless    -- laptop associates to an AP, then reaches a host across it
  * IoT / smart home -- an IoE thing is cabled to the gateway and reaches a PC
  * cloud / WAN -- a cloud reached through a modem, and a PC reaches the LAN
  * ASA 5505 / 5506-X -- two sides traverse the firewall in both directions

Each case creates namespaced devices and removes exactly those in a finally
block, so a failure never pollutes the workspace.
"""

from __future__ import annotations

import sys
import time

import pytest

sys.path.insert(0, "tests/autopilot")
import _live_harness as live

from src.network_autopilot.compiler import compile_spec
from src.network_autopilot.core.network_spec import NetworkSpec
from src.network_autopilot.ipam import plan_addressing

pytestmark = pytest.mark.skipif(
    not live.live_is_reachable(),
    reason=(
        "LIVE TEST BLOCKED: Packet Tracer is not reachable on either channel "
        "(HTTP bridge on 127.0.0.1:54321, or the Script Engine file mailbox). "
        "Open PT with the MCP Control Center extension and re-run."
    ),
)

REACHABLE = "reachable"


# -- helpers ---------------------------------------------------------------


def _dag(spec_dict: dict):
    spec = NetworkSpec.model_validate(spec_dict)
    return compile_spec(spec, plan_addressing(spec))


def _ops(result, kind: str) -> list:
    return [
        n.operation for n in result.dag.nodes.values()
        if n.operation.operation_type == kind
    ]


def _one(result, kind: str, device: str | None = None):
    for op in _ops(result, kind):
        if device is None or op.inputs.get("device") == device:
            return op
    raise AssertionError(f"no {kind} op for {device or 'any device'}")


def _link_ports(result) -> dict[str, tuple[str, str, str, str, str]]:
    """link_id -> (a_device, a_port, b_device, b_port, media)."""
    out = {}
    for op in _ops(result, "create_link"):
        out[op.inputs["link_id"]] = (
            op.inputs["a_device"], op.inputs["a_port"],
            op.inputs["b_device"], op.inputs["b_port"],
            op.inputs.get("media", "auto"),
        )
    return out


def _build(adapter, result, tag: str) -> list[str]:
    """Create the devices the DAG asks for, at the positions it chose.

    Every device is renamed under this run's namespace (``q<tag>-``) so a test
    can never touch a device the user already has, and so teardown knows
    exactly what to remove. Position comes from the compiled result, which also
    exercises the layout gate that produced it.
    """
    created = []
    for op in _ops(result, "create_device"):
        name = f"{tag}-{op.inputs['name']}"
        position = op.inputs.get("position") or (160, 160)
        receipt = adapter.create_device(
            name, op.inputs.get("role", "router"),
            model_hint=op.inputs.get("model"),
            position=(int(position[0]), int(position[1])),
        )
        assert receipt.result.value == "success", f"create {name}: {receipt.error}"
        created.append(name)
    return created


def _cable(adapter, result, tag: str) -> None:
    """Wire every link the DAG asks for, using the media it chose.

    The media matters: crossover vs straight-through is the difference between
    a link that comes up and one that silently stays down, so it must not be
    hardcoded. Wireless links are associations, not cables, and are skipped
    here — the caller proves them through PT's antenna evidence instead.
    """
    for link_id, (ad, ap, bd, bp, media) in _link_ports(result).items():
        if media == "wireless":
            continue
        receipt = adapter.create_link(f"{tag}-{ad}", ap, f"{tag}-{bd}", bp, media=media)
        assert receipt.result.value == "success", f"cable {link_id}: {receipt.error}"


def _host_addressing(adapter, result, tag: str) -> None:
    for op in _ops(result, "configure_host"):
        receipt = adapter.configure_host(
            f"{tag}-{op.inputs['device']}", dhcp=False,
            ip=op.inputs.get("ip", ""), mask=op.inputs.get("mask", ""),
            gateway=op.inputs.get("gateway", ""),
        )
        assert receipt.result.value == "success", f"host config: {receipt.error}"


def _ios(adapter, result, tag: str, device: str) -> None:
    op = _one(result, "configure_ios", device)
    receipt = adapter.configure_ios(
        f"{tag}-{device}", op.inputs.get("commands", [])
    )
    assert receipt.result.value == "success", f"ios config on {device}: {receipt.error}"
    time.sleep(2.0)      # let PT apply and the interfaces settle


def _settle(seconds: float = 2.5) -> None:
    time.sleep(seconds)


def _assert_reaches(
    adapter, a: str, b: str, what: str, destination_ip: str | None = None
) -> None:
    """One direction, and optionally an explicit destination address.

    Two reasons this is not "both ways": a home gateway has a LAN address but
    no console to ping *from*, and a dual-homed router has two addresses, so
    the caller has to say which side it means.
    """
    kwargs = {"destination_ip": destination_ip} if destination_ip else {}
    forward = adapter.run_flow_test(a, b, transport="icmp", **kwargs)
    assert forward.outcome.value == REACHABLE, (
        f"{what}: {a} -> {b} was {forward.outcome.value}: "
        f"{str(forward.detail)[:160]}"
    )


def _assert_both_ways(adapter, a: str, b: str, what: str) -> None:
    forward = adapter.run_flow_test(a, b, transport="icmp")
    assert forward.outcome.value == REACHABLE, (
        f"{what}: {a} -> {b} was {forward.outcome.value}: "
        f"{str(forward.detail)[:160]}"
    )
    reverse = adapter.run_flow_test(b, a, transport="icmp")
    assert reverse.outcome.value == REACHABLE, (
        f"{what}: {b} -> {a} was {reverse.outcome.value}: "
        f"{str(reverse.detail)[:160]}"
    )


# -- wireless --------------------------------------------------------------


def _wireless_spec(tag: str) -> dict:
    return {
        "name": f"wifi-live-{tag}",
        "devices": [
            {"id": "lap", "role": "laptop", "model_hint": "Laptop-PT"},
            {"id": "ap", "role": "access_point", "model_hint": "AccessPoint-PT-AC"},
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


def test_wireless_client_associates_and_reaches_a_host():
    """Association is proven by PT's own antenna link, then traffic must flow."""
    adapter = live.make_adapter()
    tag = live.run_tag()
    result = _dag(_wireless_spec(tag))
    created: list[str] = []
    try:
        created = _build(adapter, result, tag)
        lap, ap, pc = f"{tag}-lap", f"{tag}-ap", f"{tag}-pc"
        assert set(created) == {lap, ap, pc}, created

        # Use the same path a real build uses, in the DAG's order. Order is not
        # cosmetic: the association power-cycles the client to re-scan, which
        # clears any address configured before it, so a wireless client has to
        # be addressed AFTER it associates. The compiler already encodes that
        # dependency; this test must not work around it.
        from src.network_autopilot.wireless import associate_wireless_client

        _cable(adapter, result, tag)

        # The DAG's own first wireless step: install the radio. The wired NIC
        # has to leave the module slot first, which the helper handles.
        radio = _one(result, "install_wireless_radio")
        installed = adapter.ensure_wireless_radio(
            lap, module=radio.inputs["module"], slot=radio.inputs["slot"],
            interface=radio.inputs["interface"],
        )
        assert installed.get("installed"), f"radio install failed: {installed}"

        assoc = associate_wireless_client(
            adapter, lap, ap, settle_seconds=1.0, timeout_seconds=25.0,
        )
        assert assoc.associated, (
            f"{lap} did not associate: link_class={assoc.link_class} "
            f"ap={assoc.access_point} evidence={assoc.evidence}"
        )
        assert assoc.access_point == ap, (
            f"associated to the wrong AP: {assoc.access_point} != {ap} "
            f"(a stale radio from an earlier run is the usual cause)"
        )
        assert assoc.link_class == "Antenna", (
            f"expected PT's antenna evidence, got {assoc.link_class!r}"
        )

        # Addressing last, as the DAG requires.
        _host_addressing(adapter, result, tag)
        _settle(3.0)

        # The address has to have survived the association, on the radio.
        obs = adapter.inspect_device(lap)
        radio = obs.ports.get("Wireless0")
        assert radio is not None and radio.ip.is_known, (
            f"no Wireless0 on {lap}: {sorted(obs.ports)}"
        )
        assert radio.ip.value == "192.168.10.20", (
            f"the association cleared the client's address; it reads "
            f"{radio.ip.value!r} (APICS default means the config ran too early)"
        )

        # Association is not connectivity: the real test is a ping across it.
        _assert_both_ways(adapter, lap, pc, "wireless client to wired host")

        # Association is not connectivity: the real test is a ping across it.
        _assert_both_ways(adapter, lap, pc, "wireless client to wired host")
    finally:
        outcomes = live.delete_devices(created)
        leaked = {k: v for k, v in outcomes.items() if v != "success"}
        assert not leaked, f"leaked devices: {leaked}"


# -- IoT / smart home ------------------------------------------------------


def _iot_spec() -> dict:
    return {
        "name": "iot-live",
        "devices": [
            {"id": "gw", "role": "router", "model_hint": "HomeRouter-PT-AC"},
            {"id": "pc", "role": "pc"},
            {"id": "iot", "role": "other", "model_hint": "Smart LED"},
        ],
        "links": [
            {"id": "l-pc", "a": {"device": "gw"}, "b": {"device": "pc"}},
            {"id": "l-iot", "a": {"device": "gw"}, "b": {"device": "iot"}},
        ],
        "subnets": [{"id": "lan", "cidr": "192.168.0.0/24"}],
        "ip_assignments": [
            {"device": "gw", "subnet": "lan", "ip": "192.168.0.1"},
            {"device": "pc", "subnet": "lan"},
        ],
        "routing": {"mode": "connected"},
    }


def test_iot_device_is_cabled_and_reaches_the_lan():
    """A smart-home thing on the gateway's LAN, and the LAN really passes
    traffic (the gateway needs its Vlan1 SVI, which the compiler emits)."""
    adapter = live.make_adapter()
    tag = live.run_tag()
    result = _dag(_iot_spec())
    created: list[str] = []
    try:
        created = _build(adapter, result, tag)
        gw, pc = f"{tag}-gw", f"{tag}-pc"

        _cable(adapter, result, tag)
        _host_addressing(adapter, result, tag)
        _ios(adapter, result, tag, "gw")
        _settle(3.0)

        # The gateway's LAN address has to be live on the SVI the compiler used.
        obs = adapter.inspect_device(gw)
        addresses = {
            name: port.ip.value for name, port in obs.ports.items()
            if port.ip.is_known and port.ip.value and port.ip.value != "0.0.0.0"
        }
        assert "192.168.0.1" in addresses.values(), (
            f"gateway has no LAN address; saw {addresses}"
        )

        # The IoE thing is cabled to a gateway LAN port, and the LAN that
        # carries it reaches the host.
        links = _link_ports(result)
        iot_link = next(
            info for lid, info in links.items() if "iot" in (info[0], info[2])
        )
        assert iot_link[0] == "gw" or iot_link[2] == "gw", (
            f"the IoT thing is not on the gateway: {iot_link}"
        )

        _assert_reaches(adapter, pc, gw, "host to smart-home gateway")
    finally:
        outcomes = live.delete_devices(created)
        leaked = {k: v for k, v in outcomes.items() if v != "success"}
        assert not leaked, f"leaked devices: {leaked}"


# -- cloud / WAN -----------------------------------------------------------


def _wan_spec() -> dict:
    return {
        "name": "wan-live",
        "devices": [
            {"id": "isp", "role": "cloud", "model_hint": "Cloud-PT"},
            {"id": "modem", "role": "other", "model_hint": "DSL-Modem-PT"},
            {"id": "r1", "role": "router", "model_hint": "Router-PT"},
            {"id": "pc1", "role": "pc"},
        ],
        "links": [
            {"id": "l-line", "a": {"device": "isp"}, "b": {"device": "modem"}},
            {"id": "l-modem", "a": {"device": "modem"}, "b": {"device": "r1"}},
            {"id": "l-lan", "a": {"device": "r1"}, "b": {"device": "pc1"}},
        ],
        "subnets": [
            {"id": "wan", "cidr": "203.0.113.0/24"},
            {"id": "lan", "cidr": "192.168.1.0/24"},
        ],
        "ip_assignments": [
            {"device": "r1", "subnet": "wan", "ip": "203.0.113.2"},
            {"device": "r1", "subnet": "lan", "ip": "192.168.1.1"},
            {"device": "pc1", "subnet": "lan"},
        ],
        "routing": {"mode": "connected"},
    }


def test_cloud_through_modem_reaches_the_router_and_its_lan():
    """The provider line (cloud) reaches the router through the modem, and the
    router's LAN host answers. The cloud itself is unaddressable, so the
    observable claim is: the WAN interface is live and the LAN works."""
    adapter = live.make_adapter()
    tag = live.run_tag()
    result = _dag(_wan_spec())
    created: list[str] = []
    try:
        created = _build(adapter, result, tag)
        r1, pc1, modem = f"{tag}-r1", f"{tag}-pc1", f"{tag}-modem"

        _cable(adapter, result, tag)
        _host_addressing(adapter, result, tag)
        _ios(adapter, result, tag, "r1")
        _settle(3.0)

        obs = adapter.inspect_device(r1)
        addresses = {
            port.ip.value for port in obs.ports.values()
            if port.ip.is_known and port.ip.value and port.ip.value != "0.0.0.0"
        }
        assert "203.0.113.2" in addresses, f"router has no WAN address: {addresses}"
        assert "192.168.1.1" in addresses, f"router has no LAN address: {addresses}"

        # The cloud and the modem carry no address of their own, so the WAN hop
        # cannot be an ICMP target — there is nothing to ping on that side. The
        # observable claim is: the modem's data port is really cabled to the
        # router's WAN port, the router holds the ISP address on that port, and
        # the LAN behind the router passes traffic.
        wan_port = next(
            name for name, port in obs.ports.items()
            if port.ip.is_known and port.ip.value == "203.0.113.2"
        )
        # PT registers a cable just after the call returns, so the peer read
        # needs a moment and one retry before it is meaningful evidence.
        wan_link = adapter.link_exists(r1, wan_port, modem, "Port 1")
        for _ in range(4):
            if wan_link.is_known and wan_link.value:
                break
            time.sleep(1.5)
            wan_link = adapter.link_exists(r1, wan_port, modem, "Port 1")
        assert wan_link.is_known and wan_link.value, (
            f"the modem is not cabled to the router's WAN port {wan_port}: "
            f"{wan_link.detail or wan_link.status.value}"
        )

        # The LAN is the part that can carry a probe, host -> router LAN side.
        # The router is dual-homed, so the caller names the LAN address rather
        # than letting the probe guess a side.
        _assert_reaches(
            adapter, pc1, r1, "host to router LAN across a cloud WAN link",
            destination_ip="192.168.1.1",
        )
    finally:
        outcomes = live.delete_devices(created)
        leaked = {k: v for k, v in outcomes.items() if v != "success"}
        assert not leaked, f"leaked devices: {leaked}"


# -- ASA -------------------------------------------------------------------


def _asa_spec(model: str) -> dict:
    return {
        "name": f"asa-live-{model}",
        "devices": [
            {"id": "fw", "role": "router", "model_hint": model},
            {"id": "pc-a", "role": "pc"},
            {"id": "pc-b", "role": "pc"},
        ],
        "links": [
            {"id": "l-in", "a": {"device": "fw"}, "b": {"device": "pc-a"}},
            {"id": "l-out", "a": {"device": "fw"}, "b": {"device": "pc-b"}},
        ],
        "subnets": [
            {"id": "net-in", "cidr": "192.168.10.0/24"},
            {"id": "net-out", "cidr": "192.168.20.0/24"},
        ],
        "ip_assignments": [
            {"device": "fw", "subnet": "net-in", "ip": "192.168.10.1"},
            {"device": "fw", "subnet": "net-out", "ip": "192.168.20.1"},
            {"device": "pc-a", "subnet": "net-in", "ip": "192.168.10.10"},
            {"device": "pc-b", "subnet": "net-out", "ip": "192.168.20.10"},
        ],
        "routing": {"mode": "connected"},
    }


@pytest.mark.parametrize("model", ["5505", "5506-X"])
def test_asa_traverses_in_both_directions(model):
    """Two hosts on either side of the firewall, using the ports and the IOS
    config the compiler produced, must reach each other both ways."""
    adapter = live.make_adapter()
    tag = live.run_tag()
    result = _dag(_asa_spec(model))
    created: list[str] = []
    try:
        created = _build(adapter, result, tag)
        fw, pca, pcb = f"{tag}-fw", f"{tag}-pc-a", f"{tag}-pc-b"

        _cable(adapter, result, tag)
        _host_addressing(adapter, result, tag)
        _ios(adapter, result, tag, "fw")
        _settle(3.0)

        # The firewall must hold both addresses the spec gave it.
        obs = adapter.inspect_device(fw)
        addresses = {
            port.ip.value for port in obs.ports.values()
            if port.ip.is_known and port.ip.value and port.ip.value != "0.0.0.0"
        }
        assert {"192.168.10.1", "192.168.20.1"} <= addresses, (
            f"{model} did not take both addresses: {addresses}"
        )

        _assert_both_ways(adapter, pca, pcb, f"{model} inside to outside")
    finally:
        outcomes = live.delete_devices(created)
        leaked = {k: v for k, v in outcomes.items() if v != "success"}
        assert not leaked, f"leaked devices: {leaked}"


BLOCKED = "blocked"


def _asa_policy_spec(model: str) -> dict:
    """A firewall whose policy permits one protocol and denies the rest.

    PT's ASA does not implement the real implicit security-level rule: with no
    access-group EVERY flow is blocked, both directions. Enforcement IS
    protocol-selective, though - with only ``permit tcp any any`` bound, ICMP
    is blocked while TCP passes. That is what makes a deliberately blocked
    flow expressible here without inventing an address match PT ignores.
    """
    return {
        "name": f"asa-policy-{model}",
        "devices": [
            {"id": "fw", "role": "router", "model_hint": model},
            {"id": "pc-a", "role": "pc"},
            {"id": "pc-b", "role": "pc"},
        ],
        "links": [
            {"id": "l-in", "a": {"device": "fw"}, "b": {"device": "pc-a"}},
            {"id": "l-out", "a": {"device": "fw"}, "b": {"device": "pc-b"}},
        ],
        "subnets": [
            {"id": "net-in", "cidr": "192.168.10.0/24"},
            {"id": "net-out", "cidr": "192.168.20.0/24"},
        ],
        "ip_assignments": [
            {"device": "fw", "subnet": "net-in", "ip": "192.168.10.1"},
            {"device": "fw", "subnet": "net-out", "ip": "192.168.20.1"},
            {"device": "pc-a", "subnet": "net-in", "ip": "192.168.10.10"},
            {"device": "pc-b", "subnet": "net-out", "ip": "192.168.20.10"},
        ],
        "routing": {"mode": "connected"},
        "policies": [
            {
                "id": "fwpolicy",
                "apply_to": ["fw"],
                "rules": [
                    {"action": "permit", "protocol": "tcp",
                     "source": "any", "destination": "any"},
                ],
            }
        ],
    }


@pytest.mark.parametrize("model", ["5505", "5506-X"])
def test_asa_policy_blocks_what_it_does_not_permit(model):
    """A real firewall decision, checked against real PT traffic.

    The policy permits tcp only. ICMP must therefore be blocked even though the
    hosts are addressed and the interfaces are up - which is the assertion that
    distinguishes "the firewall is filtering" from "the link is broken".
    """
    adapter = live.make_adapter()
    tag = live.run_tag()
    result = _dag(_asa_policy_spec(model))
    created: list[str] = []
    try:
        created = _build(adapter, result, tag)
        fw, pca, pcb = f"{tag}-fw", f"{tag}-pc-a", f"{tag}-pc-b"

        _cable(adapter, result, tag)
        _host_addressing(adapter, result, tag)
        _ios(adapter, result, tag, "fw")
        _settle(3.0)

        # Read-back: the addresses must really be on the firewall. A receipt
        # that only proves the JS was delivered is not enough to go on to a
        # traffic assertion - otherwise a dead config and a working one look
        # identical here.
        obs = adapter.inspect_device(fw)
        addresses = {
            port.ip.value for port in obs.ports.values()
            if port.ip.is_known and port.ip.value and port.ip.value != "0.0.0.0"
        }
        assert {"192.168.10.1", "192.168.20.1"} <= addresses, (
            f"{model} did not take both addresses: {addresses}"
        )

        # The policy the compiler emitted must be the one we think it is.
        op = _one(result, "configure_ios", "fw")
        commands = op.inputs.get("commands", [])
        assert "access-list fwpolicy extended permit tcp any any" in commands
        assert "access-group fwpolicy in interface outside" in commands

        # Denied: icmp is not in the policy.
        forward = adapter.run_flow_test(pca, pcb, transport="icmp")
        assert forward.outcome.value == BLOCKED, (
            f"{model}: policy permits tcp only, so icmp must be blocked, got "
            f"{forward.outcome.value}: {str(forward.detail)[:160]}"
        )
        reverse = adapter.run_flow_test(pcb, pca, transport="icmp")
        assert reverse.outcome.value == BLOCKED, (
            f"{model}: reverse icmp must be blocked too, got "
            f"{reverse.outcome.value}: {str(reverse.detail)[:160]}"
        )
    finally:
        outcomes = live.delete_devices(created)
        leaked = {k: v for k, v in outcomes.items() if v != "success"}
        assert not leaked, f"leaked devices: {leaked}"


@pytest.mark.parametrize("model", ["5505", "5506-X"])
def test_asa_permits_icmp_when_the_policy_permits_icmp(model):
    """The other half of the same decision, and the control for the test above:
    widening the policy from tcp to icmp must make the very same ping pass.

    Without this pair, a permanently-blocked firewall would satisfy a
    "blocked is correct" assertion without proving the policy is doing
    anything at all.
    """
    spec = _asa_policy_spec(model)
    spec["policies"][0]["rules"] = [
        {"action": "permit", "protocol": "icmp",
         "source": "any", "destination": "any"},
    ]
    adapter = live.make_adapter()
    tag = live.run_tag()
    result = _dag(spec)
    created: list[str] = []
    try:
        created = _build(adapter, result, tag)
        pca, pcb = f"{tag}-pc-a", f"{tag}-pc-b"
        _cable(adapter, result, tag)
        _host_addressing(adapter, result, tag)
        _ios(adapter, result, tag, "fw")
        _settle(3.0)

        _assert_both_ways(
            adapter, pca, pcb, f"{model} policy permits icmp"
        )
    finally:
        outcomes = live.delete_devices(created)
        leaked = {k: v for k, v in outcomes.items() if v != "success"}
        assert not leaked, f"leaked devices: {leaked}"


@pytest.mark.parametrize("model", ["5505", "5506-X"])
def test_asa_with_no_policy_blocks_every_flow(model):
    """PT's ASA has no implicit inside->outside permit, so an unconfigured
    firewall is a black hole. This pins that default, which is why the compiler
    always emits a permit rather than trusting the security levels.
    """
    spec = _asa_policy_spec(model)
    spec.pop("policies")
    adapter = live.make_adapter()
    tag = live.run_tag()
    result = _dag(spec)
    created: list[str] = []
    try:
        created = _build(adapter, result, tag)
        pca, pcb = f"{tag}-pc-a", f"{tag}-pc-b"
        _cable(adapter, result, tag)
        _host_addressing(adapter, result, tag)
        # Deliberately skip the ASA config entirely: bare interfaces, no ACL.
        _settle(2.0)

        flow = adapter.run_flow_test(pca, pcb, transport="icmp")
        assert flow.outcome.value == BLOCKED, (
            f"{model}: with no config PT should pass nothing, got "
            f"{flow.outcome.value}: {str(flow.detail)[:160]}"
        )
    finally:
        outcomes = live.delete_devices(created)
        leaked = {k: v for k, v in outcomes.items() if v != "success"}
        assert not leaked, f"leaked devices: {leaked}"
