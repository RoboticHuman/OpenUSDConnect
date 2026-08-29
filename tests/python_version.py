"""Python compatibility values sourced from the repository version file."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_VERSION = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
PYTHON_VERSION_PARTS = tuple(int(part) for part in PYTHON_VERSION.split("."))
PYTHON_SITE_DIRECTORY = f"python{PYTHON_VERSION}"
