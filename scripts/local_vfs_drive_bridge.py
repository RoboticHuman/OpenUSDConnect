"""No-admin local drive bridge for OpenUSDConnect VFS.

This is a fallback for machines where Windows WebClient/WebDAV drive mapping
cannot be started from the current session. It creates a normal local folder,
maps it with ``subst`` (no admin), keeps ``scene.usd`` refreshed from the VFS,
and PUTs local file saves back to the VFS endpoint.
"""

from __future__ import annotations

import argparse
import http.client
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse


def _request(method: str, url: str, body: bytes | None = None) -> tuple[int, dict, bytes]:
    parsed = urlparse(url)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=10)
    path = parsed.path or "/"
    try:
        headers = {}
        if body is not None:
            headers["Content-Length"] = str(len(body))
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, dict(resp.getheaders()), data
    finally:
        conn.close()


def _download(url: str, path: Path) -> tuple[str, int]:
    status, headers, data = _request("GET", url)
    if not (200 <= status < 300):
        raise RuntimeError(f"GET {url} failed with HTTP {status}")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return headers.get("ETag", ""), len(data)


def _upload(url: str, path: Path) -> None:
    data = path.read_bytes()
    status, _headers, _body = _request("PUT", url, body=data)
    if not (200 <= status < 300):
        raise RuntimeError(f"PUT {url} failed with HTTP {status}")


def _subst(drive: str, target: Path, force: bool) -> None:
    drive = drive.rstrip("\\/")
    if not drive.endswith(":"):
        drive += ":"
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:7280/usd/scene.usd")
    parser.add_argument("--mount-dir", default=".ouc_live_mount/usd")
    parser.add_argument("--drive", default="O:")
    parser.add_argument("--poll", type=float, default=1.0)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--once", action="store_true", help="Seed/map once, then exit")
    args = parser.parse_args(argv)

    mount_dir = Path(args.mount_dir).resolve()
    mount_dir.mkdir(parents=True, exist_ok=True)
    file_path = mount_dir / Path(urlparse(args.url).path).name

    etag, size = _download(args.url, file_path)
    last_seen_mtime = file_path.stat().st_mtime
    _subst(args.drive, mount_dir, args.force)
    print(f"Mapped {args.drive.rstrip(':/')}:{os.sep} to {mount_dir}")
    print(f"Seeded {file_path} ({size} bytes, ETag={etag or 'none'})")
    print(f"Open this file: {args.drive.rstrip(':/')}:\\{file_path.name}")
    sys.stdout.flush()

    if args.once:
        return 0

    while True:
        time.sleep(args.poll)
        try:
            current_mtime = file_path.stat().st_mtime
            if current_mtime > last_seen_mtime + 0.001:
                _upload(args.url, file_path)
                etag, size = _download(args.url, file_path)
                last_seen_mtime = file_path.stat().st_mtime
                print(f"Uploaded local save; refreshed {size} bytes, ETag={etag or 'none'}")
                sys.stdout.flush()
                continue

            status, headers, _data = _request("HEAD", args.url)
            remote_etag = headers.get("ETag", "") if 200 <= status < 300 else ""
            if remote_etag and remote_etag != etag:
                etag, size = _download(args.url, file_path)
                last_seen_mtime = file_path.stat().st_mtime
                print(f"Downloaded remote update; {size} bytes, ETag={etag}")
                sys.stdout.flush()
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            print(f"bridge warning: {exc}", file=sys.stderr)
            sys.stderr.flush()


if __name__ == "__main__":
    raise SystemExit(main())
