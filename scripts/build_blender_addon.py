"""Build a Blender-installable zip of the USD Connect addon.

Bundles openusdconnect/ core library inside the addon directory so
it's importable without any path gymnastics.

Usage:
    python scripts/build_blender_addon.py

Output:
    dist/usd_connect_blender.zip
"""

import os
import shutil
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = REPO_ROOT / "dist"
ADDON_NAME = "usd_connect"
ZIP_NAME = "usd_connect_blender.zip"


def build():
    # Clean
    build_dir = REPO_ROOT / "build" / ADDON_NAME
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True)

    # Copy Blender integration files
    blender_src = REPO_ROOT / "integrations" / "blender"
    for f in blender_src.iterdir():
        if f.suffix == ".py":
            shutil.copy2(f, build_dir / f.name)

    # Copy core library (vendored inside addon)
    core_src = REPO_ROOT / "openusdconnect"
    core_dst = build_dir / "openusdconnect"
    shutil.copytree(
        core_src, core_dst,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "dashboard"),
    )

    # Vendor flatbuffers (pure Python, required by codec)
    import flatbuffers as _fb_mod
    fb_src = Path(_fb_mod.__path__[0])
    fb_dst = build_dir / "flatbuffers"
    shutil.copytree(
        fb_src, fb_dst,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )

    # Create zip
    DIST_DIR.mkdir(exist_ok=True)
    zip_path = DIST_DIR / ZIP_NAME
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(build_dir):
            # Skip __pycache__
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for fname in files:
                if fname.endswith(".pyc"):
                    continue
                full_path = Path(root) / fname
                rel = str(full_path.relative_to(build_dir))
                arcname = ADDON_NAME + "/" + rel.replace("\\", "/")
                zf.write(full_path, arcname)

    # Clean build dir
    shutil.rmtree(REPO_ROOT / "build")

    print(f"Built: {zip_path}")
    print(f"Install in Blender: Preferences > Add-ons > Install from Disk > {zip_path}")


if __name__ == "__main__":
    build()
