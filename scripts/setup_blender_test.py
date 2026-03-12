"""Download and set up a portable Blender for testing.

Usage:
    uv run python scripts/setup_blender_test.py              # latest stable (4.5.7)
    uv run python scripts/setup_blender_test.py --version 4.4.3
    uv run python scripts/setup_blender_test.py --cleanup     # remove downloaded Blender

Downloads the portable zip from download.blender.org, extracts to .blender/
in the repo root, and writes blender.test.cfg so pytest picks it up.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import platform
import shutil
import sys
import urllib.request
import zipfile
import tarfile

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BLENDER_DIR = REPO_ROOT / ".blender"
CFG_FILE = REPO_ROOT / "blender.test.cfg"

# download.blender.org URL pattern
BASE_URL = "https://download.blender.org/release"
DEFAULT_VERSION = "4.5.7"


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


def _build_url(version: str) -> str:
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
    else:
        pattern = "**/blender"

    for match in extract_dir.glob(pattern):
        if match.is_file():
            return match

    raise FileNotFoundError(f"Could not find blender executable in {extract_dir}")


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
        print(f"Extracting...")
        if filename.endswith(".zip"):
            with zipfile.ZipFile(download_path, "r") as zf:
                zf.extractall(BLENDER_DIR)
        elif filename.endswith(".tar.xz"):
            with tarfile.open(download_path, "r:xz") as tf:
                tf.extractall(BLENDER_DIR)
        else:
            raise RuntimeError(f"Unsupported archive format: {filename}")
        extract_dir = _find_extracted_dir(version)
        if not extract_dir:
            raise RuntimeError("Extraction succeeded but could not find extracted directory")

    exe_path = _find_blender_exe(extract_dir)
    print(f"Blender executable: {exe_path}")
    return exe_path


def _find_extracted_dir(version: str) -> pathlib.Path | None:
    """Find the extracted Blender directory (e.g. blender-4.5.7-windows-x64)."""
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
        epilog=f"""
Examples:
  uv run python scripts/setup_blender_test.py
  uv run python scripts/setup_blender_test.py --version 4.4.3
  uv run python scripts/setup_blender_test.py --cleanup

Files:
  .blender/          Downloaded and extracted Blender (gitignored)
  blender.test.cfg   Path to blender.exe (gitignored, read by pytest)
""",
    )
    ap.add_argument(
        "--version", default=DEFAULT_VERSION,
        help=f"Blender version to download (default: {DEFAULT_VERSION})",
    )
    ap.add_argument(
        "--cleanup", action="store_true",
        help="Remove downloaded Blender and config",
    )
    args = ap.parse_args()

    if args.cleanup:
        cleanup()
        return

    exe_path = download_and_extract(args.version)
    write_config(exe_path)

    print(f"\nReady! Run tests with:")
    print(f"  uv run pytest tests/ -v")


if __name__ == "__main__":
    main()
