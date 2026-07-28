"""Windows helper for mounting an OpenUSDConnect WebDAV share as a drive."""

from __future__ import annotations

import argparse
import http.client
import os
import string
import subprocess
import sys


def default_unc(host: str, port: int, share: str) -> str:
    share = share.strip("/").strip("\\")
    return f"\\\\{host}@{port}\\{share}"


def default_davwwwroot_unc(host: str, port: int, share: str) -> str:
    share = share.strip("/").strip("\\")
    return f"\\\\{host}@{port}\\DavWWWRoot\\{share}"


def default_url(host: str, port: int, share: str) -> str:
    share = share.strip("/").strip("\\")
    return f"http://{host}:{port}/{share}"


def candidate_targets(host: str, port: int, share: str, form: str = "auto") -> list[str]:
    if form == "url":
        return [default_url(host, port, share)]
    if form == "davwwwroot":
        return [default_davwwwroot_unc(host, port, share)]
    if form == "unc":
        return [default_unc(host, port, share)]
    return [
        default_url(host, port, share),
        default_davwwwroot_unc(host, port, share),
        default_unc(host, port, share),
    ]


def normalize_drive(value: str) -> str:
    drive = value.strip().upper().rstrip("\\/")
    if len(drive) == 1 and drive in string.ascii_uppercase:
        drive += ":"
    if len(drive) != 2 or drive[0] not in string.ascii_uppercase or drive[1] != ":":
        raise ValueError("drive must look like O: or O")
    return drive


def find_free_drive() -> str:
    for letter in reversed(string.ascii_uppercase):
        drive = f"{letter}:"
        if drive in ("A:", "B:", "C:"):
            continue
        if not os.path.exists(drive + "\\"):
            return drive
    raise RuntimeError("no free drive letter found")


def webclient_status() -> str:
    if os.name != "nt":
        return "not-windows"
    result = subprocess.run(
        ["sc.exe", "query", "WebClient"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return "unknown"
    for line in result.stdout.splitlines():
        if "STATE" in line:
            return line.strip()
    return "unknown"


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=30)


def _print_process_failure(result: subprocess.CompletedProcess) -> None:
    if result.stdout.strip():
        print(result.stdout.strip(), file=sys.stderr)
    if result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr)


def check_http_endpoint(host: str, port: int, share: str, name: str) -> tuple[bool, str]:
    share = share.strip("/").strip("\\")
    path = f"/{share}/{name.strip('/')}"
    conn = None
    try:
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        resp.read(1024)
    except OSError as exc:
        return False, f"HTTP preflight failed: {exc}"
    finally:
        try:
            if conn is not None:
                conn.close()
        except OSError:
            pass
    if 200 <= resp.status < 300:
        return True, f"HTTP preflight OK: http://{host}:{port}{path}"
    return False, f"HTTP preflight got {resp.status} for http://{host}:{port}{path}"


def mount_share(
    *,
    host: str,
    port: int,
    share: str,
    drive: str | None,
    persistent: bool,
    force: bool,
    target_form: str = "auto",
) -> tuple[str, str]:
    drive = normalize_drive(drive) if drive else find_free_drive()
    targets = candidate_targets(host, port, share, target_form)
    if force:
        _run(["net", "use", drive, "/delete", "/y"])
    failures = []
    for target in targets:
        print(f"Trying: net use {drive} {target}")
        result = _run(
            [
                "net",
                "use",
                drive,
                target,
                f"/persistent:{'yes' if persistent else 'no'}",
            ],
        )
        if result.returncode == 0:
            return drive, target
        failures.append((target, result))

    for target, result in failures:
        print(f"\nFailed target: {target}", file=sys.stderr)
        _print_process_failure(result)
    tried = ", ".join(t for t, _r in failures)
    raise RuntimeError(f"failed to map {drive}; tried {tried}")


def unmount_share(*, drive: str) -> str:
    drive = normalize_drive(drive)
    result = _run(["net", "use", drive, "/delete", "/y"])
    if result.returncode != 0:
        _print_process_failure(result)
        raise RuntimeError(f"failed to unmap {drive}")
    return drive


