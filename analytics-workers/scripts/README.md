# Manual verification

Run `python scripts/verify_log_miner.py` from `analytics-workers` against an
initialized CoreMesh PostgreSQL database. The script applies the idempotent
migration, inserts seven safe synthetic failures, runs deterministic providers
through real HDBSCAN, asserts three promoted cases, reruns the full population
with zero new embedding inputs, proves no duplicate rows, and cleans up source
rows and derived vectors.

Use `--keep-fixtures` to inspect rows afterward. `--live-openai` is the only
path that contacts OpenAI and requires `OPENAI_API_KEY`; deterministic database
assertions still run in that mode.
