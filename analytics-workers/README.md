# Analytics workers

## Purpose and system role

This subtree owns CoreMesh jobs that learn from runtime outcomes without
joining the request-serving path. Phase 4.1 implements the production log
miner; the fine-tuning loop remains a documented placeholder.

The log miner reads privacy-approved interaction records from PostgreSQL,
embeds and clusters failure prompts, creates auditable evaluation candidates,
and promotes only high-confidence labels into <code>golden_datasets</code>.

## Directory map

| Path | Responsibility | State |
| --- | --- | --- |
| <code>src/log_miner/</code> | HDBSCAN curation pipeline, providers, persistence, CLI, and scheduler. | Implemented |
| <code>src/fine_tuner/</code> | PEFT/QLoRA training pipeline. | Placeholder |
| <code>migrations/</code> | Idempotent upgrades for existing PostgreSQL volumes. | Implemented |
| <code>scripts/</code> | Deterministic manual PostgreSQL verification. | Implemented |
| <code>tests/</code> | Offline tests with injected providers/repositories. | Implemented |

Heavy clustering/model dependencies stay in this service and do not leak into
<code>services-runtime</code>.

## Data flow and trust boundary

The runtime publisher is disabled by default. When enabled, it writes only a
redacted prompt, its fingerprint, feature scope, trace ID, and bounded
arbitration metadata to <code>production_interaction_logs</code>. User IDs,
responses, feedback reasons, and raw prompts are not written there.

Each run scans the last 30 days for negative feedback or a minimum arbitration
score below 4. Prompts are partitioned by feature scope, embedded, normalized,
and clustered with HDBSCAN. A cluster medoid represents each systemic pattern;
bounded noise samples represent unique failure vectors.

Generated labels are typed and validated. Confidence at or above 0.80 is
promoted atomically to <code>golden_datasets</code>; lower-confidence labels
remain in <code>log_miner_candidates</code> for review. Stable source
fingerprints make reruns idempotent.

## Local setup and commands

From <code>analytics-workers</code>:

~~~powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python -m src.log_miner.extractor migrate
python -m src.log_miner.extractor run
python -m src.log_miner.extractor schedule
~~~

The production providers require <code>OPENAI_API_KEY</code>. Applying the
migration only requires PostgreSQL. Run offline tests and deterministic
PostgreSQL verification with:

~~~powershell
python -m pytest -q
python scripts/verify_log_miner.py
~~~

The verification script injects synthetic failures, runs real clustering with
deterministic providers, verifies promotion and rerun idempotency, and cleans
up by default. Pass <code>--live-openai</code> only for an explicit paid smoke
test.

## Docker scheduling

The Compose service is opt-in and defaults to 02:00 UTC daily:

~~~powershell
docker compose --profile analytics run --rm log-miner migrate
docker compose --profile analytics up -d log-miner
~~~

Set <code>OPENAI_API_KEY</code> in the shell or root Compose environment; it is
never stored in the manifest. <code>LOG_MINER_CRON</code> and
<code>LOG_MINER_TIMEZONE</code> control the schedule.

## Failure and retention policy

A renewable PostgreSQL lease with heartbeat and write fencing prevents
overlapping or stale workers without holding a connection across provider I/O.
Every eligible row remains in the rolling clustering population; a
prompt/model cache embeds only misses, so old noise can join later systemic
clusters. Provider failures remain retryable, and promotion is transactional.
Source logs, orphaned cached vectors, and unreviewed candidates expire after
30 days; promoted golden cases use the dataset's independent retention policy.
