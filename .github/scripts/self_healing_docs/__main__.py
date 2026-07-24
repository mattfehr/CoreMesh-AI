"""Command-line entry point for CoreMesh self-healing documentation.

System role:
    Exposes the workflow-safe ``python -m self_healing_docs`` interface.
Dependencies:
    The package configuration and pipeline modules plus a Git checkout.
Side effects:
    Writes run artifacts and, only with ``--apply``, approved Markdown edits.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import ConfigurationError, HealingConfig
from .pipeline import HealingRunError, run_healing


def build_parser() -> argparse.ArgumentParser:
    """Build the documented CLI without accepting secrets on command lines."""

    parser = argparse.ArgumentParser(
        prog="python -m self_healing_docs",
        description=(
            "Detect documentation-impacting structural changes between two Git "
            "commits and optionally apply validated Markdown repairs."
        ),
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Git repository root (default: current directory).",
    )
    parser.add_argument("--base-sha", required=True, help="Base commit or ref.")
    parser.add_argument("--head-sha", required=True, help="Candidate commit or ref.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(".self-healing-docs"),
        help="Artifact directory (default: .self-healing-docs).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply approved Markdown bodies; default behavior is dry-run.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return a process exit code suitable for Actions."""

    args = build_parser().parse_args(argv)
    output_dir = (
        args.output_dir
        if args.output_dir.is_absolute()
        else args.repo_root / args.output_dir
    )
    try:
        config = HealingConfig.from_environment(
            repo_root=args.repo_root,
            base_sha=args.base_sha,
            head_sha=args.head_sha,
            output_dir=output_dir,
            apply=args.apply,
        )
        report = run_healing(config)
    except (ConfigurationError, HealingRunError) as exc:
        print(f"self-healing-docs: {exc}", file=sys.stderr)
        return 1
    print(
        f"self-healing-docs: {report.status}; "
        f"report={config.output_dir / 'report.json'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
