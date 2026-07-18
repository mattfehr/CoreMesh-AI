"""Typed contracts shared by the production log-miner pipeline.

The worker deliberately depends on protocols rather than concrete OpenAI and
PostgreSQL adapters.  This keeps clustering deterministic in tests and makes
all external I/O explicit at the composition boundary.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Protocol, Sequence
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExpectedBehavior(str, Enum):
    ANSWER = "answer"
    REFUSE = "refuse"
    CLARIFY = "clarify"


class DifficultyRating(str, Enum):
    SIMPLE = "simple"
    MODERATE = "moderate"
    HARD = "hard"
    ADVERSARIAL = "adversarial"


class CandidateStatus(str, Enum):
    PENDING_REVIEW = "pending_review"
    PROMOTED = "promoted"


class RunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class InteractionRecord(_StrictModel):
    """Privacy-approved source row eligible for offline analysis."""

    trace_id: str = Field(min_length=1, max_length=64)
    feature_scope: str = Field(min_length=1, max_length=64)
    redacted_prompt: str = Field(min_length=1)
    prompt_fingerprint: str | None = Field(default=None, min_length=64, max_length=64)
    arbitration_scores: dict[str, int] = Field(default_factory=dict)
    min_arbitration_score: int | None = Field(default=None, ge=1, le=5)
    arbitration_status: str | None = Field(default=None, max_length=32)
    negative_feedback: bool = False
    created_at: datetime


class ValidationCriterion(_StrictModel):
    description: str = Field(min_length=1)
    required: bool = True


class ExpectedOutput(_StrictModel):
    reference_answer: str = Field(min_length=1)
    validation_criteria: list[ValidationCriterion] = Field(min_length=1)
    expected_behavior: ExpectedBehavior
    failure_pattern: str = Field(min_length=1)


class GeneratedReference(ExpectedOutput):
    """Structured generator output before persistence routing."""

    difficulty_rating: DifficultyRating
    label_confidence: float = Field(ge=0.0, le=1.0)

    def expected_output(self) -> ExpectedOutput:
        return ExpectedOutput.model_validate(
            self.model_dump(exclude={"difficulty_rating", "label_confidence"})
        )


class Candidate(_StrictModel):
    """One auditable mined case, either review-only or directly promoted."""

    source_fingerprint: str = Field(min_length=64, max_length=64)
    feature_scope: str = Field(min_length=1, max_length=64)
    user_input: str = Field(min_length=1)
    representative_trace_id: str = Field(min_length=1, max_length=64)
    member_trace_ids: list[str] = Field(min_length=1)
    cluster_label: int | None = None
    is_noise: bool = False
    outlier_score: float | None = None
    expected_output: ExpectedOutput
    difficulty_rating: DifficultyRating
    label_confidence: float = Field(ge=0.0, le=1.0)
    status: CandidateStatus
    provenance: dict[str, Any] = Field(default_factory=dict)


class PersistResult(_StrictModel):
    created: bool
    candidate_id: UUID | None = None
    golden_case_id: UUID | None = None


class RunClaim(_StrictModel):
    """Fencing token for one crash-recoverable miner run lease."""

    run_id: UUID
    lease_name: str = Field(min_length=1, max_length=128)
    claim_token: UUID
    lease_expires_at: datetime


class CachedEmbedding(_StrictModel):
    """Prompt-free embedding-cache row for one exact provider profile."""

    prompt_fingerprint: str = Field(min_length=64, max_length=64)
    provider_name: str = Field(min_length=1, max_length=128)
    embedding_model: str = Field(min_length=1, max_length=255)
    input_version: str = Field(min_length=1, max_length=64)
    embedding_dimensions: int = Field(gt=0)
    embedding_values: list[float] = Field(min_length=1)


class RunClaimLostError(RuntimeError):
    """Raised when a stale worker attempts a fenced database write."""


class RunSummary(_StrictModel):
    """Typed outcome emitted by both one-shot and scheduled executions."""

    run_id: UUID | None = None
    status: RunStatus
    window_start: datetime
    window_end: datetime
    eligible_count: int = Field(default=0, ge=0)
    embedded_count: int = Field(default=0, ge=0)
    candidate_count: int = Field(default=0, ge=0)
    promoted_count: int = Field(default=0, ge=0)
    pending_review_count: int = Field(default=0, ge=0)
    duplicate_count: int = Field(default=0, ge=0)
    error_count: int = Field(default=0, ge=0)
    purged_count: int = Field(default=0, ge=0)
    error_message: str | None = None


class EmbeddingProvider(Protocol):
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one dense vector per input text in input order."""


class ReferenceAnswerGenerator(Protocol):
    def generate(
        self,
        *,
        feature_scope: str,
        representative_prompt: str,
        cluster_examples: Sequence[str],
    ) -> GeneratedReference:
        """Generate and validate the label for a representative failure."""


class LogMinerRepository(Protocol):
    def claim_run(
        self,
        *,
        lease_name: str,
        lease_ttl_seconds: int,
        window_start: datetime,
        window_end: datetime,
        configuration: dict[str, Any],
    ) -> RunClaim | None: ...

    def renew_claim(
        self, *, claim: RunClaim, lease_ttl_seconds: int
    ) -> bool: ...

    def claim_is_active(self, *, claim: RunClaim) -> bool: ...

    def record_skipped_run(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
        configuration: dict[str, Any],
        reason: str,
    ) -> UUID: ...

    def fetch_eligible(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
        score_threshold: int,
    ) -> list[InteractionRecord]: ...

    def load_cached_embeddings(
        self,
        *,
        prompt_fingerprints: Sequence[str],
        provider_name: str,
        embedding_model: str,
        input_version: str,
    ) -> dict[str, list[CachedEmbedding]]: ...

    def store_cached_embeddings(
        self,
        *,
        claim: RunClaim,
        provider_name: str,
        embedding_model: str,
        input_version: str,
        embeddings: dict[str, list[float]],
    ) -> None: ...

    def source_fingerprint_exists(self, source_fingerprint: str) -> bool: ...

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
    ) -> bool: ...

    def persist_candidate(
        self, *, claim: RunClaim, candidate: Candidate
    ) -> PersistResult: ...

    def finalize_run(
        self,
        *,
        claim: RunClaim,
        summary: RunSummary,
        retention_cutoff: datetime,
    ) -> int: ...

    def fail_run(self, *, claim: RunClaim, summary: RunSummary) -> bool: ...
