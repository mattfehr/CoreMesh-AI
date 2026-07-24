"""Typed records shared by self-healing documentation pipeline stages.

System role:
    Defines deterministic boundaries between Git discovery, structural
    parsing, retrieval, model judgment, Markdown mutation, and reporting.
Dependencies:
    Python dataclasses and standard collection types.
Side effects:
    None; these records hold in-memory run state only.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


ChangeType = Literal["added", "removed", "modified"]


@dataclass(frozen=True)
class SourceFileDelta:
    """One changed structural source file across the requested Git revisions."""

    status: str
    old_path: str | None
    new_path: str | None
    old_text: str | None
    new_text: str | None
    language: Literal["python", "go", "compose"]

    @property
    def display_path(self) -> str:
        """Return the surviving path, or the removed path for deletions."""

        return self.new_path or self.old_path or "<unknown>"


@dataclass(frozen=True)
class StructuralUnit:
    """Normalized documentation-relevant structure extracted from one file."""

    path: str
    language: str
    kind: str
    identity: str
    name: str
    representation: str
    context: str
    search_terms: tuple[str, ...]
    start_line: int
    end_line: int
    modification_is_bounded: bool


@dataclass(frozen=True)
class StructuralChange:
    """Before/after structural adjustment used for retrieval and LLM checks."""

    change_id: str
    path: str
    language: str
    kind: str
    name: str
    change_type: ChangeType
    before: str | None
    after: str | None
    context: str
    search_terms: tuple[str, ...]
    start_line: int
    end_line: int
    auto_fix_eligible: bool

    def embedding_text(self) -> str:
        """Render the structural delta as bounded semantic retrieval text."""

        return "\n".join(
            [
                f"File: {self.path}",
                f"Language: {self.language}",
                f"Structure: {self.kind} {self.name}",
                f"Change: {self.change_type}",
                f"Before: {_bounded_text(self.before, 4_000, '<absent>')}",
                f"After: {_bounded_text(self.after, 4_000, '<absent>')}",
                f"Context: {_bounded_text(self.context, 1_000, '<none>')}",
            ]
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a stable JSON-compatible representation."""

        return asdict(self)


@dataclass(frozen=True)
class MarkdownDocument:
    """Tracked Markdown bytes plus encoding/newline metadata."""

    path: str
    text: str
    newline: str


@dataclass(frozen=True)
class DocSection:
    """One non-overlapping ATX-heading block in a tracked Markdown file."""

    section_id: str
    path: str
    heading: str
    heading_path: tuple[str, ...]
    level: int
    body: str
    body_start: int
    body_end: int
    newline: str
    ordinal: int

    @property
    def label(self) -> str:
        """Return the human-readable heading breadcrumb."""

        return " > ".join(self.heading_path)

    def embedding_text(self, *, max_chars: int = 12_000) -> str:
        """Render bounded Markdown context for embedding retrieval."""

        return f"Document: {self.path}\nSection: {self.label}\n{self.body[:max_chars]}"


@dataclass(frozen=True)
class CandidateMatch:
    """One change-to-section link produced by lexical or semantic retrieval."""

    change_id: str
    section_id: str
    similarity: float
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation."""

        return asdict(self)


@dataclass(frozen=True)
class PendingReplacement:
    """Mechanically validated body replacement awaiting file-level application."""

    section: DocSection
    replacement_body: str
    change_ids: tuple[str, ...]
    assessment_confidence: float
    validation_confidence: float
    diagnosis: str


@dataclass
class RunReport:
    """Mutable aggregate serialized to report.json and summary.md."""

    base_sha: str
    head_sha: str
    apply_requested: bool
    configuration: dict[str, Any]
    structural_changes: list[dict[str, Any]] = field(default_factory=list)
    candidate_mappings: list[dict[str, Any]] = field(default_factory=list)
    llm_decisions: list[dict[str, Any]] = field(default_factory=list)
    verified_sections: list[dict[str, Any]] = field(default_factory=list)
    proposed_repairs: list[dict[str, Any]] = field(default_factory=list)
    applied_repairs: list[dict[str, Any]] = field(default_factory=list)
    review_items: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    changed_markdown_paths: list[str] = field(default_factory=list)
    status: str = "running"

    def to_dict(self) -> dict[str, Any]:
        """Return a stable JSON-compatible report payload."""

        payload = asdict(self)
        payload["metrics"] = {
            "structural_changes": len(self.structural_changes),
            "candidate_mappings": len(self.candidate_mappings),
            "llm_decisions": len(self.llm_decisions),
            "verified_sections": len(self.verified_sections),
            "proposed_repairs": len(self.proposed_repairs),
            "applied_repairs": len(self.applied_repairs),
            "review_items": len(self.review_items),
            "errors": len(self.errors),
        }
        return payload


def _bounded_text(value: str | None, max_chars: int, fallback: str) -> str:
    if not value:
        return fallback
    if len(value) <= max_chars:
        return value
    half = (max_chars - 32) // 2
    return f"{value[:half]}\n...[input truncated]...\n{value[-half:]}"
