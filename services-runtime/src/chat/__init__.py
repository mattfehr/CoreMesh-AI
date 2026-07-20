"""Minimal OpenAI-shaped chat completions helpers for the runtime HTTP surface."""

from src.chat.completions import (
    ChatCompletionRequest,
    build_chat_completion,
    extract_last_user_text,
)

__all__ = [
    "ChatCompletionRequest",
    "build_chat_completion",
    "extract_last_user_text",
]
