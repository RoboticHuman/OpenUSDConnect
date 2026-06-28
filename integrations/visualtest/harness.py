"""Baseline comparison flow for the visual-regression harness.

The reusable regression primitive: render a stage from a camera and FLIP-compare
against a stored golden, or (re)generate the golden under ``--update-baselines``.
Integration-agnostic: it takes any ``Usd.Stage``.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass

from integrations.visualtest.compare import FlipResult, compare
from integrations.visualtest.render import render


@dataclass(frozen=True)
class BaselineCheck:
    name: str
    baseline_path: str
    test_path: str
    status: str  # "compared" | "generated" | "missing"
    result: FlipResult | None  # set only when status == "compared"


def check_against_baseline(stage, camera_path: str, baseline_path: str, *, name: str,
                           artifacts_dir: str, width: int = 512,
                           update: bool = False) -> BaselineCheck:
    """Render ``stage`` and compare to ``baseline_path``.

    ``update`` writes the freshly rendered image as the new golden (status
    ``generated``). A missing golden without ``update`` is reported as
    ``missing`` rather than silently created, so a new scene fails loudly until
    its baseline is deliberately captured.
    """
    os.makedirs(artifacts_dir, exist_ok=True)
    test_path = os.path.join(artifacts_dir, f"{name}.test.png")
    render(stage, camera_path, test_path, width=width)

    if update:
        os.makedirs(os.path.dirname(baseline_path) or ".", exist_ok=True)
        shutil.copyfile(test_path, baseline_path)
        return BaselineCheck(name, baseline_path, test_path, "generated", None)

    if not os.path.exists(baseline_path):
        return BaselineCheck(name, baseline_path, test_path, "missing", None)

    error_map = os.path.join(artifacts_dir, f"{name}.flip.png")
    result = compare(baseline_path, test_path, error_map_path=error_map)
    return BaselineCheck(name, baseline_path, test_path, "compared", result)
