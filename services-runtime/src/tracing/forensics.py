"""OpenTelemetry tracing, local trace artifacts, and failure forensics.

System role:
    Records CoreMesh execution trees without retaining prompt/document bodies,
    persists one inspectable JSON artifact per workflow, indexes trace summaries
    in SQLite, and identifies the earliest degraded causal step.
Dependencies:
    OpenTelemetry supplies real SDK spans; Pydantic defines stable artifact and
    diagnosis contracts; SQLite and JSON persistence use the standard library.
Side effects:
    Enabled workflow traces create files below the configured trace directory
    and update a local SQLite registry. Optional OTLP export can make network
    calls. All tracing failures are isolated from business execution.
"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import inspect
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence, TypeVar

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import Span, SpanKind, Status, StatusCode
from pydantic import BaseModel, Field

from src.tracing.production_logs import (
    InteractionLogSink,
    NoOpInteractionLogSink,
    configured_interaction_log_sink,
)

log = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


class SpanCategory(str, Enum):
    """Stable categories used by instrumentation and root-cause ranking."""

    WORKFLOW = "workflow"
    AGENT = "agent"
    TOOL = "tool"
    DATABASE = "database"
    MODEL = "model"
    ANALYSIS = "analysis"


class FailureTrigger(str, Enum):
    """Events that make a completed trace eligible for diagnosis."""

    EXECUTION_ERROR = "execution_error"
    ARBITRATION_FAILURE = "arbitration_failure"
    NEGATIVE_FEEDBACK = "negative_feedback"


class FailureCategory(str, Enum):
    """Portable failure taxonomy written to the trace registry."""

    EXECUTION_ERROR = "execution_error"
    EXTRACTION_DEGRADATION = "extraction_degradation"
    LOW_CONFIDENCE = "low_confidence"
    PROPAGATION_ERROR = "propagation_error"
    PROMPT_FAILURE = "prompt_failure"
    CONTEXT_LOSS = "context_loss"
    ARBITRATION_FAILURE = "arbitration_failure"
    UNKNOWN = "unknown"


class TraceEvidence(BaseModel):
    """One privacy-safe signal supporting a root-cause diagnosis."""

    signal: str
    observed: str | float | int | bool | None = None
    threshold: str | float | int | bool | None = None


class RootCauseDiagnosis(BaseModel):
    """Structured explanation of the first degraded execution step."""

    trace_id: str
    span_id: str
    span_name: str
    step_id: str | None = None
    category: FailureCategory
    explanation: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[TraceEvidence] = Field(default_factory=list)
    analyzer: str = "deterministic"


class SerializedSpan(BaseModel):
    """JSON-safe OpenTelemetry span projection used by the analyzer/UI."""

    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    name: str
    kind: str
    status: str
    start_time: str
    end_time: str
    duration_ms: float
    attributes: dict[str, Any] = Field(default_factory=dict)
    events: list[dict[str, Any]] = Field(default_factory=list)


class ForensicTraceArtifact(BaseModel):
    """Versioned trace visualization persisted as one JSON document."""

    schema_version: str = "1.0"
    trace_id: str
    status: str
    trigger: FailureTrigger | None = None
    trigger_reasons: list[str] = Field(default_factory=list)
    started_at: str
    ended_at: str
    duration_ms: float
    final_confidence: float | None = None
    summary: dict[str, Any] = Field(default_factory=dict)
    diagnosis: RootCauseDiagnosis | None = None
    spans: list[SerializedSpan] = Field(default_factory=list)
    tree: dict[str, Any] | list[dict[str, Any]] = Field(default_factory=dict)
    feedback: dict[str, Any] | None = None


class StepQualityJudge(Protocol):
    """Optional fallback judge; input contains sanitized span metadata only."""

    def judge(
        self,
        spans: Sequence[SerializedSpan],
        trigger: FailureTrigger,
    ) -> RootCauseDiagnosis | Mapping[str, Any] | None:
        ...


# Leaf segments / compound suffixes that carry bodies or secrets.
# Matching is suffix/leaf based so allowlisted metrics like
# coremesh.sql.limit_applied and input_tokens are not hashed.
_SENSITIVE_LEAF_SEGMENTS = frozenset(
    {
        "authorization",
        "document",
        "input",
        "message",
        "output",
        "password",
        "prompt",
        "secret",
        "sql",
        "stacktrace",
        "text",
        "token",
        "user_id",
    }
)
_SENSITIVE_KEY_SUFFIXES = (
    "exception.message",
    "exception.stacktrace",
    "query.text",
    "redis.key",
)
_PRESERVED_KEY_SUFFIXES = (
    ".sha256",
    ".length",
    ".token_count",
    ".input_tokens",
    ".output_tokens",
)

# Highest-priority deterministic signal wins when one span has several.
_CATEGORY_PRECEDENCE: dict[FailureCategory, int] = {
    FailureCategory.EXECUTION_ERROR: 0,
    FailureCategory.EXTRACTION_DEGRADATION: 1,
    FailureCategory.LOW_CONFIDENCE: 2,
    FailureCategory.ARBITRATION_FAILURE: 3,
    FailureCategory.PROPAGATION_ERROR: 4,
    FailureCategory.PROMPT_FAILURE: 4,
    FailureCategory.CONTEXT_LOSS: 4,
    FailureCategory.UNKNOWN: 99,
}


def content_metadata(prefix: str, value: Any) -> dict[str, Any]:
    """Return a stable fingerprint and length instead of sensitive content."""

    serialized = _stable_text(value)
    return {
        f"{prefix}.sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        f"{prefix}.length": len(serialized),
    }


def _stable_text(value: Any) -> str:
    if isinstance(value, bytes | bytearray):
        return bytes(value).hex()
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def _is_sensitive_attribute_key(key_lower: str) -> bool:
    """Return True for body/secret keys without substring false positives."""

    if key_lower.endswith(_PRESERVED_KEY_SUFFIXES) or key_lower in {
        "input_tokens",
        "output_tokens",
        "token_count",
    }:
        return False
    if any(
        key_lower == suffix or key_lower.endswith(f".{suffix}")
        for suffix in _SENSITIVE_KEY_SUFFIXES
    ):
        return True
    leaf = key_lower.rsplit(".", 1)[-1]
    return leaf in _SENSITIVE_LEAF_SEGMENTS


def _safe_attributes(
    attributes: Mapping[str, Any] | None,
    *,
    max_length: int,
) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for raw_key, raw_value in (attributes or {}).items():
        key = str(raw_key)
        key_lower = key.lower()
        if _is_sensitive_attribute_key(key_lower):
            safe.update(content_metadata(key, raw_value))
            continue
        value = _otel_value(raw_value, max_length=max_length)
        if value is not None:
            safe[key] = value
    return safe


def _prefer_category(
    current: FailureCategory,
    candidate: FailureCategory,
) -> FailureCategory:
    """Keep the higher-precedence failure category for a single span."""

    if current == FailureCategory.UNKNOWN:
        return candidate
    if _CATEGORY_PRECEDENCE.get(candidate, 99) < _CATEGORY_PRECEDENCE.get(current, 99):
        return candidate
    return current


def _otel_value(value: Any, *, max_length: int) -> Any:
    if value is None:
        return None
    if isinstance(value, Enum):
        return _otel_value(value.value, max_length=max_length)
    if isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value[:max_length]
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        items = [_otel_value(item, max_length=max_length) for item in value]
        return [item for item in items if isinstance(item, bool | int | float | str)]
    return _stable_text(value)[:max_length]


def _iso_time(nanoseconds: int | None) -> str:
    seconds = (nanoseconds or time.time_ns()) / 1_000_000_000
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


def _span_id(span_id: int | None) -> str | None:
    return f"{span_id:016x}" if span_id else None


def _trace_id(trace_id: int | None) -> str:
    return f"{trace_id or 0:032x}"


class _CollectingSpanExporter(SpanExporter):
    """Thread-safe ended-span collector for explicitly registered traces."""

    def __init__(self, *, max_attribute_length: int) -> None:
        self.max_attribute_length = max_attribute_length
        self._active: set[str] = set()
        self._spans: dict[str, list[SerializedSpan]] = {}
        self._lock = threading.RLock()

    def activate(self, trace_id: str) -> None:
        with self._lock:
            self._active.add(trace_id)
            self._spans.setdefault(trace_id, [])

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            with self._lock:
                for span in spans:
                    trace_id = _trace_id(span.context.trace_id if span.context else None)
                    if trace_id not in self._active:
                        continue
                    self._spans.setdefault(trace_id, []).append(self._serialize(span))
        except Exception:
            log.exception("failed to collect forensic OpenTelemetry span")
            return SpanExportResult.FAILURE
        return SpanExportResult.SUCCESS

    def pop(self, trace_id: str) -> list[SerializedSpan]:
        with self._lock:
            self._active.discard(trace_id)
            return self._spans.pop(trace_id, [])

    def shutdown(self) -> None:
        with self._lock:
            self._active.clear()
            self._spans.clear()

    def _serialize(self, span: ReadableSpan) -> SerializedSpan:
        context = span.context
        parent = span.parent
        start_ns = span.start_time or time.time_ns()
        end_ns = span.end_time or start_ns
        events = [
            {
                "name": event.name,
                "timestamp": _iso_time(event.timestamp),
                "attributes": _safe_attributes(
                    event.attributes,
                    max_length=self.max_attribute_length,
                ),
            }
            for event in span.events
        ]
        return SerializedSpan(
            trace_id=_trace_id(context.trace_id if context else None),
            span_id=_span_id(context.span_id if context else None) or "0000000000000000",
            parent_span_id=_span_id(parent.span_id if parent else None),
            name=span.name,
            kind=span.kind.name,
            status=span.status.status_code.name,
            start_time=_iso_time(start_ns),
            end_time=_iso_time(end_ns),
            duration_ms=round((end_ns - start_ns) / 1_000_000, 3),
            attributes=_safe_attributes(
                span.attributes,
                max_length=self.max_attribute_length,
            ),
            events=events,
        )


class RootCauseAnalyzer:
    """Backward analyzer for errors and significant quality degradation."""

    def __init__(
        self,
        *,
        confidence_threshold: float = 0.6,
        confidence_drop_threshold: float = 0.2,
        judge: StepQualityJudge | None = None,
    ) -> None:
        self.confidence_threshold = confidence_threshold
        self.confidence_drop_threshold = confidence_drop_threshold
        self.judge = judge

    def analyze(
        self,
        trace_id: str,
        spans: Sequence[SerializedSpan],
        trigger: FailureTrigger,
    ) -> RootCauseDiagnosis:
        ordered = sorted(spans, key=lambda item: (item.start_time, item.span_id))
        candidates = self._candidates(ordered)
        candidates = self._remove_wrapper_duplicates(candidates, ordered)
        if candidates:
            span, evidence, category = sorted(
                candidates,
                key=lambda item: (
                    _sequence(item[0]),
                    item[0].start_time,
                    -_depth(item[0], ordered),
                ),
            )[0]
            return RootCauseDiagnosis(
                trace_id=trace_id,
                span_id=span.span_id,
                span_name=span.name,
                step_id=_string_attribute(span, "coremesh.step.id"),
                category=category,
                explanation=_explanation(span, category),
                confidence=0.98 if span.status == "ERROR" else 0.85,
                evidence=evidence,
            )

        if self.judge is not None:
            try:
                judgment = self.judge.judge(ordered, trigger)
                if judgment is not None:
                    diagnosis = RootCauseDiagnosis.model_validate(judgment)
                    return diagnosis.model_copy(update={"analyzer": "optional_judge"})
            except Exception:
                log.exception("optional forensic quality judge failed")

        fallback = self._fallback_span(ordered, trigger)
        return RootCauseDiagnosis(
            trace_id=trace_id,
            span_id=fallback.span_id,
            span_name=fallback.name,
            step_id=_string_attribute(fallback, "coremesh.step.id"),
            category=(
                FailureCategory.ARBITRATION_FAILURE
                if trigger == FailureTrigger.ARBITRATION_FAILURE
                else FailureCategory.UNKNOWN
            ),
            explanation="No earlier deterministic degradation signal was available; the terminal trigger is the best suspect.",
            confidence=0.35,
            evidence=[TraceEvidence(signal="terminal_trigger", observed=trigger.value)],
        )

    def _candidates(
        self,
        spans: Sequence[SerializedSpan],
    ) -> list[tuple[SerializedSpan, list[TraceEvidence], FailureCategory]]:
        results: list[tuple[SerializedSpan, list[TraceEvidence], FailureCategory]] = []
        previous_confidence: float | None = None

        for span in spans:
            evidence: list[TraceEvidence] = []
            category = FailureCategory.UNKNOWN
            attrs = span.attributes

            if span.status == "ERROR":
                evidence.append(TraceEvidence(signal="span_status", observed="ERROR"))
                category = _prefer_category(category, FailureCategory.EXECUTION_ERROR)

            if attrs.get("coremesh.quality.validation_passed") is False:
                evidence.append(
                    TraceEvidence(
                        signal="extraction_validation",
                        observed=attrs.get("coremesh.quality.validation_delta"),
                        threshold=attrs.get("coremesh.quality.validation_tolerance"),
                    )
                )
                category = _prefer_category(
                    category, FailureCategory.EXTRACTION_DEGRADATION
                )

            variance = _float_attribute(span, "coremesh.quality.ocr_variance")
            variance_threshold = _float_attribute(span, "coremesh.quality.ocr_threshold")
            recovered = attrs.get("coremesh.quality.vision_recovered") is True
            if (
                variance is not None
                and variance_threshold is not None
                and variance > variance_threshold
                and not recovered
            ):
                evidence.append(
                    TraceEvidence(
                        signal="ocr_variance",
                        observed=variance,
                        threshold=variance_threshold,
                    )
                )
                category = _prefer_category(
                    category, FailureCategory.EXTRACTION_DEGRADATION
                )

            confidence = _float_attribute(span, "coremesh.quality.confidence")
            if confidence is not None:
                if confidence < self.confidence_threshold:
                    evidence.append(
                        TraceEvidence(
                            signal="low_confidence",
                            observed=confidence,
                            threshold=self.confidence_threshold,
                        )
                    )
                    category = _prefer_category(
                        category, FailureCategory.LOW_CONFIDENCE
                    )
                if (
                    previous_confidence is not None
                    and previous_confidence - confidence >= self.confidence_drop_threshold
                ):
                    evidence.append(
                        TraceEvidence(
                            signal="confidence_drop",
                            observed=round(previous_confidence - confidence, 4),
                            threshold=self.confidence_drop_threshold,
                        )
                    )
                    category = _prefer_category(
                        category, FailureCategory.LOW_CONFIDENCE
                    )
                previous_confidence = confidence

            if attrs.get("coremesh.degraded") is True and not evidence:
                evidence.append(TraceEvidence(signal="degraded_flag", observed=True))

            if attrs.get("coremesh.arbitration.failed") is True:
                evidence.append(TraceEvidence(signal="arbitration_failure", observed=True))
                category = _prefer_category(
                    category, FailureCategory.ARBITRATION_FAILURE
                )

            if evidence:
                results.append((span, evidence, category))

        return results

    @staticmethod
    def _remove_wrapper_duplicates(
        candidates: Sequence[tuple[SerializedSpan, list[TraceEvidence], FailureCategory]],
        spans: Sequence[SerializedSpan],
    ) -> list[tuple[SerializedSpan, list[TraceEvidence], FailureCategory]]:
        candidate_ids = {item[0].span_id for item in candidates}
        parents = {span.span_id: span.parent_span_id for span in spans}
        wrappers: set[str] = set()
        for candidate_id in candidate_ids:
            parent_id = parents.get(candidate_id)
            while parent_id:
                if parent_id in candidate_ids:
                    wrappers.add(parent_id)
                parent_id = parents.get(parent_id)
        return [item for item in candidates if item[0].span_id not in wrappers]

    @staticmethod
    def _fallback_span(
        spans: Sequence[SerializedSpan],
        trigger: FailureTrigger,
    ) -> SerializedSpan:
        if not spans:
            now = datetime.now(timezone.utc).isoformat()
            return SerializedSpan(
                trace_id="0" * 32,
                span_id="0" * 16,
                name="coremesh.forensics.missing_trace",
                kind="INTERNAL",
                status="UNSET",
                start_time=now,
                end_time=now,
                duration_ms=0,
            )
        if trigger == FailureTrigger.ARBITRATION_FAILURE:
            arbitration = [span for span in spans if "arbitration" in span.name]
            if arbitration:
                return arbitration[-1]
        errors = [span for span in spans if span.status == "ERROR"]
        return (errors or list(spans))[-1]


@dataclass
class TraceExecution:
    """Mutable workflow handle populated when its root span is finalized."""

    trace_id: str | None = None
    trigger: FailureTrigger | None = None
    trigger_reasons: list[str] | None = None
    status: str = "completed"
    final_confidence: float | None = None
    artifact: ForensicTraceArtifact | None = None
    diagnosis: RootCauseDiagnosis | None = None

    def set_outcome(
        self,
        *,
        status: str,
        trigger: FailureTrigger | None = None,
        reasons: Sequence[str] | None = None,
        final_confidence: float | None = None,
    ) -> None:
        self.status = status
        self.trigger = trigger
        self.trigger_reasons = list(reasons or [])
        self.final_confidence = final_confidence


_ACTIVE_FORENSICS: contextvars.ContextVar[ForensicsTracer | None] = contextvars.ContextVar(
    "coremesh_active_forensics",
    default=None,
)
_DEFAULT_FORENSICS: ForensicsTracer | None = None
_DEFAULT_LOCK = threading.Lock()


class ForensicsTracer:
    """OpenTelemetry facade plus JSON/SQLite forensic persistence."""

    def __init__(
        self,
        *,
        trace_directory: str | Path = ".traces",
        registry_path: str | Path | None = None,
        enabled: bool = True,
        confidence_threshold: float = 0.6,
        confidence_drop_threshold: float = 0.2,
        max_attribute_length: int = 256,
        judge: StepQualityJudge | None = None,
        otlp_endpoint: str | None = None,
        interaction_log_sink: InteractionLogSink | None = None,
    ) -> None:
        self.trace_directory = Path(trace_directory)
        self.registry_path = Path(registry_path or self.trace_directory / "registry.sqlite3")
        self.enabled = enabled
        self.interaction_log_sink = interaction_log_sink or NoOpInteractionLogSink()
        self.max_attribute_length = max(32, max_attribute_length)
        self.analyzer = RootCauseAnalyzer(
            confidence_threshold=confidence_threshold,
            confidence_drop_threshold=confidence_drop_threshold,
            judge=judge,
        )
        self._exporter = _CollectingSpanExporter(
            max_attribute_length=self.max_attribute_length
        )
        self.provider = TracerProvider(
            resource=Resource.create({"service.name": "coremesh-runtime"})
        )
        self.provider.add_span_processor(SimpleSpanProcessor(self._exporter))
        self._configure_otlp(otlp_endpoint)
        self.tracer = self.provider.get_tracer("coremesh.forensics", "1.0")
        self._storage_lock = threading.RLock()

    @contextmanager
    def execution(
        self,
        name: str,
        *,
        attributes: Mapping[str, Any] | None = None,
    ) -> Iterator[TraceExecution]:
        """Start a registered root execution and persist it when complete."""

        handle = TraceExecution(trigger_reasons=[])
        if not self.enabled:
            # Stable ID so opt-in production logs and later feedback can correlate
            # even when forensic JSON artifacts are disabled.
            handle.trace_id = uuid.uuid4().hex
            disabled_token = _ACTIVE_FORENSICS.set(self)
            try:
                yield handle
            finally:
                _ACTIVE_FORENSICS.reset(disabled_token)
            return

        root_attributes = {
            "coremesh.span.category": SpanCategory.WORKFLOW.value,
            **_safe_attributes(attributes, max_length=self.max_attribute_length),
        }
        token: contextvars.Token[ForensicsTracer | None] | None = None
        try:
            with self.tracer.start_as_current_span(
                name,
                kind=SpanKind.INTERNAL,
                attributes=root_attributes,
                record_exception=False,
                set_status_on_exception=False,
            ) as root_span:
                handle.trace_id = _trace_id(root_span.get_span_context().trace_id)
                self._exporter.activate(handle.trace_id)
                token = _ACTIVE_FORENSICS.set(self)
                try:
                    yield handle
                except BaseException as exc:
                    handle.set_outcome(
                        status="failed",
                        trigger=FailureTrigger.EXECUTION_ERROR,
                        reasons=[type(exc).__name__],
                    )
                    self.mark_error(root_span, exc)
                    raise
                finally:
                    root_span.set_attribute("coremesh.execution.status", handle.status)
                    if handle.trigger:
                        root_span.set_attribute("coremesh.failure.trigger", handle.trigger.value)
                    if handle.final_confidence is not None:
                        root_span.set_attribute(
                            "coremesh.quality.confidence",
                            handle.final_confidence,
                        )
                    if token is not None:
                        _ACTIVE_FORENSICS.reset(token)
                        token = None
        except BaseException:
            self._finalize_handle(handle)
            raise
        else:
            self._finalize_handle(handle)

    @contextmanager
    def span(
        self,
        name: str,
        category: SpanCategory | str,
        *,
        attributes: Mapping[str, Any] | None = None,
        kind: SpanKind = SpanKind.INTERNAL,
    ) -> Iterator[Span]:
        """Create a nested, privacy-sanitized OpenTelemetry span."""

        if not self.enabled:
            yield _NoOpSpan()
            return

        category_value = category.value if isinstance(category, SpanCategory) else str(category)
        safe = _safe_attributes(attributes, max_length=self.max_attribute_length)
        safe["coremesh.span.category"] = category_value
        with self.tracer.start_as_current_span(
            name,
            kind=kind,
            attributes=safe,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                yield span
            except BaseException as exc:
                self.mark_error(span, exc)
                raise

    def traced(
        self,
        name: str,
        category: SpanCategory | str,
        *,
        attributes: Mapping[str, Any] | None = None,
    ) -> Callable[[F], F]:
        """Decorate either a synchronous or asynchronous tool boundary."""

        def decorate(function: F) -> F:
            if inspect.iscoroutinefunction(function):
                @wraps(function)
                async def async_wrapped(*args: Any, **kwargs: Any) -> Any:
                    with self.span(name, category, attributes=attributes):
                        return await function(*args, **kwargs)

                return async_wrapped  # type: ignore[return-value]

            @wraps(function)
            def wrapped(*args: Any, **kwargs: Any) -> Any:
                with self.span(name, category, attributes=attributes):
                    return function(*args, **kwargs)

            return wrapped  # type: ignore[return-value]

        return decorate

    def mark_error(self, span: Span, exc: BaseException) -> None:
        """Set ERROR without exporting a raw exception message or stack."""

        try:
            span.set_status(Status(StatusCode.ERROR, "operation failed"))
            span.set_attribute("exception.type", type(exc).__name__)
            for key, value in content_metadata("exception.message", str(exc)).items():
                span.set_attribute(key, value)
            span.add_event(
                "exception",
                {
                    "exception.type": type(exc).__name__,
                    **content_metadata("exception.message", str(exc)),
                },
            )
        except Exception:
            log.exception("failed to attach sanitized exception metadata to span")

    def get_trace(self, trace_id: str) -> ForensicTraceArtifact:
        path = self.trace_directory / f"{trace_id}.json"
        return ForensicTraceArtifact.model_validate_json(path.read_text(encoding="utf-8"))

    def flag_negative_feedback(
        self,
        trace_id: str,
        reason: str | None = None,
    ) -> RootCauseDiagnosis | None:
        """Flag production feedback and reanalyze an artifact when available."""

        try:
            self.interaction_log_sink.flag_negative_feedback(trace_id)
        except Exception as exc:  # pragma: no cover - strict fail-open boundary
            log.warning(
                "production feedback logging failed for trace %s: %s",
                trace_id,
                exc,
            )
        if not self.enabled:
            return None
        try:
            artifact = self.get_trace(trace_id)
        except FileNotFoundError:
            log.info("no forensic artifact available for feedback trace %s", trace_id)
            return None
        diagnosis = self.analyzer.analyze(
            trace_id,
            artifact.spans,
            FailureTrigger.NEGATIVE_FEEDBACK,
        )
        feedback = {"flagged": True, "created_at": datetime.now(timezone.utc).isoformat()}
        if reason:
            feedback.update(content_metadata("reason", reason))
        artifact = artifact.model_copy(
            update={
                "trigger": FailureTrigger.NEGATIVE_FEEDBACK,
                "trigger_reasons": ["user_negative_feedback"],
                "diagnosis": diagnosis,
                "feedback": feedback,
            }
        )
        self._persist(artifact)
        return diagnosis

    def shutdown(self) -> None:
        try:
            self.provider.shutdown()
        except Exception:
            log.exception("failed to shut down forensic tracer provider")

    def _finalize_handle(self, handle: TraceExecution) -> None:
        if not handle.trace_id:
            return
        try:
            spans = self._exporter.pop(handle.trace_id)
            if not spans:
                return
            trigger = handle.trigger
            if trigger is None and any(span.status == "ERROR" for span in spans):
                trigger = FailureTrigger.EXECUTION_ERROR
            diagnosis = (
                self.analyzer.analyze(handle.trace_id, spans, trigger)
                if trigger is not None
                else None
            )
            artifact = self._build_artifact(
                handle.trace_id,
                spans,
                status=handle.status,
                trigger=trigger,
                trigger_reasons=handle.trigger_reasons or [],
                final_confidence=handle.final_confidence,
                diagnosis=diagnosis,
            )
            self._persist(artifact)
            handle.artifact = artifact
            handle.diagnosis = diagnosis
        except Exception:
            log.exception("failed to finalize forensic trace %s", handle.trace_id)

    def _build_artifact(
        self,
        trace_id: str,
        spans: Sequence[SerializedSpan],
        *,
        status: str,
        trigger: FailureTrigger | None,
        trigger_reasons: Sequence[str],
        final_confidence: float | None,
        diagnosis: RootCauseDiagnosis | None,
    ) -> ForensicTraceArtifact:
        ordered = sorted(spans, key=lambda item: (item.start_time, item.span_id))
        started_at = min(item.start_time for item in ordered)
        ended_at = max(item.end_time for item in ordered)
        duration_ms = max(
            item.duration_ms
            for item in ordered
            if item.name == "coremesh.agent.workflow"
        ) if any(item.name == "coremesh.agent.workflow" for item in ordered) else sum(
            item.duration_ms for item in ordered
        )
        return ForensicTraceArtifact(
            trace_id=trace_id,
            status=status,
            trigger=trigger,
            trigger_reasons=list(trigger_reasons),
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=round(duration_ms, 3),
            final_confidence=final_confidence,
            summary={
                "span_count": len(ordered),
                "error_count": sum(item.status == "ERROR" for item in ordered),
                "degraded_count": sum(
                    item.attributes.get("coremesh.degraded") is True for item in ordered
                ),
            },
            diagnosis=diagnosis,
            spans=list(ordered),
            tree=_build_tree(ordered),
        )

    def _persist(self, artifact: ForensicTraceArtifact) -> None:
        with self._storage_lock:
            self.trace_directory.mkdir(parents=True, exist_ok=True)
            self.registry_path.parent.mkdir(parents=True, exist_ok=True)
            target = self.trace_directory / f"{artifact.trace_id}.json"
            temporary = target.with_suffix(f".json.tmp-{threading.get_ident()}")
            temporary.write_text(
                artifact.model_dump_json(indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, target)
            self._upsert_registry(artifact, target)

    def _upsert_registry(self, artifact: ForensicTraceArtifact, path: Path) -> None:
        with sqlite3.connect(self.registry_path, timeout=5.0) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS trace_registry (
                    trace_id TEXT PRIMARY KEY,
                    session_id_hash TEXT,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    final_confidence REAL,
                    trigger TEXT,
                    root_cause_span_id TEXT,
                    root_cause_step_id TEXT,
                    failure_category TEXT,
                    artifact_path TEXT NOT NULL
                )
                """
            )
            root = next(
                (item for item in artifact.spans if item.name == "coremesh.agent.workflow"),
                artifact.spans[-1] if artifact.spans else None,
            )
            session_hash = (
                root.attributes.get("coremesh.session.id.sha256") if root is not None else None
            )
            diagnosis = artifact.diagnosis
            connection.execute(
                """
                INSERT INTO trace_registry (
                    trace_id, session_id_hash, created_at, status, final_confidence,
                    trigger, root_cause_span_id, root_cause_step_id,
                    failure_category, artifact_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trace_id) DO UPDATE SET
                    status=excluded.status,
                    final_confidence=excluded.final_confidence,
                    trigger=excluded.trigger,
                    root_cause_span_id=excluded.root_cause_span_id,
                    root_cause_step_id=excluded.root_cause_step_id,
                    failure_category=excluded.failure_category,
                    artifact_path=excluded.artifact_path
                """,
                (
                    artifact.trace_id,
                    session_hash,
                    artifact.started_at,
                    artifact.status,
                    artifact.final_confidence,
                    artifact.trigger.value if artifact.trigger else None,
                    diagnosis.span_id if diagnosis else None,
                    diagnosis.step_id if diagnosis else None,
                    diagnosis.category.value if diagnosis else None,
                    str(path),
                ),
            )

    def _configure_otlp(self, endpoint: str | None) -> None:
        endpoint = endpoint or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
        if not endpoint:
            return
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # noqa: PLC0415
                OTLPSpanExporter,
            )

            self.provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
            )
        except Exception:
            log.exception("could not configure optional OTLP trace exporter")


