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

    # Hybrid retrieval (RRF + reranker) tuning
    rag_dense_weight: float = 1.0
    rag_sparse_weight: float = 1.0
    rag_keyword_priority: bool = True

    @property
    def llm_available(self) -> bool:
        return bool(self.openai_api_key)


settings = Settings()
