"""FLIP-based image comparison for the visual-regression harness.

Wraps NVIDIA FLIP (``flip-evaluator``) into a mean + 99th-percentile error
summary and writes a magma error-map PNG for triage. The PNG writer is
stdlib-only (zlib), so the harness adds no image-IO dependency beyond FLIP.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FlipResult:
    """Outcome of a FLIP comparison. ``mean``/``p99`` are FLIP error in [0, 1]."""

    mean: float
    p99: float
    error_map_path: str | None = None


def compare(reference_path: str, test_path: str, *, error_map_path: str | None = None,
            dynamic_range: str = "LDR") -> FlipResult:
    """FLIP-compare two images; optionally write a magma error map.

    Returns mean FLIP error plus the 99th percentile, a tail guard that catches a
    small but badly-wrong region that a low mean alone would hide.
    """
    import flip_evaluator as flip
    import numpy as np

    raw, mean, _ = flip.evaluate(reference_path, test_path, dynamic_range,
                                 applyMagma=False, computeMeanError=True)
    p99 = float(np.percentile(np.asarray(raw), 99))

    if error_map_path:
        magma, _, _ = flip.evaluate(reference_path, test_path, dynamic_range,
                                    applyMagma=True, computeMeanError=False)
        _write_rgb_png(np.asarray(magma), error_map_path)

    return FlipResult(mean=float(mean), p99=p99, error_map_path=error_map_path)


def _write_rgb_png(rgb_float, path: str) -> None:
    """Write an HxWx3 float-[0,1] array as an 8-bit RGB PNG using only stdlib."""
    import struct
    import zlib

    import numpy as np

    u8 = np.clip(np.asarray(rgb_float) * 255.0 + 0.5, 0, 255).astype(np.uint8)
    h, w, _ = u8.shape
    # PNG scanlines: a zero filter byte prepended to each row of RGB bytes.
    scanlines = np.hstack([np.zeros((h, 1), np.uint8), u8.reshape(h, w * 3)])

    def _chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # 8-bit, RGB (color type 2)
    png = (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
           + _chunk(b"IDAT", zlib.compress(scanlines.tobytes(), 9)) + _chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)
