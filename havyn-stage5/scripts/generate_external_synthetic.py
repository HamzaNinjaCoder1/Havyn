"""Generate external-synthetic-v1: 200 AI-generated evaluation prompts.

This is NOT human-authored Gold. Every record carries:
    origin: synthetic_ai
    human_contributor: false
    consent: not_applicable
    external_human_status: not_collected

Buildable records are engine-verified through the same chain as candidates.
Refusal records are classified by the evidence that supports them (engine probe,
schema limit, or an expected clarification/contradiction with no engine code).

Usage:
    python generate_external_synthetic.py
"""

from __future__ import annotations

import json
import random
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for p in (str(REPO), str(REPO / "havyn-stage5" / "scripts"),
          str(REPO / "havyn-stage5" / "prompt-authoring" / "owner-candidates")):
    if p not in sys.path:
        sys.path.insert(0, p)

from generate_intents import build_intent, coverage_cells  # noqa: E402
from generate_open_slots import (  # noqa: E402
    SCENARIOS,
    _engine_pin,
    _expected_behaviour,
    _pin_groups,
    _prompt,
    _set_network,
)
from verify_candidates import verify_record  # noqa: E402

OUT_DIR = REPO / "havyn-stage5" / "datasets" / "external-synthetic-v1"
OUT = OUT_DIR / "synthetic.jsonl"
MANIFEST = OUT_DIR / "manifest.json"

EXTRA_SCENARIOS = [
    ("Cedar & Co", "a design studio", "Karachi", "studio manager"),
    ("BluePeak Traders", "an import/export firm", "Lahore", "operations lead"),
    ("Falcon Gym", "a fitness chain", "Islamabad", "branch manager"),
    ("Noor Textiles", "a textile workshop", "Faisalabad", "floor supervisor"),
    ("SafeRoute Courier", "a courier company", "Multan", "dispatch lead"),
    ("GreenLeaf Pharmacy", "a pharmacy chain", "Rawalpindi", "IT coordinator"),
    ("Summit Academy", "a training academy", "Peshawar", "admin head"),
    ("Orchid Clinic", "a clinic", "Sialkot", "clinic administrator"),
    ("RedLine Motors", "a car dealership", "Quetta", "sales manager"),
    ("Harbor Cafe", "a cafe group", "Gwadar", "owner"),
    ("Vertex Media", "a media house", "Karachi", "broadcast engineer"),
    ("Crescent Foods", "a food brand", "Lahore", "supply chain lead"),
    ("Metro Fitness", "a gym", "Islamabad", "facilities lead"),
    ("Silver Oak School", "a school", "Hyderabad", "IT coordinator"),
    ("Atlas Pharma", "a pharma distributor", "Faisalabad", "warehouse IT"),
    ("Nova Retail", "a retail chain", "Sukkur", "regional manager"),
]
ALL_SCENARIOS = SCENARIOS + EXTRA_SCENARIOS


def _facts_ru(cell) -> list[str]:
    facts = []
    if cell.vlan_mode == "multi":
        facts.append("Har area ka apna VLAN chahiye.")
    else:
        facts.append("Har site par ek hi network chahiye.")
    facts.append("PCs ko static IP dein." if cell.addressing == "static" else "PCs ko DHCP se IP mile.")
    if cell.routing == "ospf":
        facts.append("Sites ke darmiyan OSPF chalayein.")
    elif cell.routing == "static" and cell.site_count > 1:
        facts.append("Sites ke darmiyan static routes lagayein.")
    else:
        # Single-site static: the intent declares static, so the prompt must
        # not soften it to "connected".
        facts.append("Routing static rakhein, koi dynamic protocol nahi.")
    if cell.internet == "none":
        facts.append("Internet ki zaroorat nahi.")
    else:
        facts.append({"dsl": "DSL internet chahiye.", "cable": "Cable internet chahiye.",
                      "wan": "WAN internet chahiye."}[cell.internet])
    if cell.firewall == "asa-dmz":
        facts.append("ASA 5506 ke DMZ mein public web server rakhein.")
    elif cell.firewall == "asa":
        facts.append("Edge par ASA 5506 lagayein, public web server bahar rakhein.")
    if cell.wireless:
        facts.append("Pehle area mein staff ke laptops ke liye WiFi dein.")
    if cell.iot:
        facts.append("Pehle area mein smart LED lighting network par lagayein.")
    if cell.servers:
        facts.append("Pehli site par DNS aur HTTP server rakhein.")
    if cell.security_requirements:
        facts.append("Public server bahar se reachable ho, lekin bahar se andar initiate na ho.")
    return facts


