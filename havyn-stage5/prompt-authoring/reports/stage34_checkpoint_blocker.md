# Stage 3/4 checkpoint — RESOLVED

Status: **RESOLVED 2026-10-29.** The Stage 3/4 engine and evidence are committed
separately as `d47588c`, on top of the Stage-5 commits. No Stage 3/4 file is in
a Stage-5 commit, and no Stage-5 file is in the Stage 3/4 commit.

## What was committed

| Area | Contents |
| --- | --- |
| Engine | Server-PT services (DNS/HTTP/DHCP/NTP/Syslog/FTP/AAA), Cloud-PT DSL/cable and Frame Relay, RSA key generation, ASA DHCP/object handling, switch SVIs/EtherChannel/STP/port-security/VTP/voice VLAN |
| Compile guards | by-name refusals for unsupported shapes; port solver, executor and spec lint aligned |
| Stage-5 dependency | `ai/expander.py` criterion fix; the 8 candidates that needed Server-PT services now verify |
| Evidence | `probes/raw/`, `probes/live/`, `library/stage3/`, probes and labs |
| Reports | capability map, Stage 3 scorecard, CHANGELOG, skill notes, runner summary |
| Tests | `test_stage3_features.py`, `test_capability_map.py`, `test_compiler.py` |

## Sanitisation applied

- `probes/stage3_ssh_lab.py`: username/password from `HAVYN_LAB_USER` /
  `HAVYN_LAB_PASSWORD`, random per run otherwise.
- `probes/stage3_ftp_lab.py`: `HAVYN_FTP_USER` / `HAVYN_FTP_PASSWORD`, random
  per run otherwise.
- `probes/stage3_aaa_lab.py`: `HAVYN_LAB_USER`, `HAVYN_LAB_PASSWORD`,
  `HAVYN_AAA_KEY`, random per run otherwise; the ACS JS is built with
  `json.dumps`, not string interpolation.
- `library/stage3/S3-A15-ssh.json` and the Stage 3 test fixture now use
  `LAB-ONLY-CHANGE-ME`.
- Raw captures contain CLI prompts, masked values and redacted hashes
  (`***REDACTED***`, `LAB-REDACTED`), not secret values.
- No token, private key, API key or personal credential is in the commit.
- Originals of the four edited files are kept locally, outside Git, in
  `.audit-tmp/stage34-originals/` (gitignored). They contained only synthetic
  lab defaults.

## Verification

- Stage 3 tests: 86 passed.
- Full offline suite at the committed engine: 1,197 passed / 32 skipped.
- Candidate verification at `d47588c`: **56/56**, clean engine pin (no
  `+dirty-src`).
- Dataset re-generated and frozen at this engine; certificates pinned to
  `src-tree:5e437ba66b14`.

## Owner action

Review the sanitised lab values. If you prefer different lab credentials, set
`HAVYN_LAB_PASSWORD`, `HAVYN_FTP_PASSWORD` and `HAVYN_AAA_KEY` in the
environment when running the probes; do not commit real credentials. No
approval or signature is required for this checkpoint.
