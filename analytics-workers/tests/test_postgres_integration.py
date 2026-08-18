"""Opt-in PostgreSQL contract test for the idempotent migration and repository."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from src.log_miner.models import (
    Candidate,
    CandidateStatus,
    DifficultyRating,
    ExpectedBehavior,
    ExpectedOutput,
    RunClaimLostError,
    RunStatus,
    RunSummary,
    ValidationCriterion,
)
from src.log_miner.repository import (
    PostgresLogMinerRepository,
    apply_migration,
    check_log_source,
)


POSTGRES_DSN = os.getenv("LOG_MINER_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="set LOG_MINER_TEST_POSTGRES_DSN to a disposable initialized database",
)


def test_bootstrap_then_repeated_migration_preserves_log_miner_catalog() -> None:
    """Prove fresh-volume bootstrap and upgrade migration remain compatible."""

    assert POSTGRES_DSN is not None
    schema = f"log_miner_bootstrap_{uuid4().hex[:12]}"
    admin_engine = create_engine(POSTGRES_DSN)
    isolated_engine = None
    try:
        with admin_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))

        isolated_url = make_url(POSTGRES_DSN).update_query_dict(
            {"options": f"-csearch_path={schema},public"}
        )
        isolated_dsn = isolated_url.render_as_string(hide_password=False)
        isolated_engine = create_engine(isolated_dsn)
        bootstrap_path = Path(__file__).resolve().parents[2] / "init.sql"
        raw_connection = isolated_engine.raw_connection()
        try:
            cursor = raw_connection.cursor()
            try:
                cursor.execute(bootstrap_path.read_text(encoding="utf-8"))
                raw_connection.commit()
            except Exception:
                raw_connection.rollback()
                raise
            finally:
                cursor.close()
        finally:
            raw_connection.close()

        apply_migration(isolated_dsn)
        apply_migration(isolated_dsn)

        with isolated_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO production_interaction_logs (
                        trace_id, feature_scope, redacted_prompt,
                        prompt_fingerprint, min_arbitration_score
                    ) VALUES
                        ('health-eligible', 'integration', 'redacted fixture',
                         :eligible_fingerprint, 2),
                        ('health-ineligible', 'integration', 'redacted fixture',
                         :ineligible_fingerprint, 5)
                    """
                ),
                {
                    "eligible_fingerprint": "a" * 64,
                    "ineligible_fingerprint": "b" * 64,
                },
            )

        assert check_log_source(isolated_dsn) == 1

        with isolated_engine.connect() as connection:
            tables = set(
                connection.execute(
                    text(
                        """
                        SELECT table_name
                        FROM information_schema.tables
                        WHERE table_schema = :schema
                        """
                    ),
                    {"schema": schema},
                ).scalars()
            )
            constraints = set(
                connection.execute(
                    text(
                        """
                        SELECT constraint_name
                        FROM information_schema.table_constraints
                        WHERE table_schema = :schema
                        """
                    ),
                    {"schema": schema},
                ).scalars()
            )
            indexes = set(
                connection.execute(
                    text(
                        """
                        SELECT indexname
                        FROM pg_indexes
                        WHERE schemaname = :schema
                        """
                    ),
                    {"schema": schema},
                ).scalars()
            )

        assert {
            "golden_datasets",
            "production_interaction_logs",
            "log_miner_runs",
            "log_miner_leases",
            "log_miner_embedding_cache",
            "log_miner_candidates",
            "log_miner_candidate_members",
        } <= tables
        assert {
            "production_interaction_logs_pkey",
            "production_interaction_feature_scope_nonblank",
            "production_interaction_prompt_nonblank",
            "production_interaction_score_bounds",
            "log_miner_run_status",
            "log_miner_lease_ownership",
            "log_miner_embedding_width",
            "log_miner_candidate_confidence",
            "log_miner_candidate_status",
        } <= constraints
        assert {
            "production_interaction_logs_eligible_idx",
            "production_interaction_logs_prompt_fingerprint_idx",
            "golden_datasets_source_fingerprint_uq",
            "log_miner_embedding_cache_prompt_idx",
            "log_miner_candidates_review_idx",
            "log_miner_candidates_representative_trace_idx",
            "log_miner_candidate_members_trace_idx",
        } <= indexes
    finally:
        if isolated_engine is not None:
            isolated_engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin_engine.dispose()