class _NoOpSpan:
    """Small disabled-tracing stand-in for the Span methods callers use."""

    def set_attribute(self, key: str, value: Any) -> None:
        return None

    def set_status(self, status: Status) -> None:
        return None

    def add_event(self, name: str, attributes: Mapping[str, Any] | None = None) -> None:
        return None


def get_forensics() -> ForensicsTracer:
    """Return the execution-scoped tracer or lazily construct the default."""

    active = _ACTIVE_FORENSICS.get()
    if active is not None:
        return active

    global _DEFAULT_FORENSICS
    if _DEFAULT_FORENSICS is None:
        with _DEFAULT_LOCK:
            if _DEFAULT_FORENSICS is None:
                from src.config import settings  # noqa: PLC0415

                _DEFAULT_FORENSICS = ForensicsTracer(
                    trace_directory=settings.forensics_trace_directory,
                    registry_path=settings.forensics_registry_path,
                    enabled=settings.forensics_enabled,
                    confidence_threshold=settings.forensics_confidence_threshold,
                    confidence_drop_threshold=settings.forensics_confidence_drop_threshold,
                    max_attribute_length=settings.forensics_max_attribute_length,
                    interaction_log_sink=configured_interaction_log_sink(),
                )
    return _DEFAULT_FORENSICS


