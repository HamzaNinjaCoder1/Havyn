# Havyn Stage 4 scorecard

_generated 2026-10-24T13:55:06+00:00_

**Stage 4 completion: 93.3%** (verified acceptance, not code presence) · tasks implemented **12/12** · live-verified tasks **10/12**

## Required metrics

| Metric | Measured | Target |
| --- | --- | --- |
| Stage 4 tasks | 12/12 implemented | 12/12 |
| Root-cause accuracy | 100.0% | ≥90% |
| Incorrect applied fixes | 0 | 0 |
| Original criteria after enhancement | True | 100% |
| Networks enhanced | 6 simulated + 1 live | ≥5 |
| Mandatory backups | verified on every live repair; backup failure blocks offline | 100% |
| Rollback tests | live pass | Passing |
| Round-trip coverage | 16/16 | All compatible |
| Regression failures | 0 | 0 |
| Selected live regression tests | 20 passed (of 84 collected) | Environment-dependent |

## Tasks

| Task | Status | Acceptance | Offline | Live | Evidence | Limitations |
| --- | --- | --- | --- | --- | --- | --- |
| S4.1 Existing network import | **LIVE VERIFIED** | Library networks import with complete inventories of accessible facts | importer tests + 16 library simulations | live import: {'config_status': {'known': 2, 'not_read': 2}, 'devices': 4, 'incomplete': 2, 'links': 3} | `tests/autopilot/test_s4_importer.py`<br>`probes/raw/stage4-import.json`<br>`probes/raw/stage4-live-arp.json` | Linksys has no configuration read-back; hosts expose only ipconfig/ARP. |
| S4.2 IOS running-configuration reader | **LIVE VERIFIED** | Every accessible IOS device yields its real configuration with evidence | config-reader tests, retries and raw/hash evidence | live IOS router read + live ASA read | `tests/autopilot/test_s4_importer.py`<br>`probes/raw/stage4-running-s4l-r1.txt`<br>`probes/raw/stage4-running-s4l-s1.txt`<br>`probes/raw/stage4-live-asa.json` | Linksys reports UNSUPPORTED by design. ASA read is verified; ASA operational tables remain limited by its CLI. |
| S4.3 Structured IOS configuration parser | **LIVE VERIFIED** | Configurations parse to typed structures with unparsed statements reported | 6 parser tests; compiled library configs parse with zero unparsed | real router/switch/ASA configs parsed | `tests/autopilot/test_s4_parser.py`<br>`probes/raw/stage4-diagnosis.json` | Statements outside the model are preserved verbatim and listed. |
| S4.4 As-is intent builder + round trip | **OFFLINE VERIFIED** | Supported semantics survive compiler expansion; unsupported semantics documented | 16/16 library specs equivalent; 5 exact | as-is intent built during live import/enhancement | `docs/reports/stage4-roundtrip.json`<br>`tests/autopilot/test_s4_intent.py` | Asymmetric per-side trunk allowed lists are not representable; the current compiler does not emit L3-switch routed-port addresses/static routes (documented as unsupported, not claimed equal). |
| S4.5 Objective identification and confirmation | **LIVE VERIFIED** | Every actionable diagnosis references a confirmed objective | explicit/inferred tests; confirmation gate enforced | explicit objectives driven live diagnosis and repair | `tests/autopilot/test_s4_tracer_faults.py`<br>`probes/raw/stage4-repair.json` | Requirement-only statements with no probeable criterion stay non-actionable. |
| S4.6 Hop-by-hop path tracer | **LIVE VERIFIED WITH LIMITATIONS** | First observable failing hop with evidence; confirmed vs suspected | tracer tests; first-hop assertions | live routing, ACL, ARP, STP and NAT observations | `tests/autopilot/test_s4_tracer_faults.py`<br>`probes/raw/stage4-live-arp.json`<br>`probes/raw/stage4-live-stp.json`<br>`probes/raw/stage4-live-nat.json` | ARP is observed (host cache) but not used as a fault rule; NAT translations are observed but NAT is not yet a path-decision rule; a full ARP table is not exposed on every platform. |
| S4.7 Layer-ordered fault detection | **LIVE VERIFIED WITH LIMITATIONS** | Every implemented rule detects its injected fault | 96 scenarios, 96 correct | first live run 5/7; ACL/DNS reruns after fixes: 2/2; multi-fault True | `docs/reports/stage4-benchmark.json`<br>`probes/raw/stage4-live-faults.json`<br>`tests/autopilot/test_s4_tracer_faults.py` | DHCP lease failure is not live-injected; NAT and ARP are observed but not used as fault classes; STP blocking is a fault only when no alternate path exists. The first live multi-class run scored 5/7; ACL/DNS were rerun after fixes and both passed (see stage4-live-acl-dns.json). |
| S4.8 Standardized diagnosis report | **LIVE VERIFIED** | One validated schema; UNKNOWN when evidence is insufficient | schema validation + unknown tests | live diagnosis JSON/Markdown reports | `probes/raw/stage4-diagnosis.md`<br>`probes/raw/stage4-diagnosis.json` | UNKNOWN is preserved and blocks repair. |
| S4.9 Mandatory pre-fix backup | **LIVE VERIFIED** | No repair begins without a verified backup | backup create/validate/tamper tests | live repair backup verified | `tests/autopilot/test_s4_repair.py`<br>`probes/raw/stage4-repair.json` | A device whose configuration is unreadable blocks a config repair by design. |
| S4.10 Minimal repair, verification and rollback | **LIVE VERIFIED** | Correct repair succeeds; wrong repair rolls back; no unsafe fix remains | 5 repair tests including rollback/approval/backup failure | live repair objective pass; live wrong repair rolled back and verified against backup | `tests/autopilot/test_s4_repair.py`<br>`probes/raw/stage4-repair.json`<br>`probes/raw/stage4-live-rollback.json` | Approval uses the existing SafetyGate; a denied rollback is reported incomplete. |
| S4.11 Safe existing-network enhancement | **LIVE VERIFIED** | At least 5 networks enhanced; previously passing criteria preserved | 6/7 simulated networks; preserved=True | live enhancement: original and new flows reachable | `docs/reports/stage4-enhancement.json`<br>`probes/raw/stage4-live-enhance.json` | Automatic removal is refused without an explicit removal plan. |
| S4.12 Fault-injection benchmark | **OFFLINE VERIFIED** | ≥90% root-cause accuracy, 0 incorrect fixes, enhancement preservation | 96/96 (100.0%), incorrect=0 | separate live fault suite (see S4.7), not merged into the accuracy score | `docs/reports/stage4-benchmark.md`<br>`docs/reports/stage4-benchmark.json` | The 96-scenario benchmark is deterministic simulation built by the real compiler, not live PT injection; live evidence is reported separately and partially covers the classes. |

