"""Transport-agnostic virtual USD files backed by the live server stage.

The VFS exposes a small browsable directory instead of only one file:

* ``scene.usd`` - flattened universal fallback snapshot.
* ``scene.live.usda`` - composition-aware root that layers live server
  overrides over the original base layer when available.
* ``_layers/*.usda`` - exported live override/base layers for inspection and
  composition-aware opens.
* ``openusdconnect.json`` - machine-readable manifest for launchers/diagnostics.

Plugin-enabled clients can still open the flattened file and read
``customLayerData["openusdconnect"]``. USD tools that can browse the virtual
directory can open the composition root and resolve companion layer files from
the same share.
"""

from __future__ import annotations

import enum
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pxr import Sdf, Usd

from ...asset_paths import repair_missing_duplicate_asset_paths
from ...protocol_constants import PROTOCOL_VERSION
from ..types import InvalidVfsWriteError

if TYPE_CHECKING:
    from ..state import UsdSyncServer

LOG = logging.getLogger(__name__)

METADATA_KEY = "openusdconnect"

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


class WriteMode(enum.Enum):
    """How direct writes to virtual files are handled."""

    FORBID = "forbid"
    DROP = "drop"
    # Experimental fallback: parse a full saved snapshot and rebuild the
    # live event stream from the protocol-supported authored subset.
    TRANSLATE = "translate"


@dataclass(frozen=True)
class VfsStat:
    """File metadata for the current snapshot."""

    size: int
    mtime: float
    etag: str
    generation_ms: float = 0.0


@dataclass(frozen=True)
class VfsSnapshot:
    """Immutable bytes and metadata from one provider generation."""

    data: bytes
    stat: VfsStat


@dataclass(frozen=True)
class _LayerSpec:
    name: str
    identifier: str
    role: str
    original_path: str = ""


class _DiscardSink:
    """Write sink that counts and discards bytes."""

    def __init__(self):
        self.bytes_written = 0

    def write(self, data: bytes) -> int:
        self.bytes_written += len(data)
        return len(data)

    def close(self):
        pass


class _BufferedWriteSink:
    """Write sink that buffers uploaded USD bytes until WebDAV commit."""

    def __init__(self):
        self.bytes_written = 0
        self._file = tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024)

    def write(self, data: bytes) -> int:
        self.bytes_written += len(data)
        return self._file.write(data)

    def getvalue(self) -> bytes:
        self._file.seek(0)
        return self._file.read()

    def close(self):
        # WsgiDAV may call close() before end_write(); keep bytes available
        # until finish_write() has translated them.
        pass

    def dispose(self):
        self._file.close()


def _open_uploaded_stage(data: bytes) -> Usd.Stage:
    """Open uploaded USD bytes as a temporary layer.

    The server serves USDA text under a ``.usd`` file name, but a DCC may save
    back either text or crate bytes. Writing to a real temporary ``.usd`` lets
    OpenUSD pick the correct parser.
    """
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".usd")
    tmp_path = tmp.name
    try:
        with tmp:
            tmp.write(data)
        try:
            layer = Sdf.Layer.FindOrOpen(tmp_path)
            if layer is None:
                raise InvalidVfsWriteError("uploaded bytes are not a readable USD layer")
            anon = Sdf.Layer.CreateAnonymous(".usda")
            anon.TransferContent(layer)
            stage = Usd.Stage.Open(anon)
            if stage is None:
                raise InvalidVfsWriteError("uploaded USD layer did not open as a stage")
            return stage
        except InvalidVfsWriteError:
            raise
        except Exception as exc:
            raise InvalidVfsWriteError("uploaded bytes are not a valid USD file") from exc
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def _safe_stem(value: str, fallback: str) -> str:
    stem = os.path.splitext(os.path.basename(value))[0]
    if not stem:
        stem = fallback
    stem = _SAFE_NAME_RE.sub("-", stem).strip(".-")
    return stem or fallback


def _layer_asset_path(path: str) -> str:
    return path.replace("\\", "/")


