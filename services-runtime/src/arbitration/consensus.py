"""Parallel consensus arbitration for outgoing agent responses."""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from enum import Enum
from typing import Any, Callable, Literal, Mapping, Protocol, Sequence, TypeVar

import httpx
from pydantic import BaseModel, Field, field_validator
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

from src.config import settings


EvaluationDimension = Literal["factual", "logic", "completeness"]
AdjudicationAction = Literal["release_original", "remediate", "block", "manual_review"]

BLOCKED_RESPONSE = (
    "I could not safely deliver that response because the arbitration layer "
    "flagged it for review."
)


class ConsensusStatus(str, Enum):
    PASSED = "passed"
    PASSED_DEGRADED = "passed_degraded"
    REMEDIATED = "remediated"
    BLOCKED = "blocked"
    MANUAL_REVIEW = "manual_review"


class CriticAssessmentSchema(BaseModel):
    """[Project 5] Arbitration Evaluation Assessment Contract."""

    evaluation_dimension: EvaluationDimension = Field(
        description="Dimension assessed: 'factual', 'logic', 'completeness'"
    )
    assigned_score: int = Field(
        ge=1,
        le=5,
        description="Integer rating bounded between 1 (poor) to 5 (excellent)",
    )
    flagged_anomalies: list[str] = Field(
        default_factory=list,
        description="List of raw snippets indicating hallucinations or structural errors.",
    )
    confidence_coefficient: float = Field(
        ge=0.0,
        le=1.0,
        description="Model confidence measurement scaled between 0.0 and 1.0",
    )

    @field_validator("flagged_anomalies", mode="before")
    @classmethod
    def _normalize_anomalies(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        return value


class ArbitrationPayload(BaseModel):
    """The outbound agent payload being evaluated."""

    output_text: str = Field(min_length=1)
    original_prompt: str | None = None
    user_id: str | None = None
    feature_scope: str | None = None
    session_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CriticFailure(BaseModel):
    """A failed critic call that should be preserved in the verdict."""

    evaluation_dimension: str
    provider_name: str
    error: str


class AdjudicationSchema(BaseModel):
    """Structured adjudicator decision after a critic conflict or low score."""

    action: AdjudicationAction
    overall_quality_score: int = Field(ge=1, le=10)
    confidence_coefficient: float = Field(ge=0.0, le=1.0)
    confirmed_issues: list[str] = Field(default_factory=list)
    dismissed_flags: list[str] = Field(default_factory=list)
    remediated_output: str | None = None
    rationale: str = ""


class ConsensusVerdict(BaseModel):
    """Final delivery decision produced by the arbitration gate."""

    arbitration_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    payload_id: str
    status: ConsensusStatus
    delivery_allowed: bool
    delivered_output: str
    adjudication_required: bool
    critic_assessments: list[CriticAssessmentSchema] = Field(default_factory=list)
    critic_failures: list[CriticFailure] = Field(default_factory=list)
    triggered_by: list[str] = Field(default_factory=list)
    adjudication: AdjudicationSchema | None = None
    confidence_coefficient: float = Field(ge=0.0, le=1.0)
    created_at_ms: float = Field(default_factory=lambda: time.time() * 1_000)

    @classmethod
    def pass_verdict(
        cls,
        payload: ArbitrationPayload,
        assessments: Sequence[CriticAssessmentSchema],
        failures: Sequence[CriticFailure] | None = None,
    ) -> "ConsensusVerdict":
        return cls(
            payload_id=_payload_id(payload),
            status=ConsensusStatus.PASSED_DEGRADED if failures else ConsensusStatus.PASSED,
            delivery_allowed=True,
            delivered_output=payload.output_text,
            adjudication_required=False,
            critic_assessments=list(assessments),
            critic_failures=list(failures or []),
            triggered_by=["critic_failure_degraded"] if failures else [],
            confidence_coefficient=_combined_confidence(assessments, failures or []),
        )

    @classmethod
    def blocked(
        cls,
        payload: ArbitrationPayload,
        *,
        status: ConsensusStatus = ConsensusStatus.MANUAL_REVIEW,
        assessments: Sequence[CriticAssessmentSchema] | None = None,
        failures: Sequence[CriticFailure] | None = None,
        triggered_by: Sequence[str] | None = None,
        adjudication: AdjudicationSchema | None = None,
    ) -> "ConsensusVerdict":
        return cls(
            payload_id=_payload_id(payload),
            status=status,
            delivery_allowed=False,
            delivered_output=BLOCKED_RESPONSE,
            adjudication_required=True,
            critic_assessments=list(assessments or []),
            critic_failures=list(failures or []),
            triggered_by=list(triggered_by or []),
            adjudication=adjudication,
            confidence_coefficient=_combined_confidence(assessments or [], failures or []),
        )


class CriticClient(Protocol):
    dimension: EvaluationDimension
    provider_name: str

    async def assess(self, payload: ArbitrationPayload) -> CriticAssessmentSchema:
        ...


class AdjudicatorClient(Protocol):
    provider_name: str

    async def adjudicate(
        self,
        payload: ArbitrationPayload,
        assessments: Sequence[CriticAssessmentSchema],
        failures: Sequence[CriticFailure],
        triggered_by: Sequence[str],
    ) -> AdjudicationSchema:
        ...


class ConsensusArbitrator:
    """Fan out an outbound payload to independent critics and decide delivery."""

    def __init__(
        self,
        critics: Sequence[CriticClient] | None = None,
        adjudicator: AdjudicatorClient | None = None,
        *,
        score_threshold: int | None = None,
        retry_attempts: int | None = None,
    ) -> None:
        self.critics = list(critics or _default_critics())
        self.adjudicator = adjudicator or OpenAIAdjudicatorClient()
        self.score_threshold = score_threshold or settings.arbitration_score_threshold
        self.retry_attempts = retry_attempts or settings.arbitration_retry_attempts

    async def arbitrate(
        self,
        payload: ArbitrationPayload | Mapping[str, Any],
    ) -> ConsensusVerdict:
        payload = ArbitrationPayload.model_validate(payload)
        timeout_seconds = settings.arbitration_timeout_seconds
        try:
            results = await asyncio.wait_for(
                asyncio.gather(
                    *(self._run_critic(critic, payload) for critic in self.critics),
                ),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            return ConsensusVerdict.blocked(
                payload,
                failures=[
                    CriticFailure(
                        evaluation_dimension="arbitration",
                        provider_name="runtime",
                        error=f"arbitration timed out after {timeout_seconds}s",
                    )
                ],
                triggered_by=["arbitration_timeout"],
            )

        assessments = [assessment for assessment, _failure in results if assessment is not None]
        failures = [failure for _assessment, failure in results if failure is not None]

        if len(assessments) < 2:
            return ConsensusVerdict.blocked(
                payload,
                assessments=assessments,
                failures=failures,
                triggered_by=["insufficient_critic_quorum"],
            )

        triggered_by = self._find_triggers(assessments)
        triggered_by.extend(
            f"critic_failure_{failure.evaluation_dimension}" for failure in failures
        )
        if not triggered_by:
            return ConsensusVerdict.pass_verdict(payload, assessments)

        try:
            adjudication = await _with_retries(
                lambda: self.adjudicator.adjudicate(
                    payload,
                    assessments,
                    failures,
                    triggered_by,
                ),
                self.retry_attempts,
            )
        except Exception as exc:
            return ConsensusVerdict.blocked(
                payload,
                assessments=assessments,
                failures=[
                    *failures,
                    CriticFailure(
                        evaluation_dimension="adjudication",
                        provider_name=getattr(self.adjudicator, "provider_name", "adjudicator"),
                        error=str(exc),
                    ),
                ],
                triggered_by=[*triggered_by, "adjudicator_failure"],
            )

        return self._verdict_from_adjudication(payload, assessments, failures, triggered_by, adjudication)

    async def _run_critic(
        self,
        critic: CriticClient,
        payload: ArbitrationPayload,
    ) -> tuple[CriticAssessmentSchema | None, CriticFailure | None]:
        dimension = str(getattr(critic, "dimension", "unknown"))
        provider_name = str(getattr(critic, "provider_name", "unknown"))
        try:
            assessment = await _with_retries(lambda: critic.assess(payload), self.retry_attempts)
            assessment = CriticAssessmentSchema.model_validate(assessment)
            if assessment.evaluation_dimension != dimension:
                raise ValueError(
                    "critic returned dimension "
                    f"{assessment.evaluation_dimension!r}, expected {dimension!r}"
                )
            return assessment, None
        except Exception as exc:
            return None, CriticFailure(
                evaluation_dimension=dimension,
                provider_name=provider_name,
                error=str(exc),
            )

    def _find_triggers(self, assessments: Sequence[CriticAssessmentSchema]) -> list[str]:
        triggers: list[str] = []
        for assessment in assessments:
            if assessment.assigned_score < self.score_threshold:
                triggers.append(
                    f"{assessment.evaluation_dimension}_score_below_{self.score_threshold}"
                )
            if assessment.flagged_anomalies:
                triggers.append(f"{assessment.evaluation_dimension}_flagged_anomalies")
        return triggers

    def _verdict_from_adjudication(
        self,
        payload: ArbitrationPayload,
        assessments: Sequence[CriticAssessmentSchema],
        failures: Sequence[CriticFailure],
        triggered_by: Sequence[str],
        adjudication: AdjudicationSchema,
    ) -> ConsensusVerdict:
        if adjudication.action == "release_original":
            return ConsensusVerdict(
                payload_id=_payload_id(payload),
                status=ConsensusStatus.PASSED_DEGRADED if failures else ConsensusStatus.PASSED,
                delivery_allowed=True,
                delivered_output=payload.output_text,
                adjudication_required=True,
                critic_assessments=list(assessments),
                critic_failures=list(failures),
                triggered_by=list(triggered_by),
                adjudication=adjudication,
                confidence_coefficient=adjudication.confidence_coefficient,
            )

        if adjudication.action == "remediate" and adjudication.remediated_output:
            return ConsensusVerdict(
                payload_id=_payload_id(payload),
                status=ConsensusStatus.REMEDIATED,
                delivery_allowed=True,
                delivered_output=adjudication.remediated_output,
                adjudication_required=True,
                critic_assessments=list(assessments),
                critic_failures=list(failures),
                triggered_by=list(triggered_by),
                adjudication=adjudication,
                confidence_coefficient=adjudication.confidence_coefficient,
            )

        status = (
            ConsensusStatus.BLOCKED
            if adjudication.action == "block"
            else ConsensusStatus.MANUAL_REVIEW
        )
        return ConsensusVerdict.blocked(
            payload,
            status=status,
            assessments=assessments,
            failures=failures,
            triggered_by=triggered_by,
            adjudication=adjudication,
        )


class OpenAICriticClient:
    provider_name = "openai"

    def __init__(
        self,
        dimension: EvaluationDimension,
        *,
        model: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.dimension = dimension
        self.model = model or settings.openai_arbitration_model
        self.api_key = api_key if api_key is not None else settings.openai_api_key
        self.timeout_seconds = timeout_seconds or settings.arbitration_timeout_seconds

    async def assess(self, payload: ArbitrationPayload) -> CriticAssessmentSchema:
        return await asyncio.to_thread(self._assess_sync, payload)

    def _assess_sync(self, payload: ArbitrationPayload) -> CriticAssessmentSchema:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured")

        import instructor  # noqa: PLC0415
        from openai import OpenAI  # noqa: PLC0415

        client = instructor.from_openai(
            OpenAI(api_key=self.api_key, timeout=self.timeout_seconds)
        )
        return client.chat.completions.create(
            model=self.model,
            response_model=CriticAssessmentSchema,
            messages=_critic_messages(payload, self.dimension),
            max_tokens=800,
            temperature=0,
        )


class AnthropicCriticClient:
    dimension: EvaluationDimension = "logic"
    provider_name = "anthropic"

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.model = model or settings.anthropic_arbitration_model
        self.api_key = api_key if api_key is not None else settings.anthropic_api_key
        self.timeout_seconds = timeout_seconds or settings.arbitration_timeout_seconds

    async def assess(self, payload: ArbitrationPayload) -> CriticAssessmentSchema:
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not configured")

        messages = _critic_messages(payload, self.dimension)
        system = messages[0]["content"]
        user = messages[1]["content"]
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self.model,
                    "max_tokens": 800,
                    "temperature": 0,
                    "system": system,
                    "messages": [{"role": "user", "content": user}],
                },
            )
            response.raise_for_status()
        content = _extract_anthropic_text(response.json().get("content") or [])
        return CriticAssessmentSchema.model_validate(_extract_json_object(content))


class OllamaCriticClient:
    dimension: EvaluationDimension = "completeness"
    provider_name = "ollama"

    def __init__(
        self,
        *,
        model: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.model = model or settings.ollama_arbitration_model
        self.base_url = (base_url or settings.ollama_base_url).rstrip("/")
        self.timeout_seconds = timeout_seconds or settings.arbitration_timeout_seconds

    async def assess(self, payload: ArbitrationPayload) -> CriticAssessmentSchema:
        messages = _critic_messages(payload, self.dimension)
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model,
                    "messages": messages,
                    "format": "json",
                    "stream": False,
                    "options": {"temperature": 0},
                },
            )
            response.raise_for_status()
        content = response.json()["message"]["content"]
        return CriticAssessmentSchema.model_validate(_extract_json_object(content))


