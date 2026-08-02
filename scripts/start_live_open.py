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
import ctypes
import hashlib
import http.client
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from openusdconnect.cli_common import (
    add_hidden_aliases,
    add_sync_endpoint_args,
    add_vfs_resource_args,
    port_or_zero,
    positive_seconds,
    validate_port,
)
from openusdconnect.defaults import (
    DEFAULT_LIVE_OPEN_ROOT,
    DEFAULT_MIRROR_DIR,
    DEFAULT_SESSION_STATE_FILE,
    DEFAULT_STARTUP_TIMEOUT,
    VFS_WRITE_MODES,
    advertise_host_for_bind,
    vfs_url,
)


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
    vfs_host: str | None
    vfs_port: int
    vfs_share: str
    vfs_name: str
    advertise_host: str | None
    mirror_dir: Path
    state_file: Path
    log_dir: Path
    vfs_write_mode: Literal["forbid", "drop", "translate"]
    vfs_bypass_write_validation: bool
    dashboard_port: int
    open_exposure: bool
    startup_timeout: float
    exposure: ExposureConfig
    session_id: str


@dataclass(frozen=True)
class StopConfig:
    state_file: Path
    drive: str


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _is_windows() -> bool:
    return os.name == "nt"


def _request_live_metadata(url: str) -> tuple[int, dict]:
    parsed = urlparse(url)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=3)
    try:
        conn.request("GET", parsed.path or "/")
        resp = conn.getresponse()
        data = resp.read()
        if not (200 <= resp.status < 300):
            return resp.status, {}
        from pxr import Sdf

        layer = Sdf.Layer.CreateAnonymous("live-open-startup.usda")
        if not layer.ImportFromString(data.decode("utf-8")):
            raise RuntimeError("endpoint did not return a parseable USD layer")
        metadata = layer.customLayerData.get("openusdconnect")
        if not isinstance(metadata, dict):
            raise RuntimeError("endpoint USD has no OpenUSDConnect metadata")
        return resp.status, metadata
    finally:
        conn.close()


def _wait_for_http(
    url: str,
    timeout: float,
    *,
    process: subprocess.Popen | None = None,
    expected_scene_id: str = "",
    expected_sync_port: int = 0,
) -> dict:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"OpenUSDConnect server exited with status {process.poll()}")
        try:
            status, metadata = _request_live_metadata(url)
            if 200 <= status < 300:
                if metadata.get("live") is not True:
                    last_error = "endpoint metadata is not live"
                elif expected_scene_id and metadata.get("scene_id") != expected_scene_id:
                    last_error = (
                        "endpoint scene mismatch: "
                        f"expected {expected_scene_id}, got {metadata.get('scene_id') or 'none'}"
                    )
                elif expected_sync_port:
                    try:
                        metadata_port = validate_port(metadata.get("port"))
                    except ValueError:
                        last_error = "endpoint metadata has no valid sync port"
                    else:
                        if metadata_port == expected_sync_port:
                            if process is not None:
                                time.sleep(0.05)
                                if process.poll() is not None:
                                    raise RuntimeError(
                                        "OpenUSDConnect server exited with status "
                                        f"{process.poll()}"
                                    )
                            return metadata
                        last_error = (
                            "endpoint sync-port mismatch: "
                            f"expected {expected_sync_port}, got {metadata_port}"
                        )
                else:
                    if process is not None:
                        time.sleep(0.05)
                        if process.poll() is not None:
                            raise RuntimeError(
                                f"OpenUSDConnect server exited with status {process.poll()}"
                            )
                    return metadata
            else:
                last_error = f"HTTP {status}"
        except (OSError, RuntimeError, ValueError, http.client.HTTPException) as exc:
            last_error = str(exc)
        time.sleep(0.25)
    raise TimeoutError(f"timed out waiting for {url}: {last_error}")


def _expected_scene_id(base_usd_path: str) -> str:
    label = os.path.splitext(os.path.basename(base_usd_path))[0] or "scene"
    digest = hashlib.sha1(os.path.abspath(base_usd_path).encode()).hexdigest()[:12]
    return f"{label}-{digest}"


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
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2, sort_keys=True)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if _is_windows():
        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _assert_session_available(state_file: Path) -> None:
    if not state_file.exists():
        return
    try:
        state = _read_json(state_file)
    except (OSError, json.JSONDecodeError):
        return
    if state.get("stopped_at"):
        return
    live_pids = [
        pid
        for pid in (int(state.get("server_pid") or 0), int(state.get("bridge_pid") or 0))
        if _process_exists(pid)
    ]
    if live_pids:
        raise RuntimeError(
            f"live-open session is already active ({', '.join(map(str, live_pids))}): "
            f"{state_file}"
        )


