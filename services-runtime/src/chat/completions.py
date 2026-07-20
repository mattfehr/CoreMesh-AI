"""OpenAI-shaped chat completion responses for gateway regression traffic.

System role:
    Builds deterministic stub completions for keyless CI and optional live
    OpenAI completions when a provider key is configured.
Dependencies:
    Pydantic validates request payloads; the OpenAI SDK is imported lazily.
Side effects:
    Live mode sends one chat.completions request to OpenAI when enabled.
"""

from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, Field

from src.config import settings

STUB_CONTENT_PREFIX = "coremesh-chat-stub:"
STUB_COMPLETION_ID = "chatcmpl-coremesh-stub"


class ChatMessage(BaseModel):
    """One OpenAI-style chat message."""

    role: str
    content: str | list[Any] | dict[str, Any] | None = None


class ChatCompletionRequest(BaseModel):
    """Subset of the OpenAI chat.completions request used by CoreMesh."""

    model: str = "gpt-4o-mini"
    messages: list[ChatMessage] = Field(min_length=1)
    temperature: float | None = None


def extract_last_user_text(messages: list[ChatMessage]) -> str:
    """Return the last user message content as plain text."""

    for message in reversed(messages):
        if message.role != "user":
            continue
        content = message.content
        if isinstance(content, str):
            text = content.strip()
            if text:
                return text
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str) and item.strip():
                    parts.append(item.strip())
                elif isinstance(item, dict):
                    value = item.get("text")
                    if isinstance(value, str) and value.strip():
                        parts.append(value.strip())
            if parts:
                return "\n".join(parts)
    raise ValueError("messages must include a non-empty user content string")


def _stub_enabled() -> bool:
    raw = os.getenv("COREMESH_CHAT_STUB", "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return not bool(settings.openai_api_key.strip())


def _stub_response(*, model: str, user_text: str) -> dict[str, Any]:
    return {
        "id": STUB_COMPLETION_ID,
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": f"{STUB_CONTENT_PREFIX} {user_text}",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


def _openai_response(*, model: str, messages: list[ChatMessage], temperature: float | None) -> dict[str, Any]:
    from openai import OpenAI

    client = OpenAI(api_key=settings.openai_api_key)
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [message.model_dump(exclude_none=True) for message in messages],
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    completion = client.chat.completions.create(**kwargs)
    return completion.model_dump(mode="json")


def build_chat_completion(request: ChatCompletionRequest) -> dict[str, Any]:
    """Return a stub or live OpenAI chat.completion payload."""

    user_text = extract_last_user_text(request.messages)
    if _stub_enabled():
        return _stub_response(model=request.model, user_text=user_text)
    if not settings.openai_api_key.strip():
        return _stub_response(model=request.model, user_text=user_text)
    return _openai_response(
        model=request.model,
        messages=request.messages,
        temperature=request.temperature,
    )
