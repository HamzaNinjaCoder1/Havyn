## What and why

<!-- What changes, and what problem it solves. The diff already says what; explain why. -->

**Rule 9 check:** if this refuses something PT cannot do, say so here and name
the reason in the description.

## Failing-first test

A test that passes before the change is not testing the change.

- [ ] Bug fix: the new test **fails** on the unfixed code — I mutated the fix
      back out and watched it go red
- [ ] Feature: the new test fails on `main` and passes on this branch
- [ ] Test name states the behaviour, not the function under test
- [ ] No assertion weakened to make the suite green

<!-- Which test, and the command that proves it. -->
```
<command and observed result>
```

## Verification

- [ ] `python -m pytest` passes from the repo root
- [ ] `python scripts/pt_clean_workspace.py --dry-run` if this touched deletion

**Live proof** — delete this section only if the change is provably
offline-only, and say why in one line.

- [ ] Ran against a real Packet Tracer session (PT open, extension loaded)
- [ ] Named the live test that proves it: `tests/autopilot/test_live_*.py::test_x`
- [ ] PT could not be run — I state exactly which half is unverified and why

```
<live run output, or the honest reason there is none>
```

## Capability Map

The map is the contract with the user. If the change moves a line on it, update
it in the same PR.

- [ ] No capability changed → nothing to update
- [ ] Capability **added** → added to the map with how it was proven
- [ ] Capability **removed or narrowed** → changed, and the CHANGELOG says why
- [ ] A limitation was found → recorded as a limitation, never quietly dropped

<!-- What moved, or "no change". -->

## The 10 rules that do not bend

Confirm the ones your diff could plausibly have broken.

- [ ] No raw f-string JavaScript (every field via `json.dumps` / `_q()`)
- [ ] No filesystem path built by concatenating user input
- [ ] No new bridge endpoint that skips the token
- [ ] No policy validation added to a Pydantic model
- [ ] No PT API or CLI signature used that is not already in this repo
- [ ] No `UNKNOWN` turned into a `PASS` anywhere
- [ ] No operation reported confirmed without a read-back
- [ ] Nothing half-configured: an impossible spec is refused by name
- [ ] No claim of verification beyond what was actually run

## Security checklist

Only relevant if you touched Script Engine builders, paths or the bridge —
delete if not.

- [ ] Generated JavaScript uses `json.dumps` per field, not raw f-strings
- [ ] New path components are normalised before being joined to a root
- [ ] No new bridge endpoint skips the token check
- [ ] Fields reaching IOS CLI reject newlines (and `#` for banners)
