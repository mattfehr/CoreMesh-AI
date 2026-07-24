"""Configuration loading and validation for documentation healing runs.

System role:
    Converts environment defaults and CLI paths into one explicit policy object
    shared by retrieval, OpenAI calls, safety gates, and reports.
Dependencies:
    Python environment variables, dataclasses, and pathlib.
Side effects:
    Reads environment variables only.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


class ConfigurationError(ValueError):
    """Raised when a workflow policy value is missing or unsafe."""


@dataclass(frozen=True)
class HealingConfig:
    """Validated runtime configuration for one base/head comparison."""

    repo_root: Path
    base_sha: str
    head_sha: str
    output_dir: Path
    apply: bool = False
    model: str = "gpt-5.6-luna"
    embedding_model: str = "text-embedding-3-small"
    reasoning_effort: str = "low"
    similarity_threshold: float = 0.45
    confidence_threshold: float = 0.90
    top_k: int = 5
    max_candidates: int = 20
    max_section_chars: int = 16_000

    @classmethod
    def from_environment(
        cls,
        *,
        repo_root: Path,
        base_sha: str,
        head_sha: str,
        output_dir: Path,
        apply: bool,
    ) -> "HealingConfig":
        """Create and validate configuration using documented environment names."""

        config = cls(
            repo_root=repo_root.resolve(),
            base_sha=base_sha.strip(),
            head_sha=head_sha.strip(),
            output_dir=output_dir.resolve(),
            apply=apply,
            model=os.getenv("DOC_HEALING_MODEL", "gpt-5.6-luna").strip(),
            embedding_model=os.getenv(
                "DOC_HEALING_EMBEDDING_MODEL", "text-embedding-3-small"
            ).strip(),
            reasoning_effort=os.getenv(
                "DOC_HEALING_REASONING_EFFORT", "low"
            ).strip(),
            similarity_threshold=_float_env(
                "DOC_HEALING_SIMILARITY_THRESHOLD", 0.45
            ),
            confidence_threshold=_float_env(
                "DOC_HEALING_CONFIDENCE_THRESHOLD", 0.90
            ),
            top_k=_int_env("DOC_HEALING_TOP_K", 5),
            max_candidates=_int_env("DOC_HEALING_MAX_CANDIDATES", 20),
            max_section_chars=_int_env("DOC_HEALING_MAX_SECTION_CHARS", 16_000),
        )
        config.validate()
        return config

    def validate(self) -> None:
        """Reject unsafe paths, empty identifiers, and out-of-range policy values."""

        if not self.repo_root.is_dir():
            raise ConfigurationError(f"repository root does not exist: {self.repo_root}")
        if not self.base_sha or not self.head_sha:
            raise ConfigurationError("base_sha and head_sha are required")
        if not self.model or not self.embedding_model:
            raise ConfigurationError("model and embedding model must be non-empty")
        if self.reasoning_effort not in {
            "none",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        }:
            raise ConfigurationError(
                "DOC_HEALING_REASONING_EFFORT must be one of "
                "none, low, medium, high, xhigh, or max"
            )
        if not 0.0 <= self.similarity_threshold <= 1.0:
            raise ConfigurationError("similarity threshold must be in [0, 1]")
        if not 0.0 <= self.confidence_threshold <= 1.0:
            raise ConfigurationError("confidence threshold must be in [0, 1]")
        if self.top_k < 1 or self.top_k > 20:
            raise ConfigurationError("top_k must be between 1 and 20")
        if self.max_candidates < self.top_k or self.max_candidates > 100:
            raise ConfigurationError(
                "max_candidates must be at least top_k and no greater than 100"
            )
        if self.max_section_chars < 1_000 or self.max_section_chars > 100_000:
            raise ConfigurationError(
                "max_section_chars must be between 1000 and 100000"
            )
        if self.output_dir == self.repo_root:
            raise ConfigurationError("output_dir must not be the repository root")

    def report_values(self) -> dict[str, Any]:
        """Return non-secret configuration values for report provenance."""

        values = asdict(self)
        values["repo_root"] = str(self.repo_root)
        values["output_dir"] = str(self.output_dir)
        return values


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
