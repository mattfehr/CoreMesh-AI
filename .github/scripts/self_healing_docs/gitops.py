"""Safe Git discovery and worktree validation for documentation healing.

System role:
    Reads base/head source snapshots without switching revisions and enforces
    the worktree allowlist before any workflow commit can be created.
Dependencies:
    The Git command-line client and a local repository checkout.
Side effects:
    Read helpers spawn Git subprocesses; validation does not stage, commit, or
    push files.
"""
from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath

from .models import SourceFileDelta


class GitError(RuntimeError):
    """Raised when repository state is invalid or a Git command fails."""


_COMPOSE_NAMES = {
    "compose.yml",
    "compose.yaml",
    "docker-compose.yml",
    "docker-compose.yaml",
}


def run_git(
    repo_root: Path,
    *args: str,
    check: bool = True,
    text: bool = True,
) -> str | bytes:
    """Run one non-interactive Git command with argument-safe subprocess APIs."""

    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=text,
    )
    if check and result.returncode != 0:
        stderr = result.stderr.strip() if text else result.stderr.decode(errors="replace")
        raise GitError(f"git {' '.join(args)} failed: {stderr}")
    return result.stdout


def resolve_commit(repo_root: Path, revision: str) -> str:
    """Resolve a requested revision to one full commit SHA."""

    output = run_git(repo_root, "rev-parse", "--verify", f"{revision}^{{commit}}")
    assert isinstance(output, str)
    return output.strip()


def require_head_checkout(repo_root: Path, head_sha: str) -> str:
    """Require apply mode to run from the exact candidate commit."""

    current = resolve_commit(repo_root, "HEAD")
    requested = resolve_commit(repo_root, head_sha)
    if current != requested:
        raise GitError(
            f"apply mode requires HEAD {requested}, but the worktree is at {current}"
        )
    return requested


def require_clean_worktree(repo_root: Path) -> None:
    """Reject pre-existing tracked or untracked changes before mutation."""

    output = run_git(repo_root, "status", "--porcelain", "--untracked-files=all")
    assert isinstance(output, str)
    if output.strip():
        raise GitError(
            "apply mode requires a clean worktree before documentation generation"
        )


def discover_source_deltas(
    repo_root: Path,
    *,
    base_sha: str,
    head_sha: str,
) -> list[SourceFileDelta]:
    """Load documentation-relevant source snapshots changed between two commits."""

    base = resolve_commit(repo_root, base_sha)
    head = resolve_commit(repo_root, head_sha)
    output = run_git(repo_root, "diff", "--name-status", "-M", base, head, "--")
    assert isinstance(output, str)

    deltas: list[SourceFileDelta] = []
    for raw_line in output.splitlines():
        if not raw_line:
            continue
        fields = raw_line.split("\t")
        status = fields[0]
        old_path: str | None
        new_path: str | None
        if status.startswith(("R", "C")):
            if len(fields) != 3:
                raise GitError(f"unexpected rename/copy record: {raw_line!r}")
            old_path, new_path = fields[1], fields[2]
        else:
            if len(fields) != 2:
                raise GitError(f"unexpected name-status record: {raw_line!r}")
            path = fields[1]
            old_path = None if status.startswith("A") else path
            new_path = None if status.startswith("D") else path

        selected_path = new_path or old_path
        if selected_path is None:
            continue
        language = source_language(selected_path)
        if language is None or is_test_or_generated_source(selected_path):
            continue

        deltas.append(
            SourceFileDelta(
                status=status,
                old_path=old_path,
                new_path=new_path,
                old_text=(
                    read_text_at_ref(repo_root, base, old_path)
                    if old_path is not None
                    else None
                ),
                new_text=(
                    read_text_at_ref(repo_root, head, new_path)
                    if new_path is not None
                    else None
                ),
                language=language,
            )
        )
    return deltas


def source_language(path: str) -> str | None:
    """Map a repository path to a supported structural parser."""

    normalized = PurePosixPath(path.replace("\\", "/"))
    if normalized.suffix == ".py":
        return "python"
    if normalized.suffix == ".go":
        return "go"
    if normalized.name in _COMPOSE_NAMES:
        return "compose"
    return None


