# GitHub Actions workflows

## Directory role

These files are the CI boundary for CoreMesh quality gates. They are
configuration modules interpreted by GitHub Actions, not scripts called by the
gateway or runtime.

| Workflow | Intended role | Current behavior |
| --- | --- | --- |
| <code>model-regression-ci.yml</code> | Run golden-dataset evaluations when prompts/runtime/gateway change and reject material regressions. | Active on <code>pull_request</code> path filters and <code>workflow_dispatch</code>. |
| <code>self-healing-docs.yml</code> | Detect structural code changes and repair mapped documentation on pull requests. | Active for Python, Go, Compose, and analyzer changes on <code>pull_request</code>. |

## Model regression gate

The active workflow:

1. Checks out PR base and head revisions.
2. Boots Postgres/Redis/Qdrant/runtime/gateway via <code>docker compose --profile app</code> for each revision (<code>COREMESH_CHAT_STUB=true</code> for deterministic chat bodies).
3. Seeds deterministic CI rows into <code>golden_datasets</code> and evaluates every table row through the gateway.
4. Compares reports and fails when candidate overall accuracy is below <code>REGRESSION_MIN_ACCURACY</code> (default <code>0.90</code>) or any tracked accuracy drops by more than <code>REGRESSION_MAX_ACCURACY_DROP</code> (default <code>0.03</code>).

Optional secrets: <code>OPENAI_API_KEY</code> for mined <code>llm_judge</code> rows; <code>SLACK_WEBHOOK_URL</code> for non-blocking alerts.

## Self-healing documentation

The active documentation workflow compares the pull request base and head
directly from Git objects. Its repository-local Python package parses Python,
Go, and Compose structures, maps deltas to tracked Markdown blocks with exact
references and embeddings, and uses three typed model passes to assess,
rewrite, and independently validate a correction. The implementation and
local commands are documented in
[the script guide](../scripts/README.md).

Trusted same-repository pull requests run offline tests before any provider
call. The job defaults to <code>gpt-5.6-luna</code> with low reasoning,
<code>text-embedding-3-small</code>, a <code>0.45</code> similarity floor, and
<code>0.90</code> assessment and validation confidence floors. It needs the
repository <code>OPENAI_API_KEY</code> secret when a structural change reaches
retrieval. Selected structural deltas, Markdown blocks, and neighboring style
samples leave the GitHub runner for OpenAI.

Only a body-only, mechanically valid repair to an existing tracked
<code>.md</code> block can reach the worktree. Added/removed structures,
low-confidence findings, complex proposals, validation failures, and ambiguous
matches remain advisory. Before committing, the workflow compares the
worktree to <code>applied-paths.txt</code>, stages each reported path
individually, runs <code>git diff --cached --check</code>, and pushes a normal
non-force commit to the actual PR branch. A concurrent branch update therefore
fails safely. The code-only path filter and GitHub-token push prevent a
documentation repair from looping.

Fork and Dependabot pull requests receive a read-only informational job with no
checkout, secret, provider call, or push permission. The trusted job alone has
<code>contents: write</code> and <code>pull-requests: write</code>. Runs are
cancelled when superseded, bounded to 20 minutes, and always upload
<code>report.json</code>, <code>summary.md</code>,
<code>changes.patch</code>, and the internal path allowlist for 14 days. One
marker-based PR comment is updated with verified, repaired, review-only, or
failure results.

Missing credentials, parsing/provider failures, unsafe patches, unexpected
worktree changes, and rejected pushes fail without a successful branch update.
A well-formed low-confidence or complex finding is an advisory passing result.

## Workflow maintenance

For every workflow, document its exact event triggers, least-privilege
permissions, required secrets, artifact retention, concurrency/cancellation
policy, and failure behavior. Any workflow that writes to a branch or pull
request must explain that mutation in its file header and guard against
untrusted forked code.

The project blueprint describes desired end states, but the checked-in
configuration remains the source of truth for what automation actually runs.