class _CachedVirtualFile:
    """Base class for dynamic VFS files cached by the server snapshot token."""

    def __init__(
        self,
        sync_server: UsdSyncServer,
        *,
        name: str,
        write_mode: WriteMode = WriteMode.FORBID,
        content_type: str = "application/octet-stream",
        validate_writes: bool = True,
    ):
        self._server = sync_server
        self.name = name
        self._write_mode = write_mode
        self._content_type = content_type
        self._validate_writes = validate_writes
        self._cache_lock = threading.Lock()
        self._cached_bytes: bytes | None = None
        self._cached_key: tuple[int, int] | None = None
        self._cached_mtime: float = 0.0
        self._cached_generation_ms: float = 0.0

    @property
    def content_type(self) -> str:
        return self._content_type

    @property
    def write_mode(self) -> WriteMode:
        return self._write_mode

    @property
    def validate_writes(self) -> bool:
        return self._validate_writes

    def can_write(self) -> bool:
        return self._write_mode in (WriteMode.DROP, WriteMode.TRANSLATE)

    def read(self) -> bytes:
        return self.snapshot().data

    def stat(self) -> VfsStat:
        return self.snapshot().stat

    def snapshot(self) -> VfsSnapshot:
        """Return content and metadata pinned to the same cache generation."""
        data, key, mtime, generation_ms = self._ensure_current()
        return VfsSnapshot(
            data=data,
            stat=VfsStat(
                size=len(data),
                mtime=mtime,
                etag=f'"{key[0]}-{key[1]}"',
                generation_ms=generation_ms,
            ),
        )

    def prewarm(self) -> None:
        self.read()

    def _ensure_current(self) -> tuple[bytes, tuple[int, int], float, float]:
        with self._cache_lock:
            key = self._server.get_snapshot_token()
            if self._cached_bytes is not None and key == self._cached_key:
                return (
                    self._cached_bytes,
                    self._cached_key,
                    self._cached_mtime,
                    self._cached_generation_ms,
                )
            start = time.perf_counter()
            data, key = self._generate()
            generation_ms = (time.perf_counter() - start) * 1000.0
            self._cached_bytes = data
            self._cached_key = key
            self._cached_mtime = time.time()
            self._cached_generation_ms = generation_ms
            LOG.info(
                "VFS generated %s at epoch=%d seq=%d size=%d in %.1f ms",
                self.name,
                key[0],
                key[1],
                len(data),
                generation_ms,
            )
            return data, key, self._cached_mtime, generation_ms

    def _generate(self) -> tuple[bytes, tuple[int, int]]:
        raise NotImplementedError

    def write(self, data: bytes) -> None:
        if self._write_mode is WriteMode.FORBID:
            raise PermissionError(f"{self.name} is read-only")
        if self._write_mode is WriteMode.DROP:
            LOG.warning("VFS write dropped (%d bytes) for %s", len(data), self.name)
            return
        if self._write_mode is WriteMode.TRANSLATE:
            self._translate_write(data)
            return
        raise NotImplementedError(f"unknown write mode {self._write_mode}")

    def open_write_sink(self) -> _DiscardSink:
        if self._write_mode is WriteMode.FORBID:
            raise PermissionError(f"{self.name} is read-only")
        if self._write_mode is WriteMode.DROP:
            return _DiscardSink()
        if self._write_mode is WriteMode.TRANSLATE:
            return _BufferedWriteSink()
        raise NotImplementedError(f"unknown write mode {self._write_mode}")

    def finish_write(self, sink: _DiscardSink | _BufferedWriteSink) -> None:
        if self._write_mode is WriteMode.FORBID:
            raise PermissionError(f"{self.name} is read-only")
        if self._write_mode is WriteMode.DROP:
            LOG.warning("VFS write dropped (%d bytes) for %s", sink.bytes_written, self.name)
            return
        if self._write_mode is WriteMode.TRANSLATE:
            try:
                data = sink.getvalue()
                self._translate_write(data)
                LOG.warning(
                    "VFS write translated (%d bytes) for %s",
                    sink.bytes_written,
                    self.name,
                )
            finally:
                sink.dispose()
            return
        raise NotImplementedError(f"unknown write mode {self._write_mode}")

    def abort_write(self, sink: _DiscardSink | _BufferedWriteSink) -> None:
        """Release an upload sink when WebDAV aborts before commit."""
        if isinstance(sink, _BufferedWriteSink):
            sink.dispose()
        else:
            sink.close()

    def _translate_write(self, data: bytes) -> None:
        raise NotImplementedError(f"{self.name} cannot translate writes")


