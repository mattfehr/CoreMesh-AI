"""Unit tests for the Phase 4.2 model-regression evaluator."""
from __future__ import annotations

import pytest

from src.model_regression import runner


def _case(expected_output, *, case_id="case-1", fingerprint="f" * 64):
    return runner.GoldenCase(
        case_id=case_id,
        source_fingerprint=fingerprint,
        feature_scope="gateway_autopilot",
        user_input="hello",
        expected_output=expected_output,
        difficulty_rating="simple",
        origin_source="human_curated",
    )


def _http(*, headers=None, body="{}", status=200):
    return runner.HttpResult(
        status=status,
        headers=headers or {},
        body=body,
        latency_ms=12.5,
    )


def test_headers_scoring_matches_case_insensitive_response_headers(monkeypatch):
    case = _case(
        {
            "scoring": {
                "mode": "headers",
                "expect": {
                    "X-CoreMesh-Autopilot-Tier": "tier-1",
                    "X-CoreMesh-Cache-Policy": "allow",
                },
            }
        }
    )
    monkeypatch.setattr(
        runner,
        "send_gateway_request",
        lambda **kwargs: (
            _http(
                headers={
                    "x-coremesh-autopilot-tier": "tier-1",
                    "X-CoreMesh-Cache-Policy": "allow",
                }
            ),
            "/v1/chat/completions",
        ),
    )

    result = runner.evaluate_case(
        case=case,
        gateway_url="http://gateway",
        timeout=1,
        openai_api_key="",
        judge_model="judge",
    )

    assert result["mode"] == "headers"
    assert result["passed"] is True
    assert result["score"] == 1.0


def test_headers_scoring_reports_mismatched_headers(monkeypatch):
    case = _case(
        {
            "scoring": {
                "mode": "headers",
                "expect": {"X-CoreMesh-Autopilot-Tier": "tier-3"},
            }
        }
    )
    monkeypatch.setattr(
        runner,
        "send_gateway_request",
        lambda **kwargs: (
            _http(headers={"X-CoreMesh-Autopilot-Tier": "tier-1"}),
            "/v1/chat/completions",
        ),
    )

    result = runner.evaluate_case(
        case=case,
        gateway_url="http://gateway",
        timeout=1,
        openai_api_key="",
        judge_model="judge",
    )

    assert result["passed"] is False
    assert result["score"] == 0.0
    assert "tier-1" in result["failure_reasons"][0]


def test_exact_json_scoring_compares_response_body(monkeypatch):
    case = _case(
        {
            "scoring": {
                "mode": "exact_json",
                "expected_json": {"status": "ok", "service": "coremesh-runtime"},
                "request": {"method": "GET", "path": "/health"},
            }
        }
    )
    monkeypatch.setattr(
        runner,
        "send_gateway_request",
        lambda **kwargs: (
            _http(body='{"status":"ok","service":"coremesh-runtime"}'),
            "/health",
        ),
    )

    result = runner.evaluate_case(
        case=case,
        gateway_url="http://gateway",
        timeout=1,
        openai_api_key="",
        judge_model="judge",
    )

    assert result["mode"] == "exact_json"
    assert result["passed"] is True
    assert result["score"] == 1.0


def test_llm_judge_scoring_uses_injected_judge(monkeypatch):
    case = _case(
        {
            "reference_answer": "Use the safe workflow.",
            "validation_criteria": [{"description": "Mentions the workflow"}],
            "scoring": {"mode": "llm_judge", "pass_threshold": 0.8},
        }
    )
    monkeypatch.setattr(
        runner,
        "send_gateway_request",
        lambda **kwargs: (_http(body="The safe workflow applies."), "/v1/chat"),
    )

    def judge(**kwargs):
        assert kwargs["model_output"] == "The safe workflow applies."
        return runner.JudgeVerdict(passed=True, score=0.91, rationale="matches")

    result = runner.evaluate_case(
        case=case,
        gateway_url="http://gateway",
        timeout=1,
        openai_api_key="",
        judge_model="judge",
        judge=judge,
    )

    assert result["mode"] == "llm_judge"
    assert result["passed"] is True
    assert result["score"] == 0.91


