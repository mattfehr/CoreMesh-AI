"""Environment-backed configuration for offline analytics workers.

System role:
    Centralizes database, model, clustering, retention, and scheduler settings
    for the separately packaged Phase 4.1 process.
Dependencies:
    pydantic-settings reads environment variables and analytics-workers/.env.
Side effects:
    Module import reads configuration only; it opens no network connections.
"""
from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Validated production log-miner settings with local defaults."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    postgres_dsn: str = "postgresql://coremesh:coremesh_secret@localhost:5432/coremesh"
    openai_api_key: str = ""
    log_miner_embedding_model: str = "text-embedding-3-small"
    log_miner_reference_model: str = "gpt-4o"
    log_miner_embedding_batch_size: int = Field(default=128, ge=1, le=2048)
    log_miner_provider_retry_attempts: int = Field(default=3, ge=1, le=10)
    log_miner_window_days: int = Field(default=30, ge=1, le=365)
    log_miner_retention_days: int = Field(default=30, ge=1, le=365)
    log_miner_score_threshold: Literal[4] = 4
    log_miner_min_cluster_size: int = Field(default=3, ge=2)
    log_miner_min_samples: int = Field(default=2, ge=1)
    log_miner_cluster_metric: str = "euclidean"
    log_miner_cluster_selection_method: str = "eom"
    log_miner_max_noise_per_feature: int = Field(default=20, ge=0, le=1000)
    log_miner_max_cluster_examples: int = Field(default=3, ge=0, le=20)
    log_miner_promotion_confidence: float = Field(default=0.80, ge=0.0, le=1.0)
    log_miner_fingerprint_version: str = "1.0"
    log_miner_lease_ttl_seconds: int = Field(default=300, ge=30, le=86_400)
    log_miner_heartbeat_interval_seconds: int = Field(default=60, ge=1, le=28_800)
    log_miner_cron: str = "0 2 * * *"
    log_miner_timezone: str = "UTC"

    @field_validator(
        "log_miner_embedding_model",
        "log_miner_reference_model",
        "log_miner_cluster_metric",
        "log_miner_cluster_selection_method",
        "log_miner_fingerprint_version",
        "log_miner_cron",
        "log_miner_timezone",
    )
    @classmethod
    def non_empty(cls, value: str) -> str:
        """Reject whitespace-only provider, algorithm, and schedule values."""

        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @model_validator(mode="after")
    def heartbeat_precedes_lease_expiry(self) -> "Settings":
        """Leave enough time for at least two renewals before lease expiry."""

        if self.log_miner_heartbeat_interval_seconds * 2 >= self.log_miner_lease_ttl_seconds:
            raise ValueError(
                "LOG_MINER_HEARTBEAT_INTERVAL_SECONDS must be less than half "
                "LOG_MINER_LEASE_TTL_SECONDS"
            )
        return self

    @property
    def openai_available(self) -> bool:
        """Return whether production provider construction is allowed."""

        return bool(self.openai_api_key.strip())


settings = Settings()
