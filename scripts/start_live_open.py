"""Start a local live-open session with a write-capable file mirror.

This helper starts:

* the OpenUSDConnect sync server
* the VFS endpoint
* the local bridge that exposes ``scene.usd`` as a normal file

Windows additionally exposes the mirror through a ``subst`` drive alias. Use
``stop`` to terminate the recorded processes and release that alias.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse


@dataclass(frozen=True)
class DirectoryExposureConfig:
    pass


@dataclass(frozen=True)
class WindowsDriveExposureConfig:
    drive: str
    force: bool


ExposureConfig = DirectoryExposureConfig | WindowsDriveExposureConfig


@dataclass(frozen=True)
class LiveOpenConfig:
    base: str
    host: str
    port: int
    vfs_port: int
    mirror_dir: Path
    state_file: Path
    log_dir: Path
    write_mode: Literal["forbid", "drop", "translate"]
    bypass_write_validation: bool
    dashboard: int
    open_exposure: bool
    wait_timeout: float
    exposure: ExposureConfig


@dataclass(frozen=True)
class StopConfig:
    state_file: Path
    drive: str


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _is_windows() -> bool:
    return os.name == "nt"


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
        except (OSError, http.client.HTTPException) as exc:
            last_error = str(exc)
        time.sleep(0.25)
    raise TimeoutError(f"timed out waiting for {url}: {last_error}")


def _wait_for_bridge(path: Path, process: subprocess.Popen, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        if path.exists():
            try:
                status = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                last_error = str(exc)
            else:
                if status.get("state") in {"running", "seeded"}:
                    required = {"exposure_kind", "root_path", "file_path"}
                    missing = required.difference(status)
                    if not missing:
                        return status
                    last_error = f"bridge status missing: {', '.join(sorted(missing))}"
                else:
                    last_error = str(status.get("error") or status.get("state") or "starting")
        returncode = process.poll()
        if returncode is not None:
            raise RuntimeError(f"local VFS bridge exited with status {returncode}: {last_error}")
        time.sleep(0.1)
    raise TimeoutError(f"timed out waiting for {path}: {last_error}")


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _start_process(cmd: list[str], log_path: Path) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("ab")
    creationflags = 0
    if _is_windows():
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        return subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            start_new_session=not _is_windows(),
            cwd=str(Path.cwd()),
        )
    finally:
        log.close()


def _stop_pid(pid: int) -> None:
    if pid <= 0 or pid == os.getpid():
        return
    if _is_windows():
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def _build_start_parser(*, is_windows: bool | None = None) -> argparse.ArgumentParser:
    is_windows = _is_windows() if is_windows is None else is_windows
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="USD file for the sync server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7200)
    parser.add_argument("--vfs-port", type=int, default=7280)
    parser.add_argument("--mirror-dir", default=".ouc_live_mount/usd")
    parser.add_argument("--state-file", default=".ouc_live_mount/live_open_session.json")
    parser.add_argument("--log-dir", default=".ouc_live_mount")
    parser.add_argument(
        "--write-mode", choices=["forbid", "drop", "translate"], default="translate"
    )
    parser.add_argument(
        "--bypass-write-validation",
        action="store_true",
        help="Let translate write fallback accept and drop invalid USD bytes.",
    )
    parser.add_argument("--dashboard", type=int, default=0)
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--wait", type=float, default=20.0)
    if is_windows:
        exposure = parser.add_mutually_exclusive_group()
        exposure.add_argument("--drive", default=None, help="Drive alias (default: O:)")
        exposure.add_argument(
            "--no-drive",
            action="store_true",
            help="Expose only the local directory",
        )
        parser.add_argument("--force", action="store_true", help="Replace an existing drive alias")
    return parser


def _parse_start_config(
    argv: list[str],
    *,
    is_windows: bool | None = None,
) -> LiveOpenConfig:
    is_windows = _is_windows() if is_windows is None else is_windows
    parser = _build_start_parser(is_windows=is_windows)
    args = parser.parse_args(argv)
    if args.wait <= 0:
        parser.error("--wait must be greater than zero")
    if is_windows and args.no_drive and args.force:
        parser.error("--force requires a Windows drive exposure")
    if is_windows and not args.no_drive:
        exposure: ExposureConfig = WindowsDriveExposureConfig(
            drive=args.drive or "O:",
            force=args.force,
        )
    else:
        exposure = DirectoryExposureConfig()
    return LiveOpenConfig(
        base=args.base,
        host=args.host,
        port=args.port,
        vfs_port=args.vfs_port,
        mirror_dir=Path(args.mirror_dir).resolve(),
        state_file=Path(args.state_file).resolve(),
        log_dir=Path(args.log_dir).resolve(),
        write_mode=args.write_mode,
        bypass_write_validation=args.bypass_write_validation,
        dashboard=args.dashboard,
        open_exposure=args.open,
        wait_timeout=args.wait,
        exposure=exposure,
    )


def _run_start(config: LiveOpenConfig) -> int:
    log_dir = config.log_dir
    state_file = config.state_file
    mirror_dir = config.mirror_dir
    bridge_dir = log_dir / "bridge"
    bridge_status = bridge_dir / "openusdconnect_bridge_status.json"
    bridge_log = bridge_dir / "openusdconnect_bridge.log"
    bridge_process_log = bridge_dir / "openusdconnect_bridge_process.log"
    bridge_status.unlink(missing_ok=True)
    server_log = log_dir / "openusdconnect_server.log"
    server_db = log_dir / f"live-open-{config.port}.db"
    vfs_url = f"http://{config.host}:{config.vfs_port}/usd/scene.usd"

    server_cmd = [
        sys.executable,
        "-m",
        "openusdconnect.server",
        "--host",
        config.host,
        "--port",
        str(config.port),
        "--base",
        config.base,
        "--log",
        str(server_db),
        "--vfs-port",
        str(config.vfs_port),
        "--vfs-write-mode",
        config.write_mode,
    ]
    if config.bypass_write_validation:
        server_cmd.append("--vfs-bypass-write-validation")
    if config.dashboard:
        server_cmd.extend(["--dashboard", str(config.dashboard)])

    server = _start_process(server_cmd, server_log)
    try:
        _wait_for_http(vfs_url, config.wait_timeout)
    except (OSError, TimeoutError, http.client.HTTPException):
        _stop_pid(server.pid)
        raise

    bridge_cmd = [
        sys.executable,
        str(Path(__file__).with_name("local_vfs_bridge.py")),
        "--url",
        vfs_url,
        "--mirror-dir",
        str(mirror_dir),
        "--status-file",
        str(bridge_status),
        "--log-file",
        str(bridge_log),
    ]
    if isinstance(config.exposure, WindowsDriveExposureConfig):
        bridge_cmd.extend(["--drive", config.exposure.drive])
        if config.exposure.force:
            bridge_cmd.append("--force")
    elif _is_windows():
        bridge_cmd.append("--no-drive")
    if config.open_exposure:
        bridge_cmd.append("--open")
    bridge = _start_process(bridge_cmd, bridge_process_log)
    try:
        bridge_state = _wait_for_bridge(bridge_status, bridge, config.wait_timeout)
    except (OSError, RuntimeError, TimeoutError):
        _stop_pid(bridge.pid)
        _stop_pid(server.pid)
        raise

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
        "bridge_pid": int(bridge_state.get("pid") or bridge.pid),
        "exposure_kind": bridge_state["exposure_kind"],
        "root_path": bridge_state["root_path"],
        "drive": bridge_state.get("drive", ""),
        "file_path": bridge_state["file_path"],
        "write_mode": config.write_mode,
        "write_validation": config.write_mode == "translate" and not config.bypass_write_validation,
    }
    _write_json(state_file, payload)
    print(f"Server PID: {server.pid}")
    print(f"VFS URL: {vfs_url}")
    print(f"Live USD file: {payload['file_path']}")
    print(f"State: {state_file}")
    return 0


def _build_stop_parser(*, is_windows: bool | None = None) -> argparse.ArgumentParser:
    is_windows = _is_windows() if is_windows is None else is_windows
    parser = argparse.ArgumentParser(description="Stop a live-open session")
    parser.add_argument("--state-file", default=".ouc_live_mount/live_open_session.json")
    if is_windows:
        parser.add_argument("--drive", default="", help="Drive alias if state is unavailable")
    return parser


def _parse_stop_config(
    argv: list[str],
    *,
    is_windows: bool | None = None,
) -> StopConfig:
    is_windows = _is_windows() if is_windows is None else is_windows
    parser = _build_stop_parser(is_windows=is_windows)
    args = parser.parse_args(argv)
    return StopConfig(
        state_file=Path(args.state_file).resolve(),
        drive=args.drive if is_windows else "",
    )


def _run_stop(config: StopConfig) -> int:
    state_file = config.state_file
    if not state_file.exists():
        print(f"state file not found: {state_file}", file=sys.stderr)
        return 1
    state = json.loads(state_file.read_text(encoding="utf-8"))
    drive = config.drive or state.get("drive") or ""
    bridge_status = state.get("bridge_status") or ""
    bridge_pid = int(state.get("bridge_pid") or 0)
    bridge_cmd = [
        sys.executable,
        str(Path(__file__).with_name("local_vfs_bridge.py")),
        "stop",
        "--stop-process",
        "--pid",
        str(bridge_pid),
    ]
    if drive and _is_windows():
        bridge_cmd.extend(["--drive", drive])
    if bridge_status:
        bridge_cmd.extend(["--status-file", bridge_status])
    subprocess.run(bridge_cmd, check=False)
    _stop_pid(int(state.get("server_pid") or 0))
    state["stopped_at"] = _now()
    _write_json(state_file, state)
    print(f"Stopped live-open session from {state_file}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv and argv[0] == "stop":
        return _run_stop(_parse_stop_config(argv[1:]))
    if argv and argv[0] == "start":
        argv = argv[1:]
    return _run_start(_parse_start_config(argv))


if __name__ == "__main__":
    raise SystemExit(main())
