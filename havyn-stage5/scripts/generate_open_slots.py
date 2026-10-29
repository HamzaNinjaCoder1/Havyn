"""Generate AI candidates for the 44 coverage-driven Gold slots.

These are **AI-generated candidates**, not owner-authored Gold:
    origin: ai_generated_candidate
    owner_adoption: pending

Each intent is built from the certified coverage cell, pinned, and verified
through schema -> intent lint -> expander -> spec lint -> IPAM -> compile.

Usage:
    python generate_open_slots.py
"""

from __future__ import annotations

import json
import random
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for p in (str(REPO), str(REPO / "havyn-stage5" / "scripts"),
          str(REPO / "havyn-stage5" / "prompt-authoring" / "owner-candidates")):
    if p not in sys.path:
        sys.path.insert(0, p)

from generate_intents import build_intent, coverage_cells  # noqa: E402
from verify_candidates import verify_record  # noqa: E402

HERE = Path(__file__).resolve().parent.parent / "prompt-authoring" / "coverage-candidates"
OUT = HERE / "candidates"
PLAN = Path(__file__).resolve().parent.parent / "prompt-authoring" / "reports" / "owner_gold_authoring_plan.md"

SCENARIOS = [
    ("Northwind Foods", "a food distributor", "Karachi", "IT manager"),
    ("CityCare Clinic", "a private clinic", "Lahore", "practice manager"),
    ("Sunrise School", "a school", "Islamabad", "network coordinator"),
    ("Apex Manufacturing", "a factory", "Faisalabad", "plant engineer"),
    ("Bluewave Networks", "an ISP", "Multan", "network lead"),
    ("GreenMart Retail", "a retail chain", "Rawalpindi", "store operations lead"),
    ("Horizon Bank", "a bank branch", "Peshawar", "branch IT officer"),
    ("Crescent Textiles", "a textile mill", "Sialkot", "systems administrator"),
    ("Orbit Software", "a software house", "Karachi", "office manager"),
    ("Metro Hospital", "a hospital", "Lahore", "biomedical IT lead"),
    ("Falcon Logistics", "a logistics company", "Quetta", "warehouse supervisor"),
    ("Pearl Hotels", "a hotel group", "Murree", "IT coordinator"),
    ("Unity College", "a college", "Hyderabad", "lab administrator"),
    ("Delta Pharma", "a pharma plant", "Islamabad", "quality systems lead"),
    ("Redstone Builders", "a construction firm", "Gwadar", "site engineer"),
    ("Silverline Media", "a post-production studio", "Karachi", "post-production lead"),
    ("Cedar Legal", "a law firm", "Lahore", "office administrator"),
    ("Atlas Freight", "a freight company", "Faisalabad", "operations manager"),
    ("Nova Energy", "an energy company", "Sukkur", "SCADA engineer"),
    ("Harbor Seafood", "a seafood processor", "Karachi", "plant IT lead"),
    ("Meadow Farms", "an agribusiness", "Sahiwal", "farm manager"),
    ("Zenith Audit", "an audit firm", "Islamabad", "audit partner"),
    ("Pioneer Motors", "a car workshop", "Lahore", "workshop manager"),
    ("Aurora Ceramics", "a ceramics factory", "Multan", "factory IT lead"),
    ("Summit Travel", "a travel agency", "Rawalpindi", "branch manager"),
    ("Riverside University", "a university", "Peshawar", "network administrator"),
    ("Golden Bakery", "a bakery", "Karachi", "operations lead"),
    ("Vertex Data", "a data centre", "Lahore", "infrastructure lead"),
    ("Lakeside Resort", "a resort", "Naran", "guest services manager"),
    ("Ironclad Security", "a security services firm", "Islamabad", "control room lead"),
    ("Falcon Textiles", "a textile exporter", "Faisalabad", "IT officer"),
    ("Crescent Pharma", "a pharmaceutical distributor", "Karachi", "IT manager"),
    ("Bluebird School", "a school", "Multan", "IT teacher"),
    ("Orchard Foods", "a food processing plant", "Sialkot", "plant manager"),
    ("Summit Logistics", "a logistics firm", "Quetta", "fleet IT lead"),
    ("Pearl Diagnostics", "a diagnostics lab", "Lahore", "lab IT lead"),
    ("Nexus Consulting", "a consulting firm", "Karachi", "office manager"),
    ("Greenfield Seeds", "an agritech company", "Sahiwal", "operations lead"),
    ("Skyline Realty", "a real-estate office", "Islamabad", "facilities IT"),
    ("Coastal Shipping", "a shipping agent", "Gwadar", "port IT lead"),
    ("Silver Spoon Restaurant", "a restaurant", "Lahore", "owner"),
    ("Metro College", "a college", "Rawalpindi", "admin lead"),
    ("Apex Microfinance", "a microfinance bank", "Hyderabad", "branch IT officer"),
    ("Horizon Telecom", "a telecom field office", "Peshawar", "field engineer"),
]


