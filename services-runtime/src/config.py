"""Typed configuration for every Python runtime subsystem.

System role:
    Centralizes environment names/defaults shared by HTTP ingestion and the
    library-only retrieval, SQL, agent-memory, and arbitration paths.
Dependencies:
    pydantic-settings reads process variables and services-runtime/.env.
Side effects:
    The module-level settings instance reads and validates configuration during
    import; it performs no network connection or persistent write.
"""

from typing import Literal, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-backed runtime settings with local-development defaults."""
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # OpenAI
    openai_api_key: str = ""
    openai_extraction_model: str = "gpt-4o-mini"
    openai_vision_model: str = "gpt-4o"
    openai_embedding_model: str = "text-embedding-3-small"
    openai_arbitration_model: str = "gpt-4o-mini"
    openai_adjudicator_model: str = "gpt-4o-mini"

    # Anthropic / local model arbitration
    anthropic_api_key: str = ""
    anthropic_arbitration_model: str = "claude-3-5-sonnet-latest"
    ollama_base_url: str = "http://localhost:11434"
    ollama_arbitration_model: str = "llama3.1"
    arbitration_score_threshold: int = 4
    arbitration_retry_attempts: int = 2
    arbitration_timeout_seconds: float = 30.0
    arbitration_mode: Literal["external", "deterministic"] = "external"

    # OCR
    ocr_variance_threshold: float = 0.08
    ocr_easyocr_enabled: bool = True
    tesseract_cmd: str = ""
    poppler_path: Optional[str] = None

    # Infrastructure
    postgres_dsn: str = "postgresql://coremesh:coremesh_secret@localhost:5432/coremesh"
    production_interaction_logging_enabled: bool = False
    production_log_redaction_patterns: list[str] = Field(default_factory=list)
    production_log_connect_timeout_seconds: int = Field(default=3, ge=1, le=60)
    production_log_statement_timeout_ms: int = Field(default=3_000, ge=1, le=60_000)
    redis_url: str = "redis://localhost:6379"
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "coremesh_chunks"
    qdrant_vector_size: int = 1536
    reranker_model: str = "BAAI/bge-reranker-large"
    chroma_persist_directory: str = ".chroma/coremesh-agents"
    chroma_collection: str = "coremesh_agent_memory"
    agent_memory_ttl_seconds: int = 3600

    # Failure-forensics tracing
    forensics_enabled: bool = True
    forensics_trace_directory: str = ".traces"
    forensics_registry_path: str = ".traces/registry.sqlite3"
    forensics_confidence_threshold: float = 0.60
    forensics_confidence_drop_threshold: float = 0.20
    forensics_max_attribute_length: int = 256

    # Hybrid retrieval (RRF + reranker) tuning
    rag_embedding_provider: Literal["openai", "hash"] = "openai"
    rag_reranker_provider: Literal["cross_encoder", "lexical"] = "cross_encoder"
    rag_dense_weight: float = 1.0
    rag_sparse_weight: float = 1.0
    rag_keyword_priority: bool = True

    @property
    def llm_available(self) -> bool:
        """Return whether OpenAI-backed ingestion paths should be selected."""
        return bool(self.openai_api_key)


# Import-time construction gives all modules one immutable-by-convention view
# of process configuration. Reload the process after environment changes.
settings = Settings()
