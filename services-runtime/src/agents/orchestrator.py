"""LangGraph supervisor workflow for CoreMesh Project 15.

System role:
    Coordinates library-only document, retrieval, and SQL specialists, records
    working/semantic memory, synthesizes their observations, and sends every
    final response through the consensus delivery gate.
Dependencies:
    Pydantic defines state contracts; LangGraph is optional; default tools use
    ingestion, RAG, SQL, Redis, Chroma, and multi-provider arbitration.
Side effects:
    Invoked workflows can read files, call databases/model providers, write
    Redis session events and Chroma summaries, and log degraded dependencies.

The supervisor decomposes a complex runtime request into ordered specialist
steps, executes document/RAG/SQL tool nodes, and records short-term and
long-term memory around the workflow.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field, is_dataclass, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, TypedDict

from pydantic import BaseModel, Field

from src.arbitration.consensus import (
    ArbitrationPayload,
    ConsensusArbitrator,
    ConsensusStatus,
    ConsensusVerdict,
    CriticFailure,
)
from src.config import settings

log = logging.getLogger(__name__)


class SpecialistName(str, Enum):
    """Stable graph node names for the three implemented specialist domains."""
    RAG_SEARCH = "rag_search"
    DOCUMENT_EXTRACTION = "document_extraction"
    SQL_GENERATION = "sql_generation"


class ExecutionRequestPayload(BaseModel):
    """[Project 15] Core Platform Unified Query Interface."""

    user_id: str
    feature_scope: str
    payload_query: str
    session_context: dict[str, Any] | None = None


class PlanStep(BaseModel):
    """One ordered specialist assignment in the supervisor plan."""
    step_id: str
    specialist: SpecialistName
    objective: str
    expected_output: str
    depends_on: list[str] = Field(default_factory=list)
    complexity: str = "moderate"
    status: str = "pending"


class ToolObservation(BaseModel):
    """Auditable input, output, status, error, and latency for one tool step."""
    observation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    step_id: str
    specialist: SpecialistName
    status: str
    input_payload: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    latency_ms: float = 0.0
    created_at_ms: float = Field(default_factory=lambda: time.time() * 1_000)


class SupervisorState(BaseModel):
    """Mutable graph state shared by supervisor and specialist nodes."""
    request: ExecutionRequestPayload
    session_id: str
    plan: list[PlanStep] = Field(default_factory=list)
    observations: list[ToolObservation] = Field(default_factory=list)
    retrieved_memories: list[dict[str, Any]] = Field(default_factory=list)
    current_step_index: int = 0
    dispatch_next: SpecialistName | None = None
    final_response: str | None = None
    status: str = "planning"


class OrchestrationResult(BaseModel):
    """Public completed workflow including evidence and arbitration verdict."""
    session_id: str
    user_id: str
    feature_scope: str
    status: str
    plan: list[PlanStep]
    observations: list[ToolObservation]
    retrieved_memories: list[dict[str, Any]] = Field(default_factory=list)
    final_response: str
    arbitration: ConsensusVerdict | None = None


class _GraphState(TypedDict, total=False):
    request: dict[str, Any]
    session_id: str
    plan: list[dict[str, Any]]
    observations: list[dict[str, Any]]
    retrieved_memories: list[dict[str, Any]]
    current_step_index: int
    dispatch_next: str | None
    final_response: str | None
    status: str


class SpecialistTool(Protocol):
    """Structural interface implemented by every specialist adapter."""
    def run(
        self,
        request: ExecutionRequestPayload,
        step: PlanStep,
        observations: Sequence[ToolObservation],
    ) -> Any:
        ...


class ShortTermMemory(Protocol):
    """Working-state/event sink scoped to one execution session."""
    def save_state(self, session_id: str, state: Mapping[str, Any]) -> None:
        ...

    def append_event(self, session_id: str, event: Mapping[str, Any]) -> None:
        ...


class SemanticMemory(Protocol):
    """Long-term similarity lookup and completed-interaction sink."""
    def retrieve_similar(
        self,
        user_id: str,
        query: str,
        *,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        ...

    def store_interaction(self, result: OrchestrationResult) -> None:
        ...


class ResponseArbitrator(Protocol):
    """Delivery-gate interface used after workflow synthesis."""
    def arbitrate(self, payload: ArbitrationPayload) -> Any:
        ...


class HybridRAGSearchTool:
    """Specialist adapter around the existing dense/sparse RAG retriever."""

    def __init__(self, retriever: Any | None = None, top_k: int = 5) -> None:
        self.retriever = retriever
        self.top_k = top_k

    def _retriever(self) -> Any:
        if self.retriever is None:
            from src.rag.retrieval import HybridRetriever  # noqa: PLC0415

            self.retriever = HybridRetriever()
        return self.retriever

    def run(
        self,
        request: ExecutionRequestPayload,
        step: PlanStep,
        observations: Sequence[ToolObservation],
    ) -> dict[str, Any]:
        context = request.session_context or {}
        top_k = int(context.get("rag_top_k", self.top_k))
        results = self._retriever().search(request.payload_query, top_k=top_k)
        return {
            "query": request.payload_query,
            "results": [_json_ready(result) for result in results],
            "prior_observation_count": len(observations),
        }


class DocumentExtractionTool:
    """Specialist adapter around the Project 14 document extraction pipeline."""

    def run(
        self,
        request: ExecutionRequestPayload,
        step: PlanStep,
        observations: Sequence[ToolObservation],
    ) -> dict[str, Any]:
        context = request.session_context or {}

        document_text = context.get("document_text")
        if isinstance(document_text, str) and document_text.strip():
            return self._extract_from_text(document_text)

        file_bytes, filename = self._resolve_document_bytes(context)
        if file_bytes is None:
            return {
                "status": "skipped",
                "reason": "No document_text, document_bytes, document_base64, or document_path found.",
                "prior_observation_count": len(observations),
            }

        from src.ingestion.processor import process_document  # noqa: PLC0415

        response = process_document(file_bytes, filename)
        return _json_ready(response)

    def _extract_from_text(self, document_text: str) -> dict[str, Any]:
        from src.ingestion.extraction import extract_structured  # noqa: PLC0415
        from src.ingestion.validation import validate_invoice_totals  # noqa: PLC0415

        extraction, llm_used = extract_structured(document_text)
        validation = validate_invoice_totals(extraction)
        return {
            "extraction": _json_ready(extraction),
            "llm_extraction_used": llm_used,
            "validation": _json_ready(validation),
            "source": "session_context.document_text",
        }

    def _resolve_document_bytes(self, context: Mapping[str, Any]) -> tuple[bytes | None, str]:
        filename = str(context.get("document_filename") or "agent-upload")

        raw_bytes = context.get("document_bytes")
        if isinstance(raw_bytes, bytes | bytearray):
            return bytes(raw_bytes), filename
        if isinstance(raw_bytes, str) and raw_bytes:
            try:
                return base64.b64decode(raw_bytes), filename
            except Exception:
                return raw_bytes.encode("utf-8"), filename

        raw_base64 = context.get("document_base64")
        if isinstance(raw_base64, str) and raw_base64:
            return base64.b64decode(raw_base64), filename

        document_path = context.get("document_path")
        if isinstance(document_path, str) and document_path:
            path = Path(document_path)
            return path.read_bytes(), str(context.get("document_filename") or path.name)

        return None, filename


class SQLQueryGenerator(Protocol):
    """Strategy interface that turns an agent step and schema into SQL."""
    def generate_sql(
        self,
        request: ExecutionRequestPayload,
        step: PlanStep,
        observations: Sequence[ToolObservation],
        schema: Any,
    ) -> str:
        ...


class HeuristicSQLQueryGenerator:
    """Small deterministic SQL generator used until an LLM-backed generator is wired."""

    def generate_sql(
        self,
        request: ExecutionRequestPayload,
        step: PlanStep,
        observations: Sequence[ToolObservation],
        schema: Any,
    ) -> str:
        context = request.session_context or {}
        explicit_sql = context.get("sql_query")
        if isinstance(explicit_sql, str) and explicit_sql.strip():
            return explicit_sql.strip()

        tables = list(getattr(schema, "tables", []) or [])
        if not tables:
            return "SELECT 1 AS result"

        table_name = tables[0].name
        query_lower = request.payload_query.lower()
        if any(word in query_lower for word in ("count", "how many", "volume")):
            return f"SELECT COUNT(*) AS row_count FROM {table_name}"
        return f"SELECT * FROM {table_name}"


class SQLGenerationTool:
    """Specialist adapter for SQL generation plus read-only sandbox execution."""

    def __init__(
        self,
        sandbox: Any | None = None,
        generator: SQLQueryGenerator | None = None,
    ) -> None:
        self.sandbox = sandbox
        self.generator = generator or HeuristicSQLQueryGenerator()

    def _sandbox(self) -> Any:
        if self.sandbox is None:
            from src.sql_engine.sandbox import SQLSandbox  # noqa: PLC0415

            self.sandbox = SQLSandbox()
        return self.sandbox

    def run(
        self,
        request: ExecutionRequestPayload,
        step: PlanStep,
        observations: Sequence[ToolObservation],
    ) -> dict[str, Any]:
        sandbox = self._sandbox()
        schema = sandbox.introspect_schema()
        sql = self.generator.generate_sql(request, step, observations, schema)
        result = sandbox.execute(sql)
        return {
            "sql": result.sql,
            "columns": list(result.columns),
            "rows": _json_ready(result.rows),
            "row_count": result.row_count,
            "elapsed_ms": result.elapsed_ms,
            "limit_applied": result.limit_applied,
            "schema_tables": [table.name for table in getattr(schema, "tables", [])],
            "prior_observations": [_json_ready(observation) for observation in observations],
        }


class RedisShortTermMemory:
    """Redis-backed working memory scoped to a single agent execution session."""

    def __init__(
        self,
        redis_url: str | None = None,
        ttl_seconds: int | None = None,
    ) -> None:
        self.redis_url = redis_url or settings.redis_url
        self.ttl_seconds = ttl_seconds or settings.agent_memory_ttl_seconds
        self._client: Any | None = None

    @property
    def client(self) -> Any:
        if self._client is None:
            import redis  # noqa: PLC0415

            self._client = redis.Redis.from_url(
                self.redis_url,
                decode_responses=True,
            )
        return self._client

    def save_state(self, session_id: str, state: Mapping[str, Any]) -> None:
        payload = json.dumps(_json_ready(state), sort_keys=True)
        key = self._state_key(session_id)
        if self.ttl_seconds > 0:
            self.client.setex(key, self.ttl_seconds, payload)
        else:
            self.client.set(key, payload)

    def append_event(self, session_id: str, event: Mapping[str, Any]) -> None:
        payload = json.dumps(_json_ready(event), sort_keys=True)
        key = self._event_key(session_id)
        self.client.rpush(key, payload)
        if self.ttl_seconds > 0:
            self.client.expire(key, self.ttl_seconds)

    def load_state(self, session_id: str) -> dict[str, Any] | None:
        payload = self.client.get(self._state_key(session_id))
        if payload is None:
            return None
        return json.loads(payload)

    @staticmethod
    def _state_key(session_id: str) -> str:
        return f"coremesh:agents:sessions:{session_id}"

    @staticmethod
    def _event_key(session_id: str) -> str:
        return f"coremesh:agents:sessions:{session_id}:events"


class ChromaSemanticMemory:
    """ChromaDB-backed long-term memory for completed interaction summaries."""

    def __init__(
        self,
        persist_directory: str | None = None,
        collection_name: str | None = None,
    ) -> None:
        self.persist_directory = persist_directory or settings.chroma_persist_directory
        self.collection_name = collection_name or settings.chroma_collection
        self._collection: Any | None = None

    @property
    def collection(self) -> Any:
        if self._collection is None:
            import chromadb  # noqa: PLC0415

            client = chromadb.PersistentClient(path=self.persist_directory)
            self._collection = client.get_or_create_collection(
                name=self.collection_name,
                embedding_function=_HashEmbeddingFunction(),
            )
        return self._collection

    def retrieve_similar(
        self,
        user_id: str,
        query: str,
        *,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        response = self.collection.query(
            query_texts=[query],
            n_results=limit,
            where={"user_id": user_id},
        )
        documents = (response.get("documents") or [[]])[0]
        metadatas = (response.get("metadatas") or [[]])[0]
        distances = (response.get("distances") or [[]])[0]
        return [
            {
                "text": document,
                "metadata": metadata or {},
                "distance": distances[index] if index < len(distances) else None,
            }
            for index, (document, metadata) in enumerate(zip(documents, metadatas))
        ]

    def store_interaction(self, result: OrchestrationResult) -> None:
        summary = _semantic_summary(result)
        metadata = {
            "session_id": result.session_id,
            "user_id": result.user_id,
            "feature_scope": result.feature_scope,
            "status": result.status,
            "tool_sequence": ",".join(step.specialist.value for step in result.plan),
            "created_at_ms": int(time.time() * 1_000),
        }
        memory_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"coremesh:agents:{result.session_id}"))
        self.collection.upsert(
            ids=[memory_id],
            documents=[summary],
            metadatas=[metadata],
        )


class InMemoryShortTermMemory:
    """Test-friendly memory backend."""

    def __init__(self) -> None:
        self.states: list[tuple[str, dict[str, Any]]] = []
        self.events: list[tuple[str, dict[str, Any]]] = []

    def save_state(self, session_id: str, state: Mapping[str, Any]) -> None:
        self.states.append((session_id, _json_ready(state)))

    def append_event(self, session_id: str, event: Mapping[str, Any]) -> None:
        self.events.append((session_id, _json_ready(event)))


class InMemorySemanticMemory:
    """Test-friendly semantic memory backend."""

    def __init__(self, memories: Sequence[Mapping[str, Any]] | None = None) -> None:
        self.memories = [_json_ready(memory) for memory in memories or []]
        self.stored_results: list[OrchestrationResult] = []

    def retrieve_similar(
        self,
        user_id: str,
        query: str,
        *,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        return list(self.memories[:limit])

    def store_interaction(self, result: OrchestrationResult) -> None:
        self.stored_results.append(result)


@dataclass
class OrchestratorDependencies:
    """Replaceable tools, memory stores, and arbitrator used by a graph."""
    rag_tool: SpecialistTool = field(default_factory=HybridRAGSearchTool)
    document_tool: SpecialistTool = field(default_factory=DocumentExtractionTool)
    sql_tool: SpecialistTool = field(default_factory=SQLGenerationTool)
    short_term_memory: ShortTermMemory = field(default_factory=RedisShortTermMemory)
    semantic_memory: SemanticMemory = field(default_factory=ChromaSemanticMemory)
    arbitrator: ResponseArbitrator = field(default_factory=ConsensusArbitrator)


def build_supervisor_graph(dependencies: OrchestratorDependencies | None = None) -> Any:
    """Build a LangGraph network or the contract-compatible sequential fallback."""

    deps = dependencies or OrchestratorDependencies()
    nodes = _build_nodes(deps)

    try:
        from langgraph.graph import END, StateGraph  # noqa: PLC0415
    except ImportError:
        log.warning("langgraph is not installed; using sequential supervisor fallback")
        return _SequentialSupervisorGraph(nodes)

    graph = StateGraph(_GraphState)
    graph.add_node("supervisor", nodes["supervisor"])
    graph.add_node(SpecialistName.RAG_SEARCH.value, nodes[SpecialistName.RAG_SEARCH.value])
    graph.add_node(
        SpecialistName.DOCUMENT_EXTRACTION.value,
        nodes[SpecialistName.DOCUMENT_EXTRACTION.value],
    )
    graph.add_node(SpecialistName.SQL_GENERATION.value, nodes[SpecialistName.SQL_GENERATION.value])

    graph.set_entry_point("supervisor")
    graph.add_conditional_edges(
        "supervisor",
        _route_next_node,
        {
            SpecialistName.RAG_SEARCH.value: SpecialistName.RAG_SEARCH.value,
            SpecialistName.DOCUMENT_EXTRACTION.value: SpecialistName.DOCUMENT_EXTRACTION.value,
            SpecialistName.SQL_GENERATION.value: SpecialistName.SQL_GENERATION.value,
            "END": END,
        },
    )
    graph.add_edge(SpecialistName.RAG_SEARCH.value, "supervisor")
    graph.add_edge(SpecialistName.DOCUMENT_EXTRACTION.value, "supervisor")
    graph.add_edge(SpecialistName.SQL_GENERATION.value, "supervisor")
    return graph.compile()


def run_orchestration(
    request: ExecutionRequestPayload | Mapping[str, Any],
    dependencies: OrchestratorDependencies | None = None,
) -> OrchestrationResult:
    """Execute specialists, arbitrate output, persist memory, and return evidence.

    Storage failures are logged and degraded so work can continue. Arbitration
    failures are fail-closed and replace the deliverable with a blocked verdict.
    """

    deps = dependencies or OrchestratorDependencies()
    payload = ExecutionRequestPayload.model_validate(request)
    normalized_context = _normalize_session_context(payload.session_context)
    if normalized_context is not payload.session_context:
        payload = payload.model_copy(update={"session_context": normalized_context})
    session_id = _session_id(payload)
    initial_state = SupervisorState(request=payload, session_id=session_id)
    graph = build_supervisor_graph(deps)
    final_state = graph.invoke(_state_to_graph_dict(initial_state))
    state = SupervisorState.model_validate(final_state)
    result = OrchestrationResult(
        session_id=state.session_id,
        user_id=state.request.user_id,
        feature_scope=state.request.feature_scope,
        status=state.status,
        plan=state.plan,
        observations=state.observations,
        retrieved_memories=state.retrieved_memories,
        final_response=state.final_response or "",
    )
    result = _apply_arbitration(deps, result, payload)
    _store_long_term(deps, result)
    return result


def _build_nodes(deps: OrchestratorDependencies) -> dict[str, Any]:
    def supervisor_node(graph_state: Mapping[str, Any]) -> dict[str, Any]:
        state = SupervisorState.model_validate(graph_state)

        if not state.plan:
            state.retrieved_memories = _retrieve_memories(deps, state.request)
            state.plan = _create_plan(state.request, state.retrieved_memories)
            state.status = "running"

        if state.current_step_index >= len(state.plan):
            state.dispatch_next = None
            state.final_response = _synthesize_response(state)
            state.status = _completion_status(state.observations)
            _persist_short_term(deps, state, event_type="workflow_completed")
            return _state_to_graph_dict(state)

        current = state.plan[state.current_step_index]
        if current.status == "pending":
            state.plan[state.current_step_index] = current.model_copy(update={"status": "running"})
        state.dispatch_next = state.plan[state.current_step_index].specialist
        _persist_short_term(deps, state, event_type="workflow_dispatched")
        return _state_to_graph_dict(state)

    def specialist_node(specialist: SpecialistName, tool: SpecialistTool) -> Any:
        def node(graph_state: Mapping[str, Any]) -> dict[str, Any]:
            state = SupervisorState.model_validate(graph_state)
            step = state.plan[state.current_step_index]
            started = time.perf_counter()
            input_payload = {
                "query": state.request.payload_query,
                "step": _json_ready(step),
                "prior_observation_count": len(state.observations),
            }

            try:
                output = tool.run(state.request, step, state.observations)
                status = "success"
                error = None
            except Exception as exc:  # pragma: no cover - exercised by integration failures
                log.exception("agent specialist failed", extra={"specialist": specialist.value})
                output = {}
                status = "error"
                error = str(exc)

            observation = ToolObservation(
                step_id=step.step_id,
                specialist=specialist,
                status=status,
                input_payload=input_payload,
                output=_normalize_tool_output(output),
                error=error,
                latency_ms=round((time.perf_counter() - started) * 1_000, 2),
            )
            state.observations.append(observation)
            state.plan[state.current_step_index] = step.model_copy(
                update={"status": "completed" if status == "success" else "failed"}
            )
            state.current_step_index += 1
            state.dispatch_next = None
            _persist_short_term(deps, state, event_type=f"{specialist.value}_completed")
            return _state_to_graph_dict(state)

        return node

    return {
        "supervisor": supervisor_node,
        SpecialistName.RAG_SEARCH.value: specialist_node(
            SpecialistName.RAG_SEARCH,
            deps.rag_tool,
        ),
        SpecialistName.DOCUMENT_EXTRACTION.value: specialist_node(
            SpecialistName.DOCUMENT_EXTRACTION,
            deps.document_tool,
        ),
        SpecialistName.SQL_GENERATION.value: specialist_node(
            SpecialistName.SQL_GENERATION,
            deps.sql_tool,
        ),
    }


class _SequentialSupervisorGraph:
    def __init__(self, nodes: Mapping[str, Any]) -> None:
        self.nodes = nodes

    def invoke(self, state: Mapping[str, Any]) -> dict[str, Any]:
        current = dict(state)
        while True:
            current = self.nodes["supervisor"](current)
            next_node = _route_next_node(current)
            if next_node == "END":
                return current
            current = self.nodes[next_node](current)


def _route_next_node(state: Mapping[str, Any]) -> str:
    next_node = state.get("dispatch_next")
    if not next_node:
        return "END"
    if isinstance(next_node, SpecialistName):
        return next_node.value
    return str(next_node)


def _create_plan(
    request: ExecutionRequestPayload,
    retrieved_memories: Sequence[Mapping[str, Any]],
) -> list[PlanStep]:
    query = request.payload_query.lower()
    context = request.session_context or {}
    steps: list[PlanStep] = []

    needs_rag = any(
        token in query
        for token in ("lookup", "search", "find", "policy", "document", "reference", "knowledge")
    )
    needs_document = bool(
        context.get("document_text")
        or context.get("document_bytes")
        or context.get("document_base64")
        or context.get("document_path")
        or any(token in query for token in ("invoice", "extract", "document", "receipt"))
    )
    needs_sql = bool(
        context.get("sql_query")
        or any(
            token in query
            for token in ("database", "db", "sql", "analysis", "analyze", "revenue", "orders", "count")
        )
    )

    if needs_rag:
        steps.append(
            PlanStep(
                step_id="step-rag-search",
                specialist=SpecialistName.RAG_SEARCH,
                objective="Retrieve relevant document or knowledge-base context for the user request.",
                expected_output="Ranked references with source markers and supporting snippets.",
                complexity="moderate",
            )
        )

    if needs_document:
        steps.append(
            PlanStep(
                step_id="step-document-extraction",
                specialist=SpecialistName.DOCUMENT_EXTRACTION,
                objective="Extract structured data from the provided document context.",
                expected_output="Typed document extraction fields with validation metadata.",
                depends_on=[step.step_id for step in steps],
                complexity="moderate",
            )
        )

    if needs_sql:
        steps.append(
            PlanStep(
                step_id="step-sql-generation",
                specialist=SpecialistName.SQL_GENERATION,
                objective="Generate and execute a read-only SQL analysis using prior specialist findings.",
                expected_output="Sanitized SQL, result rows, and execution metadata.",
                depends_on=[step.step_id for step in steps],
                complexity="hard",
            )
        )

    if not steps:
        steps.append(
            PlanStep(
                step_id="step-rag-search",
                specialist=SpecialistName.RAG_SEARCH,
                objective="Retrieve relevant context for the user request.",
                expected_output="Ranked references with source markers.",
                complexity="simple",
            )
        )

    if retrieved_memories:
        steps[0] = steps[0].model_copy(
            update={
                "objective": (
                    f"{steps[0].objective} Consider {len(retrieved_memories)} "
                    "similar long-term memory record(s)."
                )
            }
        )

    return steps


def _retrieve_memories(
    deps: OrchestratorDependencies,
    request: ExecutionRequestPayload,
) -> list[dict[str, Any]]:
    try:
        return deps.semantic_memory.retrieve_similar(
            request.user_id,
            request.payload_query,
            limit=3,
        )
    except Exception as exc:  # pragma: no cover - protects production path
        log.warning("semantic memory retrieval failed: %s", exc)
        return []


def _persist_short_term(
    deps: OrchestratorDependencies,
    state: SupervisorState,
    *,
    event_type: str,
) -> None:
    snapshot = _state_to_graph_dict(state)
    event = {
        "event_type": event_type,
        "status": state.status,
        "current_step_index": state.current_step_index,
        "dispatch_next": state.dispatch_next.value if state.dispatch_next else None,
        "observation_count": len(state.observations),
        "created_at_ms": int(time.time() * 1_000),
    }
    try:
        deps.short_term_memory.save_state(state.session_id, snapshot)
        deps.short_term_memory.append_event(state.session_id, event)
    except Exception as exc:  # pragma: no cover - protects production path
        log.warning("short-term memory persistence failed: %s", exc)


def _apply_arbitration(
    deps: OrchestratorDependencies,
    result: OrchestrationResult,
    request: ExecutionRequestPayload,
) -> OrchestrationResult:
    if not (result.final_response or "").strip():
        empty_payload = ArbitrationPayload(
            output_text="[empty agent response]",
            original_prompt=request.payload_query,
            user_id=result.user_id,
            feature_scope=result.feature_scope,
            session_id=result.session_id,
            metadata={
                "workflow_status": result.status,
                "observation_count": len(result.observations),
            },
        )
        verdict = ConsensusVerdict.blocked(
            empty_payload,
            triggered_by=["empty_final_response"],
        )
        return result.model_copy(
            update={
                "arbitration": verdict,
                "final_response": verdict.delivered_output,
                "status": "blocked_by_arbitration",
            }
        )

    payload = ArbitrationPayload(
        output_text=result.final_response,
        original_prompt=request.payload_query,
        user_id=result.user_id,
        feature_scope=result.feature_scope,
        session_id=result.session_id,
        metadata={
            "workflow_status": result.status,
            "observation_count": len(result.observations),
        },
    )

    try:
        verdict = _invoke_arbitrator(deps.arbitrator, payload)
    except Exception as exc:  # pragma: no cover - protects production path
        log.warning("response arbitration failed closed: %s", exc)
        verdict = ConsensusVerdict.blocked(
            payload,
            failures=[
                CriticFailure(
                    evaluation_dimension="arbitration",
                    provider_name="runtime",
                    error=str(exc),
                )
            ],
            triggered_by=["arbitrator_runtime_failure"],
        )

    updates: dict[str, Any] = {
        "arbitration": verdict,
        "final_response": verdict.delivered_output,
    }
    if verdict.status == ConsensusStatus.REMEDIATED:
        updates["status"] = "remediated_by_arbitration"
    elif not verdict.delivery_allowed:
        updates["status"] = "blocked_by_arbitration"

    return result.model_copy(update=updates)


def _invoke_arbitrator(
    arbitrator: ResponseArbitrator,
    payload: ArbitrationPayload,
) -> ConsensusVerdict:
    maybe_verdict = arbitrator.arbitrate(payload)
    if inspect.isawaitable(maybe_verdict):
        maybe_verdict = _await_sync(maybe_verdict)
    return ConsensusVerdict.model_validate(maybe_verdict)


def _await_sync(awaitable: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)

    box: dict[str, Any] = {}

    def runner() -> None:
        try:
            box["value"] = asyncio.run(awaitable)
        except BaseException as exc:  # pragma: no cover - defensive bridge
            box["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _store_long_term(deps: OrchestratorDependencies, result: OrchestrationResult) -> None:
    try:
        deps.semantic_memory.store_interaction(result)
    except Exception as exc:  # pragma: no cover - protects production path
        log.warning("long-term semantic memory persistence failed: %s", exc)


def _synthesize_response(state: SupervisorState) -> str:
    lines = [
        f"Completed {len(state.observations)} specialist step(s) for: {state.request.payload_query}",
    ]

    for observation in state.observations:
        if observation.status != "success":
            lines.append(f"{observation.specialist.value}: failed ({observation.error}).")
            continue

        if observation.specialist == SpecialistName.RAG_SEARCH:
            results = observation.output.get("results") or []
            references = [
                str(result.get("reference_marker") or result.get("source") or result.get("chunk_id"))
                for result in results[:3]
                if isinstance(result, Mapping)
            ]
            if references:
                lines.append(f"RAG search found references: {', '.join(references)}.")
            else:
                lines.append("RAG search completed with no matching references.")

        elif observation.specialist == SpecialistName.DOCUMENT_EXTRACTION:
            extraction = observation.output.get("extraction") or {}
            vendor = extraction.get("vendor_name")
            invoice_id = extraction.get("invoice_id")
            invoice_total = extraction.get("invoice_total")
            if vendor or invoice_id:
                lines.append(
                    "Document extraction found "
                    f"vendor={vendor or 'UNKNOWN'}, invoice_id={invoice_id or 'UNKNOWN'}, "
                    f"invoice_total={invoice_total}."
                )
            else:
                lines.append("Document extraction completed without structured invoice fields.")

        elif observation.specialist == SpecialistName.SQL_GENERATION:
            row_count = observation.output.get("row_count", 0)
            sql = observation.output.get("sql")
            rows = observation.output.get("rows") or []
            lines.append(f"SQL analysis executed `{sql}` and returned {row_count} row(s): {rows}.")

    return " ".join(lines)


def _completion_status(observations: Sequence[ToolObservation]) -> str:
    if any(observation.status == "error" for observation in observations):
        return "completed_with_errors"
    return "completed"


def _session_id(request: ExecutionRequestPayload) -> str:
    context = request.session_context or {}
    explicit = context.get("session_id")
    if isinstance(explicit, str) and explicit.strip():
        return explicit
    seed = f"{request.user_id}:{request.feature_scope}:{request.payload_query}:{time.time_ns()}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


def _normalize_session_context(
    context: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Encode binary document payloads so graph state remains JSON-serializable."""

    if not context:
        return context

    raw_bytes = context.get("document_bytes")
    if not isinstance(raw_bytes, bytes | bytearray):
        return context

    normalized = dict(context)
    normalized.pop("document_bytes", None)
    if not normalized.get("document_base64"):
        normalized["document_base64"] = base64.b64encode(bytes(raw_bytes)).decode("ascii")
    return normalized


