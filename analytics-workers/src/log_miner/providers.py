"""Production OpenAI adapters for embeddings and structured reference labels."""
from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from typing import Any, TypeVar

from .models import GeneratedReference


_T = TypeVar("_T")
_TRANSIENT_EXCEPTION_NAMES = {
    "APIConnectionError",
    "APITimeoutError",
    "ConflictError",
    "InternalServerError",
    "RateLimitError",
}


def is_transient_provider_error(error: BaseException) -> bool:
    """Classify retryable OpenAI/network errors without importing the SDK eagerly."""

    status_code = getattr(error, "status_code", None)
    if isinstance(status_code, int) and (
        status_code in {408, 409, 429} or 500 <= status_code < 600
    ):
        return True
    return type(error).__name__ in _TRANSIENT_EXCEPTION_NAMES


def _with_retry(
    operation: Callable[[], _T],
    *,
    attempts: int,
    sleep: Callable[[float], None] = time.sleep,
) -> _T:
    """Retry transient provider failures with a small bounded backoff."""

    if attempts < 1:
        raise ValueError("attempts must be at least one")
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as error:
            if attempt >= attempts or not is_transient_provider_error(error):
                raise
            sleep(min(0.25 * (2 ** (attempt - 1)), 2.0))
    raise AssertionError("retry loop exited unexpectedly")


class OpenAIEmbeddingProvider:
    """Batch prompts through the official embeddings interface."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "text-embedding-3-small",
        batch_size: int = 128,
        retry_attempts: int = 3,
        client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be at least one")
        self._api_key = api_key
        self._model = model
        self._batch_size = batch_size
        self._retry_attempts = retry_attempts
        self._client = client
        self._sleep = sleep

    @property
    def client(self) -> Any:
        if self._client is None:
            if not self._api_key.strip():
                raise ValueError("OPENAI_API_KEY is required for production providers")
            from openai import OpenAI

            self._client = OpenAI(api_key=self._api_key)
        return self._client

    @property
    def model(self) -> str:
        return self._model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []

        vectors: list[list[float]] = []
        for offset in range(0, len(texts), self._batch_size):
            batch = list(texts[offset : offset + self._batch_size])

            def request() -> Any:
                return self.client.embeddings.create(
                    model=self._model,
                    input=batch,
                    encoding_format="float",
                )

            response = _with_retry(
                request,
                attempts=self._retry_attempts,
                sleep=self._sleep,
            )
            data = sorted(response.data, key=lambda item: item.index)
            if len(data) != len(batch):
                raise ValueError(
                    f"embedding provider returned {len(data)} vectors for {len(batch)} prompts"
                )
            vectors.extend([list(item.embedding) for item in data])
        return vectors


class OpenAIReferenceAnswerGenerator:
    """Generate Pydantic-validated labels with the Responses API."""

    _SYSTEM_PROMPT = """You curate regression cases from privacy-redacted production failures.
Return a safe, standalone reference answer, concrete validation criteria, the expected
behavior (answer, refuse, or clarify), a concise failure pattern, difficulty, and a
calibrated label confidence. Do not reconstruct redacted data. Confidence measures
whether this case can be promoted without human review; use below 0.80 when the ideal
answer depends on missing context or the examples are ambiguous."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gpt-4o",
        retry_attempts: int = 3,
        client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._retry_attempts = retry_attempts
        self._client = client
        self._sleep = sleep

    @property
    def client(self) -> Any:
        if self._client is None:
            if not self._api_key.strip():
                raise ValueError("OPENAI_API_KEY is required for production providers")
            from openai import OpenAI

            self._client = OpenAI(api_key=self._api_key)
        return self._client

    @property
    def model(self) -> str:
        return self._model

    def generate(
        self,
        *,
        feature_scope: str,
        representative_prompt: str,
        cluster_examples: Sequence[str],
    ) -> GeneratedReference:
        payload = {
            "feature_scope": feature_scope,
            "representative_prompt": representative_prompt,
            "nearest_distinct_examples": list(cluster_examples),
        }

        def request() -> Any:
            return self.client.responses.parse(
                model=self._model,
                store=False,
                input=[
                    {"role": "system", "content": self._SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                text_format=GeneratedReference,
            )

        response = _with_retry(
            request,
            attempts=self._retry_attempts,
            sleep=self._sleep,
        )
        parsed = response.output_parsed
        if parsed is None:
            raise ValueError("reference generator returned no parsed structured output")
        if isinstance(parsed, GeneratedReference):
            return parsed
        return GeneratedReference.model_validate(parsed)
