"""Conftest for the visual-regression tier.

Heavy (renders with RenderMan) and dependency-gated (flip-evaluator), so it is
skipped by default. Enable with ``--visual-tests``; regenerate goldens by adding
``--update-baselines``.
"""

import os

import pytest

from integrations.visualtest import render as vrender

_VISUAL_DIR = os.path.dirname(__file__)


def pytest_addoption(parser):
    for flag, helptext in (
        ("--visual-tests", "Run visual-regression tests (requires RenderMan + flip-evaluator)"),
        ("--update-baselines", "Regenerate visual reference goldens instead of comparing"),
    ):
        try:
            parser.addoption(flag, action="store_true", default=False, help=helptext)
        except ValueError:
            pass  # already registered by a parent conftest


def pytest_collection_modifyitems(config, items):
    if config.getoption("--visual-tests"):
        return
    skip = pytest.mark.skip(reason="visual tests disabled (use --visual-tests)")
    for item in items:
        if str(item.fspath).startswith(_VISUAL_DIR):
            item.add_marker(skip)


@pytest.fixture(scope="session")
def update_baselines(request):
    return bool(request.config.getoption("--update-baselines"))


@pytest.fixture
def visual_artifacts_dir(tmp_path):
    return str(tmp_path)


@pytest.fixture(autouse=True)
def _visual_env():
    """Skip the tier cleanly when its render/compare prerequisites are absent."""
    if not vrender.is_available():
        pytest.skip("RenderMan not available (set RMANTREE / install RenderManProServer)")
    try:
        import flip_evaluator  # noqa: F401
    except ImportError:
        pytest.skip("flip-evaluator not installed (uv sync --group visual)")
