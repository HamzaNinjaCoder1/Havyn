# Nightly automation setup (S2.3)

The repository carries everything the night needs; three one-time actions
on the laptop itself require you (admin/interactive), listed first. After
that the job runs itself and the morning scorecard is the only thing to read.

## One-time actions (you, ~10 minutes)

1. **Repo path in the task file.** Open `scripts/nightly/havyn-nightly.xml`
   and replace every `%REPOROOT%` with this repo's absolute path
   (e.g. `D:\Agent\MCP-Packet-Tracer`). The scheduler needs absolute paths.
2. **Import the task (admin PowerShell).**
   `schtasks /create /tn "HavynNightly" /xml "<repo>\scripts\nightly\havyn-nightly.xml"`
3. **Power: stay awake on charger.**
   `powercfg /change standby-timeout-ac 0` and
   `powercfg /change hibernate-timeout-ac 0` (admin). Sleep kills PT
   mid-build; on battery the task still refuses to start draining you.
4. **Notifications (optional).** Set a machine env var `HAVYN_NOTIFY_FILE`
   (e.g. `D:\Agent\havyn-nightly.log`) and/or `HAVYN_NOTIFY_URL`
   (a webhook that accepts `{"text": ...}`). Without either, failures are
   only visible in the Task Scheduler history and `runner_results/`.
5. **Build the queue once** (and after every library change):
   `python scripts/nightly/make_queue.py --out scripts/nightly/nightly-queue.json`

## What the night does

02:00 — task wakes the laptop, `havyn-nightly.ps1` starts Packet Tracer
*only if it is closed*, waits ≤5 min for the Script Engine heartbeat, runs
`scripts/havyn_runner.py` over `nightly-queue.json` (bridge adapter), then
closes PT again *only if the script started it*. Every case cleans the
workspace first (protected power devices survive); an uncleanable workspace
stops the run instead of building on leftovers. Every 10 cases the evidence
is pruned and results upload is attempted when configured.

`--clean-workspace` is never passed by automation: mass deletion stays an
explicit human decision, every time.

## Proof it ran itself (S2.3 gate)

Three consecutive nights with `runner_results/summary.json` files and a
fresh scorecard each morning. The scorecard generator (S2.4) reads the
results dir, so "the job ran" is proven by artifacts, not by testimony.
