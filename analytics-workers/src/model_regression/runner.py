"""CoreMesh golden-dataset regression evaluation CLI.

System role:
    Provides the Phase 4.2 quality gate used by GitHub Actions. It seeds
    deterministic CI cases, evaluates every row in golden_datasets through the
    gateway API, and compares baseline/head reports for accuracy regressions.
Dependencies:
    SQLAlchemy reads PostgreSQL, urllib calls the gateway and optional Slack
    webhook, and OpenAI is used only for llm_judge rows.
Side effects:
    The seed command upserts deterministic golden rows. The run command sends
    HTTP requests to the configured gateway and writes report files. The
    compare command writes diff reports and exits nonzero for critical drops.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text


DEFAULT_POSTGRES_DSN = "postgresql://coremesh:coremesh_secret@localhost:5432/coremesh"
DEFAULT_GATEWAY_URL = "http://localhost:8080"
DEFAULT_GATEWAY_PATH = "/v1/chat/completions"
DEFAULT_MAX_DROP = 0.03
DEFAULT_MIN_ACCURACY = 0.90
FLOAT_EPSILON = 1e-12
REPORT_SCHEMA_VERSION = "coremesh-model-regression-report-v1"
COMPARISON_SCHEMA_VERSION = "coremesh-model-regression-comparison-v1"
CHAT_STUB_PROMPT = "Ping CoreMesh chat path."
CHAT_STUB_EXPECTED_JSON = {
    "id": "chatcmpl-coremesh-stub",
    "object": "chat.completion",
    "model": "gpt-4o-mini",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": f"coremesh-chat-stub: {CHAT_STUB_PROMPT}",
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


class RegressionRunnerError(RuntimeError):
    """Base class for user-correctable evaluator failures."""


class NoGoldenCasesError(RegressionRunnerError):
    """Raised when the loaded golden dataset is empty."""


class UnsupportedScoringModeError(RegressionRunnerError):
    """Raised when a row requests a scoring mode this runner cannot execute."""


class ScoringConfigurationError(RegressionRunnerError):
    """Raised when a scoring block is incomplete or malformed."""


class JudgeVerdict(BaseModel):
    """Structured OpenAI judge result for llm_judge scoring."""

    passed: bool
    score: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=1)


@dataclass(frozen=True)
class GoldenCase:
    """One loaded golden_datasets row."""

    case_id: str
    source_fingerprint: str | None
    feature_scope: str
    user_input: str
    expected_output: dict[str, Any]
    difficulty_rating: str
    origin_source: str

    @property
    def case_key(self) -> str:
        return self.source_fingerprint or self.case_id


@dataclass(frozen=True)
class HttpResult:
    """Normalized HTTP outcome for a gateway call."""

    status: int | str
    headers: dict[str, str]
    body: str
    latency_ms: float


@dataclass(frozen=True)
class EvaluationOutcome:
    """Normalized score for one case."""

    passed: bool
    score: float
    failure_reasons: list[str]
    mode: str
    request_path: str
    status: int | str
    latency_ms: float
    details: dict[str, Any]


def utc_now() -> str:
    """Return an RFC 3339 UTC timestamp for report metadata."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _stable_fingerprint(material: str) -> str:
    import hashlib

    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _seed_cases() -> list[dict[str, Any]]:
    """Return deterministic CI cases that exercise gateway routing and chat output."""

    simple_id = uuid5(NAMESPACE_URL, "coremesh:model-regression-ci:simple-routing")
    complex_id = uuid5(NAMESPACE_URL, "coremesh:model-regression-ci:complex-routing")
    chat_stub_id = uuid5(NAMESPACE_URL, "coremesh:model-regression-ci:chat-stub-output")
    return [
        {
            "case_id": str(simple_id),
            "source_fingerprint": _stable_fingerprint("ci:simple-routing:v1"),
            "feature_scope": "gateway_autopilot",
            "user_input": (
                "Reformat this contact into JSON with name and phone fields: "
                "Ada Lovelace, 555-0100."
            ),
            "expected_output": {
                "reference_answer": "The gateway routes a simple request to the tier-1 model.",
                "validation_criteria": [
                    {
                        "description": "Autopilot classifies the request as tier-1.",
                        "required": True,
                    },
                    {
                        "description": "Semantic cache is allowed for the simple request.",
                        "required": True,
                    },
                ],
                "expected_behavior": "answer",
                "failure_pattern": "Simple prompts should not be over-routed.",
                "scoring": {
                    "mode": "headers",
                    "request": {
                        "method": "POST",
                        "path": DEFAULT_GATEWAY_PATH,
                        "json": {
                            "model": "client-selected-model",
                            "messages": [
                                {
                                    "role": "user",
                                    "content": (
                                        "Reformat this contact into JSON with name and "
                                        "phone fields: Ada Lovelace, 555-0100."
                                    ),
                                }
                            ],
                            "temperature": 0.2,
                        },
                    },
                    "expect": {
                        "X-CoreMesh-Autopilot-Tier": "tier-1",
                        "X-CoreMesh-Routed-Model": "gpt-4o-mini",
                        "X-CoreMesh-Cache-Policy": "allow",
                    },
                },
            },
            "difficulty_rating": "simple",
            "origin_source": "human_curated",
        },
        {
            "case_id": str(complex_id),
            "source_fingerprint": _stable_fingerprint("ci:complex-routing:v1"),
            "feature_scope": "gateway_autopilot",
            "user_input": (
                "Analyze this Go concurrency bug, debug the root cause, and compare "
                "two safe fixes with tradeoffs:\n```go\nfunc worker(ch chan int) { "
                "close(ch); ch <- 1 }\n```"
            ),
            "expected_output": {
                "reference_answer": "The gateway routes complex/debugging requests to tier-3.",
                "validation_criteria": [
                    {
                        "description": "Autopilot classifies the request as tier-3.",
                        "required": True,
                    },
                    {
                        "description": "Complex requests bypass semantic cache lookup.",
                        "required": True,
                    },
                ],
                "expected_behavior": "answer",
                "failure_pattern": "Complex prompts should not be routed to the cheap tier.",
                "scoring": {
                    "mode": "headers",
                    "request": {
                        "method": "POST",
                        "path": DEFAULT_GATEWAY_PATH,
                        "json": {
                            "model": "client-selected-model",
                            "messages": [
                                {
                                    "role": "user",
                                    "content": (
                                        "Analyze this Go concurrency bug, debug the root "
                                        "cause, and compare two safe fixes with tradeoffs:\n"
                                        "```go\nfunc worker(ch chan int) { close(ch); "
                                        "ch <- 1 }\n```"
                                    ),
                                }
                            ],
                            "temperature": 0.2,
                        },
                    },
                    "expect": {
                        "X-CoreMesh-Autopilot-Tier": "tier-3",
                        "X-CoreMesh-Routed-Model": "gpt-4o",
                        "X-CoreMesh-Cache-Policy": "bypass",
                    },
                },
            },
            "difficulty_rating": "hard",
            "origin_source": "human_curated",
        },
        {
            "case_id": str(chat_stub_id),
            "source_fingerprint": _stable_fingerprint("ci:chat-stub-output:v1"),
            "feature_scope": "runtime_chat",
            "user_input": CHAT_STUB_PROMPT,
            "expected_output": {
                "reference_answer": "Runtime returns the deterministic chat stub body.",
                "validation_criteria": [
                    {
                        "description": "Gateway reaches runtime /v1/chat/completions.",
                        "required": True,
                    }
                ],
                "expected_behavior": "answer",
                "failure_pattern": "Chat path must not 404 or return a non-stub body in CI.",
                "scoring": {
                    "mode": "exact_json",
                    "expected_json": CHAT_STUB_EXPECTED_JSON,
                    "request": {
                        "method": "POST",
                        "path": DEFAULT_GATEWAY_PATH,
                        "json": {
                            "model": "client-selected-model",
                            "messages": [
                                {"role": "user", "content": CHAT_STUB_PROMPT}
                            ],
                            "temperature": 0.0,
                        },
                    },
                },
            },
            "difficulty_rating": "simple",
            "origin_source": "human_curated",
        },
    ]


