# Roadmap

This folder holds the plan the product is built against. It is a copy of the
blueprint, kept in the repository so the contract survives a reinstall, a
laptop change, or an agent that has never seen it.

## The blueprint

**[`havyn-production-blueprint.html`](havyn-production-blueprint.html)** — open
it in a browser. It is a single self-contained file: no build step, no local
assets, no network access except the web fonts it requests.

Its sections, in order:

| # | Section | What it fixes |
| --- | --- | --- |
| 0 | Yahan se shuru karo | the entry point and the first concrete step |
| 1 | Vision aur product ka wada | who this is for, and what separates it from a generic PT script tool |
| 2 | Na tootne wale niyam | the ten rules — mirrored in [`AGENTS.md`](../../AGENTS.md) |
| 3 | 5 capabilities | the three journeys: create, diagnose + fix, enhance |
| 4 | System architecture | how the layers fit |
| 5 | Engine ko pakka kana | the foundation work |
| 6 | Device and feature coverage | the 8-step method per feature, the Capability Map, the refusal policy |
| 7 | Existing networks | import, diagnose, repair, enhance |
| 8 | The Havyn model | the intent layer and its guarantees |
| 9 | Training data pipeline | certification, data sources |

## How this relates to the rest of the repository

- The **10 non-negotiable rules** in section 2 are the source of truth; the
  same ten are in `AGENTS.md` so an agent reads them without opening the HTML,
  and in `.github/pull_request_template.md` as checkboxes a reviewer ticks.
- The **PT Capability Map** in section 6 is the contract with the user. A change
  that moves a line on the map updates the map in the same pull request — see
  the Capability Map checklist in the PR template.
- The **refusal policy** in section 6 is why the engine raises instead of
  emitting config PT would accept and ignore. `compiler/firewall_policy.py` is
  one concrete instance: it refuses a malformed ACL operand or a transport
  port on icmp, because PT takes such a line and then matches nothing (or
  aborts the block). Note the S1.16 live probe: an *address-scoped* rule is
  NOT refused — PT's ASA matches addresses, so those render in netmask syntax.

## Open notes against blueprint tasks

### S1.12 — `DEVICE_TYPE_CODES`: four keys sitting on PT type 1

> S1.12: switch, patch_panel, wall_mount, power_dist sab PT type 1 pe map hain;
> live probe se sahi codes confirm karne hain. power_dist ka koi model nahi
> (dead entry).

Spelled out against `src/network_autopilot/adapter/packet_tracer/js_commands.py`
(`DEVICE_TYPE_CODES`, lines 24 and 60-62):

| Key | Mapped to | Status |
| --- | --- | --- |
| `switch` | 1 | Real PT switch type — correct, pinned by `test_upstream_js_commands.py`. |
| `patch_panel` | 1 | **Placeholder.** `Copper-Patch-Panel` is the only catalogued model of this category (`pt/catalog/devices.py`), and its true PT type has never been read back from a live probe. |
| `wall_mount` | 1 | **Placeholder.** No catalogued model carries this category at all. |
| `power_dist` | 1 | **Placeholder and dead entry.** No catalogued model carries `category="power_dist"`, so the production path (`device_type_code_for_model()`) can never return this key — it is reachable only by literal key lookup. |

The three non-switch keys are guesses parked on the switch's type id. The
S1.12 auto-probe — create every model from `allDeviceTypes`, read its real
device type back, diff against the catalog — is what turns them into verified
codes; until a probe says otherwise, none of the three counts as confirmed.

## Editing the blueprint

**This folder is the source of truth.** The blueprint is committed here; a copy
kept at the repository root is an untracked working file (it is gitignored, like
`ISSUES.md` and `QA_REPORT.md`), so the repository never carries two copies that
can drift apart. To revise the plan, edit the file in this folder.
