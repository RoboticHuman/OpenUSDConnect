"""No-admin local drive bridge for OpenUSDConnect VFS.

This is a fallback for machines where Windows WebClient/WebDAV drive mapping
cannot be started from the current session. It creates a normal local folder,
maps it with ``subst`` (no admin), keeps ``scene.usd`` refreshed from the VFS,
and PUTs local file saves back to the VFS endpoint.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import logging
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

LOG = logging.getLogger("openusdconnect.vfs_bridge")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _drive_name(drive: str) -> str:
    drive = drive.rstrip("\\/")
    if not drive.endswith(":"):
        drive += ":"
    return drive.upper()


def _is_windows() -> bool:
    return os.name == "nt"


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


def _default_status_file(mount_dir: Path) -> Path:
    return mount_dir.parent / "bridge" / "openusdconnect_bridge_status.json"


def _default_log_file(mount_dir: Path) -> Path:
    return mount_dir.parent / "bridge" / "openusdconnect_bridge.log"


def _remove_legacy_control_files(mount_dir: Path) -> None:
    for name in ("openusdconnect_bridge_status.json", "openusdconnect_bridge.log"):
        try:
            (mount_dir / name).unlink(missing_ok=True)
        except OSError:
            LOG.warning("Could not remove legacy bridge control file %s", mount_dir / name)


def _spawn_background(args: argparse.Namespace, argv: list[str]) -> int:
    mount_dir = Path(args.mount_dir).resolve()
    status_file = Path(args.status_file).resolve() if args.status_file else _default_status_file(mount_dir)
    log_file = Path(args.log_file).resolve() if args.log_file else _default_log_file(mount_dir)
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        *[arg for arg in argv if arg != "--background"],
    ]
    if "--status-file" not in argv:
        cmd.extend(["--status-file", str(status_file)])
    if "--log-file" not in argv:
        cmd.extend(["--log-file", str(log_file)])

    creationflags = 0
    if _is_windows():
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        cwd=str(Path.cwd()),
    )
    _write_status(
        status_file,
        state="starting",
        pid=proc.pid,
        url=args.url,
        drive=_drive_name(args.drive),
        mount_dir=str(mount_dir),
        file_path=str(mount_dir / Path(urlparse(args.url).path).name),
        log_file=str(log_file),
        error="",
    )
    print(f"Started bridge PID {proc.pid}")
    print(f"Status: {status_file}")
    print(f"Log: {log_file}")
    print(f"Open this file after seed: {_drive_name(args.drive)}\\{Path(urlparse(args.url).path).name}")
    return 0


def _maybe_open(path: str) -> None:
    if _is_windows():
        os.startfile(path)  # type: ignore[attr-defined]
        return
    subprocess.Popen(["xdg-open", path])


def _build_run_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:7280/usd/scene.usd")
    parser.add_argument("--mount-dir", default=".ouc_live_mount/usd")
    parser.add_argument("--drive", default="O:")
    parser.add_argument("--poll", type=float, default=1.0)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--once", action="store_true", help="Seed/map once, then exit")
    parser.add_argument("--background", action="store_true", help="Start a detached bridge process")
    parser.add_argument("--open", action="store_true", help="Open the mapped drive in Explorer")
    parser.add_argument("--status-file", default="", help="Write bridge health JSON to this path")
    parser.add_argument("--log-file", default="", help="Write bridge logs to this path")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--unmount-on-exit", action="store_true")
    return parser


def _parse_unmount(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Unmount and optionally stop a local VFS bridge")
    parser.add_argument("--drive", default="O:")
    parser.add_argument("--status-file", default="")
    parser.add_argument("--stop-process", action="store_true")
    parser.add_argument("--cleanup-status", action="store_true")
    return parser.parse_args(argv)


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
        os.kill(pid, 15)


def _run_unmount(argv: list[str]) -> int:
    args = _parse_unmount(argv)
    status_path = Path(args.status_file).resolve() if args.status_file else None
    if args.stop_process and status_path and status_path.exists():
        try:
            data = json.loads(status_path.read_text(encoding="utf-8"))
            _stop_pid(int(data.get("pid") or 0))
        except Exception as exc:
            print(f"warning: failed to stop bridge process: {exc}", file=sys.stderr)
    _unsubst(args.drive)
    if status_path:
        _write_status(status_path, state="unmounted", pid=os.getpid(), drive=_drive_name(args.drive))
        if args.cleanup_status:
            status_path.unlink(missing_ok=True)
    print(f"Unmapped {_drive_name(args.drive)}")
    return 0


def _run_status(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Print local VFS bridge status JSON")
    parser.add_argument("--status-file", default=".ouc_live_mount/bridge/openusdconnect_bridge_status.json")
    args = parser.parse_args(argv)
    path = Path(args.status_file).resolve()
    if not path.exists():
        print(f"status file not found: {path}", file=sys.stderr)
        return 1
    print(path.read_text(encoding="utf-8"))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv and argv[0] == "unmount":
        return _run_unmount(argv[1:])
    if argv and argv[0] == "status":
        return _run_status(argv[1:])

    parser = _build_run_parser()
    args = parser.parse_args(argv)

    mount_dir = Path(args.mount_dir).resolve()
    mount_dir.mkdir(parents=True, exist_ok=True)
    _remove_legacy_control_files(mount_dir)
    file_path = mount_dir / Path(urlparse(args.url).path).name
    status_file = Path(args.status_file).resolve() if args.status_file else _default_status_file(mount_dir)
    log_file = Path(args.log_file).resolve() if args.log_file else None

    if args.background:
        return _spawn_background(args, argv)

    _setup_logging(log_file, verbose=args.verbose)

    etag, size, last_seen_hash = _download(args.url, file_path)
    last_seen_mtime = file_path.stat().st_mtime
    _subst(args.drive, mount_dir, args.force)
    drive = _drive_name(args.drive)
    mapped_file = f"{drive}\\{file_path.name}"
    _write_status(
        status_file,
        state="running",
        pid=os.getpid(),
        url=args.url,
        drive=drive,
        mount_dir=str(mount_dir),
        file_path=mapped_file,
        etag=etag,
        size=size,
        last_download_at=_now(),
        last_upload_at="",
        last_head_at="",
        error="",
    )
    LOG.info("Mapped %s to %s", drive + "\\", mount_dir)
    LOG.info("Seeded %s (%d bytes, ETag=%s)", file_path, size, etag or "none")
    LOG.info("Open this file: %s", mapped_file)
    print(f"Mapped {drive}\\ to {mount_dir}")
    print(f"Seeded {file_path} ({size} bytes, ETag={etag or 'none'})")
    print(f"Open this file: {mapped_file}")
    if args.open:
        _maybe_open(drive + "\\")

    if args.once:
        _write_status(
            status_file,
            state="seeded",
            pid=os.getpid(),
            url=args.url,
            drive=drive,
            mount_dir=str(mount_dir),
            file_path=mapped_file,
            etag=etag,
            size=size,
            last_download_at=_now(),
            last_upload_at="",
            last_head_at="",
            error="",
        )
        return 0

    try:
        while True:
            time.sleep(args.poll)
            try:
                current_mtime = file_path.stat().st_mtime
                if current_mtime > last_seen_mtime + 0.001:
                    changed, current_hash = _content_changed(file_path, last_seen_hash)
                    if not changed:
                        last_seen_mtime = current_mtime
                        continue
                    _upload(args.url, file_path, etag)
                    etag, size, last_seen_hash = _download(args.url, file_path)
                    last_seen_mtime = file_path.stat().st_mtime
                    _write_status(
                        status_file,
                        state="running",
                        pid=os.getpid(),
                        url=args.url,
                        drive=drive,
                        mount_dir=str(mount_dir),
                        file_path=mapped_file,
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

                status, headers, _data = _request("HEAD", args.url)
                remote_etag = headers.get("ETag", "") if 200 <= status < 300 else ""
                _write_status(
                    status_file,
                    state="running",
                    pid=os.getpid(),
                    url=args.url,
                    drive=drive,
                    mount_dir=str(mount_dir),
                    file_path=mapped_file,
                    etag=etag,
                    size=size,
                    last_head_at=_now(),
                    error="",
                )
                if remote_etag and remote_etag != etag:
                    etag, size, last_seen_hash = _download(args.url, file_path)
                    last_seen_mtime = file_path.stat().st_mtime
                    _write_status(
                        status_file,
                        state="running",
                        pid=os.getpid(),
                        url=args.url,
                        drive=drive,
                        mount_dir=str(mount_dir),
                        file_path=mapped_file,
                        etag=etag,
                        size=size,
                        last_download_at=_now(),
                        error="",
                    )
                    LOG.info("Downloaded remote update; %d bytes, ETag=%s", size, etag)
            except Exception as exc:
                _write_status(
                    status_file,
                    state="degraded",
                    pid=os.getpid(),
                    url=args.url,
                    drive=drive,
                    mount_dir=str(mount_dir),
                    file_path=mapped_file,
                    etag=etag,
                    size=size,
                    error=str(exc),
                )
                LOG.warning("bridge warning: %s", exc)
    except KeyboardInterrupt:
        _write_status(status_file, state="stopped", pid=os.getpid(), drive=drive, error="")
        return 0
    finally:
        if args.unmount_on_exit:
            try:
                _unsubst(drive)
            except Exception:
                LOG.exception("failed to unmount %s", drive)


if __name__ == "__main__":
    raise SystemExit(main())
