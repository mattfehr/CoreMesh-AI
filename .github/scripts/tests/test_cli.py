"""Command-line integration coverage using temporary Git repositories."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from self_healing_docs.__main__ import main


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _clean_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "tests@example.com")
    _git(repo, "config", "user.name", "Self Healing CLI Tests")
    (repo / "README.md").write_text("# Demo\n\nCurrent.\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base")
    return repo


def test_cli_dry_run_and_apply_resolve_git_revisions_without_provider(
    tmp_path: Path,
) -> None:
    repo = _clean_repo(tmp_path)
    dry_output = tmp_path / "dry"
    apply_output = tmp_path / "apply"

    assert main(
        [
            "--repo-root",
            str(repo),
            "--base-sha",
            "HEAD",
            "--head-sha",
            "HEAD",
            "--output-dir",
            str(dry_output),
        ]
    ) == 0
    dry_report = json.loads((dry_output / "report.json").read_text())
    assert dry_report["status"] == "no_structural_changes"
    assert dry_report["apply_requested"] is False
    assert len(dry_report["base_sha"]) == 40

    assert main(
        [
            "--repo-root",
            str(repo),
            "--base-sha",
            "HEAD",
            "--head-sha",
            "HEAD",
            "--output-dir",
            str(apply_output),
            "--apply",
        ]
    ) == 0
    apply_report = json.loads((apply_output / "report.json").read_text())
    assert apply_report["apply_requested"] is True
    assert _git(repo, "status", "--porcelain") == ""


def test_cli_invalid_revision_fails_with_diagnostic_report(tmp_path: Path) -> None:
    repo = _clean_repo(tmp_path)
    output = tmp_path / "invalid"

    assert main(
        [
            "--repo-root",
            str(repo),
            "--base-sha",
            "missing-revision",
            "--head-sha",
            "HEAD",
            "--output-dir",
            str(output),
        ]
    ) == 1
    report = json.loads((output / "report.json").read_text())
    assert report["status"] == "failed"
    assert report["errors"][0]["type"] == "GitError"