def is_test_or_generated_source(path: str) -> bool:
    """Return whether a source path is intentionally outside documentation CI."""

    normalized = path.replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    name = PurePosixPath(normalized).name
    if any(
        part in {
            ".git",
            ".venv",
            "node_modules",
            "vendor",
            "__pycache__",
            ".pytest_cache",
            "artifacts",
        }
        or part.startswith(".venv-")
        for part in parts
    ):
        return True
    return (
        "tests" in parts
        or name.startswith("test_")
        or name.endswith("_test.go")
        or name.endswith(".generated.go")
    )


def read_text_at_ref(repo_root: Path, revision: str, path: str) -> str:
    """Read one UTF-8 repository file from a commit without checkout mutation."""

    normalized = validate_relative_repo_path(path)
    output = run_git(
        repo_root,
        "show",
        f"{revision}:{normalized}",
        text=False,
    )
    assert isinstance(output, bytes)
    try:
        return output.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GitError(f"source file is not UTF-8: {normalized}") from exc


def tracked_markdown_paths(repo_root: Path) -> list[str]:
    """Return safe tracked Markdown paths in deterministic order."""

    output = run_git(repo_root, "ls-files", "-z", "--", "*.md", text=False)
    assert isinstance(output, bytes)
    paths: list[str] = []
    for raw in output.split(b"\0"):
        if not raw:
            continue
        try:
            path = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GitError("tracked Markdown path is not UTF-8") from exc
        normalized = validate_relative_repo_path(path)
        absolute = (repo_root / Path(normalized)).resolve()
        if not _is_within(absolute, repo_root.resolve()):
            raise GitError(f"tracked Markdown escapes repository root: {normalized}")
        lexical = repo_root / Path(normalized)
        if lexical.is_symlink():
            continue
        if absolute.is_file():
            paths.append(normalized)
    return sorted(paths)


def validate_relative_repo_path(path: str) -> str:
    """Reject absolute, parent-traversing, empty, and NUL-bearing paths."""

    if not path or "\0" in path:
        raise GitError("repository path must be non-empty and NUL-free")
    normalized = PurePosixPath(path.replace("\\", "/"))
    if normalized.is_absolute() or ".." in normalized.parts:
        raise GitError(f"unsafe repository path: {path!r}")
    return normalized.as_posix()


def validate_markdown_worktree_changes(
    repo_root: Path,
    expected_paths: set[str],
) -> list[str]:
    """Require every post-run worktree change to be an expected Markdown edit."""

    normalized_expected = {validate_relative_repo_path(path) for path in expected_paths}
    output = run_git(repo_root, "status", "--porcelain", "--untracked-files=all")
    assert isinstance(output, str)
    actual: list[str] = []
    for line in output.splitlines():
        if len(line) < 4:
            raise GitError(f"unexpected porcelain status line: {line!r}")
        status = line[:2]
        path = line[3:]
        if " -> " in path or status == "??":
            raise GitError(f"unexpected renamed or untracked path after healing: {path}")
        normalized = validate_relative_repo_path(path)
        if normalized not in normalized_expected:
            raise GitError(f"unexpected worktree change after healing: {normalized}")
        if not normalized.lower().endswith(".md"):
            raise GitError(f"non-Markdown worktree change after healing: {normalized}")
        if "D" in status:
            raise GitError(f"documentation healing must not delete files: {normalized}")
        actual.append(normalized)

    missing = normalized_expected - set(actual)
    if missing:
        raise GitError(
            "reported Markdown changes are absent from the worktree: "
            + ", ".join(sorted(missing))
        )
    return sorted(actual)


def markdown_patch(repo_root: Path, paths: list[str]) -> str:
    """Return the unstaged patch for explicitly approved Markdown paths."""

    if not paths:
        return ""
    output = run_git(repo_root, "diff", "--", *paths)
    assert isinstance(output, str)
    return output


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