def _parse_plan() -> list[dict]:
    text = PLAN.read_text(encoding="utf-8")
    rows = re.findall(
        r"\| (gold-\d{3}) \| `([^`]+)` \| ([a-z-]+) \| (L\d) \| OPEN \| O \|", text
    )
    if len(rows) != 44:
        raise SystemExit(f"expected 44 open slots in the plan, found {len(rows)}")
    return [
        {"slot": slot, "cell": cell, "language": language, "difficulty": difficulty}
        for slot, cell, language, difficulty in rows
    ]


def _pick(options, index: int) -> str:
    return options[index % len(options)]


def _site_phrase(cell, lang: str) -> str:
    if lang == "en":
        return (f"one building with {cell.areas_per_site} area(s)"
                if cell.site_count == 1
                else f"{cell.site_count} sites, each with {cell.areas_per_site} area(s)")
    if lang == "roman-urdu":
        return (f"ek building jismein {cell.areas_per_site} areas hain"
                if cell.site_count == 1
                else f"{cell.site_count} sites, har ek mein {cell.areas_per_site} areas")
    return (f"one building with {cell.areas_per_site} areas"
            if cell.site_count == 1
            else f"{cell.site_count} sites, har site pe {cell.areas_per_site} areas")


def _vlan_sentence(cell, lang: str, i: int) -> str:
    if lang == "en":
        opts = (["Keep each area on its own VLAN.", "Put every area in its own VLAN."]
                if cell.vlan_mode == "multi"
                else ["Use one shared network per site.", "Keep a single network for the whole site."])
    elif lang == "roman-urdu":
        opts = (["Har area ko apni alag VLAN par rakhein.", "Har area ki apni alag VLAN honi chahiye."]
                if cell.vlan_mode == "multi"
                else ["Har site ke liye ek hi shared network rakhein.", "Poori site ke liye ek hi network use karein."])
    else:
        opts = (["Har area apni alag VLAN pe rakhni hai, so that broadcast domains separate rahein.",
                 "Each area should sit on its own VLAN, taake traffic alag rahe."]
                if cell.vlan_mode == "multi"
                else ["Ek hi shared network per site theek rahega, no VLAN split needed.",
                      "Single network per site is fine, so keep it simple."])
    return _pick(opts, i)


def _addressing(cell, lang: str, i: int) -> str:
    static = cell.addressing == "static"
    if lang == "en":
        opts = (["Use static addresses on the PCs.", "Set the PCs to static addressing."] if static
                else ["Give the PCs DHCP addresses.", "Let the PCs pick up DHCP addresses."])
    elif lang == "roman-urdu":
        opts = (["PCs par static address set karein.", "PCs ke addresses static rakhein."] if static
                else ["PCs ko DHCP se address milna chahiye.", "PCs ke liye DHCP chalu rakhein."])
    else:
        opts = (["PCs pe static addressing use karein, no DHCP.", "Addresses static rakhne hain on the PCs."] if static
                else ["PCs ko DHCP se addresses milne chahiye, and keep it simple.",
                      "DHCP chalu ho for the PCs, so they get addresses automatically."])
    return _pick(opts, i)