def test_llm_judge_without_api_key_is_a_configuration_failure(monkeypatch):
    case = _case(
        {
            "reference_answer": "Reference",
            "validation_criteria": [{"description": "criterion"}],
        }
    )
    monkeypatch.setattr(
        runner,
        "send_gateway_request",
        lambda **kwargs: (_http(body="candidate output"), "/v1/chat"),
    )

    with pytest.raises(runner.ScoringConfigurationError, match="OPENAI_API_KEY"):
        runner.evaluate_case(
            case=case,
            gateway_url="http://gateway",
            timeout=1,
            openai_api_key="",
            judge_model="judge",
        )


def test_empty_golden_dataset_fails_before_reporting():
    with pytest.raises(runner.NoGoldenCasesError):
        runner.build_report(
            cases=[],
            gateway_url="http://gateway",
            timeout=1,
            openai_api_key="",
            judge_model="judge",
            run_label="head",
            revision="abc",
        )


def test_unsupported_scoring_mode_fails_the_row(monkeypatch):
    case = _case({"scoring": {"mode": "new_metric"}})
    monkeypatch.setattr(
        runner,
        "send_gateway_request",
        lambda **kwargs: (_http(), "/v1/chat"),
    )

    with pytest.raises(runner.UnsupportedScoringModeError):
        runner.evaluate_case(
            case=case,
            gateway_url="http://gateway",
            timeout=1,
            openai_api_key="",
            judge_model="judge",
        )


def _report(accuracy, *, passed=True, scope="gateway_autopilot"):
    score = accuracy
    return {
        "summary": {
            "case_count": 1,
            "passed_count": 1 if passed else 0,
            "failed_count": 0 if passed else 1,
            "accuracy": accuracy,
            "pass_rate": 1.0 if passed else 0.0,
        },
        "by_feature_scope": {
            scope: {
                "case_count": 1,
                "passed_count": 1 if passed else 0,
                "failed_count": 0 if passed else 1,
                "accuracy": accuracy,
                "pass_rate": 1.0 if passed else 0.0,
            }
        },
        "cases": [
            {
                "case_key": "stable-case",
                "feature_scope": scope,
                "passed": passed,
                "score": score,
                "failure_reasons": [] if passed else ["failed"],
            }
        ],
    }


def test_compare_allows_exact_three_point_drop_as_warning():
    comparison = runner.compare_reports(
        baseline=_report(1.0),
        candidate=_report(0.97),
        max_drop=0.03,
        min_accuracy=0.90,
    )

    assert comparison["status"] == "warn"
    assert comparison["checks"][0]["drop"] == 0.03
    assert all(check["status"] != "fail" for check in comparison["checks"])


def test_compare_fails_when_drop_exceeds_three_points():
    comparison = runner.compare_reports(
        baseline=_report(1.0),
        candidate=_report(0.969),
        max_drop=0.03,
        min_accuracy=0.90,
    )

    assert comparison["status"] == "fail"
    assert comparison["checks"][0]["status"] == "fail"


def test_compare_fails_when_both_sides_are_zero_accuracy():
    comparison = runner.compare_reports(
        baseline=_report(0.0, passed=False),
        candidate=_report(0.0, passed=False),
        max_drop=0.03,
        min_accuracy=0.90,
    )

    assert comparison["status"] == "fail"
    floor = next(check for check in comparison["checks"] if check["scope"] == "min_accuracy")
    assert floor["status"] == "fail"


def test_compare_passes_when_candidate_meets_floor_and_drop():
    comparison = runner.compare_reports(
        baseline=_report(0.97),
        candidate=_report(0.95),
        max_drop=0.03,
        min_accuracy=0.90,
    )

    assert comparison["status"] == "warn"
    floor = next(check for check in comparison["checks"] if check["scope"] == "min_accuracy")
    assert floor["status"] == "pass"


def test_compare_fails_when_candidate_accuracy_below_floor():
    comparison = runner.compare_reports(
        baseline=_report(0.88, passed=False),
        candidate=_report(0.85, passed=False),
        max_drop=0.03,
        min_accuracy=0.90,
    )

    assert comparison["status"] == "fail"
    floor = next(check for check in comparison["checks"] if check["scope"] == "min_accuracy")
    assert floor["status"] == "fail"
    assert floor["candidate_accuracy"] == 0.85
