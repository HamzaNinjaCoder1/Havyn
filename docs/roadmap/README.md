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
  one concrete instance: it refuses an address-scoped ASA rule because PT takes
  the line and then matches nothing.

## Editing the blueprint

**This folder is the source of truth.** The blueprint is committed here; a copy
kept at the repository root is an untracked working file (it is gitignored, like
`ISSUES.md` and `QA_REPORT.md`), so the repository never carries two copies that
can drift apart. To revise the plan, edit the file in this folder.
