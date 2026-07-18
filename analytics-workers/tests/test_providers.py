"""Provider adapter tests; no external API calls are made."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.log_miner.models import GeneratedReference
from src.log_miner.providers import (
    OpenAIEmbeddingProvider,
    OpenAIReferenceAnswerGenerator,
)


class RateLimitError(Exception):
    pass


class _EmbeddingEndpoint:
    def __init__(self) -> None:
        self.calls = []
        self.failures = 2

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.failures:
            self.failures -= 1
            raise RateLimitError("retry me")
        data = [
            SimpleNamespace(index=index, embedding=[float(len(text)), float(index + 1)])
            for index, text in reversed(list(enumerate(kwargs["input"])))
        ]
        return SimpleNamespace(data=data)


def test_embeddings_batch_order_and_transient_retry() -> None:
    endpoint = _EmbeddingEndpoint()
    provider = OpenAIEmbeddingProvider(
        api_key="",
        model="embedding-test",
        batch_size=2,
        retry_attempts=3,
        client=SimpleNamespace(embeddings=endpoint),
        sleep=lambda seconds: None,
    )
    vectors = provider.embed(["a", "bbbb", "cc"])
    assert vectors == [[1.0, 1.0], [4.0, 2.0], [2.0, 1.0]]
    assert len(endpoint.calls) == 4
    assert all(call["model"] == "embedding-test" for call in endpoint.calls)
    assert all(call["encoding_format"] == "float" for call in endpoint.calls)


class _BadEndpoint:
    def __init__(self) -> None:
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        raise ValueError("not transient")


def test_non_transient_provider_error_is_not_retried() -> None:
    endpoint = _BadEndpoint()
    provider = OpenAIEmbeddingProvider(
        api_key="",
        client=SimpleNamespace(embeddings=endpoint),
        sleep=lambda seconds: None,
    )
    with pytest.raises(ValueError, match="not transient"):
        provider.embed(["prompt"])
    assert endpoint.calls == 1


class _ResponsesEndpoint:
    def __init__(self) -> None:
        self.kwargs = None

    def parse(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            output_parsed={
                "reference_answer": "Use the documented safe workflow.",
                "validation_criteria": [
                    {"description": "Explains the safe workflow", "required": True}
                ],
                "expected_behavior": "answer",
                "failure_pattern": "The response skipped a required step.",
                "difficulty_rating": "moderate",
                "label_confidence": 0.91,
            }
        )


def test_structured_generator_uses_responses_parse() -> None:
    endpoint = _ResponsesEndpoint()
    generator = OpenAIReferenceAnswerGenerator(
        api_key="",
        model="reference-test",
        client=SimpleNamespace(responses=endpoint),
        sleep=lambda seconds: None,
    )
    result = generator.generate(
        feature_scope="support",
        representative_prompt="Why did this fail?",
        cluster_examples=["Similar one", "Similar two"],
    )
    assert isinstance(result, GeneratedReference)
    assert result.label_confidence == 0.91
    assert endpoint.kwargs["model"] == "reference-test"
    assert endpoint.kwargs["store"] is False
    assert endpoint.kwargs["text_format"] is GeneratedReference
    assert len(endpoint.kwargs["input"]) == 2
