"""Inject safe failures and verify real HDBSCAN/PostgreSQL idempotency.

Run from ``analytics-workers`` after the base CoreMesh schema exists.  OpenAI
is never contacted unless ``--live-openai`` is supplied explicitly.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4


WORKER_ROOT = Path(__file__).resolve().parents[1]
if str(WORKER_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKER_ROOT))

from psycopg2 import connect
from psycopg2.extras import Json

from src.log_miner.extractor import HDBSCANClusterer, ProductionLogMiner
from src.log_miner.models import (
    DifficultyRating,
    ExpectedBehavior,
    GeneratedReference,
    RunStatus,
    ValidationCriterion,
)
from src.log_miner.providers import (
    OpenAIEmbeddingProvider,
    OpenAIReferenceAnswerGenerator,
)
from src.log_miner.repository import PostgresLogMinerRepository, apply_migration


DEFAULT_DSN = "postgresql://coremesh:coremesh_secret@localhost:5432/coremesh"


class DeterministicEmbeddingProvider:
    """Known geometry: two three-point clusters and one distant noise point."""

    provider_name = "deterministic"
    model = "manual-fixture-v1"

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self.vectors = vectors
        self.calls: list[tuple[str, ...]] = []

    def embed(self, texts):
        self.calls.append(tuple(texts))
        return [self.vectors[text] for text in texts]


class DeterministicReferenceGenerator:
    def generate(self, *, feature_scope, representative_prompt, cluster_examples):
        return GeneratedReference(
            reference_answer=(
                "Handle this synthetic regression using the documented, safe workflow "
                "and state any required assumptions."
            ),
            validation_criteria=[
                ValidationCriterion(
                    description="Uses the safe workflow without inventing private data",
                    required=True,
                ),
                ValidationCriterion(
                    description="Addresses the representative failure pattern",
                    required=True,
                ),
            ],
            expected_behavior=ExpectedBehavior.ANSWER,
            failure_pattern=f"Synthetic {feature_scope} production failure",
            difficulty_rating=DifficultyRating.MODERATE,
            label_confidence=0.95,
        )


def _fixtures(scope: str):
    fixture_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    prompts_and_vectors = [
        ("Synthetic invoice rounding failure variant A1", [1.0, 0.01, 0.0]),
        ("Synthetic invoice rounding failure variant A2", [1.0, 0.0, 0.01]),
        ("Synthetic invoice rounding failure variant A3", [1.0, -0.01, 0.0]),
        ("Synthetic citation omission failure variant B1", [0.0, 1.0, 0.01]),
        ("Synthetic citation omission failure variant B2", [0.01, 1.0, 0.0]),
        ("Synthetic citation omission failure variant B3", [0.0, 1.0, -0.01]),
        ("Synthetic unique schema mismatch failure", [-1.0, -1.0, 1.0]),
    ]
    rows = []
    vectors = {}
    for index, (prompt, vector) in enumerate(prompts_and_vectors):
        trace_id = f"verify-{uuid4().hex}"
        negative_feedback = index in {1, 4, 6}
        score = 5 if negative_feedback else (2 if index % 2 == 0 else 3)
        rows.append(
            (
                trace_id,
                scope,
                prompt,
                hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                Json({"accuracy": score, "safety": 5}),
                score,
                "approved",
                negative_feedback,
                fixture_time + timedelta(seconds=index),
            )
        )
        vectors[prompt] = vector
    return rows, vectors


def _insert_fixtures(postgres_dsn: str, rows) -> None:
    with connect(postgres_dsn) as connection:
        with connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO production_interaction_logs (
                    trace_id, feature_scope, redacted_prompt, prompt_fingerprint,
                    arbitration_scores, min_arbitration_score,
                    arbitration_status, negative_feedback, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                rows,
            )


def _counts(postgres_dsn: str, scope: str) -> tuple[int, int, int]:
    with connect(postgres_dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*), COUNT(*) FILTER (WHERE status = 'promoted')
                FROM log_miner_candidates WHERE feature_scope = %s
                """,
                (scope,),
            )
            candidate_count, promoted_count = cursor.fetchone()
            cursor.execute(
                "SELECT COUNT(*) FROM golden_datasets WHERE feature_scope = %s",
                (scope,),
            )
            golden_count = cursor.fetchone()[0]
    return candidate_count, promoted_count, golden_count


