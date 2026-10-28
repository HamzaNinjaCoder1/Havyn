"""Model A drafts: AI-assisted owner-gold candidate pool (Stage 5 prompt authoring).

PROVENANCE (do not bend):
    Every record here is an AI *assisted draft*. None of it is owner-authored
    and none of it is friend/colleague-authored. Hamza reviews each candidate,
    rewrites it in his own wording, confirms the expected intent, and only then
    adopts it into ``datasets/gold-v1`` as owner-authored. Until adoption these
    are candidate drafts with ``provenance.model_a`` provenance, never gold.

SOURCE OF TRUTH:
    The ``expected_intent`` (or the refusal) is the source of truth. Model A may
    vary wording, structure, tone, ordering, verbosity, user style and language,
    but must not add network facts absent from the intent, and must not drop a
    requirement the intent carries.

Each record:
    id                 stable candidate id (oc-NNN). NOT a gold-NNN id.
    prompt             the humanized candidate request (Model A draft).
    language           en | roman-urdu | mixed  (Schema v1 language labels).
    difficulty         L1..L5.
    category           scenario category.
    scenario           one-line scenario description.
    coverage_cell      which coverage cell it targets (repo matrix).
    length_category    short | medium | long | document.
    expected_intent    Havyn Intent v1 dict, OR null when expect_refusal.
    expect_refusal     true for ambiguous/contradictory/impossible candidates.
    expected_behaviour what the correct system behaviour is (for refusal cases:
                       which clarification to ask, or which named refusal).
    why_valuable       why this example earns a slot in the 100.
    research_inspiration  public source(s) that informed the realism (advisory
                       only; the scenario is original and Havyn-validated).

Language policy: technical networking terms (VLAN, DHCP, OSPF, DMZ, SSID,
WPA2) stay in English even inside Roman-Urdu / mixed prompts, because that is
how real Pakistani/South-Asian engineers actually speak.
"""

from __future__ import annotations

# Convenience constructors keep intent dicts readable and schema-safe.
def _site(sid, areas):
    return {"id": sid, "areas": areas}

def _area(aid, groups, kind="area"):
    return {"id": aid, "kind": kind, "host_groups": groups}

def _pc(gid, area, count, vlan=None, addressing=None):
    g = {"id": gid, "area": area, "count": count, "device": "pc"}
    if vlan is not None:
        g["vlan"] = vlan
    if addressing is not None:
        g["addressing"] = addressing
    return g

def _wifi(gid, area, count, device="laptop", vlan=None):
    g = {"id": gid, "area": area, "count": count, "device": device}
    if vlan is not None:
        g["vlan"] = vlan
    return g

def _vlan(vid, name, site=None, required=None, cidr=None, dhcp=False,
          guest=False, mgmt=False, purpose=None):
    v = {"id": vid, "name": name, "dhcp": dhcp}
    if site is not None:
        v["site"] = site
    if required is not None:
        v["required_hosts"] = required
    if cidr is not None:
        v["cidr"] = cidr
    if guest:
        v["is_guest"] = True
    if mgmt:
        v["is_management"] = True
    if purpose is not None:
        v["purpose"] = purpose
    return v

def _req(rid, statement, rtype="connectivity", priority="normal", source="prompt"):
    return {"id": rid, "statement": statement, "type": rtype,
            "priority": priority, "source": source}

