import asyncio
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.arbitration.consensus import (  # noqa: E402
    AdjudicationSchema,
    ArbitrationPayload,
    BLOCKED_RESPONSE,
    ConsensusArbitrator,
    ConsensusStatus,
    CriticAssessmentSchema,
    _extract_anthropic_text,
)


def _assessment(dimension, *, score=5, anomalies=None, confidence=0.9):
    return CriticAssessmentSchema(
        evaluation_dimension=dimension,
        assigned_score=score,
        flagged_anomalies=anomalies or [],
        confidence_coefficient=confidence,
    )


class FakeCritic:
    provider_name = "fake"

    def __init__(
        self,
        dimension,
        *,
        assessment=None,
        error=None,
        starts=None,
        release=None,
    ):
        self.dimension = dimension
        self.assessment = assessment or _assessment(dimension)
        self.error = error
        self.starts = starts
        self.release = release

    async def assess(self, payload):
        if self.starts is not None:
            self.starts.append(self.dimension)
            if len(self.starts) == 3:
                self.release.set()
            await self.release.wait()
        if self.error:
            raise RuntimeError(self.error)
        return self.assessment


class FakeAdjudicator:
    provider_name = "fake_adjudicator"

    def __init__(self, adjudication):
        self.adjudication = adjudication
        self.calls = []

    async def adjudicate(self, payload, assessments, failures, triggered_by):
        self.calls.append((payload, list(assessments), list(failures), list(triggered_by)))
        return self.adjudication


def _payload(text="The answer is safe."):
    return ArbitrationPayload(
        original_prompt="Answer the question.",
        output_text=text,
        user_id="user-1",
        feature_scope="test",
        session_id="session-1",
    )


def test_critic_assessment_schema_validates_bounds_and_dimensions():
    with pytest.raises(ValidationError):
        CriticAssessmentSchema(
            evaluation_dimension="logic",
            assigned_score=0,
            flagged_anomalies=[],
            confidence_coefficient=0.5,
        )

    with pytest.raises(ValidationError):
        CriticAssessmentSchema(
            evaluation_dimension="style",
            assigned_score=4,
            flagged_anomalies=[],
            confidence_coefficient=0.5,
        )

    with pytest.raises(ValidationError):
        CriticAssessmentSchema(
            evaluation_dimension="factual",
            assigned_score=4,
            flagged_anomalies=[],
            confidence_coefficient=1.5,
        )


def test_three_critics_start_concurrently_before_fan_in():
    async def scenario():
        starts = []
        release = asyncio.Event()
        adjudicator = FakeAdjudicator(
            AdjudicationSchema(
                action="release_original",
                overall_quality_score=9,
                confidence_coefficient=0.9,
            )
        )
        arbitrator = ConsensusArbitrator(
            critics=[
                FakeCritic("factual", starts=starts, release=release),
                FakeCritic("logic", starts=starts, release=release),
                FakeCritic("completeness", starts=starts, release=release),
            ],
            adjudicator=adjudicator,
            retry_attempts=1,
        )

        verdict = await asyncio.wait_for(arbitrator.arbitrate(_payload()), timeout=1)
        return verdict, starts, adjudicator

    verdict, starts, adjudicator = asyncio.run(scenario())

    assert set(starts) == {"factual", "logic", "completeness"}
    assert verdict.status == ConsensusStatus.PASSED
    assert adjudicator.calls == []


def test_clean_assessments_skip_adjudication_and_allow_delivery():
    adjudicator = FakeAdjudicator(
        AdjudicationSchema(
            action="block",
            overall_quality_score=1,
            confidence_coefficient=0.9,
        )
    )
    arbitrator = ConsensusArbitrator(
        critics=[
            FakeCritic("factual"),
            FakeCritic("logic"),
            FakeCritic("completeness"),
        ],
        adjudicator=adjudicator,
        retry_attempts=1,
    )
    payload = _payload("All good.")

    verdict = asyncio.run(arbitrator.arbitrate(payload))

    assert verdict.status == ConsensusStatus.PASSED
    assert verdict.delivery_allowed is True
    assert verdict.delivered_output == "All good."
    assert adjudicator.calls == []