class VirtualStageFile(_CachedVirtualFile):
    """Flattened live stage as a single normal-looking USD file."""

    def __init__(
        self,
        sync_server: UsdSyncServer,
        *,
        name: str,
        advertise_host: str,
        sync_port: int,
        write_mode: WriteMode = WriteMode.FORBID,
        fmt: str = "usda",
        department: str | None = None,
        scene_id: str | None = None,
        vfs_url: str = "",
        validate_writes: bool = True,
    ):
        if fmt != "usda":
            raise ValueError(f"format {fmt!r} is not implemented yet (only 'usda')")
        super().__init__(
            sync_server,
            name=name,
            write_mode=write_mode,
            validate_writes=validate_writes,
        )
        self._advertise_host = advertise_host
        self._sync_port = sync_port
        self._fmt = fmt
        self._department = department
        self._scene_id = scene_id or getattr(sync_server, "scene_id", name.rsplit(".", 1)[0])
        self._vfs_url = vfs_url

    def _generate(self) -> tuple[bytes, tuple[int, int]]:
        srv = self._server
        srv.txn_barrier.acquire_shared()
        try:
            epoch, seq = srv.get_snapshot_token()
            with srv.stage_lock:
                flat = srv.stage.Flatten()
        finally:
            srv.txn_barrier.release_shared()

        repair_missing_duplicate_asset_paths(flat)

        cld = dict(flat.customLayerData)
        cld[METADATA_KEY] = self.build_metadata(seq, epoch)
        flat.customLayerData = cld
        return flat.ExportToString().encode("utf-8"), (epoch, seq)

    def build_metadata(self, snapshot_seq: int, epoch: int, **extra) -> dict:
        meta = {
            "live": True,
            "host": self._advertise_host,
            "port": self._sync_port,
            "protocol_version": PROTOCOL_VERSION,
            "scene_id": self._scene_id,
            "snapshot_seq": snapshot_seq,
            "epoch": epoch,
            "vfs_url": self._vfs_url,
            "department": self._department or "",
            "requires_token": bool(self._server.require_token),
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        meta.update(extra)
        return meta

    def _translate_write(self, data: bytes) -> None:
        try:
            stage = _open_uploaded_stage(data)
        except InvalidVfsWriteError:
            if self._validate_writes:
                raise
            LOG.warning(
                "VFS write validation bypassed for %s; invalid USD bytes were accepted and dropped",
                self.name,
            )
            return
        event_count = self._server.replace_from_stage_snapshot(stage)
        LOG.warning(
            "VFS write fallback translated %d bytes from %s into %d events",
            len(data),
            self.name,
            event_count,
        )


class _GeneratedTextFile(_CachedVirtualFile):
    def __init__(
        self,
        sync_server: UsdSyncServer,
        *,
        name: str,
        generator: Callable[[int, int], str | bytes],
        content_type: str = "application/octet-stream",
        write_mode: WriteMode = WriteMode.FORBID,
        validate_writes: bool = True,
    ):
        super().__init__(
            sync_server,
            name=name,
            write_mode=write_mode,
            content_type=content_type,
            validate_writes=validate_writes,
        )
        self._generator = generator

    def _generate(self) -> tuple[bytes, tuple[int, int]]:
        srv = self._server
        srv.txn_barrier.acquire_shared()
        try:
            epoch, seq = srv.get_snapshot_token()
            data = self._generator(epoch, seq)
        finally:
            srv.txn_barrier.release_shared()
        if isinstance(data, str):
            data = data.encode("utf-8")
        return data, (epoch, seq)


class VirtualStageFileSet:
    """Browsable virtual directory for one live server scene."""

    def __init__(
        self,
        sync_server: UsdSyncServer,
        *,
        flat_name: str,
        advertise_host: str,
        sync_port: int,
        share: str,
        vfs_base_url: str,
        write_mode: WriteMode = WriteMode.FORBID,
        live_name: str | None = None,
        layer_dir: str = "_layers",
        manifest_name: str = "openusdconnect.json",
        scene_id: str | None = None,
        validate_writes: bool = True,
    ):
        self._server = sync_server
        self.flat_name = flat_name
        self.live_name = live_name or _default_live_name(flat_name)
        self.layer_dir = layer_dir.strip("/")
        self.manifest_name = manifest_name
        self.share = share
        self.vfs_base_url = vfs_base_url.rstrip("/")
        self._advertise_host = advertise_host
        self._sync_port = sync_port
        self._scene_id = scene_id or getattr(sync_server, "scene_id", flat_name)
        self._write_mode = write_mode
        self._validate_writes = validate_writes
        self.flattened = VirtualStageFile(
            sync_server,
            name=flat_name,
            advertise_host=advertise_host,
            sync_port=sync_port,
            write_mode=write_mode,
            scene_id=self._scene_id,
            vfs_url=f"{self.vfs_base_url}/{flat_name}",
            validate_writes=validate_writes,
        )
        self._composition = _GeneratedTextFile(
            sync_server,
            name=self.live_name,
            generator=self._generate_composition_root,
        )
        self._manifest = _GeneratedTextFile(
            sync_server,
            name=self.manifest_name,
            generator=self._generate_manifest,
            content_type="application/json",
        )

    def prewarm(self, include_flattened: bool = True) -> threading.Thread:
        def _run():
            try:
                self._composition.prewarm()
                self._manifest.prewarm()
                if include_flattened:
                    self.flattened.prewarm()
            except Exception:
                LOG.exception("VFS prewarm failed")

        thread = threading.Thread(target=_run, name="OpenUSDConnect_VFS_Prewarm", daemon=True)
        thread.start()
        return thread

    def get_file(self, path: str):
        rel = path.strip("/")
        if rel == self.flat_name:
            return self.flattened
        if rel == self.live_name:
            return self._composition
        if rel == self.manifest_name:
            return self._manifest
        prefix = self.layer_dir + "/"
        if rel.startswith(prefix):
            layer_name = rel[len(prefix) :]
            if "/" in layer_name or not layer_name:
                return None
            spec = self._layer_spec_by_name(layer_name)
            if spec is None:
                return None
            return _GeneratedTextFile(
                self._server,
                name=spec.name,
                generator=lambda _epoch, _seq, name=spec.name: self._export_layer(name),
            )
        return None

    def is_collection(self, path: str) -> bool:
        rel = path.strip("/")
        return rel in ("", self.layer_dir)

    def get_member_names(self, path: str) -> list[str]:
        rel = path.strip("/")
        if rel == "":
            return [self.flat_name, self.live_name, self.manifest_name, self.layer_dir]
        if rel == self.layer_dir:
            return [spec.name for spec in self._layer_specs()]
        return []

    def _base_metadata(self, seq: int, epoch: int, vfs_url: str, **extra) -> dict:
        return self.flattened.build_metadata(seq, epoch, vfs_url=vfs_url, **extra)

    def _generate_composition_root(self, epoch: int, seq: int) -> str:
        specs = self._layer_specs()
        overlay_specs = [spec for spec in specs if spec.role != "base"]
        base_spec = next((spec for spec in specs if spec.role == "base"), None)
        base_asset = (
            _layer_asset_path(base_spec.original_path)
            if base_spec and base_spec.original_path
            else f"{self.layer_dir}/base.usda"
        )

        layer = Sdf.Layer.CreateAnonymous(".usda")
        for spec in overlay_specs:
            layer.subLayerPaths.append(f"{self.layer_dir}/{spec.name}")
        layer.subLayerPaths.append(base_asset)
        layer.customLayerData = {
            METADATA_KEY: self._base_metadata(
                seq,
                epoch,
                f"{self.vfs_base_url}/{self.live_name}",
                composition_preserving=True,
                flattened_fallback=f"{self.vfs_base_url}/{self.flat_name}",
                manifest=f"{self.vfs_base_url}/{self.manifest_name}",
                layer_dir=f"{self.vfs_base_url}/{self.layer_dir}/",
                base_asset=base_asset,
            )
        }
        return layer.ExportToString()

    def _generate_manifest(self, epoch: int, seq: int) -> str:
        specs = self._layer_specs()
        files = [
            {
                "name": self.flat_name,
                "kind": "flattened_snapshot",
                "url": f"{self.vfs_base_url}/{self.flat_name}",
            },
            {
                "name": self.live_name,
                "kind": "composition_root",
                "url": f"{self.vfs_base_url}/{self.live_name}",
            },
        ]
        for spec in specs:
            files.append(
                {
                    "name": f"{self.layer_dir}/{spec.name}",
                    "kind": f"{spec.role}_layer",
                    "url": f"{self.vfs_base_url}/{self.layer_dir}/{spec.name}",
                    "original_path": spec.original_path,
                }
            )
        payload = {
            "openusdconnect": self._base_metadata(
                seq,
                epoch,
                f"{self.vfs_base_url}/{self.manifest_name}",
            ),
            "files": files,
            "write_mode": self._write_mode.value,
            "write_validation": self._write_mode is WriteMode.TRANSLATE and self._validate_writes,
            "notes": [
                "scene.usd is the universal flattened fallback.",
                (
                    "scene.live.usda preserves the live overlay stack and uses "
                    "the original base layer path when available."
                ),
            ],
        }
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"

    def _export_layer(self, layer_name: str) -> str:
        spec = self._layer_spec_by_name(layer_name)
        if spec is None:
            return "#usda 1.0\n"
        with self._server.stage_lock:
            layer = Sdf.Layer.Find(spec.identifier)
            return layer.ExportToString() if layer else "#usda 1.0\n"

    def _layer_spec_by_name(self, name: str) -> _LayerSpec | None:
        for spec in self._layer_specs():
            if spec.name == name:
                return spec
        return None

    def _layer_specs(self) -> list[_LayerSpec]:
        srv = self._server
        with srv.stage_lock:
            root = srv.stage.GetRootLayer()
            specs = [
                _LayerSpec(
                    name="base.usda",
                    identifier=root.identifier,
                    role="base",
                    original_path=root.realPath or "",
                )
            ]
            muted = set(srv.stage.GetMutedLayers())
            session = srv.stage.GetSessionLayer()
            used = {"base.usda"}
            for identifier in list(session.subLayerPaths):
                if identifier in muted:
                    continue
                layer = Sdf.Layer.Find(identifier)
                if layer is None:
                    continue
                name = self._name_for_layer(layer, used)
                specs.append(
                    _LayerSpec(
                        name=name,
                        identifier=identifier,
                        role=self._role_for_layer(layer),
                    )
                )
                used.add(name)
        return specs

    def _role_for_layer(self, layer: Sdf.Layer) -> str:
        srv = self._server
        if layer is srv.edit_layer or layer.identifier == srv.edit_layer.identifier:
            return "edit"
        department = srv.department_for_layer(layer)
        if department:
            return f"department:{department}"
        return "overlay"

    def _name_for_layer(self, layer: Sdf.Layer, used: set[str]) -> str:
        srv = self._server
        if layer is srv.edit_layer or layer.identifier == srv.edit_layer.identifier:
            candidate = "server-edits.usda"
        else:
            candidate = ""
            department = srv.department_for_layer(layer)
            if department:
                candidate = f"dept-{_safe_stem(department, 'department')}.usda"
            if not candidate:
                digest = hashlib.sha1(layer.identifier.encode("utf-8")).hexdigest()[:8]
                candidate = f"{_safe_stem(layer.identifier, 'layer')}-{digest}.usda"
        if candidate not in used:
            return candidate
        stem, ext = os.path.splitext(candidate)
        i = 2
        while f"{stem}-{i}{ext}" in used:
            i += 1
        return f"{stem}-{i}{ext}"


def _default_live_name(flat_name: str) -> str:
    stem, _ext = os.path.splitext(flat_name)
    return f"{stem or 'scene'}.live.usda"
