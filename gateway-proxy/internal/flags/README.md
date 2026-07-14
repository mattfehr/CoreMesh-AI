# Feature flags placeholder

This directory is reserved for the blueprint's general quality-aware rollout
engine. It currently contains only <code>.gitkeep</code>; there is no package,
evaluator, persistence layer, or request middleware.

Autopilot already reads the limited routing fields in
<code>feature_experiments</code>. Do not describe that as a general flag system.
A future implementation must define evaluation inputs, deterministic identity,
quality rollback signals, store consistency, fail-open/fail-closed policy, and
its exact position in the gateway middleware chain.
