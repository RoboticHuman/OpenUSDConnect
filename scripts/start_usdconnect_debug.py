"""Launch a USD Connect debug session: server + Blender instance(s).

Usage:
    uv run python scripts/start_usdconnect_debug.py
    uv run python scripts/start_usdconnect_debug.py --two-blenders
    uv run python scripts/start_usdconnect_debug.py --reload
    uv run python scripts/start_usdconnect_debug.py --wait-for-debugger

Starts the sync server, builds the addon if needed, and launches one or
two Blender instances with the bootstrap script.  Stops the server when
all Blender instances exit.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import subprocess
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
ADDON_ZIP = REPO_ROOT / "dist" / "usd_connect_blender.zip"
BOOTSTRAP_SCRIPT = REPO_ROOT / "scripts" / "blender_bootstrap_instance.py"
BLENDER_CFG = REPO_ROOT / "blender.test.cfg"
BLENDER_USER_DATA = REPO_ROOT / ".blender" / "user_data"


def _find_blender(explicit: str) -> str:
    """Resolve the Blender executable path."""
    if explicit:
        return explicit
    if BLENDER_CFG.exists():
        for line in BLENDER_CFG.read_text().splitlines():
            line = line.strip()
            if line:
                return line
    raise SystemExit(
        "Blender executable not provided and blender.test.cfg is empty/missing."
    )


def _find_uv_python() -> str:
    """Find the Python interpreter managed by uv."""
    result = subprocess.run(
        ["uv", "python", "find"],
        capture_output=True, text=True,
    )
    python = result.stdout.strip()
    if not python or not pathlib.Path(python).exists():
        raise SystemExit("Could not find Python via uv. Run 'uv sync' first.")
    return python


def _build_addon():
    """Build the Blender addon zip if missing."""
    if ADDON_ZIP.exists():
        return
    print("[launcher] Addon zip missing. Building addon...")
    subprocess.run(
        ["uv", "run", "--no-group", "dashboard", "python",
         str(REPO_ROOT / "scripts" / "build_blender_addon.py")],
        cwd=str(REPO_ROOT), check=True,
    )
    if not ADDON_ZIP.exists():
        raise SystemExit(f"Addon build failed: {ADDON_ZIP} not found")


def _reload_addon():
    """Build the addon and signal running Blender instances to reload."""
    print("[launcher] Building addon...")
    subprocess.run(
        ["uv", "run", "--no-group", "dashboard", "python",
         str(REPO_ROOT / "scripts" / "build_blender_addon.py")],
        cwd=str(REPO_ROOT), check=True,
    )
    if not ADDON_ZIP.exists():
        raise SystemExit(f"Addon build failed: {ADDON_ZIP} not found")
    trigger = REPO_ROOT / ".reload_addon"
    trigger.write_text(str(ADDON_ZIP), encoding="utf-8")
    print("[launcher] Addon built and reload triggered."
          " Running Blender instances will pick it up within ~2s.")


def _start_server(python: str, host: str, port: int,
                   base_usd: str, log_path: str) -> subprocess.Popen:
    """Start the sync server as a subprocess."""
    args = [
        python, "-m", "openusdconnect.server",
        "--port", str(port),
        "--base", base_usd,
        "--log", log_path,
    ]
    print(f"[launcher] Starting server on {host}:{port} ...")
    return subprocess.Popen(args, cwd=str(REPO_ROOT))


def _start_blender(blender_exe: str, role: str, host: str, port: int,
                    debug_port: int, wait_for_debugger: bool,
                    start_emitter: bool, start_receiver: bool) -> subprocess.Popen:
    """Start a Blender instance with the bootstrap script."""
    args = [
        blender_exe,
        "--python", str(BOOTSTRAP_SCRIPT),
        "--",
        "--addon-zip", str(ADDON_ZIP),
        "--host", host,
        "--port", str(port),
        "--role", role,
    ]
    if debug_port > 0:
        args.extend(["--debug-port", str(debug_port)])
        if wait_for_debugger:
            args.append("--wait-for-client")
    if start_emitter:
        args.append("--start-emitter")
    if start_receiver:
        args.append("--start-receiver")

    env = os.environ.copy()
    env["BLENDER_USER_RESOURCES"] = str(BLENDER_USER_DATA)

    print(f"[launcher] Starting Blender instance {role} ...")
    return subprocess.Popen(args, cwd=str(REPO_ROOT), env=env)


def main():
    ap = argparse.ArgumentParser(description="Launch USD Connect debug session")
    ap.add_argument("--server-port", type=int, default=7200)
    ap.add_argument("--server-host", default="127.0.0.1")
    ap.add_argument("--two-blenders", action="store_true")
    ap.add_argument("--start-emitter", action="store_true")
    ap.add_argument("--start-receiver", action="store_true")
    ap.add_argument("--debug-port", type=int, default=5678)
    ap.add_argument("--debug-port-b", type=int, default=5679)
    ap.add_argument("--wait-for-debugger", action="store_true")
    ap.add_argument("--reload", action="store_true",
                    help="Build addon and signal running Blenders to reload")
    ap.add_argument("--no-server", action="store_true",
                    help="Skip starting the server (connect to an existing one)")
    ap.add_argument("--blender-exe", default="")
    ap.add_argument("--base-usd", default="")
    ap.add_argument("--log-path", default="")
    args = ap.parse_args()

    base_usd = args.base_usd or str(REPO_ROOT / "test_scene.usda")
    log_path = args.log_path or str(REPO_ROOT / "usd_events.db")

    # Reload mode — build and signal, then exit
    if args.reload:
        _reload_addon()
        return

    blender_exe = _find_blender(args.blender_exe)
    if not pathlib.Path(blender_exe).exists():
        raise SystemExit(f"Blender executable not found: {blender_exe}")
    if not BOOTSTRAP_SCRIPT.exists():
        raise SystemExit(f"Bootstrap script not found: {BOOTSTRAP_SCRIPT}")
    if not args.no_server and not pathlib.Path(base_usd).exists():
        raise SystemExit(f"Base USD file not found: {base_usd}")

    _build_addon()

    # Start server (unless --no-server)
    server_proc = None
    if not args.no_server:
        python = _find_uv_python()
        server_proc = _start_server(
            python, args.server_host, args.server_port, base_usd, log_path,
        )
        time.sleep(1)

    # Start Blender A
    blender_a = _start_blender(
        blender_exe, "A", args.server_host, args.server_port,
        args.debug_port, args.wait_for_debugger,
        args.start_emitter, args.start_receiver,
    )

    # Start Blender B (optional)
    blender_b = None
    if args.two_blenders:
        blender_b = _start_blender(
            blender_exe, "B", args.server_host, args.server_port,
            args.debug_port_b, args.wait_for_debugger,
            args.start_emitter, args.start_receiver,
        )

    # Print session info
    print()
    print("=========================================")
    print(" USD Connect Debug Session")
    print("=========================================")
    print()
    if server_proc:
        print(f"  Server           PID {server_proc.pid}")
    else:
        print(f"  Server           (external on {args.server_host}:{args.server_port})")
    print(f"  Blender A        PID {blender_a.pid}"
          f"    debug :{args.debug_port}")
    if blender_b:
        print(f"  Blender B        PID {blender_b.pid}"
              f"    debug :{args.debug_port_b}")
    print()
    if args.wait_for_debugger:
        print(f"[launcher] Blender A waiting for debugger on :{args.debug_port}")
        if blender_b:
            print(f"[launcher] Blender B waiting for debugger"
                  f" on :{args.debug_port_b}")
        print()

    pids = [blender_a.pid]
    if server_proc:
        pids.insert(0, server_proc.pid)
    if blender_b:
        pids.append(blender_b.pid)
    print(f"[launcher] PIDs: {', '.join(str(p) for p in pids)}")
    print("[launcher] Waiting for Blender to exit"
          " (close Blender windows to stop)...")
    print()

    # Wait for Blender(s) to exit, then stop server
    try:
        blender_a.wait()
        print("[launcher] Blender A exited.")
        if blender_b:
            blender_b.wait()
            print("[launcher] Blender B exited.")
    except KeyboardInterrupt:
        print("\n[launcher] Interrupted.")

    if server_proc:
        print(f"[launcher] Stopping server (PID {server_proc.pid})...")
        server_proc.terminate()
        try:
            server_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server_proc.kill()
    print("[launcher] Session ended.")


if __name__ == "__main__":
    main()