class OpenAIAdjudicatorClient:
    provider_name = "openai_adjudicator"

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.model = model or settings.openai_adjudicator_model
        self.api_key = api_key if api_key is not None else settings.openai_api_key
        self.timeout_seconds = timeout_seconds or settings.arbitration_timeout_seconds

    async def adjudicate(
        self,
        payload: ArbitrationPayload,
        assessments: Sequence[CriticAssessmentSchema],
        failures: Sequence[CriticFailure],
        triggered_by: Sequence[str],
    ) -> AdjudicationSchema:
        return await asyncio.to_thread(
            self._adjudicate_sync,
            payload,
            assessments,
            failures,
            triggered_by,
        )

    def _adjudicate_sync(
        self,
        payload: ArbitrationPayload,
        assessments: Sequence[CriticAssessmentSchema],
        failures: Sequence[CriticFailure],
        triggered_by: Sequence[str],
    ) -> AdjudicationSchema:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured for adjudication")

        import instructor  # noqa: PLC0415
        from openai import OpenAI  # noqa: PLC0415

        client = instructor.from_openai(
            OpenAI(api_key=self.api_key, timeout=self.timeout_seconds)
        )
        return client.chat.completions.create(
            model=self.model,
            response_model=AdjudicationSchema,
            messages=_adjudication_messages(payload, assessments, failures, triggered_by),
            max_tokens=1200,
            temperature=0,
        )


