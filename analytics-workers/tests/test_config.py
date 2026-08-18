"""Environment parsing contracts for the analytics worker."""

from src.config import Settings


def test_score_threshold_accepts_compose_string(monkeypatch):
    """Compose environment values are strings even for numeric settings."""

    monkeypatch.setenv("LOG_MINER_SCORE_THRESHOLD", "4")

    worker_settings = Settings(_env_file=None)

    assert worker_settings.log_miner_score_threshold == 4
