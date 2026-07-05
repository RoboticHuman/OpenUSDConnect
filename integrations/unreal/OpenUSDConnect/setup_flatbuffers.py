"""Fetch the FlatBuffers headers the OpenUSDConnect plugin compiles against.

Source-engine checkouts ship the headers at
``Engine/Source/ThirdParty/flatbuffers/flatbuffers-<version>/include`` and the
plugin's Build.cs uses them directly. Launcher builds ship only the license
stub, so run this once to download the matching headers (header-only, no
build step) into the plugin's ThirdParty folder:

    python setup_flatbuffers.py --engine "D:/UE_5.8"

The version to fetch resolves in this order:
  1. ``--version X.Y.Z``
  2. the engine's ``Flatbuffers_v<version>.tps`` declaration (``--engine``)
  3. DEFAULT_VERSION below

Stdlib only; works with any Python 3.
"""

from __future__ import annotations

import argparse
import io
import re
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

DEFAULT_VERSION = "24.3.25"
ARCHIVE_URL = "https://github.com/google/flatbuffers/archive/refs/tags/v{version}.zip"

PLUGIN_ROOT = Path(__file__).resolve().parent
DEST = PLUGIN_ROOT / "Source" / "OpenUSDConnect" / "ThirdParty" / "flatbuffers"


def engine_headers_present(engine_root: Path) -> bool:
    fb_dir = engine_root / "Engine" / "Source" / "ThirdParty" / "flatbuffers"
    return any(fb_dir.glob("flatbuffers-*/include/flatbuffers/flatbuffer_builder.h"))


def engine_declared_version(engine_root: Path) -> str | None:
    """Version from the engine's third-party-software declaration file."""
    fb_dir = engine_root / "Engine" / "Source" / "ThirdParty" / "flatbuffers"
    for tps in fb_dir.glob("Flatbuffers_v*.tps"):
        match = re.fullmatch(r"Flatbuffers_v(.+)\.tps", tps.name)
        if match:
            return match.group(1)
    return None


def install(version: str) -> None:
    url = ARCHIVE_URL.format(version=version)
    print(f"Downloading FlatBuffers v{version} from {url}")
    with urllib.request.urlopen(url) as resp:
        archive = zipfile.ZipFile(io.BytesIO(resp.read()))

    src_prefix = f"flatbuffers-{version}/include/flatbuffers/"
    headers = [n for n in archive.namelist() if n.startswith(src_prefix) and not n.endswith("/")]
    if not headers:
        sys.exit(f"error: archive layout unexpected, no headers under {src_prefix}")

    include_dst = DEST / "include" / "flatbuffers"
    if DEST.exists():
        shutil.rmtree(DEST)
    include_dst.mkdir(parents=True)

    for name in headers:
        target = include_dst / name[len(src_prefix):]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archive.read(name))

    license_name = f"flatbuffers-{version}/LICENSE"
    if license_name in archive.namelist():
        (DEST / "LICENSE").write_bytes(archive.read(license_name))
    (DEST / "VERSION").write_text(version + "\n")

    print(f"Installed {len(headers)} headers to {include_dst}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--engine", type=Path, default=None,
                        help="Unreal Engine root (e.g. D:/UE_5.8); used to "
                             "detect the version the engine declares")
    parser.add_argument("--version", default=None,
                        help=f"FlatBuffers version to fetch (default: engine "
                             f"declaration, else {DEFAULT_VERSION})")
    args = parser.parse_args()

    version = args.version
    if args.engine:
        engine_root = args.engine.resolve()
        if not (engine_root / "Engine").is_dir():
            sys.exit(f"error: {engine_root} does not look like an engine root "
                     f"(no Engine/ directory)")
        if engine_headers_present(engine_root):
            print(f"{engine_root} ships the FlatBuffers headers; the plugin "
                  f"uses them directly. Nothing to do.")
            return
        if version is None:
            version = engine_declared_version(engine_root)
            if version:
                print(f"Engine declares FlatBuffers v{version}")

    if version is None:
        version = DEFAULT_VERSION
        print(f"No engine declaration found; using default v{version}")

    install(version)


if __name__ == "__main__":
    main()
