"""Unified execution and forensic HTTP boundary tests.

System role:
    Protects the browser-facing request whitelist, orchestration delegation,
    trace pagination, and redacted artifact lookup.
Dependencies:
    FastAPI TestClient plus injected orchestration and forensics fakes.
Side effects:
    Writes isolated forensic artifacts below pytest temporary directories.
"""

import sys
import threading
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import src.main as runtime_main  # noqa: E402
from src.agents.orchestrator import OrchestrationResult  # noqa: E402
from src.tracing.forensics import ForensicsTracer, SpanCategory  # noqa: E402


client = TestClient(runtime_main.app)


def test_orchestrator_dependencies_are_lazily_reused(monkeypatch):
    sentinel = object()
    constructions = []
    if hasattr(runtime_main.app.state, "orchestrator_dependencies"):
        del runtime_main.app.state.orchestrator_dependencies

    def build_dependencies():
        constructions.append(True)
        return sentinel

    monkeypatch.setattr(runtime_main, "OrchestratorDependencies", build_dependencies)
    try:
        first = runtime_main.get_orchestrator_dependencies()
        second = runtime_main.get_orchestrator_dependencies()
    finally:
        if hasattr(runtime_main.app.state, "orchestrator_dependencies"):
            del runtime_main.app.state.orchestrator_dependencies

    assert first is sentinel
    assert second is sentinel
    assert constructions == [True]


def test_execute_accepts_only_whitelisted_context_and_delegates(monkeypatch):
    captured = {}
    calling_thread_id = threading.get_ident()
    original_to_thread = runtime_main.asyncio.to_thread

    async def recording_to_thread(function, *args):
        captured["delegated_function"] = function
        return await original_to_thread(function, *args)

    def fake_run(request, dependencies):
        captured["thread_id"] = threading.get_ident()
        captured["request"] = request
        captured["dependencies"] = dependencies
        return OrchestrationResult(
            session_id=request.session_context["session_id"],
            user_id=request.user_id,
            feature_scope=request.feature_scope,
            status="completed",
            plan=[],
            observations=[],
            final_response="safe result",
            trace_id="1" * 32,
        )

    dependencies = object()
    monkeypatch.setattr(runtime_main, "run_orchestration", fake_run)
    monkeypatch.setattr(runtime_main.asyncio, "to_thread", recording_to_thread)
    runtime_main.app.dependency_overrides[
        runtime_main.get_orchestrator_dependencies
    ] = lambda: dependencies
    try:
        response = client.post(
            "/v1/execute",
            json={
                "user_id": "  dashboard-user  ",
                "feature_scope": "rag",
                "payload_query": "  Find CircuitBreakerState.OPEN  ",
                "session_context": {
                    "session_id": "  browser-session  ",
                    "rag_top_k": 4,
                },
            },
        )
    finally:
        runtime_main.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["trace_id"] == "1" * 32
    assert captured["dependencies"] is dependencies
    assert captured["delegated_function"] is fake_run
    assert captured["thread_id"] != calling_thread_id
    assert captured["request"].user_id == "dashboard-user"
    assert captured["request"].payload_query == "Find CircuitBreakerState.OPEN"
    assert captured["request"].session_context == {
        "session_id": "browser-session",
        "rag_top_k": 4,
    }


def test_execute_rejects_untrusted_context_fields():
    response = client.post(
        "/v1/execute",
        json={
            "user_id": "dashboard-user",
            "feature_scope": "agent_orchestrator",
            "payload_query": "Read this file.",
            "session_context": {"document_path": "C:/private/customer.txt"},
        },
    )

    assert response.status_code == 422
    assert "document_path" in response.text


def test_execute_sanitizes_unexpected_boundary_failures(monkeypatch):
    def fail_execution(_request, _dependencies):
        raise RuntimeError("PRIVATE_PROVIDER_FAILURE")

    monkeypatch.setattr(runtime_main, "run_orchestration", fail_execution)
    runtime_main.app.dependency_overrides[
        runtime_main.get_orchestrator_dependencies
    ] = object
    try:
        response = client.post(
            "/v1/execute",
            json={
                "user_id": "dashboard-user",
                "feature_scope": "rag",
                "payload_query": "Find the resilience policy.",
            },
        )
    finally:
        runtime_main.app.dependency_overrides.clear()

    assert response.status_code == 502
    assert response.json() == {"detail": "CoreMesh execution failed."}
    assert "PRIVATE_PROVIDER_FAILURE" not in response.text


def test_trace_routes_list_and_read_redacted_artifacts(tmp_path):
    tracer = ForensicsTracer(trace_directory=tmp_path / "traces")
    try:
        with tracer.execution(
            "coremesh.agent.workflow",
            attributes={"coremesh.request.query": "PRIVATE_PROMPT"},
        ) as execution:
            with tracer.span("coremesh.tool.rag_search", SpanCategory.TOOL):
                pass
            execution.set_outcome(status="completed", final_confidence=0.95)

        runtime_main.app.dependency_overrides[
            runtime_main.get_runtime_forensics
        ] = lambda: tracer
        list_response = client.get("/v1/traces?limit=10")
        detail_response = client.get(f"/v1/traces/{execution.trace_id}")
    finally:
        runtime_main.app.dependency_overrides.clear()
        tracer.shutdown()

    assert list_response.status_code == 200
    assert list_response.json()["total"] == 1
    assert "artifact_path" not in list_response.text
    assert detail_response.status_code == 200
    assert detail_response.json()["trace_id"] == execution.trace_id
    assert "PRIVATE_PROMPT" not in detail_response.text


def test_trace_detail_rejects_invalid_id_and_reports_missing(tmp_path):
    tracer = ForensicsTracer(trace_directory=tmp_path / "traces")
    runtime_main.app.dependency_overrides[
        runtime_main.get_runtime_forensics
    ] = lambda: tracer
    try:
        invalid = client.get("/v1/traces/NOT-A-TRACE")
        missing = client.get(f"/v1/traces/{'0' * 32}")
    finally:
        runtime_main.app.dependency_overrides.clear()
        tracer.shutdown()

    assert invalid.status_code == 422
    assert missing.status_code == 404


def test_trace_detail_sanitizes_corrupt_artifact(tmp_path):
    trace_directory = tmp_path / "traces"
    trace_directory.mkdir()
    trace_id = "a" * 32
    (trace_directory / f"{trace_id}.json").write_bytes(b"\xff\xfePRIVATE")
    tracer = ForensicsTracer(trace_directory=trace_directory)
    runtime_main.app.dependency_overrides[
        runtime_main.get_runtime_forensics
    ] = lambda: tracer
    try:
        response = client.get(f"/v1/traces/{trace_id}")
    finally:
        runtime_main.app.dependency_overrides.clear()
        tracer.shutdown()

    assert response.status_code == 503
    assert response.json() == {
        "detail": "Forensic trace artifact is unavailable."
    }
    assert "PRIVATE" not in response.text
