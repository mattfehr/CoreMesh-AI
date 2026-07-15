"""Failure-forensics tracing and deliberate sub-agent failure tests.

All exporters and registries are isolated below pytest's temporary directory;
no model, Redis, Chroma, Qdrant, or configured SQL service is contacted.
"""

import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.agents.orchestrator import (  # noqa: E402
    ExecutionRequestPayload,
    InMemorySemanticMemory,
    InMemoryShortTermMemory,
    OrchestratorDependencies,
    SpecialistName,
    run_orchestration,
)
from src.arbitration.consensus import (  # noqa: E402
    ConsensusVerdict,
    CriticAssessmentSchema,
)
from src.tracing.forensics import (  # noqa: E402
    FailureCategory,
    FailureTrigger,
    ForensicsTracer,
    RootCauseAnalyzer,
    SerializedSpan,
    SpanCategory,
    _safe_attributes,
)


class PassingArbitrator:
    async def arbitrate(self, payload):
        assessments = [
            CriticAssessmentSchema(
                evaluation_dimension=dimension,
                assigned_score=5,
                flagged_anomalies=[],
                confidence_coefficient=0.9,
            )
            for dimension in ("factual", "logic", "completeness")
        ]
        return ConsensusVerdict.pass_verdict(payload, assessments)


class HealthyRAGTool:
    def run(self, request, step, observations):
        return {
            "query": request.payload_query,
            "results": [
                {
                    "chunk_id": "policy-1",
                    "source": "policy",
                    "reference_marker": "[policy:policy-1]",
                    "text": "safe fixture result",
                }
            ],
        }


class DeliberatelyFailingSQLTool:
    def run(self, request, step, observations):
        raise RuntimeError("DELIBERATE_SECRET_SUB_AGENT_FAILURE")


class UnusedDocumentTool:
    def run(self, request, step, observations):  # pragma: no cover - safety guard
        raise AssertionError("document specialist should not run")


def test_deliberate_sub_agent_error_writes_exact_json_root_cause(tmp_path):
    trace_directory = tmp_path / "traces"
    registry_path = trace_directory / "registry.sqlite3"
    tracer = ForensicsTracer(
        trace_directory=trace_directory,
        registry_path=registry_path,
    )
    dependencies = OrchestratorDependencies(
        rag_tool=HealthyRAGTool(),
        document_tool=UnusedDocumentTool(),
        sql_tool=DeliberatelyFailingSQLTool(),
        short_term_memory=InMemoryShortTermMemory(),
        semantic_memory=InMemorySemanticMemory(),
        arbitrator=PassingArbitrator(),
        forensics=tracer,
    )
    secret_query = "Lookup policy then analyze the database SENSITIVE_CUSTOMER_7788"

    try:
        result = run_orchestration(
            ExecutionRequestPayload(
                user_id="sensitive-user@example.test",
                feature_scope="forensics-test",
                payload_query=secret_query,
                session_context={"session_id": "forensics-failure-session"},
            ),
            dependencies,
        )

        assert result.status == "completed_with_errors"
        assert result.trace_id
        assert result.root_cause is not None
        assert result.root_cause.span_name == "coremesh.tool.sql_generation"
        assert result.root_cause.step_id == "step-sql-generation"
        assert result.root_cause.category == FailureCategory.EXECUTION_ERROR

        artifact_path = trace_directory / f"{result.trace_id}.json"
        artifact_text = artifact_path.read_text(encoding="utf-8")
        artifact = json.loads(artifact_text)
        assert artifact["trigger"] == FailureTrigger.EXECUTION_ERROR.value
        assert artifact["diagnosis"]["span_name"] == "coremesh.tool.sql_generation"
        assert artifact["tree"]["name"] == "coremesh.agent.workflow"
        assert artifact["summary"]["error_count"] == 2

        span_names = {span["name"] for span in artifact["spans"]}
        assert "coremesh.agent.node.supervisor" in span_names
        assert "coremesh.agent.node.rag_search" in span_names
        assert "coremesh.tool.rag_search" in span_names
        assert "coremesh.agent.node.sql_generation" in span_names
        assert "coremesh.tool.sql_generation" in span_names
        assert "coremesh.db.semantic_memory.lookup" in span_names

        assert secret_query not in artifact_text
        assert "sensitive-user@example.test" not in artifact_text
        assert "DELIBERATE_SECRET_SUB_AGENT_FAILURE" not in artifact_text

        with sqlite3.connect(registry_path) as connection:
            row = connection.execute(
                """
                SELECT trigger, root_cause_step_id, failure_category, artifact_path
                FROM trace_registry WHERE trace_id = ?
                """,
                (result.trace_id,),
            ).fetchone()
        assert row == (
            "execution_error",
            "step-sql-generation",
            "execution_error",
            str(artifact_path),
        )
    finally:
        tracer.shutdown()


