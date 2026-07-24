"""Static contract tests for the guarded GitHub Actions workflow."""
from __future__ import annotations

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "self-healing-docs.yml"


def _workflow() -> tuple[str, dict]:
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    parsed = yaml.load(text, Loader=yaml.BaseLoader)
    assert isinstance(parsed, dict)
    return text, parsed


def test_workflow_has_code_only_pr_trigger_and_cancellation() -> None:
    text, workflow = _workflow()

    assert "pull_request_target" not in text
    trigger = workflow["on"]["pull_request"]
    assert trigger["types"] == ["opened", "synchronize", "reopened", "ready_for_review"]
    paths = set(trigger["paths"])
    assert "**/*.py" in paths
    assert "**/*.go" in paths
    assert "docker-compose.yml" in paths
    assert ".github/scripts/**" in paths
    assert "**/*.md" not in paths
    assert workflow["concurrency"]["cancel-in-progress"] == "true"


def test_untrusted_job_has_no_checkout_secret_or_write_permission() -> None:
    text, workflow = _workflow()
    job = workflow["jobs"]["untrusted-pr-notice"]

    assert "head.repo.full_name != github.repository" in job["if"]
    assert "pull_request.user.login == 'dependabot[bot]'" in job["if"]
    assert "dependabot[bot]" in job["if"]
    assert job["permissions"] == {"contents": "read"}
    assert all("uses" not in step for step in job["steps"])
    assert "no code was checked out" in text


def test_trusted_job_tests_before_live_calls_and_has_bounded_permissions() -> None:
    text, workflow = _workflow()
    job = workflow["jobs"]["heal-docs"]

    assert "pull_request.user.login != 'dependabot[bot]'" in job["if"]
    assert job["permissions"] == {
        "contents": "write",
        "pull-requests": "write",
    }
    assert job["timeout-minutes"] == "20"
    steps = job["steps"]
    names = [step["name"] for step in steps]
    assert names.index("Run offline unit and integration tests") < names.index(
        "Analyze and repair documentation"
    )
    checkout = next(step for step in steps if step["name"].startswith("Check out"))
    assert checkout["with"]["fetch-depth"] == "0"
    assert "pull_request.head.ref" in checkout["with"]["ref"]
    setup = next(step for step in steps if step["name"] == "Set up Python")
    assert setup["with"]["cache"] == "pip"
    analyze = next(step for step in steps if step["name"] == "Analyze and repair documentation")
    assert "OPENAI_API_KEY" in analyze["env"]
    assert "--apply" in analyze["run"]
    assert "OPENAI_API_KEY" not in job["env"]
    assert "git push origin \"HEAD:${HEAD_REF}\"" in text
    assert "--force" not in text


def test_workflow_always_reports_and_stages_only_reported_markdown() -> None:
    text, workflow = _workflow()
    steps = workflow["jobs"]["heal-docs"]["steps"]
    artifact = next(step for step in steps if step["name"].startswith("Upload "))
    comment = next(step for step in steps if step["name"] == "Update the pull-request report")
    commit = next(step for step in steps if step.get("id") == "commit")

    assert artifact["if"] == "always()"
    assert artifact["with"]["if-no-files-found"] == "error"
    assert comment["if"] == "always()"
    assert "<!-- coremesh-self-healing-docs -->" in text
    assert 'git add -- "$path"' in commit["run"]
    assert "git diff --cached --check" in commit["run"]
    assert "applied-paths.txt" in commit["run"]
    assert "docs: synchronize documentation for PR #${PR_NUMBER}" in commit["run"]