def _state_to_graph_dict(state: SupervisorState) -> dict[str, Any]:
    return _json_ready(state.model_dump())


def _normalize_tool_output(output: Any) -> dict[str, Any]:
    normalized = _json_ready(output)
    if isinstance(normalized, dict):
        return normalized
    return {"result": normalized}


def _json_ready(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _json_ready(value.model_dump())
    if is_dataclass(value):
        return _json_ready(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, tuple | list | set):
        return [_json_ready(item) for item in value]
    if isinstance(value, bytes | bytearray):
        return f"<{len(value)} bytes>"
    if isinstance(value, Path):
        return str(value)
    return value


def _semantic_summary(result: OrchestrationResult) -> str:
    tool_sequence = " -> ".join(step.specialist.value for step in result.plan)
    return (
        f"User {result.user_id} asked: {result.final_response}. "
        f"Feature scope: {result.feature_scope}. "
        f"Workflow status: {result.status}. "
        f"Tool sequence: {tool_sequence}."
    )


def _hash_embedding(text: str, dimensions: int = 64) -> list[float]:
    vector = [0.0] * dimensions
    tokens = str(text).lower().split()
    for token in tokens or [""]:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[bucket] += sign
    magnitude = sum(component * component for component in vector) ** 0.5 or 1.0
    return [component / magnitude for component in vector]


try:
    from chromadb.api.types import Documents, EmbeddingFunction, Embeddings
    from chromadb.utils.embedding_functions import register_embedding_function

    @register_embedding_function
    class _HashEmbeddingFunction(EmbeddingFunction[Documents]):
        """Local deterministic embedding function to keep Chroma memory self-contained."""

        def __init__(self, dimensions: int = 64) -> None:
            self._dimensions = dimensions

        def __call__(self, input: Documents) -> Embeddings:  # noqa: A002
            return [_hash_embedding(document, self._dimensions) for document in input]

        @staticmethod
        def name() -> str:
            return "coremesh_hash_embedding"

        def get_config(self) -> dict[str, Any]:
            return {"dimensions": self._dimensions}

        @staticmethod
        def build_from_config(config: dict[str, Any]) -> "_HashEmbeddingFunction":
            return _HashEmbeddingFunction(dimensions=int(config.get("dimensions", 64)))

except ImportError:

    class _HashEmbeddingFunction:
        """Fallback when chromadb is unavailable (tests / minimal installs)."""

        def __init__(self, dimensions: int = 64) -> None:
            self._dimensions = dimensions

        def __call__(self, input: Sequence[str]) -> list[list[float]]:  # noqa: A002
            return [_hash_embedding(document, self._dimensions) for document in input]

        @staticmethod
        def name() -> str:
            return "coremesh_hash_embedding"

        def get_config(self) -> dict[str, Any]:
            return {"dimensions": self._dimensions}

        @staticmethod
        def build_from_config(config: dict[str, Any]) -> "_HashEmbeddingFunction":
            return _HashEmbeddingFunction(dimensions=int(config.get("dimensions", 64)))


__all__ = [
    "ChromaSemanticMemory",
    "DocumentExtractionTool",
    "ExecutionRequestPayload",
    "HeuristicSQLQueryGenerator",
    "HybridRAGSearchTool",
    "InMemorySemanticMemory",
    "InMemoryShortTermMemory",
    "OrchestrationResult",
    "OrchestratorDependencies",
    "PlanStep",
    "RedisShortTermMemory",
    "ResponseArbitrator",
    "SQLGenerationTool",
    "SQLQueryGenerator",
    "SpecialistName",
    "SupervisorState",
    "ToolObservation",
    "build_supervisor_graph",
    "run_orchestration",
]
