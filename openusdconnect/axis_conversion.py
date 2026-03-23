"""Axis conversion utilities for Y-up ↔ Z-up coordinate systems.

Two kinds of rotation operation, each for a different situation:

**Basis-change** (yup_to_zup_quat / zup_to_yup_quat):
  Q' = Conv · Q · Conv⁻¹  — used for normal objects whose local TRS
  is being converted between coordinate systems uniformly.

**Compose / strip** (compose_axis_rotation / strip_axis_rotation):
  Q' = Conv · Q  (or Conv⁻¹ · Q) — used for DCC-imported asset roots
  that carry an explicit Rx(90°) for geometry display.  The emitter
  strips it to recover the USD rotation; the receiver composes it back.

Conversion formulas (derived from Conv = Rx(90°)):
  Translation:  (x, y, z)_Yup → (x, -z, y)_Zup
  Rotation:     Q' = Q_conv · Q · Q_conv⁻¹   (quaternion conjugation)
  Scale:        (sx, sy, sz)  → (sx, sz, sy)   (swap Y ↔ Z)
"""

from __future__ import annotations

import math

# Quaternion for Rx(+90°) = (cos 45°, sin 45°, 0, 0)
_HALF_SQRT2 = math.sqrt(2) / 2.0
_Q_YUP_TO_ZUP = (_HALF_SQRT2, _HALF_SQRT2, 0.0, 0.0)  # [w, x, y, z]
_Q_ZUP_TO_YUP = (_HALF_SQRT2, -_HALF_SQRT2, 0.0, 0.0)  # Rx(-90°)


def _quat_mul(a: tuple, b: tuple) -> tuple:
    """Hamilton product of two quaternions in (w, x, y, z) order."""
    w1, x1, y1, z1 = a
    w2, x2, y2, z2 = b
    return (
        w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
        w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
        w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
        w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
    )


def _quat_conjugate(conv: tuple, q: tuple) -> tuple:
    """Basis-change a quaternion: conv · q · conv⁻¹.

    *conv* must be a unit quaternion.
    """
    conv_inv = (conv[0], -conv[1], -conv[2], -conv[3])
    return _quat_mul(_quat_mul(conv, q), conv_inv)


# -- public API ---------------------------------------------------------------

def needs_conversion(up_axis: str) -> bool:
    """Return True when the stage upAxis requires Y-up ↔ Z-up conversion."""
    return str(up_axis).upper() == "Y"


def yup_to_zup_vec(x: float, y: float, z: float) -> tuple[float, float, float]:
    """Y-up translation → Z-up: Rx(90°) applied to (x, y, z)."""
    return (x, -z, y)


def zup_to_yup_vec(x: float, y: float, z: float) -> tuple[float, float, float]:
    """Z-up translation → Y-up: Rx(-90°) applied to (x, y, z)."""
    return (x, z, -y)


def yup_to_zup_quat(w: float, x: float, y: float, z: float) -> tuple[float, float, float, float]:
    """Y-up quaternion → Z-up via basis change (Q_conv · Q · Q_conv⁻¹)."""
    return _quat_conjugate(_Q_YUP_TO_ZUP, (w, x, y, z))


def zup_to_yup_quat(w: float, x: float, y: float, z: float) -> tuple[float, float, float, float]:
    """Z-up quaternion → Y-up via basis change (Q_conv⁻¹ · Q · Q_conv)."""
    return _quat_conjugate(_Q_ZUP_TO_YUP, (w, x, y, z))


def yup_to_zup_scale(sx: float, sy: float, sz: float) -> tuple[float, float, float]:
    """Y-up scale → Z-up: swap Y and Z components."""
    return (sx, sz, sy)


def zup_to_yup_scale(sx: float, sy: float, sz: float) -> tuple[float, float, float]:
    """Z-up scale → Y-up: swap Y and Z components."""
    return (sx, sz, sy)


def compose_axis_rotation(w: float, x: float, y: float, z: float) -> tuple[float, float, float, float]:
    """Prepend Rx(90°) to a quaternion: Rx(90°) · Q.

    Adds the Y-up → Z-up axis-conversion rotation to a USD rotation
    so it displays correctly in a Z-up DCC.
    """
    return _quat_mul(_Q_YUP_TO_ZUP, (w, x, y, z))


def strip_axis_rotation(w: float, x: float, y: float, z: float) -> tuple[float, float, float, float]:
    """Prepend Rx(-90°) to a quaternion: Rx(-90°) · Q.

    Removes the Y-up → Z-up axis-conversion rotation from a Z-up DCC
    rotation to recover the original USD rotation.
    """
    return _quat_mul(_Q_ZUP_TO_YUP, (w, x, y, z))
