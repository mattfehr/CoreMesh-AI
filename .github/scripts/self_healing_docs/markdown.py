"""Safe Markdown section parsing and bounded replacement application.

System role:
    Builds the searchable documentation corpus and guarantees that an approved
    model proposal can change only an existing heading body.
Dependencies:
    Tracked-file discovery from the Git boundary and Python text processing.
Side effects:
    Corpus loading is read-only; apply functions rewrite approved UTF-8
    Markdown files in place after path and heading validation.
"""
from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from .gitops import GitError, tracked_markdown_paths, validate_relative_repo_path
from .models import DocSection, MarkdownDocument, PendingReplacement


class MarkdownSafetyError(ValueError):
    """Raised when a Markdown source or proposed replacement is unsafe."""


_HEADING_RE = re.compile(
    r"^ {0,3}(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*(?:\r\n|\n|\r)?$"
)
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_GENERATED_MARKDOWN_RE = re.compile(
    r"^analytics-workers/(?:smoke-)?(?:comparison|report)\.md$",
    re.IGNORECASE,
)


def load_markdown_corpus(
    repo_root: Path,
) -> tuple[dict[str, MarkdownDocument], list[DocSection]]:
    """Load tracked, human-authored Markdown and split it into heading blocks."""

    documents: dict[str, MarkdownDocument] = {}
    sections: list[DocSection] = []
    for relative_path in tracked_markdown_paths(repo_root):
        if is_generated_markdown(relative_path):
            continue
        absolute = _safe_markdown_path(repo_root, relative_path)
        raw = absolute.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MarkdownSafetyError(
                f"tracked Markdown is not UTF-8: {relative_path}"
            ) from exc
        newline = "\r\n" if "\r\n" in text else "\n"
        document = MarkdownDocument(path=relative_path, text=text, newline=newline)
        documents[relative_path] = document
        sections.extend(split_markdown_document(document))
    return documents, sections


def is_generated_markdown(path: str) -> bool:
    """Return whether tracked Markdown belongs to generated/dependency storage."""

    normalized = path.replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    if any(
        part in {
            ".git",
            "__pycache__",
            "artifacts",
            "env",
            "node_modules",
            "vendor",
            "venv",
        }
        or part.startswith(".venv")
        for part in parts
    ):
        return True
    return bool(_GENERATED_MARKDOWN_RE.match(normalized))


def split_markdown_document(document: MarkdownDocument) -> list[DocSection]:
    """Split one Markdown document into flat, non-overlapping ATX blocks."""

    text = document.text
    stack: list[str] = []
    sections: list[DocSection] = []
    pending: dict[str, object] | None = None
    offset = 0
    fence_char: str | None = None
    fence_length = 0
    ordinal = 0

    for line in text.splitlines(keepends=True):
        fence_match = _FENCE_RE.match(line)
        if fence_char is not None:
            if fence_match:
                marker = fence_match.group(1)
                if marker[0] == fence_char and len(marker) >= fence_length:
                    fence_char = None
                    fence_length = 0
            offset += len(line)
            continue
        if fence_match:
            marker = fence_match.group(1)
            fence_char = marker[0]
            fence_length = len(marker)
            offset += len(line)
            continue

        heading_match = _HEADING_RE.match(line)
        if heading_match:
            if pending is not None:
                sections.append(
                    _make_section(
                        document=document,
                        pending=pending,
                        body_end=offset,
                    )
                )
            level = len(heading_match.group(1))
            heading = heading_match.group(2).strip()
            stack = stack[: level - 1]
            while len(stack) < level - 1:
                stack.append("<untitled>")
            stack.append(heading)
            ordinal += 1
            pending = {
                "heading": heading,
                "heading_path": tuple(stack),
                "level": level,
                "body_start": offset + len(line),
                "ordinal": ordinal,
            }
        offset += len(line)

    if pending is not None:
        sections.append(
            _make_section(document=document, pending=pending, body_end=len(text))
        )
    return sections


def normalize_replacement_body(
    *,
    section: DocSection,
    proposed_body: str,
    max_chars: int,
) -> str:
    """Validate model text and preserve the original block's blank-line framing."""

    if "\0" in proposed_body:
        raise MarkdownSafetyError("replacement contains a NUL byte")
    if not proposed_body.strip():
        raise MarkdownSafetyError("replacement body must not be empty")
    if len(proposed_body) > max_chars:
        raise MarkdownSafetyError(
            f"replacement exceeds the {max_chars}-character section limit"
        )
    if _contains_atx_heading_outside_fence(proposed_body):
        raise MarkdownSafetyError(
            "replacement must not add, remove, or reproduce Markdown headings"
        )

    normalized = proposed_body.replace("\r\n", "\n").replace("\r", "\n")
    if section.newline != "\n":
        normalized = normalized.replace("\n", section.newline)
    core = normalized.strip(" \t\r\n")
    if not core:
        raise MarkdownSafetyError("replacement body has no non-whitespace content")

    leading = _leading_blank_lines(section.body)
    trailing = _trailing_blank_lines(section.body)
    return f"{leading}{core}{trailing}"


