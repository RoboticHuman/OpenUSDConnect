"""Download and set up a portable Blender for testing.

Usage:
    uv run python scripts/setup_blender_test.py              # latest stable
    uv run python scripts/setup_blender_test.py --lts        # latest LTS
    uv run python scripts/setup_blender_test.py --version 5.0.1
    uv run python scripts/setup_blender_test.py --cleanup     # remove downloaded Blender

Resolves the latest version dynamically from download.blender.org.
Downloads the portable zip, extracts to .blender/ in the repo root,
and writes blender.test.cfg so pytest picks it up.
"""

from __future__ import annotations

import argparse
import pathlib
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BLENDER_DIR = REPO_ROOT / ".blender"
CFG_FILE = REPO_ROOT / "blender.test.cfg"
PYTHON_DEPS_DIR = BLENDER_DIR / "python_deps"

BASE_URL = "https://download.blender.org/release"
FALLBACK_VERSION = "5.0.1"

# LTS minor versions (from blender.org/download/lts/).
# Blender declares specific releases as LTS — not a simple even/odd rule.
_KNOWN_LTS_MINORS = {"2.83", "2.93", "3.3", "3.6", "4.2", "4.5"}


def _safe_zip_extractall(zf: zipfile.ZipFile, target_dir: pathlib.Path) -> None:
    """Extract zip members, rejecting path traversal attempts."""
    target = pathlib.Path(target_dir).resolve()
    for name in zf.namelist():
        dest = (target / name).resolve()
        if not dest.is_relative_to(target):
            raise RuntimeError(f"Path traversal in zip archive: {name}")
    zf.extractall(target_dir)