## Known limitations

- The 96-scenario benchmark is deterministic simulation over the real compiler; it is not live PT injection.
- Live fault coverage is a subset: DHCP lease failure is not live-injected; ARP/NAT are observed but not diagnostic fault classes.
- The current compiler does not emit L3-switch routed-port addresses/static routes; the round trip reports these as unsupported, not equivalent.
- A NetworkSpec link stores one trunk allowed list; asymmetric per-side lists are reported as unsupported.
- Linksys exposes no running-config read-back and is UNSUPPORTED for configuration extraction.
- ASA operational tables are constrained by its CLI; the ASA running-config read itself is live verified.
- STP blocking is only a fault when no alternate forwarding path exists.
- The full 84-test live suite was not completed in this run; 12 selected adapter/policy/import live tests and the live connectivity/flow set were run separately.
- The Stage 1.19 performance gate remains unresolved; Stage 3 is not declared released.

## Evidence categories

- **LIVE VERIFIED** — demonstrated against a real Packet Tracer device.
- **OFFLINE VERIFIED** — automated tests or deterministic simulation only.
- **LIVE VERIFIED WITH LIMITATIONS** — live evidence exists, with a named gap.
- **IMPLEMENTED — NOT LIVE VERIFIED** — code exists without the required live proof.
- **FAILED** — tested and did not pass.

Live evidence: `probes/raw/stage4-live.json`, `probes/raw/stage4-live-asa.json`, `probes/raw/stage4-live-stp.json`, `probes/raw/stage4-live-rollback.json`, `probes/raw/stage4-live-arp.json`, `probes/raw/stage4-live-nat.json`, `probes/raw/stage4-live-faults.json`, `probes/raw/stage4-live-enhance.json`.
