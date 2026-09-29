# Release checklist

A release is cut only when every box below is checked. The point of this
list is to make "green but unproven" impossible: the offline suite can pass
while live coverage was silently skipped, so live proof is a separate,
mandatory gate.

## 1. Offline suite green

```bash
python -m pytest -q --ignore-glob="*test_live_*"
```

Must be fully green from the repo root. No PT required.

## 2. Live suite with `--require-live`

```bash
python -m pytest -q tests/autopilot --require-live
```

- PT open with the MCP Control Center extension, heartbeat fresh.
- `--require-live` turns every unreachable-PT skip into a FAILURE: a dead
  PT fails the release instead of silently passing it.
- Every live test owns its namespace (`run_tag`), removes its devices in a
  `finally`/teardown, and asserts nothing leaked. A leak fails the test.
- After 3–4 heavy builds, restart PT before continuing (stale sessions
  produce false failures).

## 3. Leak check

- Re-read the workspace after the live suite: no `q<short>-*` names and no
  spec ids from the runs may remain (only pre-existing user devices).
- `--clean-workspace` and any delete always require explicit user approval,
  every time — never as part of the release routine itself.

## 4. Foundation gate (Stage 1 exit)

- Workspace cleaned (approved), reference spec 10× via the runner.
- 10/10 PASS, every run under 5 minutes, exit code 0 each time.
- One failure restarts the count after the fix — no partial credit.

## 5. Evidence

- The evidence zip for the gate runs is kept (`keep` setting, default 20
  builds); older ones are pruned automatically.
- The scorecard (`scorecard.json` + `scorecard.md`) is regenerated from the
  runs and published with the release notes.

## 6. Capability map

- `docs/capability-map.md` regenerated from live lab results: every feature
  is Verified / Unverified / Refused with the run that proved it.
- A feature the live lab did not exercise ships as Unverified, never as
  Verified.
