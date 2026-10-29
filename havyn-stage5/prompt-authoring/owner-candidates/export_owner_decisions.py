"""Export the technical decision record for the 8 owner-decision candidates.

This does NOT record owner approval. It records the technical recommendation,
its basis, what the engine can and cannot verify, and the adoption gate.
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "reports"

DECISIONS = [
    {
        "id": "oc-007",
        "requirement": "Staff WiFi plus guest WiFi; guests get internet only and must not reach internal machines.",
        "technical_recommendation": "REFUSAL",
        "decision_basis": "The engine refuses guest_isolation by name (guest-isolation-unsupported). No buildable form delivers the requested guarantee; the only buildable alternative is a plain separate subnet with no isolation promise.",
        "engine_can_verify": "The named refusal fires for a minimal guest_isolation intent (engine probe). The refusal candidate is checked for null intent.",
        "engine_cannot_verify": "Guest-to-internal blocking; any enforced isolation guarantee.",
        "provenance_status": "ai_assisted_candidate",
        "owner_confirmation_required": True,
        "if_approved": "Remains a refusal candidate; rewrite the prompt in owner words, keep expect_refusal=true and null intent, adopt as gold-007.",
        "if_rejected": "Would require dropping the isolation guarantee and rewriting the request as a plain-subnet build, or dropping the slot.",
    },
    {
        "id": "oc-012",
        "requirement": "Four VLANs (sales, warehouse, finance, reception); finance unreachable from other groups; HTTP+DNS servers; DHCP except static finance; no wireless or firewall.",
        "technical_recommendation": "BUILD",
        "decision_basis": "All four VLANs, static finance, servers and exclusions compile. Finance isolation has no ACL; it is recorded UNKNOWN. Generic reachability criteria no longer attach to the isolation requirement (engine fix R-ENG-001).",
        "engine_can_verify": "Topology compiles; server-reachability criterion for the first host group; generic reachability/links under the engine requirement.",
        "engine_cannot_verify": "Finance-to-other-group blocking; warehouse-to-intranet audience; DNS existence; static/DHCP configuration; exclusions.",
        "provenance_status": "ai_assisted_candidate",
        "owner_confirmation_required": True,
        "if_approved": "Stays buildable; rewrite the prompt, keep the UNKNOWN annotation, adopt as gold-012. Do not use it as live isolation proof.",
        "if_rejected": "Rewriting it to segment-only removes the critical isolation requirement; a refusal would be wrong because the topology builds.",
    },
    {
        "id": "oc-013",
        "requirement": "Two sites with OSPF; accounts not visible to sales; production separate; internet via head office; DNS; no wireless or firewall.",
        "technical_recommendation": "BUILD",
        "decision_basis": "Two sites, three VLANs, OSPF, static production, DNS and WAN compile. Accounts isolation and factory-internet verification are not asserted; they stay UNKNOWN. Server criteria now attach to the DNS service requirement, not the internet requirement.",
        "engine_can_verify": "Topology compiles; cross-site reachability and links under the engine requirement; server reachability to the DNS service requirement.",
        "engine_cannot_verify": "Accounts isolation; production separation; OSPF configuration; factory internet via HO; DHCP/static; DNS serving both sites; exclusions.",
        "provenance_status": "ai_assisted_candidate",
        "owner_confirmation_required": True,
        "if_approved": "Stays buildable; rewrite the prompt, keep the UNKNOWN annotations, adopt as gold-013.",
        "if_rejected": "A refusal would be wrong; a segment-only rewrite would drop the isolation and internet-via-HO requirements.",
    },
    {
        "id": "oc-028",
        "requirement": "Warehouse ops wireless handheld terminals on their own segment and able to reach the warehouse system; office PCs/laptops separate; contractors internet-only; DHCP; cable internet.",
        "technical_recommendation": "BUILD_AFTER_REWRITE",
        "decision_basis": "The rewritten prompt now uses expressible wireless handheld terminals (modelled as tablets) and models the warehouse system as an HTTP server on the office VLAN. Segmentation, two wireless cells, the WMS server, DHCP and cable internet compile.",
        "engine_can_verify": "Topology compiles; WMS server-reachability criterion on REQ-005; wireless association; generic reachability/links under the engine requirement.",
        "engine_cannot_verify": "Scanner/forklift device types (schema has no such device); contractor hosts/egress; inter-VLAN blocking; internet-only enforcement.",
        "provenance_status": "ai_assisted_candidate",
        "owner_confirmation_required": True,
        "if_approved": "Stays buildable; rewrite the prompt in owner words, keep the tablet mapping and contractor-scope note, adopt as gold-028.",
        "if_rejected": "The old scanner/forklift/WMS prompt would be unrepresentable and should become a refusal or be dropped.",
    },
    {
        "id": "oc-035",
        "requirement": "Head office plus two branches; static routing; internet via the head office; DHCP at all sites.",
        "technical_recommendation": "BUILD",
        "decision_basis": "The three-site static WAN and DHCP shape compiles with no fidelity defect. It shares a topology with oc-030 but uses different counts (10/5/5 versus 8/4/4) and language; this is a dataset-level note, not a technical defect.",
        "engine_can_verify": "Topology compiles; branch-to-HQ reachability and links under the engine requirement.",
        "engine_cannot_verify": "Internet via HQ; static-routing configuration; DHCP configuration.",
        "provenance_status": "ai_assisted_candidate",
        "owner_confirmation_required": True,
        "if_approved": "Stays buildable; adopt as gold-035; the duplicate topology is deliberate and recorded.",
        "if_rejected": "The slot becomes coverage-driven; oc-030 remains the retained topology.",
    },
    {
        "id": "oc-040",
        "requirement": "Retail shop with till PCs and smart LED lighting on its own network, wired in, DSL internet.",
        "technical_recommendation": "BUILD",
        "decision_basis": "Till VLAN, IoT VLAN declaration, six cabled smart_led and DSL compile. IoT has no IP/VLAN placement, so lighting separation stays UNKNOWN. It shares an IoT shape with oc-011, which is a dataset-level note.",
        "engine_can_verify": "Topology compiles; till reachability and link existence under the engine requirement.",
        "engine_cannot_verify": "IoT IP/VLAN placement; lighting separation from tills; DSL/internet.",
        "provenance_status": "ai_assisted_candidate",
        "owner_confirmation_required": True,
        "if_approved": "Stays buildable; adopt as gold-040 with the UNKNOWN annotation.",
        "if_rejected": "The slot becomes coverage-driven; oc-011 remains the retained smart-LED case.",
    },
    {
        "id": "oc-053",
        "requirement": "Accounting firm; auditors isolated from accounting machines and servers; ledger HTTP and DNS servers; DHCP for accountants/reception, static servers; WAN for the accounting side only; no wireless or public kiosks.",
        "technical_recommendation": "BUILD",
        "decision_basis": "Three VLANs, two servers and WAN compile. Auditor isolation has no ACL criterion; the accounting-only WAN scope is not asserted; both stay UNKNOWN. Server criteria attach to the service requirement, not the WAN requirement.",
        "engine_can_verify": "Topology compiles; server reachability to the service requirement; generic reachability/links under the engine requirement.",
        "engine_cannot_verify": "Auditor isolation; accounting-only WAN scope; DHCP/static; exclusions.",
        "provenance_status": "ai_assisted_candidate",
        "owner_confirmation_required": True,
        "if_approved": "Stays buildable; adopt as gold-053 with both UNKNOWN annotations.",
        "if_rejected": "The slot becomes coverage-driven; refusal would be wrong because the topology and servers build.",
    },
    {
        "id": "oc-054",
        "requirement": "Two-site retail: POS isolated and static, IP cameras on their own network, guest WiFi internet-only, static routing between sites, internet at both sites with a separate store line.",
        "technical_recommendation": "REFUSAL",
        "decision_basis": "The combined request has three independent gaps: guest isolation is refused by name, cameras cannot be expressed (smart_led only), and the engine builds one internet block at sites[0]. No faithful buildable form exists as written.",
        "engine_can_verify": "The named guest-isolation refusal fires; the camera enum and single internet block are engine-probed.",
        "engine_cannot_verify": "Guest isolation; camera kinds; the store's second internet line; the POS/guest separation guarantee.",
        "provenance_status": "ai_assisted_candidate",
        "owner_confirmation_required": True,
        "if_approved": "Remains a refusal; rewrite the prompt in owner words, keep null intent, adopt as gold-054.",
        "if_rejected": "Splitting into a buildable subset would drop three explicit requirements; it is a different request.",
    },
]


def main() -> None:
    payload = {
        "status": "TECHNICAL RECOMMENDATIONS — owner approval has NOT been recorded",
        "candidate_revision": "working tree after the Stage-5 engine criterion fix and the 8 technical decisions",
        "engine_commit_at_decision": "b69be22d0118647691d189da89624ac8edfef004 (working tree not yet committed)",
        "provenance_rule": "All eight remain ai_assisted_candidate; none is owner-authored Gold. owner_confirmation_required is true for every row.",
        "decisions": DECISIONS,
        "summary": {
            "total": len(DECISIONS),
            "build": sum(1 for d in DECISIONS if d["technical_recommendation"].startswith("BUILD")),
            "refusal": sum(1 for d in DECISIONS if d["technical_recommendation"] == "REFUSAL"),
            "owner_approved": 0,
        },
    }
    (OUT / "owner_decisions.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    lines = [
        "# Owner-decision recommendations (8 candidates)",
        "",
        "> **No owner approval has been recorded.** These are technical",
        "> recommendations. Every record remains an AI-assisted candidate with",
        "> `owner_confirmation_required: true`; none is owner-authored Gold.",
        "",
        "| ID | Recommendation | Basis | Engine can verify | Engine cannot verify |",
        "| --- | --- | --- | --- | --- |",
    ]
    for d in DECISIONS:
        lines.append(
            "| {id} | **{rec}** | {basis} | {can} | {cannot} |".format(
                id=d["id"],
                rec=d["technical_recommendation"],
                basis=d["decision_basis"],
                can=d["engine_can_verify"],
                cannot=d["engine_cannot_verify"],
            )
        )
    lines += ["", "## If approved / if rejected", ""]
    for d in DECISIONS:
        lines += [
            f"### {d['id']}",
            "",
            f"- **Requirement:** {d['requirement']}",
            f"- **If approved:** {d['if_approved']}",
            f"- **If rejected:** {d['if_rejected']}",
            f"- **Provenance:** `{d['provenance_status']}`; owner confirmation required.",
            "",
        ]
    (OUT / "owner_decisions.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(payload["summary"], indent=2))


if __name__ == "__main__":
    main()
