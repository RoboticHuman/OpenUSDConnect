"""Small argparse helpers shared by OpenUSDConnect command-line tools."""

from __future__ import annotations

import argparse

from .defaults import (
    DEFAULT_HOST,
    DEFAULT_SYNC_PORT,
    DEFAULT_VFS_NAME,
    DEFAULT_VFS_PORT,
    DEFAULT_VFS_SHARE,
)


def validate_port(value: str | int) -> int:
    """Return a valid TCP port or raise ``ValueError``."""

    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("must be between 1 and 65535")
    return port


def port_number(value: str | int) -> int:
    """Argparse adapter for :func:`validate_port`."""

    try:
        return validate_port(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def port_or_zero(value: str | int) -> int:
    """Parse a TCP port while allowing zero to mean disabled/automatic."""

    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if number == 0:
        return 0
    return port_number(number)


def positive_seconds(value: str | float) -> float:
    """Parse a strictly positive duration in seconds."""

    try:
        return validate_positive_seconds(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def validate_positive_seconds(value: str | float) -> float:
    """Return a positive duration or raise ``ValueError``."""

    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("must be a number") from exc
    if seconds <= 0:
        raise ValueError("must be greater than zero")
    return seconds


def nonnegative_float(value: str | float) -> float:
    """Parse a non-negative floating-point value."""

    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if seconds < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return seconds


def positive_float(value: str | float) -> float:
    """Parse a strictly positive floating-point value."""

    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def nonnegative_seconds(value: str | float) -> float:
    """Parse a duration that may use zero to disable a feature."""

    return nonnegative_float(value)


def positive_int(value: str | int) -> int:
    """Parse a strictly positive integer."""

    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def nonnegative_int(value: str | int) -> int:
    """Parse an integer that may use zero to disable a feature."""

    try:
        return validate_nonnegative_int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def validate_nonnegative_int(value: str | int) -> int:
    """Return a non-negative integer or raise ``ValueError``."""

    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("must be an integer") from exc
    if number < 0:
        raise ValueError("must be zero or greater")
    return number


def parse_bool(value: str | bool) -> bool:
    """Parse a conventional environment/configuration boolean."""

    if isinstance(value, bool):
        return value
    normalized = value.strip().lower()
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off"):
        return False
    raise ValueError("must be a boolean (1/0, true/false, yes/no, on/off)")


def comma_separated(value: str) -> list[str]:
    """Parse a non-empty comma-separated list while trimming whitespace."""

    values = [item.strip() for item in value.split(",") if item.strip()]
    if not values:
        raise argparse.ArgumentTypeError("must contain at least one value")
    return values


def validate_path_segment(value: str) -> str:
    """Return one non-empty relative path segment or raise ``ValueError``."""

    normalized = value.strip("/")
    if (
        not normalized.strip()
        or normalized in (".", "..")
        or "/" in normalized
        or "\\" in normalized
    ):
        raise ValueError("must be a single non-empty path segment")
    return normalized


def path_segment(value: str) -> str:
    """Argparse adapter for :func:`validate_path_segment`."""

    try:
        return validate_path_segment(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def validate_file_name(value: str) -> str:
    """Return one file name without directory components or raise ``ValueError``."""

    if not value.strip() or value in (".", "..") or "/" in value or "\\" in value:
        raise ValueError("must be a single file name, not a path")
    return value


def file_name(value: str) -> str:
    """Argparse adapter for :func:`validate_file_name`."""

    try:
        return validate_file_name(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def add_sync_endpoint_args(
    parser,
    *,
    host_default: str | None = DEFAULT_HOST,
    port_default: int | None = DEFAULT_SYNC_PORT,
    option_prefix: str = "",
    description: str = "sync server",
) -> None:
    """Add consistently named host/port options for a sync endpoint."""

    normalized = option_prefix.strip("-")
    option_stem = f"{normalized}-" if normalized else ""
    dest_stem = f"{normalized.replace('-', '_')}_" if normalized else ""
    parser.add_argument(
        f"--{option_stem}host",
        dest=f"{dest_stem}host",
        default=host_default,
        help=f"{description.capitalize()} host",
    )
    parser.add_argument(
        f"--{option_stem}port",
        dest=f"{dest_stem}port",
        type=port_number,
        default=port_default,
        metavar="PORT",
        help=f"{description.capitalize()} port",
    )


def add_vfs_resource_args(
    parser,
    *,
    option_prefix: str = "vfs-",
    host_default: str | None = DEFAULT_HOST,
    port_default: int | None = DEFAULT_VFS_PORT,
    share_default: str = DEFAULT_VFS_SHARE,
    name_default: str = DEFAULT_VFS_NAME,
) -> None:
    """Add host/port/share/name options for a virtual USD resource."""

    normalized = option_prefix.strip("-")
    option_stem = f"{normalized}-" if normalized else ""
    dest_stem = f"{normalized.replace('-', '_')}_" if normalized else ""
    parser.add_argument(
        f"--{option_stem}host",
        dest=f"{dest_stem}host",
        default=host_default,
        metavar="HOST",
        help="WebDAV host/interface",
    )
    parser.add_argument(
        f"--{option_stem}port",
        dest=f"{dest_stem}port",
        type=port_number,
        default=port_default,
        metavar="PORT",
        help="WebDAV port",
    )
    parser.add_argument(
        f"--{option_stem}share",
        dest=f"{dest_stem}share",
        type=path_segment,
        default=share_default,
        metavar="NAME",
        help="WebDAV share/collection name",
    )
    parser.add_argument(
        f"--{option_stem}name",
        dest=f"{dest_stem}name",
        type=file_name,
        default=name_default,
        metavar="FILE",
        help="Virtual USD filename",
    )
