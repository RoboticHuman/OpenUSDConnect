"""Build the optional Sdf notice bridge against the active OpenUSD install."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

from .cli_common import add_version_argument


def _source_directory() -> Path:
    package = Path(__file__).resolve().parent
    candidates = (
        package / "native" / "sdf_notice_bridge",
        package.parent / "native" / "sdf_notice_bridge",
    )
    for candidate in candidates:
        if (candidate / "CMakeLists.txt").is_file():
            return candidate
    raise FileNotFoundError("the packaged Sdf notice bridge source is unavailable")


def _pxr_directory() -> Path:
    from pxr import Sdf

    module_path = Path(Sdf.__file__).resolve()
    for parent in module_path.parents:
        if (parent / "pxrConfig.cmake").is_file():
            return parent
        if (parent / "lib" / "cmake" / "pxr" / "pxrConfig.cmake").is_file():
            return parent / "lib" / "cmake" / "pxr"
    raise FileNotFoundError("could not locate pxrConfig.cmake for the active OpenUSD install")


def _built_library(build_directory: Path) -> Path:
    suffixes = {".dll", ".dylib", ".so"}
    candidates = sorted(
        path
        for path in build_directory.rglob("*openusdconnect_sdf_delegate_bridge*")
        if path.is_file() and path.suffix.lower() in suffixes
    )
    if not candidates:
        raise FileNotFoundError("CMake completed without producing the Sdf notice bridge")
    return candidates[0]


def _install_prefix(pxr_directory: Path) -> Path:
    if (
        pxr_directory.name == "pxr"
        and pxr_directory.parent.name == "cmake"
        and pxr_directory.parent.parent.name == "lib"
    ):
        return pxr_directory.parents[2]
    return pxr_directory


def build_bridge(
    *,
    pxr_directory: Path,
    build_directory: Path,
    configuration: str = "Release",
    parallel: int | None = None,
) -> Path:
    """Configure and build the bridge, returning its absolute library path."""
    source = _source_directory()
    build_directory = build_directory.expanduser().resolve()
    pxr_directory = pxr_directory.expanduser().resolve()
    subprocess.run(
        [
            "cmake",
            "-S",
            os.fspath(source),
            "-B",
            os.fspath(build_directory),
            f"-Dpxr_DIR={pxr_directory}",
            f"-DCMAKE_PREFIX_PATH={_install_prefix(pxr_directory)}",
            f"-DCMAKE_BUILD_TYPE={configuration}",
        ],
        check=True,
    )
    command = [
        "cmake",
        "--build",
        os.fspath(build_directory),
        "--config",
        configuration,
    ]
    if parallel is not None:
        command.extend(("--parallel", str(parallel)))
    subprocess.run(command, check=True)
    return _built_library(build_directory).resolve()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build the Sdf notice bridge against this process's OpenUSD install.",
    )
    add_version_argument(parser)
    parser.add_argument(
        "--pxr-dir",
        type=Path,
        help="Directory containing pxrConfig.cmake; detected from pxr when omitted.",
    )
    parser.add_argument(
        "--build-dir",
        type=Path,
        default=Path("build/sdf-notice-bridge"),
    )
    parser.add_argument("--config", default="Release")
    parser.add_argument("--parallel", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    library = build_bridge(
        pxr_directory=args.pxr_dir or _pxr_directory(),
        build_directory=args.build_dir,
        configuration=args.config,
        parallel=args.parallel,
    )
    manifest = Path(str(library) + ".json")

    # Install to the user-local cache so auto-discovery finds it.
    dest_dir = Path.home() / ".openusdconnect"
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(library, dest_dir / library.name)
    if manifest.is_file():
        shutil.copy2(manifest, dest_dir / manifest.name)
    print(f"Installed to {dest_dir / library.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