def test_forced_logical_error_is_adjudicated_and_blocked():
    adjudicator = FakeAdjudicator(
        AdjudicationSchema(
            action="block",
            overall_quality_score=2,
            confidence_coefficient=0.95,
            confirmed_issues=["The response asserts 2 + 2 = 5."],
            rationale="The logic critic found an explicit arithmetic contradiction.",
        )
    )
    arbitrator = ConsensusArbitrator(
        critics=[
            FakeCritic("factual"),
            FakeCritic(
                "logic",
                assessment=_assessment(
                    "logic",
                    score=2,
                    anomalies=["2 + 2 = 5"],
                    confidence=0.95,
                ),
            ),
            FakeCritic("completeness"),
        ],
        adjudicator=adjudicator,
        retry_attempts=1,
    )

    verdict = asyncio.run(arbitrator.arbitrate(_payload("The calculation is clear: 2 + 2 = 5.")))

    assert verdict.status == ConsensusStatus.BLOCKED
    assert verdict.delivery_allowed is False
    assert verdict.delivered_output == BLOCKED_RESPONSE
    assert len(adjudicator.calls) == 1
    assert "logic_flagged_anomalies" in verdict.triggered_by


def test_single_critic_failure_routes_to_adjudication_before_degraded_delivery():
    adjudicator = FakeAdjudicator(
        AdjudicationSchema(
            action="release_original",
            overall_quality_score=7,
            confidence_coefficient=0.75,
        )
    )
    arbitrator = ConsensusArbitrator(
        critics=[
            FakeCritic("factual", error="provider unavailable"),
            FakeCritic("logic"),
            FakeCritic("completeness"),
        ],
        adjudicator=adjudicator,
        retry_attempts=1,
    )

    verdict = asyncio.run(arbitrator.arbitrate(_payload("Still safe.")))

    assert verdict.status == ConsensusStatus.PASSED_DEGRADED
    assert verdict.delivery_allowed is True
    assert verdict.delivered_output == "Still safe."
    assert len(verdict.critic_failures) == 1
    assert len(adjudicator.calls) == 1
    assert "critic_failure_factual" in verdict.triggered_by


def test_logic_critic_failure_does_not_pass_without_adjudicator_release():
    adjudicator = FakeAdjudicator(
        AdjudicationSchema(
            action="block",
            overall_quality_score=2,
            confidence_coefficient=0.9,
            confirmed_issues=["Logic critic unavailable; cannot verify reasoning."],
        )
    )
    arbitrator = ConsensusArbitrator(
        critics=[
            FakeCritic("factual"),
            FakeCritic("logic", error="provider unavailable"),
            FakeCritic("completeness"),
        ],
        adjudicator=adjudicator,
        retry_attempts=1,
    )

    verdict = asyncio.run(
        arbitrator.arbitrate(_payload("The calculation is clear: 2 + 2 = 5."))
    )

    assert verdict.status == ConsensusStatus.BLOCKED
    assert verdict.delivery_allowed is False
    assert verdict.delivered_output == BLOCKED_RESPONSE
    assert len(adjudicator.calls) == 1
    assert "critic_failure_logic" in verdict.triggered_by


def test_insufficient_critic_quorum_fails_closed_to_manual_review():
    arbitrator = ConsensusArbitrator(
        critics=[
            FakeCritic("factual", error="provider unavailable"),
            FakeCritic("logic", error="provider unavailable"),
            FakeCritic("completeness"),
        ],
        adjudicator=FakeAdjudicator(
            AdjudicationSchema(
                action="release_original",
                overall_quality_score=10,
                confidence_coefficient=0.9,
            )
        ),
        retry_attempts=1,
    )

    verdict = asyncio.run(arbitrator.arbitrate(_payload("Only one critic passed.")))

    assert verdict.status == ConsensusStatus.MANUAL_REVIEW
    assert verdict.delivery_allowed is False
    assert verdict.delivered_output == BLOCKED_RESPONSE
    assert "insufficient_critic_quorum" in verdict.triggered_by


def test_extract_anthropic_text_joins_text_blocks_and_skips_non_text():
    content = _extract_anthropic_text(
        [
            {"type": "tool_use", "id": "tool-1", "name": "lookup"},
            {"type": "text", "text": '{"evaluation_dimension": "logic"}'},
        ]
    )

    assert content == '{"evaluation_dimension": "logic"}'


def test_extract_anthropic_text_raises_when_no_text_blocks():
    with pytest.raises(ValueError, match="text content block"):
        _extract_anthropic_text([{"type": "tool_use", "id": "tool-1"}])