T = TypeVar("T")


def _is_transient_exception(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError | asyncio.TimeoutError | ConnectionError | OSError):
        return True
    if isinstance(exc, httpx.TimeoutException | httpx.ConnectError | httpx.NetworkError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {429, 500, 502, 503, 504}
    try:
        from openai import (  # noqa: PLC0415
            APIConnectionError,
            APITimeoutError,
            InternalServerError,
            RateLimitError,
        )

        if isinstance(exc, APIConnectionError | APITimeoutError | RateLimitError | InternalServerError):
            return True
    except ImportError:
        pass
    return False


async def _with_retries(operation: Callable[[], Any], attempts: int) -> T:
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(max(1, attempts)),
        wait=wait_exponential(multiplier=0.2, max=2),
        retry=retry_if_exception(_is_transient_exception),
        reraise=True,
    ):
        with attempt:
            result = operation()
            if asyncio.iscoroutine(result):
                return await result
            return result
    raise RuntimeError("retry loop exited without a result")


def _default_critics() -> list[CriticClient]:
    return [
        OpenAICriticClient("factual"),
        AnthropicCriticClient(),
        OllamaCriticClient(),
    ]


def _critic_messages(payload: ArbitrationPayload, dimension: EvaluationDimension) -> list[dict[str, str]]:
    role_descriptions = {
        "factual": "Check factual accuracy, verifiability, and unsupported claims.",
        "logic": "Check whether the reasoning is internally consistent and conclusions follow.",
        "completeness": "Check whether the response addresses all parts of the prompt.",
    }
    return [
        {
            "role": "system",
            "content": (
                "You are a strict LLM output critic. "
                f"{role_descriptions[dimension]} "
                "Return only JSON matching these fields: evaluation_dimension, "
                "assigned_score, flagged_anomalies, confidence_coefficient. "
                f"Set evaluation_dimension exactly to {dimension!r}."
            ),
        },
        {
            "role": "user",
            "content": (
                "Original prompt:\n"
                f"{payload.original_prompt or '[not provided]'}\n\n"
                "Outgoing response to evaluate:\n"
                f"{payload.output_text}"
            ),
        },
    ]


