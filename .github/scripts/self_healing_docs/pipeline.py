"""End-to-end orchestration for self-healing documentation runs.

System role:
    Joins Git snapshots, structural parsing, Markdown retrieval, typed model
    judgments, deterministic safety gates, bounded mutation, and reporting.
Dependencies:
    Workflow-local parser/retrieval/provider modules and ``OPENAI_API_KEY`` for
    production runs containing meaningful structural changes.
Side effects:
    Calls OpenAI through production providers, writes run artifacts, and in
    explicit apply mode rewrites only mechanically approved Markdown bodies.
"""
from __future__ import annotations

import os
from collections import defaultdict
from pathlib import Path
from typing import Any

from .config import HealingConfig
from .gitops import (
    discover_source_deltas,
    markdown_patch,
    require_clean_worktree,
    require_head_checkout,
    resolve_commit,
    validate_markdown_worktree_changes,
)
from .markdown import (
    MarkdownSafetyError,
    apply_markdown_replacements,
    load_markdown_corpus,
    neighboring_context,
    normalize_replacement_body,
)
from .models import (
    CandidateMatch,
    DocSection,
    PendingReplacement,
    RunReport,
    StructuralChange,
)
from .providers import (
    DocRepairProvider,
    OpenAIDocRepairProvider,
    OpenAIEmbeddingProvider,
    ProviderError,
)
from .reporting import write_run_outputs
from .retrieval import EmbeddingProvider, retrieve_candidates
from .structural import extract_structural_changes


class HealingRunError(RuntimeError):
    """Raised after a failed run has written diagnostic artifacts."""

    def __init__(self, message: str, *, report: RunReport) -> None:
        super().__init__(message)
        self.report = report


def run_healing(
    config: HealingConfig,
    *,
    embedding_provider: EmbeddingProvider | None = None,
    repair_provider: DocRepairProvider | None = None,
) -> RunReport:
    """Execute one documentation-healing comparison and always write artifacts."""

    config.validate()
    report = RunReport(
        base_sha=config.base_sha,
        head_sha=config.head_sha,
        apply_requested=config.apply,
        configuration=config.report_values(),
    )
    patch = ""
    try:
        report.base_sha = resolve_commit(config.repo_root, config.base_sha)
        report.head_sha = resolve_commit(config.repo_root, config.head_sha)
        if config.apply:
            require_head_checkout(config.repo_root, report.head_sha)
            require_clean_worktree(config.repo_root)
        patch = _execute(
            config=config,
            report=report,
            embedding_provider=embedding_provider,
            repair_provider=repair_provider,
        )
    except Exception as exc:
        report.status = "failed"
        report.errors.append(
            {
                "type": type(exc).__name__,
                "message": str(exc)[:1_000],
            }
        )
        write_run_outputs(output_dir=config.output_dir, report=report, patch=patch)
        raise HealingRunError(str(exc), report=report) from exc

    write_run_outputs(output_dir=config.output_dir, report=report, patch=patch)
    return report


