"""Document-ingestion pipeline package.

System role:
    Converts uploaded PDFs and raster images into validated invoice data for
    the runtime API and the document-extraction agent specialist.
Dependencies:
    Sibling modules integrate Pillow/OpenCV, Tesseract, EasyOCR, Pydantic, and
    optional OpenAI vision and structured-extraction clients.
Side effects:
    Importing this package alone has none; invoked pipeline functions can use
    native OCR/model resources, perform external API calls, and log results.
"""
