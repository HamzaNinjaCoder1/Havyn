# Real-world networking research — Stage 5 prompt authoring

Purpose: ground the owner-gold candidate scenarios in how these networks are
actually built and talked about, so prompts read like real requests instead of
textbook exercises. **Research is advisory only.** The Havyn engine is the
source of truth: a scenario is used only in the shape the engine can build or
refuse by name, and anything the engine cannot verify is annotated UNKNOWN in
`expected_behaviour` (see `reports/capability_gap_register.md`).

Provenance: sources below were used to shape scenarios in
`owner-candidates/candidate_data.py`; each candidate records its source in
`research_inspiration`. No source text is copied into prompts — scenarios are
original and every one is engine-checked by `verify_candidates.py`.

## Access log (honest)

- The source URLs in the ledger were accessed during the Stage 5 research
  session; the candidate metadata is the record of use.
- In this writing pass, **three sources were re-fetched and confirmed live**
  (marked ✅ below, with what they actually say).
- The remaining sources are listed as recorded; they were **not re-fetched in
  this pass**, so their summaries below are the topic they informed, not a
  fresh reading. Re-fetch them before quoting.

## ✅ Re-verified sources (fetched this pass)

### netraclos.com — "VLANs and Guest Wi-Fi for Small Business Networks" ✅
Small-business network design, written for owners rather than engineers: a flat
network lets a visitor laptop see the file server and a camera on a default
password reach an accounting workstation; segmentation is "several separate
networks over the same physical switches", each device VLAN looking like its
own network. A typical small office lands on four or five VLANs (staff,
guest-internet-only, cameras/connected equipment, card-handling, sometimes
voice), and guest Wi-Fi needs three things: internet-only reach, client
isolation between visitors, and a bandwidth limit. Firewall rules are expressed
in plain language ("guests reach the internet and nothing else").
→ Informed: **oc-003** (small office guest Wi-Fi split), **oc-032** (same idea
in Roman-Urdu register).

### purple.ai — "WiFi network segmentation: VLANs, SSIDs and guest traffic isolation" ✅
Vendor technical guide: 802.1Q tagging maps SSIDs to distinct Layer-2 domains;
true guest isolation needs **two** layers — AP client isolation plus egress
firewall drop rules for RFC1918 ranges; RADIUS dynamic VLAN assignment (RFC
2868 attributes 64/65/81) avoids SSID sprawl; PCI DSS v4.0 req. 1.2/1.3 and ISO
27001 are cited as the compliance drivers. It also gives a concrete VLAN/subnet
matrix (staff/guest/voice/IoT/management) with per-VLAN DHCP lease lengths.
→ Informed: **oc-013** (guest Wi-Fi + VLAN segmentation request).

### censinet.com — "5 Network Segmentation Strategies for Healthcare IT" ✅
Healthcare segmentation practice: split EHR tiers (web/app/db) with default-deny
between zones; dedicated medical-device/IoMT zones by clinical Risk; vendor
access only through MFA jump hosts with session recording; microsegmentation at
the workload; continuous validation (baseline 30-90 days in monitor mode,
quarterly access recertification). Notes 2025 HHS proposals that would make
segmentation a prescriptive HIPAA requirement.
→ Informed: **oc-053** (clinic/hospital segmentation with clinical devices).

## Recorded source ledger (not re-fetched this pass)