def _execute(
    *,
    config: HealingConfig,
    report: RunReport,
    embedding_provider: EmbeddingProvider | None,
    repair_provider: DocRepairProvider | None,
) -> str:
    deltas = discover_source_deltas(
        config.repo_root,
        base_sha=report.base_sha,
        head_sha=report.head_sha,
    )
    changes = extract_structural_changes(deltas)
    report.structural_changes = [change.to_dict() for change in changes]
    if not changes:
        report.status = "no_structural_changes"
        return ""

    documents, sections = load_markdown_corpus(config.repo_root)
    if not sections:
        for change in changes:
            report.review_items.append(
                _review_for_change(change, "No tracked Markdown sections are available")
            )
        report.status = "review_required"
        return ""

    if embedding_provider is None or repair_provider is None:
        api_key = os.getenv("OPENAI_API_KEY", "")
        embedding_provider = embedding_provider or OpenAIEmbeddingProvider(
            api_key=api_key,
            model=config.embedding_model,
        )
        repair_provider = repair_provider or OpenAIDocRepairProvider(
            api_key=api_key,
            model=config.model,
            reasoning_effort=config.reasoning_effort,
        )

    retrieval = retrieve_candidates(
        changes=changes,
        sections=sections,
        provider=embedding_provider,
        similarity_threshold=config.similarity_threshold,
        top_k=config.top_k,
        max_candidates=config.max_candidates,
    )
    report.candidate_mappings = [match.to_dict() for match in retrieval.matches]

    changes_by_id = {change.change_id: change for change in changes}
    sections_by_id = {section.section_id: section for section in sections}
    matched_change_ids = {match.change_id for match in retrieval.matches}
    truncated_ids = set(retrieval.truncated_change_ids)
    for change in changes:
        if change.change_id in matched_change_ids or change.change_id in truncated_ids:
            continue
        report.review_items.append(
            _review_for_change(
                change,
                "No Markdown section passed exact-reference or similarity retrieval",
            )
        )
    for change_id in retrieval.truncated_change_ids:
        change = changes_by_id[change_id]
        report.review_items.append(
            _review_for_change(
                change,
                "Candidate processing was truncated by DOC_HEALING_MAX_CANDIDATES",
            )
        )

    matches_by_section: dict[str, list[CandidateMatch]] = defaultdict(list)
    for match in retrieval.matches:
        matches_by_section[match.section_id].append(match)

    pending: list[PendingReplacement] = []
    pending_report_records: dict[str, dict[str, Any]] = {}
    for section_id in sorted(matches_by_section):
        section = sections_by_id.get(section_id)
        if section is None:
            raise RuntimeError(f"retrieval returned unknown section {section_id}")
        section_changes = _unique_changes(
            matches_by_section[section_id], changes_by_id
        )
        if len(section.body) > config.max_section_chars:
            report.review_items.append(
                _review_for_section(
                    section,
                    section_changes,
                    f"Section exceeds the {config.max_section_chars}-character model limit",
                )
            )
            continue

        assessment = repair_provider.assess(
            changes=section_changes,
            section=section,
        )
        report.llm_decisions.append(
            _decision_record(
                stage="staleness_assessment",
                section=section,
                changes=section_changes,
                output=assessment.model_dump(),
            )
        )
        if not assessment.stale:
            report.verified_sections.append(
                {
                    "path": section.path,
                    "heading": section.label,
                    "change_ids": [change.change_id for change in section_changes],
                    "confidence": assessment.confidence,
                    "diagnosis": assessment.diagnosis,
                }
            )
            continue

        style_context = neighboring_context(section, sections)
        proposal = repair_provider.propose(
            changes=section_changes,
            section=section,
            assessment=assessment,
            neighboring_style=style_context,
        )
        report.llm_decisions.append(
            _decision_record(
                stage="targeted_rewrite",
                section=section,
                changes=section_changes,
                output=proposal.model_dump(),
            )
        )
        try:
            normalized_body = normalize_replacement_body(
                section=section,
                proposed_body=proposal.replacement_body,
                max_chars=config.max_section_chars,
            )
        except MarkdownSafetyError as exc:
            report.review_items.append(
                _review_for_section(
                    section,
                    section_changes,
                    f"Generated repair failed mechanical validation: {exc}",
                    assessment_confidence=assessment.confidence,
                )
            )
            continue
        if normalized_body == section.body:
            report.review_items.append(
                _review_for_section(
                    section,
                    section_changes,
                    "Staleness was reported but the generated body is unchanged",
                    assessment_confidence=assessment.confidence,
                )
            )
            continue

        validation = repair_provider.validate(
            changes=section_changes,
            section=section,
            assessment=assessment,
            proposal=proposal.model_copy(update={"replacement_body": normalized_body}),
            neighboring_style=style_context,
        )
        report.llm_decisions.append(
            _decision_record(
                stage="correction_validation",
                section=section,
                changes=section_changes,
                output=validation.model_dump(),
            )
        )
        eligible = (
            all(change.auto_fix_eligible for change in section_changes)
            and assessment.complexity == "bounded"
            and assessment.confidence >= config.confidence_threshold
            and validation.approved
            and validation.confidence >= config.confidence_threshold
        )
        record = {
            "path": section.path,
            "heading": section.label,
            "section_id": section.section_id,
            "change_ids": [change.change_id for change in section_changes],
            "assessment_confidence": assessment.confidence,
            "validation_confidence": validation.confidence,
            "diagnosis": assessment.diagnosis,
            "rationale": proposal.rationale,
            "replacement_body": normalized_body,
            "auto_fix_eligible": eligible,
            "validation": validation.model_dump(),
            "disposition": (
                "apply" if config.apply and eligible else "would_apply" if eligible else "review"
            ),
        }
        report.proposed_repairs.append(record)
        if not eligible:
            report.review_items.append(
                _review_for_section(
                    section,
                    section_changes,
                    _ineligibility_reason(
                        changes=section_changes,
                        assessment_confidence=assessment.confidence,
                        assessment_complexity=assessment.complexity,
                        validation_approved=validation.approved,
                        validation_confidence=validation.confidence,
                        confidence_threshold=config.confidence_threshold,
                        validation_issues=validation.issues,
                    ),
                    assessment_confidence=assessment.confidence,
                    validation_confidence=validation.confidence,
                )
            )
            continue

        replacement = PendingReplacement(
            section=section,
            replacement_body=normalized_body,
            change_ids=tuple(change.change_id for change in section_changes),
            assessment_confidence=assessment.confidence,
            validation_confidence=validation.confidence,
            diagnosis=assessment.diagnosis,
        )
        pending.append(replacement)
        pending_report_records[section.section_id] = record

    if config.apply and pending:
        changed_paths = apply_markdown_replacements(
            repo_root=config.repo_root,
            documents=documents,
            replacements=pending,
        )
        report.changed_markdown_paths = validate_markdown_worktree_changes(
            config.repo_root,
            set(changed_paths),
        )
        patch = markdown_patch(config.repo_root, report.changed_markdown_paths)
        for replacement in pending:
            if replacement.section.path not in report.changed_markdown_paths:
                continue
            source_record = pending_report_records[replacement.section.section_id]
            report.applied_repairs.append(
                {
                    "path": replacement.section.path,
                    "heading": replacement.section.label,
                    "section_id": replacement.section.section_id,
                    "change_ids": list(replacement.change_ids),
                    "assessment_confidence": replacement.assessment_confidence,
                    "validation_confidence": replacement.validation_confidence,
                    "diagnosis": replacement.diagnosis,
                    "rationale": source_record["rationale"],
                }
            )
    else:
        patch = ""

    report.status = _success_status(report, config.apply)
    return patch