def test_migration_upgrades_isolated_legacy_schema_and_resyncs_members() -> None:
    assert POSTGRES_DSN is not None
    schema = f"log_miner_legacy_{uuid4().hex[:12]}"
    admin_engine = create_engine(POSTGRES_DSN)
    url = make_url(POSTGRES_DSN)
    isolated_url = url.update_query_dict(
        {"options": f"-csearch_path={schema},public"}
    )
    isolated_dsn = isolated_url.render_as_string(hide_password=False)
    isolated_engine = None
    try:
        with admin_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(
                text(
                    f"""
                    CREATE TABLE "{schema}".golden_datasets (
                        case_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        feature_scope VARCHAR(64) NOT NULL,
                        user_input TEXT NOT NULL,
                        expected_output JSONB NOT NULL,
                        difficulty_rating VARCHAR(16) NOT NULL,
                        origin_source VARCHAR(32) NOT NULL,
                        created_at TIMESTAMP WITHOUT TIME ZONE
                            DEFAULT TIMEZONE('utc', NOW())
                    )
                    """
                )
            )

        apply_migration(isolated_dsn)
        isolated_engine = create_engine(isolated_dsn)
        with isolated_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO log_miner_candidates (
                        source_fingerprint, feature_scope, user_input,
                        representative_trace_id, member_trace_ids,
                        expected_output, difficulty_rating, label_confidence,
                        status, provenance
                    ) VALUES (
                        :fingerprint, 'legacy', 'Legacy prompt', 'representative',
                        CAST(:members AS JSONB), '{}'::jsonb, 'simple', 0.5,
                        'pending_review', '{"member_count": 999}'::jsonb
                    )
                    """
                ),
                {
                    "fingerprint": "c" * 64,
                    "members": json.dumps(["member", "member"]),
                },
            )

        # A rerun upgrades the normalized membership representation and proves
        # the full migration remains idempotent on an existing Phase 4.1 shape.
        apply_migration(isolated_dsn)
        with isolated_engine.connect() as connection:
            golden_columns = set(
                connection.execute(
                    text(
                        """
                        SELECT column_name FROM information_schema.columns
                        WHERE table_schema = :schema
                          AND table_name = 'golden_datasets'
                        """
                    ),
                    {"schema": schema},
                ).scalars()
            )
            candidate = connection.execute(
                text(
                    """
                    SELECT member_trace_ids, provenance
                    FROM log_miner_candidates
                    WHERE source_fingerprint = :fingerprint
                    """
                ),
                {"fingerprint": "c" * 64},
            ).mappings().one()
            normalized_count = connection.execute(
                text("SELECT COUNT(*) FROM log_miner_candidate_members")
            ).scalar_one()
        assert {"source_fingerprint", "provenance"} <= golden_columns
        assert candidate["member_trace_ids"] == ["member", "representative"]
        assert candidate["provenance"]["member_count"] == 2
        assert normalized_count == 2
    finally:
        if isolated_engine is not None:
            isolated_engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin_engine.dispose()


def test_postgres_json_uuid_lease_cache_transaction_and_idempotency() -> None:
    assert POSTGRES_DSN is not None
    engine = create_engine(POSTGRES_DSN)
    scope = f"integration-{uuid4().hex[:12]}"
    trace_id = f"trace-{uuid4().hex}"
    fingerprint = uuid4().hex + uuid4().hex
    shared_prompt_fingerprint = uuid4().hex + uuid4().hex
    orphan_prompt_fingerprint = uuid4().hex + uuid4().hex
    pending_source_fingerprint = uuid4().hex + uuid4().hex
    fixture_prompt_fingerprints = [
        "f" * 64,
        shared_prompt_fingerprint,
        orphan_prompt_fingerprint,
    ]
    run_ids = []

    # Ensure the baseline table exists when the disposable target starts empty;
    # the isolated-schema test above covers a true legacy upgrade.
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS golden_datasets (
                    case_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    feature_scope VARCHAR(64) NOT NULL,
                    user_input TEXT NOT NULL,
                    expected_output JSONB NOT NULL,
                    difficulty_rating VARCHAR(16) NOT NULL,
                    origin_source VARCHAR(32) NOT NULL,
                    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT TIMEZONE('utc', NOW())
                )
                """
            )
        )

    apply_migration(POSTGRES_DSN)
    apply_migration(POSTGRES_DSN)
    repository = PostgresLogMinerRepository(POSTGRES_DSN)
    now = datetime.now(timezone.utc)

    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO production_interaction_logs (
                        trace_id, feature_scope, redacted_prompt, prompt_fingerprint,
                        arbitration_scores, min_arbitration_score, arbitration_status
                    ) VALUES (
                        :trace_id, :feature_scope, :prompt, :prompt_fingerprint,
                        CAST(:scores AS JSONB), 2, 'approved'
                    )
                    """
                ),
                {
                    "trace_id": trace_id,
                    "feature_scope": scope,
                    "prompt": "Safe synthetic low-score prompt",
                    "prompt_fingerprint": "f" * 64,
                    "scores": json.dumps({"accuracy": 2}),
                },
            )

        records = repository.fetch_eligible(
            window_start=now - timedelta(days=1),
            window_end=now + timedelta(minutes=1),
            score_threshold=4,
        )
        record = next(item for item in records if item.trace_id == trace_id)
        assert record.arbitration_scores == {"accuracy": 2}

        claim = repository.claim_run(
            lease_name="production-log-miner",
            lease_ttl_seconds=60,
            window_start=now - timedelta(days=30),
            window_end=now,
            configuration={"integration": True},
        )
        assert claim is not None
        run_ids.append(claim.run_id)
        repository_two = PostgresLogMinerRepository(POSTGRES_DSN)
        assert repository_two.claim_run(
            lease_name="production-log-miner",
            lease_ttl_seconds=60,
            window_start=now - timedelta(days=30),
            window_end=now,
            configuration={"integration": "overlap"},
        ) is None
        assert repository.renew_claim(claim=claim, lease_ttl_seconds=60) is True

        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE log_miner_leases
                    SET lease_expires_at = clock_timestamp() - INTERVAL '1 second'
                    WHERE lease_name = :lease_name
                    """
                ),
                {"lease_name": claim.lease_name},
            )
        assert repository.renew_claim(claim=claim, lease_ttl_seconds=60) is False

        recovered = repository_two.claim_run(
            lease_name="production-log-miner",
            lease_ttl_seconds=60,
            window_start=now - timedelta(days=30),
            window_end=now,
            configuration={"integration": "recovered"},
        )
        assert recovered is not None
        run_ids.append(recovered.run_id)
        with pytest.raises(RunClaimLostError):
            repository.store_cached_embeddings(
                claim=claim,
                provider_name="integration",
                embedding_model="fixture-v1",
                input_version="test-v1",
                embeddings={"f" * 64: [1.0, 0.0]},
            )

        repository_two.store_cached_embeddings(
            claim=recovered,
            provider_name="integration",
            embedding_model="fixture-v1",
            input_version="test-v1",
            embeddings={"f" * 64: [1.0, 0.0]},
        )
        cached = repository.load_cached_embeddings(
            prompt_fingerprints=["f" * 64],
            provider_name="integration",
            embedding_model="fixture-v1",
            input_version="test-v1",
        )
        assert cached["f" * 64][0].embedding_values == [1.0, 0.0]
        candidate = Candidate(
            source_fingerprint=fingerprint,
            feature_scope=scope,
            user_input=record.redacted_prompt,
            representative_trace_id=trace_id,
            member_trace_ids=[trace_id],
            cluster_label=0,
            expected_output=ExpectedOutput(
                reference_answer="Synthetic reference answer",
                validation_criteria=[
                    ValidationCriterion(description="Matches the safe fixture")
                ],
                expected_behavior=ExpectedBehavior.ANSWER,
                failure_pattern="Synthetic integration failure",
            ),
            difficulty_rating=DifficultyRating.SIMPLE,
            label_confidence=0.95,
            status=CandidateStatus.PROMOTED,
            provenance={"integration": True},
        )
        first = repository_two.persist_candidate(claim=recovered, candidate=candidate)
        repository_two.merge_candidate_members(
            claim=recovered,
            source_fingerprint=fingerprint,
            member_trace_ids=[trace_id, "later-one", "later-two"],
            cluster_label=0,
            is_noise=False,
            outlier_score=0.0,
            nearest_example_count=2,
        )
        second = repository_two.persist_candidate(claim=recovered, candidate=candidate)
        assert first.created is True
        assert first.candidate_id is not None
        assert first.golden_case_id is not None
        assert second.created is False

        with engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT c.expected_output, c.golden_case_id, g.origin_source, g.provenance
                    FROM log_miner_candidates c
                    JOIN golden_datasets g ON g.case_id = c.golden_case_id
                    WHERE c.source_fingerprint = :fingerprint
                    """
                ),
                {"fingerprint": fingerprint},
            ).mappings().one()
            member_count = connection.execute(
                text(
                    """
                    SELECT COUNT(*) FROM log_miner_candidate_members AS member
                    JOIN log_miner_candidates AS candidate
                      ON candidate.candidate_id = member.candidate_id
                    WHERE candidate.source_fingerprint = :fingerprint
                    """
                ),
                {"fingerprint": fingerprint},
            ).scalar_one()
            expired_status = connection.execute(
                text("SELECT status FROM log_miner_runs WHERE run_id = :run_id"),
                {"run_id": claim.run_id},
            ).scalar_one()
        assert row["expected_output"]["expected_behavior"] == "answer"
        assert row["origin_source"] == "production_miner"
        assert row["provenance"]["integration"] is True
        assert member_count == 3
        assert expired_status == "failed"

        retention_cutoff = now - timedelta(days=30)
        pending_trace_id = f"pending-{uuid4().hex}"
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO production_interaction_logs (
                        trace_id, feature_scope, redacted_prompt,
                        prompt_fingerprint, min_arbitration_score,
                        arbitration_status, created_at
                    ) VALUES
                        (:old_shared, :scope, 'Shared retained prompt',
                         :shared_fingerprint, 2, 'approved', :expired_at),
                        (:new_shared, :scope, 'Shared retained prompt',
                         :shared_fingerprint, 2, 'approved', :retained_at),
                        (:old_orphan, :scope, 'Orphaned cache prompt',
                         :orphan_fingerprint, 2, 'approved', :expired_at)
                    """
                ),
                {
                    "old_shared": f"old-shared-{uuid4().hex}",
                    "new_shared": f"new-shared-{uuid4().hex}",
                    "old_orphan": f"old-orphan-{uuid4().hex}",
                    "scope": scope,
                    "shared_fingerprint": shared_prompt_fingerprint,
                    "orphan_fingerprint": orphan_prompt_fingerprint,
                    "expired_at": now - timedelta(days=31),
                    "retained_at": now - timedelta(days=1),
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO log_miner_candidates (
                        run_id, source_fingerprint, feature_scope, user_input,
                        representative_trace_id, member_trace_ids,
                        expected_output, difficulty_rating, label_confidence,
                        status, provenance, created_at
                    ) VALUES (
                        :run_id, :source_fingerprint, :scope, 'Pending expiry',
                        :trace_id, CAST(:members AS JSONB),
                        CAST(:expected_output AS JSONB), 'simple', 0.50,
                        'pending_review', '{}'::jsonb, :created_at
                    )
                    """
                ),
                {
                    "run_id": recovered.run_id,
                    "source_fingerprint": pending_source_fingerprint,
                    "scope": scope,
                    "trace_id": pending_trace_id,
                    "members": json.dumps([pending_trace_id]),
                    "expected_output": json.dumps(
                        {
                            "reference_answer": "Review fixture",
                            "validation_criteria": [
                                {"description": "fixture", "required": True}
                            ],
                            "expected_behavior": "answer",
                            "failure_pattern": "fixture",
                        }
                    ),
                    "created_at": now - timedelta(days=31),
                },
            )
            connection.execute(
                text(
                    """
                    UPDATE log_miner_candidates
                    SET created_at = :created_at
                    WHERE source_fingerprint = :source_fingerprint
                    """
                ),
                {
                    "created_at": now - timedelta(days=31),
                    "source_fingerprint": fingerprint,
                },
            )

        repository_two.store_cached_embeddings(
            claim=recovered,
            provider_name="integration",
            embedding_model="fixture-v1",
            input_version="test-v1",
            embeddings={
                shared_prompt_fingerprint: [0.0, 1.0],
                orphan_prompt_fingerprint: [0.5, 0.5],
            },
        )
        with engine.connect() as connection:
            preexisting_orphan_cache_count = connection.execute(
                text(
                    """
                    SELECT COUNT(*) FROM log_miner_embedding_cache AS cache
                    WHERE NOT EXISTS (
                        SELECT 1 FROM production_interaction_logs AS source
                        WHERE source.prompt_fingerprint = cache.prompt_fingerprint
                    )
                    """
                )
            ).scalar_one()

        summary = RunSummary(
            run_id=recovered.run_id,
            status=RunStatus.COMPLETED,
            window_start=now - timedelta(days=30),
            window_end=now,
            candidate_count=1,
            promoted_count=1,
        )
        purged_count = repository_two.finalize_run(
            claim=recovered,
            summary=summary,
            retention_cutoff=retention_cutoff,
        )
        assert purged_count == preexisting_orphan_cache_count + 4
        with engine.connect() as connection:
            retained_source_count = connection.execute(
                text(
                    """
                    SELECT COUNT(*) FROM production_interaction_logs
                    WHERE prompt_fingerprint = :prompt_fingerprint
                    """
                ),
                {"prompt_fingerprint": shared_prompt_fingerprint},
            ).scalar_one()
            orphan_source_count = connection.execute(
                text(
                    """
                    SELECT COUNT(*) FROM production_interaction_logs
                    WHERE prompt_fingerprint = :prompt_fingerprint
                    """
                ),
                {"prompt_fingerprint": orphan_prompt_fingerprint},
            ).scalar_one()
            cache_rows = connection.execute(
                text(
                    """
                    SELECT prompt_fingerprint
                    FROM log_miner_embedding_cache
                    WHERE prompt_fingerprint IN (:shared, :orphan)
                    """
                ),
                {
                    "shared": shared_prompt_fingerprint,
                    "orphan": orphan_prompt_fingerprint,
                },
            ).scalars().all()
            candidate_statuses = dict(
                connection.execute(
                    text(
                        """
                        SELECT source_fingerprint, status
                        FROM log_miner_candidates
                        WHERE source_fingerprint IN (:promoted, :pending)
                        """
                    ),
                    {
                        "promoted": fingerprint,
                        "pending": pending_source_fingerprint,
                    },
                ).all()
            )
        assert retained_source_count == 1
        assert orphan_source_count == 0
        assert set(cache_rows) == {shared_prompt_fingerprint}
        assert candidate_statuses == {fingerprint: "promoted"}
        with pytest.raises(RunClaimLostError):
            repository_two.persist_candidate(claim=recovered, candidate=candidate)
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM log_miner_candidates WHERE feature_scope = :scope"),
                {"scope": scope},
            )
            connection.execute(
                text("DELETE FROM golden_datasets WHERE feature_scope = :scope"),
                {"scope": scope},
            )
            connection.execute(
                text("DELETE FROM production_interaction_logs WHERE feature_scope = :scope"),
                {"scope": scope},
            )
            connection.execute(
                text(
                    """
                    DELETE FROM log_miner_embedding_cache
                    WHERE prompt_fingerprint IN (:first, :second, :third)
                    """
                ),
                {
                    "first": fixture_prompt_fingerprints[0],
                    "second": fixture_prompt_fingerprints[1],
                    "third": fixture_prompt_fingerprints[2],
                },
            )
            if run_ids:
                connection.execute(
                    text(
                        """
                        UPDATE log_miner_leases
                        SET claim_token = NULL,
                            run_id = NULL,
                            lease_expires_at = NULL
                        WHERE run_id = ANY(CAST(:run_ids AS UUID[]))
                        """
                    ),
                    {"run_ids": run_ids},
                )
                connection.execute(
                    text(
                        "DELETE FROM log_miner_runs "
                        "WHERE run_id = ANY(CAST(:run_ids AS UUID[]))"
                    ),
                    {"run_ids": run_ids},
                )
        if "repository" in locals():
            repository.engine.dispose()
        if "repository_two" in locals():
            repository_two.engine.dispose()
        engine.dispose()
