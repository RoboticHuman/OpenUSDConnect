"""Start a production-style local live-open session.

This helper intentionally uses the no-admin local drive bridge instead of the
Windows WebClient/WebDAV mount. It starts:

* the OpenUSDConnect sync server
* the VFS endpoint
* the local ``subst`` bridge that exposes ``scene.usd`` as a normal drive file

Use ``stop`` to terminate the recorded server process and unmount the bridge.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _request_head(url: str) -> int:
    parsed = urlparse(url)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=3)
    try:
        conn.request("HEAD", parsed.path or "/")
        resp = conn.getresponse()
        resp.read()
        return resp.status
    finally:
        conn.close()


def _wait_for_http(url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            status = _request_head(url)
            if 200 <= status < 300:
                return
            last_error = f"HTTP {status}"
        except Exception as exc:
            last_error = str(exc)
        time.sleep(0.25)
    raise TimeoutError(f"timed out waiting for {url}: {last_error}")


def _wait_for_file(path: Path, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.1)
    raise TimeoutError(f"timed out waiting for {path}")


def _drive_name(drive: str) -> str:
    drive = drive.rstrip("\\/")
    if not drive.endswith(":"):
        drive += ":"
    return drive.upper()


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _start_process(cmd: list[str], log_path: Path) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("ab")
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        return subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            cwd=str(Path.cwd()),
        )
    finally:
        log.close()


def _stop_pid(pid: int) -> None:
    if pid <= 0 or pid == os.getpid():
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    else:
        os.kill(pid, 15)


def _run_start(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="USD file for the sync server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7200)
    parser.add_argument("--vfs-port", type=int, default=7280)
    parser.add_argument("--drive", default="O:")
    parser.add_argument("--mount-dir", default=".ouc_live_mount/usd")
    parser.add_argument("--state-file", default=".ouc_live_mount/live_open_session.json")
    parser.add_argument("--log-dir", default=".ouc_live_mount")
    parser.add_argument("--write-mode", choices=["forbid", "drop", "translate"], default="translate")
    parser.add_argument(
        "--bypass-write-validation",
        action="store_true",
        help="Let translate write fallback accept and drop invalid USD bytes.",
    )
    parser.add_argument("--dashboard", type=int, default=0)
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--wait", type=float, default=20.0)
    args = parser.parse_args(argv)

    log_dir = Path(args.log_dir).resolve()
    state_file = Path(args.state_file).resolve()
    mount_dir = Path(args.mount_dir).resolve()
    bridge_dir = log_dir / "bridge"
    bridge_status = bridge_dir / "openusdconnect_bridge_status.json"
    bridge_log = bridge_dir / "openusdconnect_bridge.log"
    bridge_process_log = bridge_dir / "openusdconnect_bridge_process.log"
    bridge_status.unlink(missing_ok=True)
    server_log = log_dir / "openusdconnect_server.log"
    server_db = log_dir / f"live-open-{args.port}.db"
    vfs_url = f"http://{args.host}:{args.vfs_port}/usd/scene.usd"

    server_cmd = [
        sys.executable,
        "-m",
        "openusdconnect.server",
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--base",
        args.base,
        "--log",
        str(server_db),
        "--vfs-port",
        str(args.vfs_port),
        "--vfs-write-mode",
        args.write_mode,
    ]
    if args.bypass_write_validation:
        server_cmd.append("--vfs-bypass-write-validation")
    if args.dashboard:
        server_cmd.extend(["--dashboard", str(args.dashboard)])

    server = _start_process(server_cmd, server_log)
    try:
        _wait_for_http(vfs_url, args.wait)
    except Exception:
        _stop_pid(server.pid)
        raise

    bridge_cmd = [
        sys.executable,
        str(Path(__file__).with_name("local_vfs_drive_bridge.py")),
        "--url",
        vfs_url,
        "--mount-dir",
        str(mount_dir),
        "--drive",
        args.drive,
        "--status-file",
        str(bridge_status),
        "--log-file",
        str(bridge_log),
    ]
    if args.force:
        bridge_cmd.append("--force")
    if args.open:
        bridge_cmd.append("--open")
    bridge = _start_process(bridge_cmd, bridge_process_log)
    _wait_for_file(bridge_status, args.wait)

    payload = {
        "started_at": _now(),
        "server_pid": server.pid,
        "server_cmd": server_cmd,
        "server_log": str(server_log),
        "server_db": str(server_db),
        "vfs_url": vfs_url,
        "bridge_status": str(bridge_status),
        "bridge_log": str(bridge_log),
        "bridge_process_log": str(bridge_process_log),
        "bridge_pid": bridge.pid,
        "drive": _drive_name(args.drive),
        "file_path": f"{_drive_name(args.drive)}\\scene.usd",
        "write_mode": args.write_mode,
        "write_validation": args.write_mode == "translate" and not args.bypass_write_validation,
    }
    _write_json(state_file, payload)
    print(f"Server PID: {server.pid}")
    print(f"VFS URL: {vfs_url}")
    print(f"Live USD file: {payload['file_path']}")
    print(f"State: {state_file}")
    return 0


def _run_stop(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Stop a live-open session")
    parser.add_argument("--state-file", default=".ouc_live_mount/live_open_session.json")
    parser.add_argument("--drive", default="")
    args = parser.parse_args(argv)
    state_file = Path(args.state_file).resolve()
    if not state_file.exists():
        print(f"state file not found: {state_file}", file=sys.stderr)
        return 1
    state = json.loads(state_file.read_text(encoding="utf-8"))
    drive = args.drive or state.get("drive") or "O:"
    bridge_status = state.get("bridge_status") or ""
    bridge_pid = int(state.get("bridge_pid") or 0)
    bridge_cmd = [
        sys.executable,
        str(Path(__file__).with_name("local_vfs_drive_bridge.py")),
        "unmount",
        "--drive",
        drive,
        "--stop-process",
    ]
    if bridge_status:
        bridge_cmd.extend(["--status-file", bridge_status])
    subprocess.run(bridge_cmd, check=False)
    _stop_pid(bridge_pid)
    _stop_pid(int(state.get("server_pid") or 0))
    state["stopped_at"] = _now()
    _write_json(state_file, state)
    print(f"Stopped live-open session from {state_file}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv and argv[0] == "stop":
        return _run_stop(argv[1:])
    if argv and argv[0] == "start":
        argv = argv[1:]
    return _run_start(argv)


if __name__ == "__main__":
    raise SystemExit(main())
