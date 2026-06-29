from typing import Any, Dict, List

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# [Project 14] Structured Document Target Extraction Contract
# Defined verbatim in plan/coremesh.txt — Data Contracts section.
# ---------------------------------------------------------------------------
class ExtractionTargetSchema(BaseModel):
    vendor_name: str = Field(description="Normalized corporate legal entity name.")
    invoice_id: str = Field(description="Unique identity token extracted from document metadata header.")
    line_items: List[Dict[str, Any]] = Field(description="Array listing units, description parameters, totals.")
    calculated_tax: float = Field(description="Extracted aggregate processing fees or transactional taxes.")
    invoice_total: float = Field(description="Total absolute cost validation metric.")


class ValidationResult(BaseModel):
    passed: bool
    computed_sum: float = Field(description="Sum of all line item totals plus calculated_tax.")
    delta: float = Field(description="Absolute difference between computed_sum and invoice_total.")
    tolerance: float = Field(description="Acceptance tolerance used for the comparison.")


class IngestResponse(BaseModel):
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
