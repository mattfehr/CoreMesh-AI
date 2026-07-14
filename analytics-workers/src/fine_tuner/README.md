# Fine-tuning pipeline

## Planned role

This directory is reserved for the Phase 4.3 training pipeline. The blueprint
envisions converting reviewed failure clusters into versioned datasets,
training parameter-efficient adapters, comparing them with the current model,
and promoting an artifact only after quality and safety gates pass.

## Current state

Only a <code>.gitkeep</code> marker exists. No training code, dataset loader,
GPU dependency, model download, artifact store, or deployment hook is present.

## Requirements for a future implementation

Keep training dependencies isolated from <code>services-runtime</code>.
Document dataset lineage and licensing, random seeds, base-model revision,
hardware expectations, checkpoint/output paths, network downloads, cost,
evaluation thresholds, and rollback behavior. Training must produce an
immutable manifest that ties each adapter to its inputs and evaluation report.
