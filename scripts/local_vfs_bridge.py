"""Mirror one OpenUSDConnect VFS file for reliable local open and save.

The bridge downloads a managed virtual file into a normal local directory,
keeps it current, and uploads completed local saves with an ETag guard. Windows
may additionally expose the directory through a ``subst`` drive alias.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import ctypes.wintypes
import hashlib
import http.client
import json
import logging
import os
import platform
import signal
import string
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from openusdconnect.cli_common import (
    nonnegative_seconds,
    positive_seconds,
)
from openusdconnect.defaults import (
    DEFAULT_BRIDGE_POLL_INTERVAL,
    DEFAULT_BRIDGE_SETTLE_TIME,
    DEFAULT_BRIDGE_STATUS_FILE,
    DEFAULT_HOST,
    DEFAULT_MIRROR_DIR,
    DEFAULT_VFS_NAME,
    DEFAULT_VFS_PORT,
    DEFAULT_VFS_SHARE,
    vfs_url,
)

LOG = logging.getLogger("openusdconnect.vfs_bridge")


@dataclass(frozen=True)
class LocalExposure:
    kind: str
    local_root: Path
    root_path: str
    file_path: str
    drive: str = ""


@dataclass(frozen=True)
class DirectoryExposureConfig:
    pass


@dataclass(frozen=True)
class WindowsDriveExposureConfig:
    drive: str
    force: bool
    release_on_exit: bool


ExposureConfig = DirectoryExposureConfig | WindowsDriveExposureConfig


@dataclass(frozen=True)
class BridgeConfig:
    vfs_url: str
    mirror_dir: Path
    poll_interval: float
    settle_time: float
    once: bool
    background: bool
    open: bool
    status_file: Path
    log_file: Path | None
    verbose: bool
    exposure: ExposureConfig
    owner_id: str


@dataclass(frozen=True)
class StopConfig:
    status_file: Path | None
    pid: int
    stop_process: bool
    cleanup_status: bool
    drive: str


@dataclass(frozen=True)
class FileObservation:
    mtime_ns: int
    size: int
    digest: str


@dataclass(frozen=True)
class SaveCandidate:
    observation: FileObservation
    stable_since: float


@dataclass(frozen=True)
class ControlPaths:
    root: Path
    lock: Path
    sync_state: Path
    recovery_dir: Path


class HttpRequestError(RuntimeError):
    def __init__(self, method: str, url: str, status: int):
        super().__init__(f"{method} {url} failed with HTTP {status}")
        self.status = status


class LocalFileChangedError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _drive_name(drive: str) -> str:
    value = drive.strip().upper().rstrip("\\/")
    if len(value) == 1 and value in string.ascii_uppercase:
        value += ":"
    if len(value) != 2 or value[0] not in string.ascii_uppercase or value[1] != ":":
        raise ValueError("drive must look like O: or O")
    return value


def _is_windows() -> bool:
    return os.name == "nt"


def _is_macos() -> bool:
    return platform.system() == "Darwin"


def _request(
    method: str,
    url: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("VFS URL must use HTTP or HTTPS and include a host")
    connection_type = (
        http.client.HTTPSConnection
        if parsed.scheme == "https"
        else http.client.HTTPConnection
    )
    default_port = 443 if parsed.scheme == "https" else 80
    conn = connection_type(parsed.hostname, parsed.port or default_port, timeout=10)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    try:
        request_headers = dict(headers or {})
        if body is not None:
            request_headers["Content-Length"] = str(len(body))
        conn.request(method, path, body=body, headers=request_headers)
        resp = conn.getresponse()
        data = resp.read()
        response_headers = {name.lower(): value for name, value in resp.getheaders()}
        return resp.status, response_headers, data
    finally:
        conn.close()


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@contextlib.contextmanager
def _open_shared_read(path: Path):
    if not _is_windows():
        with path.open("rb") as file:
            yield file
        return

    import msvcrt

    generic_read = 0x80000000
    share_read_write_delete = 0x00000001 | 0x00000002 | 0x00000004
    open_existing = 3
    file_attribute_normal = 0x00000080
    invalid_handle_value = ctypes.wintypes.HANDLE(-1).value
    create_file = ctypes.windll.kernel32.CreateFileW  # type: ignore[attr-defined]
    create_file.argtypes = [
        ctypes.wintypes.LPCWSTR,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.HANDLE,
    ]
    create_file.restype = ctypes.wintypes.HANDLE
    handle = create_file(
        str(path),
        generic_read,
        share_read_write_delete,
        None,
        open_existing,
        file_attribute_normal,
        None,
    )
    if handle == invalid_handle_value:
        raise ctypes.WinError()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    try:
        fd = msvcrt.open_osfhandle(int(handle), flags)
    except OSError:
        ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
        raise
    with os.fdopen(fd, "rb") as file:
        yield file


def _read_file_bytes(path: Path) -> bytes:
    with _open_shared_read(path) as file:
        return file.read()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with _open_shared_read(path) as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _observe_file(path: Path) -> FileObservation:
    before = path.stat()
    digest = _hash_file(path)
    after = path.stat()
    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
        raise LocalFileChangedError(f"{path} changed while it was being read")
    return FileObservation(after.st_mtime_ns, after.st_size, digest)


def _observe_if_changed(path: Path, previous: FileObservation) -> FileObservation:
    current = path.stat()
    if (current.st_mtime_ns, current.st_size) == (previous.mtime_ns, previous.size):
        return previous
    return _observe_file(path)


def _read_file_version(path: Path, expected_hash: str) -> bytes:
    data = _read_file_bytes(path)
    if _hash_bytes(data) != expected_hash:
        raise LocalFileChangedError(f"{path} changed before its save was complete")
    return data


def _content_changed(path: Path, previous_hash: str) -> tuple[bool, str]:
    current_hash = _hash_file(path)
    return current_hash != previous_hash, current_hash


def _fetch(url: str) -> tuple[str, bytes, str]:
    status, headers, data = _request("GET", url)
    if not (200 <= status < 300):
        raise HttpRequestError("GET", url, status)
    return headers.get("etag", ""), data, _hash_bytes(data)


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _replace_remote_if_unchanged(
    path: Path,
    data: bytes,
    *,
    expected_local_hash: str | None,
) -> str:
    if expected_local_hash is not None and path.exists():
        current_hash = _hash_file(path)
        if current_hash != expected_local_hash:
            raise LocalFileChangedError(
                f"refusing to overwrite local edits in {path} with a remote update"
            )
    remote_hash = _hash_bytes(data)
    _write_bytes_atomic(path, data)
    return remote_hash


def _download(
    url: str,
    path: Path,
    *,
    expected_local_hash: str | None = None,
) -> tuple[str, int, str]:
    etag, data, remote_hash = _fetch(url)
    _replace_remote_if_unchanged(path, data, expected_local_hash=expected_local_hash)
    return etag, len(data), remote_hash


def _upload_bytes(url: str, data: bytes, etag: str = "") -> str:
    headers = {"If-Match": etag} if etag else None
    status, response_headers, _body = _request("PUT", url, body=data, headers=headers)
    if not (200 <= status < 300):
        raise HttpRequestError("PUT", url, status)
    return response_headers.get("etag", "")


def _upload(url: str, path: Path, etag: str = "") -> str:
    return _upload_bytes(url, _read_file_bytes(path), etag)


def _subst(drive: str, target: Path, force: bool) -> None:
    drive = _drive_name(drive)
    if force:
        subprocess.run(["subst", drive, "/D"], capture_output=True, text=True, timeout=10)
    result = subprocess.run(
        ["subst", drive, str(target)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stdout + result.stderr).strip() or f"subst {drive} failed")


def _unsubst(drive: str) -> None:
    drive = _drive_name(drive)
    result = subprocess.run(
        ["subst", drive, "/D"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        text = (result.stdout + result.stderr).strip()
        if "Invalid parameter" not in text and "not found" not in text:
            raise RuntimeError(text or f"subst {drive} /D failed")


def _describe_exposure(
    mirror_dir: Path,
    filename: str,
    config: ExposureConfig,
) -> LocalExposure:
    if isinstance(config, WindowsDriveExposureConfig):
        root_path = config.drive + "\\"
        return LocalExposure(
            kind="windows-drive",
            local_root=mirror_dir,
            root_path=root_path,
            file_path=root_path + filename,
            drive=config.drive,
        )
    return LocalExposure(
        kind="local-directory",
        local_root=mirror_dir,
        root_path=str(mirror_dir),
        file_path=str(mirror_dir / filename),
    )


def _prepare_exposure(
    mirror_dir: Path,
    filename: str,
    *,
    config: ExposureConfig,
) -> LocalExposure:
    exposure = _describe_exposure(mirror_dir, filename, config)
    if isinstance(config, WindowsDriveExposureConfig):
        _subst(config.drive, mirror_dir, config.force)
    return exposure


def _release_exposure(exposure: LocalExposure) -> None:
    if exposure.drive:
        _unsubst(exposure.drive)


def _exposure_fields(exposure: LocalExposure) -> dict[str, str]:
    return {
        "exposure_kind": exposure.kind,
        "root_path": exposure.root_path,
        "file_path": exposure.file_path,
        "mirror_dir": str(exposure.local_root),
        "drive": exposure.drive,
    }


def _replace_json_with_retry(path: Path, payload: dict, *, attempts: int = 5) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(encoded)
            file.flush()
            os.fsync(file.fileno())
        for attempt in range(attempts):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt + 1 == attempts:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        tmp.unlink(missing_ok=True)


def _same_status(path: Path, fields: dict) -> bool:
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    existing.pop("updated_at", None)
    return existing == fields


def _write_status(path: Path | None, **fields) -> bool:
    if path is None:
        return True
    if _same_status(path, fields):
        return True
    payload = {"updated_at": _now(), **fields}
    try:
        _replace_json_with_retry(path, payload)
    except OSError as exc:
        LOG.warning("Could not publish bridge status %s: %s", path, exc)
        return False
    return True


def _setup_logging(log_file: Path | None, *, verbose: bool) -> None:
    handlers: list[logging.Handler] = []
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    else:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )


def _default_status_file(mirror_dir: Path) -> Path:
    return mirror_dir.parent / "bridge" / "openusdconnect_bridge_status.json"


def _default_log_file(mirror_dir: Path) -> Path:
    return mirror_dir.parent / "bridge" / "openusdconnect_bridge.log"


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _control_paths(config: BridgeConfig, filename: str) -> ControlPaths:
    status_parent = config.status_file.parent
    root = (
        config.mirror_dir.parent / "bridge"
        if _is_within(status_parent, config.mirror_dir)
        else status_parent
    )
    key_source = f"{config.mirror_dir.resolve()}\0{config.vfs_url}".encode()
    key = hashlib.sha256(key_source).hexdigest()[:16]
    recovery_dir = root / "recovery" / key
    return ControlPaths(
        root=root,
        lock=root / "locks" / f"{key}.lock",
        sync_state=recovery_dir / "sync-state.json",
        recovery_dir=recovery_dir,
    )


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


def _read_json(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _acquire_bridge_lock(path: Path, *, owner_id: str, url: str, mirror_dir: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "owner_id": owner_id,
        "pid": os.getpid(),
        "url": url,
        "mirror_dir": str(mirror_dir),
        "created_at": _now(),
    }
    for _attempt in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                existing = _read_json(path)
            except (OSError, json.JSONDecodeError):
                existing = {}
            existing_pid = int(existing.get("pid") or 0)
            if existing_pid and _process_exists(existing_pid):
                raise RuntimeError(
                    f"mirror is already owned by bridge PID {existing_pid}: {mirror_dir}"
                ) from None
            try:
                path.unlink()
            except OSError as exc:
                raise RuntimeError(f"could not clear stale bridge lock {path}: {exc}") from exc
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2, sort_keys=True)
        return
    raise RuntimeError(f"could not acquire bridge lock {path}")


def _release_bridge_lock(path: Path, owner_id: str) -> None:
    try:
        existing = _read_json(path)
    except (OSError, json.JSONDecodeError):
        existing = {}
    if not existing or existing.get("owner_id") == owner_id:
        path.unlink(missing_ok=True)


def _load_sync_state(path: Path, config: BridgeConfig) -> dict:
    try:
        state = _read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        LOG.warning("Ignoring unreadable bridge sync state %s: %s", path, exc)
        return {}
    if state.get("url") != config.vfs_url or state.get("mirror_dir") != str(config.mirror_dir):
        return {}
    return state


def _write_sync_state(path: Path, config: BridgeConfig, **fields) -> bool:
    payload = {
        "version": 1,
        "url": config.vfs_url,
        "mirror_dir": str(config.mirror_dir),
        "updated_at": _now(),
        **fields,
    }
    try:
        _replace_json_with_retry(path, payload)
    except OSError as exc:
        LOG.warning("Could not persist bridge recovery state %s: %s", path, exc)
        return False
    return True


def _preserve_recovery(
    paths: ControlPaths,
    filename: str,
    data: bytes,
    *,
    previous: str = "",
) -> str:
    digest = _hash_bytes(data)
    if previous:
        previous_path = Path(previous)
        try:
            if previous_path.is_file() and _hash_file(previous_path) == digest:
                return str(previous_path)
        except OSError:
            pass
    suffix = Path(filename).suffix or ".usd"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    target = paths.recovery_dir / f"{Path(filename).stem}.{stamp}.{digest[:8]}.local{suffix}"
    _write_bytes_atomic(target, data)
    return str(target)


def _remove_recovery(path: str) -> None:
    if path:
        with contextlib.suppress(OSError):
            Path(path).unlink(missing_ok=True)


def _remove_control_files_from_mirror(mirror_dir: Path) -> None:
    for name in ("openusdconnect_bridge_status.json", "openusdconnect_bridge.log"):
        try:
            (mirror_dir / name).unlink(missing_ok=True)
        except OSError:
            LOG.warning("Could not remove bridge control file %s", mirror_dir / name)


def _url_filename(url: str) -> str:
    name = Path(unquote(urlparse(url).path)).name
    if not name:
        raise ValueError("VFS URL must identify a file")
    return name


def _foreground_command(config: BridgeConfig) -> list[str]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--vfs-url",
        config.vfs_url,
        "--mirror-dir",
        str(config.mirror_dir),
        "--poll-interval",
        str(config.poll_interval),
        "--settle-time",
        str(config.settle_time),
        "--status-file",
        str(config.status_file),
        "--owner-id",
        config.owner_id,
    ]
    if config.log_file is not None:
        command.extend(["--log-file", str(config.log_file)])
    if config.once:
        command.append("--once")
    if config.open:
        command.append("--open")
    if config.verbose:
        command.append("--verbose")
    if isinstance(config.exposure, WindowsDriveExposureConfig):
        command.extend(["--drive", config.exposure.drive])
        if config.exposure.force:
            command.append("--force")
        if config.exposure.release_on_exit:
            command.append("--release-on-exit")
    elif _is_windows():
        command.append("--no-drive")
    return command


def _spawn_background(config: BridgeConfig) -> int:
    log_file = config.log_file or _default_log_file(config.mirror_dir)
    exposure = _describe_exposure(
        config.mirror_dir,
        _url_filename(config.vfs_url),
        config.exposure,
    )
    command = _foreground_command(config)

    creationflags = 0
    if _is_windows():
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
    proc = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        start_new_session=not _is_windows(),
        cwd=str(Path.cwd()),
    )
    deadline = time.monotonic() + 10.0
    last_error = ""
    while time.monotonic() < deadline:
        try:
            status = _read_json(config.status_file)
        except (OSError, json.JSONDecodeError) as exc:
            last_error = str(exc)
        else:
            if (
                status.get("owner_id") == config.owner_id
                and status.get("state") in {"running", "seeded", "conflict"}
            ):
                break
            last_error = str(status.get("error") or status.get("state") or "starting")
        returncode = proc.poll()
        if returncode is not None:
            raise RuntimeError(
                f"background bridge exited with status {returncode}: {last_error}"
            )
        time.sleep(0.05)
    else:
        _stop_pid(proc.pid)
        raise RuntimeError(f"timed out waiting for background bridge: {last_error}")
    print(f"Started bridge PID {proc.pid}")
    print(f"Status: {config.status_file}")
    print(f"Log: {log_file}")
    print(f"Live USD file after seed: {exposure.file_path}")
    return 0


def _maybe_open(path: str) -> None:
    if _is_windows():
        os.startfile(path)  # type: ignore[attr-defined]
    elif _is_macos():
        subprocess.Popen(["/usr/bin/open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def _build_run_parser(*, is_windows: bool | None = None) -> argparse.ArgumentParser:
    is_windows = _is_windows() if is_windows is None else is_windows
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--vfs-url",
        default=vfs_url(DEFAULT_HOST, DEFAULT_VFS_PORT, DEFAULT_VFS_SHARE, DEFAULT_VFS_NAME),
        help="HTTP URL of the virtual USD file",
    )
    parser.add_argument("--mirror-dir", default=DEFAULT_MIRROR_DIR)
    parser.add_argument(
        "--poll-interval",
        type=positive_seconds,
        default=DEFAULT_BRIDGE_POLL_INTERVAL,
        metavar="SECONDS",
        help="Seconds between local and remote change checks",
    )
    parser.add_argument(
        "--settle-time",
        type=nonnegative_seconds,
        default=DEFAULT_BRIDGE_SETTLE_TIME,
        metavar="SECONDS",
        help="Seconds a changed file must remain stable before upload",
    )
    parser.add_argument("--once", action="store_true", help="Seed and expose once, then exit")
    parser.add_argument("--background", action="store_true", help="Start a detached bridge process")
    parser.add_argument("--open", action="store_true", help="Open the local exposure")
    parser.add_argument("--status-file", default="", help="Write bridge health JSON to this path")
    parser.add_argument("--log-file", default="", help="Write bridge logs to this path")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--owner-id", default="", help=argparse.SUPPRESS)
    if is_windows:
        exposure = parser.add_mutually_exclusive_group()
        exposure.add_argument("--drive", default=None, help="Drive alias (default: O:)")
        exposure.add_argument(
            "--no-drive",
            action="store_true",
            help="Expose only the local directory",
        )
        parser.add_argument("--force", action="store_true", help="Replace an existing drive alias")
        parser.add_argument("--release-on-exit", action="store_true")
    return parser


def _parse_bridge_config(
    argv: list[str],
    *,
    is_windows: bool | None = None,
) -> BridgeConfig:
    is_windows = _is_windows() if is_windows is None else is_windows
    parser = _build_run_parser(is_windows=is_windows)
    args = parser.parse_args(argv)
    if is_windows and args.no_drive and (args.force or args.release_on_exit):
        parser.error("--force and --release-on-exit require a Windows drive exposure")
    mirror_dir = Path(args.mirror_dir).resolve()
    status_file = (
        Path(args.status_file).resolve() if args.status_file else _default_status_file(mirror_dir)
    )
    log_file = Path(args.log_file).resolve() if args.log_file else None
    if args.background and log_file is None:
        log_file = _default_log_file(mirror_dir)
    if is_windows and not args.no_drive:
        exposure: ExposureConfig = WindowsDriveExposureConfig(
            drive=_drive_name(args.drive or "O:"),
            force=args.force,
            release_on_exit=args.release_on_exit,
        )
    else:
        exposure = DirectoryExposureConfig()
    return BridgeConfig(
        vfs_url=args.vfs_url,
        mirror_dir=mirror_dir,
        poll_interval=args.poll_interval,
        settle_time=args.settle_time,
        once=args.once,
        background=args.background,
        open=args.open,
        status_file=status_file,
        log_file=log_file,
        verbose=args.verbose,
        exposure=exposure,
        owner_id=args.owner_id or uuid.uuid4().hex,
    )


def _build_stop_parser(*, is_windows: bool | None = None) -> argparse.ArgumentParser:
    is_windows = _is_windows() if is_windows is None else is_windows
    parser = argparse.ArgumentParser(
        description="Stop a local VFS bridge",
        allow_abbrev=False,
    )
    parser.add_argument("--pid", type=int, default=0, help="Bridge PID if no status is available")
    parser.add_argument("--status-file", default="")
    parser.add_argument("--stop-process", action="store_true")
    parser.add_argument("--cleanup-status", action="store_true")
    if is_windows:
        parser.add_argument("--drive", default="", help="Drive alias if no status is available")
    return parser


def _parse_stop_config(
    argv: list[str],
    *,
    is_windows: bool | None = None,
) -> StopConfig:
    is_windows = _is_windows() if is_windows is None else is_windows
    args = _build_stop_parser(is_windows=is_windows).parse_args(argv)
    drive = _drive_name(args.drive) if is_windows and args.drive else ""
    return StopConfig(
        status_file=Path(args.status_file).resolve() if args.status_file else None,
        pid=args.pid,
        stop_process=args.stop_process,
        cleanup_status=args.cleanup_status,
        drive=drive,
    )


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
        return
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


def _run_stop(config: StopConfig) -> int:
    status_path = config.status_file
    status = {}
    status_error = ""
    if status_path and status_path.exists():
        try:
            status = _read_json(status_path)
        except (OSError, json.JSONDecodeError) as exc:
            status_error = f"could not read bridge status {status_path}: {exc}"
            print(status_error, file=sys.stderr)

    errors: list[str] = []
    pid = int(status.get("pid") or config.pid)
    if config.stop_process:
        if pid:
            try:
                _stop_pid(pid)
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                errors.append(f"could not stop bridge PID {pid}: {exc}")
        else:
            errors.append("no bridge PID was available")

    drive = config.drive or str(status.get("drive") or "")
    if drive:
        try:
            if not _is_windows():
                raise RuntimeError("a Windows drive alias cannot be released on this platform")
            _unsubst(drive)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            errors.append(f"could not release {drive}: {exc}")

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    lock_file = str(status.get("lock_file") or "")
    owner_id = str(status.get("owner_id") or "")
    if lock_file:
        try:
            _release_bridge_lock(Path(lock_file), owner_id)
        except OSError as exc:
            print(f"could not remove bridge lock {lock_file}: {exc}", file=sys.stderr)
            return 1

    if status_path:
        fields = {
            key: value
            for key, value in status.items()
            if key not in {"updated_at", "state", "pid", "error"}
        }
        if not _write_status(status_path, **fields, state="stopped", pid=0, error=""):
            print(f"could not mark bridge status stopped: {status_path}", file=sys.stderr)
            return 1
        if config.cleanup_status:
            try:
                status_path.unlink(missing_ok=True)
            except OSError as exc:
                print(f"could not remove bridge status {status_path}: {exc}", file=sys.stderr)
                return 1
    print("Stopped local VFS bridge")
    if drive:
        print(f"Released {drive}")
    if status_error:
        print("Cleanup used the explicit PID/drive fallback", file=sys.stderr)
    return 0


def _run_status(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Print local VFS bridge status JSON",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--status-file",
        default=DEFAULT_BRIDGE_STATUS_FILE,
    )
    args = parser.parse_args(argv)
    path = Path(args.status_file).resolve()
    if not path.exists():
        print(f"status file not found: {path}", file=sys.stderr)
        return 1
    print(path.read_text(encoding="utf-8"))
    return 0


def _advance_save_candidate(
    candidate: SaveCandidate | None,
    observation: FileObservation,
    *,
    synced_hash: str,
    blocked_hash: str,
    now: float,
    settle: float,
) -> tuple[SaveCandidate | None, bool]:
    if observation.digest in {synced_hash, blocked_hash}:
        return None, False
    if candidate is None or candidate.observation != observation:
        candidate = SaveCandidate(observation=observation, stable_since=now)
    return candidate, now - candidate.stable_since >= settle


def _run_bridge(config: BridgeConfig) -> int:
    mirror_dir = config.mirror_dir
    mirror_dir.mkdir(parents=True, exist_ok=True)
    _remove_control_files_from_mirror(mirror_dir)
    filename = _url_filename(config.vfs_url)
    local_file = mirror_dir / filename

    if config.background:
        return _spawn_background(config)

    _setup_logging(config.log_file, verbose=config.verbose)
    paths = _control_paths(config, filename)
    _acquire_bridge_lock(
        paths.lock,
        owner_id=config.owner_id,
        url=config.vfs_url,
        mirror_dir=mirror_dir,
    )
    exposure: LocalExposure | None = None
    try:
        stored = _load_sync_state(paths.sync_state, config)
        recovery_file = str(stored.get("recovery_file") or "")
        blocked_hash = str(stored.get("blocked_hash") or "")
        conflict_reason = str(stored.get("conflict_reason") or "")
        pending_refresh = bool(stored.get("pending_refresh"))
        synced_hash = str(stored.get("synced_hash") or "")
        last_upload_at = str(stored.get("last_upload_at") or "")

        existing = _observe_file(local_file) if local_file.exists() else None
        etag, remote_data, remote_hash = _fetch(config.vfs_url)
        remote_size = len(remote_data)
        replaced_from_remote = False

        if existing is None:
            _replace_remote_if_unchanged(local_file, remote_data, expected_local_hash=None)
            synced_hash = remote_hash
            blocked_hash = ""
            conflict_reason = ""
            pending_refresh = False
            _remove_recovery(recovery_file)
            recovery_file = ""
            replaced_from_remote = True
        else:
            current = _observe_file(local_file)
            changed_during_fetch = current.digest != existing.digest
            stored_etag = str(stored.get("etag") or "")
            stored_clean = bool(stored) and existing.digest == str(
                stored.get("synced_hash") or ""
            )
            accepted_pending = pending_refresh and stored_clean
            if not changed_during_fetch and existing.digest == remote_hash:
                synced_hash = remote_hash
                blocked_hash = ""
                conflict_reason = ""
                pending_refresh = False
                _remove_recovery(recovery_file)
                recovery_file = ""
            elif not changed_during_fetch and (stored_clean or accepted_pending):
                _replace_remote_if_unchanged(
                    local_file,
                    remote_data,
                    expected_local_hash=existing.digest,
                )
                synced_hash = remote_hash
                blocked_hash = ""
                conflict_reason = ""
                pending_refresh = False
                _remove_recovery(recovery_file)
                recovery_file = ""
                replaced_from_remote = True
            else:
                current = _observe_file(local_file)
                local_data = _read_file_version(local_file, current.digest)
                recovery_file = _preserve_recovery(
                    paths,
                    filename,
                    local_data,
                    previous=recovery_file,
                )
                can_retry = (
                    bool(stored)
                    and bool(synced_hash)
                    and stored_etag == etag
                    and blocked_hash != current.digest
                )
                if not can_retry:
                    blocked_hash = current.digest
                    conflict_reason = (
                        "local file changed while the remote snapshot was loading"
                        if changed_during_fetch
                        else "local recovery differs from the current remote snapshot"
                    )
                pending_refresh = False

        observation = _observe_file(local_file)
        if not synced_hash:
            synced_hash = remote_hash
        exposure = _prepare_exposure(mirror_dir, filename, config=config.exposure)
        base_status = {
            "pid": os.getpid(),
            "owner_id": config.owner_id,
            "url": config.vfs_url,
            "lock_file": str(paths.lock),
            "sync_state_file": str(paths.sync_state),
            **_exposure_fields(exposure),
        }
        health = {
            "state": "conflict" if blocked_hash else "running",
            "etag": etag,
            "size": observation.size,
            "last_download_at": _now() if replaced_from_remote or existing is None else "",
            "last_upload_at": last_upload_at,
            "pending_refresh": pending_refresh,
            "recovery_file": recovery_file,
            "conflict_reason": conflict_reason,
            "error": "",
        }

        def persist() -> None:
            _write_sync_state(
                paths.sync_state,
                config,
                etag=etag,
                synced_hash=synced_hash,
                local_hash=observation.digest,
                pending_refresh=pending_refresh,
                blocked_hash=blocked_hash,
                recovery_file=recovery_file,
                conflict_reason=conflict_reason,
                last_upload_at=last_upload_at,
            )

        def publish(**updates) -> None:
            health.update(updates)
            _write_status(config.status_file, **{**base_status, **health})

        persist()
        publish()
        if exposure.drive:
            LOG.info("Exposed %s from local mirror %s", exposure.root_path, mirror_dir)
        else:
            LOG.info("Local mirror ready at %s", mirror_dir)
        LOG.info("Loaded %s (%d bytes, ETag=%s)", local_file, remote_size, etag or "none")
        if recovery_file:
            LOG.warning("Preserved unsynchronized local work at %s", recovery_file)
        LOG.info("Live USD file: %s", exposure.file_path)
        print(f"Local mirror: {mirror_dir}")
        if exposure.drive:
            print(f"Windows drive: {exposure.root_path}")
        print(f"Live USD file: {exposure.file_path}")
        if recovery_file:
            print(f"Recovery copy: {recovery_file}")
        if config.open:
            _maybe_open(exposure.root_path)

        release_on_exit = (
            isinstance(config.exposure, WindowsDriveExposureConfig)
            and config.exposure.release_on_exit
        )
        if config.once:
            publish(state="conflict" if blocked_hash else "seeded", pid=0)
            if release_on_exit:
                _release_exposure(exposure)
            return 0

        candidate: SaveCandidate | None = None
        try:
            while True:
                time.sleep(config.poll_interval)
                try:
                    observation = _observe_if_changed(local_file, observation)

                    if pending_refresh:
                        status, headers, _data = _request("HEAD", config.vfs_url)
                        if not (200 <= status < 300):
                            raise HttpRequestError("HEAD", config.vfs_url, status)
                        acknowledged_etag = headers.get("etag", "")
                        if acknowledged_etag:
                            etag = acknowledged_etag
                        current = _observe_if_changed(local_file, observation)
                        if current.digest == synced_hash:
                            fetched_etag, data, fetched_hash = _fetch(config.vfs_url)
                            _replace_remote_if_unchanged(
                                local_file,
                                data,
                                expected_local_hash=current.digest,
                            )
                            etag = fetched_etag or etag
                            synced_hash = fetched_hash
                            observation = _observe_file(local_file)
                            _remove_recovery(recovery_file)
                            recovery_file = ""
                            health["last_download_at"] = _now()
                            LOG.info(
                                "Reconciled acknowledged upload; %d bytes, ETag=%s",
                                len(data),
                                etag or "none",
                            )
                        else:
                            observation = current
                        pending_refresh = False
                        persist()
                        publish(
                            state="running",
                            etag=etag,
                            size=observation.size,
                            pending_refresh=False,
                            recovery_file=recovery_file,
                            error="",
                        )

                    observation = _observe_if_changed(local_file, observation)
                    if blocked_hash and observation.digest != blocked_hash:
                        blocked_hash = ""
                        conflict_reason = ""
                        candidate = None

                    candidate, ready = _advance_save_candidate(
                        candidate,
                        observation,
                        synced_hash=synced_hash,
                        blocked_hash=blocked_hash,
                        now=time.monotonic(),
                        settle=config.settle_time,
                    )
                    if candidate is not None:
                        if not ready:
                            continue
                        data = _read_file_version(local_file, candidate.observation.digest)
                        recovery_file = _preserve_recovery(
                            paths,
                            filename,
                            data,
                            previous=recovery_file,
                        )
                        observation = candidate.observation
                        persist()
                        try:
                            response_etag = _upload_bytes(config.vfs_url, data, etag)
                        except HttpRequestError as exc:
                            if exc.status in {409, 412}:
                                blocked_hash = observation.digest
                                conflict_reason = str(exc)
                                candidate = None
                                persist()
                                publish(
                                    state="conflict",
                                    recovery_file=recovery_file,
                                    conflict_reason=conflict_reason,
                                    error=str(exc),
                                )
                            else:
                                candidate = SaveCandidate(observation, time.monotonic())
                                persist()
                                publish(
                                    state="degraded",
                                    recovery_file=recovery_file,
                                    error=str(exc),
                                )
                            LOG.warning("Bridge warning: %s", exc)
                            continue

                        synced_hash = observation.digest
                        etag = response_etag or etag
                        pending_refresh = True
                        blocked_hash = ""
                        conflict_reason = ""
                        candidate = None
                        last_upload_at = _now()
                        persist()
                        publish(
                            state="running",
                            etag=etag,
                            size=observation.size,
                            last_upload_at=last_upload_at,
                            pending_refresh=True,
                            recovery_file=recovery_file,
                            conflict_reason="",
                            error="",
                        )
                        LOG.info("Server acknowledged local save; refresh pending")
                        continue

                    baseline_hash = observation.digest
                    status, headers, _data = _request("HEAD", config.vfs_url)
                    if not (200 <= status < 300):
                        raise HttpRequestError("HEAD", config.vfs_url, status)
                    remote_etag = headers.get("etag", "")

                    if blocked_hash:
                        if remote_etag and remote_etag != etag:
                            etag = remote_etag
                            persist()
                            publish(etag=etag)
                        if health.get("state") == "degraded":
                            publish(state="conflict", error="")
                        continue

                    if remote_etag and remote_etag != etag:
                        fetched_etag, data, fetched_hash = _fetch(config.vfs_url)
                        try:
                            _replace_remote_if_unchanged(
                                local_file,
                                data,
                                expected_local_hash=baseline_hash,
                            )
                        except LocalFileChangedError as exc:
                            observation = _observe_file(local_file)
                            local_data = _read_file_version(local_file, observation.digest)
                            recovery_file = _preserve_recovery(
                                paths,
                                filename,
                                local_data,
                                previous=recovery_file,
                            )
                            etag = fetched_etag or remote_etag
                            blocked_hash = observation.digest
                            conflict_reason = str(exc)
                            persist()
                            publish(
                                state="conflict",
                                etag=etag,
                                recovery_file=recovery_file,
                                conflict_reason=conflict_reason,
                                error=str(exc),
                            )
                            continue
                        etag = fetched_etag or remote_etag
                        synced_hash = fetched_hash
                        observation = _observe_file(local_file)
                        persist()
                        publish(
                            state="running",
                            etag=etag,
                            size=observation.size,
                            last_download_at=_now(),
                            error="",
                        )
                        LOG.info(
                            "Downloaded remote update; %d bytes, ETag=%s",
                            len(data),
                            etag,
                        )
                    elif health.get("state") == "degraded":
                        publish(state="running", error="")
                except (
                    OSError,
                    RuntimeError,
                    http.client.HTTPException,
                ) as exc:
                    publish(state="degraded", error=str(exc))
                    LOG.warning("Bridge warning: %s", exc)
        except KeyboardInterrupt:
            publish(state="stopped", error="")
            return 0
        finally:
            if release_on_exit:
                try:
                    _release_exposure(exposure)
                except (OSError, RuntimeError):
                    LOG.exception("Failed to release %s", exposure.root_path)
    finally:
        _release_bridge_lock(paths.lock, config.owner_id)


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv and argv[0] == "stop":
        try:
            return _run_stop(_parse_stop_config(argv[1:]))
        except (OSError, RuntimeError, ValueError) as exc:
            print(exc, file=sys.stderr)
            return 1
    if argv and argv[0] == "status":
        return _run_status(argv[1:])

    try:
        return _run_bridge(_parse_bridge_config(argv))
    except (OSError, RuntimeError, ValueError, http.client.HTTPException) as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
