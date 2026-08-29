"""OpenUSD compatibility values sourced from the repository lock file."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OPENUSD_LOCK = json.loads((ROOT / "openusd.lock.json").read_text(encoding="utf-8"))

OPENUSD_VERSION = str(OPENUSD_LOCK["version"])
OPENUSD_VERSION_PARTS = tuple(int(part) for part in OPENUSD_VERSION.split("."))
OPENUSD_TAG = str(OPENUSD_LOCK["tag"])
OPENUSD_COMMIT = str(OPENUSD_LOCK["commit"])
OPENUSD_CORE_VERSION = str(OPENUSD_LOCK["usd_core"])
