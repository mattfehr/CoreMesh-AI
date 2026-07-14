# Analytics workers

## Purpose and system role

This subtree is the reserved home for CoreMesh background optimization jobs.
The architecture blueprint places two offline feedback loops here: production
log curation and targeted model fine-tuning. Neither loop is implemented yet,
and this directory is not referenced by <code>docker-compose.yml</code>.

That distinction matters operationally: no scheduler, queue consumer, training
job, or analytics process starts from the current repository.

## Directory map

| Path | Planned responsibility | Current state |
| --- | --- | --- |
| <code>src/log_miner/</code> | Convert low-quality production interactions into reviewable evaluation candidates. | Documentation and placeholder only. |
| <code>src/fine_tuner/</code> | Build and evaluate PEFT/QLoRA adapters for persistently weak task categories. | Documentation and placeholder only. |

The empty <code>.gitkeep</code> files retain these directories in Git and are
generated-structure markers, not executable modules.

## Integration boundary

Future workers are expected to read explicitly approved operational data and
write versioned datasets, evaluation results, or model artifacts. Before code
is added, define retention/redaction rules, idempotency keys, checkpointing,
resource limits, and promotion/rollback criteria. These jobs must never be
treated as part of the request-serving path.
