# Production log miner

## Planned role

The log miner is the Phase 4.1 feedback loop described by the CoreMesh
blueprint. Its intended job is to sample poor or unusual production
interactions, redact sensitive data, cluster related failures, deduplicate
candidates, and place high-value examples into a human-reviewed evaluation
queue.

## Current state

Only a <code>.gitkeep</code> marker exists. There is no reader, schema,
schedule, queue, database mutation, or executable command.

## Requirements for a future implementation

A real worker should document its input event schema, source and destination
stores, sampling window, PII handling, deterministic deduplication key,
checkpoint semantics, retry/dead-letter behavior, and the human approval step.
Writes to <code>golden_datasets</code> should be explicit and auditable; raw
model logs must not silently become trusted test expectations.
