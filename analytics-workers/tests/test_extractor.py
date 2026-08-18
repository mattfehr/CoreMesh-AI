"""Offline contract tests for selection, clustering, and idempotent routing."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import numpy as np
import pytest

import src.log_miner.extractor as extractor_module
import src.log_miner.repository as repository_module
from src.log_miner.extractor import (
    ClusterAssignments,
    HDBSCANClusterer,
    INPUT_NORMALIZATION_VERSION,
    ProductionLogMiner,
    build_prompt_fingerprint,
    build_source_fingerprint,
    is_eligible,
    normalize_prompt,
    select_medoid,
    validate_and_normalize_embeddings,
)
from src.log_miner.models import (
    CandidateStatus,
    CachedEmbedding,
    DifficultyRating,
    ExpectedBehavior,
    GeneratedReference,
    InteractionRecord,
    PersistResult,
    RunClaim,
    RunStatus,
    ValidationCriterion,
)


NOW = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)


def _record(
    trace_id: str,
    prompt: str,
    *,
    scope: str = "support",
    score: int | None = 3,
    negative: bool = False,
    created_at: datetime = NOW,
) -> InteractionRecord:
    return InteractionRecord(
        trace_id=trace_id,
        feature_scope=scope,
        redacted_prompt=prompt,
        prompt_fingerprint=build_prompt_fingerprint(prompt),
        arbitration_scores={"accuracy": score} if score else {},
        min_arbitration_score=score,
        arbitration_status="approved",
        negative_feedback=negative,
        created_at=created_at,
    )


class _EmbeddingProvider:
    provider_name = "deterministic"
    model = "fixture-v1"
    vectors = {
        "cluster-a-1": [1.0, 0.01, 0.0],
        "cluster-a-2": [1.0, 0.0, 0.01],
        "cluster-a-3": [1.0, -0.01, 0.0],
        "cluster-b-1": [0.0, 1.0, 0.01],
        "cluster-b-2": [0.01, 1.0, 0.0],
        "cluster-b-3": [0.0, 1.0, -0.01],
        "unique-noise": [-1.0, -1.0, 1.0],
    }

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def embed(self, texts):
        self.calls.append(tuple(texts))
        return [self.vectors[text] for text in texts]


class _Generator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def generate(self, *, feature_scope, representative_prompt, cluster_examples):
        self.calls.append((representative_prompt, tuple(cluster_examples)))
        confidence = 0.60 if representative_prompt == "cluster-b-2" else 0.95
        return GeneratedReference(
            reference_answer=f"Reference for {representative_prompt}",
            validation_criteria=[
                ValidationCriterion(description="Addresses the failure safely", required=True)
            ],
            expected_behavior=ExpectedBehavior.ANSWER,
            failure_pattern="Synthetic regression pattern",
            difficulty_rating=DifficultyRating.MODERATE,
            label_confidence=confidence,
        )


class _MemoryRepository:
    def __init__(self, records: list[InteractionRecord]) -> None:
        self.records = records
        self.candidates = []
        self.fingerprints: set[str] = set()
        self.candidate_members: dict[str, set[str]] = {}
        self.cache: dict[
            tuple[str, str, str, str], list[list[float]]
        ] = {}
        self.completed = []
        self.failed = []
        self.skipped = []
        self.active_claim: RunClaim | None = None
        self.lock_held = False
        self.lock_hold_observations: list[bool] = []

    def _assert_claim(self, claim):
        if self.active_claim is None or claim.claim_token != self.active_claim.claim_token:
            raise RuntimeError("stale claim")

    def claim_run(self, *, lease_name, lease_ttl_seconds, **kwargs):
        if self.active_claim is not None:
            return None
        self.active_claim = RunClaim(
            run_id=uuid4(),
            lease_name=lease_name,
            claim_token=uuid4(),
            lease_expires_at=NOW + timedelta(seconds=lease_ttl_seconds),
        )
        return self.active_claim

    def renew_claim(self, *, claim, lease_ttl_seconds):
        if self.active_claim is None or claim.claim_token != self.active_claim.claim_token:
            return False
        return True

    def claim_is_active(self, *, claim):
        return (
            self.active_claim is not None
            and claim.claim_token == self.active_claim.claim_token
        )

    def record_skipped_run(self, **kwargs):
        run_id = uuid4()
        self.skipped.append((run_id, kwargs))
        return run_id

    def fetch_eligible(self, **kwargs):
        return list(self.records)

    def load_cached_embeddings(
        self,
        *,
        prompt_fingerprints,
        provider_name,
        embedding_model,
        input_version,
    ):
        result = {}
        for fingerprint in prompt_fingerprints:
            values_list = self.cache.get(
                (fingerprint, provider_name, embedding_model, input_version), []
            )
            if values_list:
                result[fingerprint] = [
                    CachedEmbedding(
                        prompt_fingerprint=fingerprint,
                        provider_name=provider_name,
                        embedding_model=embedding_model,
                        input_version=input_version,
                        embedding_dimensions=len(values),
                        embedding_values=values,
                    )
                    for values in values_list
                ]
        return result

    def store_cached_embeddings(
        self,
        *,
        claim,
        provider_name,
        embedding_model,
        input_version,
        embeddings,
    ):
        self._assert_claim(claim)
        for fingerprint, values in embeddings.items():
            self.cache[
                (fingerprint, provider_name, embedding_model, input_version)
            ] = [list(values)]

    def source_fingerprint_exists(self, source_fingerprint):
        return source_fingerprint in self.fingerprints

    def merge_candidate_members(
        self,
        *,
        claim,
        source_fingerprint,
        member_trace_ids,
        cluster_label,
        is_noise,
        outlier_score,
        nearest_example_count,
    ):
        self._assert_claim(claim)
        if source_fingerprint not in self.fingerprints:
            return False
        self.candidate_members.setdefault(source_fingerprint, set()).update(
            member_trace_ids
        )
        for candidate in self.candidates:
            if candidate.source_fingerprint == source_fingerprint:
                candidate.cluster_label = cluster_label
                candidate.is_noise = is_noise
                candidate.outlier_score = outlier_score
                candidate.member_trace_ids = sorted(
                    self.candidate_members[source_fingerprint]
                )
                candidate.provenance.update(
                    {
                        "member_count": len(
                            self.candidate_members[source_fingerprint]
                        ),
                        "nearest_example_count": nearest_example_count,
                        "cluster_label": cluster_label,
                        "is_noise": is_noise,
                        "outlier_score": outlier_score,
                    }
                )
                break
        return True

    def persist_candidate(self, *, claim, candidate):
        self._assert_claim(claim)
        if candidate.source_fingerprint in self.fingerprints:
            self.candidate_members.setdefault(candidate.source_fingerprint, set()).update(
                candidate.member_trace_ids
            )
            return PersistResult(created=False)
        self.fingerprints.add(candidate.source_fingerprint)
        self.candidate_members[candidate.source_fingerprint] = set(
            candidate.member_trace_ids
        ) | {candidate.representative_trace_id}
        self.candidates.append(candidate)
        return PersistResult(
            created=True,
            candidate_id=uuid4(),
            golden_case_id=uuid4() if candidate.status is CandidateStatus.PROMOTED else None,
        )

    def finalize_run(self, *, claim, summary, retention_cutoff):
        self._assert_claim(claim)
        self.completed.append(summary)
        self.active_claim = None
        return 0

    def fail_run(self, *, claim, summary):
        if not self.claim_is_active(claim=claim):
            return False
        self.failed.append(summary)
        self.active_claim = None
        return True


class _LockAwareEmbeddingProvider(_EmbeddingProvider):
    def __init__(self, repository: _MemoryRepository) -> None:
        super().__init__()
        self.repository = repository
        self.embed_calls = 0

    def embed(self, texts):
        self.embed_calls += 1
        self.repository.lock_hold_observations.append(self.repository.lock_held)
        return super().embed(texts)


class _LockAwareGenerator(_Generator):
    def __init__(self, repository: _MemoryRepository) -> None:
        super().__init__()
        self.repository = repository

    def generate(self, *, feature_scope, representative_prompt, cluster_examples):
        self.repository.lock_hold_observations.append(self.repository.lock_held)
        return super().generate(
            feature_scope=feature_scope,
            representative_prompt=representative_prompt,
            cluster_examples=cluster_examples,
        )


def test_eligibility_uses_feedback_or_score_and_window() -> None:
    start = NOW - timedelta(days=30)
    assert is_eligible(_record("low", "x", score=3), window_start=start, window_end=NOW)
    assert is_eligible(
        _record("feedback", "x", score=5, negative=True),
        window_start=start,
        window_end=NOW,
    )
    assert not is_eligible(
        _record("healthy", "x", score=4), window_start=start, window_end=NOW
    )
    assert not is_eligible(
        _record("old", "x", created_at=start - timedelta(seconds=1)),
        window_start=start,
        window_end=NOW,
    )


def test_embedding_validation_normalizes_and_rejects_bad_vectors() -> None:
    matrix = validate_and_normalize_embeddings([[3.0, 4.0], [0.0, 2.0]], expected_count=2)
    np.testing.assert_allclose(np.linalg.norm(matrix, axis=1), [1.0, 1.0])
    with pytest.raises(ValueError, match="zero-length"):
        validate_and_normalize_embeddings([[0.0, 0.0]])
    with pytest.raises(ValueError, match="finite"):
        validate_and_normalize_embeddings([[float("nan"), 1.0]])
    with pytest.raises(ValueError, match="consistent"):
        validate_and_normalize_embeddings([[1.0], [1.0, 2.0]])
    with pytest.raises(ValueError, match="1 vectors for 2"):
        validate_and_normalize_embeddings([[1.0]], expected_count=2)


def test_medoid_and_source_fingerprint_are_stable() -> None:
    assert select_medoid(np.asarray([[0.0], [1.0], [10.0]])) == 1
    assert normalize_prompt("  A\u00a0  B ") == "A B"
    first = build_source_fingerprint(
        feature_scope="scope", representative_prompt=" A  B ", version="1"
    )
    second = build_source_fingerprint(
        feature_scope="scope", representative_prompt="A B", version="1"
    )
    assert first == second
    assert len(first) == 64


def test_real_hdbscan_two_clusters_one_noise_and_idempotent_rerun() -> None:
    records = [
        _record("a1", "cluster-a-1", created_at=NOW - timedelta(minutes=7)),
        _record("a2", "cluster-a-2", created_at=NOW - timedelta(minutes=6)),
        _record("a3", "cluster-a-3", created_at=NOW - timedelta(minutes=5)),
        _record("b1", "cluster-b-1", created_at=NOW - timedelta(minutes=4)),
        _record("b2", "cluster-b-2", created_at=NOW - timedelta(minutes=3)),
        _record("b3", "cluster-b-3", created_at=NOW - timedelta(minutes=2)),
        _record("noise", "unique-noise", score=5, negative=True, created_at=NOW),
    ]
    repository = _MemoryRepository(records)
    generator = _Generator()
    embedding = _EmbeddingProvider()
    miner = ProductionLogMiner(
        repository=repository,
        embedding_provider=embedding,
        reference_generator=generator,
        clusterer=HDBSCANClusterer(min_cluster_size=3, min_samples=2),
        now=lambda: NOW,
    )

    first = miner.run_once()
    assert first.status is RunStatus.COMPLETED
    assert first.eligible_count == 7
    assert first.embedded_count == 7
    assert first.candidate_count == 3
    assert first.promoted_count == 2
    assert first.pending_review_count == 1
    assert {candidate.user_input for candidate in repository.candidates} == {
        "cluster-a-2",
        "cluster-b-2",
        "unique-noise",
    }
    assert sum(candidate.is_noise for candidate in repository.candidates) == 1
    assert {
        candidate.user_input: candidate.status for candidate in repository.candidates
    }["cluster-b-2"] is CandidateStatus.PENDING_REVIEW
    assert len(generator.calls) == 3
    assert all(len(examples) <= 3 for _, examples in generator.calls)

    second = miner.run_once()
    assert second.status is RunStatus.COMPLETED
    assert second.eligible_count == 7
    assert second.embedded_count == 0
    assert second.candidate_count == 0
    assert second.duplicate_count == 3
    assert len(repository.candidates) == 3
    assert len(generator.calls) == 3
    assert len(embedding.calls) == 1
    assert len(embedding.calls[0]) == 7


def test_provider_calls_do_not_hold_a_database_session_lock() -> None:
    records = [
        _record("a1", "cluster-a-1"),
        _record("a2", "cluster-a-2"),
        _record("a3", "cluster-a-3"),
    ]
    repository = _MemoryRepository(records)
    embedding = _LockAwareEmbeddingProvider(repository)
    generator = _LockAwareGenerator(repository)
    miner = ProductionLogMiner(
        repository=repository,
        embedding_provider=embedding,
        reference_generator=generator,
        clusterer=HDBSCANClusterer(min_cluster_size=3, min_samples=2),
        now=lambda: NOW,
    )
    summary = miner.run_once()
    assert summary.status is RunStatus.COMPLETED
    assert embedding.embed_calls == 1
    assert generator.calls
    assert repository.lock_hold_observations
    assert all(held is False for held in repository.lock_hold_observations)


def test_full_window_rerun_uses_cached_embeddings() -> None:
    records = [
        _record("a1", "cluster-a-1"),
        _record("a2", "cluster-a-2"),
        _record("a3", "cluster-a-3"),
        _record("noise", "unique-noise", score=5, negative=True),
    ]
    repository = _MemoryRepository(records)
    embedding = _LockAwareEmbeddingProvider(repository)
    miner = ProductionLogMiner(
        repository=repository,
        embedding_provider=embedding,
        reference_generator=_Generator(),
        clusterer=HDBSCANClusterer(min_cluster_size=3, min_samples=2),
        now=lambda: NOW,
    )
    first = miner.run_once()
    second = miner.run_once()
    assert first.embedded_count == 4
    assert second.eligible_count == 4
    assert second.embedded_count == 0
    assert embedding.embed_calls == 1


def test_new_trace_with_same_prompt_uses_cache_without_leaving_the_population() -> None:
    repository = _MemoryRepository([_record("first", "cluster-a-1")])
    embedding = _EmbeddingProvider()
    generator = _Generator()
    miner = ProductionLogMiner(
        repository=repository,
        embedding_provider=embedding,
        reference_generator=generator,
        clusterer=HDBSCANClusterer(min_cluster_size=3, min_samples=2),
        now=lambda: NOW,
    )

    first = miner.run_once()
    repository.records.append(
        _record("second", "cluster-a-1", created_at=NOW)
    )
    second = miner.run_once()

    assert first.embedded_count == 1
    assert second.eligible_count == 2
    assert second.embedded_count == 0
    assert len(embedding.calls) == 1
    assert len(generator.calls) == 1
    fingerprint = repository.candidates[0].source_fingerprint
    assert repository.candidate_members[fingerprint] == {"first", "second"}


class _EvolvingClusterer:
    def cluster(self, vectors):
        if len(vectors) == 1:
            return ClusterAssignments(
                labels=np.asarray([-1]),
                outlier_scores=np.asarray([1.0]),
            )
        return ClusterAssignments(
            labels=np.zeros(len(vectors), dtype=int),
            outlier_scores=np.zeros(len(vectors), dtype=float),
        )


def test_old_noise_can_join_a_later_systemic_cluster() -> None:
    repository = _MemoryRepository(
        [_record("a2", "cluster-a-2", created_at=NOW - timedelta(seconds=3))]
    )
    embedding = _EmbeddingProvider()
    generator = _Generator()
    miner = ProductionLogMiner(
        repository=repository,
        embedding_provider=embedding,
        reference_generator=generator,
        clusterer=_EvolvingClusterer(),
        now=lambda: NOW,
    )

    first = miner.run_once()
    repository.records.extend(
        [
            _record("a1", "cluster-a-1", created_at=NOW - timedelta(seconds=2)),
            _record("a3", "cluster-a-3", created_at=NOW - timedelta(seconds=1)),
        ]
    )
    second = miner.run_once()

    assert first.candidate_count == 1
    assert second.eligible_count == 3
    assert second.embedded_count == 2
    assert second.candidate_count == 0
    assert second.duplicate_count == 1
    assert len(generator.calls) == 1
    fingerprint = repository.candidates[0].source_fingerprint
    assert repository.candidate_members[fingerprint] == {"a1", "a2", "a3"}
    evolved = repository.candidates[0]
    assert evolved.is_noise is False
    assert evolved.cluster_label == 0
    assert evolved.outlier_score == 0.0
    assert evolved.provenance["member_count"] == 3
    assert evolved.provenance["is_noise"] is False


@pytest.mark.parametrize(
    "cached_values",
    [
        [[0.0, 0.0, 0.0]],
        [[float("nan"), 1.0, 0.0]],
        [[1.0, 0.0, 0.0], [1.0, 0.0]],
    ],
)
def test_invalid_cached_embedding_is_recomputed(cached_values) -> None:
    prompt = "cluster-a-1"
    repository = _MemoryRepository([_record("cached", prompt)])
    key = (
        build_prompt_fingerprint(prompt),
        _EmbeddingProvider.provider_name,
        _EmbeddingProvider.model,
        INPUT_NORMALIZATION_VERSION,
    )
    repository.cache[key] = cached_values
    embedding = _EmbeddingProvider()
    miner = ProductionLogMiner(
        repository=repository,
        embedding_provider=embedding,
        reference_generator=_Generator(),
        clusterer=HDBSCANClusterer(min_cluster_size=3, min_samples=2),
        now=lambda: NOW,
    )

    summary = miner.run_once()

    assert summary.status is RunStatus.COMPLETED
    assert summary.embedded_count == 1
    assert len(embedding.calls) == 1
    assert len(repository.cache[key]) == 1
    np.testing.assert_allclose(np.linalg.norm(repository.cache[key][0]), 1.0)


class _LeaseDroppingGenerator(_Generator):
    def __init__(self, repository):
        super().__init__()
        self.repository = repository

    def generate(self, **kwargs):
        generated = super().generate(**kwargs)
        self.repository.active_claim = None
        return generated


def test_lost_lease_fences_candidate_and_run_writes() -> None:
    repository = _MemoryRepository([_record("lost", "cluster-a-1")])
    miner = ProductionLogMiner(
        repository=repository,
        embedding_provider=_EmbeddingProvider(),
        reference_generator=_LeaseDroppingGenerator(repository),
        clusterer=HDBSCANClusterer(min_cluster_size=3, min_samples=2),
        now=lambda: NOW,
    )

    summary = miner.run_once()

    assert summary.status is RunStatus.FAILED
    assert summary.error_message == "RunClaimLostError"
    assert summary.candidate_count == 0
    assert repository.candidates == []
    assert repository.failed == []
    assert len(repository.cache) == 1


class _NoiseClusterer:
    def cluster(self, vectors):
        return ClusterAssignments(
            labels=np.full(len(vectors), -1, dtype=int),
            outlier_scores=np.asarray([0.1, 0.9, 0.5]),
        )


def test_noise_is_capped_by_descending_outlier_score() -> None:
    # created_at order matches the positional outlier_scores after eligibility sort
    records = [
        _record("one", "cluster-a-1", created_at=NOW - timedelta(seconds=3)),
        _record("two", "cluster-a-2", created_at=NOW - timedelta(seconds=2)),
        _record("three", "cluster-a-3", created_at=NOW - timedelta(seconds=1)),
    ]
    repository = _MemoryRepository(records)
    miner = ProductionLogMiner(
        repository=repository,
        embedding_provider=_EmbeddingProvider(),
        reference_generator=_Generator(),
        clusterer=_NoiseClusterer(),
        max_noise_per_feature=2,
        now=lambda: NOW,
    )
    summary = miner.run_once()
    assert summary.candidate_count == 2
    assert [candidate.representative_trace_id for candidate in repository.candidates] == [
        "two",
        "three",
    ]
    assert [candidate.outlier_score for candidate in repository.candidates] == [0.9, 0.5]


class _PartitionClusterer:
    def __init__(self) -> None:
        self.partition_sizes = []

    def cluster(self, vectors):
        self.partition_sizes.append(len(vectors))
        return ClusterAssignments(
            labels=np.full(len(vectors), -1, dtype=int),
            outlier_scores=np.ones(len(vectors)),
        )


def test_feature_scopes_are_clustered_independently() -> None:
    repository = _MemoryRepository(
        [
            _record("a1", "cluster-a-1", scope="billing"),
            _record("a2", "cluster-a-2", scope="billing"),
            _record("b1", "cluster-b-1", scope="search"),
            _record("b2", "cluster-b-2", scope="search"),
        ]
    )
    clusterer = _PartitionClusterer()
    miner = ProductionLogMiner(
        repository=repository,
        embedding_provider=_EmbeddingProvider(),
        reference_generator=_Generator(),
        clusterer=clusterer,
        max_noise_per_feature=1,
        now=lambda: NOW,
    )
    summary = miner.run_once()
    assert summary.candidate_count == 2
    assert clusterer.partition_sizes == [2, 2]
    assert {candidate.feature_scope for candidate in repository.candidates} == {
        "billing",
        "search",
    }


class _FailingGenerator:
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, **kwargs):
        self.calls += 1
        raise RuntimeError("synthetic generator failure")


def test_failed_label_is_not_persisted_or_marked_processed() -> None:
    repository = _MemoryRepository([_record("a1", "cluster-a-1")])
    generator = _FailingGenerator()
    embedding = _EmbeddingProvider()
    miner = ProductionLogMiner(
        repository=repository,
        embedding_provider=embedding,
        reference_generator=generator,
        clusterer=HDBSCANClusterer(min_cluster_size=3, min_samples=2),
        now=lambda: NOW,
    )
    first = miner.run_once()
    second = miner.run_once()
    assert first.error_count == second.error_count == 1
    assert first.candidate_count == second.candidate_count == 0
    assert repository.candidates == []
    assert repository.fingerprints == set()
    assert generator.calls == 2
    assert first.embedded_count == 1
    assert second.embedded_count == 0
    assert len(embedding.calls) == 1


class _UnavailableRepository(_MemoryRepository):
    def claim_run(self, **kwargs):
        raise ConnectionError("contains details that must not be persisted")


def test_claim_failure_returns_sanitized_typed_summary() -> None:
    miner = ProductionLogMiner(
        repository=_UnavailableRepository([]),
        embedding_provider=_EmbeddingProvider(),
        reference_generator=_Generator(),
        clusterer=_NoiseClusterer(),
        now=lambda: NOW,
    )
    summary = miner.run_once()
    assert summary.status is RunStatus.FAILED
    assert summary.run_id is None
    assert summary.error_message == "ConnectionError"


class _LockedRepository(_MemoryRepository):
    def claim_run(self, **kwargs):
        return None


def test_overlapping_run_is_skipped_and_audited() -> None:
    repository = _LockedRepository([])
    miner = ProductionLogMiner(
        repository=repository,
        embedding_provider=_EmbeddingProvider(),
        reference_generator=_Generator(),
        clusterer=_NoiseClusterer(),
        now=lambda: NOW,
    )
    summary = miner.run_once()
    assert summary.status is RunStatus.SKIPPED
    assert summary.run_id is not None
    assert len(repository.skipped) == 1


def test_check_command_emits_content_free_health_json_without_providers(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(repository_module, "check_log_source", lambda _dsn: 7)
    monkeypatch.setattr(
        extractor_module,
        "build_default_miner",
        lambda _settings: pytest.fail("check must not construct model providers"),
    )

    exit_code = extractor_module.main(["check"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "eligible_count": 7,
        "schema": "ready",
        "status": "ok",
    }


def test_check_command_sanitizes_database_failures(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sensitive_detail = "postgresql://user:secret@example/prompt-row"

    def unavailable(_dsn: str) -> int:
        raise RuntimeError(sensitive_detail)

    monkeypatch.setattr(repository_module, "check_log_source", unavailable)

    exit_code = extractor_module.main(["check"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert sensitive_detail not in captured.out
    assert sensitive_detail not in captured.err
    assert json.loads(captured.out) == {
        "eligible_count": 0,
        "schema": "invalid",
        "status": "error",
    }
