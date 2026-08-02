"""Mount an OpenUSDConnect WebDAV share using the host operating system."""

from __future__ import annotations

import argparse
import http.client
import os
import platform
import shlex
import string
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from .cli_common import add_vfs_resource_args
from .defaults import host_for_url


@dataclass(frozen=True)
class MountConfig:
    action: Literal["mount", "unmount"]
    host: str
    port: int
    share: str
    name: str
    force: bool
    open: bool
    skip_http_check: bool
    print_only: bool


@dataclass(frozen=True)
class WindowsMountConfig:
    common: MountConfig
    drive: str | None
    persistent: bool
    start_webclient: bool
    target_form: str


@dataclass(frozen=True)
class MacOSMountConfig:
    common: MountConfig
    mount_point: Path
    volume_name: str
    read_only: bool


NativeMountConfig = WindowsMountConfig | MacOSMountConfig


@dataclass(frozen=True)
class MountedLocation:
    root_path: str
    file_path: str


def _share_name(share: str) -> str:
    value = share.strip("/").strip("\\")
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError("share must be one non-empty path component")
    return value


def _relative_name(name: str) -> str:
    value = name.replace("\\", "/")
    path = PurePosixPath(value)
    if not path.parts or path.is_absolute() or ".." in path.parts:
        raise ValueError("name must be a relative path inside the VFS share")
    return str(path)


def default_unc(host: str, port: int, share: str) -> str:
    return f"\\\\{host}@{port}\\{_share_name(share)}"


def default_davwwwroot_unc(host: str, port: int, share: str) -> str:
    return f"\\\\{host}@{port}\\DavWWWRoot\\{_share_name(share)}"


def default_url(host: str, port: int, share: str) -> str:
    return f"http://{host_for_url(host)}:{port}/{_share_name(share)}"


def default_macos_mount_point(share: str, *, home: Path | None = None) -> Path:
    root = home if home is not None else Path.home()
    directory = _share_name(share).replace("/", "-").replace("\\", "-")
    return root / ".openusdconnect" / "mounts" / directory


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


def native_backend(system: str | None = None) -> str:
    system = system or platform.system()
    if system == "Windows":
        return "windows"
    if system == "Darwin":
        return "macos"
    raise RuntimeError(
        f"native WebDAV mounting is not implemented for {system or 'this platform'}; "
        "use scripts/local_vfs_bridge.py instead"
    )


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


def _process_error(result: subprocess.CompletedProcess, fallback: str) -> str:
    return (result.stderr or result.stdout).strip() or fallback


def _print_process_failure(result: subprocess.CompletedProcess) -> None:
    if result.stdout.strip():
        print(result.stdout.strip(), file=sys.stderr)
    if result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr)


def check_http_endpoint(host: str, port: int, share: str, name: str) -> tuple[bool, str]:
    path = f"/{_share_name(share)}/{_relative_name(name)}"
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
    tried = ", ".join(target for target, _result in failures)
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


def _prepare_macos_mount_point(path: Path, *, force: bool) -> Path:
    mount_point = path.expanduser().resolve()
    if mount_point.exists() and not mount_point.is_dir():
        raise RuntimeError(f"mount point is not a directory: {mount_point}")
    if os.path.ismount(mount_point):
        if not force:
            raise RuntimeError(f"mount point is already mounted: {mount_point}")
        unmount_macos_share(mount_point=mount_point)
    mount_point.mkdir(parents=True, exist_ok=True)
    if any(mount_point.iterdir()):
        raise RuntimeError(f"refusing to mount over non-empty directory: {mount_point}")
    return mount_point


def macos_mount_command(
    *,
    url: str,
    mount_point: Path,
    volume_name: str,
    read_only: bool,
) -> list[str]:
    command = ["/sbin/mount_webdav", "-S"]
    if read_only:
        command.extend(["-o", "rdonly"])
    command.extend(["-v", volume_name, url, str(mount_point)])
    return command


