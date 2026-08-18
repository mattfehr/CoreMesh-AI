# Consensus arbitration

Arbitration is the final delivery gate for supervisor output.
<code>ARBITRATION_MODE=external</code>, the default, evaluates factual quality
with OpenAI, logic with Anthropic, and completeness with Ollama in parallel,
then uses an OpenAI adjudicator when any quality or availability trigger fires.

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

## Deterministic validation mode

<code>ARBITRATION_MODE=deterministic</code> makes no provider call, does not
inspect user or specialist content, and consumes only categorical workflow
metadata. A healthy completed workflow receives three score-5 assessments. A
failed or skipped observation gives the completeness critic score 2 with an
<code>incomplete_specialist_output</code> anomaly, and the deterministic
adjudicator blocks delivery. This mode exists for repeatable integration and
demo validation; it is not a substitute for production quality review.

External-mode calls can transmit original prompts and generated responses
outside the local process and incur cost. Use injected fake clients in tests
and explicitly assess data-handling requirements before enabling external
critics.