def seed_golden_cases(postgres_dsn: str) -> int:
    """Upsert deterministic CI cases into golden_datasets."""

    engine = create_engine(postgres_dsn, pool_pre_ping=True)
    seeded = _seed_cases()
    try:
        with engine.begin() as connection:
            for case in seeded:
                connection.execute(
                    text(
                        """
                        INSERT INTO golden_datasets (
                            case_id,
                            feature_scope,
                            user_input,
                            expected_output,
                            difficulty_rating,
                            origin_source,
                            source_fingerprint,
                            provenance
                        ) VALUES (
                            :case_id,
                            :feature_scope,
                            :user_input,
                            CAST(:expected_output AS JSONB),
                            :difficulty_rating,
                            :origin_source,
                            :source_fingerprint,
                            CAST(:provenance AS JSONB)
                        )
                        ON CONFLICT (case_id) DO UPDATE
                        SET feature_scope = EXCLUDED.feature_scope,
                            user_input = EXCLUDED.user_input,
                            expected_output = EXCLUDED.expected_output,
                            difficulty_rating = EXCLUDED.difficulty_rating,
                            origin_source = EXCLUDED.origin_source,
                            source_fingerprint = EXCLUDED.source_fingerprint,
                            provenance = EXCLUDED.provenance
                        """
                    ),
                    {
                        **case,
                        "expected_output": json.dumps(case["expected_output"]),
                        "provenance": json.dumps(
                            {
                                "ci_seed": True,
                                "seed_version": "model-regression-ci-v1",
                            }
                        ),
                    },
                )
    finally:
        engine.dispose()
    return len(seeded)


