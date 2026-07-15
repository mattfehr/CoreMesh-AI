"""Supervisor orchestration and memory integration tests.

System role:
    Protects planning, specialist dispatch, partial failure, memory, and
    arbitration contracts for the library-only agent workflow.
Dependencies:
    pytest-compatible discovery and injected in-memory tools/stores.
Side effects:
    Uses temporary/in-memory state only; no Redis, Chroma, database, or model
    provider is contacted.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.arbitration.consensus import (  # noqa: E402
    BLOCKED_RESPONSE,
    ConsensusStatus,
    ConsensusVerdict,
    CriticAssessmentSchema,
)
from src.agents.orchestrator import (  # noqa: E402
    ExecutionRequestPayload,
    InMemorySemanticMemory,
    InMemoryShortTermMemory,
    OrchestrationResult,
    OrchestratorDependencies,
    SpecialistName,
    _apply_arbitration,
    run_orchestration,
)
from src.tracing.forensics import ForensicsTracer  # noqa: E402


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
