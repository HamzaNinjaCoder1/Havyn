# Owner-decision recommendations (8 candidates)

> **No owner approval has been recorded.** These are technical
> recommendations. Every record remains an AI-assisted candidate with
> `owner_confirmation_required: true`; none is owner-authored Gold.

| ID | Recommendation | Basis | Engine can verify | Engine cannot verify |
| --- | --- | --- | --- | --- |
| oc-007 | **REFUSAL** | The engine refuses guest_isolation by name (guest-isolation-unsupported). No buildable form delivers the requested guarantee; the only buildable alternative is a plain separate subnet with no isolation promise. | The named refusal fires for a minimal guest_isolation intent (engine probe). The refusal candidate is checked for null intent. | Guest-to-internal blocking; any enforced isolation guarantee. |
| oc-012 | **BUILD** | All four VLANs, static finance, servers and exclusions compile. Finance isolation has no ACL; it is recorded UNKNOWN. Generic reachability criteria no longer attach to the isolation requirement (engine fix R-ENG-001). | Topology compiles; server-reachability criterion for the first host group; generic reachability/links under the engine requirement. | Finance-to-other-group blocking; warehouse-to-intranet audience; DNS existence; static/DHCP configuration; exclusions. |
| oc-013 | **BUILD** | Two sites, three VLANs, OSPF, static production, DNS and WAN compile. Accounts isolation and factory-internet verification are not asserted; they stay UNKNOWN. Server criteria now attach to the DNS service requirement, not the internet requirement. | Topology compiles; cross-site reachability and links under the engine requirement; server reachability to the DNS service requirement. | Accounts isolation; production separation; OSPF configuration; factory internet via HO; DHCP/static; DNS serving both sites; exclusions. |
| oc-028 | **BUILD_AFTER_REWRITE** | The rewritten prompt now uses expressible wireless handheld terminals (modelled as tablets) and models the warehouse system as an HTTP server on the office VLAN. Segmentation, two wireless cells, the WMS server, DHCP and cable internet compile. | Topology compiles; WMS server-reachability criterion on REQ-005; wireless association; generic reachability/links under the engine requirement. | Scanner/forklift device types (schema has no such device); contractor hosts/egress; inter-VLAN blocking; internet-only enforcement. |
| oc-035 | **BUILD** | The three-site static WAN and DHCP shape compiles with no fidelity defect. It shares a topology with oc-030 but uses different counts (10/5/5 versus 8/4/4) and language; this is a dataset-level note, not a technical defect. | Topology compiles; branch-to-HQ reachability and links under the engine requirement. | Internet via HQ; static-routing configuration; DHCP configuration. |
| oc-040 | **BUILD** | Till VLAN, IoT VLAN declaration, six cabled smart_led and DSL compile. IoT has no IP/VLAN placement, so lighting separation stays UNKNOWN. It shares an IoT shape with oc-011, which is a dataset-level note. | Topology compiles; till reachability and link existence under the engine requirement. | IoT IP/VLAN placement; lighting separation from tills; DSL/internet. |
| oc-053 | **BUILD** | Three VLANs, two servers and WAN compile. Auditor isolation has no ACL criterion; the accounting-only WAN scope is not asserted; both stay UNKNOWN. Server criteria attach to the service requirement, not the WAN requirement. | Topology compiles; server reachability to the service requirement; generic reachability/links under the engine requirement. | Auditor isolation; accounting-only WAN scope; DHCP/static; exclusions. |
| oc-054 | **REFUSAL** | The combined request has three independent gaps: guest isolation is refused by name, cameras cannot be expressed (smart_led only), and the engine builds one internet block at sites[0]. No faithful buildable form exists as written. | The named guest-isolation refusal fires; the camera enum and single internet block are engine-probed. | Guest isolation; camera kinds; the store's second internet line; the POS/guest separation guarantee. |

## If approved / if rejected

### oc-007

- **Requirement:** Staff WiFi plus guest WiFi; guests get internet only and must not reach internal machines.
- **If approved:** Remains a refusal candidate; rewrite the prompt in owner words, keep expect_refusal=true and null intent, adopt as gold-007.
- **If rejected:** Would require dropping the isolation guarantee and rewriting the request as a plain-subnet build, or dropping the slot.
- **Provenance:** `ai_assisted_candidate`; owner confirmation required.

### oc-012

- **Requirement:** Four VLANs (sales, warehouse, finance, reception); finance unreachable from other groups; HTTP+DNS servers; DHCP except static finance; no wireless or firewall.
- **If approved:** Stays buildable; rewrite the prompt, keep the UNKNOWN annotation, adopt as gold-012. Do not use it as live isolation proof.
- **If rejected:** Rewriting it to segment-only removes the critical isolation requirement; a refusal would be wrong because the topology builds.
- **Provenance:** `ai_assisted_candidate`; owner confirmation required.

### oc-013

- **Requirement:** Two sites with OSPF; accounts not visible to sales; production separate; internet via head office; DNS; no wireless or firewall.
- **If approved:** Stays buildable; rewrite the prompt, keep the UNKNOWN annotations, adopt as gold-013.
- **If rejected:** A refusal would be wrong; a segment-only rewrite would drop the isolation and internet-via-HO requirements.
- **Provenance:** `ai_assisted_candidate`; owner confirmation required.

### oc-028

- **Requirement:** Warehouse ops wireless handheld terminals on their own segment and able to reach the warehouse system; office PCs/laptops separate; contractors internet-only; DHCP; cable internet.
- **If approved:** Stays buildable; rewrite the prompt in owner words, keep the tablet mapping and contractor-scope note, adopt as gold-028.
- **If rejected:** The old scanner/forklift/WMS prompt would be unrepresentable and should become a refusal or be dropped.
- **Provenance:** `ai_assisted_candidate`; owner confirmation required.

### oc-035

- **Requirement:** Head office plus two branches; static routing; internet via the head office; DHCP at all sites.
- **If approved:** Stays buildable; adopt as gold-035; the duplicate topology is deliberate and recorded.
- **If rejected:** The slot becomes coverage-driven; oc-030 remains the retained topology.
- **Provenance:** `ai_assisted_candidate`; owner confirmation required.

### oc-040

- **Requirement:** Retail shop with till PCs and smart LED lighting on its own network, wired in, DSL internet.
- **If approved:** Stays buildable; adopt as gold-040 with the UNKNOWN annotation.
- **If rejected:** The slot becomes coverage-driven; oc-011 remains the retained smart-LED case.
- **Provenance:** `ai_assisted_candidate`; owner confirmation required.

### oc-053

- **Requirement:** Accounting firm; auditors isolated from accounting machines and servers; ledger HTTP and DNS servers; DHCP for accountants/reception, static servers; WAN for the accounting side only; no wireless or public kiosks.
- **If approved:** Stays buildable; adopt as gold-053 with both UNKNOWN annotations.
- **If rejected:** The slot becomes coverage-driven; refusal would be wrong because the topology and servers build.
- **Provenance:** `ai_assisted_candidate`; owner confirmation required.

### oc-054

- **Requirement:** Two-site retail: POS isolated and static, IP cameras on their own network, guest WiFi internet-only, static routing between sites, internet at both sites with a separate store line.
- **If approved:** Remains a refusal; rewrite the prompt in owner words, keep null intent, adopt as gold-054.
- **If rejected:** Splitting into a buildable subset would drop three explicit requirements; it is a different request.
- **Provenance:** `ai_assisted_candidate`; owner confirmation required.