def _normalize_expected_output(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        raise ScoringConfigurationError("expected_output must be a JSON object")
    return raw


def load_golden_cases(postgres_dsn: str) -> list[GoldenCase]:
    """Load all golden cases in stable execution order."""

    engine = create_engine(postgres_dsn, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT
                        case_id::text AS case_id,
                        source_fingerprint,
                        feature_scope,
                        user_input,
                        expected_output,
                        difficulty_rating,
                        origin_source
                    FROM golden_datasets
                    ORDER BY feature_scope ASC,
                             COALESCE(source_fingerprint, '') ASC,
                             case_id ASC
                    """
                )
            ).mappings()
            return [
                GoldenCase(
                    case_id=row["case_id"],
                    source_fingerprint=(
                        str(row["source_fingerprint"]).strip()
                        if row["source_fingerprint"]
                        else None
                    ),
                    feature_scope=str(row["feature_scope"]),
                    user_input=str(row["user_input"]),
                    expected_output=_normalize_expected_output(row["expected_output"]),
                    difficulty_rating=str(row["difficulty_rating"]),
                    origin_source=str(row["origin_source"]),
                )
                for row in rows
            ]
    finally:
        engine.dispose()


def scoring_config(case: GoldenCase) -> dict[str, Any]:
    """Return the row's scoring block, defaulting mined rows to llm_judge."""

    raw = case.expected_output.get("scoring")
    if raw is None:
        return {"mode": "llm_judge"}
    if not isinstance(raw, dict):
        raise ScoringConfigurationError(
            f"case {case.case_key} has non-object expected_output.scoring"
        )
    mode = str(raw.get("mode") or "llm_judge").strip()
    if not mode:
        raise ScoringConfigurationError(f"case {case.case_key} has empty scoring mode")
    return {**raw, "mode": mode}


def _case_url(gateway_url: str, path: str) -> str:
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{gateway_url.rstrip('/')}{path}"


def _request_payload(
    case: GoldenCase, config: Mapping[str, Any]
) -> tuple[str, str, dict[str, str], bytes | None]:
    request_cfg = config.get("request") or {}
    if not isinstance(request_cfg, dict):
        raise ScoringConfigurationError(
            f"case {case.case_key} has non-object scoring.request"
        )

    method = str(request_cfg.get("method") or "POST").upper()
    path = str(request_cfg.get("path") or DEFAULT_GATEWAY_PATH)
    headers = {
        "Content-Type": "application/json",
        "X-Team-ID": "model-regression-ci",
        "X-User-ID": case.case_key,
    }
    configured_headers = request_cfg.get("headers") or {}
    if not isinstance(configured_headers, dict):
        raise ScoringConfigurationError(
            f"case {case.case_key} has non-object request.headers"
        )
    headers.update({str(key): str(value) for key, value in configured_headers.items()})

    if method in {"GET", "HEAD"}:
        return method, path, headers, None

    body = request_cfg.get("json")
    if body is None:
        body = {
            "model": "client-selected-model",
            "messages": [{"role": "user", "content": case.user_input}],
            "temperature": 0.2,
        }
    return method, path, headers, json.dumps(body).encode("utf-8")


def send_gateway_request(
    *,
    gateway_url: str,
    case: GoldenCase,
    config: Mapping[str, Any],
    timeout: float,
) -> tuple[HttpResult, str]:
    """Send one configured request and normalize HTTP errors as results."""

    method, path, headers, data = _request_payload(case, config)
    request = urllib.request.Request(
        _case_url(gateway_url, path),
        data=data,
        method=method,
        headers=headers,
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            status: int | str = response.status
            response_headers = dict(response.headers.items())
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        status = error.code
        response_headers = dict(error.headers.items())
    except urllib.error.URLError as error:
        body = ""
        status = f"error:{error.reason}"
        response_headers = {}
    latency_ms = (time.perf_counter() - started) * 1000.0
    return HttpResult(status, response_headers, body, latency_ms), path


def _header(headers: Mapping[str, str], name: str) -> str:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return ""


def _score_headers(
    *,
    case: GoldenCase,
    config: Mapping[str, Any],
    result: HttpResult,
    path: str,
) -> EvaluationOutcome:
    expected = config.get("expect") or config.get("headers") or {}
    if not isinstance(expected, dict):
        raise ScoringConfigurationError(
            f"case {case.case_key} has non-object headers expectations"
        )
    statuses = config.get("status") or config.get("expected_status")
    if isinstance(statuses, int):
        expected_statuses: set[int | str] | None = {statuses}
    elif isinstance(statuses, list):
        expected_statuses = set(statuses)
    elif statuses is None:
        expected_statuses = None
    else:
        raise ScoringConfigurationError(
            f"case {case.case_key} has invalid expected status"
        )
    if not expected and expected_statuses is None:
        raise ScoringConfigurationError(
            f"case {case.case_key} headers scoring needs headers or status"
        )

    failures: list[str] = []
    details: dict[str, Any] = {"expected_headers": expected}
    if expected_statuses is not None and result.status not in expected_statuses:
        failures.append(f"status {result.status!r}, want one of {sorted(expected_statuses)!r}")
    actual_headers: dict[str, str] = {}
    for name, wanted in expected.items():
        actual = _header(result.headers, str(name))
        actual_headers[str(name)] = actual
        if actual != str(wanted):
            failures.append(f"{name}: {actual!r}, want {wanted!r}")
    details["actual_headers"] = actual_headers

    passed = not failures
    return EvaluationOutcome(
        passed=passed,
        score=1.0 if passed else 0.0,
        failure_reasons=failures,
        mode="headers",
        request_path=path,
        status=result.status,
        latency_ms=result.latency_ms,
        details=details,
    )


def _score_exact_json(
    *,
    case: GoldenCase,
    config: Mapping[str, Any],
    result: HttpResult,
    path: str,
) -> EvaluationOutcome:
    sentinel = object()
    expected = config.get("expected_json", sentinel)
    if expected is sentinel:
        expected = case.expected_output.get("expected_json", sentinel)
    if expected is sentinel:
        raise ScoringConfigurationError(
            f"case {case.case_key} exact_json scoring needs expected_json"
        )
    failures: list[str] = []
    try:
        actual = json.loads(result.body)
    except json.JSONDecodeError as error:
        actual = None
        failures.append(f"response body is not JSON: {error.msg}")
    if not failures and actual != expected:
        failures.append("response JSON did not exactly match expected_json")
    passed = not failures
    return EvaluationOutcome(
        passed=passed,
        score=1.0 if passed else 0.0,
        failure_reasons=failures,
        mode="exact_json",
        request_path=path,
        status=result.status,
        latency_ms=result.latency_ms,
        details={"expected_json": expected, "actual_json": actual},
    )


class OpenAIJudge:
    """Lazy OpenAI structured judge adapter for llm_judge rows."""

    def __init__(self, *, api_key: str, model: str) -> None:
        if not api_key.strip():
            raise ScoringConfigurationError(
                "OPENAI_API_KEY is required for llm_judge scoring rows"
            )
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key)
        self.model = model

    def __call__(
        self,
        *,
        case: GoldenCase,
        model_output: str,
        config: Mapping[str, Any],
    ) -> JudgeVerdict:
        payload = {
            "feature_scope": case.feature_scope,
            "user_input": case.user_input,
            "model_output": model_output,
            "reference_answer": case.expected_output.get("reference_answer", ""),
            "validation_criteria": case.expected_output.get("validation_criteria", []),
            "expected_behavior": case.expected_output.get("expected_behavior", "answer"),
            "failure_pattern": case.expected_output.get("failure_pattern", ""),
            "pass_threshold": config.get("pass_threshold", 0.80),
        }
        response = self.client.responses.parse(
            model=self.model,
            store=False,
            text_format=JudgeVerdict,
            input=[
                {
                    "role": "system",
                    "content": (
                        "You are a strict regression evaluator. Score whether the "
                        "model output satisfies the reference answer and required "
                        "criteria. Return a score from 0.0 to 1.0."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(payload, sort_keys=True),
                },
            ],
        )
        return JudgeVerdict.model_validate(response.output_parsed)


def _score_llm_judge(
    *,
    case: GoldenCase,
    config: Mapping[str, Any],
    result: HttpResult,
    path: str,
    judge: Callable[..., JudgeVerdict],
) -> EvaluationOutcome:
    threshold = float(config.get("pass_threshold", 0.80))
    verdict = judge(case=case, model_output=result.body, config=config)
    passed = bool(verdict.passed) and verdict.score >= threshold
    failures = [] if passed else [verdict.rationale]
    return EvaluationOutcome(
        passed=passed,
        score=float(verdict.score),
        failure_reasons=failures,
        mode="llm_judge",
        request_path=path,
        status=result.status,
        latency_ms=result.latency_ms,
        details={
            "judge_model": config.get("judge_model"),
            "pass_threshold": threshold,
            "rationale": verdict.rationale,
        },
    )


def evaluate_case(
    *,
    case: GoldenCase,
    gateway_url: str,
    timeout: float,
    openai_api_key: str,
    judge_model: str,
    judge: Callable[..., JudgeVerdict] | None = None,
) -> dict[str, Any]:
    """Evaluate one golden case and return its report payload."""

    config = scoring_config(case)
    mode = str(config["mode"])
    result, path = send_gateway_request(
        gateway_url=gateway_url,
        case=case,
        config=config,
        timeout=timeout,
    )

    if mode == "headers":
        outcome = _score_headers(case=case, config=config, result=result, path=path)
    elif mode == "exact_json":
        outcome = _score_exact_json(case=case, config=config, result=result, path=path)
    elif mode == "llm_judge":
        if judge is None:
            judge = OpenAIJudge(
                api_key=openai_api_key,
                model=str(config.get("judge_model") or judge_model),
            )
        outcome = _score_llm_judge(
            case=case,
            config=config,
            result=result,
            path=path,
            judge=judge,
        )
    else:
        raise UnsupportedScoringModeError(
            f"case {case.case_key} requested unsupported scoring mode {mode!r}"
        )

    return {
        "case_id": case.case_id,
        "source_fingerprint": case.source_fingerprint,
        "case_key": case.case_key,
        "feature_scope": case.feature_scope,
        "difficulty_rating": case.difficulty_rating,
        "origin_source": case.origin_source,
        "mode": outcome.mode,
        "passed": outcome.passed,
        "score": round(outcome.score, 6),
        "status": outcome.status,
        "latency_ms": round(outcome.latency_ms, 2),
        "request_path": outcome.request_path,
        "failure_reasons": outcome.failure_reasons,
        "details": outcome.details,
    }


def _metric_summary(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    count = len(results)
    passed = sum(1 for item in results if bool(item["passed"]))
    score_sum = sum(float(item["score"]) for item in results)
    return {
        "case_count": count,
        "passed_count": passed,
        "failed_count": count - passed,
        "accuracy": round(score_sum / count, 6) if count else 0.0,
        "pass_rate": round(passed / count, 6) if count else 0.0,
    }


def build_report(
    *,
    cases: Sequence[GoldenCase],
    gateway_url: str,
    timeout: float,
    openai_api_key: str,
    judge_model: str,
    run_label: str,
    revision: str,
    judge: Callable[..., JudgeVerdict] | None = None,
) -> dict[str, Any]:
    """Evaluate all cases and return a serializable report."""

    if not cases:
        raise NoGoldenCasesError("golden_datasets contains no rows")

    results = [
        evaluate_case(
            case=case,
            gateway_url=gateway_url,
            timeout=timeout,
            openai_api_key=openai_api_key,
            judge_model=judge_model,
            judge=judge,
        )
        for case in cases
    ]
    by_scope: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for result in results:
        by_scope[str(result["feature_scope"])].append(result)

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "run_id": str(uuid4()),
        "generated_at": utc_now(),
        "run_label": run_label,
        "revision": revision,
        "gateway_url": gateway_url,
        "summary": _metric_summary(results),
        "by_feature_scope": {
            scope: _metric_summary(scope_results)
            for scope, scope_results in sorted(by_scope.items())
        },
        "cases": results,
    }


def _percent(value: float) -> str:
    return f"{value * 100:.2f}%"


def report_markdown(report: Mapping[str, Any]) -> str:
    """Render one evaluation report as Markdown."""

    summary = report["summary"]
    lines = [
        "# CoreMesh Model Regression Report",
        "",
        f"- Run: `{report.get('run_label') or '-'}`",
        f"- Revision: `{report.get('revision') or '-'}`",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Accuracy: **{_percent(float(summary['accuracy']))}**",
        f"- Cases: {summary['passed_count']} passed / {summary['case_count']} total",
        "",
        "## Feature Scopes",
        "",
        "| Scope | Accuracy | Passed | Cases |",
        "| --- | ---: | ---: | ---: |",
    ]
    for scope, metrics in report["by_feature_scope"].items():
        lines.append(
            f"| `{scope}` | {_percent(float(metrics['accuracy']))} | "
            f"{metrics['passed_count']} | {metrics['case_count']} |"
        )

    failed = [case for case in report["cases"] if not case["passed"]]
    lines.extend(["", "## Failed Cases", ""])
    if not failed:
        lines.append("No failed cases.")
    else:
        for case in failed:
            reasons = "; ".join(case["failure_reasons"]) or "score below threshold"
            lines.append(f"- `{case['case_key']}` `{case['feature_scope']}`: {reasons}")
    return "\n".join(lines) + "\n"


def _html_page(title: str, markdown_text: str) -> str:
    escaped = html.escape(markdown_text)
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<title>{html.escape(title)}</title>"
        "<style>body{font-family:Arial,sans-serif;max-width:960px;margin:32px auto;"
        "line-height:1.5}pre{white-space:pre-wrap;background:#f6f8fa;padding:16px;"
        "border-radius:6px}</style></head><body>"
        f"<h1>{html.escape(title)}</h1><pre>{escaped}</pre></body></html>"
    )


def write_outputs(
    payload: Mapping[str, Any],
    *,
    output: Path,
    markdown_output: Path | None,
    html_output: Path | None,
    markdown_factory: Callable[[Mapping[str, Any]], str],
    title: str,
) -> None:
    """Write JSON plus optional human-readable artifacts."""

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if markdown_output is not None:
        markdown_output.parent.mkdir(parents=True, exist_ok=True)
        markdown = markdown_factory(payload)
        markdown_output.write_text(markdown, encoding="utf-8")
        if html_output is not None:
            html_output.parent.mkdir(parents=True, exist_ok=True)
            html_output.write_text(_html_page(title, markdown), encoding="utf-8")


def _load_report(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _case_map(report: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {str(case["case_key"]): case for case in report.get("cases", [])}


def _drop_check(
    *,
    scope: str,
    baseline_accuracy: float,
    candidate_accuracy: float,
    max_drop: float,
) -> dict[str, Any]:
    drop = round(baseline_accuracy - candidate_accuracy, 6)
    status = "pass"
    if drop > max_drop + FLOAT_EPSILON:
        status = "fail"
    elif drop > FLOAT_EPSILON:
        status = "warn"
    return {
        "scope": scope,
        "baseline_accuracy": round(baseline_accuracy, 6),
        "candidate_accuracy": round(candidate_accuracy, 6),
        "drop": drop,
        "max_drop": max_drop,
        "status": status,
    }


def _min_accuracy_check(*, candidate_accuracy: float, min_accuracy: float) -> dict[str, Any]:
    status = "pass"
    if candidate_accuracy + FLOAT_EPSILON < min_accuracy:
        status = "fail"
    return {
        "scope": "min_accuracy",
        "baseline_accuracy": None,
        "candidate_accuracy": round(candidate_accuracy, 6),
        "drop": None,
        "max_drop": None,
        "min_accuracy": min_accuracy,
        "status": status,
    }


def compare_reports(
    *,
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    max_drop: float,
    min_accuracy: float = DEFAULT_MIN_ACCURACY,
) -> dict[str, Any]:
    """Compare baseline/candidate reports and classify regression severity."""

    candidate_accuracy = float(candidate["summary"]["accuracy"])
    checks = [
        _drop_check(
            scope="overall",
            baseline_accuracy=float(baseline["summary"]["accuracy"]),
            candidate_accuracy=candidate_accuracy,
            max_drop=max_drop,
        ),
        _min_accuracy_check(
            candidate_accuracy=candidate_accuracy,
            min_accuracy=min_accuracy,
        ),
    ]
    candidate_scopes = candidate.get("by_feature_scope", {})
    for scope, metrics in sorted(baseline.get("by_feature_scope", {}).items()):
        candidate_metrics = candidate_scopes.get(scope, {"accuracy": 0.0})
        checks.append(
            _drop_check(
                scope=scope,
                baseline_accuracy=float(metrics["accuracy"]),
                candidate_accuracy=float(candidate_metrics["accuracy"]),
                max_drop=max_drop,
            )
        )

    baseline_cases = _case_map(baseline)
    candidate_cases = _case_map(candidate)
    regressions = [
        {
            "case_key": key,
            "feature_scope": baseline_cases[key]["feature_scope"],
            "baseline_score": baseline_cases[key]["score"],
            "candidate_score": candidate_cases[key]["score"],
            "candidate_failures": candidate_cases[key].get("failure_reasons", []),
        }
        for key in sorted(baseline_cases.keys() & candidate_cases.keys())
        if baseline_cases[key].get("passed") and not candidate_cases[key].get("passed")
    ]
    improvements = [
        {
            "case_key": key,
            "feature_scope": candidate_cases[key]["feature_scope"],
            "baseline_score": baseline_cases[key]["score"],
            "candidate_score": candidate_cases[key]["score"],
        }
        for key in sorted(baseline_cases.keys() & candidate_cases.keys())
        if not baseline_cases[key].get("passed") and candidate_cases[key].get("passed")
    ]
    missing = sorted(baseline_cases.keys() - candidate_cases.keys())
    added = sorted(candidate_cases.keys() - baseline_cases.keys())

    status = "pass"
    if any(check["status"] == "fail" for check in checks) or missing:
        status = "fail"
    elif any(check["status"] == "warn" for check in checks) or regressions:
        status = "warn"

    return {
        "schema_version": COMPARISON_SCHEMA_VERSION,
        "generated_at": utc_now(),
        "status": status,
        "max_drop": max_drop,
        "min_accuracy": min_accuracy,
        "baseline": {
            "run_label": baseline.get("run_label"),
            "revision": baseline.get("revision"),
            "summary": baseline.get("summary"),
        },
        "candidate": {
            "run_label": candidate.get("run_label"),
            "revision": candidate.get("revision"),
            "summary": candidate.get("summary"),
        },
        "checks": checks,
        "regressions": regressions,
        "improvements": improvements,
        "missing_cases": missing,
        "added_cases": added,
    }


def comparison_markdown(comparison: Mapping[str, Any]) -> str:
    """Render a baseline/candidate comparison as Markdown."""

    status = str(comparison["status"]).upper()
    baseline_summary = comparison["baseline"]["summary"]
    candidate_summary = comparison["candidate"]["summary"]
    lines = [
        "# CoreMesh Model Regression Comparison",
        "",
        f"Status: **{status}**",
        f"Max allowed drop: **{_percent(float(comparison['max_drop']))}**",
        (
            f"Min candidate accuracy: "
            f"**{_percent(float(comparison.get('min_accuracy', DEFAULT_MIN_ACCURACY)))}**"
        ),
        "",
        "| Run | Accuracy | Passed | Cases | Revision |",
        "| --- | ---: | ---: | ---: | --- |",
        (
            f"| Baseline | {_percent(float(baseline_summary['accuracy']))} | "
            f"{baseline_summary['passed_count']} | {baseline_summary['case_count']} | "
            f"`{comparison['baseline'].get('revision') or '-'}` |"
        ),
        (
            f"| Candidate | {_percent(float(candidate_summary['accuracy']))} | "
            f"{candidate_summary['passed_count']} | {candidate_summary['case_count']} | "
            f"`{comparison['candidate'].get('revision') or '-'}` |"
        ),
        "",
        "## Accuracy Checks",
        "",
        "| Scope | Baseline | Candidate | Drop | Min | Status |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for check in comparison["checks"]:
        baseline_value = check.get("baseline_accuracy")
        drop_value = check.get("drop")
        min_value = check.get("min_accuracy")
        lines.append(
            f"| `{check['scope']}` | "
            f"{_percent(float(baseline_value)) if baseline_value is not None else '-'} | "
            f"{_percent(float(check['candidate_accuracy']))} | "
            f"{_percent(float(drop_value)) if drop_value is not None else '-'} | "
            f"{_percent(float(min_value)) if min_value is not None else '-'} | "
            f"{check['status']} |"
        )

    lines.extend(["", "## Case Regressions", ""])
    if not comparison["regressions"]:
        lines.append("No baseline-passing cases flipped to failing.")
    else:
        for item in comparison["regressions"]:
            reasons = "; ".join(item.get("candidate_failures", [])) or "candidate failed"
            lines.append(f"- `{item['case_key']}` `{item['feature_scope']}`: {reasons}")

    if comparison["missing_cases"]:
        lines.extend(["", "## Missing Candidate Cases", ""])
        for case_key in comparison["missing_cases"]:
            lines.append(f"- `{case_key}`")
    return "\n".join(lines) + "\n"


def notify_slack(*, comparison: Mapping[str, Any], webhook_url: str, run_url: str) -> None:
    """Send a compact, non-secret Slack webhook notification."""

    status = str(comparison["status"]).upper()
    overall = next(
        check for check in comparison["checks"] if check["scope"] == "overall"
    )
    text_value = (
        f"CoreMesh model regression {status}: candidate accuracy "
        f"{_percent(float(overall['candidate_accuracy']))}, drop "
        f"{_percent(float(overall['drop']))}."
    )
    if run_url:
        text_value = f"{text_value} {run_url}"
    request = urllib.request.Request(
        webhook_url,
        data=json.dumps({"text": text_value}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        response.read()


def _cmd_seed(args: argparse.Namespace) -> int:
    count = seed_golden_cases(args.postgres_dsn)
    print(f"seeded {count} deterministic model-regression golden cases")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    cases = load_golden_cases(args.postgres_dsn)
    report = build_report(
        cases=cases,
        gateway_url=args.gateway_url,
        timeout=args.timeout,
        openai_api_key=args.openai_api_key or os.getenv("OPENAI_API_KEY", ""),
        judge_model=args.judge_model,
        run_label=args.run_label,
        revision=args.revision,
    )
    write_outputs(
        report,
        output=Path(args.output),
        markdown_output=Path(args.markdown_output) if args.markdown_output else None,
        html_output=Path(args.html_output) if args.html_output else None,
        markdown_factory=report_markdown,
        title="CoreMesh Model Regression Report",
    )
    summary = report["summary"]
    print(
        f"evaluated {summary['case_count']} golden cases; "
        f"accuracy={_percent(float(summary['accuracy']))}; "
        f"passed={summary['passed_count']}"
    )
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    comparison = compare_reports(
        baseline=_load_report(Path(args.baseline)),
        candidate=_load_report(Path(args.candidate)),
        max_drop=args.max_drop,
        min_accuracy=args.min_accuracy,
    )
    write_outputs(
        comparison,
        output=Path(args.output),
        markdown_output=Path(args.markdown_output) if args.markdown_output else None,
        html_output=Path(args.html_output) if args.html_output else None,
        markdown_factory=comparison_markdown,
        title="CoreMesh Model Regression Comparison",
    )
    print(f"comparison status={comparison['status']}")
    return 1 if comparison["status"] == "fail" else 0


def _cmd_slack(args: argparse.Namespace) -> int:
    notify_slack(
        comparison=_load_report(Path(args.comparison)),
        webhook_url=args.webhook_url,
        run_url=args.run_url,
    )
    print("slack notification sent")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CoreMesh model regression CLI")
    subcommands = parser.add_subparsers(dest="command", required=True)

    seed = subcommands.add_parser("seed", help="seed deterministic CI golden cases")
    seed.add_argument("--postgres-dsn", default=os.getenv("POSTGRES_DSN", DEFAULT_POSTGRES_DSN))
    seed.set_defaults(func=_cmd_seed)

    run = subcommands.add_parser("run", help="evaluate golden_datasets through the gateway")
    run.add_argument("--postgres-dsn", default=os.getenv("POSTGRES_DSN", DEFAULT_POSTGRES_DSN))
    run.add_argument("--gateway-url", default=DEFAULT_GATEWAY_URL)
    run.add_argument("--output", required=True)
    run.add_argument("--markdown-output")
    run.add_argument("--html-output")
    run.add_argument("--timeout", type=float, default=10.0)
    run.add_argument("--openai-api-key", default="")
    run.add_argument("--judge-model", default=os.getenv("REGRESSION_JUDGE_MODEL", "gpt-4o-mini"))
    run.add_argument("--run-label", default="")
    run.add_argument("--revision", default="")
    run.set_defaults(func=_cmd_run)

    compare = subcommands.add_parser("compare", help="compare baseline and candidate reports")
    compare.add_argument("--baseline", required=True)
    compare.add_argument("--candidate", required=True)
    compare.add_argument("--output", required=True)
    compare.add_argument("--markdown-output")
    compare.add_argument("--html-output")
    compare.add_argument("--max-drop", type=float, default=DEFAULT_MAX_DROP)
    compare.add_argument(
        "--min-accuracy",
        type=float,
        default=float(os.getenv("REGRESSION_MIN_ACCURACY", DEFAULT_MIN_ACCURACY)),
    )
    compare.set_defaults(func=_cmd_compare)

    slack = subcommands.add_parser("slack", help="send a Slack comparison notification")
    slack.add_argument("--comparison", required=True)
    slack.add_argument("--webhook-url", required=True)
    slack.add_argument("--run-url", default="")
    slack.set_defaults(func=_cmd_slack)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except RegressionRunnerError as error:
        print(f"model-regression: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