def _acquire_start_lock(state_file: Path, session_id: str) -> Path:
    lock = state_file.with_suffix(state_file.suffix + ".start.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    for _attempt in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                existing = _read_json(lock)
            except (OSError, json.JSONDecodeError):
                existing = {}
            pid = int(existing.get("pid") or 0)
            if pid and _process_exists(pid):
                raise RuntimeError(f"another launcher owns {state_file}") from None
            lock.unlink(missing_ok=True)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump({"pid": os.getpid(), "session_id": session_id}, file)
        return lock
    raise RuntimeError(f"could not claim launcher state {state_file}")


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
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0 and _process_exists(pid):
            detail = (result.stdout + result.stderr).strip()
            raise RuntimeError(detail or f"taskkill failed for PID {pid}")
        if _process_exists(pid):
            raise RuntimeError(f"PID {pid} is still running after taskkill")
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if not _process_exists(pid):
                return
            time.sleep(0.05)
        raise RuntimeError(f"PID {pid} did not terminate")


def _build_start_parser(*, is_windows: bool | None = None) -> argparse.ArgumentParser:
    is_windows = _is_windows() if is_windows is None else is_windows
    parser = argparse.ArgumentParser(description=__doc__)
    endpoint = parser.add_argument_group("sync endpoint")
    add_sync_endpoint_args(endpoint)
    scene = parser.add_argument_group("scene and local files")
    scene.add_argument("--base", required=True, help="USD file for the sync server")
    scene.add_argument("--mirror-dir", default=DEFAULT_MIRROR_DIR)
    scene.add_argument("--state-file", default=DEFAULT_SESSION_STATE_FILE)
    scene.add_argument("--log-dir", default=DEFAULT_LIVE_OPEN_ROOT)
    vfs = parser.add_argument_group("virtual file service")
    add_vfs_resource_args(vfs, host_default=None)
    vfs.add_argument(
        "--advertise-host",
        default=None,
        metavar="HOST",
        help="Host embedded in live metadata and used by the local bridge",
    )
    vfs.add_argument(
        "--vfs-write-mode",
        choices=VFS_WRITE_MODES,
        default="translate",
        help="How saves to the virtual USD file are handled",
    )
    add_hidden_aliases(parser, ["--write-mode"], dest="vfs_write_mode", choices=VFS_WRITE_MODES)
    vfs.add_argument(
        "--vfs-bypass-write-validation",
        action="store_true",
        help="Let translate write fallback accept and drop invalid USD bytes.",
    )
    add_hidden_aliases(
        parser,
        ["--bypass-write-validation"],
        dest="vfs_bypass_write_validation",
        action="store_true",
    )
    services = parser.add_argument_group("services and startup")
    services.add_argument(
        "--dashboard-port",
        type=port_or_zero,
        default=0,
        metavar="PORT",
        help="Start the admin dashboard on this port (0 disables it)",
    )
    add_hidden_aliases(parser, ["--dashboard"], dest="dashboard_port", type=port_or_zero)
    services.add_argument("--open", action="store_true", help="Open the local exposure")
    services.add_argument(
        "--startup-timeout",
        type=positive_seconds,
        default=DEFAULT_STARTUP_TIMEOUT,
        metavar="SECONDS",
        help="Seconds to wait for the server and bridge",
    )
    add_hidden_aliases(
        parser,
        ["--wait"],
        dest="startup_timeout",
        type=positive_seconds,
    )
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
        vfs_host=args.vfs_host,
        vfs_port=args.vfs_port,
        vfs_share=args.vfs_share,
        vfs_name=args.vfs_name,
        advertise_host=args.advertise_host,
        mirror_dir=Path(args.mirror_dir).resolve(),
        state_file=Path(args.state_file).resolve(),
        log_dir=Path(args.log_dir).resolve(),
        vfs_write_mode=args.vfs_write_mode,
        vfs_bypass_write_validation=args.vfs_bypass_write_validation,
        dashboard_port=args.dashboard_port,
        open_exposure=args.open,
        startup_timeout=args.startup_timeout,
        exposure=exposure,
        session_id=uuid.uuid4().hex,
    )


