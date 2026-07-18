"""Publish privacy-approved runtime interactions for offline log mining.

System role:
    Fail-open bridge from orchestration/feedback events to the Phase 4.1
    PostgreSQL source table without weakening forensic artifact redaction.
Dependencies:
    psycopg2 performs parameterized writes; runtime settings select the sink.
Side effects:
    Enabled sinks open PostgreSQL connections and insert or update redacted
    interaction rows. Disabled sinks perform no I/O.
"""
from __future__ import annotations

import hashlib
import ipaddress
import logging
import re
import unicodedata
from contextlib import closing
from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProductionInteractionLog:
    """One redacted prompt plus bounded arbitration signals."""

    trace_id: str
    feature_scope: str
    redacted_prompt: str
    prompt_fingerprint: str
    arbitration_scores: dict[str, int]
    min_arbitration_score: int | None
    arbitration_status: str


class InteractionLogSink(Protocol):
    """Persistence boundary shared by orchestration and feedback handling."""

    def record_interaction(self, record: ProductionInteractionLog) -> None:
        """Upsert one redacted interaction without clearing prior feedback."""
        ...

    def flag_negative_feedback(self, trace_id: str) -> None:
        """Flag an existing trace without accepting feedback content."""
        ...


class NoOpInteractionLogSink:
    """Disabled-by-default sink that intentionally performs no I/O."""

    def record_interaction(self, record: ProductionInteractionLog) -> None:
        return None

    def flag_negative_feedback(self, trace_id: str) -> None:
        return None


class PromptRedactor:
    """Apply privacy-biased built-in and deployment-specific regex rules."""

    _BUILT_INS: tuple[tuple[str, str], ...] = (
        (
            "CREDENTIAL",
            r"(?i)\b(?:authorization\s*:\s*)?bearer\s+[a-z0-9._~+/=-]{8,}",
        ),
        ("CREDENTIAL", r"\b(?:sk|pk)-(?:proj-)?[A-Za-z0-9_-]{12,}\b"),
        ("PHONE", r"(?<!\w)\+[1-9]\d{9,14}(?!\w)"),
        (
            "PHONE",
            r"(?<!\w)(?:1[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}(?!\w)",
        ),
        ("PAYMENT", r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)"),
        ("GOVERNMENT_ID", r"(?<!\d)\d{3}[- ]\d{2}[- ]\d{4}(?!\d)"),
        ("GOVERNMENT_ID", r"(?<!\d)\d{2}-\d{7}(?!\d)"),
        ("EMAIL", r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
        (
            "IP_ADDRESS",
            r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}"
            r"(?:25[0-5]|2[0-4]\d|1?\d?\d)\b",
        ),
        (
            "PHONE",
            r"(?<!\w)(?:\+?\d{1,3}[ .-]?)?(?:\(?\d{3}\)?[ .-]?)"
            r"\d{3}[ .-]\d{4}(?!\w)",
        ),
    )
    _CREDENTIAL_ASSIGNMENT = re.compile(
        r'''(?ix)
        (?P<prefix>
            (?:
                "(?:api[-_ ]?key|access[-_ ]?token|client[-_ ]?secret|password|passwd|secret)"
                | '(?:api[-_ ]?key|access[-_ ]?token|client[-_ ]?secret|password|passwd|secret)'
                | \b(?:api[-_ ]?key|access[-_ ]?token|client[-_ ]?secret|password|passwd|secret)\b
            )
            \s*[:=]\s*
        )
        (?P<value>
            "(?:\\.|[^"\\\r\n])*"
            | '(?:\\.|[^'\\\r\n])*'
            | [^\s,;}\]]{4,}
        )
        '''
    )
    _IPV6_CANDIDATE = re.compile(
        r"(?i)(?<![0-9a-z_:])(?:[0-9a-f]{0,4}:){2,7}"
        r"[0-9a-f]{0,4}(?![0-9a-z_:])"
    )
    _IPV4_EMBEDDED_IPV6_CANDIDATE = re.compile(
        r"(?i)(?<![0-9a-z_:])(?:[0-9a-f]{0,4}:){2,7}"
        r"(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}"
        r"(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![0-9a-z_:])"
    )

    def __init__(self, custom_patterns: Sequence[str] | None = None) -> None:
        self._patterns: list[tuple[str, re.Pattern[str]]] = [
            (label, re.compile(pattern)) for label, pattern in self._BUILT_INS
        ]
        for pattern in custom_patterns or ():
            try:
                compiled = re.compile(pattern)
                if compiled.match("") is not None:
                    log.warning("ignoring production-log redaction regex that matches empty text")
                    continue
                self._patterns.append(("CUSTOM", compiled))
            except re.error as exc:
                # A bad optional rule must not disable the built-in boundary.
                log.warning("ignoring invalid production-log redaction regex: %s", exc)

    def redact(self, prompt: str) -> str:
        """Replace every configured match with a stable categorical marker."""

        redacted = self._CREDENTIAL_ASSIGNMENT.sub(
            self._replace_credential_assignment,
            str(prompt),
        )
        redacted = self._IPV4_EMBEDDED_IPV6_CANDIDATE.sub(
            self._replace_ipv6, redacted
        )
        redacted = self._IPV6_CANDIDATE.sub(self._replace_ipv6, redacted)
        for label, pattern in self._patterns:
            redacted = pattern.sub(f"[REDACTED_{label}]", redacted)
        return redacted

    @staticmethod
    def _replace_credential_assignment(match: re.Match[str]) -> str:
        """Preserve assignment/JSON syntax while replacing only the value."""

        value = match.group("value")
        marker = "[REDACTED_CREDENTIAL]"
        if value and value[0] in {'"', "'"}:
            marker = f"{value[0]}{marker}{value[0]}"
        return f"{match.group('prefix')}{marker}"

    @staticmethod
    def _replace_ipv6(match: re.Match[str]) -> str:
        """Redact only candidates accepted by the standard IPv6 parser."""

        try:
            ipaddress.IPv6Address(match.group(0))
        except ipaddress.AddressValueError:
            return match.group(0)
        return "[REDACTED_IP_ADDRESS]"


