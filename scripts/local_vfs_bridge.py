"""Mirror one OpenUSDConnect VFS file for reliable local open and save.

The bridge downloads a managed virtual file into a normal local directory,
keeps it current, and uploads completed local saves with an ETag guard. Windows
may additionally expose the directory through a ``subst`` drive alias.
"""

from __future__ import annotations

import argparse
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
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

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
    url: str
    mirror_dir: Path
    poll: float
    once: bool
    background: bool
    open: bool
    status_file: Path
    log_file: Path | None
    verbose: bool
    exposure: ExposureConfig


@dataclass(frozen=True)
class StopConfig:
    status_file: Path | None
    pid: int
    stop_process: bool
    cleanup_status: bool
    drive: str


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
) -> tuple[int, dict, bytes]:
    parsed = urlparse(url)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=10)
    path = parsed.path or "/"
    try:
        request_headers = dict(headers or {})
        if body is not None:
            request_headers["Content-Length"] = str(len(body))
        conn.request(method, path, body=body, headers=request_headers)
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, dict(resp.getheaders()), data
    finally:
        conn.close()


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _content_changed(path: Path, previous_hash: str) -> tuple[bool, str]:
    current_hash = _hash_file(path)
    return current_hash != previous_hash, current_hash


def _download(url: str, path: Path) -> tuple[str, int, str]:
    status, headers, data = _request("GET", url)
    if not (200 <= status < 300):
        raise RuntimeError(f"GET {url} failed with HTTP {status}")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return headers.get("ETag", ""), len(data), _hash_bytes(data)


def _upload(url: str, path: Path, etag: str = "") -> None:
    data = path.read_bytes()
    headers = {"If-Match": etag} if etag else None
    status, _headers, _body = _request("PUT", url, body=data, headers=headers)
    if not (200 <= status < 300):
        raise RuntimeError(f"PUT {url} failed with HTTP {status}")


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


def _write_status(path: Path | None, **fields) -> None:
    if path is None:
        return
    payload = {"updated_at": _now(), **fields}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


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


def _remove_control_files_from_mirror(mirror_dir: Path) -> None:
    for name in ("openusdconnect_bridge_status.json", "openusdconnect_bridge.log"):
        try:
            (mirror_dir / name).unlink(missing_ok=True)
        except OSError:
            LOG.warning("Could not remove bridge control file %s", mirror_dir / name)


def _url_filename(url: str) -> str:
    name = Path(urlparse(url).path).name
    if not name:
        raise ValueError("VFS URL must identify a file")
    return name


def _foreground_command(config: BridgeConfig) -> list[str]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--url",
        config.url,
        "--mirror-dir",
        str(config.mirror_dir),
        "--poll",
        str(config.poll),
        "--status-file",
        str(config.status_file),
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
        _url_filename(config.url),
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
    _write_status(
        config.status_file,
        state="starting",
        pid=proc.pid,
        url=config.url,
        **_exposure_fields(exposure),
        log_file=str(log_file),
        error="",
    )
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:7280/usd/scene.usd")
    parser.add_argument("--mirror-dir", default=".ouc_live_mount/usd")
    parser.add_argument("--poll", type=float, default=1.0)
    parser.add_argument("--once", action="store_true", help="Seed and expose once, then exit")
    parser.add_argument("--background", action="store_true", help="Start a detached bridge process")
    parser.add_argument("--open", action="store_true", help="Open the local exposure")
    parser.add_argument("--status-file", default="", help="Write bridge health JSON to this path")
    parser.add_argument("--log-file", default="", help="Write bridge logs to this path")
    parser.add_argument("--verbose", action="store_true")
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
    if args.poll <= 0:
        parser.error("--poll must be greater than zero")
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
        url=args.url,
        mirror_dir=mirror_dir,
        poll=args.poll,
        once=args.once,
        background=args.background,
        open=args.open,
        status_file=status_file,
        log_file=log_file,
        verbose=args.verbose,
        exposure=exposure,
    )


def _build_stop_parser(*, is_windows: bool | None = None) -> argparse.ArgumentParser:
    is_windows = _is_windows() if is_windows is None else is_windows
    parser = argparse.ArgumentParser(description="Stop a local VFS bridge")
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
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def _run_stop(config: StopConfig) -> int:
    status_path = config.status_file
    status = {}
    if status_path and status_path.exists():
        status = json.loads(status_path.read_text(encoding="utf-8"))
    if config.stop_process:
        _stop_pid(int(status.get("pid") or config.pid))

    drive = config.drive or str(status.get("drive") or "")
    if drive:
        if not _is_windows():
            raise RuntimeError("a Windows drive alias cannot be released on this platform")
        _unsubst(drive)

    if status_path:
        fields = {
            key: value
            for key, value in status.items()
            if key not in {"updated_at", "state", "pid", "error"}
        }
        _write_status(status_path, **fields, state="stopped", pid=0, error="")
        if config.cleanup_status:
            status_path.unlink(missing_ok=True)
    print("Stopped local VFS bridge")
    if drive:
        print(f"Released {drive}")
    return 0


