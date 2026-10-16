"""Configure the fabric before the routing layer, or routes cannot be read back.

PT installs an IOS static route only while its next hop is resolvable, and it
drops the route from ``show ip route`` the moment the interface carrying that
next hop goes down (measured live on risp: shutting FastEthernet0/0 removed
``S 10.0.0.0/8 via 203.0.113.1`` from the table, and bringing it back up
re-installed it). The route is still in the running config either way, so the
push is accepted — ``configureIosDevice`` answers ``true`` — and only the
read-back notices.

That makes the read-back meaningful only once the far end of the link is up.
The reference build configured its routers (redge, rhq, rbr, risp) *before*
its ASAs (fwhq, fwdc, fwbr), so when risp asked for a route whose next hop is
fwhq's outside address the ASA had not brought its interface up yet, the next
hop was unresolvable, and op-config-risp failed with "routes still missing
after retry" on a device that does carry the route (build-dcca6bd4ab7f).

The fabric therefore has to be configured first. These tests pin that order
with the router listed FIRST in the spec, so only the compiler's own ordering
can make them pass.
"""

from __future__ import annotations

from src.network_autopilot.compiler import compile_spec
from src.network_autopilot.compiler.operations import NetworkOperation
from src.network_autopilot.core.network_spec import NetworkSpec
from src.network_autopilot.ipam import plan_addressing

# The router is deliberately listed before the firewall. In the reference
# spec the same inversion exists: every router precedes every ASA, which is
# exactly the ordering that produced the false failure.
_FABRIC_SPEC = {
    "name": "fabric-before-routing",
    "devices": [
        {"id": "r1", "role": "router"},
        {"id": "fw1", "role": "firewall"},
    ],
    "links": [
        {"id": "l-r1-fw1", "a": {"device": "r1"}, "b": {"device": "fw1"}},
    ],
    "subnets": [
        {"id": "net-edge", "cidr": "10.0.0.0/30"},
    ],
    "ip_assignments": [
        {
            "device": "r1",
            "interface_hint": "FastEthernet0/0",
            "subnet": "net-edge",
            "ip": "10.0.0.2",
        },
        {
            "device": "fw1",
            "interface_hint": "GigabitEthernet1/1",
            "subnet": "net-edge",
            "ip": "10.0.0.1",
        },
    ],
    "routing": {
        "mode": "static",
        # The next hop is the firewall's own address, so r1's `ip route` can
        # only be read back after fw1 has brought its interface up.
        "static_routes": [
            {"device": "r1", "destination": "192.0.2.0/24", "next_hop": "10.0.0.1"},
        ],
    },
    "requirements": [
        {"id": "R-1", "statement": "r1 reaches 192.0.2.0/24"},
    ],
    "acceptance_criteria": [],
}


def _configure_order(raw: dict) -> list[str]:
    """The devices configured by ``configure_ios`` ops, in topological order."""
    spec = NetworkSpec.model_validate(raw)
    plan = plan_addressing(spec)
    dag = compile_spec(spec, plan).dag
    order: list[str] = []
    for op_id in dag.topo_order():
        op: NetworkOperation = dag.nodes[op_id].operation
        if op.operation_type == "configure_ios":
            order.append(op.inputs["device"])
    return order


def test_the_fabric_is_configured_before_the_routing_layer():
    """fw1 must be configured before r1, despite being listed second.

    r1's static route points at fw1's address. Until fw1 brings its interface
    up that next hop is unresolvable, PT will not install the route, and the
    postcondition read-back reports the device's own accepted route as
    missing — a build failure for a network that is actually correct.
    """
    order = _configure_order(_FABRIC_SPEC)

    assert "fw1" in order and "r1" in order, order
    assert order.index("fw1") < order.index("r1"), (
        "the firewall was configured after the router that points a static "
        f"route at it, so that route cannot be confirmed: {order}"
    )


def test_a_firewall_with_no_router_never_loses_its_own_config_op():
    """Ordering must not drop the fabric's config op."""
    raw = dict(_FABRIC_SPEC)
    raw["devices"] = [{"id": "fw1", "role": "firewall"}]
    raw["links"] = []
    raw["ip_assignments"] = [
        {
            "device": "fw1",
            "interface_hint": "GigabitEthernet1/1",
            "subnet": "net-edge",
            "ip": "10.0.0.1",
        },
    ]
    raw["routing"] = {"mode": "static", "static_routes": []}

    assert _configure_order(raw) == ["fw1"]


def test_two_routers_keep_their_spec_order():
    """Routers stay in spec order among themselves — the fix only moves fabric."""
    raw = dict(_FABRIC_SPEC)
    raw["devices"] = [
        {"id": "ra", "role": "router"},
        {"id": "rb", "role": "router"},
    ]
    raw["links"] = [
        {"id": "l-ra-rb", "a": {"device": "ra"}, "b": {"device": "rb"}},
    ]
    raw["ip_assignments"] = [
        {
            "device": "ra",
            "interface_hint": "FastEthernet0/0",
            "subnet": "net-edge",
            "ip": "10.0.0.1",
        },
        {
            "device": "rb",
            "interface_hint": "FastEthernet0/0",
            "subnet": "net-edge",
            "ip": "10.0.0.2",
        },
    ]
    raw["routing"] = {"mode": "static", "static_routes": []}

    assert _configure_order(raw) == ["ra", "rb"]


def test_the_reference_shape_puts_every_asa_before_every_router():
    """The property that failed in build-dcca6bd4ab7f, on a multi-device shape."""
    raw = dict(_FABRIC_SPEC)
    raw["devices"] = [
        {"id": "r1", "role": "router"},
        {"id": "r2", "role": "router"},
        {"id": "fw1", "role": "firewall"},
        {"id": "fw2", "role": "firewall"},
    ]
    raw["links"] = [
        {"id": "l-r1-fw1", "a": {"device": "r1"}, "b": {"device": "fw1"}},
        {"id": "l-r2-fw2", "a": {"device": "r2"}, "b": {"device": "fw2"}},
        {"id": "l-fw1-fw2", "a": {"device": "fw1"}, "b": {"device": "fw2"}},
    ]
    raw["subnets"] = [
        {"id": "net-edge", "cidr": "10.0.0.0/30"},
        {"id": "net-edge2", "cidr": "10.0.1.0/30"},
        {"id": "net-fabric", "cidr": "10.0.2.0/30"},
    ]
    raw["ip_assignments"] = [
        {"device": "r1", "interface_hint": "FastEthernet0/0", "subnet": "net-edge", "ip": "10.0.0.2"},
        {"device": "fw1", "interface_hint": "GigabitEthernet1/1", "subnet": "net-edge", "ip": "10.0.0.1"},
        {"device": "r2", "interface_hint": "FastEthernet0/0", "subnet": "net-edge2", "ip": "10.0.1.2"},
        {"device": "fw2", "interface_hint": "GigabitEthernet1/1", "subnet": "net-edge2", "ip": "10.0.1.1"},
        {"device": "fw1", "interface_hint": "GigabitEthernet1/2", "subnet": "net-fabric", "ip": "10.0.2.1"},
        {"device": "fw2", "interface_hint": "GigabitEthernet1/2", "subnet": "net-fabric", "ip": "10.0.2.2"},
    ]
    raw["routing"] = {
        "mode": "static",
        "static_routes": [
            {"device": "r1", "destination": "192.0.2.0/24", "next_hop": "10.0.0.1"},
            {"device": "r2", "destination": "198.51.100.0/24", "next_hop": "10.0.1.1"},
        ],
    }

    order = _configure_order(raw)
    assert order == ["fw1", "fw2", "r1", "r2"], order
