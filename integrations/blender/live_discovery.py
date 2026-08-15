"""Discover OpenUSDConnect live-sync metadata from a USD file or URL.

A virtual USD file served by the sync server carries a metadata block in
``customLayerData["openusdconnect"]`` (see
``openusdconnect/server/vfs/provider.py``):

    {"live": True, "host": ..., "port": ..., "protocol_version": ...,
     "scene_id": ..., "snapshot_seq": N, "epoch": ..., "vfs_url": ...,
     "department": ..., "requires_token": ...}

This module reads that block so the Blender addon can configure live sync when the
user imports a live file. It is deliberately free of ``bpy`` so it can be
unit-tested outside Blender; it depends only on ``pxr`` and the stdlib.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import urllib.request

LIVE_METADATA_KEY = "openusdconnect"

_REMOTE_SCHEMES = ("http://", "https://")
_CACHE_DIR = os.path.join(tempfile.gettempdir(), "openusdconnect-live-cache")
_TEXTURE_CACHE_DIR = os.path.join(tempfile.gettempdir(), "openusdconnect-texture-cache")
_SAFE_TOKEN_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_PACKAGE_TEXTURE_MARKER = b".usdz["


def is_remote(path: str) -> bool:
    """True if *path* is a URL we fetch over HTTP rather than a local file."""
    return path.startswith(_REMOTE_SCHEMES)


def _cache_token(value: str, fallback: bytes) -> str:
    token = value.strip().strip('"')
    if not token:
        token = hashlib.sha1(fallback).hexdigest()[:16]
    token = _SAFE_TOKEN_RE.sub("_", token).strip("._-")
    return token or hashlib.sha1(fallback).hexdigest()[:16]


def _write_cached_snapshot(url: str, data: bytes, etag: str) -> str:
    url_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    token = _cache_token(etag, data)
    os.makedirs(_CACHE_DIR, exist_ok=True)
    path = os.path.join(_CACHE_DIR, f"ouc_live_{url_hash}_{token}.usd")
    tmp_path = f"{path}.tmp-{os.getpid()}"

    if not os.path.exists(path):
        with open(tmp_path, "wb") as f:
            f.write(data)
        os.replace(tmp_path, path)

    prefix = f"ouc_live_{url_hash}_"
    for name in os.listdir(_CACHE_DIR):
        if name.startswith(prefix) and name != os.path.basename(path):
            try:
                os.remove(os.path.join(_CACHE_DIR, name))
            except OSError:
                pass
    return path


def fetch_to_temp(url: str, timeout: float = 10.0) -> str:
    """Download a remote live file to a cached local ``.usd`` and return it.

    The path is stable for the same URL+ETag so repeated imports do not leak
    temp files. Older cached snapshots for the same URL are pruned when a new
    ETag is observed.
    """
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        data = resp.read()
        etag = resp.headers.get("ETag", "")
    return _write_cached_snapshot(url, data, etag)


def _has_package_texture_references(local_path: str) -> bool:
    if local_path.lower().endswith(".usdz"):
        return True
    try:
        overlap = b""
        with open(local_path, "rb") as stream:
            while chunk := stream.read(1024 * 1024):
                candidate = overlap + chunk.lower()
                if _PACKAGE_TEXTURE_MARKER in candidate:
                    return True
                overlap = candidate[-(len(_PACKAGE_TEXTURE_MARKER) - 1) :]
    except OSError:
        return False
    return False


def texture_import_options(local_path: str, metadata: dict | None = None) -> dict:
    """Return Blender import options that keep package textures alive.

    Blender extracts USDZ textures into a short-lived directory for its default
    packed mode. Some builds retain lazy image datablocks after deleting that
    directory. Copy package-backed textures into a stable per-scene cache so
    Blender can load them on demand after the USD import operator returns.
    """
    if not _has_package_texture_references(local_path):
        return {}

    scene_id = str((metadata or {}).get("scene_id", "")).strip()
    identity = f"scene:{scene_id}" if scene_id else os.path.normcase(os.path.abspath(local_path))
    token = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:20]
    texture_dir = os.path.join(_TEXTURE_CACHE_DIR, token)
    os.makedirs(texture_dir, exist_ok=True)
    return {
        "import_textures_mode": "IMPORT_COPY",
        "import_textures_dir": texture_dir,
        "tex_name_collision_mode": "OVERWRITE",
    }


def read_live_metadata(local_path: str) -> dict | None:
    """Return the live-sync metadata dict from a local USD file, or None.

    Returns None if the file can't be opened, has no metadata block, or the
    block is not marked ``live``.
    """
    from pxr import Sdf

    layer = Sdf.Layer.FindOrOpen(local_path)
    if layer is None:
        return None
    meta = layer.customLayerData.get(LIVE_METADATA_KEY)
    if not meta or not meta.get("live"):
        return None
    # Copy out of the Vt dictionary into plain Python types.
    return {
        "live": bool(meta.get("live")),
        "host": str(meta.get("host", "")),
        "port": int(meta.get("port", 0)),
        "protocol_version": int(meta.get("protocol_version", 0)),
        "scene_id": str(meta.get("scene_id", "")),
        "snapshot_seq": int(meta.get("snapshot_seq", 0)),
        "epoch": int(meta.get("epoch", 0)),
        "vfs_url": str(meta.get("vfs_url", "")),
        "department": str(meta.get("department", "")),
        "requires_token": bool(meta.get("requires_token", False)),
        "composition_preserving": bool(meta.get("composition_preserving", False)),
        "flattened_fallback": str(meta.get("flattened_fallback", "")),
        "manifest": str(meta.get("manifest", "")),
    }


def resolve_import_source(src: str) -> tuple[str, dict | None]:
    """Resolve an import source to (local_path, live_metadata_or_None).

    *src* may be a local file path or an ``http(s)://`` URL. Remote sources
    are fetched to a temp file. The returned local path is what Blender's USD
    importer should open; the metadata (if any) drives live sync setup.
    """
    local_path = fetch_to_temp(src) if is_remote(src) else src
    meta = read_live_metadata(local_path)
    if is_remote(src) and meta and meta.get("composition_preserving"):
        fallback = meta.get("flattened_fallback")
        if fallback:
            local_path = fetch_to_temp(fallback)
            meta = read_live_metadata(local_path)
    return local_path, meta
