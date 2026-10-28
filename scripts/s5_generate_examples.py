"""Generate the S5.1 canonical intent examples (data/intents/).

Valid examples exercise every expander-v1 shape; invalid examples pin the
schema errors, the lint findings and the expander refusals. Re-running this
script regenerates the same bytes (fixed content, no timestamps).
"""

from __future__ import annotations

import json
from pathlib import Path

from src.network_autopilot.ai.intent import HavynIntent

ROOT = Path(__file__).resolve().parents[1]
OUT_VALID = ROOT / "data" / "intents" / "examples"
OUT_INVALID = ROOT / "data" / "intents" / "invalid"


def req(rid: str, statement: str, type_: str = "connectivity", **kw):
    return {"id": rid, "statement": statement, "type": type_, **kw}


def hg(gid: str, area: str, count: int = 2, device: str = "pc", **kw):
    return {"id": gid, "area": area, "count": count, "device": device, **kw}


def dump(intent: dict, path: Path) -> None:
    path.write_text(json.dumps(intent, indent=2, sort_keys=True) + "\n", encoding="utf-8")


VALID: list[tuple[str, dict]] = [
    ("e01-small-office", {
        "intent_version": "1",
        "name": "small office",
        "description": "Five DHCP PCs behind one router (T01 shape).",
        "language": "en",
        "sites": [{"id": "office", "areas": [{"id": "main", "host_groups": [
            hg("staff", "main", 5)]}]}],
        "vlans": [{"id": "users", "name": "users", "required_hosts": 10, "dhcp": True}],
        "requirements": [req("REQ-001", "Every PC gets a DHCP address and reaches the others")],
    }),
    ("e02-two-vlan-office", {
        "intent_version": "1",
        "name": "two vlan office",
        "sites": [{"id": "office", "areas": [{"id": "floor1", "host_groups": [
            hg("staff", "floor1", 4)]}, {"id": "floor2", "host_groups": [
            hg("ops", "floor2", 4)]}]}],
        "vlans": [
            {"id": "staff", "name": "staff", "required_hosts": 10, "dhcp": True},
            {"id": "ops", "name": "operations", "required_hosts": 10, "dhcp": True},
        ],
        "requirements": [req("REQ-001", "Staff and operations reach each other across VLANs")],
    }),
    ("e03-campus-three-floors", {
        "intent_version": "1",
        "name": "three floor campus",
        "sites": [{"id": "campus", "areas": [
            {"id": "floor1", "host_groups": [hg("f1", "floor1", 10)]},
            {"id": "floor2", "host_groups": [hg("f2", "floor2", 10)]},
            {"id": "floor3", "host_groups": [hg("f3", "floor3", 10)]},
        ]}],
        "vlans": [
            {"id": "v10", "name": "floor1", "required_hosts": 20, "dhcp": True},
            {"id": "v20", "name": "floor2", "required_hosts": 20, "dhcp": True},
            {"id": "v30", "name": "floor3", "required_hosts": 20, "dhcp": True},
        ],
        "requirements": [req("REQ-001", "Users on all floors communicate")],
    }),
    ("e04-wireless-office", {
        "intent_version": "1",
        "name": "wireless office",
        "sites": [{"id": "office", "areas": [{"id": "main", "host_groups": [
            hg("wired", "main", 2), hg("wifi", "main", 3, "laptop")]}]}],
        "vlans": [{"id": "users", "name": "users", "required_hosts": 10, "dhcp": True}],
        "wireless": [{"id": "cell1", "site": "office", "area": "main",
                      "ssid": "OfficeNet", "security": "wpa2",
                      "passphrase": "officepass123", "client_group_ids": ["wifi"]}],
        "requirements": [req("REQ-001", "Laptops associate and reach wired PCs")],
    }),
    ("e05-iot-home", {
        "intent_version": "1",
        "name": "iot home",
        "sites": [{"id": "home", "areas": [{"id": "living", "host_groups": [
            hg("laptop", "living", 1, "laptop")]}]}],
        "vlans": [{"id": "users", "name": "home", "required_hosts": 10, "dhcp": True}],
        "wireless": [{"id": "homecell", "site": "home", "area": "living",
                      "ssid": "HomeNet", "security": "wpa2",
                      "passphrase": "homepass123", "client_group_ids": ["laptop"]}],
        "iot": [{"id": "led", "site": "home", "area": "living", "count": 2}],
        "requirements": [
            req("REQ-001", "Laptop reaches the home network"),
            req("REQ-002", "Smart LEDs are cabled and registered", "operational"),
        ],
    }),
    ("e06-server-farm", {
        "intent_version": "1",
        "name": "server farm",
        "sites": [{"id": "hq", "areas": [{"id": "office", "host_groups": [
            hg("staff", "office", 4)]}]}],
        "vlans": [
            {"id": "users", "name": "users", "required_hosts": 10, "dhcp": True},
            {"id": "srv", "name": "servers", "required_hosts": 5},
        ],
        "servers": [{"id": "srv1", "site": "hq", "vlan": "srv",
                     "services": ["dns", "http", "ftp"],
                     "records": [{"name": "hq.local", "ip": "10.1.1.10"}],
                     "pages": {"/index.html": "welcome"}}],
        "requirements": [req("REQ-001", "Staff reach the server farm services")],
    }),
    ("e07-internet-nat", {
        "intent_version": "1",
        "name": "nat internet",
        "sites": [{"id": "office", "areas": [{"id": "main", "host_groups": [
            hg("staff", "main", 3)]}]}],
        "vlans": [{"id": "users", "name": "users", "required_hosts": 10, "dhcp": True}],
        "internet": {"access": "wan", "nat": True, "public_server": True},
        "requirements": [req("REQ-001", "PCs reach the public server through PAT")],
    }),
    ("e08-firewall-perimeter", {
        "intent_version": "1",
        "name": "asa perimeter",
        "sites": [{"id": "office", "areas": [{"id": "main", "host_groups": [
            hg("staff", "main", 3)]}]}],
        "vlans": [{"id": "users", "name": "users", "required_hosts": 10, "dhcp": True}],
        "internet": {"access": "wan", "nat": False, "public_server": True},
        "firewall": {"enabled": True, "model": "asa5506", "guest_isolation": False},
        "requirements": [
            req("REQ-001", "Inside reaches the public server"),
            req("REQ-002", "Outside cannot initiate into the inside", "security",
                priority="critical"),
        ],
    }),
    ("e09-firewall-dmz", {
        "intent_version": "1",
        "name": "asa dmz",
        "sites": [{"id": "office", "areas": [{"id": "main", "host_groups": [
            hg("staff", "main", 2)]}]}],
        "vlans": [{"id": "users", "name": "users", "required_hosts": 10, "dhcp": True}],
        "internet": {"access": "wan", "nat": False, "public_server": False},
        "firewall": {"enabled": True, "model": "asa5506", "dmz": True},
        "requirements": [req("REQ-001", "Inside users reach the internet path")],
    }),
    ("e10-dsl-internet", {
        "intent_version": "1",
        "name": "dsl internet",
        "sites": [{"id": "home", "areas": [{"id": "main", "host_groups": [
            hg("pc", "main", 2)]}]}],
        "vlans": [{"id": "users", "name": "home", "required_hosts": 10, "dhcp": True}],
        "internet": {"access": "dsl", "nat": True, "public_server": True},
        "requirements": [req("REQ-001", "Home PCs reach the internet test server")],
    }),
    ("e11-cable-internet", {
        "intent_version": "1",
        "name": "cable internet",
        "sites": [{"id": "home", "areas": [{"id": "main", "host_groups": [
            hg("pc", "main", 2)]}]}],
        "vlans": [{"id": "users", "name": "home", "required_hosts": 10, "dhcp": True}],
        "internet": {"access": "cable", "nat": True, "public_server": True},
        "requirements": [req("REQ-001", "Home PCs reach the internet test server")],
    }),
    ("e12-two-sites-serial", {
        "intent_version": "1",
        "name": "hq branch serial",
        "sites": [
            {"id": "hq", "areas": [{"id": "main", "host_groups": [hg("hqstaff", "main", 3)]}]},
            {"id": "branch", "areas": [{"id": "main", "host_groups": [hg("brstaff", "main", 2)]}]},
        ],
        "vlans": [
            {"id": "hqusers", "name": "hq users", "site": "hq", "required_hosts": 10, "dhcp": True},
            {"id": "brusers", "name": "branch users", "site": "branch", "required_hosts": 10, "dhcp": True},
        ],
        "requirements": [req("REQ-001", "HQ and branch reach each other over the WAN")],
    }),
    ("e13-three-sites", {
        "intent_version": "1",
        "name": "three sites",
        "sites": [
            {"id": "hq", "areas": [{"id": "main", "host_groups": [hg("hqstaff", "main", 2)]}]},
            {"id": "branch1", "areas": [{"id": "main", "host_groups": [hg("b1staff", "main", 2)]}]},
            {"id": "branch2", "areas": [{"id": "main", "host_groups": [hg("b2staff", "main", 2)]}]},
        ],
        "vlans": [
            {"id": "hqusers", "name": "hq", "site": "hq", "required_hosts": 10, "dhcp": True},
            {"id": "b1users", "name": "branch1", "site": "branch1", "required_hosts": 10, "dhcp": True},
            {"id": "b2users", "name": "branch2", "site": "branch2", "required_hosts": 10, "dhcp": True},
        ],
        "requirements": [req("REQ-001", "All sites reach each other")],
    }),
    ("e14-ospf-two-sites", {
        "intent_version": "1",
        "name": "ospf two sites",
        "sites": [
            {"id": "hq", "areas": [{"id": "main", "host_groups": [hg("hqstaff", "main", 2)]}]},
            {"id": "branch", "areas": [{"id": "main", "host_groups": [hg("brstaff", "main", 2)]}]},
        ],
        "vlans": [
            {"id": "hqusers", "name": "hq", "site": "hq", "required_hosts": 10, "dhcp": True},
            {"id": "brusers", "name": "branch", "site": "branch", "required_hosts": 10, "dhcp": True},
        ],
        "routing": {"mode": "ospf", "ospf_area": "0"},
        "requirements": [req("REQ-001", "Sites route through OSPF")],
    }),
    ("e15-static-addressing", {
        "intent_version": "1",
        "name": "static lab",
        "sites": [{"id": "lab", "areas": [{"id": "main", "host_groups": [
            hg("nodes", "main", 4, "pc", addressing="static")]}]}],
        "vlans": [{"id": "users", "name": "lab", "required_hosts": 10}],
        "requirements": [req("REQ-001", "Static hosts reach each other")],
    }),
    ("e16-large-campus", {
        "intent_version": "1",
        "name": "large campus",
        "sites": [{"id": "campus", "areas": [
            {"id": "floor1", "host_groups": [hg("f1", "floor1", 40)]},
            {"id": "floor2", "host_groups": [hg("f2", "floor2", 40)]},
        ]}],
        "vlans": [
            {"id": "v1", "name": "floor1", "required_hosts": 60, "dhcp": True},
            {"id": "v2", "name": "floor2", "required_hosts": 60, "dhcp": True},
        ],
        "internet": {"access": "wan", "nat": True, "public_server": True},
        "requirements": [req("REQ-001", "All floors and the internet path work")],
    }),
    ("e17-mixed-language", {
        "intent_version": "1",
        "name": "mixed language office",
        "language": "mixed",
        "source_excerpt": "Bhai ek office network chahiye: 6 PC aur 2 laptop, sab internet bhi chahiye.",
        "sites": [{"id": "office", "areas": [{"id": "main", "host_groups": [
            hg("pc", "main", 6), hg("wifi", "main", 2, "laptop")]}]}],
        "vlans": [{"id": "users", "name": "users", "required_hosts": 15, "dhcp": True}],
        "internet": {"access": "wan", "nat": True, "public_server": True},
        "wireless": [{"id": "cell1", "site": "office", "area": "main",
                      "ssid": "Office", "security": "wpa2",
                      "passphrase": "password123", "client_group_ids": ["wifi"]}],
        "requirements": [
            req("REQ-001", "Sab devices ek dusre ko reach karein", "connectivity"),
            req("REQ-002", "Internet sab ke liye chahiye", "connectivity"),
        ],
    }),
    ("e18-long-document", {
        "intent_version": "1",
        "name": "enterprise document campus",
        "language": "en",
        "source_excerpt": "Section 1: topology overview... Section 4: VLAN plan... "
            "Section 8: security restrictions... Section 13: routing... "
            "Section 19: wireless... Section 24: management.",
        "sites": [{"id": "hq", "areas": [
            {"id": "floor1", "host_groups": [hg("f1users", "floor1", 12)]},
            {"id": "floor2", "host_groups": [hg("f2users", "floor2", 12),
                                             hg("f2wifi", "floor2", 4, "laptop")]},
        ]}],
        "vlans": [
            {"id": "f1", "name": "floor1", "required_hosts": 20, "dhcp": True},
            {"id": "f2", "name": "floor2", "required_hosts": 20, "dhcp": True},
        ],
        "wireless": [{"id": "hqcell", "site": "hq", "area": "floor2",
                      "ssid": "HQ-Staff", "security": "wpa2",
                      "passphrase": "hqstaffpass1", "client_group_ids": ["f2wifi"]}],
        "servers": [{"id": "srvc", "site": "hq", "vlan": "f1",
                     "services": ["dns", "http"]}],
        "internet": {"access": "wan", "nat": True, "public_server": True},
        "requirements": [
            req("REQ-001", "Floor users reach each other (section 1)", "connectivity",
                source="section_1"),
            req("REQ-002", "Wireless laptops associate to the staff SSID (section 19)",
                "connectivity", source="section_19", priority="high"),
            req("REQ-003", "Perimeter blocks outside-initiated traffic (section 8)",
                "security", source="section_8", priority="critical",
                dependencies=["REQ-001"]),
        ],
        "constraints": ["budget: existing 2960 switches reused"],
    }),
    ("e19-security-critical", {
        "intent_version": "1",
        "name": "security focused office",
        "sites": [{"id": "office", "areas": [{"id": "main", "host_groups": [
            hg("staff", "main", 4)]}]}],
        "vlans": [{"id": "users", "name": "users", "required_hosts": 10, "dhcp": True}],
        "internet": {"access": "wan", "nat": False, "public_server": True},
        "firewall": {"enabled": True, "model": "asa5506"},
        "requirements": [
            req("REQ-001", "Staff reach internal services", "connectivity"),
            req("REQ-002", "The perimeter blocks outside-initiated access", "security",
                priority="critical", confidence=0.99),
            req("REQ-003", "Addressing stays DHCP for staff", "addressing"),
        ],
    }),
    ("e20-fully-specified", {
        "intent_version": "1",
        "name": "complete office build",
        "language": "en",
        "sites": [{"id": "office", "areas": [
            {"id": "floor1", "host_groups": [hg("sales", "floor1", 8),
                                             hg("printers", "floor1", 1, "printer")]},
            {"id": "floor2", "host_groups": [hg("dev", "floor2", 8),
                                             hg("devwifi", "floor2", 4, "laptop")]},
        ]}],
        "vlans": [
            {"id": "salesv", "name": "sales", "required_hosts": 15, "dhcp": True},
            {"id": "devv", "name": "development", "required_hosts": 15, "dhcp": True},
        ],
        "wireless": [{"id": "devcell", "site": "office", "area": "floor2",
                      "ssid": "Dev", "security": "wpa2",
                      "passphrase": "devpass1234", "client_group_ids": ["devwifi"]}],
        "servers": [{"id": "core-srv", "site": "office", "vlan": "salesv",
                     "services": ["dns", "http", "ntp"]}],
        "internet": {"access": "wan", "nat": True, "public_server": True},
        "requirements": [
            req("REQ-001", "Sales and development reach each other", "connectivity",
                priority="high"),
            req("REQ-002", "Wireless development laptops associate", "connectivity"),
            req("REQ-003", "Servers stay static", "addressing"),
            req("REQ-004", "Internet through PAT", "connectivity"),
        ],
        "constraints": ["DHCP for user VLANs", "static servers"],
    }),
]

