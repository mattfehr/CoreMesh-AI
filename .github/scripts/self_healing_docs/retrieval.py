"""Lexical and embedding retrieval for structural change-to-doc mappings.

System role:
    Narrows the tracked Markdown corpus to bounded candidate sections before
    any LLM judgment, combining exact technical references with cosine search.
Dependencies:
    An injected embedding provider and normalized pipeline records.
Side effects:
    The production provider performs OpenAI network calls; this module itself
    only validates vectors and computes rankings in memory.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Protocol

from .models import CandidateMatch, DocSection, StructuralChange


class RetrievalError(ValueError):
    """Raised when embeddings or retrieval inputs violate expected contracts."""


class EmbeddingProvider(Protocol):
    """Interface implemented by OpenAI and deterministic test providers."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one finite non-empty vector per input text."""


@dataclass(frozen=True)
class RetrievalResult:
    """Candidate mappings plus changes omitted by the global safety cap."""

    matches: tuple[CandidateMatch, ...]
    truncated_change_ids: tuple[str, ...]


_COMMON_TERMS = {
    "application",
    "class",
    "config",
    "configuration",
    "context",
    "default",
    "dict",
    "float",
    "field",
    "function",
    "handler",
    "int",
    "integer",
    "list",
    "method",
    "module",
    "request",
    "response",
    "route",
    "service",
    "settings",
    "string",
    "type",
}


def retrieve_candidates(
    *,
    changes: list[StructuralChange],
    sections: list[DocSection],
    provider: EmbeddingProvider,
    similarity_threshold: float,
    top_k: int,
    max_candidates: int,
) -> RetrievalResult:
    """Map every structural change to exact and semantically similar sections."""

    if not changes or not sections:
        return RetrievalResult(matches=(), truncated_change_ids=())

    texts = [change.embedding_text() for change in changes]
    texts.extend(section.embedding_text() for section in sections)
    vectors = provider.embed(texts)
    vectors = _validate_vectors(vectors, expected_count=len(texts))
    change_vectors = vectors[: len(changes)]
    section_vectors = vectors[len(changes) :]

    proposed: list[CandidateMatch] = []
    for change, change_vector in zip(changes, change_vectors, strict=True):
        scored = [
            (section, _cosine_similarity(change_vector, section_vector))
            for section, section_vector in zip(sections, section_vectors, strict=True)
        ]
        semantic_ids = {
            section.section_id
            for section, score in sorted(
                scored, key=lambda item: (-item[1], item[0].section_id)
            )[:top_k]
            if score >= similarity_threshold
        }
        lexical_ids = {
            section.section_id
            for section in sections
            if _has_exact_reference(change, section)
        }
        for section, score in scored:
            reasons: list[str] = []
            if section.section_id in lexical_ids:
                reasons.append("exact_reference")
            if section.section_id in semantic_ids:
                reasons.append("embedding_similarity")
            if reasons:
                proposed.append(
                    CandidateMatch(
                        change_id=change.change_id,
                        section_id=section.section_id,
                        similarity=round(score, 6),
                        reasons=tuple(reasons),
                    )
                )

    ordered = sorted(
        proposed,
        key=lambda item: (
            0 if "exact_reference" in item.reasons else 1,
            -item.similarity,
            item.change_id,
            item.section_id,
        ),
    )
    accepted = ordered[:max_candidates]
    accepted_change_ids = {item.change_id for item in accepted}
    proposed_change_ids = {item.change_id for item in ordered}
    truncated = tuple(
        sorted(
            change_id
            for change_id in proposed_change_ids
            if change_id not in accepted_change_ids
        )
    )
    return RetrievalResult(matches=tuple(accepted), truncated_change_ids=truncated)


def _has_exact_reference(change: StructuralChange, section: DocSection) -> bool:
    haystack = f"{section.label}\n{section.body}".casefold()
    for term in _lexical_terms(change):
        normalized = term.casefold()
        if normalized.startswith("/") and normalized in haystack:
            return True
        pattern = rf"(?<![a-z0-9_.-]){re.escape(normalized)}(?![a-z0-9_.-])"
        if re.search(pattern, haystack):
            return True
    return False


def _lexical_terms(change: StructuralChange) -> tuple[str, ...]:
    terms: set[str] = {change.name}
    for raw in change.search_terms:
        value = raw.strip()
        if 3 <= len(value) <= 160:
            terms.add(value)
        terms.update(
            token
            for token in re.findall(r"[A-Za-z_][A-Za-z0-9_.-]{2,}", value)
            if token.casefold() not in _COMMON_TERMS
        )
        terms.update(
            route
            for route in re.findall(r"/[A-Za-z0-9_./{}:-]+", value)
            if len(route) >= 3
        )
    return tuple(
        sorted(
            {
                term
                for term in terms
                if len(term) >= 3 and term.casefold() not in _COMMON_TERMS
            },
            key=lambda item: (-len(item), item.casefold()),
        )
    )


def _validate_vectors(
    vectors: list[list[float]],
    *,
    expected_count: int,
) -> list[list[float]]:
    if len(vectors) != expected_count:
        raise RetrievalError(
            f"embedding provider returned {len(vectors)} vectors for "
            f"{expected_count} texts"
        )
    width: int | None = None
    normalized: list[list[float]] = []
    for index, vector in enumerate(vectors):
        if not vector:
            raise RetrievalError(f"embedding {index} is empty")
        values = [float(value) for value in vector]
        if not all(math.isfinite(value) for value in values):
            raise RetrievalError(f"embedding {index} contains non-finite values")
        if width is None:
            width = len(values)
        elif len(values) != width:
            raise RetrievalError("embedding vectors have inconsistent dimensions")
        norm = math.sqrt(sum(value * value for value in values))
        if norm == 0.0:
            raise RetrievalError(f"embedding {index} has zero magnitude")
        normalized.append([value / norm for value in values])
    return normalized


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise RetrievalError("cannot compare embeddings with different dimensions")
    return max(-1.0, min(1.0, sum(a * b for a, b in zip(left, right, strict=True))))