def mount_macos_share(
    *,
    host: str,
    port: int,
    share: str,
    mount_point: Path,
    volume_name: str,
    read_only: bool,
    force: bool,
) -> tuple[Path, str]:
    mount_point = _prepare_macos_mount_point(mount_point, force=force)
    url = default_url(host, port, share)
    result = _run(
        macos_mount_command(
            url=url,
            mount_point=mount_point,
            volume_name=volume_name,
            read_only=read_only,
        )
    )
    if result.returncode != 0:
        raise RuntimeError(_process_error(result, f"failed to mount {url} at {mount_point}"))
    return mount_point, url


def unmount_macos_share(*, mount_point: Path) -> Path:
    mount_point = mount_point.expanduser().resolve()
    if not os.path.ismount(mount_point):
        raise RuntimeError(f"mount point is not mounted: {mount_point}")
    result = _run(["/sbin/umount", str(mount_point)])
    if result.returncode != 0:
        raise RuntimeError(_process_error(result, f"failed to unmount {mount_point}"))
    return mount_point


def _open_path(path: str, backend: str) -> None:
    if backend == "windows":
        subprocess.Popen(["explorer.exe", path])
    elif backend == "macos":
        subprocess.Popen(["/usr/bin/open", path])


def _print_windows_paths(config: WindowsMountConfig) -> None:
    common = config.common
    url = default_url(common.host, common.port, common.share)
    unc = default_unc(common.host, common.port, common.share)
    dav_unc = default_davwwwroot_unc(common.host, common.port, common.share)
    display_drive = config.drive or "O:"
    name = common.name.replace("/", "\\")
    print(f"HTTP share: {url}")
    print(f"UNC share: {unc}")
    print(f"DavWWWRoot UNC share: {dav_unc}")
    print(f"Preferred map command: net use {display_drive} {url} /persistent:no")
    print(f"UNC fallback: net use {display_drive} {dav_unc} /persistent:no")
    print(f"Live USD file: {display_drive}\\{name}")


def _print_macos_paths(config: MacOSMountConfig) -> None:
    common = config.common
    url = default_url(common.host, common.port, common.share)
    command = macos_mount_command(
        url=url,
        mount_point=config.mount_point,
        volume_name=config.volume_name,
        read_only=config.read_only,
    )
    print(f"HTTP share: {url}")
    print(f"Mount command: {shlex.join(command)}")
    print(f"Live USD file: {config.mount_point / common.name}")
    print(f"Access: {'read-only' if config.read_only else 'read-write'}")


def _build_native_parser(backend: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=["mount", "unmount"], default="mount")
    endpoint = parser.add_argument_group("virtual file service")
    add_vfs_resource_args(endpoint, option_prefix="")
    parser.add_argument("--force", action="store_true", help="Replace an existing native mount")
    parser.add_argument("--open", action="store_true", help="Open the mounted directory")
    parser.add_argument(
        "--skip-http-check",
        action="store_true",
        help="Skip checking that the VFS endpoint is reachable before mounting",
    )
    parser.add_argument("--print-only", action="store_true", help="Print paths and commands only")

    if backend == "windows":
        parser.add_argument("--drive", default=None, help="Drive letter, for example O:")
        parser.add_argument(
            "--persistent", action="store_true", help="Persist mapping across logon"
        )
        webclient = parser.add_mutually_exclusive_group()
        webclient.add_argument(
            "--start-webclient",
            dest="start_webclient",
            action="store_true",
            help="Start WebClient when it is not running (default)",
        )
        webclient.add_argument(
            "--no-start-webclient",
            dest="start_webclient",
            action="store_false",
            help="Do not start WebClient automatically",
        )
        parser.set_defaults(start_webclient=True)
        parser.add_argument(
            "--target-form",
            choices=["auto", "url", "davwwwroot", "unc"],
            default="auto",
            help="Mapping target form; auto tries URL, DavWWWRoot UNC, then short UNC",
        )
    elif backend == "macos":
        parser.add_argument("--mount-point", default="", help="Native mount directory")
        parser.add_argument("--volume-name", default="OpenUSDConnect")
        parser.add_argument(
            "--read-write",
            action="store_true",
            help="Allow native writes; OpenUSD safe-save operations may still fail",
        )
    else:
        raise ValueError(f"unsupported native mount backend: {backend}")
    return parser


