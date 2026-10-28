# Post-fix semantic re-check — the 16 remaining REVIEW candidates

Reviewed 2026-10-29 against the emitted candidate JSON (working tree after the
Stage-5 fix pass). Engine commit at review time:
`e8503cdfb32a83817a2fbc84ad4245057acf75a2` (HEAD).

## What this is, and what it is not

This is a **semantic re-check by the same base model that drafted the
candidates and performed Model B**. It is **not** an independent review and it
is **not** cross-family validation. The reviewer had access to drafting
context; a true independent reviewer should see only prompt + intent +
expected behaviour. The per-candidate record is `post-fix-recheck.json`.

The previous Model-B rounds (batch-1..4, plus batch-5) are preserved unchanged.
Several batch-5 REVIEW reasons describe pre-fix text that the shipped revision
no longer contains — for example, oc-030's "the prompt states no endpoints" and
oc-012's "kiosks are silently dropped". Those verdicts are valid for the
revision they saw, but they are **not verdicts on the shipped text**. This
re-check supersedes the *status only*, not the historical record.

## Evidence classes

| Class | Candidates | What was actually run |
| --- | --- | --- |
| Engine-executed buildable chain | oc-004, 005, 006, 012, 013, 028, 030, 035, 040, 053, 056 | schema → intent lint → expander → spec lint → IPAM → compile (`verify_candidates.py`) |
| Expected refusal / clarify, source-traced | oc-007, 044, 048, 049, 054 | `verify_candidates.py` asserts only `expected_intent is null`; the named refusal code was traced in source, not executed against Packet Tracer |
| Live Packet Tracer verification | none | **not run** |

## Outcome

**ACCEPT 8 / REVIEW 8 / REJECT 0** among the 16.

| id | prior | post-fix | residual owner question |
| --- | --- | --- | --- |
| oc-004 | REVIEW | **ACCEPT** | — |
| oc-005 | REVIEW | **ACCEPT** | — |
| oc-006 | REVIEW | **ACCEPT** | — |
| oc-007 | REVIEW | **REVIEW** | Adopt a named refusal for guest-WiFi isolation (source-traced, not live-PT-executed) as a desired eval signal? |
| oc-012 | REVIEW | **REVIEW** | Accept a critical finance-isolation clause that is recorded UNKNOWN (CG-1)? |
| oc-013 | REVIEW | **REVIEW** | Accept a critical accounts-vs-sales isolation clause that is recorded UNKNOWN (CG-1)? |
| oc-028 | REVIEW | **REVIEW** | Accept tablets-for-handhelds/forklifts, WMS out of scope, empty contractor segment, and "internet only" as scope? |
| oc-030 | REVIEW | **ACCEPT** | — |
| oc-035 | REVIEW | **REVIEW** | Keep oc-030 + oc-035 as two slots for one topology (language pair)? |
| oc-040 | REVIEW | **REVIEW** | Keep both smart-LED IoT candidates (office + retail) despite the similar shape? |
| oc-044 | REVIEW | **ACCEPT** | — |
| oc-048 | REVIEW | **ACCEPT** | — |
| oc-049 | REVIEW | **ACCEPT** | — |
| oc-053 | REVIEW | **REVIEW** | Accept UNKNOWN auditor isolation and accounting-only WAN scope? |
| oc-054 | REVIEW | **REVIEW** | Is refusal (not build + UNKNOWN) the single correct answer for guest isolation? |
| oc-056 | REVIEW | **ACCEPT** | — |

## Objective defects fixed in this pass

1. **oc-004** — expected behaviour said "NAT at each"; the engine builds one
   WAN internet block at `sites[0]` (CG-3). Corrected.
2. **oc-056** — expected behaviour left an open "may ask" menu; replaced with a
   definite build statement.
3. **oc-027 and oc-054** — the refused shapes invented a `camerapanel` PC that
   neither prompt mentions. Removed; the 3 cameras remain as the forced
   `smart_led` mapping. (oc-027 is outside the 16 but shared the artifact.)

## What would count as independent

A different model family, given only prompt + intent + expected behaviour,
re-deriving the requirements and producing ACCEPT/REVIEW/REJECT. See
`cross-family-review-protocol.md` for the exact package and prompt.
