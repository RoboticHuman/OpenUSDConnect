"""Locate files belonging to a selected usdview installation."""

from __future__ import annotations

import os
from pathlib import Path


def usd_install_root(executable: str | os.PathLike) -> Path | None:
    """Return the OpenUSD prefix for a standard ``<prefix>/bin/usdview`` path."""
    resolved = Path(executable).resolve()
    return resolved.parent.parent if resolved.parent.name.lower() == "bin" else None
