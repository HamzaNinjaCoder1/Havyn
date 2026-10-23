"""Bootstrap generator for the Stage 3 live-lab specs under library/stage3/.

The JSON files are authoritative. A2, A8 and A12 were revised after the first
live runs (A2's ABR topology, A8's server-side HSRP group, A12's per-device
BGP), so re-running this generator would overwrite those revisions; use it as
a reference for the original A1/A3/A4/A5/A7 shapes, not as a build step.
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "library" / "stage3"


def routers(*ids):
    return [{"id": i, "role": "router"} for i in ids]


def pcs(*ids):
    return [{"id": i, "role": "pc"} for i in ids]


def switches(*ids):
    return [{"id": i, "role": "switch"} for i in ids]


def flow(cid, source, destination, expect="reachable"):
    return {
        "id": cid,
        "requirement_ids": ["R-1"],
        "test": {"kind": "flow", "source": source, "destination": destination,
                 "expect": expect},
    }


REQ = [{"id": "R-1", "statement": "Every LAN reaches every other LAN"}]


def save(name, data):
    data.setdefault("description", f"Stage 3 live lab: {name}")
    (OUT / f"{name}.json").write_text(
        json.dumps(data, indent=2), encoding="utf-8"
    )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    save("S3-A1-ospf-single", {
        "name": "S3.A1 OSPF single-area",
        "devices": routers("r1", "r2", "r3") + switches("s1", "s2", "s3")
        + pcs("pc1", "pc2", "pc3"),
        "links": [
            {"id": "l-r1-r2", "a": {"device": "r1"}, "b": {"device": "r2"}},
            {"id": "l-r2-r3", "a": {"device": "r2"}, "b": {"device": "r3"}},
            {"id": "l-r1-s1", "a": {"device": "r1"}, "b": {"device": "s1"}},
            {"id": "l-r2-s2", "a": {"device": "r2"}, "b": {"device": "s2"}},
            {"id": "l-r3-s3", "a": {"device": "r3"}, "b": {"device": "s3"}},
            {"id": "l-s1-pc1", "a": {"device": "s1"}, "b": {"device": "pc1"}},
            {"id": "l-s2-pc2", "a": {"device": "s2"}, "b": {"device": "pc2"}},
            {"id": "l-s3-pc3", "a": {"device": "s3"}, "b": {"device": "pc3"}},
        ],
        "subnets": [
            {"id": "wan12", "cidr": "10.0.12.0/30"},
            {"id": "wan23", "cidr": "10.0.23.0/30"},
            {"id": "lan1", "cidr": "10.0.1.0/24"},
            {"id": "lan2", "cidr": "10.0.2.0/24"},
            {"id": "lan3", "cidr": "10.0.3.0/24"},
        ],
        "ip_assignments": [
            {"device": "r1", "subnet": "wan12", "ip": "10.0.12.1"},
            {"device": "r2", "subnet": "wan12", "ip": "10.0.12.2"},
            {"device": "r2", "subnet": "wan23", "ip": "10.0.23.1"},
            {"device": "r3", "subnet": "wan23", "ip": "10.0.23.2"},
            {"device": "r1", "subnet": "lan1", "ip": "10.0.1.1"},
            {"device": "r2", "subnet": "lan2", "ip": "10.0.2.1"},
            {"device": "r3", "subnet": "lan3", "ip": "10.0.3.1"},
            {"device": "pc1", "subnet": "lan1"},
            {"device": "pc2", "subnet": "lan2"},
            {"device": "pc3", "subnet": "lan3"},
        ],
        "routing": {"mode": "ospf", "ospf": {
            "process_id": 1,
            "networks": ["10.0.12.0/30", "10.0.23.0/30", "10.0.1.0/24",
                         "10.0.2.0/24", "10.0.3.0/24"],
        }},
        "requirements": REQ,
        "acceptance_criteria": [flow("A-1", "pc1", "pc2"),
                               flow("A-2", "pc2", "pc3"),
                               flow("A-3", "pc1", "pc3")],
    })

    save("S3-A2-ospf-multi-area", {
        "name": "S3.A2 OSPF multi-area",
        "devices": routers("r1", "r2", "r3") + switches("s1", "s3")
        + pcs("pc1", "pc3"),
        "links": [
            {"id": "l-r1-r2", "a": {"device": "r1"}, "b": {"device": "r2"}},
            {"id": "l-r2-r3", "a": {"device": "r2"}, "b": {"device": "r3"}},
            {"id": "l-r1-s1", "a": {"device": "r1"}, "b": {"device": "s1"}},
            {"id": "l-r3-s3", "a": {"device": "r3"}, "b": {"device": "s3"}},
            {"id": "l-s1-pc1", "a": {"device": "s1"}, "b": {"device": "pc1"}},
            {"id": "l-s3-pc3", "a": {"device": "s3"}, "b": {"device": "pc3"}},
        ],
        "subnets": [
            {"id": "wan12", "cidr": "10.0.12.0/30"},
            {"id": "wan23", "cidr": "10.0.23.0/30"},
            {"id": "lan1", "cidr": "10.0.1.0/24"},
            {"id": "lan3", "cidr": "10.0.3.0/24"},
        ],
        "ip_assignments": [
            {"device": "r1", "subnet": "wan12", "ip": "10.0.12.1"},
            {"device": "r2", "subnet": "wan12", "ip": "10.0.12.2"},
            {"device": "r2", "subnet": "wan23", "ip": "10.0.23.1"},
            {"device": "r3", "subnet": "wan23", "ip": "10.0.23.2"},
            {"device": "r1", "subnet": "lan1", "ip": "10.0.1.1"},
            {"device": "r3", "subnet": "lan3", "ip": "10.0.3.1"},
            {"device": "pc1", "subnet": "lan1"},
            {"device": "pc3", "subnet": "lan3"},
        ],
        "routing": {"mode": "ospf", "ospf": {
            "process_id": 1,
            "area_networks": [
                {"network": "10.0.12.0/30", "area": "0"},
                {"network": "10.0.1.0/24", "area": "0"},
                {"network": "10.0.23.0/30", "area": "1"},
                {"network": "10.0.3.0/24", "area": "1"},
            ],
        }},
        "requirements": REQ,
        "acceptance_criteria": [flow("A-1", "pc1", "pc3")],
    })

    save("S3-A3-eigrp", {
        "name": "S3.A3 EIGRP",
        "devices": routers("r1", "r2") + switches("s1", "s2") + pcs("pc1", "pc2"),
        "links": [
            {"id": "l-r1-r2", "a": {"device": "r1"}, "b": {"device": "r2"}},
            {"id": "l-r1-s1", "a": {"device": "r1"}, "b": {"device": "s1"}},
            {"id": "l-r2-s2", "a": {"device": "r2"}, "b": {"device": "s2"}},
            {"id": "l-s1-pc1", "a": {"device": "s1"}, "b": {"device": "pc1"}},
            {"id": "l-s2-pc2", "a": {"device": "s2"}, "b": {"device": "pc2"}},
        ],
        "subnets": [
            {"id": "wan", "cidr": "10.0.0.0/30"},
            {"id": "lan1", "cidr": "10.0.1.0/24"},
            {"id": "lan2", "cidr": "10.0.2.0/24"},
        ],
        "ip_assignments": [
            {"device": "r1", "subnet": "wan", "ip": "10.0.0.1"},
            {"device": "r2", "subnet": "wan", "ip": "10.0.0.2"},
            {"device": "r1", "subnet": "lan1", "ip": "10.0.1.1"},
            {"device": "r2", "subnet": "lan2", "ip": "10.0.2.1"},
            {"device": "pc1", "subnet": "lan1"},
            {"device": "pc2", "subnet": "lan2"},
        ],
        "routing": {"mode": "eigrp", "eigrp": {
            "as_number": 10,
            "networks": ["10.0.0.0/30", "10.0.1.0/24", "10.0.2.0/24"],
        }},
        "requirements": REQ,
        "acceptance_criteria": [flow("A-1", "pc1", "pc2")],
    })

    save("S3-A4-ripv2", {
        "name": "S3.A4 RIP v2",
        "devices": routers("r1", "r2", "r3") + switches("s1", "s2", "s3")
        + pcs("pc1", "pc2", "pc3"),
        "links": [
            {"id": "l-r1-r2", "a": {"device": "r1"}, "b": {"device": "r2"}},
            {"id": "l-r2-r3", "a": {"device": "r2"}, "b": {"device": "r3"}},
            {"id": "l-r1-s1", "a": {"device": "r1"}, "b": {"device": "s1"}},
            {"id": "l-r2-s2", "a": {"device": "r2"}, "b": {"device": "s2"}},
            {"id": "l-r3-s3", "a": {"device": "r3"}, "b": {"device": "s3"}},
            {"id": "l-s1-pc1", "a": {"device": "s1"}, "b": {"device": "pc1"}},
            {"id": "l-s2-pc2", "a": {"device": "s2"}, "b": {"device": "pc2"}},
            {"id": "l-s3-pc3", "a": {"device": "s3"}, "b": {"device": "pc3"}},
        ],
        "subnets": [
            {"id": "wan12", "cidr": "10.0.12.0/30"},
            {"id": "wan23", "cidr": "10.0.23.0/30"},
            {"id": "lan1", "cidr": "10.0.1.0/24"},
            {"id": "lan2", "cidr": "10.0.2.0/24"},
            {"id": "lan3", "cidr": "10.0.3.0/24"},
        ],
        "ip_assignments": [
            {"device": "r1", "subnet": "wan12", "ip": "10.0.12.1"},
            {"device": "r2", "subnet": "wan12", "ip": "10.0.12.2"},
            {"device": "r2", "subnet": "wan23", "ip": "10.0.23.1"},
            {"device": "r3", "subnet": "wan23", "ip": "10.0.23.2"},
            {"device": "r1", "subnet": "lan1", "ip": "10.0.1.1"},
            {"device": "r2", "subnet": "lan2", "ip": "10.0.2.1"},
            {"device": "r3", "subnet": "lan3", "ip": "10.0.3.1"},
            {"device": "pc1", "subnet": "lan1"},
            {"device": "pc2", "subnet": "lan2"},
            {"device": "pc3", "subnet": "lan3"},
        ],
        "routing": {"mode": "rip", "rip": {
            "version": 2,
            "networks": ["10.0.12.0/30", "10.0.23.0/30", "10.0.1.0/24",
                         "10.0.2.0/24", "10.0.3.0/24"],
        }},
        "requirements": REQ,
        "acceptance_criteria": [flow("A-1", "pc1", "pc2"),
                               flow("A-2", "pc2", "pc3"),
                               flow("A-3", "pc1", "pc3")],
    })

    save("S3-A5-default-redistribute", {
        "name": "S3.A5 default route and redistribution",
        "devices": routers("edge", "internal", "isp") + switches("s1", "s2")
        + [{"id": "pc", "role": "pc"}, {"id": "srv", "role": "server"}],
        "links": [
            {"id": "l-edge-internal", "a": {"device": "edge"},
             "b": {"device": "internal"}},
            {"id": "l-edge-isp", "a": {"device": "edge"}, "b": {"device": "isp"}},
            {"id": "l-internal-s1", "a": {"device": "internal"},
             "b": {"device": "s1"}},
            {"id": "l-s1-pc", "a": {"device": "s1"}, "b": {"device": "pc"}},
            {"id": "l-isp-s2", "a": {"device": "isp"}, "b": {"device": "s2"}},
            {"id": "l-s2-srv", "a": {"device": "s2"}, "b": {"device": "srv"}},
        ],
        "subnets": [
            {"id": "wan12", "cidr": "10.0.12.0/30"},
            {"id": "wan13", "cidr": "203.0.113.0/30"},
            {"id": "internal", "cidr": "10.1.0.0/24"},
            {"id": "server", "cidr": "198.51.100.0/24"},
        ],
        "ip_assignments": [
            {"device": "edge", "subnet": "wan12", "ip": "10.0.12.1"},
            {"device": "internal", "subnet": "wan12", "ip": "10.0.12.2"},
            {"device": "edge", "subnet": "wan13", "ip": "203.0.113.1"},
            {"device": "isp", "subnet": "wan13", "ip": "203.0.113.2"},
            {"device": "internal", "subnet": "internal", "ip": "10.1.0.1"},
            {"device": "isp", "subnet": "server", "ip": "198.51.100.1"},
            {"device": "pc", "subnet": "internal"},
            {"device": "srv", "subnet": "server"},
        ],
        "routing": {
            "mode": "ospf",
            "ospf": {
                "process_id": 1,
                "devices": ["edge", "internal"],
                "networks": ["10.0.12.0/30", "10.1.0.0/24"],
                "default_information_originate": True,
            },
            "static_routes": [
                {"device": "edge", "destination": "0.0.0.0/0",
                 "next_hop": "203.0.113.2"},
                {"device": "isp", "destination": "10.1.0.0/24",
                 "next_hop": "203.0.113.1"},
            ],
        },
        "requirements": REQ,
        "acceptance_criteria": [flow("A-1", "pc", "srv")],
    })

    save("S3-A7-dhcp-relay", {
        "name": "S3.A7 DHCP relay",
        "devices": routers("central", "remote") + switches("s1", "s2")
        + pcs("pc1", "pc2"),
        "links": [
            {"id": "l-central-remote", "a": {"device": "central"},
             "b": {"device": "remote"}},
            {"id": "l-central-s1", "a": {"device": "central"},
             "b": {"device": "s1"}},
            {"id": "l-s1-pc1", "a": {"device": "s1"}, "b": {"device": "pc1"}},
            {"id": "l-remote-s2", "a": {"device": "remote"},
             "b": {"device": "s2"}},
            {"id": "l-s2-pc2", "a": {"device": "s2"}, "b": {"device": "pc2"}},
        ],
        "subnets": [
            {"id": "wan", "cidr": "10.0.12.0/30"},
            {"id": "central", "cidr": "10.0.1.0/24"},
            {"id": "remote", "cidr": "10.0.2.0/24"},
        ],
        "ip_assignments": [
            {"device": "central", "subnet": "wan", "ip": "10.0.12.1",
             "interface_hint": "GigabitEthernet0/0"},
            {"device": "remote", "subnet": "wan", "ip": "10.0.12.2",
             "interface_hint": "GigabitEthernet0/0"},
            {"device": "central", "subnet": "central", "ip": "10.0.1.1",
             "interface_hint": "GigabitEthernet0/1"},
            {"device": "remote", "subnet": "remote", "ip": "10.0.2.1",
             "interface_hint": "GigabitEthernet0/1"},
            {"device": "pc1", "subnet": "central"},
            {"device": "pc2", "subnet": "remote", "dhcp": True},
        ],
        "routing": {
            "mode": "static",
            "static_routes": [
                {"device": "central", "destination": "10.0.2.0/24",
                 "next_hop": "10.0.12.2"},
                {"device": "remote", "destination": "10.0.1.0/24",
                 "next_hop": "10.0.12.1"},
            ],
            "dhcp_pools": [{
                "device": "central", "subnet": "remote",
                "default_router": "10.0.2.1", "excluded": ["10.0.2.1"],
            }],
            "dhcp_relays": [{
                "device": "remote", "interface": "GigabitEthernet0/1",
                "helpers": ["10.0.1.1"],
            }],
        },
        "requirements": [{"id": "R-1", "statement":
                          "Remote PC leases an address from the central DHCP server"}],
        "acceptance_criteria": [flow("A-1", "pc2", "pc1")],
    })

    save("S3-A8-hsrp", {
        "name": "S3.A8 HSRP failover",
        "devices": routers("r1", "r2") + switches("sw1", "sw2")
        + [{"id": "pc", "role": "pc"}, {"id": "srv", "role": "server"}],
        "links": [
            {"id": "l-r1-sw1", "a": {"device": "r1"}, "b": {"device": "sw1"}},
            {"id": "l-r2-sw1", "a": {"device": "r2"}, "b": {"device": "sw1"}},
            {"id": "l-pc-sw1", "a": {"device": "pc"}, "b": {"device": "sw1"}},
            {"id": "l-r1-sw2", "a": {"device": "r1"}, "b": {"device": "sw2"}},
            {"id": "l-r2-sw2", "a": {"device": "r2"}, "b": {"device": "sw2"}},
            {"id": "l-srv-sw2", "a": {"device": "srv"}, "b": {"device": "sw2"}},
        ],
        "subnets": [
            {"id": "lan", "cidr": "10.0.10.0/24"},
            {"id": "srv", "cidr": "10.0.20.0/24"},
        ],
        "ip_assignments": [
            {"device": "r1", "subnet": "lan", "ip": "10.0.10.2",
             "interface_hint": "GigabitEthernet0/0"},
            {"device": "r2", "subnet": "lan", "ip": "10.0.10.3",
             "interface_hint": "GigabitEthernet0/0"},
            {"device": "r1", "subnet": "srv", "ip": "10.0.20.2",
             "interface_hint": "GigabitEthernet0/1"},
            {"device": "r2", "subnet": "srv", "ip": "10.0.20.3",
             "interface_hint": "GigabitEthernet0/1"},
            {"device": "pc", "subnet": "lan", "gateway": "10.0.10.254"},
            {"device": "srv", "subnet": "srv"},
        ],
        "routing": {"hsrp": [
            {"device": "r1", "interface": "GigabitEthernet0/0", "group": 1,
             "virtual_ip": "10.0.10.254", "priority": 120, "preempt": True},
            {"device": "r2", "interface": "GigabitEthernet0/0", "group": 1,
             "virtual_ip": "10.0.10.254", "priority": 100, "preempt": True},
        ]},
        "requirements": [{"id": "R-1", "statement":
                          "The LAN keeps a gateway when the active router fails"}],
        "acceptance_criteria": [flow("A-1", "pc", "srv")],
    })

    manifest = {
        "version": 1,
        "specs": [
            {"id": "S3-A1", "file": "S3-A1-ospf-single.json",
             "title": "OSPF single-area"},
            {"id": "S3-A2", "file": "S3-A2-ospf-multi-area.json",
             "title": "OSPF multi-area"},
            {"id": "S3-A3", "file": "S3-A3-eigrp.json", "title": "EIGRP"},
            {"id": "S3-A4", "file": "S3-A4-ripv2.json", "title": "RIP v2"},
            {"id": "S3-A5", "file": "S3-A5-default-redistribute.json",
             "title": "default route + originate"},
            {"id": "S3-A7", "file": "S3-A7-dhcp-relay.json",
             "title": "DHCP relay"},
            {"id": "S3-A8", "file": "S3-A8-hsrp.json", "title": "HSRP failover"},
        ],
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2),
                                       encoding="utf-8")
    print(f"wrote {len(list(OUT.glob('*.json')))} files to {OUT}")


if __name__ == "__main__":
    main()
