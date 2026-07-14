# Repository automation

This directory owns repository-hosted automation rather than application
runtime behavior. GitHub reads workflow definitions from
<code>.github/workflows</code>; no CoreMesh process imports these files.

## Current state

Both checked-in workflows are documented placeholders and are deliberately
inert: each has empty triggers and no jobs. They express roadmap integration
points without implying that regression evaluation or automatic documentation
repair is already running.

See [the workflow guide](workflows/README.md) for activation prerequisites and
the side effects that future implementations must make explicit.
