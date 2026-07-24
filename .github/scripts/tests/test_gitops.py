"""Unit coverage for source filters and post-run Git allowlisting."""
from __future__ import annotations

import pytest

from self_healing_docs.gitops import (
    GitError,
    is_test_or_generated_source,
    source_language,
    validate_relative_repo_path,
)


def test_source_language_includes_supported_repository_contracts() -> None:
    assert source_language("services-runtime/src/main.py") == "python"
    assert source_language("gateway-proxy/cmd/main.go") == "go"
    assert source_language("docker-compose.yml") == "compose"
    assert source_language("README.md") is None


def test_tests_generated_and_dependency_trees_are_excluded() -> None:
    assert is_test_or_generated_source("services-runtime/tests/test_main.py")
    assert is_test_or_generated_source("gateway-proxy/internal/proxy_test.go")
    assert is_test_or_generated_source("vendor/example.go")
    assert not is_test_or_generated_source("services-runtime/src/main.py")


def test_relative_path_validation_normalizes_separators() -> None:
    assert validate_relative_repo_path(r"services-runtime\README.md") == (
        "services-runtime/README.md"
    )


@pytest.mark.parametrize(
    "path",
    ["../README.md", "docs/../../README.md", "/absolute.md", "", "bad\0name.md"],
)
def test_relative_path_validation_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(GitError):
        validate_relative_repo_path(path)
