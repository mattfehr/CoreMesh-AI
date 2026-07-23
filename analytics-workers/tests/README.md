# Analytics worker tests

The default suite is offline and uses injected providers/repositories. It also
runs HDBSCAN over deterministic vectors for two clusters plus one noise point.

`test_fine_tuner.py` keeps data/configuration checks lightweight. With
`requirements-fine-tuner.txt` installed, its CPU tier trains a local tiny
Llama adapter without network or W&B access. Real NF4 is skipped unless
`RUN_FINE_TUNER_GPU_TESTS=1` and CUDA are available; that test trains twice and
checks retained allocator memory.

Set `LOG_MINER_TEST_POSTGRES_DSN` to a disposable, initialized CoreMesh database
to enable isolated legacy-schema migration, JSONB/UUID, lease
takeover/fencing, embedding-cache retention, transaction, evolving membership,
and unique-key integration coverage. Tests never contact OpenAI.