def _cleanup(
    postgres_dsn: str,
    scope: str,
    run_ids: list[UUID],
    prompt_fingerprints: list[str],
) -> None:
    with connect(postgres_dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM log_miner_candidates WHERE feature_scope = %s", (scope,))
            cursor.execute("DELETE FROM golden_datasets WHERE feature_scope = %s", (scope,))
            cursor.execute(
                "DELETE FROM production_interaction_logs WHERE feature_scope = %s", (scope,)
            )
            cursor.execute(
                "DELETE FROM log_miner_embedding_cache "
                "WHERE prompt_fingerprint = ANY(%s)",
                (prompt_fingerprints,),
            )
            if run_ids:
                cursor.execute("DELETE FROM log_miner_runs WHERE run_id = ANY(%s)", (run_ids,))


def _live_openai_smoke(api_key: str) -> None:
    if not api_key.strip():
        raise ValueError("--live-openai requires OPENAI_API_KEY")
    prompt = "Safe synthetic smoke test: explain why a response needs a citation."
    vectors = OpenAIEmbeddingProvider(api_key=api_key).embed([prompt])
    if len(vectors) != 1 or not vectors[0]:
        raise AssertionError("live embedding adapter returned no vector")
    label = OpenAIReferenceAnswerGenerator(api_key=api_key).generate(
        feature_scope="verification",
        representative_prompt=prompt,
        cluster_examples=[],
    )
    print(
        f"live OpenAI smoke passed: embedding_dimensions={len(vectors[0])}, "
        f"label_confidence={label.label_confidence:.2f}"
    )


def verify(args: argparse.Namespace) -> None:
    scope = f"manual-log-miner-{uuid4().hex[:12]}"
    run_ids: list[UUID] = []
    rows, vectors = _fixtures(scope)
    prompt_fingerprints = [row[3] for row in rows]

    apply_migration(args.postgres_dsn)
    if args.live_openai:
        _live_openai_smoke(os.getenv("OPENAI_API_KEY", ""))

    _insert_fixtures(args.postgres_dsn, rows)
    repository = PostgresLogMinerRepository(args.postgres_dsn)
    embedding_provider = DeterministicEmbeddingProvider(vectors)
    miner = ProductionLogMiner(
        repository=repository,
        embedding_provider=embedding_provider,
        reference_generator=DeterministicReferenceGenerator(),
        clusterer=HDBSCANClusterer(min_cluster_size=3, min_samples=2),
        max_noise_per_feature=20,
        promotion_confidence=0.80,
        now=lambda: datetime.now(timezone.utc),
    )

    try:
        first = miner.run_once()
        if first.run_id:
            run_ids.append(first.run_id)
        assert first.status is RunStatus.COMPLETED, first.model_dump(mode="json")
        assert (first.candidate_count, first.promoted_count) == (3, 3), first.model_dump(
            mode="json"
        )
        assert _counts(args.postgres_dsn, scope) == (3, 3, 3)

        second = miner.run_once()
        if second.run_id:
            run_ids.append(second.run_id)
        assert second.status is RunStatus.COMPLETED, second.model_dump(mode="json")
        assert second.eligible_count == 7, second.model_dump(mode="json")
        assert second.embedded_count == 0, second.model_dump(mode="json")
        assert second.candidate_count == 0, second.model_dump(mode="json")
        assert len(embedding_provider.calls) == 1
        assert _counts(args.postgres_dsn, scope) == (3, 3, 3)
        print(
            "verification passed: 7 source failures -> 2 dense clusters + 1 noise "
            "-> 3 candidates/golden cases; full-window rerun reused cached vectors"
        )
        print(f"fixture scope: {scope}")
    finally:
        if args.keep_fixtures:
            print(f"fixtures retained for inspection under feature_scope={scope}")
        else:
            _cleanup(args.postgres_dsn, scope, run_ids, prompt_fingerprints)
            print("synthetic fixtures cleaned")
        repository.engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--postgres-dsn",
        default=os.getenv("POSTGRES_DSN", DEFAULT_DSN),
        help="initialized CoreMesh PostgreSQL DSN",
    )
    parser.add_argument("--keep-fixtures", action="store_true")
    parser.add_argument(
        "--live-openai",
        action="store_true",
        help="explicitly smoke-test production OpenAI adapters before offline verification",
    )
    verify(parser.parse_args())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
