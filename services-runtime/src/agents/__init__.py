"""Public agent-orchestration API.

System role:
    Re-exports supervisor request/result contracts, dependency injection, and
    graph execution for trusted Python callers.
Dependencies:
    Importing this package loads the orchestrator, arbitration contracts, and
    runtime settings; provider/storage clients remain lazy.
Side effects:
    No network or persistence occurs until a graph is built and invoked.
"""

from src.agents.orchestrator import (
    ExecutionRequestPayload,
    OrchestrationResult,
    OrchestratorDependencies,
    PlanStep,
    ResponseArbitrator,
    SpecialistName,
    SupervisorState,
    ToolObservation,
    build_supervisor_graph,
    run_orchestration,
)

__all__ = [
    "ExecutionRequestPayload",
    "OrchestrationResult",
    "OrchestratorDependencies",
    "PlanStep",
    "ResponseArbitrator",
    "SpecialistName",
    "SupervisorState",
    "ToolObservation",
    "build_supervisor_graph",
    "run_orchestration",
]
