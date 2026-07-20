"""HTTP contract tests for the minimal chat completions route.

System role:
    Locks the OpenAI-shaped stub path used by Phase 4.2 regression CI.
Dependencies:
    FastAPI TestClient and the runtime app package.
Side effects:
    None; OpenAI is not contacted in stub mode.
"""
import sys
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.main import app  # noqa: E402


client = TestClient(app)


def test_chat_completions_stub_returns_deterministic_content(monkeypatch):
    monkeypatch.setenv("COREMESH_CHAT_STUB", "true")
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "Ping CoreMesh chat path."}],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "chatcmpl-coremesh-stub"
    assert body["object"] == "chat.completion"
    assert body["model"] == "gpt-4o-mini"
    assert body["choices"][0]["message"]["content"] == (
        "coremesh-chat-stub: Ping CoreMesh chat path."
    )


def test_chat_completions_rejects_missing_messages():
    response = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": []},
    )

    assert response.status_code == 400


def test_chat_completions_rejects_missing_user_content(monkeypatch):
    monkeypatch.setenv("COREMESH_CHAT_STUB", "true")
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "system", "content": "You are helpful."}],
        },
    )

    assert response.status_code == 400
    assert "user content" in str(response.json()["detail"]).lower()
