# GitHub Actions workflows

## Directory role

These files are the CI boundary for CoreMesh quality gates. They are
configuration modules interpreted by GitHub Actions, not scripts called by the
gateway or runtime.

| Workflow | Intended role | Current behavior |
| --- | --- | --- |
| <code>model-regression-ci.yml</code> | Run golden-dataset evaluations when prompts/runtime/gateway change and reject material regressions. | Active on <code>pull_request</code> path filters and <code>workflow_dispatch</code>. |
| <code>self-healing-docs.yml</code> | Detect structural code changes and repair mapped documentation on pull requests. | Disabled: no triggers or jobs. |

## Model regression gate

The active workflow:

1. Checks out PR base and head revisions.
2. Boots Postgres/Redis/Qdrant/runtime/gateway via <code>docker compose --profile app</code> for each revision (<code>COREMESH_CHAT_STUB=true</code> for deterministic chat bodies).
3. Seeds deterministic CI rows into <code>golden_datasets</code> and evaluates every table row through the gateway.
4. Compares reports and fails when candidate overall accuracy is below <code>REGRESSION_MIN_ACCURACY</code> (default <code>0.90</code>) or any tracked accuracy drops by more than <code>REGRESSION_MAX_ACCURACY_DROP</code> (default <code>0.03</code>).

Optional secrets: <code>OPENAI_API_KEY</code> for mined <code>llm_judge</code> rows; <code>SLACK_WEBHOOK_URL</code> for non-blocking alerts.

## Before activating a new workflow

Document its exact event triggers, least-privilege permissions, required
secrets, artifact retention, concurrency/cancellation policy, and failure
behavior. Any workflow that writes to a branch or pull request must explain
that mutation in its file header and guard against untrusted forked code.

The project blueprint describes desired end states, but the checked-in
configuration remains the source of truth for what automation actually runs.
