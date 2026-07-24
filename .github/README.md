# Repository automation

This directory owns repository-hosted automation rather than application
runtime behavior. GitHub reads workflow definitions from
<code>.github/workflows</code>; no CoreMesh process imports these files.

## Current state

| Workflow | Status |
| --- | --- |
| <code>model-regression-ci.yml</code> | Active. Boots the Compose <code>app</code> profile, evaluates <code>golden_datasets</code>, and fails on accuracy floor or >3% relative drop. |
| <code>self-healing-docs.yml</code> | Active. Detects Python, Go, and Compose structural deltas, retrieves affected Markdown, and commits only high-confidence validated repairs to trusted PR branches. |

Secrets: <code>OPENAI_API_KEY</code> is required when the trusted
self-healing-docs job reaches provider calls and remains optional for
model-regression <code>llm_judge</code> rows.
<code>SLACK_WEBHOOK_URL</code> enables non-blocking regression alerts.
Fork and Dependabot documentation checks receive neither secrets nor checkout
or write access. Selected structural code deltas and Markdown sections are sent
to OpenAI only by the trusted job.

See [the workflow guide](workflows/README.md) for triggers, permissions, data
flow, artifacts, failure policy, and branch-mutation details. The
[self-healing package guide](scripts/README.md) documents the CLI and tests.