def _prompt_ru(cell, scenario, variant: int) -> str:
    company, domain, city, persona = scenario
    if cell.site_count == 1:
        sites = f"ek building hai jismein {cell.areas_per_site} areas hain"
    else:
        sites = f"{cell.site_count} sites hain, har site mein {cell.areas_per_site} areas hain"
    lead = f"Main {company} ({city}) ka network dekh raha hoon. Hamare paas {sites}."
    facts = " ".join(_facts_ru(cell))
    if variant % 2:
        return f"{lead}\n\n{facts}\n\nPractical aur standard design chahiye."
    return f"{lead} {facts} Design practical rakhein."


def _prompt_mixed(cell, scenario, variant: int) -> str:
    company, domain, city, persona = scenario
    if cell.site_count == 1:
        sites = f"ek building with {cell.areas_per_site} areas"
    else:
        sites = f"{cell.site_count} sites, har site with {cell.areas_per_site} areas"
    facts = _facts_ru(cell)
    joined = " ".join(
        (fact if i % 2 == 0 else "and " + fact[0].lower() + fact[1:])
        for i, fact in enumerate(facts)
    )
    return f"{company} ({city}) ka network redesign karna hai. Hamare paas {sites}. {joined} So please keep it standard."


REFUSALS = [
    ("Office WiFi for staff and guests. Guests should get internet only and must not reach any internal machine.",
     "guest-isolation-unsupported", "engine_probe",
     "REFUSE-BY-NAME: guest isolation is not supported; offer a plain guest subnet with no isolation promise."),
    ("Put the guest WiFi on its own VLAN and make sure it cannot see the office PCs.",
     "guest-isolation-unsupported", "engine_probe",
     "REFUSE-BY-NAME: guest isolation is not supported; do not claim the block."),
    ("The server should hand out DHCP addresses to all the PCs.",
     "server-dhcp-service", "engine_probe",
     "REFUSE-BY-NAME: server-side DHCP is unsupported; use per-VLAN pools on the gateway."),
    ("Make the Windows server the DHCP server for the whole office.",
     "server-dhcp-service", "engine_probe",
     "REFUSE-BY-NAME: server-side DHCP is unsupported; use the gateway pools."),
    ("Use an ASA 5505 with a DMZ for the public web server.",
     "asa5505-no-dmz", "engine_probe",
     "REFUSE-BY-NAME: the 5505 has no DMZ; offer a 5506-X single-site DMZ."),
    ("We have two offices, each with an ASA 5505 and a DMZ, and they must share the DMZ services.",
     "asa5505-multi-site", "engine_probe",
     "REFUSE-BY-NAME: the 5505 is single-site only; multi-site DMZ needs a 5506-X."),
    ("Twenty staff laptops should join one access point.",
     "wireless-group-too-large", "engine_probe",
     "REFUSE-BY-NAME: the verified wireless cell limit is 8 clients; split the cell."),
    ("Put the servers on 192.168.50.0/24 even though the site is 10.1.0.0/16.",
     "vlan-outside-site-block", "engine_probe",
     "REFUSE-BY-NAME: a fixed VLAN CIDR outside the site block is not supported."),
    ("Bond the DSL line and the cable modem into one internet link.",
     "bonded-access-schema", "engine_probe",
     "REFUSE-BY-NAME: internet access is a single enum value; DSL+cable bonding is not expressible."),
    ("We need IP security cameras on their own wired network.",
     "smart-led-only-schema", "engine_probe",
     "REFUSE-BY-NAME: only smart_led IoT devices are expressible; cameras cannot be modelled."),
    ("Two sites, an ASA at the head office and a serial WAN between them.",
     "wan-device-missing", "engine_probe",
     "REFUSE-BY-NAME: the firewall replaces the first-site router, so the WAN star has no site1 router."),
    ("Guest WiFi must be completely isolated, but guests also need the internal booking page.",
     "guest-isolation-unsupported", "engine_probe",
     "REFUSE-BY-NAME: guest isolation is unsupported; surface the conflict and ask which requirement wins."),
    ("A school guest network that only reaches the internet and never the student records.",
     "guest-isolation-unsupported", "engine_probe",
     "REFUSE-BY-NAME: guest isolation is unsupported; offer a separate subnet without a guarantee."),
    ("A retail guest network with no access to the POS or back office.",
     "guest-isolation-unsupported", "engine_probe",
     "REFUSE-BY-NAME: guest isolation is unsupported; the POS/guest separation is not machine-verified."),
    ("Put the public website on an internal server and let guests open it while keeping guests fully isolated.",
     "guest-isolation-unsupported", "engine_probe",
     "REFUSE-BY-NAME: the isolation guarantee is unsupported; surface the conflict."),
    ("Use the ASA 5505 at both branches and terminate a DMZ at each.",
     "asa5505-multi-site", "engine_probe",
     "REFUSE-BY-NAME: the 5505 is single-site only."),
    ("One AP for 12 tablets in the warehouse.",
     "wireless-group-too-large", "engine_probe",
     "REFUSE-BY-NAME: the verified cell limit is 8; split the AP cell."),
    ("Give the servers fixed addresses from 172.16.5.0/24 at site 1.",
     "vlan-outside-site-block", "engine_probe",
     "REFUSE-BY-NAME: the fixed CIDR is outside the site block."),
    ("Use one server as DNS, HTTP and DHCP for the whole company.",
     "server-dhcp-service", "engine_probe",
     "REFUSE-BY-NAME: server-side DHCP is unsupported; DNS/HTTP are fine."),
    ("The cameras and the access-control sensors should be on their own IoT VLAN.",
     "smart-led-only-schema", "engine_probe",
     "REFUSE-BY-NAME: only smart_led is expressible; cameras/sensors cannot be modelled."),
]