def test_backward_analyzer_selects_first_confidence_drop_and_feedback_updates_registry(
    tmp_path,
):
    tracer = ForensicsTracer(
        trace_directory=tmp_path / "traces",
        registry_path=tmp_path / "traces" / "registry.sqlite3",
    )

    try:
        with tracer.execution("coremesh.agent.workflow") as execution:
            with tracer.span(
                "coremesh.tool.healthy",
                SpanCategory.TOOL,
                attributes={
                    "coremesh.step.id": "step-healthy",
                    "coremesh.step.index": 0,
                    "coremesh.quality.confidence": 0.92,
                },
            ):
                pass
            with tracer.span(
                "coremesh.tool.first_drop",
                SpanCategory.TOOL,
                attributes={
                    "coremesh.step.id": "step-first-drop",
                    "coremesh.step.index": 1,
                    "coremesh.quality.confidence": 0.50,
                },
            ):
                pass
            with tracer.span(
                "coremesh.tool.propagated",
                SpanCategory.TOOL,
                attributes={
                    "coremesh.step.id": "step-propagated",
                    "coremesh.step.index": 2,
                    "coremesh.quality.confidence": 0.40,
                },
            ):
                pass
            execution.set_outcome(
                status="blocked_by_arbitration",
                trigger=FailureTrigger.ARBITRATION_FAILURE,
                reasons=["logic_score_below_threshold"],
                final_confidence=0.40,
            )

        assert execution.diagnosis is not None
        assert execution.diagnosis.span_name == "coremesh.tool.first_drop"
        assert execution.diagnosis.category == FailureCategory.LOW_CONFIDENCE

        feedback_diagnosis = tracer.flag_negative_feedback(
            execution.trace_id,
            "PRIVATE_FEEDBACK_REASON",
        )
        assert feedback_diagnosis.span_name == "coremesh.tool.first_drop"
        stored = tracer.get_trace(execution.trace_id)
        assert stored.trigger == FailureTrigger.NEGATIVE_FEEDBACK
        assert stored.feedback is not None
        serialized = stored.model_dump_json()
        assert "PRIVATE_FEEDBACK_REASON" not in serialized
    finally:
        tracer.shutdown()


def test_analyzer_prefers_execution_error_over_low_confidence():
    span = SerializedSpan(
        trace_id="0" * 32,
        span_id="1" * 16,
        name="coremesh.tool.sql_generation",
        kind="INTERNAL",
        status="ERROR",
        start_time="2026-01-01T00:00:00+00:00",
        end_time="2026-01-01T00:00:01+00:00",
        duration_ms=1.0,
        attributes={
            "coremesh.step.id": "step-sql-generation",
            "coremesh.step.index": 0,
            "coremesh.quality.confidence": 0.1,
            "coremesh.arbitration.failed": True,
        },
    )
    diagnosis = RootCauseAnalyzer().analyze(
        "0" * 32,
        [span],
        FailureTrigger.EXECUTION_ERROR,
    )
    assert diagnosis.category == FailureCategory.EXECUTION_ERROR
    assert {item.signal for item in diagnosis.evidence} >= {
        "span_status",
        "low_confidence",
        "arbitration_failure",
    }


def test_safe_attributes_preserve_allowlisted_metrics_and_hash_bodies():
    safe = _safe_attributes(
        {
            "coremesh.sql.limit_applied": True,
            "coremesh.result.row_count": 3,
            "input_tokens": 12,
            "output_tokens": 8,
            "sql": "SELECT * FROM customers WHERE ssn = 'SECRET'",
            "prompt": "leak this prompt body",
            "coremesh.request.user_id": "sensitive-user@example.test",
            "exception.type": "RuntimeError",
        },
        max_length=256,
    )
    assert safe["coremesh.sql.limit_applied"] is True
    assert safe["coremesh.result.row_count"] == 3
    assert safe["input_tokens"] == 12
    assert safe["output_tokens"] == 8
    assert safe["exception.type"] == "RuntimeError"
    assert "sql" not in safe
    assert "prompt" not in safe
    assert "coremesh.request.user_id" not in safe
    assert "sql.sha256" in safe
    assert "prompt.sha256" in safe
    assert "coremesh.request.user_id.sha256" in safe
    assert "SECRET" not in str(safe)
    assert "leak this prompt body" not in str(safe)
