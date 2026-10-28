# Exploratory language pool (outside Schema v1)

Schema v1 has exactly three language labels: `en`, `roman-urdu`, `mixed`. This
directory exists so other languages can be explored **without** touching that
schema, the gold set, or the training data.

## What this is

- `pool.json` — AI-drafted surface renderings of already engine-verified
  scenarios, in scripts Schema v1 does not carry (Urdu script, Devanagari,
  Arabic).
- Each entry points at its **base candidate** in `../owner-candidates/`: the
  structured scenario behind it is the one the engine has already validated, so
  the *network* is verified by proxy — the *surface text* is not verified at all.
- These entries are **not gold**, **not owner-authored**, **not
  external-authored**, and **not training data**. They are exploratory only.

## Honest limits (read before using)

1. **No engine check applies.** A language value outside the enum fails schema
   validation, so these records cannot pass `validate_gold.py` by construction.
   The base candidate's engine status is the only verification that exists here.
2. **The text is AI-drafted and not native-reviewed.** Sentence rhythm, register
   and terminology need a native speaker's review before any of this is used for
   anything beyond smoke-testing multilingual handling. Treat awkward phrasing
   as a known defect, not a finding.
3. **No fact drift is allowed.** Each rendering carries exactly the facts of its
   base candidate (counts, VLAN separation, DHCP/static, internet type). That
   constraint is what makes the pool usable at all.
4. If these languages are ever promoted, it happens by *extending Intent
   Schema v2 with a reviewed language field* — never by editing v1 in place or
   relabelling these as `mixed`.

## Entry format

```json
{
  "id": "xl-001",
  "base_candidate": "oc-001",
  "base_candidate_engine_verified": true,
  "language_label": "ur-script",
  "script": "Arabic",
  "dir": "rtl",
  "prompt": "...",
  "surface_reviewed_by_native": false,
  "provenance": "ai-drafted rendering of an engine-verified candidate (Model A)",
  "notes": "..."
}
```

## Current contents

9 entries: three scenarios (oc-001 tiny office LAN, oc-028 warehouse segments,
oc-029 two-floor office with DSL) rendered in Urdu script, Hindi (Devanagari)
and Arabic. All `surface_reviewed_by_native: false`.