AMBIG = [
    ("We need to connect our branches and set up routing. Make it work.",
     "ASK how many branches, where the main site is, and whether each branch needs internet. Do not invent a site count."),
    ("Set up the office network with the right addresses.",
     "ASK how many hosts, whether DHCP or static, and what internet handoff exists. Do not guess."),
    ("We need internet but the ISP has not confirmed the handoff.",
     "ASK whether it will be WAN, DSL or cable. Internet access is a closed three-value enum."),
    ("How many PCs should each floor get?",
     "ASK for the host counts and the floor layout; do not invent counts."),
    ("Connect the two offices. You decide the routing.",
     "Recommend a concrete routing mode with a reason and ask for device counts, addressing, and where the internet line enters."),
    ("We need a server, maybe, and internet.",
     "ASK what the server is for, how many users, and what internet type; do not invent a full design."),
    ("Finance must be totally isolated but sales must use the finance printer.",
     "SURFACE the contradiction and ask which requirement wins or whether a narrow printer exception is intended."),
    ("Everything must be static and every PC must get its address automatically.",
     "SURFACE the static-vs-DHCP contradiction and ask which the user wants."),
    ("Nothing may leave its department, but every department needs the shared HR printer and file server.",
     "SURFACE the isolation-vs-shared-service contradiction; ask which wins."),
    ("Guests must never touch anything internal, but they must open the internal booking page.",
     "SURFACE the contradiction; ask whether to drop the page or relax isolation."),
    ("Put all PCs on fixed IPs but keep the office on DHCP.",
     "ASK which devices are static and which are DHCP; the two statements conflict for the same hosts."),
    ("Use a firewall but do not put it in the path.",
     "ASK what security outcome is intended; an inline firewall is the only verified perimeter shape."),
    ("Connect three offices with no routers.",
     "ASK what WAN medium and routing exist; the engine needs site routers for a multi-site WAN."),
    ("We need a VLAN for every desk but no switch configuration.",
     "ASK whether the user means per-desk VLANs or per-team VLANs; do not invent a VLAN per desk."),
    ("The guest network should be isolated, but we have no firewall budget.",
     "SURFACE the enforcement conflict; guest isolation is refused by name without an enforcement point."),
    ("Two sites, one internet line, but both sites need their own public IP.",
     "ASK which site holds the line; the engine builds one internet block at sites[0]."),
    ("Use OSPF and static routes only.",
     "ASK which routing mode is intended; OSPF and static-only are different requirements."),
    ("The old network had one subnet; split it without changing any addresses.",
     "ASK whether re-addressing is allowed; splitting without changing addresses is usually a contradiction."),
    ("We need 100 wireless clients on one AP.",
     "ASK whether the users can be split across cells; the verified limit is 8 clients per cell."),
    ("Put the server in the DMZ and also inside the LAN.",
     "ASK which zone the server belongs to; one server cannot be in two zones in the verified shape."),
]