def _routing(cell, lang: str, i: int) -> str:
    if cell.routing == "ospf":
        opts_en = ["Run OSPF between the sites.", "Use OSPF for routing between sites."]
        opts_ru = ["Sites ke darmiyan OSPF chalayein.", "Sites ke beech OSPF routing use karein."]
        opts_mx = ["Sites ke beech OSPF chalayein, so routes exchange ho jayein.",
                   "OSPF use karein between the sites, and keep it standard."]
    elif cell.routing == "static" and cell.site_count > 1:
        opts_en = ["Use static routes between the sites.", "Route between sites with static routes."]
        opts_ru = ["Sites ke darmiyan static routes use karein.", "Sites ke beech static routing rakhein."]
        opts_mx = ["Sites ke beech static routes use karein, but keep them documented.",
                   "Static routing between the sites is fine, so no dynamic protocol needed."]
    else:
        # Single-site static: the intent still declares static, so the prompt
        # must say static too (do not soften it to "connected").
        opts_en = ["Keep routing simple and static.", "Keep the routing static and simple."]
        opts_ru = ["Routing simple aur static rakhein.", "Routing static rakhna, koi dynamic protocol nahi."]
        opts_mx = ["Routing simple rakhein and static, no dynamic protocol.",
                   "Keep routing static and simple, so nothing dynamic chahiye."]
    return _pick({"en": opts_en, "roman-urdu": opts_ru}.get(lang, opts_mx), i)


def _internet(cell, lang: str, i: int) -> str:
    if cell.internet == "none":
        opts = {
            "en": ["No internet is required.", "Internet is not needed for this one."],
            "roman-urdu": ["Internet ki zaroorat nahi hai.", "Is setup mein internet nahi chahiye."],
            "mixed": ["Internet ki zaroorat nahi hai, so skip that part.",
                      "No internet needed for this one, filhal."],
        }
        return _pick(opts.get(lang, opts["mixed"]), i)
    label = {"dsl": "DSL", "cable": "cable", "wan": "WAN"}[cell.internet]
    if cell.firewall != "none":
        opts = {
            "en": [f"Bring in {label} internet behind an ASA 5506 firewall.",
                   f"Provide {label} internet through an ASA 5506 at the edge."],
            "roman-urdu": [f"{label} internet ASA 5506 firewall ke peeche laayein.",
                           f"Edge par ASA 5506 ke through {label} internet dein."],
            "mixed": [f"{label} internet chahiye behind an ASA 5506 firewall.",
                      f"Internet {label} se aayega through an ASA 5506 at the edge."],
        }
    else:
        opts = {
            "en": [f"Provide {label} internet.", f"Give the site {label} internet access."],
            "roman-urdu": [f"{label} internet provide karein.", f"Site ko {label} internet dein."],
            "mixed": [f"{label} internet provide karein, and NAT bhi enable ho.",
                      f"Internet {label} se aayega, so plan the edge accordingly."],
        }
    return _pick(opts.get(lang, opts["mixed"]), i)


def _firewall(cell, lang: str, i: int) -> str:
    if cell.firewall == "asa-dmz":
        opts = {
            "en": ["Put the public web server in a DMZ on the ASA 5506.",
                   "The ASA 5506 should hold the public web server in a DMZ."],
            "roman-urdu": ["Public web server ko ASA 5506 par DMZ mein rakhein.",
                           "ASA 5506 ke DMZ mein public web server rakhein."],
            "mixed": ["Public web server ko ASA 5506 pe DMZ mein rakhein, isolated from inside.",
                      "ASA 5506 par DMZ banayein and put the public web server there."],
        }
    elif cell.firewall == "asa":
        opts = {
            "en": ["Use an ASA 5506 as the edge firewall, with a public web server outside.",
                   "The edge firewall should be an ASA 5506 with a public web server outside it."],
            "roman-urdu": ["Edge par ASA 5506 firewall use karein, public web server bahar rakhein.",
                           "ASA 5506 edge firewall lagayein aur public web server bahar ho."],
            "mixed": ["Edge pe ASA 5506 firewall use karein, with the public web server outside.",
                      "ASA 5506 as edge firewall, and public web server outside rakhein."],
        }
    else:
        return ""
    return _pick(opts.get(lang, opts["mixed"]), i)


