"""Shared session-sublayer composition helpers."""

from __future__ import annotations

from collections.abc import Sequence, Set

from pxr import Sdf


def replace_managed_sublayers(
    session: Sdf.Layer,
    managed_paths: Sequence[str],
    managed_identifiers: Set[str],
) -> None:
    """Install one managed block without disturbing unrelated sublayers."""
    current_paths = list(session.subLayerPaths)
    current_offsets = list(session.subLayerOffsets)
    preserved = [
        (path, current_offsets[index])
        for index, path in enumerate(current_paths)
        if path not in managed_identifiers
    ]
    new_paths = list(managed_paths)
    new_paths.extend(path for path, _offset in preserved)
    if new_paths == current_paths and all(
        current_offsets[index] == Sdf.LayerOffset()
        for index in range(len(managed_paths))
    ):
        return

    with Sdf.ChangeBlock():
        session.subLayerPaths.clear()
        for path in new_paths:
            session.subLayerPaths.append(path)
        for index, (_path, offset) in enumerate(
            preserved,
            start=len(managed_paths),
        ):
            session.subLayerOffsets[index] = offset


__all__ = ["replace_managed_sublayers"]
