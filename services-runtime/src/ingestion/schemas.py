"""Wire contracts for document ingestion and invoice validation.

System role:
    Defines the stable typed boundary shared by extraction, validation,
    processing, FastAPI response serialization, agents, and callers.
Dependencies:
    Pydantic validates and serializes these models; field descriptions also
    feed the generated OpenAPI schema and Instructor structured output.
Side effects:
    Model validation can raise validation errors; importing this module has no
    I/O, network, logging, or persistence side effects.
"""

from typing import Any, Dict, List

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# [Project 14] Structured Document Target Extraction Contract
# Defined verbatim in plan/coremesh.txt — Data Contracts section.
# ---------------------------------------------------------------------------
class ExtractionTargetSchema(BaseModel):
    """Normalized invoice fields emitted by either extraction strategy."""
    vendor_name: str = Field(description="Normalized corporate legal entity name.")
    invoice_id: str = Field(description="Unique identity token extracted from document metadata header.")
    line_items: List[Dict[str, Any]] = Field(description="Array listing units, description parameters, totals.")
    calculated_tax: float = Field(description="Extracted aggregate processing fees or transactional taxes.")
    invoice_total: float = Field(description="Total absolute cost validation metric.")


class ValidationResult(BaseModel):
    """Arithmetic consistency result for one extracted invoice."""
    passed: bool
    computed_sum: float = Field(description="Sum of all line item totals plus calculated_tax.")
    delta: float = Field(description="Absolute difference between computed_sum and invoice_total.")
    tolerance: float = Field(description="Acceptance tolerance used for the comparison.")


class RAGIndexResult(BaseModel):
    """Stable identity and page-chunk count for an indexed document."""

    document_id: str = Field(
        description="Lowercase SHA-256 digest of the original uploaded bytes."
    )
    chunk_count: int = Field(
        ge=1,
        description="Number of non-empty page-level chunks upserted for retrieval.",
    )


class IngestResponse(BaseModel):
    """Complete public response including data, provenance, and timing."""
    extraction: ExtractionTargetSchema
    ocr_engine_used: str = Field(
        description="Primary text source: 'tesseract', 'easyocr', or 'vision_llm'."
    )
    ocr_variance: float = Field(
        description="Normalised edit-distance variance between the two OCR engines (0.0–1.0)."
    )
    vision_fallback_used: bool
    llm_extraction_used: bool = Field(
        description="True when instructor/LLM produced the extraction; False when the regex parser was used."
    )
    validation: ValidationResult
    processing_time_ms: float
    page_count: int
    rag_index: RAGIndexResult | None = Field(
        default=None,
        description="Present only when opt-in hybrid-RAG indexing succeeds.",
    )
