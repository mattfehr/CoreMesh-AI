"""Pytest bootstrap for workflow-local self-healing documentation tests.

System role:
    Makes the sibling package importable without installing CoreMesh itself.
Dependencies:
    Python path handling and pytest collection.
Side effects:
    Prepends `.github/scripts` to the test-process import path.
"""
from __future__ import annotations

import sys
from pathlib import Path


SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))
