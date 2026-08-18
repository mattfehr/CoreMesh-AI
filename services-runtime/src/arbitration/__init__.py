"""Public consensus-arbitration API.

System role:
    Re-exports critic/adjudicator contracts and the delivery verdict consumed by
    agent orchestration.
Dependencies:
    Importing loads Pydantic/httpx/Tenacity definitions and runtime settings;
    provider clients connect only when arbitration is invoked.
Side effects:
    Importing has no I/O. Default arbitration can call OpenAI, Anthropic, and
    Ollama and can block or replace an outbound response.
"""

from src.arbitration.consensus import (
    AdjudicationSchema,
    AdjudicatorClient,
    AnthropicCriticClient,
    ArbitrationPayload,
    BLOCKED_RESPONSE,
    ConsensusArbitrator,
    ConsensusStatus,
    ConsensusVerdict,
    CriticAssessmentSchema,
    CriticClient,
    CriticFailure,
    DeterministicAdjudicatorClient,
    DeterministicCriticClient,
    OllamaCriticClient,
    OpenAIAdjudicatorClient,
    OpenAICriticClient,
    configured_arbitrator,
)

__all__ = [
    "AdjudicationSchema",
    "AdjudicatorClient",
    "AnthropicCriticClient",
    "ArbitrationPayload",
    "BLOCKED_RESPONSE",
    "ConsensusArbitrator",
    "ConsensusStatus",
    "ConsensusVerdict",
    "CriticAssessmentSchema",
    "CriticClient",
    "CriticFailure",
    "DeterministicAdjudicatorClient",
    "DeterministicCriticClient",
    "OllamaCriticClient",
    "OpenAIAdjudicatorClient",
    "OpenAICriticClient",
    "configured_arbitrator",
]
