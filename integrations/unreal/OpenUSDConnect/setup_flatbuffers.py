"""Fetch the FlatBuffers headers the OpenUSDConnect plugin compiles against.

Run once before the first build (header-only, no build step):

    python setup_flatbuffers.py

The flatc-generated bindings committed under Source/OpenUSDConnect/Private/
Schema pin the exact runtime version they were produced with (the generated
header carries a static_assert), so the plugin always compiles against this
vendored copy — never whatever version an engine happens to ship.
DEFAULT_VERSION must match the flatc used by scripts/generate_flatbuffers.sh;
override with ``--version`` only when regenerating the bindings with a
different flatc.

Stdlib only; works with any Python 3.
"""

from __future__ import annotations

import argparse
import io
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

DEFAULT_VERSION = "25.12.19"
ARCHIVE_URL = "https://github.com/google/flatbuffers/archive/refs/tags/v{version}.zip"

PLUGIN_ROOT = Path(__file__).resolve().parent
DEST = PLUGIN_ROOT / "Source" / "OpenUSDConnect" / "ThirdParty" / "flatbuffers"


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
    parser.add_argument("--version", default=DEFAULT_VERSION,
                        help=f"FlatBuffers version to fetch (default: "
                             f"{DEFAULT_VERSION}, matching the committed "
                             f"generated bindings)")
    args = parser.parse_args()

    install(args.version)


if __name__ == "__main__":
    main()
