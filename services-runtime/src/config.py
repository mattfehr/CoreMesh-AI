from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
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

    # OCR
    ocr_variance_threshold: float = 0.08
    tesseract_cmd: str = ""
    poppler_path: Optional[str] = None

    # Infrastructure
    postgres_dsn: str = "postgresql://coremesh:coremesh_secret@localhost:5432/coremesh"
    redis_url: str = "redis://localhost:6379"
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "coremesh_chunks"
    qdrant_vector_size: int = 1536
    reranker_model: str = "BAAI/bge-reranker-large"
    chroma_persist_directory: str = ".chroma/coremesh-agents"
    chroma_collection: str = "coremesh_agent_memory"
    agent_memory_ttl_seconds: int = 3600

    # Hybrid retrieval (RRF + reranker) tuning
    rag_dense_weight: float = 1.0
    rag_sparse_weight: float = 1.0
    rag_keyword_priority: bool = True

    @property
    def llm_available(self) -> bool:
        return bool(self.openai_api_key)


settings = Settings()
