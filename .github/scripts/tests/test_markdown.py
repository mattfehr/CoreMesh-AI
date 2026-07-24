"""Unit coverage for Markdown retrieval blocks and mutation safety."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from self_healing_docs.markdown import (
    MarkdownSafetyError,
    apply_markdown_replacements,
    heading_signature,
    is_generated_markdown,
    normalize_replacement_body,
    split_markdown_document,
)
from self_healing_docs.models import MarkdownDocument, PendingReplacement


def _replacement(section, body: str) -> PendingReplacement:
    return PendingReplacement(
        section=section,
        replacement_body=body,
        change_ids=("change-1",),
        assessment_confidence=0.99,
        validation_confidence=0.99,
        diagnosis="The parameter changed.",
    )


def test_split_uses_non_overlapping_headings_and_ignores_fenced_hashes() -> None:
    text = """# Title

Intro.

## API

```text
# not a heading
```

Details.

## API

Second block.
"""
    document = MarkdownDocument(path="README.md", text=text, newline="\n")
    sections = split_markdown_document(document)

    assert [section.heading for section in sections] == ["Title", "API", "API"]
    assert sections[1].heading_path == ("Title", "API")
    assert sections[1].body_end == sections[2].body_start - len("## API\n")
    assert "# not a heading" in sections[1].body
    assert len({section.section_id for section in sections}) == 3


@pytest.mark.parametrize(
    "path",
    [
        "analytics-workers/report.md",
        "analytics-workers/smoke-comparison.md",
        "analytics-workers/artifacts/regression/report.md",
        ".venv-docs/README.md",
        "vendor/example/README.md",
    ],
)
def test_generated_dependency_and_virtualenv_markdown_is_excluded(path: str) -> None:
    assert is_generated_markdown(path)


def test_replacement_preserves_crlf_framing_and_rejects_headings() -> None:
    text = "# Title\r\n\r\nOld body.\r\n\r\n"
    section = split_markdown_document(
        MarkdownDocument(path="README.md", text=text, newline="\r\n")
    )[0]

    normalized = normalize_replacement_body(
        section=section,
        proposed_body="New body.",
        max_chars=1_000,
    )
    assert normalized == "\r\nNew body.\r\n\r\n"

    with pytest.raises(MarkdownSafetyError, match="headings"):
        normalize_replacement_body(
            section=section,
            proposed_body="## Injected\n\nBad.",
            max_chars=1_000,
        )


def test_apply_changes_only_selected_body_and_preserves_headings(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    path = repo / "README.md"
    original = "# Title\n\nIntro.\n\n## API\n\nOld timeout is 30.\n\n## Other\n\nKeep.\n"
    path.write_text(original, encoding="utf-8", newline="")
    document = MarkdownDocument(path="README.md", text=original, newline="\n")
    sections = split_markdown_document(document)
    api = next(section for section in sections if section.heading == "API")
    body = normalize_replacement_body(
        section=api,
        proposed_body="New timeout is 15.0.",
        max_chars=1_000,
    )

    changed = apply_markdown_replacements(
        repo_root=repo,
        documents={"README.md": document},
        replacements=[_replacement(api, body)],
    )

    updated = path.read_text(encoding="utf-8")
    assert changed == ["README.md"]
    assert "New timeout is 15.0." in updated
    assert "## Other\n\nKeep." in updated
    assert heading_signature(
        split_markdown_document(
            MarkdownDocument(path="README.md", text=updated, newline="\n")
        )
    ) == heading_signature(sections)


def test_apply_preserves_crlf_and_every_byte_outside_selected_body(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    path = repo / "README.md"
    original = (
        "# Title\r\n\r\nIntro.\r\n\r\n"
        "## API\r\n\r\nOld.\r\n\r\n"
        "## Other\r\n\r\nUntouched.\r\n"
    )
    path.write_bytes(original.encode("utf-8"))
    document = MarkdownDocument(path="README.md", text=original, newline="\r\n")
    section = next(
        item for item in split_markdown_document(document) if item.heading == "API"
    )
    replacement = normalize_replacement_body(
        section=section,
        proposed_body="New.",
        max_chars=1_000,
    )

    apply_markdown_replacements(
        repo_root=repo,
        documents={"README.md": document},
        replacements=[_replacement(section, replacement)],
    )

    expected = original[: section.body_start] + replacement + original[section.body_end :]
    assert path.read_bytes() == expected.encode("utf-8")
    assert b"\n" not in path.read_bytes().replace(b"\r\n", b"")


def test_duplicate_and_overlapping_replacements_are_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    text = "# Title\n\nBody.\n"
    (repo / "README.md").write_text(text, encoding="utf-8")
    document = MarkdownDocument(path="README.md", text=text, newline="\n")
    section = split_markdown_document(document)[0]
    replacement = _replacement(section, "\nNew.\n")

    with pytest.raises(MarkdownSafetyError, match="duplicate"):
        apply_markdown_replacements(
            repo_root=repo,
            documents={"README.md": document},
            replacements=[replacement, replacement],
        )


def test_symlink_target_is_rejected_when_supported(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "target.md"
    target.write_text("# Target\n", encoding="utf-8")
    link = repo / "README.md"
    try:
        os.symlink(target, link)
    except OSError:
        pytest.skip("symlinks are unavailable in this Windows test environment")
    document = MarkdownDocument(path="README.md", text="# Link\n\nBody.\n", newline="\n")
    section = split_markdown_document(document)[0]
    with pytest.raises(MarkdownSafetyError, match="symlink"):
        apply_markdown_replacements(
            repo_root=repo,
            documents={"README.md": document},
            replacements=[_replacement(section, "\nNew.\n")],
        )