INVALID: list[tuple[str, dict, str]] = [
    ("i01-both-size-sources", {
        "intent_version": "1", "name": "bad sizing",
        "sites": [{"id": "s1", "areas": [{"id": "a1", "host_groups": [hg("g1", "a1")]}]}],
        "vlans": [{"id": "v1", "name": "v", "cidr": "10.1.0.0/24", "required_hosts": 10}],
    }, "schema"),
    ("i02-wpa2-without-passphrase", {
        "intent_version": "1", "name": "bad wifi",
        "sites": [{"id": "s1", "areas": [{"id": "a1", "host_groups": [hg("g1", "a1", 1, "laptop")]}]}],
        "wireless": [{"id": "c1", "site": "s1", "ssid": "x", "security": "wpa2",
                      "client_group_ids": ["g1"]}],
    }, "schema"),
    ("i03-duplicate-site", {
        "intent_version": "1", "name": "dup site",
        "sites": [{"id": "s1", "areas": []}, {"id": "s1", "areas": []}],
    }, "schema"),
    ("i04-bad-requirement-id", {
        "intent_version": "1", "name": "bad req id",
        "sites": [{"id": "s1", "areas": []}],
        "requirements": [{"id": "R-1", "statement": "wrong prefix"}],
    }, "schema"),
    ("i05-unknown-wireless-site", {
        "intent_version": "1", "name": "bad cell site",
        "sites": [{"id": "s1", "areas": []}],
        "wireless": [{"id": "c1", "site": "nowhere", "ssid": "x", "security": "open"}],
    }, "schema"),
    ("i06-guest-isolation-refusal", {
        "intent_version": "1", "name": "guest isolation",
        "sites": [{"id": "s1", "areas": [{"id": "a1", "host_groups": [hg("g1", "a1", 4)]}]}],
        "vlans": [{"id": "v1", "name": "users", "required_hosts": 10, "dhcp": True}],
        "internet": {"access": "wan"},
        "firewall": {"enabled": True, "guest_isolation": True},
    }, "refusal"),
    ("i07-vlan-too-small", {
        "intent_version": "1", "name": "vlan too small",
        "sites": [{"id": "s1", "areas": [{"id": "a1", "host_groups": [
            hg("g1", "a1", 30, vlan="v1")]}]}],
        "vlans": [{"id": "v1", "name": "users", "cidr": "10.1.0.0/29", "dhcp": True}],
    }, "contradiction"),
    ("i08-wireless-client-without-ap", {
        "intent_version": "1", "name": "wifi without ap",
        "sites": [{"id": "s1", "areas": [{"id": "a1", "host_groups": [hg("g1", "a1", 2, "laptop")]}]}],
        "vlans": [{"id": "v1", "name": "users", "required_hosts": 10, "dhcp": True}],
    }, "missing-information"),
    ("i09-wired-device-on-wireless", {
        "intent_version": "1", "name": "pc on wifi",
        "sites": [{"id": "s1", "areas": [{"id": "a1", "host_groups": [hg("g1", "a1", 2, "pc")]}]}],
        "vlans": [{"id": "v1", "name": "users", "required_hosts": 10, "dhcp": True}],
        "wireless": [{"id": "c1", "site": "s1", "ssid": "x", "security": "open",
                      "client_group_ids": ["g1"]}],
    }, "contradiction"),
    ("i10-firewall-without-internet", {
        "intent_version": "1", "name": "fw without internet",
        "sites": [{"id": "s1", "areas": [{"id": "a1", "host_groups": [hg("g1", "a1", 2)]}]}],
        "vlans": [{"id": "v1", "name": "users", "required_hosts": 10, "dhcp": True}],
        "firewall": {"enabled": True},
    }, "missing-information"),
]


def main() -> None:
    OUT_VALID.mkdir(parents=True, exist_ok=True)
    OUT_INVALID.mkdir(parents=True, exist_ok=True)
    for name, data in VALID:
        HavynIntent.model_validate(data)  # refuse to ship an invalid example
        dump(data, OUT_VALID / f"{name}.json")
    for name, data, _kind in INVALID:
        dump(data, OUT_INVALID / f"{name}.json")
    print(f"wrote {len(VALID)} valid + {len(INVALID)} invalid intents")


if __name__ == "__main__":
    main()
