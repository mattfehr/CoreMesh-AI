# Consensus arbitration

Arbitration is the final delivery gate for supervisor output. The default pool
evaluates factual quality with OpenAI, logic with Anthropic, and completeness
with Ollama in parallel, then uses an OpenAI adjudicator when any quality or
availability trigger fires.

## Decision policy

- Each assessment has a 1-5 score, anomalies, dimension, and confidence.
- Transient provider/network failures retry with bounded exponential backoff.
- All critics share one overall timeout.
- At least two successful assessments are required for quorum.
- A clean quorum releases the original text.
- Low scores, anomalies, or critic failures require adjudication.
- Adjudication can release original, release remediated text, block, or request
  manual review.
- Timeout, insufficient quorum, or adjudicator failure blocks delivery with a
  safe replacement response.

The verdict retains assessments, failures, triggers, confidence, provider
adjudication, and a stable payload hash for later audit. No durable verdict
store or human-review consumer exists today.

Default calls can transmit original prompts and generated responses outside the
local process and incur cost. Use injected fake clients in tests and explicitly
assess data-handling requirements before enabling external critics.
