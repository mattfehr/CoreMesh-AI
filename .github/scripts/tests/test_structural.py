"""Unit coverage for Python, Go, and Compose structural extraction."""
from __future__ import annotations

import pytest

from self_healing_docs.models import SourceFileDelta
from self_healing_docs.structural import (
    StructuralParseError,
    extract_go_units,
    extract_structural_changes,
)


def _delta(
    *,
    language: str,
    old: str,
    new: str,
    path: str,
) -> SourceFileDelta:
    return SourceFileDelta(
        status="M",
        old_path=path,
        new_path=path,
        old_text=old,
        new_text=new,
        language=language,  # type: ignore[arg-type]
    )


def test_python_signature_field_and_route_changes_are_bounded() -> None:
    old = """
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="Demo")

class Request(BaseModel):
    timeout: int = 30

@app.post("/v1/run")
async def run(payload: Request, retries: int = 1) -> dict:
    return {"ok": True}
"""
    new = """
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="Demo")

class Request(BaseModel):
    timeout: float = 15.0

@app.post("/v2/run")
async def run(payload: Request, retries: float = 1.5) -> dict:
    return {"ok": False}
"""
    changes = extract_structural_changes(
        [_delta(language="python", old=old, new=new, path="service.py")]
    )

    names = {change.name: change for change in changes}
    assert names["Request.timeout"].change_type == "modified"
    assert names["Request.timeout"].auto_fix_eligible is True
    assert names["run"].auto_fix_eligible is True
    assert "/v2/run" in (names["run"].after or "")


def test_python_comment_and_body_only_refactor_is_ignored() -> None:
    old = """
def public(value: int = 1) -> int:
    return value + 1
"""
    new = """
# A new comment does not change the public contract.
def public(value: int = 1) -> int:
    result = value + 2
    return result
"""
    changes = extract_structural_changes(
        [_delta(language="python", old=old, new=new, path="module.py")]
    )
    assert changes == []


def test_added_python_symbol_requires_review() -> None:
    changes = extract_structural_changes(
        [
            _delta(
                language="python",
                path="module.py",
                old="",
                new="def public(value: int) -> int:\n    return value\n",
            )
        ]
    )
    assert len(changes) == 1
    assert changes[0].change_type == "added"
    assert changes[0].auto_fix_eligible is False


def test_renamed_or_deleted_python_symbols_require_review() -> None:
    renamed = extract_structural_changes(
        [
            _delta(
                language="python",
                path="module.py",
                old="def Previous(value: int) -> int:\n    return value\n",
                new="def Current(value: int) -> int:\n    return value\n",
            )
        ]
    )
    assert {change.change_type for change in renamed} == {"added", "removed"}
    assert all(not change.auto_fix_eligible for change in renamed)

    deleted = extract_structural_changes(
        [
            _delta(
                language="python",
                path="module.py",
                old="def Public(value: int) -> int:\n    return value\n",
                new="",
            )
        ]
    )
    assert len(deleted) == 1
    assert deleted[0].change_type == "removed"
    assert deleted[0].auto_fix_eligible is False


def test_go_struct_field_and_function_signature_changes_are_detected() -> None:
    old = """
package demo

type Config struct {
    Timeout int
}

func Run(cfg Config, retries int) error {
    return nil
}
"""
    new = """
package demo

type Config struct {
    Timeout float64
}

func Run(cfg Config, retries float64) error {
    println("body changes do not matter")
    return nil
}
"""
    changes = extract_structural_changes(
        [_delta(language="go", old=old, new=new, path="demo.go")]
    )
    by_name = {change.name: change for change in changes}
    assert by_name["Config.Timeout"].auto_fix_eligible is True
    assert by_name["Run"].auto_fix_eligible is True


def test_go_comment_and_body_only_change_is_ignored() -> None:
    old = "package demo\nfunc Run(value int) int { return value }\n"
    new = (
        "package demo\n// Run is still the same contract.\n"
        "func Run(value int) int { value++; return value }\n"
    )
    changes = extract_structural_changes(
        [_delta(language="go", old=old, new=new, path="demo.go")]
    )
    assert changes == []


def test_go_receiver_variable_rename_is_ignored() -> None:
    old = """
package demo
type Runner struct{}
func (left *Runner) Execute(value int) int { return value }
"""
    new = """
package demo
type Runner struct{}
func (right *Runner) Execute(value int) int { return value + 1 }
"""
    changes = extract_structural_changes(
        [_delta(language="go", old=old, new=new, path="demo.go")]
    )
    assert changes == []


def test_go_http_route_registration_is_extracted() -> None:
    units = extract_go_units(
        "main.go",
        """
package main
import "net/http"
func main() {
    mux := http.NewServeMux()
    mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {})
}
""",
    )
    route = next(unit for unit in units if unit.kind == "go_route")
    assert route.name == "/healthz"
    assert route.representation == 'mux.HandleFunc("/healthz", <handler>)'


def test_compose_service_contract_change_is_bounded() -> None:
    old = """
services:
  api:
    image: example/api:1
    ports: ["8000:8000"]
    environment:
      TIMEOUT: "30"
"""
    new = """
services:
  api:
    image: example/api:1
    ports: ["8080:8000"]
    environment:
      TIMEOUT: "15"
"""
    changes = extract_structural_changes(
        [_delta(language="compose", old=old, new=new, path="docker-compose.yml")]
    )
    assert len(changes) == 1
    assert changes[0].name == "api"
    assert changes[0].auto_fix_eligible is True


def test_compose_environment_contract_tracks_keys_not_values() -> None:
    old = """
services:
  api:
    environment:
      TIMEOUT: "30"
"""
    value_only = """
services:
  api:
    environment:
      TIMEOUT: "15"
"""
    key_added = """
services:
  api:
    environment:
      TIMEOUT: "15"
      RETRIES: "3"
"""

    assert extract_structural_changes(
        [_delta(language="compose", old=old, new=value_only, path="compose.yml")]
    ) == []
    changes = extract_structural_changes(
        [
            _delta(
                language="compose",
                old=value_only,
                new=key_added,
                path="compose.yml",
            )
        ]
    )
    assert len(changes) == 1
    assert "RETRIES" in (changes[0].after or "")


def test_compose_mapping_and_order_only_changes_are_ignored() -> None:
    old = """
services:
  api:
    ports:
      - "8080:8000"
      - "8081:8001"
    profiles: ["app", "debug"]
    depends_on: ["redis", "postgres"]
"""
    new = """
services:
  api:
    depends_on:
      - postgres
      - redis
    profiles: ["debug", "app"]
    ports: ["8081:8001", "8080:8000"]
"""
    assert extract_structural_changes(
        [_delta(language="compose", old=old, new=new, path="compose.yml")]
    ) == []


@pytest.mark.parametrize(
    ("language", "source", "path"),
    [
        ("python", "def broken(:\n", "broken.py"),
        ("go", "package demo\nfunc Broken(", "broken.go"),
        ("compose", "services: [", "docker-compose.yml"),
    ],
)
def test_malformed_structural_source_fails_closed(
    language: str,
    source: str,
    path: str,
) -> None:
    with pytest.raises(StructuralParseError):
        extract_structural_changes(
            [_delta(language=language, old="", new=source, path=path)]
        )
