"""Validate a Windows WebDAV UNC path for OpenUSDConnect live-open.

Example:

    python scripts/check_windows_unc_webdav.py --port 7280
    python scripts/check_windows_unc_webdav.py --unc "\\\\127.0.0.1@7280\\usd\\scene.usd"
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def _default_unc(host: str, port: int, share: str, name: str) -> str:
    return f"\\\\{host}@{port}\\{share}\\{name}"


def _webclient_status() -> str:
    if os.name != "nt":
        return "not-windows"
    result = subprocess.run(
        ["sc.exe", "query", "WebClient"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return "unknown"
    for line in result.stdout.splitlines():
        if "STATE" in line:
            return line.strip()
    return "unknown"


def _validate_usd_bytes(data: bytes) -> tuple[bool, str]:
    try:
        from pxr import Sdf, Usd
    except Exception as exc:
        return False, f"pxr import failed: {exc}"

    layer = Sdf.Layer.CreateAnonymous(".usda")
    if not layer.ImportFromString(data.decode("utf-8")):
        return False, "downloaded bytes did not parse as USDA"
    stage = Usd.Stage.Open(layer)
    if not stage:
        return False, "Usd.Stage.Open failed"
    meta = layer.customLayerData.get("openusdconnect")
    return True, f"parsed USD stage; live metadata present={bool(meta)}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unc", default=None, help="Full UNC path to read")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7280)
    parser.add_argument("--share", default="usd")
    parser.add_argument("--name", default="scene.usd")
    args = parser.parse_args(argv)

    if os.name != "nt":
        print("This diagnostic must run on Windows.", file=sys.stderr)
        return 2

    unc = args.unc or _default_unc(args.host, args.port, args.share, args.name)
    print(f"WebClient: {_webclient_status()}")
    print(f"UNC: {unc}")

    try:
        data = Path(unc).read_bytes()
    except Exception as exc:
        print(f"UNC read failed: {exc}", file=sys.stderr)
        return 1

    print(f"Read {len(data)} bytes")
    ok, message = _validate_usd_bytes(data)
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