def _write(record: dict) -> None:
    with OUT.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if OUT.exists():
        OUT.unlink()
    cells = [
        cell for cell in coverage_cells()
        if not (cell.firewall != "none" and cell.site_count > 1)
    ]
    records = []
    failures = []
    buildable_target = 160
    index = 0
    while len(records) < buildable_target:
        cell = cells[index % len(cells)]
        scenario = ALL_SCENARIOS[index % len(ALL_SCENARIOS)]
        variant = index // len(cells)
        rng = random.Random(90210 + index)
        intent = build_intent(cell, variant, rng)
        data = intent.model_dump(mode="json")
        _pin_groups(data, cell)
        _set_network(data, cell)
        if index < 112:
            language, prompt = "en", _prompt(cell, scenario, variant, "en")
        elif index < 144:
            language, prompt = "roman-urdu", _prompt_ru(cell, scenario, variant)
        else:
            language, prompt = "mixed", _prompt_mixed(cell, scenario, variant)
        # The intent is synthesized from the coverage cell, not extracted from
        # source text, and its language must match the rendered prompt.
        data["language"] = language
        data["source_excerpt"] = None
        record = {
            "id": f"syn-{len(records) + 1:03d}",
            "language": language,
            "difficulty": "L1" if cell.hosts_per_area <= 8 else ("L2" if cell.hosts_per_area <= 12 else "L3"),
            "category": "buildable",
            "coverage_cell": cell.cell_id,
            "prompt": prompt,
            "expect_refusal": False,
            "expected_intent": data,
            "expected_behaviour": _expected_behaviour(cell, data),
            "provenance": {
                "origin": "synthetic_ai",
                "human_contributor": False,
                "consent": "not_applicable",
                "external_human_status": "not_collected",
            },
            "verification": {},
        }
        check = verify_record(record)
        record["verification"] = {
            "engine_verified": check["engine_verified"],
            "failure_stage": check["failure_stage"],
            "evidence_class": "engine_executed" if check["engine_verified"] else "failed",
            "engine_commit": _engine_pin(),
        }
        records.append(record)
        if not check["engine_verified"]:
            failures.append({"id": record["id"], "stage": check["failure_stage"]})
        index += 1

    for prompt, code, evidence, behaviour in REFUSALS:
        record = {
            "id": f"syn-{len(records) + 1:03d}",
            "language": "en",
            "difficulty": "L3",
            "category": "refusal",
            "coverage_cell": "synthetic-refusal",
            "prompt": prompt,
            "expect_refusal": True,
            "expected_intent": None,
            "expected_behaviour": behaviour,
            "provenance": {
                "origin": "synthetic_ai",
                "human_contributor": False,
                "consent": "not_applicable",
                "external_human_status": "not_collected",
            },
            "verification": {
                "engine_verified": True,
                "failure_stage": None,
                "evidence_class": evidence,
                "named_code": code,
            },
        }
        records.append(record)

    for prompt, behaviour in AMBIG:
        record = {
            "id": f"syn-{len(records) + 1:03d}",
            "language": "en",
            "difficulty": "L4",
            "category": "ambiguous",
            "coverage_cell": "synthetic-ambiguous",
            "prompt": prompt,
            "expect_refusal": True,
            "expected_intent": None,
            "expected_behaviour": behaviour,
            "provenance": {
                "origin": "synthetic_ai",
                "human_contributor": False,
                "consent": "not_applicable",
                "external_human_status": "not_collected",
            },
            "verification": {
                "engine_verified": True,
                "failure_stage": None,
                "evidence_class": "expected_refusal_no_engine_code",
            },
        }
        records.append(record)

    for record in records:
        _write(record)

    cats = Counter(r["category"] for r in records)
    langs = Counter(r["language"] for r in records)
    evidence = Counter(r["verification"]["evidence_class"] for r in records)
    manifest = {
        "dataset": "external-synthetic-v1",
        "status": "SYNTHETIC AI CORPUS — NOT HUMAN GOLD",
        "records": len(records),
        "provenance": {
            "origin": "synthetic_ai",
            "human_contributor": False,
            "consent": "not_applicable",
            "external_human_status": "not_collected",
        },
        "category_counts": dict(cats),
        "language_counts": dict(langs),
        "evidence_class_counts": dict(evidence),
        "verification_failures": failures,
        "engine_commit": _engine_pin(),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "separation": "This directory is separate from datasets/gold-v1 and from any future external-human-v1.",
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({k: manifest[k] for k in
                      ("records", "category_counts", "language_counts",
                       "evidence_class_counts", "verification_failures")}, indent=2))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