def _wireless(cell, lang: str, i: int) -> str:
    if not cell.wireless:
        return ""
    opts = {
        "en": ["Provide staff WiFi for laptops in the first area of each site.",
               "Add a staff WiFi network for laptops in the first area of each site."],
        "roman-urdu": ["Har site ke pehle area mein laptops ke liye staff WiFi dein.",
                       "Pehle area mein laptops ke liye staff WiFi chahiye, har site par."],
        "mixed": ["Staff WiFi chahiye for laptops in the first area of each site.",
                  "Har site ke first area mein laptops ke liye staff WiFi dein, and secure it."],
    }
    return _pick(opts.get(lang, opts["mixed"]), i)


def _iot(cell, lang: str, i: int) -> str:
    if not cell.iot:
        return ""
    opts = {
        "en": ["Add smart LED lighting on the network in the first area of each site.",
               "Put smart LED lighting on the network in the first area of each site."],
        "roman-urdu": ["Har site ke pehle area mein network par smart LED lighting add karein.",
                       "Pehle area mein smart LED lights network se connect karein, har site par."],
        "mixed": ["Smart LED lighting add karein on the network in the first area of each site.",
                  "Har site ke first area mein smart LEDs network pe lagane hain."],
    }
    return _pick(opts.get(lang, opts["mixed"]), i)


def _server(cell, lang: str, i: int) -> str:
    if not cell.servers:
        return ""
    opts = {
        "en": ["Include a DNS and HTTP server on the first site.",
               "Add a DNS and HTTP server on the first site."],
        "roman-urdu": ["Pehli site par DNS aur HTTP server include karein.",
                       "Pehli site par ek DNS aur HTTP server rakhna hai."],
        "mixed": ["First site pe DNS aur HTTP server include karein.",
                  "DNS and HTTP server chahiye on the first site."],
    }
    return _pick(opts.get(lang, opts["mixed"]), i)


def _security(cell, lang: str, i: int) -> str:
    if not cell.security_requirements:
        return ""
    opts = {
        "en": ["The public server must be reachable from outside, but outside traffic must not initiate into the inside network.",
               "Outside clients should reach the public server only; they must not be able to start connections into the inside network."],
        "roman-urdu": ["Public server bahar se reachable ho, lekin bahar se koi inside network mein initiate na kar sake.",
                       "Bahar se sirf public server tak pahunchein, inside network mein connection start na kar sakein."],
        "mixed": ["Public server bahar se reachable ho, but outside traffic must not initiate into the inside network.",
                  "Outside se sirf public server reach ho, and no inbound connections into the inside network."],
    }
    return _pick(opts.get(lang, opts["mixed"]), i)


def _lead(scenario, cell, lang: str, variant: int) -> str:
    company, domain, city, persona = scenario
    phrase = _site_phrase(cell, lang)
    if lang == "en":
        opts = [
            f"I look after the network for {company}, {domain} in {city}. "
            f"We are redoing {phrase} and I need a clean design.",
            f"{company} is {domain} in {city}. Our network spans {phrase}; "
            f"I am the {persona} and need this rebuilt.",
        ]
    elif lang == "roman-urdu":
        opts = [
            f"Main {company} ka network dekhta hoon, {domain}, {city} mein. "
            f"Hum {phrase} dobara set up kar rahe hain aur mujhe ek saaf design chahiye.",
            f"{company} {city} mein {domain} hai. Hamara network {phrase} tak hai; "
            f"main {persona} hoon aur yeh rebuild karna hai.",
        ]
    else:
        opts = [
            f"Main {company} ka network handle karta hoon, {domain} in {city}. "
            f"Network rebuild karna hai for {phrase}, so I need a clean design.",
            f"{company} is {domain} in {city}, aur main {persona} hoon. "
            f"Hum {phrase} dobara kar rahe hain, so give me a practical design.",
        ]
    return _pick(opts, variant)


