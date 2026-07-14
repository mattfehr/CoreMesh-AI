# GitHub Actions workflows

## Directory role

These files are the future CI boundary for CoreMesh quality gates. They are
configuration modules interpreted by GitHub Actions, not scripts called by the
gateway or runtime.

| Workflow | Intended role | Current behavior |
| --- | --- | --- |
| <code>model-regression-ci.yml</code> | Run golden-dataset evaluations when prompts change and reject material regressions. | Disabled: no triggers or jobs. |
| <code>self-healing-docs.yml</code> | Detect structural code changes and repair mapped documentation on pull requests. | Disabled: no triggers or jobs. |

## Before activating a workflow

Document its exact event triggers, least-privilege permissions, required
secrets, artifact retention, concurrency/cancellation policy, and failure
behavior. Any workflow that writes to a branch or pull request must explain
that mutation in its file header and guard against untrusted forked code.

The project blueprint describes desired end states, but the checked-in
configuration remains the source of truth for what automation actually runs.
