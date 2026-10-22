# Overnight automation validation (S2.3)

This page records the real Windows Task Scheduler executions used to validate
the nightly workflow. They are **scheduled executions**, not a claim of three
calendar nights: the calendar-time gate is called out at the bottom.

## What was scheduled

The real wrapper (`scripts/nightly/havyn-nightly.ps1`) was invoked by Task
Scheduler as the interactive user. Each validation task used a three-topology
queue (T01, T02, T06), the repository virtualenv, and a OneDrive upload
destination. The task action was a `.cmd` launcher that sets:

- `HAVYN_QUEUE` — the three-topology queue;
- `HAVYN_RESULTS` — `runner_results/scheduled-nightN`;
- `HAVYN_UPLOAD_DIR` — `%USERPROFILE%\OneDrive\HavynUploads\scheduled-nightN`;
- `HAVYN_NOTIFY_FILE` — `runner_results/scheduled-nightN-notify.log`.

Packet Tracer was **closed** before Night 1, so the wrapper had to start it,
wait for the Script Engine heartbeat, run the queue, and close it again.

## Results

| Run | Scheduled | Cases | Result | PT lifecycle | Upload | Exit |
| --- | --- | --- | --- | --- | --- | --- |
| Night 1 | 2026-10-22 15:31:36 | T01, T02, T06 | 3/3 pass | started by wrapper, closed after | 4 files verified | 0 |
| Night 2 | 2026-10-22 15:40:50 | T01, T02, T06 | 2/3 pass; T06 failed | started by wrapper, closed after | 4 files verified | 1 |
| Night 3 | 2026-10-22 15:53:51 | T01, T02, T06 | 3/3 pass | started by wrapper, closed after | 4 files verified | 0 |
| Night 4 | 2026-10-22 16:01:06 | T01, T02, T06 | 3/3 pass | started by wrapper, closed after | 4 files verified | 0 |
| Night 5 | 2026-10-22 16:22:22 | none | exit 4: fresh heartbeat, mailbox not ready | started by wrapper, closed after | none | 1 |
| Night 6 | 2026-10-22 16:29:39 | T01, T02, T06 | 3/3 pass | started by wrapper, closed after | 4 files verified | 0 |

Four **successful** scheduled runs (Nights 1, 3, 4, 6) plus two genuine failures
(Nights 2 and 5). Each successful run recorded 15/15, 16/16 and 9/9 criteria.

### Night 5 failure and readiness probe

Night 5 started PT and saw a fresh heartbeat file, but the mailbox could not
execute a command yet; the runner exited 4 before the first case. The wrapper
reported the failure (`nightly FAILED: havyn_runner exited with code 4; no
summary.json ...`) and closed PT. The fix is a real channel probe after the
heartbeat: `Test-Channel` pings the bridge and waits up to three minutes
before the runner starts. Night 6 then passed 3/3 with the same closed-PT
start.

### Final wrapper smoke (reports and upload)

After adding report generation, one manual wrapper run with PT closed produced:

- `T06.json`, `summary.json`, `history.jsonl`;
- `scorecard.json` / `scorecard.md` (9/9 criteria, 100%);
- `capability-map.json` / `capability-map.md`;
- seven files uploaded to `OneDrive\HavynUploads\nightly-final-test`, each
  SHA-256 verified;
- PT closed and no failure notification.

The runner uploads raw results before the wrapper generates reports, so the
wrapper performs a second verified upload of the augmented set when
`HAVYN_UPLOAD_DIR` is set.

### Night 2 failure and notification

Night 2 started on a freshly launched PT. The bulk create answered `ok:true`
for all four T06 devices and placed none of them (`device not present after
create`), so the build failed. This was a real environment/readiness fault,
not swallowed:

- runner exit 1, Task Scheduler `LastTaskResult 1`;
- notification written to `runner_results/scheduled-night2-notify.log`:
  `nightly FAILED: havyn_runner exited with code 2; T06: fail`;
- evidence `autopilot_evidence/build-03f2e8596a91`;
- PT still closed by the wrapper.

The failure led to commit `7c67a8b`: a bulk create now re-sends **only the
missing devices** once and lets the read-back decide. Night 3 and Night 4
then passed with the same queue and a closed-PT start.

## Evidence locations

- Results: `runner_results/scheduled-night1`, `scheduled-night2`,
  `scheduled-night3`, `scheduled-night4` (local run artifacts, gitignored).
- Uploads: `C:\Users\Hamza\OneDrive\HavynUploads\scheduled-nightN\` (four
  files each: three results + history, SHA-256 read-back verified).
- Failure notification: `runner_results/scheduled-night2-notify.log`.
- Task history: `Get-ScheduledTaskInfo -TaskName HavynNightlyValidationN`.

## Remaining limitation

The three successful runs happened in one session, not on three separate
calendar nights. The full 13-active-spec nightly queue (including the
43-device enterprise campus) was **not** run in a single overnight window;
that would take hours. The production task is registered daily at 02:00 and
uses the generated `scripts/nightly/nightly-queue.json`; the three-night
calendar gate remains an operational observation for the operator.

Power: the machine must stay awake on AC (`standby-timeout-ac 0`,
`hibernate-timeout-ac 0`) and the task runs as the interactive user. The
wrapper never deletes anything and closes PT only when it started it.
