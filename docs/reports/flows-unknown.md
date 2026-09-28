# F1: flows UNKNOWN ki diagnosis (S1.1)

**Status:** diagnosis complete from raw evidence; two live checks still pending
(marked ⏳ — they need Packet Tracer open). No code was changed for this task;
the fix plan below is executed by S1.2/S1.7.

**Source of truth:** `autopilot_evidence/archive-20260928-031921.zip` →
`build-113ed9a77678/evidence.jsonl` (2,840,707 bytes), the reference run of
`my-network.spec.json` (`enterprise-campus-eval`, 45 devices, 42 links,
23 acceptance criteria). Every timestamp, count and quote below was re-parsed
out of that file, not copied from a summary.

---

## TL;DR — kyun 11/11 flows UNKNOWN

The flows were never *tested and found unknown*; they were **never run at all,
then reported as unknown**. Three stacked causes:

1. **Blockage cascade:** two `configure_ios` ops precondition-blocked on a
   channel outage → `op-wait-dhcp` (depends on *every* config op) blocked →
   all 11 `op-flow-*` ops (which depend on wait-dhcp + every config op)
   blocked → `execution.flow_results = {}`.
2. **Plumbing gap:** `orchestrator.build()` verifies flows *only* from
   `execution.flow_results` (`orchestrator.py:507,536`). The repair engine ran
   55 real pings during the same build and recorded 3 flows **PASS** — into
   `repair_function_check` records that verification never reads.
3. **Result:** verifier emitted `no flow result recorded for …` → UNKNOWN,
   while the same build had already proven 3 of the 11 flows reachable.

"Network chalta hai, engine ka reader toota hai" — confirmed: the engine's own
repair pings observed `ping pc1->10.10.20.6: ok` and `ping lap2->10.10.10.3: ok`
(0% loss) at 17:40:04, and the report at 17:40:46 still printed UNKNOWN.

---

## 1. Timeline of the failure (all times UTC, 2026-09-27)

| Time | Event | Evidence record |
|---|---|---|
| 17:18:16 | build request, IPAM, compile, preflight pass, 136 DAG nodes | `build_request`, `addressing_planned`, `compiled` |
| 17:18:24 | 45 devices created (single batch) | `op-create-*` receipts |
| 17:18:29 | 42 links created (single batch) | `op-link-*` receipts |
| 17:18:38 → 17:19:34 | router/firewall config; **`op-config-risp` and `op-config-fwhq` PRECONDITION-BLOCKED** | `precondition_blocked` |
| 17:19:42 → 17:19:58 | VLAN push: **`op-vlan-switch-swcore/swf1/swf2` FAIL** on trunk read-back | `operation_receipt` |
| 17:19:17 / 17:19:18 | block reason (verbatim): `no live channel: HTTP bridge sees no PT poll and the file mailbox heartbeat is stale (is PT open with the extension installed?)` | `precondition_blocked` |
| 17:20:06 | DAG execution ends: `succeeded 117, failed 3, blocked 16` — **blocked includes `op-wait-dhcp` and all 11 `op-flow-A-001..A-011`** | `execution_summary` |
| 17:20:44 → 17:40:14 | repair waves 1–3: **55 real pings** (31 hit the 25 s timeout), 3 flows observed PASS; budget exhausted at 3/3 attempts | `repair_function_check`, `repair_attempt`, `repair_blocked` |
| 17:40:46 | verification: `acceptance = {pass 9, fail 2, unknown 12}`, flows evidence: `no flow result recorded` ×11 | `verification` |

Overall run: **17:18:16 → 17:40:46 = 1350 s** (the blueprint's "1349 seconds" —
same run). DAG execution was 110 s (8%); the repair/ping loop was 1170 s (86%).

---

## 2. The blockage cascade (why `flow_results` was empty)

Three lines make the chain:

```python
# compiler.py:1949  — wait-dhcp depends on EVERY configure op
dependencies=tuple(dict.fromkeys(config_ops)) or (),
# compiler.py:1955
flow_dep = ("op-wait-dhcp",)
# compiler.py:1974-1976  — every flow depends on wait-dhcp AND every config op
dependencies=tuple(dict.fromkeys([*config_ops, *flow_dep]))
```

```python
# executor.py:123-132 — a dependency that is not SUCCEEDED blocks the op
# "dependency gate: anything not SUCCEEDED (and not SKIPPED) blocks"
node.status = OpStatus.BLOCKED
node.note = f"dependencies not satisfied: {unmet}"
```

