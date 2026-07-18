"""PostgreSQL persistence for production log-miner source and audit records."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence
from uuid import UUID, uuid4

from sqlalchemy import Engine, create_engine, text

from .models import (
    Candidate,
    CandidateStatus,
    CachedEmbedding,
    InteractionRecord,
    PersistResult,
    RunClaim,
    RunClaimLostError,
    RunSummary,
)


class PostgresLogMinerRepository:
    """SQLAlchemy repository with durable leases and transaction fencing."""

    def __init__(self, postgres_dsn: str, *, engine: Engine | None = None) -> None:
        self.engine = engine or create_engine(postgres_dsn, pool_pre_ping=True)

    @staticmethod
    def _claim_parameters(claim: RunClaim) -> dict[str, Any]:
        return {
            "lease_name": claim.lease_name,
            "claim_token": claim.claim_token,
            "run_id": claim.run_id,
        }

    def _assert_claim(self, connection: Any, claim: RunClaim) -> None:
        """Lock and verify the lease in the same transaction as a mutation."""

        owned = connection.execute(
            text(
                """
                SELECT 1
                FROM log_miner_leases
                WHERE lease_name = :lease_name
                  AND claim_token = :claim_token
                  AND run_id = :run_id
                  AND lease_expires_at > clock_timestamp()
                FOR UPDATE
                """
            ),
            self._claim_parameters(claim),
        ).scalar_one_or_none()
        if owned is None:
            raise RunClaimLostError("log-miner run lease is no longer active")

    def claim_run(
        self,
        *,
        lease_name: str,
        lease_ttl_seconds: int,
        window_start: datetime,
        window_end: datetime,
        configuration: dict[str, Any],
    ) -> RunClaim | None:
        if not lease_name or lease_ttl_seconds < 1:
            raise ValueError("lease_name and a positive lease TTL are required")

        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO log_miner_leases (lease_name)
                    VALUES (:lease_name)
                    ON CONFLICT (lease_name) DO NOTHING
                    """
                ),
                {"lease_name": lease_name},
            )
            lease = connection.execute(
                text(
                    """
                    SELECT claim_token, run_id, lease_expires_at,
                           COALESCE(
                               lease_expires_at > clock_timestamp(), FALSE
                           ) AS active
                    FROM log_miner_leases
                    WHERE lease_name = :lease_name
                    FOR UPDATE
                    """
                ),
                {"lease_name": lease_name},
            ).mappings().one()
            if lease["active"]:
                return None

            if lease["run_id"] is not None:
                connection.execute(
                    text(
                        """
                        UPDATE log_miner_runs
                        SET status = 'failed',
                            error_count = error_count + 1,
                            error_message = 'RunLeaseExpired',
                            completed_at = NOW()
                        WHERE run_id = :old_run_id AND status = 'running'
                        """
                    ),
                    {"old_run_id": lease["run_id"]},
                )

            run_id = connection.execute(
                text(
                    """
                    INSERT INTO log_miner_runs (
                        status, window_start, window_end, configuration
                    ) VALUES (
                        'running', :window_start, :window_end,
                        CAST(:configuration AS JSONB)
                    )
                    RETURNING run_id
                    """
                ),
                {
                    "window_start": window_start,
                    "window_end": window_end,
                    "configuration": json.dumps(configuration),
                },
            ).scalar_one()
            claim_token = uuid4()
            lease_expires_at = connection.execute(
                text(
                    """
                    UPDATE log_miner_leases
                    SET claim_token = :claim_token,
                        run_id = :run_id,
                        lease_expires_at = clock_timestamp()
                            + make_interval(secs => :lease_ttl_seconds),
                        heartbeat_at = clock_timestamp()
                    WHERE lease_name = :lease_name
                    RETURNING lease_expires_at
                    """
                ),
                {
                    "lease_name": lease_name,
                    "claim_token": claim_token,
                    "run_id": run_id,
                    "lease_ttl_seconds": lease_ttl_seconds,
                },
            ).scalar_one()
        return RunClaim(
            run_id=UUID(str(run_id)),
            lease_name=lease_name,
            claim_token=claim_token,
            lease_expires_at=lease_expires_at,
        )

    def renew_claim(self, *, claim: RunClaim, lease_ttl_seconds: int) -> bool:
        if lease_ttl_seconds < 1:
            raise ValueError("lease TTL must be positive")
        with self.engine.begin() as connection:
            renewed = connection.execute(
                text(
                    """
                    UPDATE log_miner_leases
                    SET lease_expires_at = clock_timestamp()
                            + make_interval(secs => :lease_ttl_seconds),
                        heartbeat_at = clock_timestamp()
                    WHERE lease_name = :lease_name
                      AND claim_token = :claim_token
                      AND run_id = :run_id
                      AND lease_expires_at > clock_timestamp()
                    RETURNING lease_expires_at
                    """
                ),
                {
                    **self._claim_parameters(claim),
                    "lease_ttl_seconds": lease_ttl_seconds,
                },
            ).scalar_one_or_none()
        return renewed is not None

    def claim_is_active(self, *, claim: RunClaim) -> bool:
        with self.engine.connect() as connection:
            return bool(
                connection.execute(
                    text(
                        """
                        SELECT EXISTS (
                            SELECT 1 FROM log_miner_leases
                            WHERE lease_name = :lease_name
                              AND claim_token = :claim_token
                              AND run_id = :run_id
                              AND lease_expires_at > clock_timestamp()
                        )
                        """
                    ),
                    self._claim_parameters(claim),
                ).scalar_one()
            )

    def record_skipped_run(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
        configuration: dict[str, Any],
        reason: str,
    ) -> UUID:
        with self.engine.begin() as connection:
            run_id = connection.execute(
                text(
                    """
                    INSERT INTO log_miner_runs (
                        status, window_start, window_end, configuration,
                        error_message, completed_at
                    ) VALUES (
                        'skipped', :window_start, :window_end,
                        CAST(:configuration AS JSONB), :reason, NOW()
                    )
                    RETURNING run_id
                    """
                ),
                {
                    "window_start": window_start,
                    "window_end": window_end,
                    "configuration": json.dumps(configuration),
                    "reason": reason,
                },
            ).scalar_one()
        return UUID(str(run_id))

    def fetch_eligible(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
        score_threshold: int,
    ) -> list[InteractionRecord]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT
                        trace_id,
                        feature_scope,
                        redacted_prompt,
                        prompt_fingerprint,
                        arbitration_scores,
                        min_arbitration_score,
                        arbitration_status,
                        negative_feedback,
                        created_at
                    FROM production_interaction_logs AS logs
                    WHERE logs.created_at >= :window_start
                      AND logs.created_at <= :window_end
                      -- Legacy or manually inserted malformed rows must not
                      -- poison the complete rolling eligibility window.
                      AND BTRIM(logs.feature_scope) <> ''
                      AND BTRIM(logs.redacted_prompt) <> ''
                      AND (
                          logs.negative_feedback
                          OR logs.min_arbitration_score < :score_threshold
                      )
                    ORDER BY logs.created_at ASC, logs.trace_id ASC
                    """
                ),
                {
                    "window_start": window_start,
                    "window_end": window_end,
                    "score_threshold": score_threshold,
                },
            ).mappings()
            return [
                InteractionRecord(
                    trace_id=row["trace_id"],
                    feature_scope=str(row["feature_scope"]).strip(),
                    redacted_prompt=str(row["redacted_prompt"]).strip(),
                    prompt_fingerprint=(
                        str(row["prompt_fingerprint"]).strip()
                        if row["prompt_fingerprint"]
                        else None
                    ),
                    arbitration_scores=dict(row["arbitration_scores"] or {}),
                    min_arbitration_score=row["min_arbitration_score"],
                    arbitration_status=row["arbitration_status"],
                    negative_feedback=row["negative_feedback"],
                    created_at=row["created_at"],
                )
                for row in rows
            ]

    def load_cached_embeddings(
        self,
        *,
        prompt_fingerprints: Sequence[str],
        provider_name: str,
        embedding_model: str,
        input_version: str,
    ) -> dict[str, list[CachedEmbedding]]:
        if not prompt_fingerprints:
            return {}
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT prompt_fingerprint, provider_name, embedding_model,
                           input_version, embedding_dimensions, embedding_values
                    FROM log_miner_embedding_cache
                    WHERE prompt_fingerprint = ANY(
                              CAST(:prompt_fingerprints AS CHAR(64)[])
                          )
                      AND provider_name = :provider_name
                      AND embedding_model = :embedding_model
                      AND input_version = :input_version
                    ORDER BY prompt_fingerprint, embedding_dimensions
                    """
                ),
                {
                    "prompt_fingerprints": list(prompt_fingerprints),
                    "provider_name": provider_name,
                    "embedding_model": embedding_model,
                    "input_version": input_version,
                },
            ).mappings()
            result: dict[str, list[CachedEmbedding]] = {}
            for row in rows:
                fingerprint = str(row["prompt_fingerprint"]).strip()
                result.setdefault(fingerprint, []).append(
                    CachedEmbedding(
                        prompt_fingerprint=fingerprint,
                        provider_name=row["provider_name"],
                        embedding_model=row["embedding_model"],
                        input_version=row["input_version"],
                        embedding_dimensions=row["embedding_dimensions"],
                        embedding_values=list(row["embedding_values"]),
                    )
                )
            return result

    def store_cached_embeddings(
        self,
        *,
        claim: RunClaim,
        provider_name: str,
        embedding_model: str,
        input_version: str,
        embeddings: dict[str, list[float]],
    ) -> None:
        if not embeddings:
            return
        with self.engine.begin() as connection:
            self._assert_claim(connection, claim)
            for fingerprint, values in sorted(embeddings.items()):
                if not values:
                    raise ValueError("cached embeddings must not be empty")
                profile = {
                    "prompt_fingerprint": fingerprint,
                    "provider_name": provider_name,
                    "embedding_model": embedding_model,
                    "input_version": input_version,
                }
                connection.execute(
                    text(
                        """
                        DELETE FROM log_miner_embedding_cache
                        WHERE prompt_fingerprint = :prompt_fingerprint
                          AND provider_name = :provider_name
                          AND embedding_model = :embedding_model
                          AND input_version = :input_version
                        """
                    ),
                    profile,
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO log_miner_embedding_cache (
                            prompt_fingerprint, provider_name, embedding_model,
                            input_version, embedding_dimensions, embedding_values
                        ) VALUES (
                            :prompt_fingerprint, :provider_name, :embedding_model,
                            :input_version, :embedding_dimensions,
                            CAST(:embedding_values AS DOUBLE PRECISION[])
                        )
                        """
                    ),
                    {
                        **profile,
                        "embedding_dimensions": len(values),
                        "embedding_values": list(values),
                    },
                )

    def source_fingerprint_exists(self, source_fingerprint: str) -> bool:
        with self.engine.connect() as connection:
            return bool(
                connection.execute(
                    text(
                        """
                        SELECT
                            EXISTS (
                                SELECT 1 FROM log_miner_candidates
                                WHERE source_fingerprint = :source_fingerprint
                            )
                            OR EXISTS (
                                SELECT 1 FROM golden_datasets
                                WHERE source_fingerprint = :source_fingerprint
                            )
                        """
                    ),
                    {"source_fingerprint": source_fingerprint},
                ).scalar_one()
            )

    @staticmethod
    def _merge_members_on_connection(
        connection: Any,
        *,
        candidate_id: UUID,
        member_trace_ids: Sequence[str],
        cluster_label: int | None,
        is_noise: bool,
        outlier_score: float | None,
        nearest_example_count: int,
    ) -> None:
        for trace_id in sorted(set(member_trace_ids)):
            connection.execute(
                text(
                    """
                    INSERT INTO log_miner_candidate_members (candidate_id, trace_id)
                    VALUES (:candidate_id, :trace_id)
                    ON CONFLICT (candidate_id, trace_id) DO NOTHING
                    """
                ),
                {"candidate_id": candidate_id, "trace_id": trace_id},
            )
        connection.execute(
            text(
                """
                UPDATE log_miner_candidates AS candidate
                SET cluster_label = :cluster_label,
                    is_noise = :is_noise,
                    outlier_score = :outlier_score,
                    member_trace_ids = (
                        SELECT COALESCE(
                            jsonb_agg(member.trace_id ORDER BY member.trace_id),
                            '[]'::jsonb
                        )
                        FROM log_miner_candidate_members AS member
                        WHERE member.candidate_id = candidate.candidate_id
                    ),
                    provenance = COALESCE(candidate.provenance, '{}'::jsonb)
                        || jsonb_build_object(
                            'member_count', (
                            SELECT COUNT(*)
                            FROM log_miner_candidate_members AS member
                            WHERE member.candidate_id = candidate.candidate_id
                            ),
                            'nearest_example_count',
                                CAST(:nearest_example_count AS INTEGER),
                            'cluster_label', CAST(:cluster_label AS INTEGER),
                            'is_noise', CAST(:is_noise AS BOOLEAN),
                            'outlier_score',
                                CAST(:outlier_score AS DOUBLE PRECISION)
                        ),
                    updated_at = NOW()
                WHERE candidate.candidate_id = :candidate_id
                """
            ),
            {
                "candidate_id": candidate_id,
                "cluster_label": cluster_label,
                "is_noise": is_noise,
                "outlier_score": outlier_score,
                "nearest_example_count": nearest_example_count,
            },
        )
        connection.execute(
            text(
                """
                UPDATE golden_datasets AS golden
                SET provenance = COALESCE(golden.provenance, '{}'::jsonb)
                    || COALESCE(candidate.provenance, '{}'::jsonb)
                FROM log_miner_candidates AS candidate
                WHERE candidate.candidate_id = :candidate_id
                  AND golden.case_id = candidate.golden_case_id
                """
            ),
            {"candidate_id": candidate_id},
        )

    def merge_candidate_members(
        self,
        *,
        claim: RunClaim,
        source_fingerprint: str,
        member_trace_ids: Sequence[str],
        cluster_label: int | None,
        is_noise: bool,
        outlier_score: float | None,
        nearest_example_count: int,
    ) -> bool:
        with self.engine.begin() as connection:
            self._assert_claim(connection, claim)
            candidate_id = connection.execute(
                text(
                    """
                    SELECT candidate_id FROM log_miner_candidates
                    WHERE source_fingerprint = :source_fingerprint
                    """
                ),
                {"source_fingerprint": source_fingerprint},
            ).scalar_one_or_none()
            if candidate_id is None:
                return False
            self._merge_members_on_connection(
                connection,
                candidate_id=UUID(str(candidate_id)),
                member_trace_ids=member_trace_ids,
                cluster_label=cluster_label,
                is_noise=is_noise,
                outlier_score=outlier_score,
                nearest_example_count=nearest_example_count,
            )
            return True

    def persist_candidate(
        self, *, claim: RunClaim, candidate: Candidate
    ) -> PersistResult:
        """Insert a candidate and optional golden case in one transaction."""

        candidate_payload = {
            "run_id": claim.run_id,
            "source_fingerprint": candidate.source_fingerprint,
            "feature_scope": candidate.feature_scope,
            "user_input": candidate.user_input,
            "representative_trace_id": candidate.representative_trace_id,
            "member_trace_ids": json.dumps(candidate.member_trace_ids),
            "cluster_label": candidate.cluster_label,
            "is_noise": candidate.is_noise,
            "outlier_score": candidate.outlier_score,
            "expected_output": json.dumps(candidate.expected_output.model_dump(mode="json")),
            "difficulty_rating": candidate.difficulty_rating.value,
            "label_confidence": candidate.label_confidence,
            "status": candidate.status.value,
            "provenance": json.dumps(candidate.provenance),
        }
        with self.engine.begin() as connection:
            self._assert_claim(connection, claim)
            candidate_id = connection.execute(
                text(
                    """
                    INSERT INTO log_miner_candidates (
                        run_id,
                        source_fingerprint,
                        feature_scope,
                        user_input,
                        representative_trace_id,
                        member_trace_ids,
                        cluster_label,
                        is_noise,
                        outlier_score,
                        expected_output,
                        difficulty_rating,
                        label_confidence,
                        status,
                        provenance
                    ) VALUES (
                        :run_id,
                        :source_fingerprint,
                        :feature_scope,
                        :user_input,
                        :representative_trace_id,
                        CAST(:member_trace_ids AS JSONB),
                        :cluster_label,
                        :is_noise,
                        :outlier_score,
                        CAST(:expected_output AS JSONB),
                        :difficulty_rating,
                        :label_confidence,
                        :status,
                        CAST(:provenance AS JSONB)
                    )
                    ON CONFLICT (source_fingerprint) DO NOTHING
                    RETURNING candidate_id
                    """
                ),
                candidate_payload,
            ).scalar_one_or_none()

            if candidate_id is None:
                existing = connection.execute(
                    text(
                        """
                        SELECT candidate_id, golden_case_id
                        FROM log_miner_candidates
                        WHERE source_fingerprint = :source_fingerprint
                        """
                    ),
                    {"source_fingerprint": candidate.source_fingerprint},
                ).mappings().one_or_none()
                if existing is not None:
                    self._merge_members_on_connection(
                        connection,
                        candidate_id=UUID(str(existing["candidate_id"])),
                        member_trace_ids=[
                            candidate.representative_trace_id,
                            *candidate.member_trace_ids,
                        ],
                        cluster_label=candidate.cluster_label,
                        is_noise=candidate.is_noise,
                        outlier_score=candidate.outlier_score,
                        nearest_example_count=int(
                            candidate.provenance.get("nearest_example_count", 0)
                        ),
                    )
                return PersistResult(
                    created=False,
                    candidate_id=(
                        UUID(str(existing["candidate_id"])) if existing is not None else None
                    ),
                    golden_case_id=(
                        UUID(str(existing["golden_case_id"]))
                        if existing is not None and existing["golden_case_id"]
                        else None
                    ),
                )

            candidate_uuid = UUID(str(candidate_id))
            self._merge_members_on_connection(
                connection,
                candidate_id=candidate_uuid,
                member_trace_ids=[
                    candidate.representative_trace_id,
                    *candidate.member_trace_ids,
                ],
                cluster_label=candidate.cluster_label,
                is_noise=candidate.is_noise,
                outlier_score=candidate.outlier_score,
                nearest_example_count=int(
                    candidate.provenance.get("nearest_example_count", 0)
                ),
            )
            golden_case_id: UUID | None = None
            if candidate.status is CandidateStatus.PROMOTED:
                golden_provenance = {
                    **candidate.provenance,
                    "candidate_id": str(candidate_uuid),
                    "run_id": str(claim.run_id),
                    "label_confidence": candidate.label_confidence,
                }
                inserted_golden_id = connection.execute(
                    text(
                        """
                        INSERT INTO golden_datasets (
                            feature_scope,
                            user_input,
                            expected_output,
                            difficulty_rating,
                            origin_source,
                            source_fingerprint,
                            provenance
                        ) VALUES (
                            :feature_scope,
                            :user_input,
                            CAST(:expected_output AS JSONB),
                            :difficulty_rating,
                            'production_miner',
                            :source_fingerprint,
                            CAST(:provenance AS JSONB)
                        )
                        ON CONFLICT (source_fingerprint)
                            WHERE source_fingerprint IS NOT NULL
                            DO NOTHING
                        RETURNING case_id
                        """
                    ),
                    {
                        "feature_scope": candidate.feature_scope,
                        "user_input": candidate.user_input,
                        "expected_output": candidate_payload["expected_output"],
                        "difficulty_rating": candidate.difficulty_rating.value,
                        "source_fingerprint": candidate.source_fingerprint,
                        "provenance": json.dumps(golden_provenance),
                    },
                ).scalar_one_or_none()
                if inserted_golden_id is None:
                    inserted_golden_id = connection.execute(
                        text(
                            """
                            SELECT case_id FROM golden_datasets
                            WHERE source_fingerprint = :source_fingerprint
                            """
                        ),
                        {"source_fingerprint": candidate.source_fingerprint},
                    ).scalar_one()
                golden_case_id = UUID(str(inserted_golden_id))
                connection.execute(
                    text(
                        """
                        UPDATE log_miner_candidates
                        SET golden_case_id = :golden_case_id,
                            updated_at = NOW()
                        WHERE candidate_id = :candidate_id
                        """
                    ),
                    {
                        "golden_case_id": golden_case_id,
                        "candidate_id": candidate_uuid,
                    },
                )

            return PersistResult(
                created=True,
                candidate_id=candidate_uuid,
                golden_case_id=golden_case_id,
            )

    @staticmethod
    def _summary_parameters(summary: RunSummary) -> dict[str, Any]:
        return {
            "status": summary.status.value,
            "eligible_count": summary.eligible_count,
            "embedded_count": summary.embedded_count,
            "candidate_count": summary.candidate_count,
            "promoted_count": summary.promoted_count,
            "pending_review_count": summary.pending_review_count,
            "duplicate_count": summary.duplicate_count,
            "error_count": summary.error_count,
            "purged_count": summary.purged_count,
            "error_message": summary.error_message,
        }

    @staticmethod
    def _update_run(connection: Any, *, run_id: UUID, summary: RunSummary) -> None:
        updated = connection.execute(
            text(
                """
                UPDATE log_miner_runs
                SET status = :status,
                    eligible_count = :eligible_count,
                    embedded_count = :embedded_count,
                    candidate_count = :candidate_count,
                    promoted_count = :promoted_count,
                    pending_review_count = :pending_review_count,
                    duplicate_count = :duplicate_count,
                    error_count = :error_count,
                    purged_count = :purged_count,
                    error_message = :error_message,
                    completed_at = NOW()
                WHERE run_id = :run_id
                """
            ),
            {
                "run_id": run_id,
                **PostgresLogMinerRepository._summary_parameters(summary),
            },
        ).rowcount
        if updated != 1:
            raise RuntimeError("log-miner run audit row is missing")

    def finalize_run(
        self,
        *,
        claim: RunClaim,
        summary: RunSummary,
        retention_cutoff: datetime,
    ) -> int:
        """Atomically purge retained data, complete the run, and clear ownership."""

        with self.engine.begin() as connection:
            self._assert_claim(connection, claim)
            source_count = connection.execute(
                text("DELETE FROM production_interaction_logs WHERE created_at < :cutoff"),
                {"cutoff": retention_cutoff},
            ).rowcount
            candidate_count = connection.execute(
                text(
                    """
                    DELETE FROM log_miner_candidates
                    WHERE status = 'pending_review' AND created_at < :cutoff
                    """
                ),
                {"cutoff": retention_cutoff},
            ).rowcount
            cache_count = connection.execute(
                text(
                    """
                    DELETE FROM log_miner_embedding_cache AS cache
                    WHERE NOT EXISTS (
                        SELECT 1 FROM production_interaction_logs AS source
                        WHERE source.prompt_fingerprint = cache.prompt_fingerprint
                    )
                    """
                )
            ).rowcount
            purged_count = sum(
                max(count or 0, 0)
                for count in (source_count, candidate_count, cache_count)
            )
            completed = summary.model_copy(update={"purged_count": purged_count})
            self._update_run(connection, run_id=claim.run_id, summary=completed)
            connection.execute(
                text(
                    """
                    UPDATE log_miner_leases
                    SET claim_token = NULL,
                        run_id = NULL,
                        lease_expires_at = NULL,
                        heartbeat_at = clock_timestamp()
                    WHERE lease_name = :lease_name
                      AND claim_token = :claim_token
                      AND run_id = :run_id
                    """
                ),
                self._claim_parameters(claim),
            )
        return purged_count

    def fail_run(self, *, claim: RunClaim, summary: RunSummary) -> bool:
        try:
            with self.engine.begin() as connection:
                self._assert_claim(connection, claim)
                self._update_run(connection, run_id=claim.run_id, summary=summary)
                connection.execute(
                    text(
                        """
                        UPDATE log_miner_leases
                        SET claim_token = NULL,
                            run_id = NULL,
                            lease_expires_at = NULL,
                            heartbeat_at = clock_timestamp()
                        WHERE lease_name = :lease_name
                          AND claim_token = :claim_token
                          AND run_id = :run_id
                        """
                    ),
                    self._claim_parameters(claim),
                )
        except RunClaimLostError:
            return False
        return True


def apply_migration(postgres_dsn: str, migration_path: Path | None = None) -> None:
    """Execute the idempotent Phase 4.1 migration as one database transaction."""

    path = migration_path or (
        Path(__file__).resolve().parents[2] / "migrations" / "001_log_miner.sql"
    )
    sql = path.read_text(encoding="utf-8")
    engine = create_engine(postgres_dsn, pool_pre_ping=True)
    raw_connection = engine.raw_connection()
    try:
        cursor = raw_connection.cursor()
        try:
            cursor.execute(sql)
            raw_connection.commit()
        except Exception:
            raw_connection.rollback()
            raise
        finally:
            cursor.close()
    finally:
        raw_connection.close()
        engine.dispose()
