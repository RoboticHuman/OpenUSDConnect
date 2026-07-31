"""Launch usdview pre-wired to an OpenUSDConnect server.

Locates the ``usdview`` executable from a system OpenUSD install, sets the
environment variables the plugin expects (``PXR_PLUGINPATH_NAME``,
``PYTHONPATH``, ``OPENUSDCONNECT_*``), and spawns usdview as a subprocess.

    python -m integrations.usdview.launcher path/to/scene.usda --host 127.0.0.1 --port 7200

Pixar RenderMan (hdPrman) is available as an opt-in renderer via ``--renderman``;
the flag sets the ``RMAN_*`` environment from ``$RMANTREE`` plus the discovered
OpenUSD install and starts usdview in the RenderMan delegate. Without it usdview
behaves exactly as before (Storm only). See
https://openusd.org/release/plugins_renderman.html

    python -m integrations.usdview.launcher scene.usda --renderman
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import sysconfig
from collections.abc import Sequence
from pathlib import Path


def find_usdview() -> Path:
    """Locate the ``usdview`` executable shipped with the system OpenUSD install.

    Resolution order:
        1. ``shutil.which`` against ``usdview[.cmd|.exe]`` (works when the
           OpenUSD ``bin`` dir is on PATH).
        2. Walk up from ``pxr.__file__`` to the install root and probe
           ``bin/usdview*``. ``pxr`` itself imports without PySide6, only
           ``pxr.Usdviewq`` needs Qt, so this works in any env where pxr
           is importable.
        3. ``USDVIEW_PATH`` environment variable as an explicit override.

    Raises ``RuntimeError`` if none of the strategies succeed.
    """
    for name in ("usdview.cmd", "usdview.exe", "usdview"):
        found = shutil.which(name)
        if found:
            return Path(found)

    try:
        import pxr
    except ImportError:
        pxr = None
    if pxr is not None:
        install_root = Path(pxr.__file__).resolve().parents[3]
        for rel in ("bin/usdview.cmd", "bin/usdview.exe", "bin/usdview"):
            candidate = install_root / rel
            if candidate.exists():
                return candidate

    override = os.environ.get("USDVIEW_PATH")
    if override:
        path = Path(override)
        if path.exists():
            return path

    raise RuntimeError(
        "Could not locate the usdview executable. Tried PATH, the pxr "
        "install walk-up, and the USDVIEW_PATH environment variable. "
        "Install OpenUSD with usdview, add its bin/ to PATH, or set "
        "USDVIEW_PATH to the executable."
    )


def _resolve_command(exe: Path) -> list[str]:
    """Return the command-line prefix that launches usdview.

    On Windows ``usdview.cmd`` invokes whichever ``python`` is first on
    PATH, which can resolve to the launcher's venv (no PySide6) instead
    of OpenUSD's. Bypass by reading the sibling script's shebang and
    running its declared interpreter directly.
    """
    if exe.suffix.lower() in (".cmd", ".bat"):
        script = exe.with_suffix("")
        if not script.exists():
            return [str(exe)]
    else:
        script = exe

    try:
        first_line = script.read_text(encoding="utf-8", errors="ignore").splitlines()[0]
    except (OSError, IndexError):
        return [str(exe)]

    if first_line.startswith("#!"):
        interpreter = Path(first_line[2:].strip())
        if interpreter.exists():
            return [str(interpreter), str(script)]

    return [str(exe)]


# Stable Hydra plugin id for the RenderMan RIS loader (display name
# "RenderMan RIS"). usdview's --renderer accepts either the id or the
# display name; the id is version-stable, so prefer it.
RENDERMAN_RENDERER_ID = "HdPrmanLoaderRendererPlugin"


def _has_renderer_arg(args: Sequence[str]) -> bool:
    """True if ``args`` already selects a Hydra renderer (-r/--renderer)."""
    return any(a in ("-r", "--renderer") or a.startswith("--renderer=") for a in args)


def launch_usdview(
    stage_path: str | os.PathLike,
    host: str = "127.0.0.1",
    port: int = 7200,
    *,
    token: str | None = None,
    extra_args: Sequence[str] = (),
    usdview_exe: str | os.PathLike | None = None,
    renderman: bool = False,
    camera_path: str | None = None,
    expected_seq: int = 0,
    scene_lights: bool = False,
) -> subprocess.Popen:
    """Spawn usdview with the OpenUSDConnect plugin discovered and auto-connecting.

    The plugin's ``configureView`` reads ``OPENUSDCONNECT_HOST`` /
    ``OPENUSDCONNECT_PORT`` / ``OPENUSDCONNECT_TOKEN`` and schedules a
    ``QTimer.singleShot`` to connect once usdview's main loop is running.
    """
    plugin_dir = Path(__file__).resolve().parent
    project_root = plugin_dir.parents[1]

    # usdview runs in the OpenUSD install's interpreter (per the shebang),
    # which lacks our runtime deps. Adding our site-packages to PYTHONPATH
    # avoids a separate install, requires matching Python minor version
    # for C extensions to load.
    purelib = sysconfig.get_paths().get("purelib")
    platlib = sysconfig.get_paths().get("platlib")
    extra_paths = [str(project_root)]
    for p in (purelib, platlib):
        if p and p not in extra_paths:
            extra_paths.append(p)

    env = os.environ.copy()
    env["PXR_PLUGINPATH_NAME"] = os.pathsep.join(
        filter(None, [str(plugin_dir), env.get("PXR_PLUGINPATH_NAME", "")])
    )
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [*extra_paths, env.get("PYTHONPATH", "")]))
    env["OPENUSDCONNECT_HOST"] = host
    env["OPENUSDCONNECT_PORT"] = str(port)
    if token:
        env["OPENUSDCONNECT_TOKEN"] = token
    if camera_path:
        env["OPENUSDCONNECT_CAMERA_PATH"] = camera_path
    if expected_seq > 0:
        env["OPENUSDCONNECT_EXPECTED_SEQ"] = str(expected_seq)
    if scene_lights:
        env["OPENUSDCONNECT_SCENE_LIGHTS"] = "1"

    exe = Path(usdview_exe) if usdview_exe else find_usdview()

    # Make RenderMan's plugin DLLs loadable so the Sdr registry initializes,
    # required even for Storm once RenderMan is installed into the shared USD
    # tree, and independent of which delegate usdview starts in. No-op when
    # RMANTREE is unset. (integrations.renderman owns the RMANTREE knowledge.)
    from integrations.renderman import dll_dirs as _rman_dll_dirs
    from integrations.renderman import renderman_env

    rman_dirs = _rman_dll_dirs()
    if rman_dirs:
        env["PATH"] = os.pathsep.join(filter(None, [*rman_dirs, env.get("PATH", "")]))

    forwarded = list(extra_args)
    if renderman:
        # hdPrman ships inside the OpenUSD install tree (parent of bin/usdview).
        install_root = exe.resolve().parents[1]
        rman_env = renderman_env(install_root)  # render-time RMAN_* search paths
        if not rman_env:
            raise RuntimeError(
                "RenderMan requested (--renderman) but RMANTREE is not set. "
                "Install RenderManProServer and set RMANTREE to its root."
            )
        env.update(rman_env)
        # hdPrman can't render the OpenPBR surface node; have the receiver
        # translate OpenPBR materials to standard_surface (see openpbr_translate).
        env["OPENUSDCONNECT_TRANSLATE_OPENPBR"] = "1"
        # Start in RenderMan unless the caller already picked a renderer.
        if not _has_renderer_arg(forwarded):
            forwarded = ["--renderer", RENDERMAN_RENDERER_ID, *forwarded]

    cmd = [*_resolve_command(exe), str(stage_path), *forwarded]
    return subprocess.Popen(cmd, env=env)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Launch usdview pre-wired to an OpenUSDConnect server."
    )
    parser.add_argument("stage", help="USD file to open in usdview")
    parser.add_argument("--host", default="127.0.0.1", help="OpenUSDConnect server host")
    parser.add_argument("--port", type=int, default=7200, help="OpenUSDConnect server port")
    parser.add_argument(
        "--token", default=None, help="Optional TOFU token (overrides cached value)"
    )
    parser.add_argument(
        "--usdview",
        default=None,
        help="Explicit path to the usdview executable (overrides auto-discovery)",
    )
    parser.add_argument(
        "--renderman",
        action="store_true",
        help="Enable Pixar RenderMan (hdPrman): set the RMAN_* env from $RMANTREE "
        "+ the OpenUSD install and start in the RenderMan delegate. Omit for the "
        "default Storm-only behavior. Pass your own --renderer to override which "
        "delegate it starts in while keeping RenderMan available in the menu.",
    )
    parser.add_argument(
        "--camera",
        default=None,
        help="Select this streamed UsdGeomCamera after replay catches up.",
    )
    parser.add_argument(
        "--expected-seq",
        type=int,
        default=0,
        help="Wait for this replay sequence before applying camera/view settings.",
    )
    parser.add_argument(
        "--scene-lights",
        action="store_true",
        help="Use streamed scene lights/materials and disable usdview's default dome/headlight.",
    )
    parser.epilog = (
        "Any arguments not recognized above are forwarded verbatim to usdview "
        "(e.g. --quitAfterStartup, --norender, --renderer Storm)."
    )
    args, unknown = parser.parse_known_args(argv)

    proc = launch_usdview(
        args.stage,
        host=args.host,
        port=args.port,
        token=args.token,
        extra_args=unknown,
        usdview_exe=args.usdview,
        renderman=args.renderman,
        camera_path=args.camera,
        expected_seq=args.expected_seq,
        scene_lights=args.scene_lights,
    )
    return proc.wait()


if __name__ == "__main__":
    sys.exit(main())