So **one blocked config op (risp) blocked `op-wait-dhcp`, which blocked all
11 flows** — including `pc1 → pc7` on a path whose config had succeeded.
`_op_wait_dhcp` itself never fails when it *runs* (it always sets
`OpStatus.SUCCEEDED` and records `still leaseless after 20s: …` truthfully);
it was not "stopping flows" as a gate — it was *stopping them as a victim of
the dependency graph*. (This answers blueprint step 3: the gate returns
SUCCEEDED-when-run; the hazard is its all-config-ops dependency fan-in.)

`_op_verify_flow` marks flow nodes SUCCEEDED whenever they run
(`executor.py:972`); verdict truth lives only in `result.flow_results` — which
was never populated because no flow op ever ran.

## 3. The plumbing gap (why real observations were discarded)

`repair/engine.py:613` runs `adapter.run_flow_test(...)` for each criterion and
stores the outcome in a `repair_function_check` record (`repair/engine.py:672`).
`orchestrator.build()` feeds verification **only** `execution.flow_results`
(`orchestrator.py:507` and `:536`, the post-repair re-verify path alike).
Nothing merges repair observations back. Final repair check (17:40:04, verbatim):

| Crit | Pair | Verdict | Detail |
|---|---|---|---|
| A-001 | pc1 → 10.10.20.6 | **pass** | `ping pc1->10.10.20.6: ok` |
| A-002 | lap2 → 10.10.10.3 | **pass** | `ping lap2->10.10.10.3: ok` |
| A-003 | pc1 → 10.20.40.10 | fail | `ping pc1->10.20.40.10: none (after ARP convergence)` |
| A-004 | lap1 → 10.20.40.11 | fail | `ping lap1->10.20.40.11: none (after ARP convergence)` |
| A-005 | pc13 → 10.20.40.11 | fail | `ping pc13->10.20.40.11: none (after ARP convergence)` |
| A-006 | pc14 → 10.10.30.3 | fail | `ping pc14->10.10.30.3: none` |
| A-007 | pc15 → 10.10.10.4 | fail | `ping pc15->10.10.10.4: none (after ARP convergence)` |
| A-008 | pc16 → 10.20.40.10 | fail | `ping pc16->10.20.40.10: none` |
| A-009 | lap3 → 10.30.50.14 | **pass** | `ping lap3->10.30.50.14: ok` |
| A-010 | pc9 → 198.51.100.10 | fail | `ping pc9->198.51.100.10: none (after ARP convergence)` |
| A-011 | pc13 → 198.51.100.10 | unknown | `no new ping statistics appeared on 'pc13' within 25s` |

Three real PASSes existed 42 seconds before the verifier printed UNKNOWN.

## 4. Ping parser, timeout window, ARP loss (blueprint step 4)

**Parser (`src/network_autopilot/pt/ping.py`): not the culprit.**
`classify_ping` parses both PT forms (`Sent/Received/Lost` host form and
`Success rate is N percent` IOS form), unparseable input degrades to `none`
(never raises). All 55 sessions in the evidence parsed to a counts verdict
where a DONE line appeared; the only non-parses were sessions that never
produced a DONE line at all (timeouts, see below).

**Timeout window: 25 s per attempt, 3 attempts, 1 s between, 1.5 s pre-settle**
(`bridge_adapter.py:2534, 86-91`). Observed: 31/55 sessions hit the full 25 s —
all of them pinging destinations that were genuinely unreachable (100% loss
answers confirm: the *next* attempt got `Received = 0, Lost = 4`). The timeout
is not misfiring; it is paying 25 s to confirm each dead path, repeatedly.
Worst case per flow ≈ 78.5 s.

**ARP / first-packet loss: already handled.** The adapter runs up to
`_FLOW_MAX_ATTEMPTS = 3` with a 1.5 s pre-settle, and the evidence shows the
signature working: A-002 wave 1 first attempt `Sent = 4, Received = 3 (25% loss)`
→ second attempt `0% loss`. That is the first-packet ARP loss being absorbed
by the retry, then reported as `… (after ARP convergence)`. The warm-up exists;
S1.2's job is to pin it with tests ("pakka karo"), not to invent it.

**Where the reader *was* broken:** not in parsing but in *whether a ping ever
happened* (§2) and whether its result was *delivered* to the verifier (§3).

## 5. Per-flow UNKNOWN table