def forensic_span(
    name: str,
    category: SpanCategory | str,
    *,
    attributes: Mapping[str, Any] | None = None,
) -> Callable[[F], F]:
    """Module-level decorator that resolves the active tracer per invocation."""

    def decorate(function: F) -> F:
        if inspect.iscoroutinefunction(function):
            @wraps(function)
            async def async_wrapped(*args: Any, **kwargs: Any) -> Any:
                with get_forensics().span(name, category, attributes=attributes):
                    return await function(*args, **kwargs)

            return async_wrapped  # type: ignore[return-value]

        @wraps(function)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            with get_forensics().span(name, category, attributes=attributes):
                return function(*args, **kwargs)

        return wrapped  # type: ignore[return-value]

    return decorate


def _build_tree(spans: Sequence[SerializedSpan]) -> dict[str, Any] | list[dict[str, Any]]:
    nodes = {
        span.span_id: {
            **span.model_dump(exclude={"trace_id", "parent_span_id"}),
            "children": [],
        }
        for span in spans
    }
    roots: list[dict[str, Any]] = []
    for span in spans:
        node = nodes[span.span_id]
        if span.parent_span_id and span.parent_span_id in nodes:
            nodes[span.parent_span_id]["children"].append(node)
        else:
            roots.append(node)
    return roots[0] if len(roots) == 1 else roots


