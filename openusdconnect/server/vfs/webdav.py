"""WebDAV frontend for OpenUSDConnect virtual USD files."""

from __future__ import annotations

import logging
import threading

from ..types import VfsWriteRejectedError

try:
    # wsgidav.util must load before wsgidav.dav_error; importing dav_error
    # first trips a circular import inside wsgidav itself.
    from wsgidav import util as _wsgidav_util  # noqa: F401
    from wsgidav.dav_error import HTTP_CONFLICT, HTTP_FORBIDDEN, DAVError
    from wsgidav.dav_provider import DAVCollection, DAVNonCollection, DAVProvider
    from wsgidav.wsgidav_app import WsgiDAVApp
except ImportError as exc:  # pragma: no cover - exercised only without deps
    raise ImportError(
        "The VFS WebDAV frontend requires optional dependencies. "
        "Install with: uv sync --group vfs  (or: pip install wsgidav cheroot)"
    ) from exc

LOG = logging.getLogger(__name__)


class _StageFileResource(DAVNonCollection):
    """A virtual file resource."""

    def __init__(self, path: str, environ: dict, provider_file):
        super().__init__(path, environ)
        self._file = provider_file

    def get_content(self):
        import io

        return io.BytesIO(self._file.read())

    def get_content_length(self):
        return self._file.stat().size

    def get_content_type(self):
        return getattr(self._file, "content_type", "application/octet-stream")

    def get_etag(self):
        # wsgidav emits this as the ETag header value and adds quotes itself.
        return self._file.stat().etag.strip('"')

    def support_etag(self):
        return True

    def support_ranges(self):
        return False

    def get_creation_date(self):
        return None

    def get_last_modified(self):
        return self._file.stat().mtime

    def support_modified(self):
        return True

    def begin_write(self, *, content_type=None):
        if not self._file.can_write():
            raise DAVError(HTTP_FORBIDDEN)
        self._sink = self._file.open_write_sink()
        return self._sink

    def end_write(self, *, with_errors):
        if with_errors:
            LOG.warning("VFS write aborted with errors for %s", self._file.name)
            return
        try:
            self._file.finish_write(self._sink)
        except VfsWriteRejectedError as exc:
            LOG.warning("VFS write rejected for %s: %s", self._file.name, exc)
            raise DAVError(HTTP_CONFLICT) from exc

    def delete(self):
        raise DAVError(HTTP_FORBIDDEN)

    def copy_move_single(self, dest_path, *, is_move):
        raise DAVError(HTTP_FORBIDDEN)

    def support_recursive_move(self, dest_path):
        return False

    def move_recursive(self, dest_path):
        raise DAVError(HTTP_FORBIDDEN)


class _ShareCollection(DAVCollection):
    """A collection in the virtual file tree."""

    def __init__(self, path: str, environ: dict, file_set, rel_path: str = ""):
        super().__init__(path, environ)
        self._file_set = file_set
        self._rel_path = rel_path.strip("/")

    def get_member_names(self):
        return self._file_set.get_member_names(self._rel_path)

    def get_member(self, name: str):
        rel = "/".join(p for p in (self._rel_path, name) if p)
        path = self.path.rstrip("/") + "/" + name
        file = self._file_set.get_file(rel)
        if file is not None:
            return _StageFileResource(path, self.environ, file)
        if self._file_set.is_collection(rel):
            return _ShareCollection(path, self.environ, self._file_set, rel)
        return None

    def create_empty_resource(self, name: str):
        raise DAVError(HTTP_FORBIDDEN)

    def create_collection(self, name: str):
        raise DAVError(HTTP_FORBIDDEN)

    def delete(self):
        raise DAVError(HTTP_FORBIDDEN)

    def copy_move_single(self, dest_path, *, is_move):
        raise DAVError(HTTP_FORBIDDEN)

    def support_recursive_move(self, dest_path):
        return False

    def move_recursive(self, dest_path):
        raise DAVError(HTTP_FORBIDDEN)


class _SingleFileSet:
    """Compatibility adapter for callers that pass one VirtualStageFile."""

    def __init__(self, provider_file):
        self._file = provider_file

    def get_file(self, path: str):
        return self._file if path.strip("/") == self._file.name else None

    def is_collection(self, path: str) -> bool:
        return path.strip("/") == ""

    def get_member_names(self, path: str) -> list[str]:
        return [self._file.name] if path.strip("/") == "" else []


def _as_file_set(provider_file):
    if hasattr(provider_file, "get_file") and hasattr(provider_file, "get_member_names"):
        return provider_file
    return _SingleFileSet(provider_file)


class OpenUsdConnectDavProvider(DAVProvider):
    """Maps WebDAV resources to the OpenUSDConnect virtual file tree."""

    def __init__(self, provider_file):
        super().__init__()
        self._file_set = _as_file_set(provider_file)

    def get_resource_inst(self, path: str, environ: dict):
        self._count_get_resource_inst += 1
        rel = path.strip("/")
        norm = "/" + rel if rel else "/"
        file = self._file_set.get_file(rel)
        if file is not None:
            return _StageFileResource(norm, environ, file)
        if self._file_set.is_collection(rel):
            return _ShareCollection(norm, environ, self._file_set, rel)
        return None


def _no_cache_middleware(app):
    """Force revalidation so WebDAV clients never serve stale stage bytes."""

    def wrapped(environ, start_response):
        def sr(status, headers, exc_info=None):
            if environ.get("REQUEST_METHOD") in ("GET", "HEAD"):
                headers = [h for h in headers if h[0].lower() != "cache-control"]
                headers.append(("Cache-Control", "no-cache"))
            return start_response(status, headers, exc_info)

        return app(environ, sr)

    return wrapped


class VfsServerHandle:
    """Running WebDAV server; stop() shuts it down and joins the thread."""

    def __init__(self, server, thread: threading.Thread):
        self._server = server
        self._thread = thread

    def stop(self) -> None:
        try:
            self._server.stop()
        except Exception:
            LOG.exception("Failed to stop VFS WebDAV server")
        self._thread.join(timeout=5.0)


def run_vfs_server(
    provider_file,
    host: str,
    port: int,
    share: str = "usd",
) -> VfsServerHandle:
    """Start the WebDAV server on a daemon thread and return a stop handle."""
    from cheroot import wsgi as cheroot_wsgi

    config = {
        "host": host,
        "port": port,
        "provider_mapping": {f"/{share}": OpenUsdConnectDavProvider(provider_file)},
        # Anonymous access; live sync auth remains on the TCP protocol.
        "simple_dc": {"user_mapping": {"*": True}},
        # Default lock manager stays enabled (class-2 DAV for Windows WebClient).
        "verbose": 0,
    }
    app = _no_cache_middleware(WsgiDAVApp(config))

    server = cheroot_wsgi.Server((host, port), app, server_name="openusdconnect-vfs")
    server.prepare()
    thread = threading.Thread(target=server.serve, name="OpenUSDConnect_VFS", daemon=True)
    thread.start()
    LOG.info("VFS WebDAV serving /%s on %s:%d", share, host, port)
    return VfsServerHandle(server, thread)