CANDIDATES: list[dict] = [

# ---------------------------------------------------------------------------
# L1 — simple / basic  (English + Roman Urdu + mixed)
# ---------------------------------------------------------------------------
{
    "id": "oc-001",
    "language": "en", "difficulty": "L1", "category": "basic",
    "scenario": "Tiny four-PC office LAN, one VLAN, no internet.",
    "coverage_cell": "small-s1-a1-single-static-none-none",
    "length_category": "short",
    "prompt": "Set up a simple network for a small back-office. Four desktop PCs, "
              "all on one network, they just need to reach each other. No internet "
              "needed for now.",
    "expected_intent": {
        "intent_version": "1", "name": "small-backoffice",
        "sites": [_site("office", [_area("main", [_pc("pcs", "main", 4)])])],
        "vlans": [_vlan("officeusers", "Office users", site="office", required=10)],
        "routing": {"mode": "connected"},
        "requirements": [_req("REQ-001", "All four PCs reach each other")],
    },
    "expect_refusal": False,
    "expected_behaviour": "Build the single-VLAN LAN; confirm connectivity intent.",
    "why_valuable": "Baseline L1: proves the model handles a plain, complete request without inventing internet or extra VLANs.",
    "research_inspiration": [],
},
{
    "id": "oc-002",
    "language": "roman-urdu", "difficulty": "L1", "category": "basic",
    "scenario": "Do PC, aik hi network, sirf aapas mein communicate.",
    "coverage_cell": "small-s1-a1-single-static-none-none",
    "length_category": "short",
    "prompt": "Mujhe apne chhote office ka network banana hai. Do desktop PCs hain, "
              "dono ko aapas mein chalna hai, bas itna hi kaam hai. Internet ki "
              "abhi zaroorat nahi.",
    "expected_intent": {
        "intent_version": "1", "name": "chota-office",
        "sites": [_site("dokan", [_area("main", [_pc("pc1", "main", 2)])])],
        "vlans": [_vlan("users", "Users", site="dokan", required=8)],
        "routing": {"mode": "connected"},
        "requirements": [_req("REQ-001", "Dono PCs aapas mein reach karein")],
    },
    "expect_refusal": False,
    "expected_behaviour": "Build the two-PC LAN. Language stays Roman Urdu; tech terms (PC, network) remain English.",
    "why_valuable": "Natural Roman-Urdu L1. Real users mix scripts; proves the model is not English-only.",
    "research_inspiration": [],
},
{
    "id": "oc-003",
    "language": "en", "difficulty": "L2", "category": "vlan-dhcp",
    "scenario": "Two-department office, staff + admin VLANs, DHCP everywhere.",
    "coverage_cell": "small-s1-a2-multi-dhcp-dsl-none",
    "length_category": "medium",
    "prompt": "We're moving into a small office with a reception area and a finance "
              "room. I want the reception PCs and the finance PCs on separate networks "
              "so finance traffic stays private. Everyone should get their IP automatically.",
    "expected_intent": {
        "intent_version": "1", "name": "office-two-dept",
        "sites": [_site("office", [
            _area("reception", [_pc("receptionpcs", "reception", 5, vlan="reception")]),
            _area("finance", [_pc("financepcs", "finance", 4, vlan="finance")]),
        ])],
        "vlans": [
            _vlan("reception", "Reception", site="office", required=12, dhcp=True),
            _vlan("finance", "Finance", site="office", required=12, dhcp=True),
        ],
        "routing": {"mode": "connected"},
        "requirements": [
            _req("REQ-001", "Reception and finance are on separate networks"),
            _req("REQ-002", "Finance traffic is isolated from reception", "isolation"),
            _req("REQ-003", "All hosts receive addresses automatically", "addressing"),
        ],
    },
    "expect_refusal": False,
    "expected_behaviour": "Two VLANs, both DHCP, finance isolated from reception via separate subnets.",
    "why_valuable": "Classic small-business segmentation; maps to real 'reception vs finance' language, not textbook 'VLAN 10/20'.",
    "research_inspiration": ["https://netraclos.com/vlans-and-guest-wifi-for-small-business-networks"],
},
{
    "id": "oc-004",
    "language": "mixed", "difficulty": "L2", "category": "wan-multi-site",
    "scenario": "Head office + one branch connected, DHCP at both, internet both.",
    "coverage_cell": "small-s2-a1-multi-dhcp-wan-none",
    "length_category": "medium",
    "prompt": "Head office aur ek branch ko connect karna hai. Dono jagah DHCP chahiye "
              "and dono ko internet bhi chahiye. Branch se head office tak traffic "
              "static routing se jaani chahiye, so that routes haath se set karni "
              "parein aur koi dynamic protocol configure na karna pare.",
    "expected_intent": {
        "intent_version": "1", "name": "ho-branch",
        "sites": [
            _site("headoffice", [_area("floor1", [_pc("hopcs", "floor1", 8)])]),
            _site("branch", [_area("shop", [_pc("branchpcs", "shop", 5)])]),
        ],
        "vlans": [
            _vlan("housers", "HO users", site="headoffice", required=16, dhcp=True),
            _vlan("branchusers", "Branch users", site="branch", required=12, dhcp=True),
        ],
        "routing": {"mode": "static"},
        "internet": {"access": "wan", "nat": True, "public_server": False},
        "requirements": [
            _req("REQ-001", "Head office and branch reach each other"),
            _req("REQ-002", "Both sites have internet access"),
            _req("REQ-003", "Both sites use DHCP", "addressing"),
        ],
    },
    "expect_refusal": False,
    "expected_behaviour": "Two sites, static routing between them, WAN internet with NAT at each, DHCP per site.",
    "why_valuable": "Mixed-language multi-site; tests cross-site routing + internet without leaking English-only phrasing.",
    "research_inspiration": ["https://www.cisco.com/c/en/us/td/docs/voice_ip_comm/cvd/G2basicsmbrnt/G2basicsmbrnt_book/FeatServ.html"],
},

# ---------------------------------------------------------------------------
# L2/L3 — routing
# ---------------------------------------------------------------------------
{
    "id": "oc-005",
    "language": "en", "difficulty": "L3", "category": "routing",
    "scenario": "Three-floor building, one VLAN per floor, OSPF between floors.",
    "coverage_cell": "medium-s1-a3-multi-static-ospf-wan-none",
    "length_category": "long",
    "prompt": "We occupy three floors of one building. Treat each floor as its own "
              "network — ground floor for walk-in customers' kiosks, first floor for "
              "staff, second floor for management. All floors must talk to each other "
              "and I'd like a proper routing protocol rather than hand-written routes, "
              "because this network will grow. Staff and management should not share "
              "a broadcast domain. No internet yet.",
    "expected_intent": {
        "intent_version": "1", "name": "three-floor",
        "sites": [_site("building", [
            _area("ground", [_pc("kiosks", "ground", 6, vlan="guestkiosk")]),
            _area("first", [_pc("staffpcs", "first", 10, vlan="staffnet")]),
            _area("second", [_pc("mgmtpcs", "second", 4, vlan="mgmtnet")]),
        ])],
        "vlans": [
            _vlan("guestkiosk", "Guest kiosks", site="building", required=12, dhcp=True),
            _vlan("staffnet", "Staff", site="building", required=20, dhcp=True),
            _vlan("mgmtnet", "Management", site="building", required=10, dhcp=True),
        ],
        "routing": {"mode": "ospf", "ospf_area": "0"},
        "requirements": [
            _req("REQ-001", "Every floor reaches every other floor"),
            _req("REQ-002", "Staff and management are on separate networks", "isolation"),
            _req("REQ-003", "Use a dynamic routing protocol, not static routes", "operational"),
        ],
    },
    "expect_refusal": False,
    "expected_behaviour": "Three VLANs, OSPF area 0, staff/management separated by subnet.",
    "why_valuable": "Genuinely asks for OSPF by intent ('proper routing protocol'), not by keyword; realistic growth motivation.",
    "research_inspiration": ["https://www.cisco.com/c/dam/en/us/td/docs/solutions/CVD/Oct2015/Campus_LAN_Wireless_LAN_Design_Oct2105.pdf"],
},
{
    "id": "oc-006",
    "language": "roman-urdu", "difficulty": "L3", "category": "routing",
    "scenario": "Do office, static routing, internet head office par.",
    "coverage_cell": "small-s2-a1-multi-static-wan-none",
    "length_category": "medium",
    "prompt": "Hamare do offices hain, Karachi aur Lahore. Karachi head office hai "
              "jahan internet line hai, Lahore ko Karachi ke through routing chahiye. "
              "Lahore ke computers ko internet bhi head office se milna chahiye. "
              "Static routing theek rahegi, protocol mat lagana.",
    "expected_intent": {
        "intent_version": "1", "name": "karachi-lahore",
        "sites": [
            _site("karachi", [_area("hq", [_pc("karachipcs", "hq", 8, vlan="karachinet")])]),
            _site("lahore", [_area("office", [_pc("lahorepcs", "office", 6, vlan="lahorenet")])]),
        ],
        "vlans": [
            _vlan("karachinet", "Karachi", site="karachi", required=16, dhcp=True),
            _vlan("lahorenet", "Lahore", site="lahore", required=12, dhcp=True),
        ],
        "routing": {"mode": "static"},
        "internet": {"access": "wan", "nat": True, "public_server": False},
        "requirements": [
            _req("REQ-001", "Lahore reaches Karachi"),
            _req("REQ-002", "Lahore gets internet through the head office"),
            _req("REQ-003", "Use static routing, not a dynamic protocol", "constraint"),
        ],
    },
    "expect_refusal": False,
    "expected_behaviour": "Static routes Karachi<->Lahore; internet/NAT at Karachi; Lahore routes via Karachi.",
    "why_valuable": "Roman-Urdu, real Pakistani cities, explicit 'don't use a protocol' constraint that tests instruction-following.",
    "research_inspiration": ["https://www.ibm.com/docs/en/i/7.5.0?topic=scenarios-scenario-basic-branch-office-connection"],
},

# ---------------------------------------------------------------------------
# L3/L4 — wireless + guest
# ---------------------------------------------------------------------------
{
    "id": "oc-007",
    "language": "en", "difficulty": "L3", "category": "wireless",
    "scenario": "Office with staff WiFi and a separate guest WiFi.",
    "coverage_cell": "small-s1-a2-multi-dhcp-cable-none",
    "length_category": "medium",
    "prompt": "Our office needs WiFi for staff laptops and a separate guest WiFi for "
              "visitors. Guests should only get internet — they must not be able to "
              "reach any of our internal machines. Staff laptops stay on the internal "
              "network. Cable internet is fine.",
    "expected_intent": {
        "intent_version": "1", "name": "office-wifi",
        "sites": [_site("office", [
            _area("workspace", [
                _pc("wiredpcs", "workspace", 4),
                _wifi("stafflaptops", "workspace", 5),
            ]),
        ])],
        "vlans": [
            _vlan("staffnet", "Staff", site="office", required=20, dhcp=True),
            _vlan("guestnet", "Guest", site="office", required=15, dhcp=True, guest=True),
        ],
        "routing": {"mode": "connected"},
        "internet": {"access": "cable", "nat": True, "public_server": False},
        "firewall": {"enabled": True, "model": "asa5506", "guest_isolation": True},
        "wireless": [{
            "id": "staffwifi", "site": "office", "area": "workspace",
            "ssid": "OfficeStaff", "security": "wpa2", "passphrase": "staffroom2026",
            "client_group_ids": ["stafflaptops"],
        }],
        "requirements": [
            _req("REQ-001", "Staff laptops join the internal network over WiFi"),
            _req("REQ-002", "Guest WiFi reaches only the internet, not internal machines", "security", "critical"),
            _req("REQ-003", "Guest network is isolated from staff network", "isolation", "high"),
        ],
    },
    "expect_refusal": True,
    "expected_intent": None,
    "expected_behaviour": "REFUSE-BY-NAME: guest_isolation is unsupported by expander v1 (no live-verified ACL "
                          "enforcement point). The correct output is the named refusal 'guest-isolation-unsupported' "
                          "plus an offer to build guest on a separate subnet without a guaranteed isolation ACL. "
                          "This is a REAL current limitation, not an invented one.",
    "why_valuable": "The single most-requested real-world feature (guest WiFi isolation) is exactly what the engine "
                    "refuses today. Trains 'I cannot safely build this yet' instead of a fake build. Flagged as a capability gap.",
    "research_inspiration": ["https://help.ui.com/hc/en-us/articles/23948850278295-Best-Practices-Guest-WiFi",
                             "https://spaces.cisco.com/six-best-practices-for-guest-wi-fi-security"],
},
{
    "id": "oc-008",
    "language": "mixed", "difficulty": "L2", "category": "wireless",
    "scenario": "Small shop with staff laptops and free customer WiFi.",
    "coverage_cell": "small-s1-a1-multi-dhcp-cable-none",
    "length_category": "short",
    "prompt": "Choti si shop hai jahan staff ke laptops aur customers, dono ko WiFi "
              "chahiye. Staff ke laptops apne internal network par rahen, and customers "
              "ke liye ek alag free WiFi ho, so that dono mix na hon. Cable internet "
              "available hai.",
    "expected_intent": {
        "intent_version": "1", "name": "shop-wifi",
        "sites": [_site("shop", [_area("floor", [
            _wifi("stafflaptops", "floor", 3, device="laptop", vlan="staffnet"),
            _wifi("customerdevices", "floor", 3, device="smartphone", vlan="customernet"),
        ])])],
        "vlans": [
            _vlan("staffnet", "Staff", site="shop", required=10, dhcp=True),
            _vlan("customernet", "Customer", site="shop", required=10, dhcp=True),
        ],
        "routing": {"mode": "connected"},
        "internet": {"access": "cable", "nat": True, "public_server": False},
        "wireless": [
            {"id": "staffwifi", "site": "shop", "area": "floor", "ssid": "ShopStaff",
             "security": "wpa2", "passphrase": "shopstaff2026",
             "client_group_ids": ["stafflaptops"]},
            {"id": "guestwifi", "site": "shop", "area": "floor", "ssid": "ShopGuest",
             "security": "open", "client_group_ids": ["customerdevices"]},
        ],
        "requirements": [
            _req("REQ-001", "Staff laptops get WiFi on the internal network"),
            _req("REQ-002", "Customers get a separate free WiFi network"),
            _req("REQ-003", "Both reach the internet"),
        ],
    },
    "expect_refusal": False,
    "expected_behaviour": "Two subnets (staff/customer) with two separate WiFi cells (staff WPA2, customer open), cable "
                          "internet. The two networks are stated as separate but guests are NOT promised to be blocked "
                          "from internal hosts, so no guest_isolation is involved and this builds — the contrast with "
                          "oc-007, which demands the isolation guarantee.",
    "why_valuable": "Tests the boundary: a shop with staff laptops and customer WiFi like oc-007 but WITHOUT the isolation "
                    "guarantee, so the model must not over-refuse. Paired contrast case.",
    "research_inspiration": ["https://www.evergreenhq.com/blog/how-to-setup-wi-fi-in-your-bar-or-restaurant"],
},

# ---------------------------------------------------------------------------
# L3/L4 — firewall / DMZ / security
# ---------------------------------------------------------------------------
{
    "id": "oc-009",
    "language": "en", "difficulty": "L4", "category": "firewall-security",
    "scenario": "Single office behind an ASA with a DMZ holding a public web server.",
    "coverage_cell": "small-s1-a1-single-dhcp-wan-asa-dmz",
    "length_category": "document",
    "prompt": "We're a small law firm with a single office and about twenty staff PCs. "
              "We host our own client-intake website and outside visitors have to be able "
              "to reach it, but that website must never sit on the same network as our "
              "internal file workstations. I want a perimeter firewall between us and the "
              "ISP — we're getting a WAN handoff. The staff PCs go on one trusted inside "
              "network and should get their addresses automatically. The website goes in "
              "its own DMZ segment that's reachable from the internet, and nothing from "
              "the internet should be able to start a connection into the inside. Keep the "
              "inside to that single LAN.",
    "expected_intent": {
        "intent_version": "1", "name": "lawfirm-dmz",
        "sites": [_site("firm", [_area("office", [_pc("staffpcs", "office", 18)])])],
        "vlans": [_vlan("inside", "Inside users", site="firm", required=24, dhcp=True)],
        "routing": {"mode": "static"},
        "internet": {"access": "wan", "nat": True, "public_server": False},
        "firewall": {"enabled": True, "model": "asa5506", "dmz": True},
        "requirements": [
            _req("REQ-001", "Outside visitors reach the public intake website", "connectivity", "high"),
            _req("REQ-002", "The website is in a DMZ separate from internal workstations", "security", "critical"),
            _req("REQ-003", "The internet cannot initiate connections into the inside network", "security", "critical"),
            _req("REQ-004", "Inside hosts are automatically addressed", "addressing"),
        ],
    },
    "expect_refusal": False,
    "expected_behaviour": "Single-VLAN inside + ASA5506 DMZ + WAN internet. The DMZ is engine-created (srv-dmz on "
                          "192.168.100.0/24, no services declared), so 'outside visitors reach the website' verifies "
                          "UNKNOWN — the engine has no per-server service evidence for the auto-created DMZ host; the DMZ "
                          "isolation and single-inside-VLAN shape match the verified T07/T08 DMZ shape.",
    "why_valuable": "Long-form, business-context DMZ request in the engine's verified shape. Forces requirement extraction "
                    "from prose, not keyword spotting.",
    "research_inspiration": ["https://www.cisa.gov/resources-tools/resources/enhanced-visibility-and-hardening-guidance-communications-infrastructure"],
},
{
    "id": "oc-010",
    "language": "roman-urdu", "difficulty": "L4", "category": "firewall-security",
    "scenario": "Office behind ASA, staff private, ek public server.",
    "coverage_cell": "small-s1-a1-single-dhcp-wan-asa",
    "length_category": "long",
    "prompt": "Hamara office hai, taqreeban 15 computers hain. ISP se direct line aati "
              "hai. Mujhe beech mein firewall (ASA) chahiye taake andar ke computers "
              "hamesha safe rahen. Ek public server bhi lagana hai jise bahar se access "
              "karna ho, lekin woh server andar ke private computers ka data na dekhe "
              "sake. Andar ke sab computers automatically IP lein.",
    "expected_intent": {
        "intent_version": "1", "name": "office-asa",
        "sites": [_site("office", [_area("floor", [_pc("staffpcs", "floor", 15)])])],
        "vlans": [_vlan("inside", "Inside", site="office", required=20, dhcp=True)],
        "routing": {"mode": "static"},
        "internet": {"access": "wan", "nat": True, "public_server": True},
        "firewall": {"enabled": True, "model": "asa5506"},
        "requirements": [
            _req("REQ-001", "Firewall protects the inside network", "security", "critical"),
            _req("REQ-002", "A public server is reachable from outside", "connectivity", "high"),
            _req("REQ-003", "The public server cannot read private inside computers", "isolation", "high"),
            _req("REQ-004", "Inside hosts use DHCP", "addressing"),
        ],
    },
    "expect_refusal": False,
    "expected_behaviour": "ASA5506 perimeter, WAN internet + NAT, one inside VLAN. The public server is engine-created as "
                          "srv-int on the 198.51.100.0/24 public segment (public_server=true), outside the inside subnet, "
                          "so the server's separation from the inside is structural. No DMZ requested.",
    "why_valuable": "Roman-Urdu security request that still names the real device (ASA) the way a real admin would.",
    "research_inspiration": [],
},

# ---------------------------------------------------------------------------
# L3/L4 — IoT
# ---------------------------------------------------------------------------
{
    "id": "oc-011",
    "language": "en", "difficulty": "L3", "category": "iot",
    "scenario": "Smart-office lighting: IoT LEDs cabled in alongside normal PCs.",
    "coverage_cell": "small-s1-a2-multi-dhcp-cable-none-iot",
    "length_category": "medium",
    "prompt": "We're fitting out a new office with smart lighting — a handful of "
              "network-controlled LED units. Put the normal office PCs on their own "
              "network and wire in the smart lights as IoT devices, keeping the lights "
              "off the staff network. Everything should get its IP automatically. Cable "
              "internet is fine.",
    "expected_intent": {
        "intent_version": "1", "name": "smart-office",
        "sites": [_site("office", [
            _area("workspace", [_pc("staffpcs", "workspace", 8, vlan="staffnet")]),
            _area("ceilings", []),
        ])],
        "vlans": [
            _vlan("staffnet", "Staff", site="office", required=16, dhcp=True),
            _vlan("iotnet", "IoT", site="office", required=8, dhcp=True),
        ],
        "routing": {"mode": "connected"},
        "internet": {"access": "cable", "nat": True, "public_server": False},
        "iot": [{"id": "lights", "site": "office", "area": "ceilings", "kind": "smart_led", "count": 4}],
        "requirements": [
            _req("REQ-001", "Office PCs are on their own network"),
            _req("REQ-002", "Smart LED lighting is wired in as IoT devices", "operational"),
            _req("REQ-003", "IoT lights are not mixed with staff PCs", "isolation"),
        ],
    },
    "expect_refusal": False,
    "expected_behaviour": "Staff VLAN + IoT VLAN declaration, 4 cabled smart_led things, cable internet. The 'keeping the "
                          "lights off the staff network' clause verifies UNKNOWN — IoT things are created and cabled to "
                          "an anchor with no IP/VLAN placement (expander _place_iot, CG-2), so they do not land on a "
                          "separately verified segment.",
    "why_valuable": "IoT as a real building fit-out, not 'add 5 lights'. Separation phrased in plain language.",
    "research_inspiration": ["https://blogs.cisco.com/industrial-iot/unpacking-iot-a-series-the-security-challenge-and-what-you-can-do-about-it"],
},

# ---------------------------------------------------------------------------
# L4/L5 — long-document / enterprise
# ---------------------------------------------------------------------------
{
    "id": "oc-012",
    "language": "en", "difficulty": "L5", "category": "long-document",
    "scenario": "Multi-area enterprise office: several VLANs, servers, internet, isolation.",
    "coverage_cell": "medium-s1-a3-multi-static-wan-none",
    "length_category": "document",
    "prompt": "Background: Northwind Trading is a 45-person wholesale company moving "
              "into a single-floor unit. We have four functional groups: sales (18 "
              "desktop PCs plus 4 docked laptops), warehouse (about 12 "
              "people who use wired terminals), finance (about 8 people, wired PCs that "
              "hold our accounting system), and a reception/meeting area (about 7 "
              "shared PCs and guest-facing kiosks).\n\n"
              "Current environment: nothing — this is a green-field unit, we are "
              "pulling one fibre handoff from the ISP.\n\n"
              "Target environment:\n"
              "- Sales, warehouse and finance must each be on their own network. Finance "
              "especially must not be reachable by anyone outside finance.\n"
              "- Reception/meeting PCs and the kiosks are shared and untrusted, so keep "
              "them apart from the working groups.\n"
              "- We need one internal server hosting our intranet (HTTP) that sales and "
              "warehouse can open, and a company DNS server.\n"
              "- Everyone gets addresses automatically, except finance — the finance "
              "machines use static addresses.\n\n"
              "Constraints / exclusions:\n"
              "- No wireless for now; wired only.\n"
              "- No guest internet kiosk isolation magic — just keep them on their own "
              "segment.\n"
              "- Don't add a firewall appliance; our ISP handles the edge.\n"
              "Priority: get the four segments right first; internet second (we'll add "
              "it after the segments are signed off).",
    "expected_intent": {
        "intent_version": "1", "name": "northwind",
        "sites": [_site("unit", [
            _area("salesarea", [
                _pc("salespcs", "salesarea", 18, vlan="salesnet"),
                # Prompt says "wired only" — the sales laptops are docked/wired,
                # so they are ordinary PCs here (no wireless radio implied).
                _pc("saleslaptops", "salesarea", 4, vlan="salesnet"),
            ]),
            _area("warehousearea", [_pc("whsterminals", "warehousearea", 12, vlan="whsnet")]),
            _area("financearea", [
                _pc("financepcs", "financearea", 8, addressing="static", vlan="financenet"),
            ]),
            _area("receptionarea", [_pc("sharedpcs", "receptionarea", 7, vlan="receptionnet")]),
        ])],
        "vlans": [
            _vlan("salesnet", "Sales", site="unit", required=26, dhcp=True),
            _vlan("whsnet", "Warehouse", site="unit", required=20, dhcp=True),
            _vlan("financenet", "Finance", site="unit", required=14, dhcp=False),
            _vlan("receptionnet", "Reception", site="unit", required=14, dhcp=True),
        ],
        "routing": {"mode": "connected"},
        "servers": [
            {"id": "intranet", "site": "unit", "vlan": "salesnet", "services": ["http"]},
            {"id": "dnssrv", "site": "unit", "vlan": "salesnet", "services": ["dns"]},
        ],
        "requirements": [
            _req("REQ-001", "Sales, warehouse, finance and reception are four separate networks", "isolation", "critical"),
            _req("REQ-002", "Finance is unreachable from any other group", "security", "critical"),
            _req("REQ-003", "An internal HTTP intranet server is reachable by sales and warehouse", "service", "high"),
            _req("REQ-004", "A company DNS server exists", "service", "normal"),
            _req("REQ-005", "Finance hosts are statically addressed; everyone else uses DHCP", "addressing", "high"),
            _req("REQ-006", "No wireless and no firewall appliance", "constraint"),
        ],
    },
    "expect_refusal": False,
    "expected_behaviour": "Four VLANs (sales/warehouse/finance/reception), servers on the sales VLAN, finance machines "
                          "static while everyone else is DHCP, connected routing, no internet and no firewall (both "
                          "explicitly excluded).",
    "why_valuable": "True L5 long-document: multi-paragraph, four groups, mixed addressing, explicit exclusions, and a "
                    "priority ordering. Forces full-document extraction; the 'no internet / no firewall' exclusions trap "
                    "a model that only reads the first line.",
    "research_inspiration": ["https://www.purple.ai/en-us/guides/wifi-network-segmentation-vlans-ssids-and-guest-traffic"],
},
{
    "id": "oc-013",
    "language": "mixed", "difficulty": "L5", "category": "enterprise",
    "scenario": "Two-site company, multiple VLANs, servers, OSPF, internet at HQ.",
    "coverage_cell": "small-s2-a2-multi-static-ospf-wan-none",
    "length_category": "document",
    "prompt": "Rehman Textiles ke do sites hain — Karachi head office aur Faisalabad "
              "factory. Karachi mein sales aur accounts ka staff hai (taqreeban 12 PCs), "
              "and Faisalabad mein production floor ke terminals (taqreeban 10). Mujhe "
              "Karachi aur Faisalabad ko aapas mein connect karna hai with OSPF, kyunki "
              "sites barh rahi hain, so manual routes maintain na karni parein. Accounts "
              "ka data sensitive hai, so uska alag network hona chahiye aur sales usay na "
              "dekh sake. Faisalabad ka production network bhi alag rahe. Internet head "
              "office par hai aur factory ko wohi se mile. Accounts aur sales dono DHCP "
              "use karein, production ke terminals static. Karachi mein ek DNS server ho "
              "jis se dono sites names resolve karein. Wireless ki zaroorat nahi, sab "
              "wired, aur firewall mat lagana — hum ISP ke edge par trust karte hain.",
    "expected_intent": {
        "intent_version": "1", "name": "rehman-textiles",
        "sites": [
            _site("karachi", [
                _area("salesarea", [_pc("salespcs", "salesarea", 6, vlan="salesnet")]),
                _area("accountsarea", [_pc("accountspcs", "accountsarea", 6, vlan="accountsnet")]),
            ]),
            _site("faisalabad", [
                _area("production", [_pc("terminals", "production", 10, addressing="static", vlan="productionnet")]),
            ]),
        ],
        "vlans": [
            _vlan("salesnet", "Sales", site="karachi", required=12, dhcp=True),
            _vlan("accountsnet", "Accounts", site="karachi", required=12, dhcp=True),
            _vlan("productionnet", "Production", site="faisalabad", required=16, dhcp=False),
        ],
        "routing": {"mode": "ospf", "ospf_area": "0"},
        "internet": {"access": "wan", "nat": True, "public_server": False},
        "servers": [{"id": "dnssrv", "site": "karachi", "vlan": "salesnet", "services": ["dns"]}],
        "requirements": [
            _req("REQ-001", "Karachi and Faisalabad are connected with OSPF", "operational", "high"),
            _req("REQ-002", "Accounts network is separate and not reachable by sales", "isolation", "critical"),
            _req("REQ-003", "Factory production network is separate", "isolation", "high"),
            _req("REQ-004", "Factory gets internet through the head office"),
            _req("REQ-005", "Accounts and sales use DHCP; production is static", "addressing"),
            _req("REQ-006", "A DNS server at head office serves both sites", "service"),
            _req("REQ-007", "No wireless and no firewall appliance", "constraint"),
        ],
    },
    "expect_refusal": False,
    "expected_behaviour": "Two sites, OSPF, three VLANs, static production, WAN internet at Karachi, one DNS server. "
                          "Exclusions (no wireless/firewall) must be honoured.",
    "why_valuable": "Highest-value mixed-language L5: real Pakistani manufacturing company, OSPF, mixed addressing, "
                    "cross-site internet, explicit exclusions. Genuine multi-constraint difficulty, not padding.",
    "research_inspiration": ["https://www.cisco.com/c/en/us/td/docs/voice_ip_comm/cvd/G2basicsmbrnt/G2basicsmbrnt_book/FeatServ.html"],
},

# ---------------------------------------------------------------------------
# AMBIGUOUS  (expect_refusal: ask a clarifying question)
# ---------------------------------------------------------------------------
{
    "id": "oc-014",
    "language": "en", "difficulty": "L4", "category": "ambiguous",
    "scenario": "Branches + routing requested, but site count and internet are unstated.",
    "coverage_cell": "ambiguous-missing-sites",
    "length_category": "short",
    "prompt": "We need to connect our branches and set up routing between them. Make it "
              "work.",
    "expected_intent": None,
    "expect_refusal": True,
    "expected_behaviour": "ASK: how many branches and where is the main site? Also unclear whether each branch needs "
                          "its own internet or internet only at the main site. The model must NOT invent a site count. "
                          "Correct behaviour is a targeted clarification, not a guess and not a full refusal.",
    "why_valuable": "Deliberately missing the site count — the number one thing the expander cannot invent. Tests "
                    "clarify-don't-invent.",
    "research_inspiration": [],
},
{
    "id": "oc-015",
    "language": "roman-urdu", "difficulty": "L3", "category": "ambiguous",
    "scenario": "Office network, but host counts and DHCP-vs-static unclear.",
    "coverage_cell": "ambiguous-addressing",
    "length_category": "short",
    "prompt": "Mujhe office ka network banana hai, do alag departments hain. Sahi "
              "numbers aur addressing abhi nahi bata sakta.",
    "expected_intent": None,
    "expect_refusal": True,
    "expected_behaviour": "ASK: approximately how many devices per department, and should they get IPs automatically "
                          "(DHCP) or fixed (static)? Do not pick a host count or an addressing mode on the user's behalf.",
    "why_valuable": "Two missing facts at once (scale + addressing). Real novice phrasing; tests whether the model asks "
                    "for both rather than assuming DHCP.",
    "research_inspiration": [],
},
{
    "id": "oc-016",
    "language": "mixed", "difficulty": "L4", "category": "ambiguous",
    "scenario": "Internet requested but access type (WAN/DSL/cable) unspecified.",
    "coverage_cell": "ambiguous-internet-type",
    "length_category": "medium",
    "prompt": "Humara naya office setup hai, ek server bhi rakhna hai and internet "
              "chahiye. Lekin ISP ki line abhi lag hi rahi hai, so humein abhi confirm "
              "nahi ke connection kaise aayega.",
    "expected_intent": None,
    "expect_refusal": True,
    "expected_behaviour": "ASK what the ISP handoff will be (WAN handoff, DSL, or cable), because InternetIntent.access "
                          "only supports those three verified shapes and the model must not guess which. Also reasonable "
                          "to confirm the server's role.",
    "why_valuable": "Tests that the model knows internet access has three concrete verified shapes and asks which, instead "
                    "of silently picking one.",
    "research_inspiration": [],
},

# ---------------------------------------------------------------------------
# CONTRADICTORY  (expect_refusal: surface the conflict)
# ---------------------------------------------------------------------------
{
    "id": "oc-017",
    "language": "en", "difficulty": "L5", "category": "contradictory",
    "scenario": "Guest must be fully isolated yet also reach an internal finance server.",
    "coverage_cell": "contradiction-guest-isolation-vs-reach",
    "length_category": "medium",
    "prompt": "Set up a guest WiFi that is completely isolated — visitors must never "
              "touch anything internal. Oh, and they also need to open our booking "
              "page, which lives on a server inside the office network.",
    "expected_intent": None,
    "expect_refusal": True,
    "expected_behaviour": "SURFACE CONTRADICTION: 'completely isolated, never touch anything internal' conflicts with "
                          "'guest reaches an internal booking server'. The model must flag the conflict and ask which "
                          "wins (or propose a narrow exception), not silently build one and drop the other. Note this "
                          "also overlaps the real guest-isolation capability gap.",
    "why_valuable": "Textbook real-world contradiction (isolation vs an exception), stated as if both are firm. Tests "
                    "conflict detection rather than happy-path generation.",
    "research_inspiration": ["https://www.purple.ai/en-gb/blogs/how-to-set-up-guest-wifi"],
},
{
    "id": "oc-018",
    "language": "roman-urdu", "difficulty": "L5", "category": "contradictory",
    "scenario": "Finance must be unreachable, yet sales must print on finance's printer.",
    "coverage_cell": "contradiction-isolation-vs-service",
    "length_category": "medium",
    "prompt": "Finance ka network bilkul isolated hona chahiye, koi bahar se access na "
              "kare. Aur haan, sales wale ko finance ki printer use karni hai, woh bhi "
              " zaroori requirement hai.",
    "expected_intent": None,
    "expect_refusal": True,
    "expected_behaviour": "SURFACE CONTRADICTION: 'finance completely isolated, no outside access' vs 'sales must use "
                          "the finance printer'. Ask which requirement wins or whether a single narrow printer exception "
                          "is intended. Do not build a flat network that satisfies both by deleting the isolation.",
    "why_valuable": "Roman-Urdu contradiction that mirrors a very common real request (segmentation vs a shared printer).",
    "research_inspiration": [],
},

# ---------------------------------------------------------------------------
# IMPOSSIBLE / REFUSAL  (real Havyn limits only)
# ---------------------------------------------------------------------------
{
    "id": "oc-019",
    "language": "en", "difficulty": "L4", "category": "impossible-refusal",
    "scenario": "Multi-site ASA 5505 with DMZ — a genuinely unsupported shape.",
    "coverage_cell": "refusal-asa5505-multisite-dmz",
    "length_category": "medium",
    "prompt": "We have offices in two cities and want an ASA 5505 at each, both "
              "connected, and each with a DMZ hosting a public server. Link the two "
              "offices so they can share the DMZ services.",
    "expected_intent": None,
    "expect_refusal": True,
    "expected_behaviour": "REFUSE-BY-NAME: the ASA 5505 is verified only as a single-site inside/outside perimeter "
                          "(lint 'asa5505-multi-site'), and it has no DMZ support (expander 'asa5505-no-dmz'); multi-site "
                          "DMZ is 'dmz-multi-site-unsupported'. The engine must refuse by name and explain the 5505's "
                          "limits, offering the 5506-X single-site DMZ instead.",
    "why_valuable": "Packs three REAL named refusals into one plausible request. Trains precise, honest refusal with an "
                    "alternative, not a hallucinated multi-site DMZ.",
    "research_inspiration": [],
},
{
    "id": "oc-020",
    "language": "mixed", "difficulty": "L3", "category": "impossible-refusal",
    "scenario": "Server-side DHCP requested — unsupported service path.",
    "coverage_cell": "refusal-server-dhcp",
    "length_category": "short",
    "prompt": "Ek Windows server pe DHCP service run karni hai, so that saare office "
              "computers us se IP lein. Server ko server VLAN mein rakhna hai.",
    "expected_intent": None,
    "expect_refusal": True,
    "expected_behaviour": "REFUSE-BY-NAME: server-side DHCP is 'server-dhcp-service' (unsupported). The verified path is "
                          "per-VLAN DHCP pools on the gateway, not a Server-PT DHCP service. Explain and offer the "
                          "supported alternative.",
    "why_valuable": "A very common real ask that the engine cannot express. Trains refusing by name + offering the "
                    "verified gateway-pool alternative instead of faking a server DHCP config.",
    "research_inspiration": ["https://www.cisco.com/c/en/us/support/docs/ip/dynamic-address-allocation-resolution/27470-100.html"],
},
{
    "id": "oc-021",
    "language": "en", "difficulty": "L4", "category": "impossible-refusal",
    "scenario": "Firewall perimeter on a multi-VLAN site — engine needs a single inside VLAN.",
    "coverage_cell": "refusal-firewall-multivlan",
    "length_category": "medium",
    "prompt": "Put an ASA firewall in front of our office. Behind it we'll have three "
              "VLANs — staff, guests and cameras — all inside the firewall's inside "
              "interface, with the ASA doing the routing between them and the internet.",
    "expected_intent": None,
    "expect_refusal": True,
    "expected_behaviour": "REFUSE-BY-NAME: expander 'firewall-needs-single-vlan' — the verified T07 ASA perimeter is the "
                          "inside gateway for exactly ONE inside VLAN. A multi-VLAN inside behind one ASA is not a "
                          "verified shape. Explain and offer the supported single-inside-VLAN perimeter or a "
                          "router-inter-VLAN design without the ASA perimeter claim.",
    "why_valuable": "Realistic but currently unbuildable; forces an honest refusal grounded in the actual single-VLAN "
                    "perimeter constraint.",
    "research_inspiration": [],
},

# ---------------------------------------------------------------------------
# Additional coverage: static addressing, servers, diagnosis-style framing
# ---------------------------------------------------------------------------
{
    "id": "oc-022",
    "language": "en", "difficulty": "L2", "category": "dhcp-static",
    "scenario": "Small lab where every host is statically addressed.",
    "coverage_cell": "small-s1-a1-single-static-none-none",
    "length_category": "medium",
    "prompt": "Build a small lab network where nothing uses DHCP. Every PC should have "
              "a fixed address I set, because this is a test bench and the addresses "
              "must never change. Six PCs, all on one network, all static.",
    "expected_intent": {
        "intent_version": "1", "name": "static-lab",
        "sites": [_site("lab", [_area("bench", [_pc("labpcs", "bench", 6, addressing="static")])])],
        "vlans": [_vlan("labbench", "Lab bench", site="lab", required=10, dhcp=False)],
        "routing": {"mode": "connected"},
        "requirements": [
            _req("REQ-001", "All six PCs reach each other"),
            _req("REQ-002", "Every host is statically addressed, no DHCP", "addressing", "high"),
        ],
    },
    "expect_refusal": False,
    "expected_behaviour": "Single VLAN, dhcp=False, all groups addressing='static'. No internet.",
    "why_valuable": "Clean static-addressing case; tests dhcp=False + per-group static, the opposite of the DHCP-heavy norm.",
    "research_inspiration": [],
},
{
    "id": "oc-023",
    "language": "roman-urdu", "difficulty": "L3", "category": "servers",
    "scenario": "Small office with an internal DNS + HTTP server.",
    "coverage_cell": "small-s1-a2-multi-dhcp-none-none",
    "length_category": "medium",
    "prompt": "Hamare chhote office mein ek internal server chahiye jis par company ka "
              "intranet (website) ho aur DNS bhi chal raha ho. Office ke aath computers "
              "se website open ho aur names resolve hon. Sab computers DHCP se IP lein.",
    "expected_intent": {
        "intent_version": "1", "name": "office-server",
        "sites": [_site("office", [
            _area("workspace", [_pc("officepcs", "workspace", 8, vlan="officenet")]),
            _area("serverroom", []),
        ])],
        "vlans": [
            _vlan("officenet", "Office", site="office", required=16, dhcp=True),
            _vlan("servernet", "Servers", site="office", required=6, dhcp=True),
        ],
        "routing": {"mode": "connected"},
        "servers": [{"id": "intranet", "site": "office", "vlan": "servernet",
                     "services": ["http", "dns"]}],
        "requirements": [
            _req("REQ-001", "An internal HTTP intranet server exists", "service"),
            _req("REQ-002", "A DNS server resolves internal names", "service"),
            _req("REQ-003", "Office computers use DHCP", "addressing"),
        ],
    },
    "expect_refusal": False,
    "expected_behaviour": "Two VLANs (office + server), one server with http+dns on the server VLAN, DHCP for PCs. No DHCP "
                          "service on the server (that would be the refused path).",
    "why_valuable": "Server requirement expressed in business terms ('intranet', 'names resolve'); tests http+dns services "
                    "without tripping the server-DHCP refusal.",
    "research_inspiration": [],
},
{
    "id": "oc-024",
    "language": "en", "difficulty": "L4", "category": "diagnosis",
    "scenario": "Enhancement-style: describes a broken inter-VLAN setup to be rebuilt correctly.",
    "coverage_cell": "small-s1-a2-multi-dhcp-dsl-none",
    "length_category": "long",
    "prompt": "Our existing office network is a mess: reception and finance were put on "
              "the same network last year and finance keeps complaining that everyone "
              "can see their machines. Reception has about six PCs and finance five, and "
              "half of them end up with a self-assigned address that goes nowhere.\n\n"
              "Please design it properly this time: reception and finance on separate "
              "networks, finance private, and every PC getting a real automatic address "
              "so the self-assigned problem goes away. We use a DSL line for internet.",
    "expected_intent": {
        "intent_version": "1", "name": "office-rebuild",
        "sites": [_site("office", [
            _area("reception", [_pc("receptionpcs", "reception", 6, vlan="receptionnet")]),
            _area("finance", [_pc("financepcs", "finance", 5, vlan="financenet")]),
        ])],
        "vlans": [
            _vlan("receptionnet", "Reception", site="office", required=12, dhcp=True),
            _vlan("financenet", "Finance", site="office", required=12, dhcp=True),
        ],
        "routing": {"mode": "connected"},
        "internet": {"access": "dsl", "nat": True, "public_server": False},
        "requirements": [
            _req("REQ-001", "Reception and finance are on separate networks", "isolation", "high"),
            _req("REQ-002", "Finance machines are not visible to reception", "security", "high"),
            _req("REQ-003", "Every PC gets a proper automatic address (DHCP)", "addressing", "high"),
            _req("REQ-004", "DSL internet with NAT"),
        ],
    },
    "expect_refusal": False,
    "expected_behaviour": "Two DHCP VLANs, finance separate, DSL internet + NAT. The 'diagnosis' framing is narrative only; "
                          "the build target is fully specified.",
    "why_valuable": "Diagnosis/enhancement flavour: the prompt describes symptoms and asks for a correct redesign. Adds a "
                    "realistic 'fix my mess' register without requiring the live diagnosis engine.",
    "research_inspiration": ["https://www.engineerdirect.co.uk/guides/library/d/network-infrastructure/managed-switch-vlan-misconfiguration"],
},

# ---------------------------------------------------------------------------
# Healthcare / campus / retail scenario flavour (research-informed, original)
# ---------------------------------------------------------------------------
{
    "id": "oc-025",
    "language": "en", "difficulty": "L4", "category": "enterprise",
    "scenario": "Small clinic: reception, consulting rooms, patient guest network.",
    "coverage_cell": "small-s1-a3-multi-dhcp-wan-none",
    "length_category": "long",
    "prompt": "We're a four-doctor clinic opening a new premises. I need the network "
              "designed with clear separation, because patient information must not be "
              "reachable from waiting areas.\n\n"
              "- Reception and admin PCs (about 6) handle bookings and records.\n"
              "- Consulting rooms have a wired PC each (4 rooms).\n"
              "- The waiting area has a shared kiosk PC (1) that patients can use.\n"
              "- Patient/consulting traffic must be separate from the waiting area.\n"
              "- The records server hosts the clinic's HTTP page and DNS, so it gets a "
              "fixed address; everything else is automatic addressing.\n"
              "\n"
              "We have a WAN handoff from the ISP for internet. We're not doing patient "
              "WiFi yet, so the kiosk stays wired for now.",
    "expected_intent": {
        "intent_version": "1", "name": "clinic",
        "sites": [_site("clinic", [
            _area("reception", [_pc("receptionpcs", "reception", 6, vlan="adminnet")]),
            _area("consulting", [_pc("roompcs", "consulting", 4, vlan="clinicalnet")]),
            _area("waiting", [_pc("kioskpc", "waiting", 1, vlan="waitingnet")]),
        ])],
        "vlans": [
            _vlan("adminnet", "Admin", site="clinic", required=12, dhcp=True),
            _vlan("clinicalnet", "Clinical", site="clinic", required=10, dhcp=True),
            _vlan("waitingnet", "Waiting", site="clinic", required=8, dhcp=True),
        ],
        "routing": {"mode": "connected"},
        "internet": {"access": "wan", "nat": True, "public_server": False},
        "servers": [{"id": "recordssrv", "site": "clinic", "vlan": "clinicalnet",
                     "services": ["http", "dns"]}],
        "requirements": [
            _req("REQ-001", "Admin, clinical and waiting/guest are separate networks", "isolation", "critical"),
            _req("REQ-002", "Patient/consulting traffic is not reachable from the waiting area", "security", "critical"),
            _req("REQ-003", "The records server is statically addressed", "addressing", "high"),
            _req("REQ-004", "A records server with HTTP and DNS exists", "service"),
            _req("REQ-005", "WAN internet with NAT", "connectivity"),
        ],
    },
    "expect_refusal": False,
    "expected_behaviour": "Three VLANs, static records server (http+dns) on the clinical VLAN, WAN internet. The "
                          "patient-facing access is the wired waiting kiosk (no wireless isolation claim), so this builds.",
    "why_valuable": "Healthcare realism (patient-data separation) mapped into a shape the engine can actually build; "
                    "demonstrates research -> original scenario -> validation.",
    "research_inspiration": ["https://www.cisco.com/c/dam/global/en_uk/solutions/industries/pdfs/healthcare-secure-infrastructure.pdf"],
},
{
    "id": "oc-026",
    "language": "mixed", "difficulty": "L4", "category": "enterprise",
    "scenario": "Campus-style school: lab, classrooms, staff, guest.",
    "coverage_cell": "medium-s1-a3-multi-dhcp-cable-none",
    "length_category": "long",
    "prompt": "School hai — computer lab, classrooms aur staff room. Students ke liye "
              "alag network chahiye, aur staff ke liye bhi alag, while a guest network "
              "for visitors jo sirf internet tak mehdood ho.\n\n"
              "- Computer lab: 20 student PCs.\n"
              "- Classrooms: 12 wired PCs.\n"
              "- Staff room: 6 staff PCs aur 4 laptops (wireless).\n"
              "- Guest network sirf internet, so koi internal access nahi.\n"
              "Cable internet available hai, and students aur staff ke beech koi sharing "
              "nahi honi chahiye.",
    "expected_intent": {
        "intent_version": "1", "name": "school",
        "sites": [_site("school", [
            _area("lab", [_pc("labpcs", "lab", 20)]),
            _area("classrooms", [_pc("classpcs", "classrooms", 12)]),
            _area("staffroom", [
                _pc("staffpcs", "staffroom", 6),
                _wifi("stafflaptops", "staffroom", 4),
            ]),
        ])],
        "vlans": [
            _vlan("studentnet", "Students", site="school", required=34, dhcp=True),
            _vlan("staffnet", "Staff", site="school", required=16, dhcp=True),
            _vlan("guestnet", "Guest", site="school", required=10, dhcp=True),
        ],
        "routing": {"mode": "connected"},
        "internet": {"access": "cable", "nat": True, "public_server": False},
        "wireless": [{
            "id": "staffwifi", "site": "school", "area": "staffroom",
            "ssid": "SchoolStaff", "security": "wpa2", "passphrase": "staffroom2026",
            "client_group_ids": ["stafflaptops"],
        }],
        "requirements": [
            _req("REQ-001", "Students, staff and guest are separate networks", "isolation", "critical"),
            _req("REQ-002", "Guest reaches only the internet", "security", "high"),
            _req("REQ-003", "No sharing between students and staff", "isolation", "high"),
            _req("REQ-004", "Staff laptops connect over WiFi", "connectivity"),
        ],
    },
    "expect_refusal": True,
    "expected_intent": None,
    "expected_behaviour": "REFUSE-BY-NAME: the guest clause ('sirf internet, koi internal access nahi') is a "
                          "guest-isolation guarantee, and expander v1 refuses it by name ('guest-isolation-unsupported') — "
                          "no live-verified shape puts a guest VLAN behind a verified ACL enforcement point. The correct "
                          "output is the named refusal plus an offer to build the student/staff segmentation with the guest "
                          "network as a plain separate subnet, making no 'cannot reach internal' promise.",
    "why_valuable": "Campus realism + a second encounter with the guest-isolation capability gap in a different domain and "
                    "language. Reinforces honest refusal over fabrication.",
    "research_inspiration": ["https://www.cisco.com/c/dam/en_us/solutions/industries/docs/education/cisco_wlan_design_guide.pdf"],
},
{
    "id": "oc-027",
    "language": "en", "difficulty": "L3", "category": "enterprise",
    "scenario": "Retail store: POS, back office, guest WiFi, cameras.",
    "coverage_cell": "small-s1-a3-multi-dhcp-cable-none-iot",
    "length_category": "medium",
    "prompt": "Design the network for a new retail branch. We have two POS terminals "
              "that must be isolated for compliance, a back-office PC, IP security "
              "cameras, and free guest WiFi for customers. POS and back office stay "
              "wired; guests are wireless. Cable internet on site.",
    "expected_intent": {
        "intent_version": "1", "name": "retail-branch",
        "sites": [_site("store", [
            _area("salesfloor", [
                _pc("pos1", "salesfloor", 1),
                _pc("pos2", "salesfloor", 1),
            ]),
            _area("backoffice", [_pc("officepc", "backoffice", 1)]),
            _area("ceiling", [_pc("camerapanel", "ceiling", 1)]),
        ])],
        "vlans": [
            _vlan("posnet", "POS", site="store", required=6, dhcp=True),
            _vlan("officenet", "Back office", site="store", required=6, dhcp=True),
            _vlan("guestnet", "Guest", site="store", required=10, dhcp=True),
        ],
        "routing": {"mode": "connected"},
        "internet": {"access": "cable", "nat": True, "public_server": False},
        "iot": [{"id": "cameras", "site": "store", "area": "ceiling", "kind": "smart_led", "count": 3}],
        "requirements": [
            _req("REQ-001", "POS terminals are isolated for compliance", "security", "critical"),
            _req("REQ-002", "Back office is separate from POS", "isolation", "high"),
            _req("REQ-003", "Security cameras are wired in as IoT devices", "operational"),
            _req("REQ-004", "Guest WiFi reaches only the internet"),
        ],
    },
    "expect_refusal": True,
    "expected_intent": None,
    "expected_behaviour": "REFUSE-BY-NAME: real IP security cameras cannot be expressed — the schema's only IoT kind "
                          "is smart_led ('kind: Literal[\"smart_led\"]'), and relabelling cameras as smart LEDs is exactly "
                          "the mislabel the engine must not do. The correct output names the camera limitation and offers "
                          "either the expressible smart-LED IoT device or an explicit statement that IP cameras are out of "
                          "model scope. The POS/back-office/guest segmentation itself is buildable.",
    "why_valuable": "Retail/POS realism (PCI-style isolation) plus an honest capability-gap probe: cameras aren't a "
                    "verified IoT kind. Prevents the model from pretending it can place real IP cameras.",
    "research_inspiration": ["https://security.pditechnologies.com/blog/retail-network-segmentation-guide-secure-every-store-simplify-compliance"],
},

# ---------------------------------------------------------------------------
# Warehouse / logistics flavour
# ---------------------------------------------------------------------------
{
    "id": "oc-028",
    "language": "en", "difficulty": "L4", "category": "enterprise",
    "scenario": "Warehouse: scanner/ops wireless, staff, contractor segments.",
    "coverage_cell": "medium-s1-a2-multi-dhcp-cable-none",
    "length_category": "long",
    "prompt": "We run a distribution warehouse. I need the network split by device "
              "role, because our wireless barcode scanners and forklift terminals must "
              "stay on their own segment away from office traffic.\n\n"
              "- Operations: wireless handhelds and terminals (about 6 devices) that talk "
              "to the warehouse system.\n"
              "- Office: 8 wired PCs plus 3 laptops for supervisors (wireless).\n"
              "- Contractors: third-party drivers occasionally need internet only.\n"
              "Everything gets automatic addresses. Cable internet is available. Keep the "
              "operations wireless separate from the office and the contractors.",
    "expected_intent": {
        "intent_version": "1", "name": "warehouse",
        "sites": [_site("warehouse", [
            _area("floor", [
                _wifi("opsdevices", "floor", 6, device="tablet", vlan="opsnet"),
            ]),
            _area("office", [
                _pc("officepcs", "office", 8, vlan="officenet"),
                _wifi("supervisorlaptops", "office", 3, vlan="officenet"),
            ]),
        ])],
        "vlans": [
            _vlan("opsnet", "Operations", site="warehouse", required=12, dhcp=True),
            _vlan("officenet", "Office", site="warehouse", required=16, dhcp=True),
            _vlan("contractornet", "Contractor", site="warehouse", required=8, dhcp=True),
        ],
        "routing": {"mode": "connected"},
        "internet": {"access": "cable", "nat": True, "public_server": False},
        "wireless": [
            {"id": "opswifi", "site": "warehouse", "area": "floor",
             "ssid": "WMS-OPS", "security": "wpa2", "passphrase": "opsfloor2026",
             "client_group_ids": ["opsdevices"]},
            {"id": "officewifi", "site": "warehouse", "area": "office",
             "ssid": "WarehouseOffice", "security": "wpa2", "passphrase": "office2026",
             "client_group_ids": ["supervisorlaptops"]},
        ],
        "requirements": [
            _req("REQ-001", "Operations wireless devices are on their own segment", "isolation", "high"),
            _req("REQ-002", "Office is separate from operations", "isolation", "high"),
            _req("REQ-003", "Contractors get internet only", "security", "normal"),
            _req("REQ-004", "All hosts use DHCP", "addressing"),
        ],
    },
    "expect_refusal": False,
    "expected_behaviour": "Three VLANs (operations/office/contractor) and two wireless cells. The operations and office "
                          "client groups pin different VLANs, so they are delivered as separate subnets. The contractor "
                          "'internet only' clause is read as scope (what they need), not as an enforced block, so it does "
                          "not trip the guest-isolation refusal. Two clauses verify UNKNOWN: inter-VLAN blocking between "
                          "operations/office/contractors (CG-1 — the expander emits no inter-VLAN ACL), and the 'internet "
                          "only' guarantee itself. The contractor VLAN is a planned segment with no hosts in this build; "
                          "the internet is one line at the first site (CG-3).",
    "why_valuable": "Warehouse/logistics realism with TWO wireless cells (a rarer, harder shape) and a third encounter with "
                    "the isolation boundary in yet another register.",
    "research_inspiration": ["https://www.2mtechnology.net/unifi-warehouse-wifi-design"],
},

# ---------------------------------------------------------------------------
# More Roman-Urdu / mixed to hit language targets
# ---------------------------------------------------------------------------
{
    "id": "oc-029",
    "language": "roman-urdu", "difficulty": "L2", "category": "vlan-dhcp",
    "scenario": "Two-floor office, har floor ka alag network.",
    "coverage_cell": "small-s1-a2-multi-dhcp-dsl-none",
    "length_category": "medium",
    "prompt": "Mere office ke do floors hain. Neeche reception aur upar accounts ka "
              "office hai. Dono floors ke computers alag network par hon aur sab ko IP "
              "automatically mile. Internet DSL line se chahiye.",
    "expected_intent": {
        "intent_version": "1", "name": "do-floor",
        "sites": [_site("building", [
            _area("ground", [_pc("receptionpcs", "ground", 5, vlan="receptionnet")]),
            _area("upper", [_pc("accountspcs", "upper", 6, vlan="accountsnet")]),
        ])],
        "vlans": [
            _vlan("receptionnet", "Reception", site="building", required=12, dhcp=True),
            _vlan("accountsnet", "Accounts", site="building", required=12, dhcp=True),
        ],
        "routing": {"mode": "connected"},
        "internet": {"access": "dsl", "nat": True, "public_server": False},
        "requirements": [
            _req("REQ-001", "Har floor ka network alag ho"),
            _req("REQ-002", "Sab computers DHCP se IP lein", "addressing"),
            _req("REQ-003", "DSL internet available ho"),
        ],
    },
    "expect_refusal": False,
    "expected_behaviour": "Two area/VLANs, both DHCP, DSL internet + NAT.",
    "why_valuable": "Straight Roman-Urdu L2 that still names floors and DSL; keeps the language distribution honest.",
    "research_inspiration": [],
},
{
    "id": "oc-030",
    "language": "mixed", "difficulty": "L3", "category": "routing",
    "scenario": "Three sites, static routing, internet sirf head office.",
    "coverage_cell": "small-s3-a1-multi-static-wan-none",
    "length_category": "medium",
    "prompt": "Teen sites hain: head office, aur do branches. Head office par internet "
              "line hai, and dono branches ko head office ke through routing chahiye — "
              "internet bhi wahi se mile. Static routing use karein, so OSPF ki abhi "
              "zaroorat nahi.",
    "expected_intent": {
        "intent_version": "1", "name": "three-sites",
        "sites": [
            _site("headoffice", [_area("hq", [_pc("hopcs", "hq", 8)])]),
            _site("branch1", [_area("b1", [_pc("b1pcs", "b1", 4)])]),
            _site("branch2", [_area("b2", [_pc("b2pcs", "b2", 4)])]),
        ],
        "vlans": [
            _vlan("honet", "Head office", site="headoffice", required=16, dhcp=True),
            _vlan("b1net", "Branch 1", site="branch1", required=10, dhcp=True),
            _vlan("b2net", "Branch 2", site="branch2", required=10, dhcp=True),
        ],
        "routing": {"mode": "static"},
        "internet": {"access": "wan", "nat": True, "public_server": False},
        "requirements": [
            _req("REQ-001", "Both branches reach the head office"),
            _req("REQ-002", "Both branches get internet through the head office"),
            _req("REQ-003", "Use static routing, not OSPF", "constraint"),
        ],
    },
    "expect_refusal": False,
    "expected_behaviour": "Three sites, static routes branches<->HO, WAN internet + NAT at HO, DHCP each.",
    "why_valuable": "Three-site static routing (a topology the coverage matrix exercises) in natural mixed language.",
    "research_inspiration": ["https://www.cisco.com/c/en/us/td/docs/voice_ip_comm/cvd/G2basicsmbrnt/G2basicsmbrnt_book/FeatServ.html"],
},

# ===========================================================================
# BATCH 2 — strengthens L1/L2 mass, language balance, refusal/ambiguity
# breadth, and adds campus/warehouse/clinic variants that reuse verified
# engine shapes. Every expected_intent below passes the same chain.
# ===========================================================================

# --- extra L1 / L2 basics ----------------------------------------------------
{
    "id": "oc-031",
    "language": "en", "difficulty": "L1", "category": "basic",
    "scenario": "Three PCs, one network, nothing else — a bare minimum.",
    "coverage_cell": "small-s1-a1-single-static-none-none",
    "length_category": "short",
    "prompt": "Just three PCs wired together so they can share files. Nothing fancy, "
              "no internet, no VLANs — keep it minimal.",
    "expected_intent": {
        "intent_version": "1", "name": "three-pc",
        "sites": [_site("room", [_area("bench", [_pc("pcs", "bench", 3)])])],
        "vlans": [_vlan("lan", "LAN", site="room", required=8)],
        "routing": {"mode": "connected"},
        "requirements": [_req("REQ-001", "The three PCs can share files with each other")],
    },
    "expect_refusal": False,
    "expected_behaviour": "Single flat LAN of 3 PCs. Deliberately tests that the model does NOT invent VLANs/internet "
                          "the user explicitly ruled out.",
    "why_valuable": "Anti-over-engineering check: a real minimal request that a benchmark-trained model often inflates.",
    "research_inspiration": [],
},
{
    "id": "oc-032",
    "language": "roman-urdu", "difficulty": "L1", "category": "basic",
    "scenario": "Two PCs + one printer on a shared network.",
    "coverage_cell": "small-s1-a1-single-static-none-none",
    "length_category": "short",
    "prompt": "Do computers aur ek printer hai, teeno ko ek network par lagana hai "
              "taake printer dono se chale. Bas.",
    "expected_intent": {
        "intent_version": "1", "name": "pc-printer",
        "sites": [_site("office", [_area("room", [
            _pc("computers", "room", 2),
            {"id": "printer1", "area": "room", "count": 1, "device": "printer"},
        ])])],
        "vlans": [_vlan("lan", "LAN", site="office", required=8)],
        "routing": {"mode": "connected"},
        "requirements": [
            _req("REQ-001", "Both computers can print to the shared printer"),
        ],
    },
    "expect_refusal": False,
    "expected_behaviour": "One LAN with 2 PCs and 1 printer. Exercises the printer device type.",
    "why_valuable": "Introduces a printer in natural mixed phrasing — a device type templates rarely lead with.",
    "research_inspiration": [],
},
{
    "id": "oc-033",
    "language": "en", "difficulty": "L2", "category": "vlan-dhcp",
    "scenario": "Three-VLAN office: staff, printers, guests (guest kept buildable).",
    "coverage_cell": "small-s1-a3-multi-dhcp-dsl-none",
    "length_category": "medium",
    "prompt": "Split our office into three networks: staff computers, the printers, and "
              "a visitor network. Staff and printers need to reach each other so people "
              "can print; visitors should be kept on their own network. DHCP for "
              "everyone. DSL internet.",
    "expected_intent": {
        "intent_version": "1", "name": "three-vlan-office",
        "sites": [_site("office", [
            _area("workspace", [
                _pc("staffpcs", "workspace", 10, vlan="staffnet"),
                {"id": "printers", "area": "workspace", "count": 2, "device": "printer",
                 "vlan": "printernet"},
            ]),
            _area("lobby", [_pc("visitorpcs", "lobby", 3, vlan="visitornet")]),
        ])],
        "vlans": [
            _vlan("staffnet", "Staff", site="office", required=20, dhcp=True),
            _vlan("printernet", "Printers", site="office", required=6, dhcp=True),
            _vlan("visitornet", "Visitors", site="office", required=8, dhcp=True),
        ],
        "routing": {"mode": "connected"},
        "internet": {"access": "dsl", "nat": True, "public_server": False},
        "requirements": [
            _req("REQ-001", "Staff and printers are on separate networks but can reach each other", "isolation", "high"),
            _req("REQ-002", "Visitors are on their own network", "isolation", "normal"),
            _req("REQ-003", "All hosts use DHCP", "addressing"),
        ],
    },
    "expect_refusal": False,
    "expected_behaviour": "Three VLANs; staff<->printer reachability is stated as reachable (not an isolation guarantee), "
                          "visitor segment is separate but WITHOUT an 'internet-only' isolation claim, so it builds.",
    "why_valuable": "Carefully worded to stay on the buildable side of the guest-isolation boundary — contrasts with the "
                    "refusal cases. Tests precision about what 'kept on their own network' does and doesn't promise.",
    "research_inspiration": ["https://netraclos.com/vlans-and-guest-wifi-for-small-business-networks"],
},
{
    "id": "oc-034",
    "language": "roman-urdu", "difficulty": "L2", "category": "dhcp-static",
    "scenario": "Mixed addressing: servers static, users DHCP.",
    "coverage_cell": "small-s1-a2-multi-dhcp-none-none",
    "length_category": "medium",
    "prompt": "Office mein users ke computers DHCP se IP lein, lekin do servers ko "
              "fixed (static) IP deni hain kyunki inke addresses logs mein jane hain. "
              "Servers ka apna network ho.",
    "expected_intent": {
        "intent_version": "1", "name": "mixed-addr",
        "sites": [_site("office", [
            _area("workspace", [_pc("userpcs", "workspace", 10, vlan="usernet")]),
            _area("serverroom", [_pc("servers", "serverroom", 2, addressing="static", vlan="servernet")]),
        ])],
        "vlans": [
            _vlan("usernet", "Users", site="office", required=20, dhcp=True),
            _vlan("servernet", "Servers", site="office", required=6, dhcp=False),
        ],
        "routing": {"mode": "connected"},
        "requirements": [
            _req("REQ-001", "User computers use DHCP", "addressing"),
            _req("REQ-002", "Servers use static addresses", "addressing", "high"),
            _req("REQ-003", "Servers are on their own network", "isolation"),
        ],
    },
    "expect_refusal": False,
    "expected_behaviour": "Users VLAN dhcp=True; server VLAN dhcp=False with per-group static addressing.",
    "why_valuable": "Mixed static/DHCP in Roman Urdu — a very common real ask, and it exercises the addressing dimension "
                    "the templates under-represent.",
    "research_inspiration": [],
},

# --- extra routing / multi-site ---------------------------------------------
{
    "id": "oc-035",
    "language": "en", "difficulty": "L3", "category": "wan-multi-site",
    "scenario": "HQ + two branches, static routing, internet only at HQ.",
    "coverage_cell": "small-s3-a1-multi-static-wan-none",
    "length_category": "medium",
    "prompt": "Connect our head office to two branches. Only the head office has the "
              "internet line; both branches should reach the head office and get "
              "internet through it. Use static routing — we don't run a routing "
              "protocol on a network this size.",
    "expected_intent": {
        "intent_version": "1", "name": "hq-two-branches",
        "sites": [
            _site("hq", [_area("main", [_pc("hqpcs", "main", 10)])]),
            _site("branchnorth", [_area("north", [_pc("northpcs", "north", 5)])]),
            _site("branchsouth", [_area("south", [_pc("southpcs", "south", 5)])]),
        ],
        "vlans": [
            _vlan("hqnet", "HQ", site="hq", required=20, dhcp=True),
            _vlan("northnet", "North", site="branchnorth", required=10, dhcp=True),
            _vlan("southnet", "South", site="branchsouth", required=10, dhcp=True),
        ],
        "routing": {"mode": "static"},
        "internet": {"access": "wan", "nat": True, "public_server": False},
        "requirements": [
            _req("REQ-001", "Both branches reach the head office"),
            _req("REQ-002", "Both branches get internet through the head office"),
            _req("REQ-003", "Use static routing, not a dynamic protocol", "constraint"),
        ],
    },
    "expect_refusal": False,
    "expected_behaviour": "Three sites, static routes, WAN internet + NAT at HQ only, DHCP each.",
    "why_valuable": "English hub-and-spoke with an explicit anti-protocol constraint; complements the Urdu/mixed 3-site "
                    "cases (oc-030) so the same topology appears across languages.",
    "research_inspiration": ["https://www.cisco.com/c/en/us/td/docs/voice_ip_comm/cvd/G2basicsmbrnt/G2basicsmbrnt_book/FeatServ.html"],
},
{
    "id": "oc-036",
    "language": "en", "difficulty": "L3", "category": "routing",
    "scenario": "Two sites joined with OSPF, internet at the branch too.",
    "coverage_cell": "small-s2-a1-multi-static-ospf-wan-none",
    "length_category": "medium",
    "prompt": "We're linking our main office and a remote site. Run OSPF between them "
              "so routes appear automatically as we add subnets. Both sites should be "
              "able to reach the internet and each other.",
    "expected_intent": {
        "intent_version": "1", "name": "main-remote-ospf",
        "sites": [
            _site("mainoffice", [_area("floor", [_pc("mainpcs", "floor", 8)])]),
            _site("remotesite", [_area("shed", [_pc("remotepcs", "shed", 4)])]),
        ],
        "vlans": [
            _vlan("mainnet", "Main", site="mainoffice", required=16, dhcp=True),
            _vlan("remotenet", "Remote", site="remotesite", required=10, dhcp=True),
        ],
        "routing": {"mode": "ospf", "ospf_area": "0"},
        "internet": {"access": "wan", "nat": True, "public_server": False},
        "requirements": [
            _req("REQ-001", "Main office and remote site reach each other"),
            _req("REQ-002", "Both sites reach the internet", "connectivity", "high"),
            _req("REQ-003", "OSPF is used between the sites", "operational", "high"),
        ],
    },
    "expect_refusal": False,
    "expected_behaviour": "Two sites, OSPF, one WAN internet block + NAT built at the main site (the engine builds the "
                          "internet only at sites[0], CG-3); the remote site reaches the internet through it. A second, "
                          "independent line at the remote site is a capability gap, not something the engine delivers.",
    "why_valuable": "OSPF across two sites with internet at both — a shape the coverage matrix certifies; keeps routing "
                    "coverage from being static-only.",
    "research_inspiration": [],
},
{
    "id": "oc-037",
    "language": "roman-urdu", "difficulty": "L4", "category": "wan-multi-site",
    "scenario": "Three floors + two sites combined; mixed static routing and internet.",
    "coverage_cell": "small-s2-a2-multi-static-wan-none",
    "length_category": "long",
    "prompt": "Hamara setup thoda bara hai. Karachi head office hai jisme do floors "
              "hain — neeche sales aur upar accounts. Phir Hyderabad ka branch hai "
              "jis mein ek hi office floor hai.\n\n"
              "Chahiye:\n"
              "- Karachi ke dono floors ke networks alag hon (sales aur accounts).\n"
              "- Hyderabad branch Karachi se connected ho.\n"
              "- Internet sirf Karachi par hai; Hyderabad ko internet Karachi se "
              "mile.\n"
              "- Sales aur accounts dono DHCP use karein, branch ke computers bhi DHCP.\n"
              "Static routing theek hai, OSPF mat lagana.",
    "expected_intent": {
        "intent_version": "1", "name": "karachi-hyderabad",
        "sites": [
            _site("karachi", [
                _area("salesfloor", [_pc("salespcs", "salesfloor", 8, vlan="salesnet")]),
                _area("accountsfloor", [_pc("accountspcs", "accountsfloor", 6, vlan="accountsnet")]),
            ]),
            _site("hyderabad", [_area("office", [_pc("branchpcs", "office", 5, vlan="hydnet")])]),
        ],
        "vlans": [
            _vlan("salesnet", "Sales", site="karachi", required=16, dhcp=True),
            _vlan("accountsnet", "Accounts", site="karachi", required=12, dhcp=True),
            _vlan("hydnet", "Hyderabad", site="hyderabad", required=12, dhcp=True),
        ],
        "routing": {"mode": "static"},
        "internet": {"access": "wan", "nat": True, "public_server": False},
        "requirements": [
            _req("REQ-001", "Sales and accounts are separate networks", "isolation", "high"),
            _req("REQ-002", "Hyderabad connects to Karachi"),
            _req("REQ-003", "Hyderabad gets internet through Karachi"),
            _req("REQ-004", "All sites use DHCP", "addressing"),
            _req("REQ-005", "Use static routing, not OSPF", "constraint"),
        ],
    },
    "expect_refusal": False,
    "expected_behaviour": "Multi-floor + multi-site in one: three VLANs across two sites, static routes, WAN internet + "
                          "NAT at Karachi, DHCP everywhere.",
    "why_valuable": "Combines multi-floor and multi-site — the L4 'several interacting features' case in Roman Urdu, with "
                    "real Pakistani city names.",
    "research_inspiration": [],
},

# --- extra wireless / guest (buildable side) ---------------------------------
{
    "id": "oc-038",
    "language": "en", "difficulty": "L2", "category": "wireless",
    "scenario": "Office with a few wireless laptops on the internal network.",
    "coverage_cell": "small-s1-a1-multi-dhcp-cable-none",
    "length_category": "medium",
    "prompt": "Our office has four wired PCs and three laptops that should connect over "
              "WiFi. Put the laptops on the internal network — no separate guest network "
              "needed, they're all staff. Cable internet is fine.",
    "expected_intent": {
        "intent_version": "1", "name": "office-laptops",
        "sites": [_site("office", [_area("workspace", [
            _pc("wiredpcs", "workspace", 4),
            _wifi("laptops", "workspace", 3),
        ])])],
        "vlans": [_vlan("officenet", "Office", site="office", required=16, dhcp=True)],
        "routing": {"mode": "connected"},
        "internet": {"access": "cable", "nat": True, "public_server": False},
        "wireless": [{
            "id": "officewifi", "site": "office", "area": "workspace",
            "ssid": "OfficeWiFi", "security": "wpa2", "passphrase": "office2026",
            "client_group_ids": ["laptops"],
        }],
        "requirements": [
            _req("REQ-001", "Laptops connect over WiFi to the internal network"),
            _req("REQ-002", "All hosts are on the internal network"),
            _req("REQ-003", "Cable internet is available"),
        ],
    },
    "expect_refusal": False,
    "expected_behaviour": "Single office VLAN, one WPA2 cell serving 3 laptops, cable internet. No guest isolation claim.",
    "why_valuable": "The buildable wireless baseline (no guest isolation) — the control against which oc-007/oc-026's "
                    "refusals are measured.",
    "research_inspiration": [],
},
{
    "id": "oc-039",
    "language": "roman-urdu", "difficulty": "L3", "category": "wireless",
    "scenario": "Cafe with guest WiFi and a wired POS/back-of-house network.",
    "coverage_cell": "small-s1-a2-multi-dhcp-cable-none",
    "length_category": "medium",
    "prompt": "Cafe hai — customers ko free WiFi chahiye aur staff ke computers wired "
              "hain (billing aur accounts). Customer WiFi aur staff wired network alag "
              "hon. Cable internet available hai.",
    "expected_intent": {
        "intent_version": "1", "name": "cafe",
        "sites": [_site("cafe", [
            _area("counter", [
                _pc("billingpc", "counter", 1, vlan="staffnet"),
                _pc("accountspc", "counter", 1, vlan="staffnet"),
            ]),
            _area("seating", [_wifi("customerdevices", "seating", 4, device="smartphone", vlan="guestnet")]),
        ])],
        "vlans": [
            _vlan("staffnet", "Staff", site="cafe", required=8, dhcp=True),
            _vlan("guestnet", "Guest", site="cafe", required=10, dhcp=True),
        ],
        "routing": {"mode": "connected"},
        "internet": {"access": "cable", "nat": True, "public_server": False},
        "wireless": [{
            "id": "guestwifi", "site": "cafe", "area": "seating",
            "ssid": "CafeGuest", "security": "open",
            "client_group_ids": ["customerdevices"],
        }],
        "requirements": [
            _req("REQ-001", "Customer WiFi and the staff wired network are separate", "isolation", "high"),
            _req("REQ-002", "Both reach the internet"),
        ],
    },
    "expect_refusal": False,
    "expected_behaviour": "Staff wired VLAN + guest wireless VLAN, open SSID, cable internet. Separation is stated as "
                          "'separate network', NOT as an enforced internet-only ACL, so it stays buildable.",
    "why_valuable": "Cafe/hospitality register with a smartphone-only wireless cell — a device mix the templates skip.",
    "research_inspiration": ["https://www.micronet.global/insights/guest-wifi-hotels-restaurants"],
},

# --- extra IoT / clinic / campus --------------------------------------------
{
    "id": "oc-040",
    "language": "en", "difficulty": "L3", "category": "iot",
    "scenario": "Smart building: IoT sensors plus a normal office LAN.",
    "coverage_cell": "small-s1-a2-multi-dhcp-cable-none-iot",
    "length_category": "medium",
    "prompt": "We're putting a few network-controlled LED light units into a new office. "
              "Keep the IoT lighting on its own network away from the staff PCs, and wire "
              "the lights in. Ordinary cable internet is fine.",
    "expected_intent": {
        "intent_version": "1", "name": "smart-building",
        "sites": [_site("office", [
            _area("workspace", [_pc("staffpcs", "workspace", 8, vlan="staffnet")]),
            _area("ceilingspace", []),
        ])],
        "vlans": [
            _vlan("staffnet", "Staff", site="office", required=16, dhcp=True),
            _vlan("iotnet", "IoT", site="office", required=8, dhcp=True),
        ],
        "routing": {"mode": "connected"},
        "internet": {"access": "cable", "nat": True, "public_server": False},
        "iot": [{"id": "lighting", "site": "office", "area": "ceilingspace",
                 "kind": "smart_led", "count": 5}],
        "requirements": [
            _req("REQ-001", "IoT lighting is on its own network, separate from staff PCs", "isolation", "high"),
            _req("REQ-002", "IoT light units are wired in", "operational"),
        ],
    },
    "expect_refusal": False,
    "expected_behaviour": "Staff VLAN + IoT VLAN declaration, 5 cabled smart_led things, cable internet. The "
                          "'IoT lighting on its own network, separate from staff PCs' clause verifies UNKNOWN — IoT "
                          "things are created and cabled with no IP/VLAN placement (expander _place_iot, CG-2), so they do "
                          "not land on a separately verified segment.",
    "why_valuable": "Second IoT case with different wording/device framing; reinforces the cabled-only IoT model.",
    "research_inspiration": ["https://www.techgeeks.org/iot-isolation-homelab-vlans-firewall-mdns"],
},
{
    "id": "oc-041",
    "language": "roman-urdu", "difficulty": "L4", "category": "enterprise",
    "scenario": "Clinic: reception, consulting, waiting — patient-data separation.",
    "coverage_cell": "small-s1-a3-multi-dhcp-wan-none",
    "length_category": "long",
    "prompt": "Hum char doctors ki clinic khol rahe hain. Network aisa hona chahiye ke "
              "patient ki information waiting area se na dekhi ja sake.\n\n"
              "- Reception aur admin ke 6 computers (bookings aur records).\n"
              "- Consulting rooms mein har room ka ek PC (4 rooms).\n"
              "- Waiting area mein ek shared kiosk PC (1).\n"
              "- Records server ho jis par HTTP aur DNS chalen; woh static ho.\n"
              "- Baaki sab automatic addressing.\n"
              "ISP se WAN handoff internet ke liye hai. Wireless ki abhi zaroorat nahi.",
    "expected_intent": {
        "intent_version": "1", "name": "clinic-urdu",
        "sites": [_site("clinic", [
            _area("reception", [_pc("receptionpcs", "reception", 6, vlan="adminnet")]),
            _area("consulting", [_pc("roompcs", "consulting", 4, vlan="clinicalnet")]),
            _area("waiting", [_pc("kioskpc", "waiting", 1, vlan="waitingnet")]),
        ])],
        "vlans": [
            _vlan("adminnet", "Admin", site="clinic", required=12, dhcp=True),
            _vlan("clinicalnet", "Clinical", site="clinic", required=10, dhcp=True),
            _vlan("waitingnet", "Waiting", site="clinic", required=8, dhcp=True),
        ],
        "routing": {"mode": "connected"},
        "internet": {"access": "wan", "nat": True, "public_server": False},
        "servers": [{"id": "recordssrv", "site": "clinic", "vlan": "clinicalnet",
                     "services": ["http", "dns"]}],
        "requirements": [
            _req("REQ-001", "Patient/consulting data is not reachable from the waiting area", "security", "critical"),
            _req("REQ-002", "Admin, clinical and waiting are separate networks", "isolation", "high"),
            _req("REQ-003", "The records server is static with HTTP and DNS", "addressing", "high"),
            _req("REQ-004", "WAN internet with NAT", "connectivity"),
        ],
    },
    "expect_refusal": False,
    "expected_behaviour": "Three VLANs, static records server (http+dns) on the clinical VLAN, WAN internet. Roman Urdu "
                          "healthcare scenario.",
    "why_valuable": "Roman-Urdu healthcare counterpart to oc-025; keeps the language mix from clustering in 'basic' "
                    "categories only.",
    "research_inspiration": ["https://www.cisa.gov/resources-tools/resources/enhanced-visibility-and-hardening-guidance-communications-infrastructure"],
},
{
    "id": "oc-042",
    "language": "en", "difficulty": "L3", "category": "campus",
    "scenario": "Small college building: lab, classrooms, staff — no guest.",
    "coverage_cell": "medium-s1-a2-multi-dhcp-cable-none",
    "length_category": "medium",
    "prompt": "Design the wired network for one college building: a computer lab with "
              "16 student PCs, four classrooms with 3 PCs each, and a staff room with "
              "5 PCs. Students and staff must be on separate networks; the classrooms "
              "count as student machines. Cable internet. Leave guest WiFi out of scope "
              "for now.",
    "expected_intent": {
        "intent_version": "1", "name": "college-building",
        "sites": [_site("college", [
            _area("lab", [_pc("labpcs", "lab", 16, vlan="studentnet")]),
            _area("classrooms", [_pc("classpcs", "classrooms", 12, vlan="studentnet")]),
            _area("staffroom", [_pc("staffpcs", "staffroom", 5, vlan="staffnet")]),
        ])],
        "vlans": [
            _vlan("studentnet", "Students", site="college", required=32, dhcp=True),
            _vlan("staffnet", "Staff", site="college", required=12, dhcp=True),
        ],
        "routing": {"mode": "connected"},
        "internet": {"access": "cable", "nat": True, "public_server": False},
        "requirements": [
            _req("REQ-001", "Students and staff are on separate networks", "isolation", "high"),
            _req("REQ-002", "Lab and classroom PCs are both student machines"),
            _req("REQ-003", "Cable internet is available"),
        ],
    },
    "expect_refusal": False,
    "expected_behaviour": "Two VLANs (students combining lab+classrooms, staff), cable internet. Guest explicitly out of "
                          "scope, so no isolation gap.",
    "why_valuable": "Campus realism WITHOUT triggering the guest-isolation refusal — shows scope-cutting as a legitimate "
                    "design move, and keeps an English campus case in the pool.",
    "research_inspiration": ["https://www.cisco.com/c/dam/en/us/td/docs/solutions/CVD/Oct2015/Campus_LAN_Wireless_LAN_Design_Oct2105.pdf"],
},

# --- more ambiguity ----------------------------------------------------------
{
    "id": "oc-043",
    "language": "en", "difficulty": "L3", "category": "ambiguous",
    "scenario": "Host count unstated; model must ask how many devices.",
    "coverage_cell": "ambiguous-host-count",
    "length_category": "short",
    "prompt": "I need a network for my office with a staff section and a guest section. "
              "Not sure how big it needs to be yet.",
    "expected_intent": None,
    "expect_refusal": True,
    "expected_behaviour": "ASK: roughly how many devices in the staff section (and guest section)? The VLAN sizing "
                          "(required_hosts) depends on it and the model must not invent a count.",
    "why_valuable": "Missing host count — a second, distinct ambiguity axis from oc-014 (sites) and oc-015 (addressing).",
    "research_inspiration": [],
},
{
    "id": "oc-044",
    "language": "roman-urdu", "difficulty": "L4", "category": "ambiguous",
    "scenario": "Routing preference unstated across two sites.",
    "coverage_cell": "ambiguous-routing",
    "length_category": "medium",
    "prompt": "Do offices hain aur unhe connect karna hai. Routing kaise karni hai yeh "
              "aap hi batayein, mujhe difference nahi pata. Internet dono ko chahiye.",
    "expected_intent": None,
    "expect_refusal": True,
    "expected_behaviour": "The user delegates the routing choice. Preferred behaviour: briefly lay out the static-vs-"
                          "dynamic options and confirm one before building. Acceptable: proceed with a clearly stated "
                          "assumption (static routing for a two-site network). Not acceptable: silently pick a routing mode "
                          "and present it as the user's own choice. Recorded as ambiguous because the 'right' routing mode "
                          "is not recoverable with certainty from the text.",
    "why_valuable": "Realistic 'I don't know, you tell me' delegation. Tests whether the model surfaces the decision "
                    "instead of burying an assumption.",
    "research_inspiration": [],
},
{
    "id": "oc-045",
    "language": "roman-urdu", "difficulty": "L5", "category": "ambiguous",
    "scenario": "Server role + internet type both unclear in a business-context ask.",
    "coverage_cell": "ambiguous-server-internet",
    "length_category": "medium",
    "prompt": "Humara startup hai, abhi 4 log hain. Ek server rakhna chahiye shayad, aur "
              "internet chahiye. Kya kya banwana hai aap hi suggest karein — mujhe "
              "networking ka idea nahi hai lekin budget tight hai.",
    "expected_intent": None,
    "expect_refusal": True,
    "expected_behaviour": "Highly open: no site layout, unclear whether a server is even needed, no internet type, no "
                          "host counts, and the user asks for suggestions on a tight budget. Correct behaviour is a short "
                          "clarifying dialogue (or a modest proposed design + questions), NOT a confident full build. This "
                          "is the hardest ambiguity because it invites invention at every layer.",
    "why_valuable": "L5 ambiguity: a novice, budget-constrained, open-ended business request. The temptation to over-build "
                    "is maximal — exactly what we want to test.",
    "research_inspiration": [],
},

# --- more contradiction / refusal -------------------------------------------
{
    "id": "oc-046",
    "language": "en", "difficulty": "L4", "category": "contradictory",
    "scenario": "All traffic must be isolated, yet every group must reach every other.",
    "coverage_cell": "contradiction-total-isolation-vs-full-mesh",
    "length_category": "medium",
    "prompt": "I want four networks — one per department. Nothing should be able to "
              "reach anything outside its own department. But also, every department "
              "needs to print to the shared printer in HR and open the file server.",
    "expected_intent": None,
    "expect_refusal": True,
    "expected_behaviour": "SURFACE CONTRADICTION: 'absolutely nothing reaches outside its own department' cannot coexist "
                          "with 'every department reaches the HR printer and the file server'. The model must flag the "
                          "conflict and ask which wins (or propose per-port exceptions), not silently resolve it.",
    "why_valuable": "A sharper form of the isolation-vs-service contradiction in oc-018, stated with 'non-negotiable' on "
                    "both sides — the model must refuse to silently pick one.",
    "research_inspiration": [],
},
{
    "id": "oc-047",
    "language": "roman-urdu", "difficulty": "L4", "category": "contradictory",
    "scenario": "Static addressing demanded, but also 'everyone gets IPs automatically'.",
    "coverage_cell": "contradiction-static-vs-dhcp",
    "length_category": "short",
    "prompt": "Saare computers ko fixed (static) IP addresses dein, aur yeh bhi chahiye "
              "ke har computer automatically apna IP le. Dono same rehne chahiye.",
    "expected_intent": None,
    "expect_refusal": True,
    "expected_behaviour": "SURFACE CONTRADICTION: static addressing and automatic (DHCP) addressing are mutually exclusive "
                          "for the same hosts. The model must point out the conflict and ask which the user actually wants "
                          "(or clarify: static servers + DHCP users), not build a contradiction or pick silently.",
    "why_valuable": "A very common real-world confusion (static vs DHCP) expressed as a flat contradiction. Great test of "
                    "basic addressing concept grounding.",
    "research_inspiration": [],
},
{
    "id": "oc-048",
    "language": "en", "difficulty": "L5", "category": "contradictory",
    "scenario": "Two mutually exclusive internet access types demanded at one site.",
    "coverage_cell": "contradiction-internet-shape",
    "length_category": "medium",
    "prompt": "Our single office needs internet. It has to come in over the DSL line, "
              "and at the same time over the cable modem, bonded together into one "
              "link. Security also wants the ASA firewall in front, and it has to be "
              "a 5505.",
    "expected_intent": None,
    "expect_refusal": True,
    "expected_behaviour": "TWO issues the model must surface: (1) demanding both DSL and cable as one bonded link is not "
                          "an expressible access shape (InternetIntent.access is exactly one of wan/dsl/cable) and bonding "
                          "is unsupported; (2) an ASA 5505 with the described edge is fine only single-site/single-VLAN, so "
                          "the model must confirm the topology. Must not silently pick one access type or fake a bonded "
                          "link.",
    "why_valuable": "L5 contradiction touching a real schema limit (single internet access shape) plus a device constraint "
                    "— a genuinely hard, realistic 'requirements that can't both hold' case.",
    "research_inspiration": [],
},
{
    "id": "oc-049",
    "language": "roman-urdu", "difficulty": "L5", "category": "contradictory",
    "scenario": "Guest must be internet-only AND reach internal servers, plus no firewall allowed.",
    "coverage_cell": "contradiction-guest-triple",
    "length_category": "medium",
    "prompt": "Guest network bilkul internet-only honi chahiye, koi internal access nahi. "
              "Lekin guest ko company ki website bhi dekhni hai jo andar ke server par "
              "hai. Alag firewall appliance ka budget bhi nahi hai, to us ke bagair hi "
              "yeh sab sort karna hai.",
    "expected_intent": None,
    "expect_refusal": True,
    "expected_behaviour": "TRIPLE conflict: (a) guest internet-only vs (b) guest reaching an internal server, and (c) no "
                          "firewall appliance budget while guest isolation normally needs an enforcement point. The model "
                          "must surface all of it — it cannot satisfy isolation without the enforcement device it was told "
                          "not to deploy. Realistic budget-vs-requirement clash.",
    "why_valuable": "Hardest contradiction: three interlocking constraints in Roman Urdu, including 'don't use the very "
                    "tool that would enforce the policy'.",
    "research_inspiration": [],
},

# --- more impossible / refusal (real engine limits) --------------------------
{
    "id": "oc-050",
    "language": "en", "difficulty": "L3", "category": "impossible-refusal",
    "scenario": "Wireless group larger than the verified 8-client cell limit.",
    "coverage_cell": "refusal-wireless-too-large",
    "length_category": "short",
    "prompt": "We need WiFi for 20 laptops in one open room, all off a single access "
              "point.",
    "expected_intent": None,
    "expect_refusal": True,
    "expected_behaviour": "REFUSE-BY-NAME: 'wireless-group-too-large' — PT radio cells are verified up to 8 clients; 20 "
                          "in one cell exceeds that. Explain and offer to split into multiple smaller cells. This is a "
                          "REAL verified limit, not invented.",
    "why_valuable": "A concrete numeric refusal grounded in the 8-client verified ceiling — tests honest limits over "
                    "fake scalability.",
    "research_inspiration": [],
},
{
    "id": "oc-051",
    "language": "roman-urdu", "difficulty": "L3", "category": "impossible-refusal",
    "scenario": "ASA 5505 with a DMZ — a single named refusal.",
    "coverage_cell": "refusal-asa5505-dmz",
    "length_category": "short",
    "prompt": "Humein ASA 5505 firewall chahiye aur uske saath ek DMZ bhi jahan public "
              "server rakhein.",
    "expected_intent": None,
    "expect_refusal": True,
    "expected_behaviour": "REFUSE-BY-NAME: 'asa5505-no-dmz' — the ASA 5505 has no DMZ support in the verified shapes. "
                          "Offer the ASA 5506-X DMZ perimeter instead.",
    "why_valuable": "Single, clean named refusal; complements the multi-refusal oc-019 by isolating one limit.",
    "research_inspiration": [],
},
{
    "id": "oc-052",
    "language": "en", "difficulty": "L4", "category": "impossible-refusal",
    "scenario": "Server-side DHCP + a specific non-site-block subnet requested.",
    "coverage_cell": "refusal-dhcp-and-cidr",
    "length_category": "medium",
    "prompt": "Run DHCP from a Windows server for the whole office, and put the office "
              "VLAN on 192.168.50.0/24 — that exact subnet, please.",
    "expected_intent": None,
    "expect_refusal": True,
    "expected_behaviour": "TWO named refusals: (1) server-side DHCP is 'server-dhcp-service' (unsupported; use gateway "
                          "pools); (2) a user-fixed CIDR outside the engine's per-site 10.N.0.0/16 block triggers "
                          "'vlan-outside-site-block' at expansion. The model must surface both real constraints rather "
                          "than fake them.",
    "why_valuable": "Combines the DHCP-service limit with the fixed-CIDR site-block limit — two independent real "
                    "constraints that a careless model would silently 'make work'.",
    "research_inspiration": [],
},

# --- extra long-document / enterprise ---------------------------------------
{
    "id": "oc-053",
    "language": "en", "difficulty": "L5", "category": "long-document",
    "scenario": "Two-floor office with servers, mixed addressing, exclusions, priorities.",
    "coverage_cell": "medium-s1-a2-multi-static-wan-none",
    "length_category": "document",
    "prompt": "Company: a 20-person accounting firm on two floors of a building.\n\n"
              "People & devices:\n"
              "- Ground floor: 8 accountant PCs and 2 shared reception PCs.\n"
              "- First floor: 6 auditor PCs and a small server room with 2 servers.\n\n"
              "Requirements:\n"
              "- Auditors must be isolated from the accounting staff — auditors are "
              "external, so they get their own network and must not reach the accounting "
              "machines or the servers.\n"
              "- The two servers host our ledger application (HTTP) and internal DNS; "
              "accountants need them, auditors do not.\n"
              "- Accountant and reception PCs get addresses automatically; the two servers "
              "must be static.\n\n"
              "Exclusions:\n"
              "- No wireless anywhere.\n"
              "- No guest/public internet kiosks.\n"
              "We have a WAN internet handoff for the accounting side only.\n"
              "Priority: auditor isolation first, then server access, then internet.",
    "expected_intent": {
        "intent_version": "1", "name": "accounting-firm",
        "sites": [_site("building", [
            _area("serverroom", []),
            _area("ground", [
                _pc("accountantpcs", "ground", 8, vlan="accountingnet"),
                _pc("receptionpcs", "ground", 2, vlan="accountingnet"),
            ]),
            _area("first", [
                _pc("auditorpcs", "first", 6, vlan="auditornet"),
            ]),
        ])],
        "vlans": [
            _vlan("accountingnet", "Accounting", site="building", required=20, dhcp=True),
            _vlan("auditornet", "Auditors", site="building", required=12, dhcp=False),
            _vlan("servernet", "Servers", site="building", required=6, dhcp=False),
        ],
        "routing": {"mode": "connected"},
        "internet": {"access": "wan", "nat": True, "public_server": False},
        "servers": [
            {"id": "ledgersrv", "site": "building", "vlan": "servernet", "services": ["http"]},
            {"id": "dnssrv", "site": "building", "vlan": "servernet", "services": ["dns"]},
        ],
        "requirements": [
            _req("REQ-001", "Auditors are isolated and cannot reach accounting machines or servers", "security", "critical"),
            _req("REQ-002", "The ledger HTTP server and DNS server exist for accountants", "service", "high"),
            _req("REQ-003", "Accountant and reception PCs use DHCP; servers are static", "addressing", "high"),
            _req("REQ-004", "No wireless and no public kiosks", "constraint"),
            _req("REQ-005", "WAN internet for the accounting side", "connectivity"),
        ],
    },
    "expect_refusal": False,
    "expected_behaviour": "Three VLANs (accounting DHCP, auditors isolated, servers static), two servers (http, dns) on "
                          "the server VLAN, WAN internet. Auditor isolation is an internal-subnet separation the engine "
                          "models via separate subnets — no guest_isolation flag needed, so it builds.",
    "why_valuable": "Another true long-document with auditor-isolation, mixed addressing, and explicit priorities. "
                    "Forces full-document extraction; the auditor-isolation-vs-server-access split is the hard part.",
    "research_inspiration": ["https://www.censinet.com/perspectives/network-segmentation-strategies-healthcare-it"],
},
{
    "id": "oc-054",
    "language": "mixed", "difficulty": "L5", "category": "long-document",
    "scenario": "Retail chain HQ + store: POS isolation, cameras, guest, WAN.",
    "coverage_cell": "small-s2-a2-multi-static-wan-none-iot",
    "length_category": "document",
    "prompt": "Business: 'Al-Noor Mart' — ek head office aur ek retail store.\n\n"
              "Head office (Karachi):\n"
              "- 6 office PCs (accounts + admin).\n"
              "- Ek server jis par inventory system (HTTP) chale.\n\n"
              "Retail store (Lahore):\n"
              "- 2 POS terminals jo bilkul isolated honi chahiye (compliance).\n"
              "- 1 back-office PC.\n"
              "- 3 IP cameras jo store ke andar wired hon.\n"
              "- Customer ke liye WiFi (guest).\n\n"
              "Requirements:\n"
              "- POS terminals apne alag network par hon, so that koi doosra device unhe "
              "na dekh sake.\n"
              "- Cameras ka apna network ho.\n"
              "- Guest WiFi sirf internet tak.\n"
              "- Head office aur store aapas mein connected hon (static routing) so that "
              "inventory system dono jagah se open ho.\n"
              "- Internet dono jagah chahiye (store ki alag line).\n"
              "Exclusion: DHCP POS par nahi, and POS ke addresses static hon.",
    "expected_intent": {
        "intent_version": "1", "name": "alnoor-mart",
        "sites": [
            _site("karachi", [
                _area("office", [_pc("officepcs", "office", 6)]),
            ]),
            _site("lahore", [
                _area("salesfloor", [
                    _pc("pos1", "salesfloor", 1, addressing="static"),
                    _pc("pos2", "salesfloor", 1, addressing="static"),
                ]),
                _area("backoffice", [_pc("backofficepc", "backoffice", 1)]),
                _area("ceiling", [_pc("camerapanel", "ceiling", 1)]),
            ]),
        ],
        "vlans": [
            _vlan("officenet", "Head office", site="karachi", required=12, dhcp=True),
            _vlan("posnet", "POS", site="lahore", required=6, dhcp=False),
            _vlan("storenet", "Store back office", site="lahore", required=8, dhcp=True),
            _vlan("cameranet", "Cameras", site="lahore", required=6, dhcp=True),
        ],
        "routing": {"mode": "static"},
        "internet": {"access": "wan", "nat": True, "public_server": False},
        "servers": [{"id": "inventory", "site": "karachi", "vlan": "officenet",
                     "services": ["http"]}],
        "iot": [{"id": "cameras", "site": "lahore", "area": "ceiling",
                 "kind": "smart_led", "count": 3}],
        "requirements": [
            _req("REQ-001", "POS terminals are isolated on their own network", "security", "critical"),
            _req("REQ-002", "Cameras are on their own network", "isolation", "high"),
            _req("REQ-003", "The inventory HTTP server is reachable from both sites", "service", "high"),
            _req("REQ-004", "Head office and store are connected via static routing"),
            _req("REQ-005", "Internet at both sites", "connectivity"),
            _req("REQ-006", "POS uses static addresses, not DHCP", "addressing", "high"),
        ],
    },
    "expect_refusal": True,
    "expected_intent": None,
    "expected_behaviour": "REFUSE-BY-NAME with three real reasons surfaced: (a) 'guest WiFi sirf internet tak' is a "
                          "guest-isolation guarantee the engine refuses ('guest-isolation-unsupported'); (b) real IP "
                          "cameras are not expressible — the schema's only IoT kind is smart_led, so the cameras cannot be "
                          "built as asked; (c) 'internet dono jagah ... store ki alag line' over-claims — the engine builds "
                          "one internet block at sites[0] (CG-3). The POS-static and two-site static-routing parts are "
                          "otherwise buildable; the correct output names the three refusals rather than faking them.",
    "why_valuable": "The most enterprise-flavoured long document: multi-site retail with compliance-driven POS isolation. "
                    "Held at the honest capability boundary (cameras + guest isolation) instead of faking support.",
    "research_inspiration": ["https://ictally.com/resources/blog/restaurant-guest-wifi-vs-pos-network-setup"],
},

# --- diagnosis / enhancement flavour ----------------------------------------
{
    "id": "oc-055",
    "language": "en", "difficulty": "L3", "category": "diagnosis",
    "scenario": "Post-VLAN-change DHCP breakage described as a fix request.",
    "coverage_cell": "small-s1-a2-multi-dhcp-dsl-none",
    "length_category": "medium",
    "prompt": "We recently split our network into two VLANs — one side has six PCs and "
              "the other five — and now half the machines can't get an address; they "
              "just self-assign and can't reach anything, not even our intranet server. "
              "It sits on the first VLAN and also does DNS for us. Redesign it so both "
              "VLANs hand out proper addresses and everything that should reach the "
              "server can. We're on DSL.",
    "expected_intent": {
        "intent_version": "1", "name": "dhcp-fix",
        "sites": [_site("office", [
            _area("sectiona", [_pc("apcs", "sectiona", 6, vlan="anet")]),
            _area("sectionb", [_pc("bpcs", "sectionb", 5, vlan="bnet")]),
        ])],
        "vlans": [
            _vlan("anet", "Section A", site="office", required=12, dhcp=True),
            _vlan("bnet", "Section B", site="office", required=12, dhcp=True),
        ],
        "routing": {"mode": "connected"},
        "internet": {"access": "dsl", "nat": True, "public_server": False},
        "servers": [{"id": "officesrv", "site": "office", "vlan": "anet",
                     "services": ["http", "dns"]}],
        "requirements": [
            _req("REQ-001", "Both VLANs hand out proper addresses (DHCP)", "addressing", "high"),
            _req("REQ-002", "Everything that should reach the server can", "connectivity", "high"),
            _req("REQ-003", "DSL internet with NAT"),
        ],
    },
    "expect_refusal": False,
    "expected_behaviour": "Two DHCP VLANs + a server (http/dns) on VLAN A + DSL internet. The 'fix the DHCP breakage' "
                          "narrative maps to a clean buildable target.",
    "why_valuable": "Second diagnosis-flavoured case based on the very common 'VLAN split broke DHCP' real incident.",
    "research_inspiration": ["https://learningnetwork.cisco.com/s/question/0D53i00000KsvVbCAJ/dhcp-not-working-after-configuring-vlans"],
},
{
    "id": "oc-056",
    "language": "roman-urdu", "difficulty": "L3", "category": "enhancement",
    "scenario": "Existing simple office to be enhanced with a new server + VLAN.",
    "coverage_cell": "small-s1-a2-multi-dhcp-none-none",
    "length_category": "medium",
    "prompt": "Mere paas ek chhota office network hai (sab ek hi network par). Ab mujhe "
              "naya ek file server chahiye aur accounts ko alag network par lena hai "
              "kyunki naya audit aa raha hai. Baaki sab waise hi DHCP rahe.",
    "expected_intent": {
        "intent_version": "1", "name": "office-enhance",
        "sites": [_site("office", [
            _area("workspace", [_pc("generalpcs", "workspace", 8, vlan="generalnet")]),
            _area("accounts", [_pc("accountspcs", "accounts", 4, vlan="accountsnet")]),
            _area("serverroom", [_pc("filesrvhost", "serverroom", 1, addressing="static", vlan="servernet")]),
        ])],
        "vlans": [
            _vlan("generalnet", "General", site="office", required=16, dhcp=True),
            _vlan("accountsnet", "Accounts", site="office", required=10, dhcp=True),
            _vlan("servernet", "Servers", site="office", required=6, dhcp=False),
        ],
        "routing": {"mode": "connected"},
        "servers": [{"id": "filesrv", "site": "office", "vlan": "servernet",
                     "services": ["http", "ftp"]}],
        "requirements": [
            _req("REQ-001", "A new file server exists", "service", "high"),
            _req("REQ-002", "Accounts are on a separate network for the audit", "isolation", "high"),
            _req("REQ-003", "General machines keep using DHCP", "addressing"),
        ],
    },
    "expect_refusal": False,
    "expected_behaviour": "General + accounts + server VLANs; file server (http/ftp) on the static server VLAN; general "
                          "stays DHCP. Enhancement framing ('I already have X, add Y').",
    "why_valuable": "Enhancement register (modify an existing network) rather than greenfield — a distinct request type "
                    "the templates under-cover, in Roman Urdu.",
    "research_inspiration": [],
},
]

