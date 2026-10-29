# Stage 3/4 checkpoint — safety blocker

Status: **NOT COMMITTED.** The engine work is test-covered in the working tree,
but the surrounding Stage 3/4 evidence set contains credential-like material.
Committing it would expose secrets, so it is left untouched for owner
sanitisation.

## What the 8 clean-checkout failures need

The 8 candidates (`oc-012, 013, 023, 025, 041, 053, 055, 056`) fail on a clean
`e8503cd` tree with:

```text
CompileError: spec requires features outside the compiler scope: Server-PT
services (...)
```

The working tree resolves them with:

| Area | Working-tree site |
| --- | --- |
| Server-service compile support | `src/network_autopilot/compiler/compiler.py:133-160` |
| Server service fields / records / pages / pools | `src/network_autopilot/core/network_spec.py` (`ServerServiceSpec`) |
| Spec lint alignment | `src/network_autopilot/compiler/spec_lint.py` |
| Live server configuration helpers | `src/network_autopilot/adapter/packet_tracer/stage3_commands.py` (imported by `bridge_adapter.py:57`) |
| Executor / port solver additions | `compiler/executor.py`, `compiler/port_solver.py` |

Tests that cover this work in the working tree:

- `tests/autopilot/test_stage3_features.py`
- `tests/autopilot/test_compiler.py`
- `tests/autopilot/test_capability_map.py`

The full offline suite passed in this working tree (1,176 passed / 32 skipped
on the first run; one unrelated bridge timing flake on the rerun).

## Why it is not safe to commit

The Stage 3/4 evidence set is not just source:

- **34 files** under `probes/raw/` and `library/stage3/` contain credential-like
  strings (password/passwd/secret/enable secret/username/token), including
  router, switch, ASA and SSH/FTP/AAA captures.
- The capability map tests require real evidence pointers, so committing the
  capability work without the evidence would break those tests; committing the
  evidence would expose the credentials.
- There are also 69 probe files, 9 library fixtures and several `.pkt` scratch
  artifacts in the working tree. They are not a coherent, sanitised checkpoint.

A commit that includes only the compiler files would be a partial Stage 3/4
checkpoint and would still leave the capability-map/tests uncommitted. That is
not a safe checkpoint either.

## Required owner action

1. Sanitise or replace the credential-bearing captures under `probes/raw/` and
   `library/stage3/` (redact passwords, keys, tokens, usernames where sensitive).
2. Decide the Stage 3/4 checkpoint boundary (source + tests + capability map +
   sanitised evidence).
3. Commit that work **separately** from Stage 5.
4. Re-run:
   ```bash
   python havyn-stage5/scripts/certify.py
   python havyn-stage5/prompt-authoring/owner-candidates/verify_candidates.py --emit
   python -m pytest
   ```
5. Re-test a clean checkout; the expected result is 56/56 candidates and no
   Server-PT compile failures.

Until then, the Stage-5 commit is self-contained for the `ai/` layer but a clean
checkout still verifies **48/56**, not 56/56. This is recorded in
`reproducibility_dependency.md` and `stage5_handoff.md`.
