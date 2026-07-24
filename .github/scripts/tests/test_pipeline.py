"""Offline integration coverage for Git-to-Markdown healing behavior."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from self_healing_docs.config import HealingConfig
from self_healing_docs.pipeline import HealingRunError, run_healing
from self_healing_docs.providers import (
    ProviderError,
    RepairProposal,
    RepairValidation,
    StalenessAssessment,
)


class UniformEmbeddings:
    """Make every small fixture semantically comparable without network I/O."""

    def embed(self, texts):
        return [[1.0, 0.25] for _ in texts]


class FixtureRepairer:
    """Update the timeout sentence and recognize an already-correct rerun."""

    def __init__(
        self,
        *,
        assessment_confidence: float = 0.99,
        validation_confidence: float = 0.99,
        style_consistent: bool = True,
    ) -> None:
        self.assessment_confidence = assessment_confidence
        self.validation_confidence = validation_confidence
        self.style_consistent = style_consistent

    def assess(self, *, changes, section):
        already_current = "float with a default of 15.0" in section.body
        contains_stale_fact = "integer with a default of 30" in section.body
        return StalenessAssessment(
            stale=contains_stale_fact and not already_current,
            confidence=self.assessment_confidence,
            complexity="bounded",
            diagnosis=(
                "The timeout type and default changed."
                if contains_stale_fact
                else "This section does not contain the affected timeout fact."
            ),
            affected_facts=(
                ["timeout type", "timeout default"] if contains_stale_fact else []
            ),
        )

    def propose(self, *, changes, section, assessment, neighboring_style):
        return RepairProposal(
            replacement_body=(
                "The `timeout` setting is a float with a default of 15.0 seconds."
            ),
            rationale="Align the documented type and default with Settings.",
        )

    def validate(
        self,
        *,
        changes,
        section,
        assessment,
        proposal,
        neighboring_style,
    ):
        return RepairValidation(
            accurate=True,
            preserves_unaffected_content=True,
            style_consistent=self.style_consistent,
            no_unverified_claims=True,
            confidence=self.validation_confidence,
            issues=[],
        )


class FailingEmbeddings:
    def embed(self, texts):
        raise ProviderError("synthetic provider outage")


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _fixture_repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "tests@example.com")
    _git(repo, "config", "user.name", "Self Healing Tests")
    (repo / "service.py").write_text(
        """
from pydantic import BaseModel

class Settings(BaseModel):
    timeout: int = 30
""".lstrip(),
        encoding="utf-8",
    )
    (repo / "README.md").write_text(
        "# Demo\n\n## Configuration\n\n"
        "The `timeout` setting is an integer with a default of 30 seconds.\n",
        encoding="utf-8",
    )
    _git(repo, "add", "service.py", "README.md")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")

    (repo / "service.py").write_text(
        """
from pydantic import BaseModel

class Settings(BaseModel):
    timeout: float = 15.0
""".lstrip(),
        encoding="utf-8",
    )
    _git(repo, "add", "service.py")
    _git(repo, "commit", "-m", "change timeout")
    head = _git(repo, "rev-parse", "HEAD")
    return repo, base, head


def _config(
    *,
    repo: Path,
    base: str,
    head: str,
    output: Path,
    apply: bool,
    max_candidates: int = 20,
    top_k: int = 5,
) -> HealingConfig:
    return HealingConfig(
        repo_root=repo,
        base_sha=base,
        head_sha=head,
        output_dir=output,
        apply=apply,
        max_candidates=max_candidates,
        top_k=top_k,
        max_section_chars=4_000,
    )


def _truncation_fixture_repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "truncation-repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "tests@example.com")
    _git(repo, "config", "user.name", "Self Healing Tests")
    (repo / "service.py").write_text(
        """
from pydantic import BaseModel

class Settings(BaseModel):
    alpha: int = 1
    beta: int = 2
    gamma: int = 3
    delta: int = 4
    epsilon: int = 5
""".lstrip(),
        encoding="utf-8",
    )
    (repo / "README.md").write_text(
        "# Demo\n\n## Settings\n\n"
        "Document alpha, beta, gamma, delta, and epsilon defaults.\n",
        encoding="utf-8",
    )
    _git(repo, "add", "service.py", "README.md")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")

    (repo / "service.py").write_text(
        """
from pydantic import BaseModel

class Settings(BaseModel):
    alpha: int = 10
    beta: int = 20
    gamma: int = 30
    delta: int = 40
    epsilon: int = 50