def _prompt(cell, scenario, variant: int, lang: str) -> str:
    facts = [
        _vlan_sentence(cell, lang, variant),
        _addressing(cell, lang, variant),
        _routing(cell, lang, variant),
        _internet(cell, lang, variant),
        _firewall(cell, lang, variant),
        _wireless(cell, lang, variant),
        _iot(cell, lang, variant),
        _server(cell, lang, variant),
        _security(cell, lang, variant),
    ]
    facts = [f for f in facts if f]
    lead = _lead(scenario, cell, lang, variant)
    header = {"en": "What I need:", "roman-urdu": "Mujhe yeh chahiye:",
              "mixed": "What I need:"}[lang]
    closing = {"en": "Keep it practical and standard.",
               "roman-urdu": "Sab practical aur standard rakhein.",
               "mixed": "Keep it practical and standard, please."}[lang]
    connector = {"en": "Also: ", "roman-urdu": "Aur: ", "mixed": "Also, "}[lang]
    if variant % 3 == 0:
        bullets = "\n".join(f"- {fact}" for fact in facts)
        return f"{lead}\n\n{header}\n{bullets}\n\n{closing}"
    if variant % 3 == 1:
        return f"{lead} " + " ".join(facts) + f" {closing}"
    return f"{lead} " + " ".join(facts[:4]) + f"\n\n{connector}" + " ".join(facts[4:]) + f"\n\n{closing}"


def _pin_groups(data: dict, cell) -> None:
    for site in data["sites"]:
        for area_index, area in enumerate(site["areas"]):
            vlan_id = (
                f"{site['id']}-v{area_index + 1}"
                if cell.vlan_mode == "multi"
                else f"{site['id']}-users"
            )
            for group in area["host_groups"]:
                group["vlan"] = vlan_id


def _set_network(data: dict, cell) -> None:
    if cell.internet != "none":
        data["internet"] = {
            "access": cell.internet,
            "nat": True,
            "public_server": cell.firewall != "none",
        }
    else:
        data.pop("internet", None)
    if cell.firewall != "none":
        firewall = {"enabled": True, "model": "asa5506"}
        if cell.firewall == "asa-dmz":
            firewall["dmz"] = True
        data["firewall"] = firewall
    else:
        data.pop("firewall", None)


def _expected_behaviour(cell, intent: dict) -> str:
    bits = [
        f"{cell.site_count} site(s), {cell.areas_per_site} area(s) per site.",
        f"{cell.vlan_mode} VLANs, {cell.addressing} addressing, {cell.routing} routing.",
    ]
    if cell.internet != "none":
        bits.append(f"{cell.internet} internet" + (" behind an ASA 5506" if cell.firewall != "none" else "") + ".")
    else:
        bits.append("No internet.")
    if cell.firewall == "asa-dmz":
        bits.append("ASA DMZ with a public server.")
    elif cell.firewall == "asa":
        bits.append("ASA perimeter with a public server.")
    if cell.wireless:
        bits.append("Wireless cell(s) for laptops.")
    if cell.iot:
        bits.append("IoT smart LEDs (cabled only; no IP/VLAN placement — separation UNKNOWN, CG-2).")
    if cell.servers:
        bits.append("DNS+HTTP server on the first site.")
    if cell.security_requirements:
        bits.append("Outside-initiated blocking is the one blocked criterion the engine verifies on this perimeter shape.")
    if cell.vlan_mode == "multi":
        bits.append("Inter-VLAN isolation is not emitted by expander v1 (CG-1); do not claim it as verified.")
    return " ".join(bits)


