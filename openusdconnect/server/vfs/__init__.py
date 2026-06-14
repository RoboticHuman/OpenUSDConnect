"""Virtual USD file: serve the live stage as a normal-looking .usd file.

The provider layer (:class:`VirtualStageFile`) is transport-agnostic; the
WebDAV frontend (:func:`run_vfs_server`) is the first transport. A future
WinFsp/FUSE frontend can consume the same provider API.
"""

from .provider import VfsStat, VirtualStageFile, VirtualStageFileSet, WriteMode

__all__ = [
    "VfsStat",
    "VirtualStageFile",
    "VirtualStageFileSet",
    "WriteMode",
    "run_vfs_server",
]


def run_vfs_server(*args, **kwargs):
    """Lazy wrapper — importing the WebDAV frontend requires optional deps."""
    from .webdav import run_vfs_server as _run

    return _run(*args, **kwargs)
