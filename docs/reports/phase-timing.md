# S1.5: phase timing — instrumentation + reference-run ka analysis

**Status:** complete. Code + tests + live dry-run table verified; the archived
reference run (`build-113ed9a77678`) mined for the slowest phases. Receipt
timestamps turned out to be dead data (defect found while mining, fix
described below).

**Source of truth:** `autopilot_evidence/archive-20260928-031921.zip` →
`build-113ed9a77678/` (same run as `flows-unknown.md`), re-parsed record by
record. All times UTC, 2026-09-27.

---

## TL;DR — reference run (1350 s) kahan gaya

| Rank | Phase | Time | Share | Kya hota hai andar |
|---|---|---|---|---|
| 1 | **repair** | **1240 s** | 92% | console-output polling 924 s, pings 180 s, address read-backs 92 s, CLI 34 s |
| 2 | **execute** | **110 s** | 8% | address read-backs 66 s, console polling 12 s, CLI config 9 s, create bulk 4 s, cable bulk 4 s |
| 3 | **verify** | **~4 s** | <1% | final read-backs only |
| — | preflight | <1 s | 0% | plan + lint + compile + preflight, same second |

**Slowest 3:** repair ≫ execute ≫ verify. But the useful cut for S1.6 is one
level deeper — across both windows the three biggest line items are:

1. **console-output polling: 936 s** (12 s execute + 924 s repair) — the
   `getOutput()` read-until-ready loop, 384 sends total, mostly inside repair
   diagnosis.
2. **pings: 180 s** (55 sends, all inside the repair loop).
3. **address read-backs: 162 s** (66 s execute + 92 s repair, 52
   `getSubnetMask` sends).

The device work itself is cheap: one bulk send created all 45 devices (4 s),
one created all 42 links (4 s). The run does not fail because Packet Tracer
is slow at building; it fails because the engine **polls** it.

---

## 1. Kaise nikala (method, reproducible)

1. Wall segments: `ts` of `build_request` → `compiled` → `execution_summary`
   → `verification` → `build_result` in `evidence.jsonl`.
2. Family breakdown: receipts' `started_at`/`finished_at` were unusable (§3),
   so each record's `ts` minus the previous record's `ts` (second precision)
   was charged to **that record**; `bridge_send` payloads were classified by
   the JS inside them (`lwAddDevice` → create, `addLink` → cable, `ping` →
   ping, `getSubnetMask` → address read-back, `enterCommand` → CLI,
   `getOutput` → console polling).
3. Windows were cut at `execution_summary` (17:20:06) and `verification`
   (17:40:46) so repair-time polling is not blamed on execute.

Scratch script: `.audit-tmp/phase_mine.py` (gitignored; archive lives in
`%TEMP%` and will not survive forever — the numbers above are the durable
part).

## 2. What the run says about S1.6 (speed work)

- **#1 target: polling.** 936 s of `getOutput`/wait-for-console sends. The
  executor already knows what it just configured; polling until the console
  shows it is where nearly 70% of the run goes.
- **#2: ping waits.** 55 sends, 180 s of gap time here; `flows-unknown.md`
  shows 31 of them hit the full 25 s timeout because the destination had no
  address (the S1.1 cascade) — pinging early is wasted pinging.
- **#3: address read-backs.** 52 `getSubnetMask` sends ≈162 s; these verify
  DHCP/static assignment one host at a time.
- Repair *planning* alone was 16.1 s on a later dry run (64 repairs), while
  preflight stays ~3 s — planning is not the problem, waiting is.

## 3. Defect found while mining: receipt clocks are dead data

All **123/123** `operation_receipt` records in the archive have
`started_at == finished_at` (e.g. `17:18:24` → `17:18:24`), sum = 0.000 s:
`bridge_adapter.py` / `executor.py` stamp both fields at receipt-construction
time, after the work is done. Any breakdown read from receipts reports 0 for
a build that really spent minutes.

**Fix (this task):** the executor now times each dispatch with a monotonic
clock — `ExecutionResult.op_seconds: dict[op_type, seconds]`
(`executor.py`, `_time_op`), exposed in `execution.to_dict()`; the
orchestrator groups it into families with `_families_from_op_seconds()`
(`orchestrator.py`) and writes the `phase_timing` evidence record and the
report table after OVERALL. Test that pins it sleeps 0.35 s in
`create_device` and asserts the `create` family ≥ 0.3
(`test_phase_timing.py::test_create_time_comes_from_the_executor_clock_not_the_receipts`);
mutating the `finally: self._time_op(...)` out turns it red.

Receipt stamps themselves were left as-is: they are pre-existing, nothing
reads them for timing anymore, and a 31-site rewrite is not this task.

## 4. Honest limits

- Evidence timestamps are **second precision**; sub-second ops round to 0 and
  window sums are 109/1237/4 vs walls of 110/1240/~0 — off-by-one from
  rounding, not drift.
- Gap attribution charges each gap to the record that *closed* it, so loop
  overhead rides on the next record; treat §1 numbers as ± a few seconds, not
  exact.
- The 11 flow ops + `op-wait-dhcp` have no time at all: they were **blocked,
  never run** (S1.1 cascade) — "0 s" there means "never attempted".
- This analysis is of an *old* run; new runs carry a real `phase_timing`
  evidence record and `op_seconds`, so future reports read them directly
  instead of mining.
- Offline suite after this change: **686 passed** (654 s). Live re-check
  after the receipt→executor-clock switch: reference dry run, exit 0,
  `PLAN OK`, table printed (`preflight 2.3s, execute 0.0s, verify 0.2s,
  repair 14.6s, total 17.2s`; families 0.0 because DRY_RUN executes
  nothing — honest, not a bug).