def _fetch_index(url: str) -> str:
    """Fetch an HTML directory listing."""
    req = urllib.request.Request(url, headers={"User-Agent": "OpenUSDConnect-Test/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _list_release_dirs() -> list[str]:
    """Return sorted list of major.minor versions from download.blender.org/release/.

    E.g. ["3.6", "4.0", "4.1", "4.2", "4.3", "4.4", "4.5", "5.0"]
    """
    html = _fetch_index(f"{BASE_URL}/")
    dirs = re.findall(r'href="Blender(\d+\.\d+)/?"', html)
    dirs = sorted(set(dirs), key=lambda v: tuple(int(x) for x in v.split(".")))
    return dirs


def _list_patch_versions(major_minor: str) -> list[str]:
    """Return sorted list of patch versions available for a major.minor release.

    Looks for files matching the current platform to determine available versions.
    E.g. for "5.0" on windows-x64: ["5.0.0", "5.0.1"]
    """
    os_name, arch, ext = _detect_platform()
    html = _fetch_index(f"{BASE_URL}/Blender{major_minor}/")
    pattern = rf'href="blender-(\d+\.\d+\.\d+)-{os_name}-{arch}\.{ext}"'
    versions = re.findall(pattern, html)
    versions = sorted(set(versions), key=lambda v: tuple(int(x) for x in v.split(".")))
    return versions


def resolve_latest(lts: bool = False) -> str:
    """Resolve the latest Blender version from download.blender.org.

    Args:
        lts: If True, restrict to known LTS minor versions.

    Returns:
        Full version string like "5.0.1" or "4.5.7".
    """
    try:
        dirs = _list_release_dirs()
    except Exception as e:
        print(f"Warning: could not fetch release index ({e}), using fallback {FALLBACK_VERSION}")
        return FALLBACK_VERSION

    if lts:
        dirs = [d for d in dirs if d in _KNOWN_LTS_MINORS]
        if not dirs:
            print(f"Warning: no LTS versions found, using fallback {FALLBACK_VERSION}")
            return FALLBACK_VERSION

    # Try from newest to oldest until we find one with downloadable patches
    for major_minor in reversed(dirs):
        try:
            patches = _list_patch_versions(major_minor)
            if patches:
                return patches[-1]  # latest patch
        except Exception:
            continue

    print(f"Warning: could not resolve any version, using fallback {FALLBACK_VERSION}")
    return FALLBACK_VERSION


def _detect_platform() -> tuple[str, str, str]:
    """Return (os_name, arch, ext) for the download URL."""
    system = platform.system().lower()
    machine = platform.machine().lower()

    if system == "windows":
        if machine in ("amd64", "x86_64"):
            return "windows", "x64", "zip"
        raise RuntimeError(f"Unsupported Windows architecture: {machine}")
    elif system == "linux":
        if machine in ("x86_64", "amd64"):
            return "linux", "x64", "tar.xz"
        raise RuntimeError(f"Unsupported Linux architecture: {machine}")
    elif system == "darwin":
        if machine == "arm64":
            return "macos", "arm64", "dmg"
        elif machine == "x86_64":
            return "macos", "x64", "dmg"
        raise RuntimeError(f"Unsupported macOS architecture: {machine}")
    else:
        raise RuntimeError(f"Unsupported platform: {system}")


def _build_url(version: str) -> tuple[str, str]:
    """Build the download URL for a given Blender version."""
    os_name, arch, ext = _detect_platform()
    major_minor = ".".join(version.split(".")[:2])
    filename = f"blender-{version}-{os_name}-{arch}.{ext}"
    return f"{BASE_URL}/Blender{major_minor}/{filename}", filename


def _find_blender_exe(extract_dir: pathlib.Path) -> pathlib.Path:
    """Find the blender executable in the extracted directory."""
    system = platform.system().lower()
    if system == "windows":
        pattern = "**/blender.exe"
    elif system == "darwin":
        pattern = "**/Blender.app/Contents/MacOS/Blender"
    else:
        pattern = "**/blender"

    for match in extract_dir.glob(pattern):
        if match.is_file():
            return match

    raise FileNotFoundError(f"Could not find blender executable in {extract_dir}")


def _extract_dmg(download_path: pathlib.Path, version: str) -> None:
    """Mount a Blender DMG read-only and copy its app bundle into .blender/."""
    _, arch, _ = _detect_platform()
    extract_dir = BLENDER_DIR / f"blender-{version}-macos-{arch}"
    mount_dir = pathlib.Path(tempfile.mkdtemp(prefix="openusdconnect-blender-"))
    mounted = False
    try:
        subprocess.run(
            [
                "hdiutil",
                "attach",
                str(download_path),
                "-nobrowse",
                "-readonly",
                "-mountpoint",
                str(mount_dir),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        mounted = True
        app = next(mount_dir.glob("*.app"), None)
        if app is None:
            raise RuntimeError(f"Blender app bundle not found in {download_path}")
        extract_dir.mkdir(parents=True, exist_ok=False)
        shutil.copytree(app, extract_dir / app.name, symlinks=True)
    finally:
        if mounted:
            subprocess.run(
                ["hdiutil", "detach", str(mount_dir)],
                check=False,
                capture_output=True,
                text=True,
            )
        shutil.rmtree(mount_dir, ignore_errors=True)


def download_and_extract(version: str) -> pathlib.Path:
    """Download Blender portable and extract to .blender/. Returns exe path."""
    url, filename = _build_url(version)
    download_path = BLENDER_DIR / filename

    BLENDER_DIR.mkdir(exist_ok=True)

    # Download if not cached
    if not download_path.exists():
        print(f"Downloading {url}...")
        print(f"  -> {download_path}")
        req = urllib.request.Request(url, headers={"User-Agent": "OpenUSDConnect-Test/1.0"})
        with urllib.request.urlopen(req) as resp:
            total_size = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            with open(download_path, "wb") as f:
                while True:
                    chunk = resp.read(1024 * 1024)  # 1MB chunks
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    _progress_hook(downloaded, total_size)
        print()  # newline after progress
    else:
        print(f"Using cached download: {download_path}")

    # Extract — find existing extracted dir or extract fresh
    extract_dir = _find_extracted_dir(version)
    if extract_dir:
        print(f"Already extracted: {extract_dir}")
    else:
        print("Extracting...")
        if filename.endswith(".zip"):
            with zipfile.ZipFile(download_path, "r") as zf:
                _safe_zip_extractall(zf, BLENDER_DIR)
        elif filename.endswith(".tar.xz"):
            with tarfile.open(download_path, "r:xz") as tf:
                tf.extractall(BLENDER_DIR, filter="data")
        elif filename.endswith(".dmg"):
            _extract_dmg(download_path, version)
        else:
            raise RuntimeError(f"Unsupported archive format: {filename}")
        extract_dir = _find_extracted_dir(version)
        if not extract_dir:
            raise RuntimeError("Extraction succeeded but could not find extracted directory")

    exe_path = _find_blender_exe(extract_dir)
    print(f"Blender executable: {exe_path}")
    return exe_path


def _find_extracted_dir(version: str) -> pathlib.Path | None:
    """Find the extracted Blender directory (e.g. blender-5.0.1-windows-x64)."""
    for item in BLENDER_DIR.iterdir():
        if item.is_dir() and item.name.startswith(f"blender-{version}"):
            return item
    return None


def _progress_hook(downloaded, total_size):
    """Download progress callback."""
    if total_size > 0:
        pct = min(100, downloaded * 100 // total_size)
        mb_done = downloaded / (1024 * 1024)
        mb_total = total_size / (1024 * 1024)
        print(f"\r  {pct:3d}%  {mb_done:.1f}/{mb_total:.1f} MB", end="", flush=True)
    else:
        mb_done = downloaded / (1024 * 1024)
        print(f"\r  {mb_done:.1f} MB", end="", flush=True)


def write_config(exe_path: pathlib.Path):
    """Write blender.test.cfg with the resolved exe path."""
    CFG_FILE.write_text(str(exe_path) + "\n")
    print(f"Wrote {CFG_FILE}")


def install_python_dependencies() -> None:
    """Vendor pure-Python runtime dependencies for Blender test scripts.

    Blender embeds a different Python minor version from the project virtual
    environment on some platforms.  Exposing the whole virtual environment
    would make incompatible extension modules (notably NumPy) shadow
    Blender's bundled copies, so only the pure-Python FlatBuffers runtime is
    copied here.
    """
    import flatbuffers

    source = pathlib.Path(flatbuffers.__path__[0])
    destination = PYTHON_DEPS_DIR / "flatbuffers"
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    print(f"Installed Blender Python dependencies: {PYTHON_DEPS_DIR}")


def cleanup():
    """Remove downloaded Blender and config."""
    if BLENDER_DIR.exists():
        print(f"Removing {BLENDER_DIR}...")
        shutil.rmtree(BLENDER_DIR)
    if CFG_FILE.exists():
        CFG_FILE.unlink()
        print(f"Removed {CFG_FILE}")
    print("Done.")


def main():
    ap = argparse.ArgumentParser(
        description="Download portable Blender for testing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  uv run python scripts/setup_blender_test.py              # latest stable
  uv run python scripts/setup_blender_test.py --lts        # latest LTS
  uv run python scripts/setup_blender_test.py --version 5.0.1
  uv run python scripts/setup_blender_test.py --cleanup

Files:
  .blender/          Downloaded and extracted Blender (gitignored)
  blender.test.cfg   Path to blender.exe (gitignored, read by pytest)
""",
    )
    ap.add_argument(
        "--version",
        default=None,
        help="Blender version to download (omit to auto-detect latest)",
    )
    ap.add_argument(
        "--lts",
        action="store_true",
        help="Download the latest LTS version instead of the latest stable",
    )
    ap.add_argument(
        "--cleanup",
        action="store_true",
        help="Remove downloaded Blender and config",
    )
    args = ap.parse_args()

    if args.cleanup:
        cleanup()
        return

    if args.version:
        version = args.version
        print(f"Using specified version: {version}")
    else:
        label = "LTS" if args.lts else "stable"
        print(f"Resolving latest {label} version from download.blender.org...")
        version = resolve_latest(lts=args.lts)
        print(f"Resolved: Blender {version}")

    exe_path = download_and_extract(version)
    install_python_dependencies()
    write_config(exe_path)

    print("\nReady! Run tests with:")
    print("  uv run pytest tests/ -v")


if __name__ == "__main__":
    main()
