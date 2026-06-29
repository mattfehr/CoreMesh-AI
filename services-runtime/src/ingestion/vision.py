"""Vision-LLM text extraction fallback (GPT-4o via instructor).

Called when the dual-OCR variance exceeds the configured threshold,
signalling that traditional OCR is struggling with the page quality.
The vision model receives the raw page image and returns a structured
text extraction that preserves the document's layout.
"""
import base64
import logging
from io import BytesIO

import numpy as np
from PIL import Image
from pydantic import BaseModel, Field

from src.config import settings

log = logging.getLogger(__name__)


class _ExtractedPageText(BaseModel):
    raw_text: str = Field(
        description=(
            "All text visible in the document image, preserving the original structure: "
            "line breaks, indentation, table rows, and column alignment."
        )
    )


def extract_text_via_vision(page_image: np.ndarray) -> str:
    """Send *page_image* to the GPT-4o vision model and return raw extracted text.

    Uses ``instructor`` to enforce the ``_ExtractedPageText`` Pydantic schema so
    that the model always returns a clean ``raw_text`` string rather than
    conversational commentary.

    Raises ``RuntimeError`` if ``OPENAI_API_KEY`` is not configured.
    """
    if not settings.llm_available:
        raise RuntimeError(
            "Vision LLM fallback invoked but OPENAI_API_KEY is not set. "
            "Set the key in .env or export it as an environment variable."
        )

    import instructor  # noqa: PLC0415
    from openai import OpenAI  # noqa: PLC0415

    pil_img = Image.fromarray(page_image)
    buf = BytesIO()
    pil_img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

    client = instructor.from_openai(OpenAI(api_key=settings.openai_api_key))
    result: _ExtractedPageText = client.chat.completions.create(
        model=settings.openai_vision_model,
        response_model=_ExtractedPageText,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Extract all text from this document image. "
                            "Preserve the original structure exactly: keep line breaks, "
                            "spacing between columns, and table row alignment. "
                            "Return only the raw document text — no commentary, "
                            "no markdown formatting."
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                    },
                ],
            }
        ],
        max_tokens=4096,
    )
    log.info("Vision LLM extracted %d characters from page", len(result.raw_text))
    return result.raw_text