def _run_start(config: LiveOpenConfig) -> int:
    _assert_session_available(config.state_file)
    lock = _acquire_start_lock(config.state_file, config.session_id)
    try:
        return _run_start_claimed(config)
    finally:
        lock.unlink(missing_ok=True)


def _run_start_claimed(config: LiveOpenConfig) -> int:
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
    vfs_bind_host = config.vfs_host or config.host
    public_host = config.advertise_host or advertise_host_for_bind(vfs_bind_host)
    live_vfs_url = vfs_url(
        public_host,
        config.vfs_port,
        config.vfs_share,
        config.vfs_name,
    )

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
        "--event-log",
        str(server_db),
        "--vfs-host",
        vfs_bind_host,
        "--vfs-port",
        str(config.vfs_port),
        "--vfs-share",
        config.vfs_share,
        "--vfs-name",
        config.vfs_name,
        "--advertise-host",
        public_host,
        "--vfs-write-mode",
        config.vfs_write_mode,
    ]
    if config.vfs_bypass_write_validation:
        server_cmd.append("--vfs-bypass-write-validation")
    if config.dashboard_port:
        server_cmd.extend(["--dashboard-port", str(config.dashboard_port)])

    server = _start_process(server_cmd, server_log)
    try:
        _wait_for_http(
            live_vfs_url,
            config.startup_timeout,
            process=server,
            expected_scene_id=_expected_scene_id(config.base),
            expected_sync_port=config.port,
        )
    except (OSError, RuntimeError, TimeoutError, http.client.HTTPException):
        try:
            _stop_pid(server.pid)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            pass
        raise

    bridge_cmd = [
        sys.executable,
        str(Path(__file__).with_name("local_vfs_bridge.py")),
        "--vfs-url",
        live_vfs_url,
        "--mirror-dir",
        str(mirror_dir),
        "--status-file",
        str(bridge_status),
        "--log-file",
        str(bridge_log),
        "--owner-id",
        config.session_id,
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
        bridge_state = _wait_for_bridge(bridge_status, bridge, config.startup_timeout)
    except (OSError, RuntimeError, TimeoutError):
        _stop_pid(bridge.pid)
        _stop_pid(server.pid)
        raise

    payload = {
        "started_at": _now(),
        "session_id": config.session_id,
        "server_pid": server.pid,
        "server_cmd": server_cmd,
        "server_log": str(server_log),
        "server_db": str(server_db),
        "vfs_url": live_vfs_url,
        "bridge_status": str(bridge_status),
        "bridge_log": str(bridge_log),
        "bridge_process_log": str(bridge_process_log),
        "bridge_pid": int(bridge_state.get("pid") or bridge.pid),
        "exposure_kind": bridge_state["exposure_kind"],
        "root_path": bridge_state["root_path"],
        "drive": bridge_state.get("drive", ""),
        "file_path": bridge_state["file_path"],
        "write_mode": config.vfs_write_mode,
        "write_validation": config.vfs_write_mode == "translate"
        and not config.vfs_bypass_write_validation,
    }
    _write_json(state_file, payload)
    print(f"Server PID: {server.pid}")
    print(f"VFS URL: {live_vfs_url}")
    print(f"Live USD file: {payload['file_path']}")
    print(f"State: {state_file}")
    return 0


def _build_stop_parser(*, is_windows: bool | None = None) -> argparse.ArgumentParser:
    is_windows = _is_windows() if is_windows is None else is_windows
    parser = argparse.ArgumentParser(description="Stop a live-open session")
    parser.add_argument("--state-file", default=DEFAULT_SESSION_STATE_FILE)
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
    try:
        state = _read_json(state_file)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"could not read live-open state {state_file}: {exc}", file=sys.stderr)
        return 1
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
    errors: list[str] = []
    try:
        helper = subprocess.run(
            bridge_cmd,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        errors.append(f"bridge cleanup helper failed: {exc}")
    else:
        if helper.returncode != 0:
            detail = (helper.stdout + helper.stderr).strip()
            errors.append(
                f"bridge cleanup helper exited with status {helper.returncode}"
                + (f": {detail}" if detail else "")
            )

    for label, pid in (
        ("bridge", bridge_pid),
        ("server", int(state.get("server_pid") or 0)),
    ):
        if not pid:
            continue
        try:
            _stop_pid(pid)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            errors.append(f"could not stop {label} PID {pid}: {exc}")

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    state["stopped_at"] = _now()
    try:
        _write_json(state_file, state)
    except OSError as exc:
        print(f"cleanup succeeded but state could not be updated: {exc}", file=sys.stderr)
        return 1
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
