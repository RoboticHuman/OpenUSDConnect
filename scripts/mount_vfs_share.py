# ruff: noqa: E402, I001
"""Mount an OpenUSDConnect WebDAV share using the native filesystem client.

This wrapper is intentionally tiny so users can run it from a checkout without
refreshing console scripts:

    uv run python scripts/mount_vfs_share.py --port 7280 --open
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from openusdconnect.vfs_mount import main


if __name__ == "__main__":
    raise SystemExit(main())