def _parse_native_config(backend: str, argv: list[str] | None) -> NativeMountConfig:
    parser = _build_native_parser(backend)
    args = parser.parse_args(argv)
    try:
        common = MountConfig(
            action=args.action,
            host=args.host,
            port=args.port,
            share=_share_name(args.share),
            name=_relative_name(args.name),
            force=args.force,
            open=args.open,
            skip_http_check=args.skip_http_check,
            print_only=args.print_only,
        )
        if backend == "windows":
            drive = normalize_drive(args.drive) if args.drive else None
            if common.action == "unmount" and drive is None:
                parser.error("--drive is required for unmount")
            return WindowsMountConfig(
                common=common,
                drive=drive,
                persistent=args.persistent,
                start_webclient=args.start_webclient,
                target_form=args.target_form,
            )
        mount_point = (
            Path(args.mount_point) if args.mount_point else default_macos_mount_point(common.share)
        )
        return MacOSMountConfig(
            common=common,
            mount_point=mount_point,
            volume_name=args.volume_name,
            read_only=not args.read_write,
        )
    except ValueError as exc:
        parser.error(str(exc))
        raise AssertionError("argparse did not exit") from exc


def _print_native_paths(config: NativeMountConfig) -> None:
    if isinstance(config, WindowsMountConfig):
        _print_windows_paths(config)
    else:
        _print_macos_paths(config)


def _unmount_native(config: NativeMountConfig) -> None:
    if isinstance(config, WindowsMountConfig):
        if config.drive is None:
            raise RuntimeError("drive is required for a Windows unmount")
        print(f"Unmapped {unmount_share(drive=config.drive)}")
    else:
        print(f"Unmounted {unmount_macos_share(mount_point=config.mount_point)}")


def _mount_windows(config: WindowsMountConfig) -> MountedLocation:
    common = config.common
    status = webclient_status()
    print(f"WebClient: {status}")
    if config.start_webclient and "RUNNING" not in status.upper():
        print("Starting WebClient service...")
        maybe_start_webclient()
        print(f"WebClient: {webclient_status()}")
    mapped_drive, source = mount_share(
        host=common.host,
        port=common.port,
        share=common.share,
        drive=config.drive,
        persistent=config.persistent,
        force=common.force,
        target_form=config.target_form,
    )
    root_path = mapped_drive + "\\"
    print(f"Mapped {mapped_drive} to {source}")
    return MountedLocation(
        root_path=root_path,
        file_path=root_path + common.name.replace("/", "\\"),
    )


def _mount_macos(config: MacOSMountConfig) -> MountedLocation:
    common = config.common
    root, source = mount_macos_share(
        host=common.host,
        port=common.port,
        share=common.share,
        mount_point=config.mount_point,
        volume_name=config.volume_name,
        read_only=config.read_only,
        force=common.force,
    )
    print(f"Mounted {source} at {root}")
    if config.read_only:
        print("Native mount is read-only; use local_vfs_bridge.py for safe saves.")
    return MountedLocation(root_path=str(root), file_path=str(root / common.name))


def _mount_native(config: NativeMountConfig) -> MountedLocation:
    if isinstance(config, WindowsMountConfig):
        return _mount_windows(config)
    return _mount_macos(config)


def main(argv: list[str] | None = None) -> int:
    try:
        backend = native_backend()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 2
    config = _parse_native_config(backend, argv)
    common = config.common

    if common.print_only:
        _print_native_paths(config)
        return 0

    if common.action == "unmount":
        try:
            _unmount_native(config)
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            return 1
        return 0

    if not common.skip_http_check:
        ok, message = check_http_endpoint(
            common.host,
            common.port,
            common.share,
            common.name,
        )
        print(message)
        if not ok:
            print(
                "Start the server with --vfs-port first, or pass --skip-http-check "
                "to try mounting anyway.",
                file=sys.stderr,
            )
            return 1

    try:
        location = _mount_native(config)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    print(f"Live USD file: {location.file_path}")
    if common.open:
        _open_path(location.root_path, backend)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
