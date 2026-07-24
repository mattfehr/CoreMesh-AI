"""Machine-readable and PR-readable output for documentation healing runs.

System role:
    Serializes link-graph provenance, decisions, repairs, review findings, and
    patches for GitHub artifacts, step summaries, and marker-based PR comments.
Dependencies:
    JSON, pathlib, and the pipeline report contract.
Side effects:
    Creates the configured output directory and writes report files there.
"""
from __future__ import annotations

import json
from pathlib import Path

from .models import RunReport


def write_run_outputs(
    *,
    output_dir: Path,
    report: RunReport,
    patch: str,
) -> None:
    """Write the stable artifact set, including an internal staging allowlist."""

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "report.json").write_text(
        json.dumps(report.to_dict(), indent=2, ensure_ascii=False, sort_keys=True)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (output_dir / "summary.md").write_text(
        render_summary(report),
        encoding="utf-8",
        newline="\n",
    )
    (output_dir / "changes.patch").write_text(
        patch,
        encoding="utf-8",
        newline="\n",
    )
    (output_dir / "applied-paths.txt").write_text(
        "".join(f"{path}\n" for path in sorted(report.changed_markdown_paths)),
        encoding="utf-8",
        newline="\n",
    )


def render_summary(report: RunReport) -> str:
    """Render a concise Markdown result suitable for a PR comment."""

    metrics = report.to_dict()["metrics"]
    lines = [
        "## Self-Healing Documentation",
        "",
        f"**Status:** `{_escape(report.status)}`",
        "",
        "| Result | Count |",
        "| --- | ---: |",
        f"| Structural changes | {metrics['structural_changes']} |",
        f"| Candidate mappings | {metrics['candidate_mappings']} |",
        f"| Typed model decisions | {metrics['llm_decisions']} |",
        f"| Verified accurate | {metrics['verified_sections']} |",
        f"| Proposed repairs | {metrics['proposed_repairs']} |",
        f"| Applied repairs | {metrics['applied_repairs']} |",
        f"| Needs review | {metrics['review_items']} |",
        f"| Errors | {metrics['errors']} |",
        "",
    ]

    if report.verified_sections:
        lines.extend(["### Verified accurate", ""])
        for item in report.verified_sections:
            lines.append(
                f"- `{_escape(str(item['path']))}` — "
                f"{_escape(str(item['heading']))} "
                f"(confidence `{float(item['confidence']):.2f}`): "
                f"{_escape(str(item['diagnosis']))}"
            )
        lines.append("")

    if report.applied_repairs:
        lines.extend(["### Applied", ""])
        for item in report.applied_repairs:
            lines.append(
                f"- `{_escape(str(item['path']))}` — "
                f"{_escape(str(item['heading']))} "
                f"(assessment `{float(item['assessment_confidence']):.2f}`, "
                f"validation `{float(item['validation_confidence']):.2f}`)"
            )
        lines.append("")

    if report.review_items:
        lines.extend(["### Human review", ""])
        for item in report.review_items:
            location = str(item.get("path") or item.get("change_id") or "unmapped")
            heading = item.get("heading")
            label = f"{location} — {heading}" if heading else location
            reason = str(item.get("reason", "Review required"))
            lines.append(f"- `{_escape(label)}`: {_escape(reason)}")
        lines.append("")

    if report.errors:
        lines.extend(["### Errors", ""])
        for item in report.errors:
            lines.append(
                f"- `{_escape(str(item.get('type', 'error')))}`: "
                f"{_escape(str(item.get('message', 'Unknown error')))}"
            )
        lines.append("")

    lines.extend(
        [
            "<sub>",
            f"Base `{_escape(report.base_sha[:12])}` · "
            f"Head `{_escape(report.head_sha[:12])}` · "
            f"Model `{_escape(str(report.configuration.get('model', 'unknown')))}` · "
            f"Embedding `{_escape(str(report.configuration.get('embedding_model', 'unknown')))}`",
            "</sub>",
            "",
        ]
    )
    return "\n".join(lines)


def _escape(value: str) -> str:
    value = value.replace("\r", " ").replace("\n", " ")
    if len(value) > 1_000:
        value = value[:997] + "..."
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("@", "@\u200b")
        .replace("`", "ˋ")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )
