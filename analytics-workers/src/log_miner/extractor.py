"""Production log-miner orchestration and command-line entry point.

The pure pipeline accepts injected persistence, embedding, generation, and
clustering adapters. Concrete PostgreSQL/OpenAI dependencies are composed only
for the ``migrate``, ``run``, and ``schedule`` worker commands. The ``check``
command inspects PostgreSQL only and never constructs an external provider.

A renewable PostgreSQL lease fences database effects while provider calls run
without holding a database connection. Prompt/model embeddings are cached so
the full rolling population can be reclustered without repeated provider cost.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import threading
import unicodedata
import warnings
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

import numpy as np

from .models import (
    Candidate,
    CandidateStatus,
    EmbeddingProvider,
    InteractionRecord,
    LogMinerRepository,
    ReferenceAnswerGenerator,
    RunClaim,
    RunClaimLostError,
    RunStatus,
    RunSummary,
)


logger = logging.getLogger(__name__)

INPUT_NORMALIZATION_VERSION = "nfkc-whitespace-v1"
DEFAULT_LEASE_NAME = "production-log-miner"


def normalize_prompt(prompt: str) -> str:
    """Create a stable, content-preserving representation for deduplication."""

    return " ".join(unicodedata.normalize("NFKC", prompt).split())


def build_prompt_fingerprint(prompt: str) -> str:
    """Fingerprint the exact canonical string submitted for embedding."""

    return hashlib.sha256(normalize_prompt(prompt).encode("utf-8")).hexdigest()


def build_source_fingerprint(
    *, feature_scope: str, representative_prompt: str, version: str = "1.0"
) -> str:
    normalized = normalize_prompt(representative_prompt)
    material = f"{version}\0{feature_scope}\0{normalized}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def validate_and_normalize_embeddings(
    vectors: Sequence[Sequence[float]], *, expected_count: int | None = None
) -> np.ndarray:
    """Validate one finite, non-zero, fixed-width vector per source prompt."""

    try:
        matrix = np.asarray(vectors, dtype=np.float64)
    except (TypeError, ValueError) as error:
        raise ValueError("embeddings must have consistent numeric dimensions") from error
    if matrix.ndim != 2 or matrix.shape[1] == 0:
        raise ValueError("embeddings must be a non-empty two-dimensional matrix")
    if expected_count is not None and matrix.shape[0] != expected_count:
        raise ValueError(
            f"embedding provider returned {matrix.shape[0]} vectors for {expected_count} prompts"
        )
    if not np.isfinite(matrix).all():
        raise ValueError("embeddings must contain only finite values")
    norms = np.linalg.norm(matrix, axis=1)
    if np.any(norms <= 0.0):
        raise ValueError("embeddings must not contain zero-length vectors")
    return matrix / norms[:, np.newaxis]


@dataclass(frozen=True)
class ClusterAssignments:
    labels: np.ndarray
    outlier_scores: np.ndarray


class Clusterer(Protocol):
    def cluster(self, vectors: np.ndarray) -> ClusterAssignments: ...


class HDBSCANClusterer:
    """Thin adapter around HDBSCAN with Phase 4.1 production defaults."""

    def __init__(
        self,
        *,
        min_cluster_size: int = 3,
        min_samples: int = 2,
        metric: str = "euclidean",
        cluster_selection_method: str = "eom",
    ) -> None:
        self.min_cluster_size = min_cluster_size
        self.min_samples = min_samples
        self.metric = metric
        self.cluster_selection_method = cluster_selection_method

    def cluster(self, vectors: np.ndarray) -> ClusterAssignments:
        if vectors.ndim != 2:
            raise ValueError("cluster input must be a two-dimensional matrix")
        if len(vectors) < self.min_cluster_size:
            return ClusterAssignments(
                labels=np.full(len(vectors), -1, dtype=int),
                outlier_scores=np.ones(len(vectors), dtype=float),
            )

        import hdbscan

        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=".*force_all_finite.*ensure_all_finite.*",
                category=FutureWarning,
                module=r"sklearn\.utils\.deprecation",
            )
            model = hdbscan.HDBSCAN(
                min_cluster_size=self.min_cluster_size,
                min_samples=self.min_samples,
                metric=self.metric,
                cluster_selection_method=self.cluster_selection_method,
            ).fit(vectors)
        labels = np.asarray(model.labels_, dtype=int)
        scores = np.asarray(model.outlier_scores_, dtype=float)
        if labels.shape != (len(vectors),) or scores.shape != (len(vectors),):
            raise ValueError("HDBSCAN returned assignments with unexpected dimensions")
        return ClusterAssignments(labels=labels, outlier_scores=scores)


def select_medoid(vectors: np.ndarray) -> int:
    """Return the stable medoid without allocating an N-by-N-by-D tensor."""

    if vectors.ndim != 2 or len(vectors) == 0:
        raise ValueError("medoid selection requires at least one vector")
    squared_norms = np.einsum("ij,ij->i", vectors, vectors)
    distance_sums = np.empty(len(vectors), dtype=np.float64)
    block_size = 256
    for start in range(0, len(vectors), block_size):
        stop = min(start + block_size, len(vectors))
        squared_distances = (
            squared_norms[start:stop, np.newaxis]
            + squared_norms[np.newaxis, :]
            - 2.0 * (vectors[start:stop] @ vectors.T)
        )
        np.maximum(squared_distances, 0.0, out=squared_distances)
        np.sqrt(squared_distances, out=squared_distances)
        distance_sums[start:stop] = squared_distances.sum(axis=1)
    return int(np.argmin(distance_sums))


def is_eligible(
    record: InteractionRecord,
    *,
    window_start: datetime,
    window_end: datetime,
    score_threshold: int = 4,
) -> bool:
    """Apply the authoritative feedback/score rule within the rolling window."""

    created_at = _as_utc(record.created_at)
    in_window = _as_utc(window_start) <= created_at <= _as_utc(window_end)
    low_score = (
        record.min_arbitration_score is not None
        and record.min_arbitration_score < score_threshold
    )
    return in_window and (record.negative_feedback or low_score)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _safe_error_message(error: BaseException) -> str:
    """Keep provider/driver payloads out of persistent run summaries."""

    status_code = getattr(error, "status_code", None)
    suffix = f" (status {status_code})" if isinstance(status_code, int) else ""
    return f"{type(error).__name__}{suffix}"


@dataclass(frozen=True)
class _CandidateSeed:
    representative: InteractionRecord
    members: tuple[InteractionRecord, ...]
    cluster_examples: tuple[str, ...]
    cluster_label: int | None
    is_noise: bool
    outlier_score: float | None


def _finite_outlier_score(value: float) -> float:
    return float(value) if np.isfinite(value) else 0.0


def _nearest_distinct_examples(
    *,
    representative_index: int,
    member_indices: Sequence[int],
    records: Sequence[InteractionRecord],
    vectors: np.ndarray,
    maximum: int,
) -> tuple[str, ...]:
    if maximum <= 0:
        return ()
    representative_vector = vectors[representative_index]
    ordered = sorted(
        (index for index in member_indices if index != representative_index),
        key=lambda index: (
            float(np.linalg.norm(vectors[index] - representative_vector)),
            _as_utc(records[index].created_at),
            records[index].trace_id,
        ),
    )
    examples: list[str] = []
    seen = {normalize_prompt(records[representative_index].redacted_prompt)}
    for index in ordered:
        normalized = normalize_prompt(records[index].redacted_prompt)
        if normalized in seen:
            continue
        seen.add(normalized)
        examples.append(records[index].redacted_prompt)
        if len(examples) >= maximum:
            break
    return tuple(examples)


def _build_candidate_seeds(
    *,
    records: Sequence[InteractionRecord],
    vectors: np.ndarray,
    assignments: ClusterAssignments,
    max_noise: int,
    max_cluster_examples: int,
) -> list[_CandidateSeed]:
    if len(assignments.labels) != len(records) or len(assignments.outlier_scores) != len(records):
        raise ValueError("cluster assignments must align with source records")

    seeds: list[_CandidateSeed] = []
    cluster_labels = sorted({int(label) for label in assignments.labels if label >= 0})
    for label in cluster_labels:
        member_indices = [
            index for index, assigned in enumerate(assignments.labels) if int(assigned) == label
        ]
        local_medoid = select_medoid(vectors[member_indices])
        representative_index = member_indices[local_medoid]
        seeds.append(
            _CandidateSeed(
                representative=records[representative_index],
                members=tuple(records[index] for index in member_indices),
                cluster_examples=_nearest_distinct_examples(
                    representative_index=representative_index,
                    member_indices=member_indices,
                    records=records,
                    vectors=vectors,
                    maximum=max_cluster_examples,
                ),
                cluster_label=label,
                is_noise=False,
                outlier_score=_finite_outlier_score(
                    float(assignments.outlier_scores[representative_index])
                ),
            )
        )

    if max_noise > 0:
        noise_indices = [
            index for index, assigned in enumerate(assignments.labels) if int(assigned) == -1
        ]
        noise_indices.sort(
            key=lambda index: (
                -_finite_outlier_score(float(assignments.outlier_scores[index])),
                _as_utc(records[index].created_at),
                records[index].trace_id,
            )
        )
        for index in noise_indices[:max_noise]:
            seeds.append(
                _CandidateSeed(
                    representative=records[index],
                    members=(records[index],),
                    cluster_examples=(),
                    cluster_label=None,
                    is_noise=True,
                    outlier_score=_finite_outlier_score(
                        float(assignments.outlier_scores[index])
                    ),
                )
            )
    return seeds


def _empty_counts() -> dict[str, int]:
    return {
        "eligible_count": 0,
        "embedded_count": 0,
        "candidate_count": 0,
        "promoted_count": 0,
        "pending_review_count": 0,
        "duplicate_count": 0,
        "error_count": 0,
        "purged_count": 0,
    }


@dataclass(frozen=True)
class _MemberMerge:
    source_fingerprint: str
    member_trace_ids: tuple[str, ...]
    cluster_label: int | None
    is_noise: bool
    outlier_score: float | None
    nearest_example_count: int


class _LeaseHeartbeat:
    """Renew a durable claim without retaining a checked-out DB connection."""

    def __init__(
        self,
        *,
        repository: LogMinerRepository,
        claim: RunClaim,
        lease_ttl_seconds: int,
        interval_seconds: int,
    ) -> None:
        if lease_ttl_seconds < 1 or interval_seconds < 1:
            raise ValueError("lease TTL and heartbeat interval must be positive")
        if interval_seconds >= lease_ttl_seconds:
            raise ValueError("heartbeat interval must be shorter than lease TTL")
        self.repository = repository
        self.claim = claim
        self.lease_ttl_seconds = lease_ttl_seconds
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"log-miner-heartbeat-{claim.run_id}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                renewed = self.repository.renew_claim(
                    claim=self.claim,
                    lease_ttl_seconds=self.lease_ttl_seconds,
                )
            except Exception:
                logger.exception(
                    "log-miner lease heartbeat failed",
                    extra={"run_id": str(self.claim.run_id)},
                )
                renewed = False
            if not renewed:
                self._lost.set()
                return

    def ensure_active(self) -> None:
        if self._lost.is_set():
            raise RunClaimLostError("log-miner run lease heartbeat was lost")
        try:
            active = self.repository.claim_is_active(claim=self.claim)
        except Exception:
            self._lost.set()
            raise
        if not active:
            self._lost.set()
            raise RunClaimLostError("log-miner run lease is no longer active")

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=max(1.0, float(self.interval_seconds) + 1.0))


class ProductionLogMiner:
    """Coordinate one idempotent, audited production-failure mining run."""

    def __init__(
        self,
        *,
        repository: LogMinerRepository,
        embedding_provider: EmbeddingProvider,
        reference_generator: ReferenceAnswerGenerator,
        clusterer: Clusterer,
        window_days: int = 30,
        retention_days: int = 30,
        score_threshold: int = 4,
        max_noise_per_feature: int = 20,
        max_cluster_examples: int = 3,
        promotion_confidence: float = 0.80,
        fingerprint_version: str = "1.0",
        lease_name: str = DEFAULT_LEASE_NAME,
        lease_ttl_seconds: int = 300,
        heartbeat_interval_seconds: int = 60,
        embedding_input_version: str = INPUT_NORMALIZATION_VERSION,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not lease_name:
            raise ValueError("lease_name is required")
        if lease_ttl_seconds < 1 or heartbeat_interval_seconds < 1:
            raise ValueError("lease TTL and heartbeat interval must be positive")
        if heartbeat_interval_seconds >= lease_ttl_seconds:
            raise ValueError("heartbeat interval must be shorter than lease TTL")
        self.repository = repository
        self.embedding_provider = embedding_provider
        self.reference_generator = reference_generator
        self.clusterer = clusterer
        self.window_days = window_days
        self.retention_days = retention_days
        self.score_threshold = score_threshold
        self.max_noise_per_feature = max_noise_per_feature
        self.max_cluster_examples = max_cluster_examples
        self.promotion_confidence = promotion_confidence
        self.fingerprint_version = fingerprint_version
        self.lease_name = lease_name
        self.lease_ttl_seconds = lease_ttl_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.embedding_input_version = embedding_input_version
        self._now = now or (lambda: datetime.now(timezone.utc))

    @property
    def embedding_provider_name(self) -> str:
        return str(
            getattr(
                self.embedding_provider,
                "provider_name",
                type(self.embedding_provider).__name__,
            )
        )

    @property
    def embedding_model(self) -> str:
        return str(
            getattr(
                self.embedding_provider,
                "model",
                type(self.embedding_provider).__name__,
            )
        )

    def configuration(self) -> dict[str, Any]:
        cluster_config = {
            key: getattr(self.clusterer, key)
            for key in (
                "min_cluster_size",
                "min_samples",
                "metric",
                "cluster_selection_method",
            )
            if hasattr(self.clusterer, key)
        }
        return {
            "window_days": self.window_days,
            "retention_days": self.retention_days,
            "score_threshold": self.score_threshold,
            "max_noise_per_feature": self.max_noise_per_feature,
            "max_cluster_examples": self.max_cluster_examples,
            "promotion_confidence": self.promotion_confidence,
            "fingerprint_version": self.fingerprint_version,
            "lease_name": self.lease_name,
            "lease_ttl_seconds": self.lease_ttl_seconds,
            "heartbeat_interval_seconds": self.heartbeat_interval_seconds,
            "embedding_provider": type(self.embedding_provider).__name__,
            "embedding_model": self.embedding_model,
            "embedding_input_version": self.embedding_input_version,
            "reference_generator": type(self.reference_generator).__name__,
            "reference_model": getattr(self.reference_generator, "model", None),
            "clustering": cluster_config,
        }

    def run_once(self) -> RunSummary:
        window_end = _as_utc(self._now())
        window_start = window_end - timedelta(days=self.window_days)

        try:
            return self._run_pipeline(window_start=window_start, window_end=window_end)
        except Exception as error:
            logger.exception("log-miner could not claim or start a run")
            return RunSummary(
                status=RunStatus.FAILED,
                window_start=window_start,
                window_end=window_end,
                error_count=1,
                error_message=_safe_error_message(error),
            )

    def _embed_records(
        self,
        *,
        records: Sequence[InteractionRecord],
        claim: RunClaim,
        heartbeat: _LeaseHeartbeat,
        counts: dict[str, int],
    ) -> np.ndarray:
        """Load valid cached vectors and embed only unique canonical misses."""

        if not records:
            return np.empty((0, 0), dtype=np.float64)

        canonical_by_fingerprint: dict[str, str] = {}
        record_fingerprints: list[str] = []
        for record in records:
            canonical = normalize_prompt(record.redacted_prompt)
            fingerprint = build_prompt_fingerprint(canonical)
            if record.prompt_fingerprint and record.prompt_fingerprint != fingerprint:
                logger.warning(
                    "source prompt fingerprint did not match canonical embedding input",
                    extra={"trace_id": record.trace_id},
                )
            previous = canonical_by_fingerprint.setdefault(fingerprint, canonical)
            if previous != canonical:
                raise ValueError("prompt fingerprint collision detected")
            record_fingerprints.append(fingerprint)

        unique_fingerprints = list(canonical_by_fingerprint)
        heartbeat.ensure_active()
        cached_rows = self.repository.load_cached_embeddings(
            prompt_fingerprints=unique_fingerprints,
            provider_name=self.embedding_provider_name,
            embedding_model=self.embedding_model,
            input_version=self.embedding_input_version,
        )

        cached_vectors: dict[str, np.ndarray] = {}
        for fingerprint in unique_fingerprints:
            rows = cached_rows.get(fingerprint, [])
            if len(rows) != 1:
                continue
            row = rows[0]
            if (
                row.prompt_fingerprint != fingerprint
                or row.provider_name != self.embedding_provider_name
                or row.embedding_model != self.embedding_model
                or row.input_version != self.embedding_input_version
                or len(row.embedding_values) != row.embedding_dimensions
            ):
                continue
            try:
                normalized = validate_and_normalize_embeddings(
                    [row.embedding_values], expected_count=1
                )
            except ValueError:
                logger.warning(
                    "discarding invalid cached embedding",
                    extra={"prompt_fingerprint": fingerprint},
                )
                continue
            cached_vectors[fingerprint] = normalized[0]

        cached_dimensions = {len(vector) for vector in cached_vectors.values()}
        if len(cached_dimensions) > 1:
            logger.warning("discarding mixed-width cached embedding profile")
            cached_vectors.clear()
            cached_dimensions.clear()

        provider_embedded: set[str] = set()

        def embed_fingerprints(fingerprints: Sequence[str]) -> dict[str, np.ndarray]:
            if not fingerprints:
                return {}
            heartbeat.ensure_active()
            raw = self.embedding_provider.embed(
                [canonical_by_fingerprint[item] for item in fingerprints]
            )
            heartbeat.ensure_active()
            matrix = validate_and_normalize_embeddings(
                raw, expected_count=len(fingerprints)
            )
            provider_embedded.update(fingerprints)
            return {
                fingerprint: matrix[index]
                for index, fingerprint in enumerate(fingerprints)
            }

        missing = [
            fingerprint
            for fingerprint in unique_fingerprints
            if fingerprint not in cached_vectors
        ]
        fresh_vectors = embed_fingerprints(missing)
        if cached_vectors and fresh_vectors:
            fresh_dimensions = {len(vector) for vector in fresh_vectors.values()}
            if fresh_dimensions != cached_dimensions:
                logger.warning(
                    "cached embedding width changed; recomputing the cached profile"
                )
                cached_keys = list(cached_vectors)
                cached_vectors = embed_fingerprints(cached_keys)

        vectors_by_fingerprint = {**cached_vectors, **fresh_vectors}
        to_store = {
            fingerprint: vectors_by_fingerprint[fingerprint].tolist()
            for fingerprint in provider_embedded
        }
        if to_store:
            heartbeat.ensure_active()
            self.repository.store_cached_embeddings(
                claim=claim,
                provider_name=self.embedding_provider_name,
                embedding_model=self.embedding_model,
                input_version=self.embedding_input_version,
                embeddings=to_store,
            )
        counts["embedded_count"] = len(provider_embedded)
        return validate_and_normalize_embeddings(
            [vectors_by_fingerprint[item] for item in record_fingerprints],
            expected_count=len(records),
        )

    def _build_candidates(
        self,
        *,
        records: Sequence[InteractionRecord],
        matrix: np.ndarray,
        counts: dict[str, int],
        heartbeat: _LeaseHeartbeat,
    ) -> tuple[list[Candidate], list[_MemberMerge]]:
        """Cluster the full rolling population and label new representatives."""

        prepared: list[Candidate] = []
        merges: list[_MemberMerge] = []
        if not records:
            return prepared, merges
        if matrix.ndim != 2 or matrix.shape[0] != len(records):
            raise ValueError("embedding matrix must align with source records")

        feature_indices: dict[str, list[int]] = defaultdict(list)
        for index, record in enumerate(records):
            feature_indices[record.feature_scope].append(index)

        seeds: list[_CandidateSeed] = []
        for feature_scope in sorted(feature_indices):
            heartbeat.ensure_active()
            indices = feature_indices[feature_scope]
            feature_records = [records[index] for index in indices]
            feature_vectors = matrix[indices]
            assignments = self.clusterer.cluster(feature_vectors)
            heartbeat.ensure_active()
            seeds.extend(
                _build_candidate_seeds(
                    records=feature_records,
                    vectors=feature_vectors,
                    assignments=assignments,
                    max_noise=self.max_noise_per_feature,
                    max_cluster_examples=self.max_cluster_examples,
                )
            )

        for seed in seeds:
            fingerprint = build_source_fingerprint(
                feature_scope=seed.representative.feature_scope,
                representative_prompt=seed.representative.redacted_prompt,
                version=self.fingerprint_version,
            )
            if self.repository.source_fingerprint_exists(fingerprint):
                counts["duplicate_count"] += 1
                merges.append(
                    _MemberMerge(
                        source_fingerprint=fingerprint,
                        member_trace_ids=tuple(
                            member.trace_id for member in seed.members
                        ),
                        cluster_label=seed.cluster_label,
                        is_noise=seed.is_noise,
                        outlier_score=seed.outlier_score,
                        nearest_example_count=len(seed.cluster_examples),
                    )
                )
                continue
            try:
                heartbeat.ensure_active()
                generated = self.reference_generator.generate(
                    feature_scope=seed.representative.feature_scope,
                    representative_prompt=seed.representative.redacted_prompt,
                    cluster_examples=seed.cluster_examples,
                )
                heartbeat.ensure_active()
            except RunClaimLostError:
                raise
            except Exception:
                counts["error_count"] += 1
                logger.exception(
                    "reference generation failed",
                    extra={"trace_id": seed.representative.trace_id},
                )
                continue

            status = (
                CandidateStatus.PROMOTED
                if generated.label_confidence >= self.promotion_confidence
                else CandidateStatus.PENDING_REVIEW
            )
            prepared.append(
                Candidate(
                    source_fingerprint=fingerprint,
                    feature_scope=seed.representative.feature_scope,
                    user_input=seed.representative.redacted_prompt,
                    representative_trace_id=seed.representative.trace_id,
                    member_trace_ids=[member.trace_id for member in seed.members],
                    cluster_label=seed.cluster_label,
                    is_noise=seed.is_noise,
                    outlier_score=seed.outlier_score,
                    expected_output=generated.expected_output(),
                    difficulty_rating=generated.difficulty_rating,
                    label_confidence=generated.label_confidence,
                    status=status,
                    provenance={
                        "origin_source": "production_miner",
                        "fingerprint_version": self.fingerprint_version,
                        "source_prompt_fingerprint": seed.representative.prompt_fingerprint,
                        "member_count": len(seed.members),
                        "nearest_example_count": len(seed.cluster_examples),
                        "cluster_label": seed.cluster_label,
                        "is_noise": seed.is_noise,
                        "outlier_score": seed.outlier_score,
                        "embedding_provider": self.embedding_provider_name,
                        "embedding_model": self.embedding_model,
                        "embedding_dimensions": int(matrix.shape[1]),
                        "embedding_input_version": self.embedding_input_version,
                        "reference_model": getattr(
                            self.reference_generator, "model", None
                        ),
                    },
                )
            )
        return prepared, merges

    def _persist_prepared(
        self,
        *,
        claim: RunClaim,
        heartbeat: _LeaseHeartbeat,
        prepared: Sequence[Candidate],
        merges: Sequence[_MemberMerge],
        counts: dict[str, int],
    ) -> None:
        """Persist only while the durable claim remains active and fenced."""

        for merge in merges:
            heartbeat.ensure_active()
            self.repository.merge_candidate_members(
                claim=claim,
                source_fingerprint=merge.source_fingerprint,
                member_trace_ids=merge.member_trace_ids,
                cluster_label=merge.cluster_label,
                is_noise=merge.is_noise,
                outlier_score=merge.outlier_score,
                nearest_example_count=merge.nearest_example_count,
            )

        for candidate in prepared:
            heartbeat.ensure_active()
            persisted = self.repository.persist_candidate(
                claim=claim, candidate=candidate
            )
            if not persisted.created:
                counts["duplicate_count"] += 1
                continue
            counts["candidate_count"] += 1
            if candidate.status is CandidateStatus.PROMOTED:
                counts["promoted_count"] += 1
            else:
                counts["pending_review_count"] += 1

    def _fail_run(
        self,
        *,
        claim: RunClaim | None,
        window_start: datetime,
        window_end: datetime,
        counts: dict[str, int],
        error: BaseException,
    ) -> RunSummary:
        counts["error_count"] += 1
        summary = RunSummary(
            run_id=claim.run_id if claim is not None else None,
            status=RunStatus.FAILED,
            window_start=window_start,
            window_end=window_end,
            error_message=_safe_error_message(error),
            **counts,
        )
        if claim is None:
            return summary
        try:
            self.repository.fail_run(claim=claim, summary=summary)
        except Exception:
            logger.exception(
                "failed to update log-miner run audit",
                extra={"run_id": str(claim.run_id)},
            )
        logger.exception(
            "log-miner run failed", extra={"run_id": str(claim.run_id)}
        )
        return summary

    def _run_pipeline(
        self, *, window_start: datetime, window_end: datetime
    ) -> RunSummary:
        counts = _empty_counts()
        claim: RunClaim | None = None
        heartbeat: _LeaseHeartbeat | None = None
        try:
            configuration = self.configuration()
            claim = self.repository.claim_run(
                lease_name=self.lease_name,
                lease_ttl_seconds=self.lease_ttl_seconds,
                window_start=window_start,
                window_end=window_end,
                configuration=configuration,
            )
            if claim is None:
                reason = "another log-miner run owns the durable lease"
                skipped_run_id = self.repository.record_skipped_run(
                    window_start=window_start,
                    window_end=window_end,
                    configuration=configuration,
                    reason=reason,
                )
                return RunSummary(
                    run_id=skipped_run_id,
                    status=RunStatus.SKIPPED,
                    window_start=window_start,
                    window_end=window_end,
                    error_message=reason,
                )

            heartbeat = _LeaseHeartbeat(
                repository=self.repository,
                claim=claim,
                lease_ttl_seconds=self.lease_ttl_seconds,
                interval_seconds=self.heartbeat_interval_seconds,
            )
            heartbeat.start()
            heartbeat.ensure_active()
            records = self.repository.fetch_eligible(
                window_start=window_start,
                window_end=window_end,
                score_threshold=self.score_threshold,
            )
            records = sorted(
                (
                    record
                    for record in records
                    if is_eligible(
                        record,
                        window_start=window_start,
                        window_end=window_end,
                        score_threshold=self.score_threshold,
                    )
                ),
                key=lambda record: (_as_utc(record.created_at), record.trace_id),
            )
            counts["eligible_count"] = len(records)
            matrix = self._embed_records(
                records=records,
                claim=claim,
                heartbeat=heartbeat,
                counts=counts,
            )
            prepared, merges = self._build_candidates(
                records=records,
                matrix=matrix,
                counts=counts,
                heartbeat=heartbeat,
            )
            self._persist_prepared(
                claim=claim,
                heartbeat=heartbeat,
                prepared=prepared,
                merges=merges,
                counts=counts,
            )
            heartbeat.stop()
            heartbeat.ensure_active()
            provisional = RunSummary(
                run_id=claim.run_id,
                status=RunStatus.COMPLETED,
                window_start=window_start,
                window_end=window_end,
                **counts,
            )
            purged_count = self.repository.finalize_run(
                claim=claim,
                summary=provisional,
                retention_cutoff=window_end - timedelta(days=self.retention_days),
            )
            return provisional.model_copy(update={"purged_count": purged_count})
        except Exception as error:
            if heartbeat is not None:
                heartbeat.stop()
            return self._fail_run(
                claim=claim,
                window_start=window_start,
                window_end=window_end,
                counts=counts,
                error=error,
            )
        finally:
            if heartbeat is not None:
                heartbeat.stop()


def build_default_miner(worker_settings: Any) -> ProductionLogMiner:
    """Compose production adapters without exposing credentials in run metadata."""

    from .providers import OpenAIEmbeddingProvider, OpenAIReferenceAnswerGenerator
    from .repository import PostgresLogMinerRepository

    return ProductionLogMiner(
        repository=PostgresLogMinerRepository(worker_settings.postgres_dsn),
        embedding_provider=OpenAIEmbeddingProvider(
            api_key=worker_settings.openai_api_key,
            model=worker_settings.log_miner_embedding_model,
            batch_size=worker_settings.log_miner_embedding_batch_size,
            retry_attempts=worker_settings.log_miner_provider_retry_attempts,
        ),
        reference_generator=OpenAIReferenceAnswerGenerator(
            api_key=worker_settings.openai_api_key,
            model=worker_settings.log_miner_reference_model,
            retry_attempts=worker_settings.log_miner_provider_retry_attempts,
        ),
        clusterer=HDBSCANClusterer(
            min_cluster_size=worker_settings.log_miner_min_cluster_size,
            min_samples=worker_settings.log_miner_min_samples,
            metric=worker_settings.log_miner_cluster_metric,
            cluster_selection_method=worker_settings.log_miner_cluster_selection_method,
        ),
        window_days=worker_settings.log_miner_window_days,
        retention_days=worker_settings.log_miner_retention_days,
        score_threshold=worker_settings.log_miner_score_threshold,
        max_noise_per_feature=worker_settings.log_miner_max_noise_per_feature,
        max_cluster_examples=worker_settings.log_miner_max_cluster_examples,
        promotion_confidence=worker_settings.log_miner_promotion_confidence,
        fingerprint_version=worker_settings.log_miner_fingerprint_version,
        lease_ttl_seconds=worker_settings.log_miner_lease_ttl_seconds,
        heartbeat_interval_seconds=(
            worker_settings.log_miner_heartbeat_interval_seconds
        ),
    )


def _emit_summary(summary: RunSummary) -> None:
    print(json.dumps(summary.model_dump(mode="json"), sort_keys=True))


def _emit_check(*, status: str, schema: str, eligible_count: int) -> None:
    """Emit a stable, content-free health payload for operators and Compose."""

    print(
        json.dumps(
            {
                "status": status,
                "schema": schema,
                "eligible_count": eligible_count,
            },
            sort_keys=True,
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CoreMesh production log miner")
    parser.add_argument("command", choices=("check", "migrate", "run", "schedule"))
    arguments = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    from src.config import settings

    if arguments.command == "check":
        from .repository import check_log_source

        try:
            eligible_count = check_log_source(settings.postgres_dsn)
        except Exception:
            # A health check must not echo driver messages: they can contain
            # connection details, SQL fragments, or row-level values.
            _emit_check(status="error", schema="invalid", eligible_count=0)
            return 1
        _emit_check(status="ok", schema="ready", eligible_count=eligible_count)
        return 0

    if arguments.command == "migrate":
        from .repository import apply_migration

        apply_migration(settings.postgres_dsn)
        logger.info("log-miner migration applied")
        return 0

    miner = build_default_miner(settings)
    if arguments.command == "run":
        summary = miner.run_once()
        _emit_summary(summary)
        return 1 if summary.status is RunStatus.FAILED else 0

    from .scheduler import run_scheduler

    run_scheduler(
        miner=miner,
        cron=settings.log_miner_cron,
        timezone_name=settings.log_miner_timezone,
        on_summary=_emit_summary,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
