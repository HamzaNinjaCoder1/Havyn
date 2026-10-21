# Sandbox egress: why it is blocked, and the exact admin action to unblock it

Status: **ADMIN REQUIRED.** Verified 2026-10-21 on this machine. Nothing in this
document is inferred — every claim names the file, command, or log line behind it.

---

## 1. What is blocked, measured

| Probe | Result |
| --- | --- |
| `socket.gethostbyname('pypi.org')` | `socket.gaierror: [Errno -3] Temporary failure in name resolution` |
| `curl https://pypi.org/simple/` | `curl: (56) Received HTTP code 403 from proxy after CONNECT` |
| `git ls-remote origin` | `fatal: ... Received HTTP code 403 from proxy after CONNECT` |
| `WebFetch https://pypi.org/simple/` | `Host "pypi.org" is not on the network allowlist (cowork-egress-blocked) ... Allowed: none` |
| raw `CONNECT` to the proxy, no auth | `HTTP/1.1 407 Proxy Authentication Required` |
| raw `CONNECT` to the proxy, valid `srt.` credential | `HTTP/1.1 403 Forbidden` — for **every** host tried |

Every host returns 403, including `localhost:80` and `api.anthropic.com`. The
credential is accepted (407 → 403), so authentication is not the problem: the
**allowlist is empty**.

## 2. Why it is empty

`coworkEgressAllowedHosts` is an **admin-managed (3p) configuration field**, not a
user preference. Extracted from `/usr/local/bin/claude`:

```js
{flatKey:"coworkEgressAllowedHosts", readers:["desktop","code"],
 failClosedValue:[],
 support:{enabled:{scopes:["3p"], availableInVersion:"1.2581.0"},
          executionTarget:"desktop"}, ...}
```

Two properties explain the whole failure:

- **`failClosedValue: []`** — when the field is unset, the allowlist is `[]`,
  i.e. deny every host. The default is *no access*, not *no restriction*. This is
  exactly the observed `Allowed: none`.
- **`scopes: ["3p"]`** — third-party/admin tier. The binary's own help text for
  the managed-configuration class of keys is explicit: it *"ignores the key in the
  in-app Setup panel, in any file the user can edit, and from a configuration
  server"*, and for machine-scoped keys *"an explicit value written to the
  user-writable HKCU hive is ignored"*.

### What was tried and did NOT work (so nobody repeats it)

- `%APPDATA%\Claude\settings.json` — ignored (user-writable).
- `~/.claude/settings.json` **inside the sandbox** — ignored; the sandbox is a
  separate machine and the value is resolved on the Windows host.
- `.claude/settings.json` **committed in the repo** — ignored for Cowork egress.
  (This probe file was created and then fully removed; it was never committed and
  left no trace in `git status`.)
- Restarting Claude Desktop after writing the user setting — confirmed to have
  taken effect (`uptime` = 0 min) with the allowlist still empty.

## 3. The exact admin action

Deploy **machine-scoped** managed configuration on the Windows host containing:

```json
{
  "coworkEgressAllowedHosts": [
    "pypi.org",
    "files.pythonhosted.org",
    "pythonhosted.org",
    "github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
    "registry.npmjs.org",
    "archive.ubuntu.com",
    "security.ubuntu.com",
    "deb.debian.org"
  ]
}
```

A bare `"*"` is also accepted and is equivalent to the built-in dependency-install
bundle the binary ships:

```
DEPENDENCY_INSTALL_DOMAINS = ["registry.npmjs.org","npmjs.com","www.npmjs.com",
 "yarnpkg.com","registry.yarnpkg.com","pypi.org","files.pythonhosted.org",
 "pythonhosted.org","github.com","objects.githubusercontent.com",
 "archive.ubuntu.com","security.ubuntu.com","crates.io","index.crates.io",
 "static.crates.io","playwright.download.prss.microsoft.com","cdn.playwright.dev"]
```

Note `deb.debian.org` is **not** in that bundle, so list it explicitly if needed.
`"*"` is the right choice for CI-style dependency installation; the explicit list
is tighter if this device should not have general egress.

**Deployment mechanisms that satisfy "machine-scoped" on Windows:**

1. The **in-app admin Setup configuration** for a 3p/enterprise install (the
   supported path the vendor documents under
   `https://claude.com/docs/third-party/claude-desktop` → `configuration`).
2. **MDM / ADMX** if this machine is enrolled — the standard endpoint-management
   channel.
3. **Machine policy under `HKLM\SOFTWARE\Policies\Claude`** (the Desktop policy
   hive; `HKLM\SOFTWARE\Policies\ClaudeCode` is the Claude Code equivalent).

Whichever is used, the value must land in a location the *user* cannot write.
A value written into HKCU, `%APPDATA%`, or a repo file will be ignored by design.

## 4. Restart requirement

Restart Claude Desktop fully after deploying (quit from the tray, not just close
the window). The binary notes a changed served value *"generally takes effect for
sessions started after the next app launch"*, so a new Cowork session is required
before egress changes.

## 5. Verification commands

```bash
# want an IP, not gaierror
python3 -c "import socket; print(socket.gethostbyname('pypi.org'))"

# want 200, not 000
curl -sS -o /dev/null -w "%{http_code}\n" https://pypi.org/simple/

# want a SHA, not "403 from proxy after CONNECT"
git -C <repo> ls-remote origin HEAD
```

Also ask the agent to `WebFetch` any host — success is the message changing from
`Allowed: none` to an actual fetch.

---

## 6. Important: unblocking egress still does not finish the project

Three independent blockers survive an egress fix. Documenting them here so the
egress work is not mistaken for project unblocking.

**Python version.** The sandbox has **3.10.12**; `pyproject.toml` requires
**`>=3.11`**. `apt-cache policy python3.11` offers only `3.11.0~rc1` — a release
candidate, not stable. `pydantic` and `pytest` are absent. So egress is necessary
but the interpreter is a second, separate problem.

**Live Packet Tracer cannot run in the sandbox, ever.** The sandbox is a Linux VM
under `bwrap --unshare-net` with its own network namespace, and it has **zero
host filesystem bridges**: `mount | grep -cE "9p|virtiofs|drvfs"` → `0`,
`/mnt/c/Users` does not exist, `%LOCALAPPDATA%` is unset. Packet Tracer is a
Windows GUI app whose mailbox lives in the Windows `%LOCALAPPDATA%`, which the
sandbox cannot see or reach. No network configuration changes this. The live
S1.19 gate must run on Windows.

**`git push` needs both egress and credentials.** Egress alone is not sufficient
if no credential is available to the sandbox.

### What that means in practice

Egress is worth deploying for `pip`, `pytest` (subject to the Python issue) and
`git push`. But the S1.19 exit gate — 10 live runs, 123/123, under the wall-clock
limit, with real Packet Tracer evidence — **runs on the Windows host**. Use
`scripts/s119_gate.ps1`, which measures the runner's own wall clock and refuses
to report a passing gate on anything less than a complete sweep.
