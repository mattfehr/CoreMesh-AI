"""Detect structural code drift and repair mapped CoreMesh documentation.

System role:
    Workflow-local package behind the Phase 4.4 self-healing documentation
    GitHub Action.
Dependencies:
    Git, Python ASTs, Tree-sitter Go, PyYAML, OpenAI embeddings/Responses, and
    Pydantic structured outputs.
Side effects:
    The CLI reads Git objects and tracked Markdown, calls OpenAI when structural
    changes exist, writes run reports, and edits approved Markdown only when
    explicit apply mode is enabled.
"""

from .config import HealingConfig
from .pipeline import run_healing

__all__ = ["HealingConfig", "run_healing"]
