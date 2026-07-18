# Analytics worker tests

The default suite is offline and uses injected providers/repositories. It also
runs HDBSCAN over deterministic vectors for two clusters plus one noise point.

Set `LOG_MINER_TEST_POSTGRES_DSN` to a disposable, initialized CoreMesh database
to enable isolated legacy-schema migration, JSONB/UUID, lease
takeover/fencing, embedding-cache retention, transaction, evolving membership,
and unique-key integration coverage. Tests never contact OpenAI.
