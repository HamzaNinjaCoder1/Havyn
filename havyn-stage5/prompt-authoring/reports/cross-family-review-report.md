# Cross-family review report

> # CROSS-FAMILY REVIEW: BLOCKED
>
> No genuinely different, sufficiently capable model family could be executed in
> the available environment. No verdict was produced, and none was fabricated.

## 1. Reviewer model/family

**None executed.** The preferred reviewer
`mistralai/Mistral-Small-24B-Instruct-2501` (Mistral family) is not runnable
here: 4-bit weights need roughly 14 GB and this machine has **8 GB RAM, no GPU,
2 CPU cores**. Downloading it is also a large model download, which this step
forbids.

No Mistral weights are present locally, and no hosted Mistral/other-family API
key exists in the environment.

## 2. Package SHA

`f7a0a1f4edb1a3a16b990878e2333cc00a5b6a0aa141a6891d8ab418c02bc492`
(56 records, `cross-family-input.jsonl`, unchanged).

## 3–6. Reviewed / ACCEPT / REVIEW / REJECT

| Metric | Value |
| --- | --- |
| Total reviewed | **0** |
| ACCEPT | **0** |
| REVIEW | **0** |
| REJECT | **0** |

## 7. Disagreements with the previous review

**Not applicable:** there are no independent verdicts to compare. The same-base
Model B batches and the post-fix re-check are not cross-family evidence.

## 8. Records requiring action

All **56** records still require an independent-family verdict. No record was
marked ACCEPT, REVIEW or REJECT by an independent reviewer.

## 9. Can the frozen dataset remain unchanged?

**Yes.** No review verdict exists that would require a correction, so
`havyn-train-v1` and the Gold files were not touched. Freeze remains valid.

## 10. Can Stage 5 proceed to Colab?

**No.** The cross-family freeze gate is not met. Do not start bake-off,
training or evaluation until a genuinely different family has reviewed the
package.

## 11. Blocker

The environment has no runnable different-family reviewer:

| Constraint | Observed |
| --- | --- |
| GPU | none (`torch 2.10.0+cpu`, CUDA unavailable) |
| CPU | Intel i5-6300U, 2 cores / 4 threads |
| RAM | 8 GB |
| Mistral weights | none |
| Ollama / LM Studio | not installed |
| Hosted API keys | none |
| Cached generative models | TinyLlama-1.1B, flan-t5-small placeholder, tiny-random test model |
| Large downloads allowed in this step | no |

Models considered:

| Model | Family | Verdict | Reason |
| --- | --- | --- | --- |
| `mistralai/Mistral-Small-24B-Instruct-2501` | Mistral | NOT FEASIBLE | ~14 GB at 4-bit > 8 GB RAM; no GPU; download disallowed |
| `mistralai/Mistral-7B-Instruct-v0.3` | Mistral | NOT FEASIBLE | no local weights; ~4 GB download disallowed; 2-core CPU inference unreasonable; bitsandbytes absent |
| `TinyLlama/TinyLlama-1.1B-Chat-v1.0` | Llama-2-derived | INVALID PER INSTRUCTION | only local generative model; prior one-record smoke timed out after 10 minutes; task says not to treat as valid |
| `google/flan-t5-small` | T5 | INVALID PER INSTRUCTION | explicitly excluded; cached copy is a 9 KB placeholder |
| `hf-internal-testing/tiny-random-LlamaForCausalLM` | test-only | INVALID | random weights; meaningless output |


## Executor prepared (still not run)

The review is now one command on a GPU runtime:

- Script: `havyn-stage5/scripts/cross_family_review.py`
- Notebook: `havyn-stage5/notebooks/12-cross-family-review.ipynb`
- Offline precheck: **56/56 engine-verified, 0 issues**, 56 baseline entries
  (55 ACCEPT / 1 REVIEW / 0 REJECT after the resolved decisions). See
  `review/cross-family-precheck.json`.
- Command:
  `REVIEW_MODEL_ID=mistralai/Mistral-Small-24B-Instruct-2501 python havyn-stage5/scripts/cross_family_review.py --model "$REVIEW_MODEL_ID" --load-in-4bit`

The executor records model id/family/revision, environment, input SHA,
timestamp and raw output; it refuses a Qwen-family reviewer and marks any
unparseable output as UNPARSED rather than inventing a verdict.

## What would unblock it

1. Run the prepared protocol on a **Colab GPU** runtime with
   `REVIEW_MODEL_ID=mistralai/Mistral-Small-24B-Instruct-2501` (24 GB class),
   or a 4-bit 7B Mistral on ≥8 GB VRAM.
2. Or supply a **hosted Mistral-family endpoint/API key** and run the same
   56-record package there.
3. Record model id, revision, inference environment, temperature, package SHA,
   timestamp and raw per-record output.

## Honest boundaries

- No GPU was used.
- No model weights were downloaded.
- Nothing was published.
- The frozen dataset was not modified.
- No verdict, approval or human authorship was invented.
