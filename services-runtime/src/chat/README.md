# Chat completions helpers for the minimal OpenAI-compatible HTTP route.

This package owns deterministic stub responses for keyless CI and optional
live OpenAI forwarding when ``OPENAI_API_KEY`` is configured. Set
``COREMESH_CHAT_STUB=true`` to force the stub even when a provider key is
present.