class PostgresInteractionLogSink:
    """Short-lived, transaction-scoped PostgreSQL production-log writer."""

    def __init__(
        self,
        dsn: str,
        *,
        connect_timeout_seconds: int = 3,
        statement_timeout_ms: int = 3_000,
    ) -> None:
        if connect_timeout_seconds < 1:
            raise ValueError("connect_timeout_seconds must be at least 1")
        if statement_timeout_ms < 1:
            raise ValueError("statement_timeout_ms must be at least 1")
        self.dsn = dsn
        self.connect_timeout_seconds = int(connect_timeout_seconds)
        self.statement_timeout_ms = int(statement_timeout_ms)

    def _connect(self):
        import psycopg2  # noqa: PLC0415

        return psycopg2.connect(
            self.dsn,
            connect_timeout=self.connect_timeout_seconds,
        )

    def _set_statement_timeout(self, cursor) -> None:
        cursor.execute(
            "SET LOCAL statement_timeout = %s",
            (self.statement_timeout_ms,),
        )

    def record_interaction(self, record: ProductionInteractionLog) -> None:
        """Upsert an interaction while preserving an existing feedback flag."""

        from psycopg2.extras import Json  # noqa: PLC0415

        statement = """
            INSERT INTO production_interaction_logs (
                trace_id, feature_scope, redacted_prompt, prompt_fingerprint,
                arbitration_scores, min_arbitration_score,
                arbitration_status, negative_feedback
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, FALSE)
            ON CONFLICT (trace_id) DO UPDATE SET
                feature_scope = EXCLUDED.feature_scope,
                redacted_prompt = EXCLUDED.redacted_prompt,
                prompt_fingerprint = EXCLUDED.prompt_fingerprint,
                arbitration_scores = CASE
                    WHEN EXCLUDED.arbitration_status = 'pending'
                         AND production_interaction_logs.arbitration_status IS NOT NULL
                         AND production_interaction_logs.arbitration_status <> 'pending'
                    THEN production_interaction_logs.arbitration_scores
                    ELSE EXCLUDED.arbitration_scores
                END,
                min_arbitration_score = CASE
                    WHEN EXCLUDED.arbitration_status = 'pending'
                         AND production_interaction_logs.arbitration_status IS NOT NULL
                         AND production_interaction_logs.arbitration_status <> 'pending'
                    THEN production_interaction_logs.min_arbitration_score
                    ELSE EXCLUDED.min_arbitration_score
                END,
                arbitration_status = CASE
                    WHEN EXCLUDED.arbitration_status = 'pending'
                         AND production_interaction_logs.arbitration_status IS NOT NULL
                         AND production_interaction_logs.arbitration_status <> 'pending'
                    THEN production_interaction_logs.arbitration_status
                    ELSE EXCLUDED.arbitration_status
                END,
                updated_at = NOW()
        """
        with closing(self._connect()) as connection:
            with connection:
                with connection.cursor() as cursor:
                    self._set_statement_timeout(cursor)
                    cursor.execute(
                        statement,
                        (
                            record.trace_id,
                            record.feature_scope,
                            record.redacted_prompt,
                            record.prompt_fingerprint,
                            Json(record.arbitration_scores),
                            record.min_arbitration_score,
                            record.arbitration_status,
                        ),
                    )

    def flag_negative_feedback(self, trace_id: str) -> None:
        """Set only the feedback flag and timestamp for an existing trace."""

        with closing(self._connect()) as connection:
            with connection:
                with connection.cursor() as cursor:
                    self._set_statement_timeout(cursor)
                    cursor.execute(
                        """
                        UPDATE production_interaction_logs
                        SET negative_feedback = TRUE, updated_at = NOW()
                        WHERE trace_id = %s
                        """,
                        (trace_id,),
                    )


