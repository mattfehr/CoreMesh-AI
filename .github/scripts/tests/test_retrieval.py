"""Unit coverage for exact-reference and cosine documentation retrieval."""
from __future__ import annotations

import math

import pytest

from self_healing_docs.models import DocSection, StructuralChange
from self_healing_docs.retrieval import RetrievalError, retrieve_candidates


class KeywordEmbeddings:
    """Deterministic semantic geometry keyed by timeout/cache vocabulary."""

    def embed(self, texts):
        vectors = []
        for text in texts:
            lower = text.lower()
            vectors.append(
                [
                    1.0 if "timeout" in lower else 0.01,
                    1.0 if "cache" in lower else 0.01,
                ]
            )
        return vectors


def _change(name="Settings.timeout", terms=("timeout",)) -> StructuralChange:
    return StructuralChange(
        change_id="change-1",
        path="service.py",
        language="python",
        kind="python_field",
        name=name,
        change_type="modified",
        before="Settings.timeout: int = 30",
        after="Settings.timeout: float = 15.0",
        context="Settings field",
        search_terms=terms,
        start_line=1,
        end_line=1,
        auto_fix_eligible=True,
    )


def _section(section_id: str, heading: str, body: str) -> DocSection:
    return DocSection(
        section_id=section_id,
        path="README.md",
        heading=heading,
        heading_path=("README", heading),
        level=2,
        body=body,
        body_start=0,
        body_end=len(body),
        newline="\n",
        ordinal=1,
    )


def test_exact_and_semantic_candidates_are_deduplicated() -> None:
    sections = [
        _section("timeout", "Configuration", "TIMEOUT is currently an integer."),
        _section("cache", "Caching", "The semantic cache stores responses."),
    ]
    result = retrieve_candidates(
        changes=[_change(terms=("TIMEOUT",))],
        sections=sections,
        provider=KeywordEmbeddings(),
        similarity_threshold=0.8,
        top_k=1,
        max_candidates=5,
    )

    assert len(result.matches) == 1
    assert result.matches[0].section_id == "timeout"
    assert set(result.matches[0].reasons) == {
        "exact_reference",
        "embedding_similarity",
    }


def test_short_identifier_does_not_match_inside_unrelated_word() -> None:
    sections = [
        _section("runtime", "Runtime", "The runtime remains available."),
        _section("run", "API", "Call `run` with a timeout."),
    ]
    result = retrieve_candidates(
        changes=[_change(name="run", terms=("run",))],
        sections=sections,
        provider=KeywordEmbeddings(),
        similarity_threshold=1.0,
        top_k=1,
        max_candidates=5,
    )

    exact_ids = {
        match.section_id
        for match in result.matches
        if "exact_reference" in match.reasons
    }
    assert exact_ids == {"run"}


@pytest.mark.parametrize(
    "vectors",
    [
        [],
        [[0.0, 0.0], [1.0, 0.0]],
        [[math.nan, 1.0], [1.0, 0.0]],
        [[1.0], [1.0, 0.0]],
    ],
)
def test_invalid_embedding_contract_fails_closed(vectors) -> None:
    class InvalidProvider:
        def embed(self, texts):
            return vectors

    with pytest.raises(RetrievalError):
        retrieve_candidates(
            changes=[_change()],
            sections=[_section("timeout", "Configuration", "timeout")],
            provider=InvalidProvider(),
            similarity_threshold=0.4,
            top_k=1,
            max_candidates=5,
        )