def _run_status(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Print local VFS bridge status JSON")
    parser.add_argument(
        "--status-file",
        default=".ouc_live_mount/bridge/openusdconnect_bridge_status.json",
    )
    args = parser.parse_args(argv)
    path = Path(args.status_file).resolve()
    if not path.exists():
        print(f"status file not found: {path}", file=sys.stderr)
        return 1
    print(path.read_text(encoding="utf-8"))
    return 0


def _run_bridge(config: BridgeConfig) -> int:
    mirror_dir = config.mirror_dir
    mirror_dir.mkdir(parents=True, exist_ok=True)
    _remove_control_files_from_mirror(mirror_dir)
    filename = _url_filename(config.url)
    local_file = mirror_dir / filename

    if config.background:
        return _spawn_background(config)

    _setup_logging(config.log_file, verbose=config.verbose)

    etag, size, last_seen_hash = _download(config.url, local_file)
    last_seen_mtime = local_file.stat().st_mtime_ns
    exposure = _prepare_exposure(
        mirror_dir,
        filename,
        config=config.exposure,
    )
    base_status = {
        "pid": os.getpid(),
        "url": config.url,
        **_exposure_fields(exposure),
    }
    health = {
        "state": "running",
        "etag": etag,
        "size": size,
        "last_download_at": _now(),
        "last_upload_at": "",
        "last_head_at": "",
        "error": "",
    }

    def publish(**updates) -> None:
        health.update(updates)
        _write_status(config.status_file, **base_status, **health)

    publish()
    if exposure.drive:
        LOG.info("Exposed %s from local mirror %s", exposure.root_path, mirror_dir)
    else:
        LOG.info("Local mirror ready at %s", mirror_dir)
    LOG.info("Seeded %s (%d bytes, ETag=%s)", local_file, size, etag or "none")
    LOG.info("Live USD file: %s", exposure.file_path)
    print(f"Local mirror: {mirror_dir}")
    if exposure.drive:
        print(f"Windows drive: {exposure.root_path}")
    print(f"Seeded {local_file} ({size} bytes, ETag={etag or 'none'})")
    print(f"Live USD file: {exposure.file_path}")
    if config.open:
        _maybe_open(exposure.root_path)

    release_on_exit = (
        isinstance(config.exposure, WindowsDriveExposureConfig) and config.exposure.release_on_exit
    )
    if config.once:
        publish(state="seeded")
        if release_on_exit:
            _release_exposure(exposure)
        return 0

    try:
        while True:
            time.sleep(config.poll)
            try:
                current_mtime = local_file.stat().st_mtime_ns
                if current_mtime != last_seen_mtime:
                    changed, current_hash = _content_changed(local_file, last_seen_hash)
                    if not changed:
                        last_seen_mtime = current_mtime
                        continue
                    _upload(config.url, local_file, etag)
                    etag, size, last_seen_hash = _download(config.url, local_file)
                    last_seen_mtime = local_file.stat().st_mtime_ns
                    publish(
                        state="running",
                        etag=etag,
                        size=size,
                        last_download_at=_now(),
                        last_upload_at=_now(),
                        error="",
                    )
                    LOG.info(
                        "Uploaded local save; refreshed %d bytes, ETag=%s",
                        size,
                        etag or "none",
                    )
                    continue

                status, headers, _data = _request("HEAD", config.url)
                if not (200 <= status < 300):
                    raise RuntimeError(f"HEAD {config.url} failed with HTTP {status}")
                remote_etag = headers.get("ETag", "")
                publish(state="running", last_head_at=_now(), error="")
                if remote_etag and remote_etag != etag:
                    etag, size, last_seen_hash = _download(config.url, local_file)
                    last_seen_mtime = local_file.stat().st_mtime_ns
                    publish(
                        etag=etag,
                        size=size,
                        last_download_at=_now(),
                    )
                    LOG.info("Downloaded remote update; %d bytes, ETag=%s", size, etag)
            except (OSError, RuntimeError, http.client.HTTPException) as exc:
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
