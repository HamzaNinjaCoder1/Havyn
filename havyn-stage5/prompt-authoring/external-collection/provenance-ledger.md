# Provenance ledger — external gold contributors

Every external record must be traceable to a real person through this ledger.
Pseudonymous ids only in gold records and submissions; contact details stay in
the collector's private store (not in this repo).

## Rules

1. No id is created without a recorded consent (scope, date, withdrawal window).
2. `method` must say how the words were captured: `typed`, `dictated-transcribed`
   or `handwritten-transcribed`. Transcribed means *their* words, verbatim.
3. Submissions with AI involvement in the wording (even partial) are recorded
   with `ai_assisted_in_authoring: yes` and **cannot** be adopted as
   external-authored gold.
4. Withdrawal before adoption deletes the row and the submission; after adoption
   it is recorded as a revocation with the date.

## Ledger

| contributor_id | relationship / how reached | languages | consent_date | withdrawal_window | method | submissions | adopted (gold ids) | notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| _example-ext-001_ | colleague at <org> | en, roman-urdu | YYYY-MM-DD | until adoption | typed | sub-ext-001-1..3 | — | — |

## Adoption record (filled when a submission becomes gold)

| gold_id | submission_id | contributor_id | adopted_by | adopted_at | engine_commit | validator_output |
| --- | --- | --- | --- | --- | --- | --- |
| _gold-101_ | _sub-ext-001-1_ | _ext-001_ | _Hamza_ | _YYYY-MM-DD_ | _<sha>_ | _validate_gold.py: PASS_ |
