# Owner decisions — technical resolution

Date: 2026-10-29.

> **No owner approval is recorded.** These eight records have a technical
> resolution applied to the candidate pool. They are still AI-assisted
> candidates (`origin: ai_assisted_candidate`, `owner_adoption: pending`) and
> `owner_confirmation_required: true`. Nothing here is owner-authored Gold.

## What was applied

| ID | Recommendation | Applied action | Owner approval | Owner action |
| --- | --- | --- | --- | --- |
| oc-007 | REFUSAL | Stays a refusal candidate; null intent; names `guest-isolation-unsupported` | false | confirm or reject |
| oc-012 | BUILD | Stays buildable; finance isolation recorded UNKNOWN | false | confirm or reject |
| oc-013 | BUILD | Stays buildable; accounts isolation and factory internet recorded UNKNOWN | false | confirm or reject |
| oc-028 | BUILD_AFTER_REWRITE | Rewritten prompt (tablets for handhelds, WMS as HTTP server) stays buildable | false | confirm or reject |
| oc-035 | BUILD | Stays buildable; duplicate topology with oc-030 documented | false | confirm or reject |
| oc-040 | BUILD | Stays buildable; IoT lighting separation recorded UNKNOWN | false | confirm or reject |
| oc-053 | BUILD | Stays buildable; auditor isolation and WAN scope recorded UNKNOWN | false | confirm or reject |
| oc-054 | REFUSAL | Stays a multi-reason refusal (`guest-isolation-unsupported`, camera kind, second internet line) | false | confirm or reject |

Counts: 6 BUILD (one after rewrite), 2 REFUSAL, **0 owner approvals**.

## Why this is not fabricated consent

- The recommendation and its basis are the ones already documented in
  `owner_decisions.json` / `owner_decisions.md`.
- Applying a technical resolution is a pool-maintenance action, not an owner
  decision. `owner_approval` is `false` on every record, and
  `owner_confirmation_required` remains `true`.
- The owner still has to confirm or reject each recommendation, rewrite the
  prompt in their own words, and adopt it with the provenance block in
  `datasets/gold-v1/README.md`.

## Pool state after resolution

- 56 candidates, **56/56** engine-verified (34 buildable + 22 refusal).
- 0 duplicates, 0 unpinned multi-VLAN groups.
- 56 adoption-pending drafts in `accepted-candidates/`; the 8 resolved records
  carry `decision.resolution_status: technical_recommendation_applied`,
  `decision.owner_approval: false`, and an explicit owner action.
- 0 owner-authored Gold records.