""".lstrip(),
        encoding="utf-8",
    )
    _git(repo, "add", "service.py")
    _git(repo, "commit", "-m", "change defaults")
    head = _git(repo, "rev-parse", "HEAD")
    return repo, base, head


def test_truncated_candidates_get_one_accurate_review_item(tmp_path: Path) -> None:
    repo, base, head = _truncation_fixture_repo(tmp_path)
    report = run_healing(
        _config(
            repo=repo,
            base=base,
            head=head,
            output=tmp_path / "output",
            apply=False,
            max_candidates=1,
            top_k=1,
        ),
        embedding_provider=UniformEmbeddings(),
        repair_provider=FixtureRepairer(),
    )

    truncated_reason = (
        "Candidate processing was truncated by DOC_HEALING_MAX_CANDIDATES"
    )
    no_match_reason = (
        "No Markdown section passed exact-reference or similarity retrieval"
    )
    truncated_items = [
        item for item in report.review_items if item["reason"] == truncated_reason
    ]
    assert truncated_items
    truncated_change_ids = {item["change_id"] for item in truncated_items}
    assert len(truncated_items) == len(truncated_change_ids)
    assert not any(
        item["change_id"] in truncated_change_ids and item["reason"] == no_match_reason
        for item in report.review_items
    )

def test_apply_edits_only_markdown_and_rerun_is_idempotent(tmp_path: Path) -> None:
    repo, base, head = _fixture_repo(tmp_path)
    report = run_healing(
        _config(
            repo=repo,
            base=base,
            head=head,
            output=tmp_path / "output-1",
            apply=True,
        ),
        embedding_provider=UniformEmbeddings(),
        repair_provider=FixtureRepairer(),
    )

    assert report.status == "repaired"
    assert report.changed_markdown_paths == ["README.md"]
    assert "float with a default of 15.0" in (repo / "README.md").read_text()
    assert _git(repo, "status", "--porcelain") == "M README.md"
    artifact = json.loads((tmp_path / "output-1" / "report.json").read_text())
    assert artifact["metrics"]["applied_repairs"] == 1
    assert [item["stage"] for item in artifact["llm_decisions"]] == [
        "staleness_assessment",
        "targeted_rewrite",
        "correction_validation",
        "staleness_assessment",
    ]
    assert (tmp_path / "output-1" / "changes.patch").read_text()
    summary = (tmp_path / "output-1" / "summary.md").read_text()
    assert "### Verified accurate" in summary
    assert "### Applied" in summary

    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "heal docs")
    healed_head = _git(repo, "rev-parse", "HEAD")
    rerun = run_healing(
        _config(
            repo=repo,
            base=base,
            head=healed_head,
            output=tmp_path / "output-2",
            apply=True,
        ),
        embedding_provider=UniformEmbeddings(),
        repair_provider=FixtureRepairer(),
    )
    assert rerun.status == "accurate"
    assert rerun.changed_markdown_paths == []
    assert _git(repo, "status", "--porcelain") == ""


def test_low_confidence_is_advisory_and_does_not_mutate(tmp_path: Path) -> None:
    repo, base, head = _fixture_repo(tmp_path)
    report = run_healing(
        _config(
            repo=repo,
            base=base,
            head=head,
            output=tmp_path / "output",
            apply=True,
        ),
        embedding_provider=UniformEmbeddings(),
        repair_provider=FixtureRepairer(validation_confidence=0.50),
    )

    assert report.status == "review_required"
    assert report.applied_repairs == []
    assert report.review_items
    assert _git(repo, "status", "--porcelain") == ""


@pytest.mark.parametrize(
    "repairer",
    [
        FixtureRepairer(assessment_confidence=0.50),
        FixtureRepairer(style_consistent=False),
    ],
)
def test_assessment_or_validation_rejection_is_advisory(
    tmp_path: Path,
    repairer: FixtureRepairer,
) -> None:
    repo, base, head = _fixture_repo(tmp_path)
    report = run_healing(
        _config(
            repo=repo,
            base=base,
            head=head,
            output=tmp_path / "output",
            apply=True,
        ),
        embedding_provider=UniformEmbeddings(),
        repair_provider=repairer,
    )

    assert report.status == "review_required"
    assert report.applied_repairs == []
    assert report.review_items
    assert _git(repo, "status", "--porcelain") == ""


def test_provider_failure_writes_diagnostics_and_fails_without_mutation(
    tmp_path: Path,
) -> None:
    repo, base, head = _fixture_repo(tmp_path)
    output = tmp_path / "output"
    with pytest.raises(HealingRunError, match="synthetic provider outage"):
        run_healing(
            _config(
                repo=repo,
                base=base,
                head=head,
                output=output,
                apply=True,
            ),
            embedding_provider=FailingEmbeddings(),
            repair_provider=FixtureRepairer(),
        )

    artifact = json.loads((output / "report.json").read_text())
    assert artifact["status"] == "failed"
    assert artifact["errors"][0]["type"] == "ProviderError"
    assert _git(repo, "status", "--porcelain") == ""


def test_dry_run_generates_decision_without_worktree_change(tmp_path: Path) -> None:
    repo, base, head = _fixture_repo(tmp_path)
    report = run_healing(
        _config(
            repo=repo,
            base=base,
            head=head,
            output=tmp_path / "output",
            apply=False,
        ),
        embedding_provider=UniformEmbeddings(),
        repair_provider=FixtureRepairer(),
    )

    assert report.status == "dry_run"
    assert report.proposed_repairs[0]["disposition"] == "would_apply"
    assert report.applied_repairs == []
    assert _git(repo, "status", "--porcelain") == ""
