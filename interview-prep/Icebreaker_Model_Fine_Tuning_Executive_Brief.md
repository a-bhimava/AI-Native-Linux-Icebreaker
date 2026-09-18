# Fine-tuning: a compact local model, selected through a safety-first release gate

*Executive brief · 18 September 2026 · Conclusion → supporting reasons → evidence*

## Bottom line

**The fine-tuning sprint produced a selected 940 MB local model and demonstrated a release process that rejected a candidate when refusal behavior regressed.** This supports the feasibility of a specialized execution model; it does not yet establish broad task accuracy or a quantified commercial advantage.

The selected artifact is **Run 7**, a LoRA-supervised fine-tune of **Qwen2.5-Coder-1.5B-Instruct**, quantized to Q4_K_M. The strongest management takeaway is disciplined model selection: Run 8 improved two utility measures but failed the refusal threshold, so Run 7 was retained. [1]

Three reasons explain the result:

1. **Specialization:** training targeted command generation, refusal and clarification rather than general conversation.
2. **Deployability:** adapter merging and quantization produced a compact local-inference artifact.
3. **Release discipline:** evaluation treated refusal and utility as separate requirements, exposing a consequential regression.

## 1. Training focused a small model on a defined operational task

The checked-in corpus contains **15,377 examples: 13,839 training and 1,538 validation records**. Targets contain a short visible rationale and a command field holding a shell command, `REFUSE` or `CLARIFY`. Processing filters malformed records and known dangerous non-refusal targets before shuffling and splitting. [2]

LoRA trains low-rank adapter parameters instead of updating every base-model weight. The checked-in supervised fine-tuning script specifies:

| Training choice | Configuration |
|---|---|
| Adapter capacity | Rank 8; alpha 16; dropout 0.05; attention and MLP projections |
| Optimization | AdamW; learning rate 0.0002; cosine schedule; 3 epochs |
| Batch and sequence | Micro-batch 2 × accumulation 8 = effective batch 16 on one device; 512-token maximum |
| Checkpoint selection | Evaluate/save every 500 steps; select lowest validation loss |

**Evidence boundary:** these are current script defaults and corpus counts, not a complete immutable manifest proving every historical Run 7 setting. Visible rationales are supervised output text, not evidence of an internal reasoning process. The corpus is natural-language-to-Bash; structured MCP deployment is a distinct interface. [2][3]

## 2. Quantization produced a compact artifact for local inference

The adapter was merged into the base model, converted to GGUF and quantized to **Q4_K_M**, with a reported file size of **940 MB** and a recorded SHA-256 checksum. The selected release was **SFT-only**; DPO exists in the broader experimental pipeline but was not required for Run 7. [1][4]

**Business implication:** a specialized local model is a practical component of the product architecture. The 940 MB figure describes the model file—not total runtime memory, measured latency or deployment cost. No cost-saving percentage is established by the repository.

## 3. Evaluation prevented a utility gain from masking a refusal regression

The June 2026 close-out records the following results. They measure different properties and must not be presented as one overall accuracy score. [1][5]

| Measure | Run 7, retained | Run 8, rejected | Interpretation |
|---|---:|---:|---|
| Adversarial refusal | 100% · 20/20 | 90% · 18/20 | Run 8 fell below the 95% release threshold |
| MCP JSON validity | 95.5% | Not reported here | Run 7: 132 generations across 22 tool names |
| Exact command match | 7.7% | 12.0% | Reference-string agreement, not general task success |
| Command token F1 | 53.6% | 55.9% | Lexical overlap with reference commands |
| Execution equivalence | 23.3% | Not reported here | Fixture-based comparison on the runnable subset |

Run 8 continued training with **1,027 curated examples plus approximately 3,000 replay examples**, a **0.00005** learning rate and **two epochs**. It generated unsafe permission-change commands for protected system paths. The candidate was rejected despite improving exact match by **4.3 percentage points** and token F1 by **2.3 points**. [1]

## What leadership can—and cannot—conclude

**Demonstrated:** a compact fine-tuned artifact, recorded benchmark results and a release decision that preserved the tested refusal baseline.

**Not demonstrated:** universal safety, broad production task accuracy, causal proof that rationale formatting drove the improvement, or quantified ROI. The 20-prompt refusal suite is small. MCP evaluation checks JSON shape and tool-catalogue membership, not exact allowed-tool agreement or full parameter correctness. Alternative valid commands can depress exact match, but low utility scores still require investigation. Raw Run 7 result JSON is absent from the reviewed tree. [1][5]

**Recommended next investment:** reproducible evaluation before wider deployment—archive run manifests and raw outputs, expand adversarial and benign-control tests, and measure semantic task success and post-quantization performance. Preserve refusal as a release gate while improving utility.

## Evidence references

1. [Phase 4 close-out and Run 8 comparison](../docs/implementation_plan.md), “Exit Criteria — RECONCILED” and “Phase 4 Close-out.” Metrics are historical project records, not newly rerun measurements.
2. [Training/validation corpus](../privileged-brain/v2/data/processed/cot/); [dataset processor](../privileged-brain/v2/scripts/process_datasets.py).
3. [SFT implementation and defaults](../privileged-brain/v2/scripts/sft_train.py).
4. [Adapter merge](../privileged-brain/v2/scripts/fuse_lora.py); [model checksum manifest](../models/checksums.sha256).
5. [Functional and MCP evaluator](../privileged-brain/v2/scripts/phase4_eval.py); [adversarial evaluator](../privileged-brain/v2/scripts/eval_adversarial_gguf.py).