def maybe_start_webclient() -> None:
    result = _run(["sc.exe", "start", "WebClient"])
    output = (result.stdout + "\n" + result.stderr).lower()
    if result.returncode not in (0, 1056) and "already been started" not in output:
        _print_process_failure(result)
        raise RuntimeError(
            "failed to start WebClient service; run an elevated PowerShell or "
            "start the 'WebClient' service from Services"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=["mount", "unmount"], default="mount")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7280)
    parser.add_argument("--share", default="usd")
    parser.add_argument("--name", default="scene.usd")
    parser.add_argument("--drive", default=None, help="Drive letter, for example O:")
    parser.add_argument("--persistent", action="store_true", help="Persist mapping across logon")
    parser.add_argument("--force", action="store_true", help="Unmap the drive first if needed")
    parser.add_argument("--open", action="store_true", help="Open Explorer at the mapped share")
    parser.add_argument(
        "--start-webclient",
        action="store_true",
        help="Try to start WebClient first (default unless --no-start-webclient is set)",
    )
    parser.add_argument(
        "--no-start-webclient",
        action="store_true",
        help="Do not try to start WebClient automatically",
    )
    parser.add_argument(
        "--target-form",
        choices=["auto", "url", "davwwwroot", "unc"],
        default="auto",
        help="Mapping target form to use; auto tries URL, DavWWWRoot UNC, then short UNC",
    )
    parser.add_argument(
        "--skip-http-check",
        action="store_true",
        help="Skip checking that the VFS HTTP endpoint is reachable before mapping",
    )
    parser.add_argument("--print-only", action="store_true", help="Print paths and commands only")
    args = parser.parse_args(argv)

    drive = normalize_drive(args.drive) if args.drive else None
    unc = default_unc(args.host, args.port, args.share)
    dav_unc = default_davwwwroot_unc(args.host, args.port, args.share)
    url = default_url(args.host, args.port, args.share)
    if args.print_only:
        display_drive = drive or "O:"
        print(f"HTTP share: {url}")
        print(f"UNC share: {unc}")
        print(f"DavWWWRoot UNC share: {dav_unc}")
        print(f"Preferred map command: net use {display_drive} {url} /persistent:no")
        print(f"UNC fallback: net use {display_drive} {dav_unc} /persistent:no")
        print(f"Blender file path: {display_drive}\\{args.name}")
        return 0

    if os.name != "nt":
        print(
            "Drive mapping is Windows-only. Use --print-only to inspect commands.",
            file=sys.stderr,
        )
        return 2

    status = webclient_status()
    print(f"WebClient: {status}")
    should_start = args.start_webclient or not args.no_start_webclient
    if should_start and "RUNNING" not in status.upper():
        try:
            print("Starting WebClient service...")
            maybe_start_webclient()
            status = webclient_status()
            print(f"WebClient: {status}")
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            return 1

    if args.action == "unmount":
        if drive is None:
            print("--drive is required for unmount", file=sys.stderr)
            return 2
        try:
            unmapped = unmount_share(drive=drive)
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            return 1
        print(f"Unmapped {unmapped}")
        return 0

    if not args.skip_http_check:
        ok, message = check_http_endpoint(args.host, args.port, args.share, args.name)
        print(message)
        if not ok:
            print(
                "Start the server with --vfs-port first, or pass --skip-http-check "
                "if you intentionally want to try mapping anyway.",
                file=sys.stderr,
            )
            return 1

    try:
        mapped_drive, mapped_unc = mount_share(
            host=args.host,
            port=args.port,
            share=args.share,
            drive=drive,
            persistent=args.persistent,
            force=args.force,
            target_form=args.target_form,
        )
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    share_path = mapped_drive + "\\"
    file_path = share_path + args.name
    print(f"Mapped {mapped_drive} to {mapped_unc}")
    print(f"Open this in Blender or any file picker: {file_path}")
    if args.open:
        subprocess.Popen(["explorer.exe", share_path])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