def apply_markdown_replacements(
    *,
    repo_root: Path,
    documents: dict[str, MarkdownDocument],
    replacements: Iterable[PendingReplacement],
) -> list[str]:
    """Apply non-overlapping replacements and verify heading invariance."""

    grouped: dict[str, list[PendingReplacement]] = defaultdict(list)
    for replacement in replacements:
        grouped[replacement.section.path].append(replacement)

    prepared: list[tuple[str, Path, str]] = []
    for relative_path, file_replacements in sorted(grouped.items()):
        document = documents.get(relative_path)
        if document is None:
            raise MarkdownSafetyError(
                f"replacement targets an unloaded Markdown file: {relative_path}"
            )
        _validate_non_overlapping(file_replacements)
        text = document.text
        before_signature = heading_signature(split_markdown_document(document))
        for replacement in sorted(
            file_replacements,
            key=lambda item: item.section.body_start,
            reverse=True,
        ):
            section = replacement.section
            current_body = text[section.body_start : section.body_end]
            if current_body != section.body:
                raise MarkdownSafetyError(
                    f"Markdown changed after retrieval: {section.path}#{section.heading}"
                )
            text = (
                text[: section.body_start]
                + replacement.replacement_body
                + text[section.body_end :]
            )

        after_document = MarkdownDocument(
            path=document.path,
            text=text,
            newline=document.newline,
        )
        after_signature = heading_signature(split_markdown_document(after_document))
        if after_signature != before_signature:
            raise MarkdownSafetyError(
                f"heading hierarchy changed while repairing {relative_path}"
            )
        if text == document.text:
            continue
        absolute = _safe_markdown_path(repo_root, relative_path)
        prepared.append((relative_path, absolute, text))

    # All path, overlap, content, and heading checks finish before the first
    # write, so model or validation errors cannot leave a partial repair set.
    for _, absolute, text in prepared:
        absolute.write_bytes(text.encode("utf-8"))
    return [relative_path for relative_path, _, _ in prepared]


def heading_signature(
    sections: Iterable[DocSection],
) -> tuple[tuple[int, tuple[str, ...]], ...]:
    """Return the structure that must remain unchanged after body replacement."""

    return tuple((section.level, section.heading_path) for section in sections)


def neighboring_context(
    section: DocSection,
    all_sections: list[DocSection],
    *,
    max_chars: int = 2_000,
) -> str:
    """Return small adjacent blocks to anchor tone without widening edit scope."""

    same_file = [item for item in all_sections if item.path == section.path]
    index = next(
        (position for position, item in enumerate(same_file) if item.section_id == section.section_id),
        None,
    )
    if index is None:
        return ""
    pieces: list[str] = []
    if index > 0:
        previous = same_file[index - 1]
        pieces.append(f"Previous — {previous.label}\n{previous.body[: max_chars // 2]}")
    if index + 1 < len(same_file):
        following = same_file[index + 1]
        pieces.append(f"Next — {following.label}\n{following.body[: max_chars // 2]}")
    return "\n\n".join(pieces)[:max_chars]


def _make_section(
    *,
    document: MarkdownDocument,
    pending: dict[str, object],
    body_end: int,
) -> DocSection:
    body_start = int(pending["body_start"])
    heading_path = tuple(str(item) for item in pending["heading_path"])
    ordinal = int(pending["ordinal"])
    heading = str(pending["heading"])
    section_id = (
        f"{document.path}::{' > '.join(heading_path)}::section-{ordinal}"
    )
    return DocSection(
        section_id=section_id,
        path=document.path,
        heading=heading,
        heading_path=heading_path,
        level=int(pending["level"]),
        body=document.text[body_start:body_end],
        body_start=body_start,
        body_end=body_end,
        newline=document.newline,
        ordinal=ordinal,
    )


def _contains_atx_heading_outside_fence(text: str) -> bool:
    fence_char: str | None = None
    fence_length = 0
    for line in text.splitlines(keepends=True):
        fence_match = _FENCE_RE.match(line)
        if fence_char is not None:
            if fence_match:
                marker = fence_match.group(1)
                if marker[0] == fence_char and len(marker) >= fence_length:
                    fence_char = None
                    fence_length = 0
            continue
        if fence_match:
            marker = fence_match.group(1)
            fence_char = marker[0]
            fence_length = len(marker)
            continue
        if _HEADING_RE.match(line):
            return True
    return False


def _leading_blank_lines(text: str) -> str:
    match = re.match(r"(?:(?:[ \t]*)(?:\r\n|\n|\r))+", text)
    return match.group(0) if match else ""


def _trailing_blank_lines(text: str) -> str:
    match = re.search(r"(?:(?:[ \t]*)(?:\r\n|\n|\r))+$", text)
    return match.group(0) if match else ""


def _validate_non_overlapping(replacements: list[PendingReplacement]) -> None:
    ordered = sorted(replacements, key=lambda item: item.section.body_start)
    seen_sections: set[str] = set()
    previous_end = -1
    for replacement in ordered:
        section = replacement.section
        if section.section_id in seen_sections:
            raise MarkdownSafetyError(
                f"duplicate replacement for section {section.section_id}"
            )
        if section.body_start < previous_end:
            raise MarkdownSafetyError(
                f"overlapping replacement for section {section.section_id}"
            )
        seen_sections.add(section.section_id)
        previous_end = section.body_end


def _safe_markdown_path(repo_root: Path, relative_path: str) -> Path:
    normalized = validate_relative_repo_path(relative_path)
    lexical = repo_root / Path(normalized)
    if lexical.is_symlink():
        raise MarkdownSafetyError(f"refusing Markdown symlink: {normalized}")
    resolved_root = repo_root.resolve()
    resolved = lexical.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise MarkdownSafetyError(
            f"Markdown path escapes repository root: {normalized}"
        ) from exc
    if resolved.suffix.lower() != ".md":
        raise MarkdownSafetyError(f"replacement target is not Markdown: {normalized}")
    if not resolved.is_file():
        raise GitError(f"tracked Markdown file is missing: {normalized}")
    return resolved