def _adjudication_messages(
    payload: ArbitrationPayload,
    assessments: Sequence[CriticAssessmentSchema],
    failures: Sequence[CriticFailure],
    triggered_by: Sequence[str],
) -> list[dict[str, str]]:
    evidence = {
        "triggered_by": list(triggered_by),
        "critic_assessments": [assessment.model_dump() for assessment in assessments],
        "critic_failures": [failure.model_dump() for failure in failures],
    }
    return [
        {
            "role": "system",
            "content": (
                "You are the final adjudicator for an LLM safety and quality gate. "
                "Return only JSON matching the AdjudicationSchema. Use action "
                "'release_original' only when delivery is safe, 'remediate' only "
                "with a corrected remediated_output, and otherwise 'block' or "
                "'manual_review'."
            ),
        },
        {
            "role": "user",
            "content": (
                "Original prompt:\n"
                f"{payload.original_prompt or '[not provided]'}\n\n"
                "Outgoing response:\n"
                f"{payload.output_text}\n\n"
                "Critic evidence JSON:\n"
                f"{json.dumps(evidence, sort_keys=True)}"
            ),
        },
    ]


def _extract_anthropic_text(content_blocks: Sequence[Mapping[str, Any]]) -> str:
    text_parts: list[str] = []
    for block in content_blocks:
        if not isinstance(block, Mapping):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            text_parts.append(block["text"])
    if not text_parts:
        raise ValueError("anthropic response did not contain a text content block")
    return "\n".join(text_parts)


