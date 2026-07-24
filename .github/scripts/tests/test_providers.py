"""Offline contract tests for typed OpenAI provider adapters."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from self_healing_docs.models import DocSection, StructuralChange
from self_healing_docs.providers import (
    OpenAIDocRepairProvider,
    OpenAIEmbeddingProvider,
    ProviderError,
    RepairValidation,
    StalenessAssessment,
)


def _change() -> StructuralChange:
    return StructuralChange(
        change_id="change",
        path="service.py",
        language="python",
        kind="python_field",
        name="Settings.timeout",
        change_type="modified",
        before="Settings.timeout: int = 30",
        after="Settings.timeout: float = 15.0",
        context="Settings field",
        search_terms=("timeout",),
        start_line=1,
        end_line=1,
        auto_fix_eligible=True,
    )


def _section() -> DocSection:
    return DocSection(
        section_id="section",
        path="README.md",
        heading="Configuration",
        heading_path=("Demo", "Configuration"),
        level=2,
        body="\nTimeout is an integer.\n",
        body_start=10,
        body_end=34,
        newline="\n",
        ordinal=2,
    )


def test_responses_parse_uses_explicit_model_reasoning_and_no_tools() -> None:
    captured = {}

    class Responses:
        def parse(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                output_parsed=StalenessAssessment(
                    stale=True,
                    confidence=0.95,
                    complexity="bounded",
                    diagnosis="Stale type.",
                    affected_facts=["type"],
                )
            )

    provider = OpenAIDocRepairProvider(
        api_key="test",
        model="gpt-5.6-luna",
        reasoning_effort="low",
    )
    provider.client = SimpleNamespace(responses=Responses())
    assessment = provider.assess(changes=[_change()], section=_section())

    assert assessment.stale is True
    assert captured["model"] == "gpt-5.6-luna"
    assert captured["reasoning"] == {"effort": "low"}
    assert captured["text_format"] is StalenessAssessment
    assert captured["tools"] == []
    assert captured["max_output_tokens"] == 6_000
    assert captured["store"] is False
    assert "untrusted data" in captured["input"][0]["content"]


def test_embedding_adapter_batches_and_restores_response_index_order() -> None:
    calls = []

    class Embeddings:
        def create(self, **kwargs):
            calls.append(kwargs)
            data = [
                SimpleNamespace(index=index, embedding=[float(len(text)), float(index)])
                for index, text in enumerate(kwargs["input"])
            ]
            return SimpleNamespace(data=list(reversed(data)))

    provider = OpenAIEmbeddingProvider(
        api_key="test",
        model="text-embedding-3-small",
        batch_size=2,
    )
    provider.client = SimpleNamespace(embeddings=Embeddings())

    assert provider.embed(["a", "bbbb", "cc"]) == [
        [1.0, 0.0],
        [4.0, 1.0],
        [2.0, 0.0],
    ]
    assert [call["input"] for call in calls] == [["a", "bbbb"], ["cc"]]
    assert all(call["encoding_format"] == "float" for call in calls)


@pytest.mark.parametrize("parsed", [None, {"stale": "not-a-boolean"}])
def test_responses_refusal_or_invalid_schema_fails_closed(parsed) -> None:
    class Responses:
        def parse(self, **kwargs):
            return SimpleNamespace(output_parsed=parsed)

    provider = OpenAIDocRepairProvider(
        api_key="test",
        model="gpt-5.6-luna",
        reasoning_effort="low",
    )
    provider.client = SimpleNamespace(responses=Responses())

    with pytest.raises(ProviderError):
        provider.assess(changes=[_change()], section=_section())


def test_validation_approval_requires_every_dimension_and_no_issues() -> None:
    approved = RepairValidation(
        accurate=True,
        preserves_unaffected_content=True,
        style_consistent=True,
        no_unverified_claims=True,
        confidence=0.99,
        issues=[],
    )
    rejected = approved.model_copy(update={"issues": ["Unsupported claim"]})
    assert approved.approved is True
    assert rejected.approved is False