def _unique_changes(
    matches: list[CandidateMatch],
    changes_by_id: dict[str, StructuralChange],
) -> list[StructuralChange]:
    change_ids = sorted({match.change_id for match in matches})
    return [changes_by_id[change_id] for change_id in change_ids]


def _decision_record(
    *,
    stage: str,
    section: DocSection,
    changes: list[StructuralChange],
    output: dict[str, Any],
) -> dict[str, Any]:
    return {
        "stage": stage,
        "path": section.path,
        "heading": section.label,
        "section_id": section.section_id,
        "change_ids": [change.change_id for change in changes],
        "output": output,
    }


def _review_for_change(change: StructuralChange, reason: str) -> dict[str, Any]:
    return {
        "change_id": change.change_id,
        "path": change.path,
        "symbol": change.name,
        "reason": reason,
    }


def _review_for_section(
    section: DocSection,
    changes: list[StructuralChange],
    reason: str,
    *,
    assessment_confidence: float | None = None,
    validation_confidence: float | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "path": section.path,
        "heading": section.label,
        "section_id": section.section_id,
        "change_ids": [change.change_id for change in changes],
        "reason": reason,
    }
    if assessment_confidence is not None:
        item["assessment_confidence"] = assessment_confidence
    if validation_confidence is not None:
        item["validation_confidence"] = validation_confidence
    return item


def _ineligibility_reason(
    *,
    changes: list[StructuralChange],
    assessment_confidence: float,
    assessment_complexity: str,
    validation_approved: bool,
    validation_confidence: float,
    confidence_threshold: float,
    validation_issues: list[str],
) -> str:
    reasons: list[str] = []
    if not all(change.auto_fix_eligible for change in changes):
        reasons.append("one or more changes are added, removed, or structurally complex")
    if assessment_complexity != "bounded":
        reasons.append("the staleness assessment classified the repair as complex")
    if assessment_confidence < confidence_threshold:
        reasons.append(
            f"assessment confidence {assessment_confidence:.2f} is below "
            f"{confidence_threshold:.2f}"
        )
    if not validation_approved:
        reasons.append("the independent validation dimensions did not all pass")
    if validation_confidence < confidence_threshold:
        reasons.append(
            f"validation confidence {validation_confidence:.2f} is below "
            f"{confidence_threshold:.2f}"
        )
    if validation_issues:
        reasons.append("validation issues: " + "; ".join(validation_issues))
    return "; ".join(reasons) or "repair did not meet the auto-fix policy"


def _success_status(report: RunReport, apply: bool) -> str:
    if report.applied_repairs and report.review_items:
        return "repaired_with_review"
    if report.applied_repairs:
        return "repaired"
    if report.review_items:
        return "review_required"
    if report.proposed_repairs and not apply:
        return "dry_run"
    if report.verified_sections:
        return "accurate"
    return "no_documentation_impact"