Verification timestamp for all 11: `2026-09-27T17:40:46+00:00`.
Reason for all 11 (verbatim): `no flow result recorded for <src> -> <dst> (icmp)`.

| Crit | Pair | Root cause | Repair-wave observation | Ping time spent |
|---|---|---|---|---|
| A-001 | pc1 → pc7 (10.10.20.6) | blocked flow op | **PASS** @17:40:04 | 17 s, 2 attempts |
| A-002 | lap2 → pc6 (10.10.10.3) | blocked flow op | **PASS** | 20 s, 3 attempts |
| A-003 | pc1 → srv1 (10.20.40.10) | blocked flow op | fail (100% loss after ARP) | 105 s, 6 attempts |
| A-004 | lap1 → srv2 (10.20.40.11) | blocked flow op | fail (after ARP) | 107 s, 6 attempts |
| A-005 | pc13 → srv2 (10.20.40.11) | blocked flow op | fail (after ARP) | 106 s, 6 attempts |
| A-006 | pc14 → lap1 (10.10.30.3) | blocked flow op | fail | 107 s, 6 attempts |
| A-007 | pc15 → pc4 (10.10.10.4) | blocked flow op | fail (after ARP) | 106 s, 6 attempts |
| A-008 | pc16 → srv1 (10.20.40.10) | blocked flow op | fail | 107 s, 6 attempts |
| A-009 | lap3 → pc16 (10.30.50.14) | blocked flow op | **PASS** | 10 s, 2 attempts |
| A-010 | pc9 → srvisp (198.51.100.10) | blocked flow op | fail (after ARP) | 79 s, 6 attempts |
| A-011 | pc13 → srvisp (198.51.100.10) | blocked flow op | unknown (25 s quiet console) | 128 s, 6 attempts |

`op-flow-*` never executed, so "duration" of the flow test itself is 0; the
ping time shown was spent by the repair engine and thrown away.

The blueprint's manual ping (`10.10.20.7 chala, TTL=127`) is corroborated by
A-001's engine-observed PASS on the same subnet (10.10.20.6, 0% loss) — the
data plane was up for that pair the whole time.

## 6. Related defects found in the same evidence (feed S1.7)

These are not flow defects, but the same run's report includes them, classified
per blueprint S1.7 ("config galat / read-back galat / criterion galat"):

