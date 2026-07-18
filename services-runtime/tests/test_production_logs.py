"""Privacy and contract tests for the opt-in production interaction sink.

System role:
    Protects the redaction and score-mining boundary before runtime prompts can
    enter the Phase 4.1 PostgreSQL source table.
Dependencies:
    Standard-library assertions and the production-log value builder.
Side effects:
    None; no database or provider is contacted.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.tracing.production_logs import (  # noqa: E402
    PostgresInteractionLogSink,
    PromptRedactor,
    build_production_interaction,
)


def test_prompt_redactor_removes_builtin_and_custom_sensitive_values():
    redactor = PromptRedactor([r"CUSTOM-\d+"])
    prompt = (
        "Email alice@example.com or +1 (415) 555-0199 from 192.168.1.9 "
        "or 2001:db8::1. SSN 123-45-6789 card 4111 1111 1111 1111. "
        "Authorization: Bearer abcdefghijklmnop and CUSTOM-991."
    )

    redacted = redactor.redact(prompt)

    for secret in (
        "alice@example.com",
        "+1 (415) 555-0199",
        "192.168.1.9",
        "2001:db8::1",
        "123-45-6789",
        "4111 1111 1111 1111",
        "abcdefghijklmnop",
        "CUSTOM-991",
    ):
        assert secret not in redacted
    assert "[REDACTED_CREDENTIAL]" in redacted
    assert "[REDACTED_CUSTOM]" in redacted


def test_interaction_builder_extracts_min_score_and_stable_fingerprint():
    redactor = PromptRedactor()
    first = build_production_interaction(
        trace_id="trace-1",
        feature_scope="billing",
        prompt="Why   is my invoice wrong?",
        arbitration_scores={"factual": 5, "logic": 2, "completeness": 4},
        arbitration_status="blocked",
        redactor=redactor,
    )
    second = build_production_interaction(
        trace_id="trace-2",
        feature_scope="billing",
        prompt="Why is my invoice wrong?",
        arbitration_scores={},
        arbitration_status="passed",
        redactor=redactor,
    )

    assert first.min_arbitration_score == 2
    assert second.min_arbitration_score is None
    assert len(first.prompt_fingerprint) == 64
    assert first.prompt_fingerprint == second.prompt_fingerprint


def test_interaction_builder_normalizes_identifiers_and_rejects_blank_fields():
    redactor = PromptRedactor()
    record = build_production_interaction(
        trace_id=" trace-1 ",
        feature_scope=" support ",
        prompt="safe prompt",
        arbitration_scores=None,
        arbitration_status="pending",
        redactor=redactor,
    )
    assert record.trace_id == "trace-1"
    assert record.feature_scope == "support"

    for kwargs in (
        {"trace_id": "   ", "feature_scope": "support", "prompt": "safe"},
        {"trace_id": "trace", "feature_scope": "   ", "prompt": "safe"},
        {"trace_id": "trace", "feature_scope": "support", "prompt": "   "},
    ):
        with pytest.raises(ValueError, match="required"):
            build_production_interaction(
                **kwargs,
                arbitration_scores=None,
                arbitration_status="pending",
                redactor=redactor,
            )


def test_redactor_handles_compact_phone_without_corrupting_colon_syntax():
    redactor = PromptRedactor([""])
    redacted = redactor.redact(
        "Call +14155550199 at 10:30:45 from namespace::method or 2001:db8::1"
    )

    assert "+14155550199" not in redacted
    assert "2001:db8::1" not in redacted
    assert "10:30:45" in redacted
    assert "namespace::method" in redacted
    assert "[REDACTED_CUSTOM]" not in redacted


def test_redactor_handles_compact_phones_and_ipv4_embedded_ipv6():
    redactor = PromptRedactor()
    prompt = (
        "Call 4155550199, 14155550199, or (415)5550199; "
        "mapped addresses ::ffff:192.168.1.1 and ::192.0.2.1."
    )

    redacted = redactor.redact(prompt)

    for secret in (
        "4155550199",
        "14155550199",
        "(415)5550199",
        "::ffff:192.168.1.1",
        "::192.0.2.1",
        ".168.1.1",
    ):
        assert secret not in redacted
    assert redacted.count("[REDACTED_PHONE]") == 3
    assert redacted.count("[REDACTED_IP_ADDRESS]") == 2


def test_prompt_redactor_preserves_json_syntax_while_redacting_credentials():
    redactor = PromptRedactor()
    prompt = (
        '{"api_key" : "vendor-secret-abcdef123456", '
        '"password": "correct horse battery staple", '
        '"safe": "leave me"}; '
        "client_secret = 'another private value'"
    )

    redacted = redactor.redact(prompt)

    assert '"api_key" : "[REDACTED_CREDENTIAL]"' in redacted
    assert '"password": "[REDACTED_CREDENTIAL]"' in redacted
    assert "client_secret = '[REDACTED_CREDENTIAL]'" in redacted
    assert '"safe": "leave me"' in redacted
    for secret in (
        "vendor-secret-abcdef123456",
        "correct horse battery staple",
        "another private value",
    ):
        assert secret not in redacted


def test_postgres_sink_bounds_both_writes_and_preserves_terminal_arbitration():
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    record = build_production_interaction(
        trace_id="trace-timeout",
        feature_scope="support",
        prompt="Safe prompt",
        arbitration_scores={"logic": 2},
        arbitration_status="blocked",
        redactor=PromptRedactor(),
    )
    dsn = "postgresql://example.invalid/coremesh"
    sink = PostgresInteractionLogSink(
        dsn,
        connect_timeout_seconds=2,
        statement_timeout_ms=1_250,
    )

    with patch("psycopg2.connect", return_value=connection) as connect:
        sink.record_interaction(record)
        sink.flag_negative_feedback(record.trace_id)

    connect.assert_has_calls(
        [call(dsn, connect_timeout=2), call(dsn, connect_timeout=2)]
    )
    assert cursor.execute.call_args_list[0] == call(
        "SET LOCAL statement_timeout = %s", (1_250,)
    )
    assert cursor.execute.call_args_list[2] == call(
        "SET LOCAL statement_timeout = %s", (1_250,)
    )
    upsert = cursor.execute.call_args_list[1].args[0]
    assert "EXCLUDED.arbitration_status = 'pending'" in upsert
    assert "THEN production_interaction_logs.arbitration_scores" in upsert
    assert "THEN production_interaction_logs.min_arbitration_score" in upsert
    assert "THEN production_interaction_logs.arbitration_status" in upsert
    assert "UPDATE production_interaction_logs" in cursor.execute.call_args_list[3].args[0]