def _engine_pin() -> str | None:
    """Record the engine commit, and say so when src/ is dirty.

    A bare HEAD would imply the record is reproducible from that commit; it is
    not while the Stage 3/4 work is uncommitted, so the dirty marker is part of
    the provenance rather than a footnote.
    """
    try:
        head = subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(REPO), "status", "--porcelain", "--", "src"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return f"{head}+dirty-src" if dirty else head
    except Exception:
        return None


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    cells = {cell.cell_id: cell for cell in coverage_cells()}
    plan = _parse_plan()
    results = []
    failures = []
    for index, row in enumerate(plan):
        cell = cells[row["cell"]]
        rng = random.Random(20261029 + index)
        intent = build_intent(cell, index, rng)
        data = intent.model_dump(mode="json")
        _pin_groups(data, cell)
        _set_network(data, cell)
        # The intent is synthesized from the coverage cell, not extracted from
        # any real source text, and its language must match the rendered prompt.
        data["language"] = row["language"]
        data["source_excerpt"] = None
        prompt = _prompt(cell, SCENARIOS[index % len(SCENARIOS)], index, row["language"])
        record = {
            "id": f"cc-{row['slot'].split('-')[1]}",
            "language": row["language"],
            "difficulty": row["difficulty"],
            "category": "coverage-driven",
            "scenario": f"{SCENARIOS[index % len(SCENARIOS)][0]} — {row['cell']}",
            "coverage_cell": row["cell"],
            "length_category": "long" if cell.areas_per_site >= 3 else "medium",
            "prompt": prompt,
            "expected_intent": data,
            "expect_refusal": False,
            "expected_behaviour": _expected_behaviour(cell, data),
            "why_valuable": f"Fills the certified cell {row['cell']} at the planned language/difficulty.",
            "research_inspiration": [],
            "provenance": {
                "origin": "ai_generated_candidate",
                "author_status": "ai_generated_or_ai_assisted",
                "owner_adoption": "pending",
                "owner_confirmation_required": True,
            },
        }
        check = verify_record(record)
        refusal_evidence = None
        if not check["engine_verified"] and "wan-device-missing" in str(
            check["failure_stage"]
        ):
            # The certified matrix contains multi-site ASA cells, but the
            # expander replaces site1-r1 with the firewall and then cannot
            # build the WAN star. Record the real gap as a refusal, keeping the
            # engine's own failure string as evidence.
            refusal_evidence = check["failure_stage"]
            record["expect_refusal"] = True
            record["expected_intent"] = None
            record["refusal_kind"] = "capability-gap"
            record["expected_behaviour"] = (
                "REFUSE-BY-NAME: a multi-site WAN star with an ASA at the first "
                "site is not a supported shape. The firewall replaces site1-r1, "
                "so the serial WAN has no first-site router and the expander "
                "refuses with 'wan-device-missing'. Offer a single-site ASA "
                "perimeter, or a multi-site WAN without the ASA."
            )
            record["why_valuable"] = (
                f"Records a real matrix/engine mismatch: the certified cell "
                f"{row['cell']} names a multi-site ASA shape the expander cannot "
                "place."
            )
            check = verify_record(record)
        record["prompt_hash"] = check["prompt_hash"]
        record["engine_verified"] = check["engine_verified"]
        record["failure_stage"] = check["failure_stage"]
        record["verified_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        record["engine_commit"] = _engine_pin()
        if refusal_evidence is not None:
            record["refusal_evidence"] = refusal_evidence
        path = OUT / f"{record['id']}.json"
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        results.append({"slot": row["slot"], "candidate": record["id"],
                        "cell": row["cell"], "verified": check["engine_verified"]})
        if not check["engine_verified"]:
            failures.append({"id": record["id"], "stage": check["failure_stage"]})

    manifest = {
        "status": "AI-GENERATED CANDIDATES — NOT OWNER-AUTHORED GOLD",
        "provenance": {
            "origin": "ai_generated_candidate",
            "author_status": "ai_generated_or_ai_assisted",
            "owner_adoption": "pending",
        },
        "slots": len(results),
        "verified": sum(1 for r in results if r["verified"]),
        "failed": len(failures),
        "failures": failures,
        "results": results,
    }
    (HERE / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({k: manifest[k] for k in ("slots", "verified", "failed")}, indent=2))
    if failures:
        for failure in failures:
            print("FAIL", failure["id"], failure["stage"])
        raise SystemExit(1)


if __name__ == "__main__":
    main()