def _extract_json_object(text: str) -> dict[str, Any]:
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise
        loaded = json.loads(match.group(0))
    if not isinstance(loaded, dict):
        raise ValueError("model response did not contain a JSON object")
    return loaded


def _payload_id(payload: ArbitrationPayload) -> str:
    seed = json.dumps(
        {
            "output_text": payload.output_text,
            "original_prompt": payload.original_prompt,
            "session_id": payload.session_id,
        },
        sort_keys=True,
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"coremesh:arbitration:{seed}"))


def _combined_confidence(
    assessments: Sequence[CriticAssessmentSchema],
    failures: Sequence[CriticFailure],
) -> float:
    if not assessments:
        return 0.0
    base = sum(item.confidence_coefficient for item in assessments) / len(assessments)
    penalty = 0.15 * len(failures)
    return max(0.0, round(base - penalty, 4))


__all__ = [
    "AdjudicationSchema",
    "ArbitrationPayload",
    "BLOCKED_RESPONSE",
    "ConsensusArbitrator",
    "ConsensusStatus",
    "ConsensusVerdict",
    "CriticAssessmentSchema",
    "CriticClient",
    "CriticFailure",
    "AdjudicatorClient",
    "OpenAIAdjudicatorClient",
    "OpenAICriticClient",
    "AnthropicCriticClient",
    "OllamaCriticClient",
]