def _sequence(span: SerializedSpan) -> float:
    raw = span.attributes.get("coremesh.step.index")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float("inf")


def _depth(span: SerializedSpan, spans: Sequence[SerializedSpan]) -> int:
    parents = {item.span_id: item.parent_span_id for item in spans}
    depth = 0
    parent = span.parent_span_id
    while parent:
        depth += 1
        parent = parents.get(parent)
    return depth


def _string_attribute(span: SerializedSpan, key: str) -> str | None:
    value = span.attributes.get(key)
    return str(value) if value is not None else None


def _float_attribute(span: SerializedSpan, key: str) -> float | None:
    try:
        value = span.attributes.get(key)
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _explanation(span: SerializedSpan, category: FailureCategory) -> str:
    step_id = _string_attribute(span, "coremesh.step.id")
    target = f"step {step_id}" if step_id else f"span {span.name}"
    return f"The earliest specific degradation signal occurred at {target} ({category.value})."


__all__ = [
    "FailureCategory",
    "FailureTrigger",
    "ForensicTraceArtifact",
    "ForensicsTracer",
    "RootCauseAnalyzer",
    "RootCauseDiagnosis",
    "SerializedSpan",
    "SpanCategory",
    "StepQualityJudge",
    "TraceEvidence",
    "TraceExecution",
    "content_metadata",
    "forensic_span",
    "get_forensics",
]