| Finding | Class | Evidence |
|---|---|---|
| `op-vlan-switch-swcore/swf1/swf2` FAIL | **read-back galat** — the pushed CLI was correct (`switchport mode trunk; allowed vlan 10,20,30`) and inter-VLAN pings crossed those very trunks, but the postcondition demands trunk ports appear in `show vlan brief` VLAN membership — PT omits trunk ports from every membership list (raw capture: Fa0/1, Fa0/2, Gig0/1 absent; unused ports sit in VLAN 1). The assertion can never pass. | `operation_receipt` 17:19:42/48/58; raw `show vlan brief` captures |
| A-015 trunk UNKNOWN (target `rhq:Gi0/0`, router-on-a-stick) | **criterion/observation gap** — trunk state is only ever derived from switch-side `show vlan brief`; a router subinterface trunk has no read path at all (`trunk state not determinable`) | **S1.7 fix** — `_fill_trunk_from_subinterfaces` (bridge_adapter); offline-verified, live re-run pending |
| A-021/A-022 wireless FAIL (`lap4`/`lap5` have no `Wireless0`) | **spec/criterion galat** — the spec declares these criteria but **no wireless link and no radio** for lap4/lap5 (only `wr1`'s uplink `l-swf1-wr1` exists); the compiler never emitted radio/association ops. The FAIL itself is true. | **S1.7 fix** — links declared in `my-network.spec.json` + compile-time refusal guard (offline-verified, live re-run pending) |
| `VT-addr-risp-FastEthernet1/0` = `0.0.0.0/0.0.0.0` (the 1 addressing FAIL) | **config galat (never applied)** — `op-config-risp` precondition-blocked by the channel outage; repair wave 3 fixed Fa0/0 but budget exhausted (3/3) before Fa1/0 | `precondition_blocked` 17:19:17; `repair_blocked` 17:40:14 |

## 7. Pending live checks (⏳, blueprint steps 2–3) — RESOLVED by S1.2

These could not be done without Packet Tracer open; both are now closed
(2026-09-28):

1. ~~⏳ Run `run_flow_test` live for the manual-ping pair (pc1 → 10.10.20.7)~~
   **Moot — the reference devices are no longer in the workspace.** A live
   read on 2026-09-28 shows only 18 `Power Distribution Device*` names;
   `pc1`/`pc7`/`r1` are gone, so the pair cannot be re-pinged. The original
   corroboration stands unchanged from the archived evidence (A-001's
   engine-observed PASS at 17:40:04, verbatim above). The same probe
   machinery is now proven live end-to-end by
   `tests/autopilot/test_live_flows.py` — the engine's own pings PASSed
   **5/5 runs** on a fresh lab (S1.2's done criterion).
2. ~~⏳ Confirm live that `_op_wait_dhcp` returns SUCCEEDED-with-truthful-note~~
   **Confirmed.** Live DHCP lab, 2026-09-28: `dhcp_convergence` evidence =
   `{"pending": [], "polls": 1, "timeout_s": 20.0}`, build failed ops `[]`,
   blocked ops `[]` — the gate ran, the lease landed on the first poll, and
   the op SUCCEEDED without ever failing the DAG (`.audit-tmp/
   wait_dhcp_live_check.py`, teardown leaked nothing).

## 8. Fix plan (executed by S1.2 / S1.7, not by this task)

| # | Defect | Fix | Task |
|---|---|---|---|
| F-1 | Repair flow observations never reach verification | merge repair `run_flow_test` results into `execution.flow_results` before the post-repair re-verify | **S1.2 done** (`test_flow_verification_fix.py`, mutation-proven) |
| F-2 | one blocked config op blocks *all* flows (wait-dhcp + flow fan-in) | probes waive unmet deps in executing modes (READ_ONLY/DRY_RUN stay blocked on purpose) | **S1.2 done** (same file, mutation-proven) |
| F-3 | ARP warm-up/retry exists but is unpinned | pinned by `test_bridge_adapter_receipts.py` #27 tests (re-run green 2026-09-28) | **S1.2 done** |
| F-4 | trunk postcondition can never pass (`show vlan brief` omits trunks) | re-derive the trunk read-back from what PT actually prints (absence from every membership list, proven against the captured raw output) | **S1.7 done** (`test_trunk_readback.py`, mutation-proven; live-confirmed in build-68648f705ccb — vlan ops succeeded, `configured_vlans_present passed=true`) |
| F-5 | A-015 router-side trunk has no read path | observe or refuse explicitly; never a permanent UNKNOWN that looks like a missing check | **S1.7 done** — trunk read from ROAS subinterfaces in the topology read (`_fill_trunk_from_subinterfaces`, same file, mutation-proven; live re-run pending) |
| F-6 | A-021/A-022 unsatisfiable as specified | spec declares the wireless links the criteria demand (radio + association ops appear); engine refuses such criteria at compile time when undeclared | **S1.7 done** — `l-wl-lap4`/`l-wl-lap5` in the reference spec (DAG +2 radio +2 association, plan unchanged at 40 assignments); `_reject_wireless_criteria_without_links` refuses by criterion id (mutation-proven); live-confirmed in build-68648f705ccb (A-021/A-022 PASS) |
| F-7 | 25 s × 3 attempts × dead paths eats ~1140 s of the run | speed pass: adaptive polling and batched reads without changing any verdict (S1.6) | **S1.6 done** — `_FLOW_ATTEMPT_TIMEOUTS (8,15,25)` + snapshot-batched `inspect_device`/`observe_state`, mutation-proven; live reference run 1350 s → **621 s**; the remaining time is dead-path polling and clears with S1.7 correctness, `<5min` re-measured after it |
| F-8 | one heartbeat hiccup turns into a *wrong* verdict at seven read sites: precondition UNKNOWN → op blocked forever; radio read-back fired before the swap landed; flow resolve poisoned; one stale push answer aborted a whole `configure_ios`; an unknown workspace snapshot made 20/20 static hosts FAILED with "address not observed"; teardown fail-closed `delete_device` as `rejected` and leaked the device; an executor exception left no receipt at all | bounded settle/retry at each site, sized against `mailbox.HEARTBEAT_FRESH_S` (6 s); unknown stays UNKNOWN with the channel detail (rule 7) — never a device-blaming FAILED; `_fail` and the executor's exception handler now always record a named receipt (rule 8) | **S1.7 done** — `test_transient_reads.py` (14 tests, each RED-first then mutation-proven: precondition/radio/resolve/push/bulk-readback/delete-settle/exception-receipt); live: scale test **63/63, 5/5 flows, clean teardown PASSED**; reference re-run on the healthy session still pending |
