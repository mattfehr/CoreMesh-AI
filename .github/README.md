# Repository automation

This directory owns repository-hosted automation rather than application
runtime behavior. GitHub reads workflow definitions from
<code>.github/workflows</code>; no CoreMesh process imports these files.

## Current state

| Workflow | Status |
| --- | --- |
| <code>model-regression-ci.yml</code> | Active. Boots the Compose <code>app</code> profile, evaluates <code>golden_datasets</code>, and fails on accuracy floor or >3% relative drop. |
| <code>self-healing-docs.yml</code> | Still inert (empty triggers/jobs). |

Optional secrets: <code>OPENAI_API_KEY</code> (required only for <code>llm_judge</code> rows), <code>SLACK_WEBHOOK_URL</code> (non-blocking alerts).

See [the workflow guide](workflows/README.md) for triggers, permissions, and gate details.