| # | Source | Topic it informed | Candidate(s) |
|---|--------|-------------------|--------------|
| 1 | netraclos.com — VLANs and guest Wi-Fi for small business | Small-business VLAN + guest Wi-Fi split | oc-003, oc-032 |
| 2 | cisco.com CVD *G2 Basics…Feature Services* (voice/IP) | Small business/voice-capable office builds, WAN + feature services | oc-004, oc-014, oc-030, oc-035 |
| 3 | cisco.com *Campus LAN & Wireless LAN Design* (Oct 2015) | Campus/floor VLAN design, campus WLAN architecture | oc-005, oc-043 |
| 4 | ibm.com docs — "Scenario: Basic branch office connection" | Branch-office WAN/connection realism | oc-006 |
| 5 | evergreenhq.com — "How to set up Wi-Fi in your bar or restaurant" | Hospitality Wi-Fi expectations (guest + staff device mix) | oc-008 |
| 6 | cisa.gov — "Enhanced Visibility and Hardening Guidance for Communications Infrastructure" | Perimeter hardening expectations, outside-initiated blocking | oc-010, oc-041 |
| 7 | blogs.cisco.com — "Unpacking IoT… the security challenge" | IoT device security posture / segmenting unpatched devices | oc-012 |
| 8 | purple.ai — WiFi network segmentation guide | Guest/segment VLAN design (verified above) | oc-013 |
| 9 | purple.ai — "How to set up guest Wi-Fi" | Guest Wi-Fi onboarding and access expectations | oc-017 |
| 10 | cisco.com support — DHCP address allocation (27470-100) | DHCP behaviour questions and troubleshooting framing | oc-018 |
| 11 | engineerdirect.co.uk — managed switch VLAN misconfiguration | Real misconfiguration symptoms (uplink/trunk mistakes) | oc-023 |
| 12 | cisco.com — Healthcare secure infrastructure (PDF) | Healthcare network zoning and device separation | oc-025 |
| 13 | cisco.com — WLAN design guide (education, PDF) | Campus/education wireless design and density | oc-026 |
| 14 | security.pditechnologies.com — retail network segmentation | Retail store segmentation (POS vs guest vs cameras) | oc-027 |
| 15 | 2mtechnology.net — UniFi warehouse Wi-Fi design | Warehouse/logistics wireless coverage and roaming | oc-028 |
| 16 | micronet.global — guest Wi-Fi for hotels & restaurants | Guest Wi-Fi scale/onboarding in hospitality | oc-039 |
| 17 | techgeeks.org — IoT isolation homelab VLANs/firewall/mDNS | Hobbyist/small-scale IoT isolation and its limits (mDNS across VLANs) | oc-040 |
| 18 | censinet.com — network segmentation strategies, healthcare | Clinical segmentation (verified above) | oc-053 |
| 19 | ictally.com — restaurant guest Wi-Fi vs POS network | POS/guest separation in a small site | oc-055 |
| 20 | learningnetwork.cisco.com — "DHCP not working after configuring VLANs" | Real DHCP-after-VLAN-change troubleshooting thread | oc-056 |

## What the research changed in the pool

1. **Guest/internet-only scoping is the most common real ask** (sources 1, 8, 9,
   14, 16, 19). That drove a whole family of candidates — and exposed a genuine
   engine gap: the engine cannot deliver or verify "guests cannot reach
   internal systems" (CG-4). Prompts keep the requirement; the refusal cases
   cite `guest-isolation-unsupported`, and buildable ones make no guarantee
   they cannot keep.
2. **Segmentation is normal, blocking is the hard part** (sources 3, 6, 8, 11,
   17, 18): every practitioner source assumes inter-VLAN ACLs; Havyn builds
   separate subnets but has no inter-VLAN ACL path (CG-1). Handled by keeping
   separation requests faithful and marking the blocking clause UNKNOWN.
3. **IoT and cameras are always "on their own segment"** (sources 7, 14, 17):
   the schema can only express `smart_led` and the engine cables IoT without
   IP/VLAN placement (CG-2, CG-7). Prompts were trimmed to expressible kinds;
   what could not be expressed was dropped rather than faked.
4. **Hospitality/retail/healthcare bring compliance language** (sources 12, 14,
   18, 19) — PCI/POS separation, clinical zones, vendor access. This is where
   the "documents written by a real network manager" candidates came from.
5. **Troubleshooting prompts come from real support threads** (sources 11, 20):
   a VLAN change that broke DHCP, an uplink left untagged. The diagnosis-family
   candidates reproduce the *symptom*, not the source's answer.
6. **Multi-site branch realism** (sources 2, 4) confirmed that branch sites
   normally reach the internet through the head end — which is exactly what the
   engine does (single internet block at `sites[0]`, CG-3). The earlier draft
   that invented a second line at the branch office was corrected rather than
   kept as a fake feature.

## Limits

- Research never overrides the engine: where a public source describes a design
  Havyn cannot build or verify, the candidate either refuses by name or carries
  a UNKNOWN annotation — never a claim the engine cannot support.
- Only 3 of the 20 recorded sources were re-fetched in this pass; the rest are
  recorded as used, with topics, and should be re-checked before any quotation.
- Wireless clients per cell are capped by the engine at 8 (CG-9), so several
  research-inspired "big office Wi-Fi" scenarios are modelled as multiple
  cells rather than one large SSID population.
