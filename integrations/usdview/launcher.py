"""Launch usdview pre-wired to an OpenUSDConnect server.

Locates the ``usdview`` executable from a system OpenUSD install, sets the
environment variables the plugin expects (``PXR_PLUGINPATH_NAME``,
``PYTHONPATH``, ``OPENUSDCONNECT_*``), and spawns usdview as a subprocess.

    python -m integrations.usdview.launcher path/to/scene.usda --host 127.0.0.1 --port 7200
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
           ``bin/usdview*``. ``pxr`` itself imports without PySide6 — only
           ``pxr.Usdviewq`` needs Qt — so this works in any env where pxr
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
    PATH — which can resolve to the launcher's venv (no PySide6) instead
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


def launch_usdview(
    stage_path: str | os.PathLike,
    host: str = "127.0.0.1",
    port: int = 7200,
    *,
    token: str | None = None,
    extra_args: Sequence[str] = (),
    usdview_exe: str | os.PathLike | None = None,
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
    # avoids a separate install — requires matching Python minor version
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
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [*extra_paths, env.get("PYTHONPATH", "")])
    )
    env["OPENUSDCONNECT_HOST"] = host
    env["OPENUSDCONNECT_PORT"] = str(port)
    if token:
        env["OPENUSDCONNECT_TOKEN"] = token

    exe = Path(usdview_exe) if usdview_exe else find_usdview()
    cmd = [*_resolve_command(exe), str(stage_path), *extra_args]
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
    )
    return proc.wait()


if __name__ == "__main__":
    sys.exit(main())
