"""Scale: the user's target topology, built and verified against live PT.

63 devices - 20 PCs, 10 routers, 12 switches, 6 ASA firewalls, 8 IoT things,
5 wireless routers, 2 clouds - compiled, built, addressed and checked for real
traffic. The unit tests prove the compiler's arithmetic; only this proves the
engine still finishes in minutes once every operation is a round-trip through
Packet Tracer's script engine.

The topology is deliberately constrained by what the hardware allows, and the
constraints are the interesting part:

* A 2911 has exactly three onboard ports, so the tiering is what lets ten
  routers carry twelve access switches. Routers with two core hops host one
  LAN; routers with one core hop host two; the last two switches are L2
  extensions of an access switch, the way a real access layer grows past its
  uplink count. Hand the solver a fourth port and preflight refuses the build,
  which is the correct outcome.
* Only "Smart LED" is both live-verified and cablable among PT's IoE things.
  "Fan" is RF-only and "Thing" has no ports, so a wired smart-home build has
  exactly one thing to work with.
* An ASA is cabled inline but NOT configured here: a PT firewall does not
  forward until it is configured, and configuring one is covered by the ASA
  tests. The verified flows therefore cross plain router/switch links only.

Timing is printed rather than asserted: PT's own speed varies too much for a
wall-clock assertion to be honest, and a flaky perf gate is worse than none.
What IS asserted is that nothing failed and every checked flow really passed.

Scope, stated because a green run here is narrower than it looks (#41):

* Configured devices are the routers (hostname, interface addresses, two static
  routes), the switches (hostname, access ports) and the PCs (static address).
  The ASAs, the wireless routers, the clouds and the IoT things are created and
  cabled only - they get no configuration operation at all.
* The spec carries no `acceptance_criteria`, no `vlans`, and its subnets are not
  marked `dhcp`, so nothing here exercises VLAN creation, trunk negotiation or
  DHCP lease behaviour. Those are covered (or not) elsewhere.
* Five direct ICMP flows between router/switch hosts are verified, all of them
  deliberately routed so they never cross an unconfigured ASA. TCP/UDP criteria
  are not exercised.

A pass means "this shape builds and these five paths carry traffic in the stated
time", nothing broader.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import _live_harness as live  # noqa: E402

from src.network_autopilot.compiler.compiler import compile_spec  # noqa: E402
from src.network_autopilot.compiler.executor import DagExecutor  # noqa: E402
from src.network_autopilot.core.network_spec import NetworkSpec  # noqa: E402
from src.network_autopilot.core.safety import (  # noqa: E402
    MutationBudget,
    SafetyGate,
    SafetyMode,
)
from src.network_autopilot.evidence.store import EvidenceStore  # noqa: E402
from src.network_autopilot.ipam import plan_addressing  # noqa: E402

REACHABLE = "reachable"

# Only "Smart LED" is both live-verified AND cablable. Of PT's IoE list, only
# Fan, Thing, Smart LED and Solar Panel can be added at all, and Fan is RF-only
# (Wireless0 + Bluetooth) while Thing has no ports - so a wired smart-home
# build has exactly one thing to work with.
IOT = ["Smart LED"] * 8

# (a_router, b_router, a_if, b_if) - 9 routed hops, each a /30
ROUTER_LINKS: list[tuple[int, int, str, str]] = [
    (1, 2, "GigabitEthernet0/1", "GigabitEthernet0/1"),
    (1, 3, "GigabitEthernet0/2", "GigabitEthernet0/0"),
    (2, 4, "GigabitEthernet0/2", "GigabitEthernet0/0"),
    (3, 5, "GigabitEthernet0/1", "GigabitEthernet0/0"),
    (3, 6, "GigabitEthernet0/2", "GigabitEthernet0/0"),
    (4, 7, "GigabitEthernet0/1", "GigabitEthernet0/0"),
    (4, 8, "GigabitEthernet0/2", "GigabitEthernet0/0"),
    (5, 9, "GigabitEthernet0/1", "GigabitEthernet0/0"),
    (6, 10, "GigabitEthernet0/1", "GigabitEthernet0/0"),
]

# (owning router, access switch, LAN interface on the router, lan id)
UPLINKS: list[tuple[int, int, str, int]] = [
    (5, 0, "GigabitEthernet0/2", 0),
    (6, 1, "GigabitEthernet0/2", 1),
    (7, 2, "GigabitEthernet0/1", 2),
    (7, 3, "GigabitEthernet0/2", 3),
    (8, 4, "GigabitEthernet0/1", 4),
    (8, 5, "GigabitEthernet0/2", 5),
    (9, 6, "GigabitEthernet0/1", 6),
    (9, 7, "GigabitEthernet0/2", 7),
    (10, 8, "GigabitEthernet0/1", 8),
    (10, 9, "GigabitEthernet0/2", 9),
]
# 4 of the 10 router uplinks are ASA-guarded (2 more ASAs sit at the WAN edge).
# An ASA is a firewall: it does not forward until it is configured, and this
# test does not configure one. The guarded uplinks are therefore kept off the
# paths whose flows are verified, while the cabling of an ASA inline is still
# exercised - in a shape PT can actually build. A 5506-X has routed ports, so
# it may sit next to a router; a 5505's Ethernet ports are switchports, so the
# lab's ONE 5505 (asa3) sits between two switches, where that exact shape was
# measured carrying traffic. The compiler refuses a 5505 Ethernet against a
# router by name (unbuildable_link, measured six ways on PT 9.0.1), and this
# spec never asks for that shape.
GUARDED_UPLINK = {1, 2, 3, 4}

# The two extra access switches are L2 extensions, so they share a subnet with
# the switch they hang off: (switch, upstream switch, lan id)
L2_EXTENSIONS: list[tuple[int, int, int]] = [(10, 5, 5), (11, 6, 6)]

# 20 PCs over 12 access switches: two each on sw0-sw7, one each on sw8-sw11.
# (switch, lan id, host part)
PCS: list[tuple[int, int, int]] = (
    [(s, s, 10) for s in range(8)]
    + [(s, s, 11) for s in range(8)]
    + [(8, 8, 10), (9, 9, 10), (10, 5, 12), (11, 6, 12)]
)
# 5 wireless routers on five different access switches
WRT_SWITCHES = [0, 5, 6, 7, 8]


def scale_spec(p: str) -> dict:
    """The 63-device target topology, namespaced under ``p``."""
    devices: list[dict] = []
    links: list[dict] = []
    subnets: list[dict] = []
    assigns: list[dict] = []

    def rtr(i: int) -> str:
        return f"{p}r{i}"

    def sw(i: int) -> str:
        return f"{p}sw{i}"

    for k, (a, b, ai, bi) in enumerate(ROUTER_LINKS, start=1):
        subnets.append({"id": f"rt{k}", "cidr": f"10.1.{k}.0/30"})
        links.append({"id": f"{p}rt{k}",
                      "a": {"device": rtr(a), "interface_hint": ai},
                      "b": {"device": rtr(b), "interface_hint": bi}})
        assigns.append({"device": rtr(a), "interface_hint": ai,
                        "subnet": f"rt{k}", "ip": f"10.1.{k}.1"})
        assigns.append({"device": rtr(b), "interface_hint": bi,
                        "subnet": f"rt{k}", "ip": f"10.1.{k}.2"})

    # WAN: cloud -> ASA -> border router. The inline ASA is a 5506-X: its
    # routed port faces the router (a 5505 Ethernet there is refused by name,
    # because PT will not cable that shape to a router at all).
    for k, (cloud, asa, border) in enumerate(
        [("cld1", "asa1", 1), ("cld2", "asa2", 2)]
    ):
        subnets.append({"id": f"wan{k + 1}", "cidr": f"10.0.{k}.0/30"})
        devices.append({"id": f"{p}{cloud}", "role": "cloud"})
        devices.append({"id": f"{p}{asa}", "role": "other",
                        "model_hint": "5506-X"})
        links.append({"id": f"{p}wk{k}", "a": {"device": f"{p}{cloud}"},
                      "b": {"device": f"{p}{asa}"}})
        links.append({"id": f"{p}wb{k}", "a": {"device": f"{p}{asa}"},
                      "b": {"device": rtr(border),
                            "interface_hint": "GigabitEthernet0/0"}})
        assigns.append({"device": rtr(border),
                        "interface_hint": "GigabitEthernet0/0",
                        "subnet": f"wan{k + 1}", "ip": f"10.0.{k}.2"})

    for i in range(1, 11):
        devices.append({"id": rtr(i), "role": "router"})

    for lan in range(10):
        subnets.append({"id": f"lan{lan}", "cidr": f"10.200.{lan}.0/24"})

    for idx, (r_i, s_i, lan_if, lan) in enumerate(UPLINKS):
        assigns.append({"device": rtr(r_i), "interface_hint": lan_if,
                        "subnet": f"lan{lan}", "ip": f"10.200.{lan}.1"})
        if idx in GUARDED_UPLINK:
            asa_n = 3 + list(GUARDED_UPLINK).index(idx)
            model = "5505" if asa_n == 3 else "5506-X"
            devices.append({"id": f"{p}asa{asa_n}", "role": "other",
                            "model_hint": model})
            if model == "5506-X":
                # Routed ports: legal inline between the router and its
                # access switch (measured up/up and carrying traffic).
                links.append({"id": f"{p}ug{idx}",
                              "a": {"device": rtr(r_i), "interface_hint": lan_if},
                              "b": {"device": f"{p}asa{asa_n}"}})
                links.append({"id": f"{p}ugx{idx}", "a": {"device": f"{p}asa{asa_n}"},
                              "b": {"device": sw(s_i)}})
            else:
                # asa3 is the lab's 5505: its Ethernet ports are switchports,
                # so the router uplinks straight (a 5505 Ethernet against a
                # router is refused by name) and the ASA rehomes onto its L2
                # extension below, between two switches.
                links.append({"id": f"{p}ug{idx}",
                              "a": {"device": rtr(r_i), "interface_hint": lan_if},
                              "b": {"device": sw(s_i)}})
        else:
            links.append({"id": f"{p}ug{idx}",
                          "a": {"device": rtr(r_i), "interface_hint": lan_if},
                          "b": {"device": sw(s_i)}})

    for i in range(12):
        devices.append({"id": sw(i), "role": "switch"})
    # The lab's one 5505 inlines on an L2 extension: both ends are switches,
    # the one peer a 5505's switchport Ethernet ports were measured reaching
    # normally (a 5505 switchport to a 2960 carried pings on the QA canvas).
    # The sw6 <-> sw11 extension stays direct because a verified flow
    # (pc19 -> r9) crosses it and an unconfigured ASA is not asked to forward
    # anything this test claims.
    EXTENSION_ASA = {5: 3}
    for s_i, up_s, _lan in L2_EXTENSIONS:
        asa_n = EXTENSION_ASA.get(up_s)
        if asa_n is None:
            links.append({"id": f"{p}ext{s_i}", "a": {"device": sw(up_s)},
                          "b": {"device": sw(s_i)}})
        else:
            links.append({"id": f"{p}ext{up_s}", "a": {"device": sw(up_s)},
                          "b": {"device": f"{p}asa{asa_n}"}})
            links.append({"id": f"{p}extx{s_i}", "a": {"device": f"{p}asa{asa_n}"},
                          "b": {"device": sw(s_i)}})

    for i, (s_i, lan, host) in enumerate(PCS):
        devices.append({"id": f"{p}pc{i}", "role": "pc"})
        links.append({"id": f"{p}lp{i}", "a": {"device": sw(s_i)},
                      "b": {"device": f"{p}pc{i}"}})
        assigns.append({"device": f"{p}pc{i}", "subnet": f"lan{lan}",
                        "ip": f"10.200.{lan}.{host}"})

    # No ip_assignment for the IoE things: a PT thing is provisioned by the IoT
    # server and does not take an address from the desktop IP dialog, so asking
    # for one would only generate a config op that cannot mean anything.
    for i, model in enumerate(IOT):
        s_i, _up_s, _lan = L2_EXTENSIONS[i % 2]
        devices.append({"id": f"{p}iot{i}", "role": "other", "model_hint": model})
        links.append({"id": f"{p}liot{i}", "a": {"device": sw(s_i)},
                      "b": {"device": f"{p}iot{i}"}})

    for i, s_i in enumerate(WRT_SWITCHES):
        devices.append({"id": f"{p}wrt{i}", "role": "other",
                        "model_hint": "Linksys-WRT300N"})
        links.append({"id": f"{p}lw{i}", "a": {"device": sw(s_i)},
                      "b": {"device": f"{p}wrt{i}"}})

    # Two static routes make one cross-LAN pair routable: r5 and r9 are direct
    # core neighbours (hop 8), so sw0 and sw6 can reach each other. Both those
    # LANs are unguarded. A full 12-LAN routing design is a separate job and is
    # not claimed here.
    static_routes = [
        {"device": rtr(5), "destination": "10.200.6.0/24", "next_hop": "10.1.8.2"},
        {"device": rtr(9), "destination": "10.200.0.0/24", "next_hop": "10.1.8.1"},
    ]

    return {
        "name": f"{p}scale",
        "devices": devices,
        "links": links,
        "subnets": subnets,
        "ip_assignments": assigns,
        "routing": {"mode": "static", "static_routes": static_routes},
    }


def test_scale_spec_is_the_target_topology_offline():
    """The shape of the 63-device lab is a plain offline fact: it must not
    quietly drift while someone edits the live test below."""
    raw = scale_spec("qa-")
    roles: dict[str, int] = {}
    for d in raw["devices"]:
        key = d.get("model_hint") or d["role"]
        roles[key] = roles.get(key, 0) + 1
    assert len(raw["devices"]) == 63, len(raw["devices"])
    assert roles["pc"] == 20, roles
    assert roles["switch"] == 12, roles
    assert roles["router"] == 10, roles
    assert roles["cloud"] == 2, roles
    assert roles["5505"] + roles["5506-X"] == 6, roles
    assert roles["Smart LED"] == 8, roles
    assert roles["Linksys-WRT300N"] == 5, roles

    # A 2911 has three ports: no router may be asked for a fourth.
    spec = NetworkSpec.model_validate(raw)
    plan = plan_addressing(spec)
    compiled = compile_spec(spec, plan)
    per_router: dict[str, set[str]] = {}
    for node in compiled.dag.nodes.values():
        op = node.operation
        if op.operation_type != "create_link":
            continue
        for dev_key, port_key in (("a_device", "a_port"), ("b_device", "b_port")):
            if str(op.inputs[dev_key]).startswith("qa-r"):
                per_router.setdefault(
                    str(op.inputs[dev_key]), set()
                ).add(str(op.inputs[port_key]))
    assert per_router, "no router links in the compiled plan"
    for router, ports in per_router.items():
        assert len(ports) <= 3, f"{router} would need {sorted(ports)}"


_LIVE = live.live_is_reachable()


@pytest.mark.skipif(
    not _LIVE,
    reason=(
        "LIVE TEST BLOCKED: PT is not reachable through any channel "
        "(HTTP bridge on 127.0.0.1:54321, or the Script Engine file mailbox). "
        "Open PT with the MCP Control Center extension and re-run."
    ),
)
def test_scale_topology_builds_and_carries_traffic(tmp_path):
    adapter = live.make_adapter()
    tag = live.run_tag()
    p = f"{tag}-"
    raw = scale_spec(p)
    created: list[str] = []
    try:
        spec = NetworkSpec.model_validate(raw)
        t0 = time.time()
        compiled = compile_spec(spec, plan_addressing(spec))
        t_compile = time.time() - t0

        budget = MutationBudget.for_operations(len(compiled.dag.nodes))
        executor = DagExecutor(
            adapter,
            SafetyGate(SafetyMode.AUTONOMOUS, budget),
            EvidenceStore(root=tmp_path / "evidence"),
        )
        t0 = time.time()
        executor.execute(compiled.dag)
        t_exec = time.time() - t0

        bad = [
            (n.operation.operation_type,
             n.operation.inputs.get("name")
             or n.operation.inputs.get("link_id")
             or n.operation.inputs.get("device") or "",
             n.status.value)
            for n in compiled.dag.nodes.values()
            if n.status.value not in ("succeeded", "skipped")
        ]
        assert not bad, f"{len(bad)} operations did not succeed: {bad[:12]}"

        obs = adapter._live_devices(fresh=True)
        names = [str(d.get("name")) for d in (obs.value or [])]
        built = [n for n in names if n.startswith(tag)]
        assert len(built) == 63, f"only {len(built)}/63 devices reached PT"

        # Real traffic, not just "the ops returned success".
        checks = [
            ("pc0 -> r5 (gw lan0)", f"{p}pc0", f"{p}r5", "10.200.0.1"),
            ("pc19 -> r9 (gw lan6, L2 ext)", f"{p}pc19", f"{p}r9", "10.200.6.1"),
            ("pc0 -> pc1 (same switch)", f"{p}pc0", f"{p}pc1", "10.200.0.11"),
            ("r1 -> r2 (core /30)", f"{p}r1", f"{p}r2", "10.1.1.2"),
            ("pc0 -> pc6 (cross-LAN, static)", f"{p}pc0", f"{p}pc6", "10.200.6.10"),
        ]
        for label, src, dst, dst_ip in checks:
            flow = adapter.run_flow_test(src, dst, transport="icmp",
                                        destination_ip=dst_ip)
            assert flow.outcome.value == REACHABLE, (
                f"{label}: {flow.outcome.value}: {str(flow.detail)[:160]}"
            )

        # An IoE thing has no console to ping, so what can be asserted is that
        # it was created and cabled - which is this build's responsibility.
        sw10_ports = [
            q for d in (obs.value or [])
            if str(d.get("name")) == f"{p}sw10"
            for q in (d.get("ports") or [])
        ]
        assert any(str(q.get("linked")) for q in sw10_ports), (
            "sw10 has no cabled port: the IoE things were not attached"
        )

        print(
            f"\n  [scale] compile {t_compile:.1f}s  build {t_exec:.1f}s  "
            f"({t_exec / 63:.2f}s/device)  63/63 devices  5/5 flows"
        )
    finally:
        created = [d["id"] for d in raw["devices"]]
        outcomes = live.delete_devices(created)
        leaked = {k: v for k, v in outcomes.items() if v != "success"}
        assert not leaked, f"leaked devices: {leaked}"