def build_production_interaction(
    *,
    trace_id: str,
    feature_scope: str,
    prompt: str,
    arbitration_scores: Mapping[str, int] | None,
    arbitration_status: str,
    redactor: PromptRedactor,
) -> ProductionInteractionLog:
    """Redact and validate one interaction before it crosses into PostgreSQL."""

    normalized_trace_id = str(trace_id).strip()
    normalized_feature_scope = str(feature_scope).strip()
    redacted_prompt = redactor.redact(prompt)
    normalized = " ".join(unicodedata.normalize("NFKC", redacted_prompt).split())
    if not normalized_trace_id or not normalized_feature_scope or not normalized:
        raise ValueError("trace_id, feature_scope, and redacted prompt are required")

    scores = {str(key): int(value) for key, value in (arbitration_scores or {}).items()}
    if any(value < 1 or value > 5 for value in scores.values()):
        raise ValueError("arbitration scores must be between 1 and 5")

    return ProductionInteractionLog(
        trace_id=normalized_trace_id,
        feature_scope=normalized_feature_scope,
        redacted_prompt=redacted_prompt,
        prompt_fingerprint=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        arbitration_scores=scores,
        min_arbitration_score=min(scores.values()) if scores else None,
        arbitration_status=arbitration_status,
    )


def configured_interaction_log_sink() -> InteractionLogSink:
    """Build the configured sink without opening a database connection."""

    from src.config import settings  # noqa: PLC0415

    if not settings.production_interaction_logging_enabled:
        return NoOpInteractionLogSink()
    return PostgresInteractionLogSink(
        settings.postgres_dsn,
        connect_timeout_seconds=settings.production_log_connect_timeout_seconds,
        statement_timeout_ms=settings.production_log_statement_timeout_ms,
    )


def configured_prompt_redactor() -> PromptRedactor:
    """Build the redactor from validated runtime settings."""

    from src.config import settings  # noqa: PLC0415

    return PromptRedactor(settings.production_log_redaction_patterns)


__all__ = [
    "InteractionLogSink",
    "NoOpInteractionLogSink",
    "PostgresInteractionLogSink",
    "ProductionInteractionLog",
    "PromptRedactor",
    "build_production_interaction",
    "configured_interaction_log_sink",
    "configured_prompt_redactor",
]
