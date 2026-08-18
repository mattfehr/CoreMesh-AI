"""Supervisor orchestration and memory integration tests.

System role:
    Protects planning, specialist dispatch, partial failure, memory, and
    arbitration contracts for trusted callers and public execution scopes.
Dependencies:
    pytest-compatible discovery and injected in-memory tools/stores.
Side effects:
    Uses temporary/in-memory state only; no Redis, Chroma, database, or model
    provider is contacted.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.arbitration.consensus import (  # noqa: E402
    BLOCKED_RESPONSE,
    ConsensusArbitrator,
    ConsensusStatus,
    ConsensusVerdict,
    CriticAssessmentSchema,
    DeterministicAdjudicatorClient,
    DeterministicCriticClient,
)
from src.agents.orchestrator import (  # noqa: E402
    ExecutionRequestPayload,
    DocumentExtractionTool,
    InMemorySemanticMemory,
    InMemoryShortTermMemory,
    OrchestrationResult,
    OrchestratorDependencies,
    SpecialistName,
    _apply_arbitration,
    _create_plan,
    run_orchestration,
)
from src.tracing.forensics import ForensicsTracer  # noqa: E402
from src.tracing.production_logs import PromptRedactor  # noqa: E402


DISABLED_FORENSICS = ForensicsTracer(enabled=False)


def _clean_assessments():
    return [
        CriticAssessmentSchema(
            evaluation_dimension=dimension,
            assigned_score=5,
            flagged_anomalies=[],
            confidence_coefficient=0.9,
        )
        for dimension in ("factual", "logic", "completeness")
    ]


@pytest.mark.parametrize(
    ("feature_scope", "query", "expected"),
    [
        ("rag", "Analyze database revenue.", [SpecialistName.RAG_SEARCH]),
        ("text_to_sql", "Find the policy document.", [SpecialistName.SQL_GENERATION]),
    ],
)
def test_public_execution_scopes_force_one_specialist(feature_scope, query, expected):
    request = ExecutionRequestPayload(
        user_id="frontend-user",
        feature_scope=feature_scope,
        payload_query=query,
    )

    plan = _create_plan(request, [])

    assert [step.specialist for step in plan] == expected


def test_agent_orchestrator_scope_retains_cue_based_multi_step_planning():
    request = ExecutionRequestPayload(
        user_id="frontend-user",
        feature_scope="agent_orchestrator",
        payload_query="Search policy references and analyze the database count.",
    )

    plan = _create_plan(request, [])

    assert [step.specialist for step in plan] == [
        SpecialistName.RAG_SEARCH,
        SpecialistName.SQL_GENERATION,
    ]


class PassingArbitrator:
    def __init__(self):
        self.payloads = []

    async def arbitrate(self, payload):
        self.payloads.append(payload)
        return ConsensusVerdict.pass_verdict(payload, _clean_assessments())


class BlockingArbitrator:
    def __init__(self):
        self.payloads = []

    async def arbitrate(self, payload):
        self.payloads.append(payload)
        return ConsensusVerdict.blocked(
            payload,
            status=ConsensusStatus.BLOCKED,
            assessments=_clean_assessments(),
            triggered_by=["logic_flagged_anomalies"],
        )


class LowScoreArbitrator:
    async def arbitrate(self, payload):
        assessments = _clean_assessments()
        assessments[1] = assessments[1].model_copy(update={"assigned_score": 2})
        return ConsensusVerdict.blocked(
            payload,
            status=ConsensusStatus.BLOCKED,
            assessments=assessments,
            triggered_by=["logic_score_below_4"],
        )


class CapturingInteractionSink:
    def __init__(self):
        self.records = []
        self.all_writes = []
        self.feedback_trace_ids = []

    def record_interaction(self, record):
        # Mirror the PostgreSQL upsert: one logical row per trace.
        self.all_writes.append(record)
        for index, existing in enumerate(self.records):
            if existing.trace_id == record.trace_id:
                self.records[index] = record
                return
        self.records.append(record)

    def flag_negative_feedback(self, trace_id):
        self.feedback_trace_ids.append(trace_id)


class RaisingInteractionSink:
    def record_interaction(self, record):
        raise RuntimeError("deliberate interaction sink failure")

    def flag_negative_feedback(self, trace_id):
        raise RuntimeError("deliberate feedback sink failure")


class FakeRAGTool:
    def __init__(self, order):
        self.order = order
        self.calls = []

    def run(self, request, step, observations):
        self.order.append(SpecialistName.RAG_SEARCH)
        self.calls.append((request, step, list(observations)))
        return {
            "query": request.payload_query,
            "results": [
                {
                    "chunk_id": "retention-policy",
                    "source": "policy",
                    "reference_marker": "[policy:retention-policy]",
                    "text": "Invoices above 500 require finance review before analysis.",
                }
            ],
        }


class FakeDocumentTool:
    def __init__(self, order):
        self.order = order
        self.calls = []

    def run(self, request, step, observations):
        self.order.append(SpecialistName.DOCUMENT_EXTRACTION)
        self.calls.append((request, step, list(observations)))
        return {
            "extraction": {
                "vendor_name": "Acme Analytics",
                "invoice_id": "INV-100",
                "line_items": [{"description": "usage", "quantity": 1, "total": 600.0}],
                "calculated_tax": 48.0,
                "invoice_total": 648.0,
            },
            "validation": {"passed": True, "computed_sum": 648.0, "delta": 0.0},
        }


class FakeSQLTool:
    def __init__(self, order):
        self.order = order
        self.calls = []

    def run(self, request, step, observations):
        self.order.append(SpecialistName.SQL_GENERATION)
        self.calls.append((request, step, list(observations)))
        return {
            "sql": "SELECT vendor_name, SUM(invoice_total) AS total FROM invoices GROUP BY vendor_name",
            "columns": ["vendor_name", "total"],
            "rows": [{"vendor_name": "Acme Analytics", "total": 648.0}],
            "row_count": 1,
            "elapsed_ms": 2.5,
            "limit_applied": False,
            "prior_observations": [observation.specialist.value for observation in observations],
        }


class LogicalErrorSQLTool:
    def __init__(self, order):
        self.order = order
        self.calls = []

    def run(self, request, step, observations):
        self.order.append(SpecialistName.SQL_GENERATION)
        self.calls.append((request, step, list(observations)))
        return {
            "sql": "SELECT '2 + 2 = 5' AS claim",
            "columns": ["claim"],
            "rows": [{"claim": "2 + 2 = 5"}],
            "row_count": 1,
            "elapsed_ms": 1.0,
            "limit_applied": False,
            "prior_observations": [],
        }


def test_supervisor_splits_and_coordinates_multi_hop_document_and_db_workflow():
    invocation_order = []
    rag_tool = FakeRAGTool(invocation_order)
    document_tool = FakeDocumentTool(invocation_order)
    sql_tool = FakeSQLTool(invocation_order)
    short_term_memory = InMemoryShortTermMemory()
    semantic_memory = InMemorySemanticMemory(
        memories=[
            {
                "text": "Prior invoice analysis used policy lookup before SQL aggregation.",
                "metadata": {"session_id": "previous-session"},
            }
        ]
    )
    arbitrator = PassingArbitrator()
    dependencies = OrchestratorDependencies(
        rag_tool=rag_tool,
        document_tool=document_tool,
        sql_tool=sql_tool,
        short_term_memory=short_term_memory,
        semantic_memory=semantic_memory,
        arbitrator=arbitrator,
        forensics=DISABLED_FORENSICS,
    )
    request = ExecutionRequestPayload(
        user_id="user-123",
        feature_scope="finance",
        payload_query=(
            "Lookup the invoice policy document, extract the invoice fields, "
            "and analyze the database totals for this vendor."
        ),
        session_context={
            "session_id": "session-multihop",
            "document_text": (
                "VENDOR: Acme Analytics\n"
                "INVOICE_ID: INV-100\n"
                "LINEITEM: description=usage qty=1 unit=600.00 total=600.00\n"
                "TAX: 48.00\n"
                "TOTAL: 648.00"
            ),
            "sql_query": (
                "SELECT vendor_name, SUM(invoice_total) AS total "
                "FROM invoices GROUP BY vendor_name"
            ),
        },
    )

    result = run_orchestration(request, dependencies)

    assert result.status == "completed"
    assert [step.specialist for step in result.plan] == [
        SpecialistName.RAG_SEARCH,
        SpecialistName.DOCUMENT_EXTRACTION,
        SpecialistName.SQL_GENERATION,
    ]
    assert invocation_order == [
        SpecialistName.RAG_SEARCH,
        SpecialistName.DOCUMENT_EXTRACTION,
        SpecialistName.SQL_GENERATION,
    ]
    assert len(rag_tool.calls) == 1
    assert len(document_tool.calls) == 1
    assert len(sql_tool.calls) == 1
    assert [observation.specialist for observation in sql_tool.calls[0][2]] == [
        SpecialistName.RAG_SEARCH,
        SpecialistName.DOCUMENT_EXTRACTION,
    ]
    assert len(short_term_memory.states) >= 4
    assert short_term_memory.states[-1][0] == "session-multihop"
    assert len(semantic_memory.stored_results) == 1
    assert semantic_memory.stored_results[0].session_id == "session-multihop"
    assert len(arbitrator.payloads) == 1
    assert result.arbitration.status == ConsensusStatus.PASSED
    assert "[policy:retention-policy]" in result.final_response
    assert "Acme Analytics" in result.final_response
    assert "SQL analysis executed" in result.final_response


def test_orchestration_accepts_binary_document_bytes_in_session_context():
    invocation_order = []
    document_tool = FakeDocumentTool(invocation_order)
    arbitrator = PassingArbitrator()
    dependencies = OrchestratorDependencies(
        rag_tool=FakeRAGTool(invocation_order),
        document_tool=document_tool,
        sql_tool=FakeSQLTool(invocation_order),
        short_term_memory=InMemoryShortTermMemory(),
        semantic_memory=InMemorySemanticMemory(),
        arbitrator=arbitrator,
        forensics=DISABLED_FORENSICS,
    )
    raw_document = b"\x89PNG\r\n\x1a\n\xff\xfe invoice-bytes"
    request = ExecutionRequestPayload(
        user_id="user-binary",
        feature_scope="finance",
        payload_query="extract the invoice fields",
        session_context={
            "session_id": "session-binary",
            "document_bytes": raw_document,
            "document_filename": "invoice.png",
        },
    )

    result = run_orchestration(request, dependencies)

    assert result.status == "completed"
    assert invocation_order == [SpecialistName.DOCUMENT_EXTRACTION]
    assert len(document_tool.calls) == 1
    assert len(arbitrator.payloads) == 1


def test_orchestration_blocks_final_response_when_arbitration_flags_logical_error():
    invocation_order = []
    sql_tool = LogicalErrorSQLTool(invocation_order)
    arbitrator = BlockingArbitrator()
    dependencies = OrchestratorDependencies(
        rag_tool=FakeRAGTool(invocation_order),
        document_tool=FakeDocumentTool(invocation_order),
        sql_tool=sql_tool,
        short_term_memory=InMemoryShortTermMemory(),
        semantic_memory=InMemorySemanticMemory(),
        arbitrator=arbitrator,
        forensics=DISABLED_FORENSICS,
    )
    request = ExecutionRequestPayload(
        user_id="user-logical-error",
        feature_scope="analysis",
        payload_query="Analyze the database and return the claim.",
        session_context={"session_id": "session-logical-error"},
    )

    result = run_orchestration(request, dependencies)

    assert result.status == "blocked_by_arbitration"
    assert invocation_order == [SpecialistName.SQL_GENERATION]
    assert len(sql_tool.calls) == 1
    assert len(arbitrator.payloads) == 1
    assert "2 + 2 = 5" in arbitrator.payloads[0].output_text
    assert result.final_response == BLOCKED_RESPONSE
    assert "2 + 2 = 5" not in result.final_response
    assert result.arbitration.status == ConsensusStatus.BLOCKED


def test_skipped_document_workflow_is_deterministically_blocked_and_mineable():
    invocation_order = []
    sink = CapturingInteractionSink()
    arbitrator = ConsensusArbitrator(
        critics=[
            DeterministicCriticClient("factual"),
            DeterministicCriticClient("logic"),
            DeterministicCriticClient("completeness"),
        ],
        adjudicator=DeterministicAdjudicatorClient(),
        retry_attempts=1,
    )
    dependencies = OrchestratorDependencies(
        rag_tool=FakeRAGTool(invocation_order),
        document_tool=DocumentExtractionTool(),
        sql_tool=FakeSQLTool(invocation_order),
        short_term_memory=InMemoryShortTermMemory(),
        semantic_memory=InMemorySemanticMemory(),
        arbitrator=arbitrator,
        forensics=DISABLED_FORENSICS,
        interaction_log_sink=sink,
        interaction_log_redactor=PromptRedactor(),
    )

    result = run_orchestration(
        ExecutionRequestPayload(
            user_id="integration-user",
            feature_scope="agent_orchestrator",
            payload_query="Extract invoice data.",
            session_context={"session_id": "integration-arbitration"},
        ),
        dependencies,
    )

    assert result.status == "blocked_by_arbitration"
    assert [step.specialist for step in result.plan] == [
        SpecialistName.DOCUMENT_EXTRACTION
    ]
    assert result.plan[0].status == "skipped"
    assert result.observations[0].status == "skipped"
    assert result.observations[0].error is None
    assert "No document_text" in result.observations[0].output["reason"]
    assert result.final_response == BLOCKED_RESPONSE
    assert result.arbitration.status == ConsensusStatus.BLOCKED
    scores = {
        item.evaluation_dimension: item.assigned_score
        for item in result.arbitration.critic_assessments
    }
    assert scores == {"factual": 5, "logic": 5, "completeness": 2}
    assert "completeness_score_below_4" in result.arbitration.triggered_by
    assert len(sink.records) == 1
    assert sink.records[0].min_arbitration_score == 2


def test_orchestration_blocks_empty_final_response_without_crashing():
    request = ExecutionRequestPayload(
        user_id="user-empty",
        feature_scope="analysis",
        payload_query="Return nothing.",
        session_context={"session_id": "session-empty"},
    )
    empty_result = OrchestrationResult(
        session_id="session-empty",
        user_id="user-empty",
        feature_scope="analysis",
        status="completed",
        plan=[],
        observations=[],
        final_response="",
    )
    dependencies = OrchestratorDependencies(
        short_term_memory=InMemoryShortTermMemory(),
        semantic_memory=InMemorySemanticMemory(),
        arbitrator=PassingArbitrator(),
        forensics=DISABLED_FORENSICS,
    )

    result = _apply_arbitration(dependencies, empty_result, request)

    assert result.status == "blocked_by_arbitration"
    assert result.final_response == BLOCKED_RESPONSE
    assert result.arbitration is not None
    assert "empty_final_response" in result.arbitration.triggered_by
    assert len(dependencies.arbitrator.payloads) == 0


def test_orchestration_publishes_only_redacted_prompt_and_bounded_scores():
    invocation_order = []
    sink = CapturingInteractionSink()
    dependencies = OrchestratorDependencies(
        rag_tool=FakeRAGTool(invocation_order),
        document_tool=FakeDocumentTool(invocation_order),
        sql_tool=FakeSQLTool(invocation_order),
        short_term_memory=InMemoryShortTermMemory(),
        semantic_memory=InMemorySemanticMemory(),
        arbitrator=LowScoreArbitrator(),
        forensics=DISABLED_FORENSICS,
        interaction_log_sink=sink,
        interaction_log_redactor=PromptRedactor(),
    )

    result = run_orchestration(
        ExecutionRequestPayload(
            user_id="private-user@example.test",
            feature_scope="support",
            payload_query="Lookup policy for alice@example.com",
            session_context={"session_id": "production-log-test"},
        ),
        dependencies,
    )

    assert result.status == "blocked_by_arbitration"
    assert len(sink.all_writes) == 2
    assert sink.all_writes[0].arbitration_status == "pending"
    assert len(sink.records) == 1
    record = sink.records[0]
    assert record.trace_id == result.trace_id
    assert "alice@example.com" not in record.redacted_prompt
    assert "private-user@example.test" not in str(record)
    assert record.arbitration_scores == {"factual": 5, "logic": 2, "completeness": 5}
    assert record.min_arbitration_score == 2
    assert record.arbitration_status == ConsensusStatus.BLOCKED.value


def test_interaction_log_published_before_workflow_failure(monkeypatch):
    sink = CapturingInteractionSink()
    dependencies = OrchestratorDependencies(
        short_term_memory=InMemoryShortTermMemory(),
        semantic_memory=InMemorySemanticMemory(),
        arbitrator=PassingArbitrator(),
        forensics=DISABLED_FORENSICS,
        interaction_log_sink=sink,
        interaction_log_redactor=PromptRedactor(),
    )

    def boom(_deps):
        return SimpleNamespace(
            invoke=lambda _state: (_ for _ in ()).throw(RuntimeError("workflow boom"))
        )

    monkeypatch.setattr(
        "src.agents.orchestrator.build_supervisor_graph",
        boom,
    )

    with pytest.raises(RuntimeError, match="workflow boom"):
        run_orchestration(
            ExecutionRequestPayload(
                user_id="user-early-log",
                feature_scope="support",
                payload_query="Lookup policy for early publishing.",
            ),
            dependencies,
        )

    assert len(sink.records) == 1
    assert sink.records[0].arbitration_status == "pending"
    assert sink.records[0].redacted_prompt


def test_interaction_sink_failure_is_fail_open():
    invocation_order = []
    dependencies = OrchestratorDependencies(
        rag_tool=FakeRAGTool(invocation_order),
        document_tool=FakeDocumentTool(invocation_order),
        sql_tool=FakeSQLTool(invocation_order),
        short_term_memory=InMemoryShortTermMemory(),
        semantic_memory=InMemorySemanticMemory(),
        arbitrator=PassingArbitrator(),
        forensics=DISABLED_FORENSICS,
        interaction_log_sink=RaisingInteractionSink(),
    )

    result = run_orchestration(
        ExecutionRequestPayload(
            user_id="user-fail-open",
            feature_scope="support",
            payload_query="Lookup the support policy.",
        ),
        dependencies,
    )

    assert result.status == "completed"
    assert result.arbitration is not None
    assert result.arbitration.delivery_allowed is True


def test_dependencies_do_not_overwrite_a_shared_forensics_sink():
    original_sink = CapturingInteractionSink()
    publisher_sink = CapturingInteractionSink()
    shared_forensics = ForensicsTracer(
        enabled=False,
        interaction_log_sink=original_sink,
    )
    try:
        dependencies = OrchestratorDependencies(
            short_term_memory=InMemoryShortTermMemory(),
            semantic_memory=InMemorySemanticMemory(),
            arbitrator=PassingArbitrator(),
            forensics=shared_forensics,
            interaction_log_sink=publisher_sink,
        )
        assert dependencies.interaction_log_sink is publisher_sink
        assert shared_forensics.interaction_log_sink is original_sink
    finally:
        shared_forensics.shutdown()
