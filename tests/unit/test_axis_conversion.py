"""Tests for openusdconnect.axis_conversion — Y-up ↔ Z-up basis change."""

from __future__ import annotations

import math

from openusdconnect.axis_conversion import (
    compose_axis_rotation,
    needs_conversion,
    strip_axis_rotation,
    yup_to_zup_quat,
    yup_to_zup_scale,
    yup_to_zup_vec,
    zup_to_yup_quat,
    zup_to_yup_scale,
    zup_to_yup_vec,
)

# Tolerance for floating-point comparisons
_EPS = 1e-9


def _assert_vec_close(a, b, tol=_EPS):
    assert len(a) == len(b)
    for i in range(len(a)):
        assert abs(a[i] - b[i]) < tol, f"index {i}: {a[i]} != {b[i]}"


# ---------------------------------------------------------------------------
# needs_conversion
# ---------------------------------------------------------------------------

def test_needs_conversion_y():
    assert needs_conversion("Y") is True
    assert needs_conversion("y") is True


def test_needs_conversion_z():
    assert needs_conversion("Z") is False
    assert needs_conversion("z") is False


# ---------------------------------------------------------------------------
# Translation round-trip
# ---------------------------------------------------------------------------

def test_vec_roundtrip_identity():
    """yup→zup→yup should give back the original vector."""
    v = (3.0, 15.0, -7.0)
    _assert_vec_close(zup_to_yup_vec(*yup_to_zup_vec(*v)), v)


def test_vec_roundtrip_reverse():
    """zup→yup→zup should give back the original vector."""
    v = (1.0, -2.0, 5.0)
    _assert_vec_close(yup_to_zup_vec(*zup_to_yup_vec(*v)), v)


def test_vec_known_values():
    """Verify specific conversion values from the test scene."""
    # Y-up (0, 10, 0) = 10 units up → Z-up (0, 0, 10)
    _assert_vec_close(yup_to_zup_vec(0, 10, 0), (0, 0, 10))

    # Y-up (2, 0, 0) = 2 units right → Z-up (2, 0, 0) — X unchanged
    _assert_vec_close(yup_to_zup_vec(2, 0, 0), (2, 0, 0))

    # Y-up (0, 0, 2) = 2 units depth → Z-up (0, -2, 0)
    _assert_vec_close(yup_to_zup_vec(0, 0, 2), (0, -2, 0))

    # Y-up (3, 5, 0) → Z-up (3, 0, 5)
    _assert_vec_close(yup_to_zup_vec(3, 5, 0), (3, 0, 5))


def test_vec_inverse_known_values():
    """Verify Z-up → Y-up conversion."""
    _assert_vec_close(zup_to_yup_vec(0, 0, 10), (0, 10, 0))
    _assert_vec_close(zup_to_yup_vec(0, -2, 0), (0, 0, 2))


# ---------------------------------------------------------------------------
# Quaternion round-trip
# ---------------------------------------------------------------------------

def test_quat_roundtrip():
    """yup→zup→yup should give back the original quaternion."""
    # Ry(45°) in Y-up
    c = math.cos(math.radians(22.5))
    s = math.sin(math.radians(22.5))
    q = (c, 0, s, 0)
    result = zup_to_yup_quat(*yup_to_zup_quat(*q))
    _assert_vec_close(result, q)


def test_quat_identity_stays_identity():
    """Identity quaternion should remain identity after conversion."""
    _assert_vec_close(yup_to_zup_quat(1, 0, 0, 0), (1, 0, 0, 0))
    _assert_vec_close(zup_to_yup_quat(1, 0, 0, 0), (1, 0, 0, 0))


def test_quat_ry_becomes_rz():
    """Ry(45°) in Y-up should become Rz(45°) in Z-up.

    Y is the up axis in Y-up; Z is the up axis in Z-up. Rotating around
    the up axis should map to the same physical rotation.
    """
    angle = math.radians(45)
    c = math.cos(angle / 2)
    s = math.sin(angle / 2)

    q_ry = (c, 0, s, 0)         # Ry(45°) in Y-up
    q_rz_expected = (c, 0, 0, s)  # Rz(45°) in Z-up

    result = yup_to_zup_quat(*q_ry)
    _assert_vec_close(result, q_rz_expected)


def test_quat_rx_stays_rx():
    """Rx(θ) should remain Rx(θ) — the X axis is the same in both systems."""
    angle = math.radians(30)
    c = math.cos(angle / 2)
    s = math.sin(angle / 2)

    q_rx = (c, s, 0, 0)
    result = yup_to_zup_quat(*q_rx)
    _assert_vec_close(result, q_rx)


def test_quat_rz_becomes_ry_negated():
    """Rz(θ) in Y-up (around depth) should become Ry(-θ) in Z-up.

    Z in Y-up is "toward viewer" (+Z). Y in Z-up is "into screen" (-Y).
    So rotating +θ around Y-up Z becomes -θ around Z-up Y.
    """
    angle = math.radians(60)
    c = math.cos(angle / 2)
    s = math.sin(angle / 2)

    q_rz_yup = (c, 0, 0, s)  # Rz(60°) in Y-up

    # Expected: Ry(-60°) in Z-up = (cos(-30°), 0, sin(-30°), 0) = (cos30, 0, -sin30, 0)
    c_neg = math.cos(angle / 2)  # cos is even
    s_neg = -math.sin(angle / 2)
    q_ry_neg = (c_neg, 0, s_neg, 0)

    result = yup_to_zup_quat(*q_rz_yup)
    _assert_vec_close(result, q_ry_neg)


# ---------------------------------------------------------------------------
# Scale
# ---------------------------------------------------------------------------

def test_scale_swap():
    """Y-up scale (1, 2, 3) → Z-up (1, 3, 2) — swap Y and Z."""
    assert yup_to_zup_scale(1, 2, 3) == (1, 3, 2)
    assert zup_to_yup_scale(1, 3, 2) == (1, 2, 3)


def test_scale_roundtrip():
    s = (1.5, 2.0, 0.5)
    assert zup_to_yup_scale(*yup_to_zup_scale(*s)) == s


def test_scale_uniform_unchanged():
    """Uniform scale should be unchanged by conversion."""
    assert yup_to_zup_scale(3, 3, 3) == (3, 3, 3)


# ---------------------------------------------------------------------------
# Hierarchy world-position consistency
# ---------------------------------------------------------------------------

def _mat4_from_trs(t, q, s):
    """Build a 4×4 transform matrix from translation + quaternion + scale.

    Quaternion q is (w, x, y, z). Returns nested tuples (row-major).
    """
    w, x, y, z = q
    x2, y2, z2 = x * 2, y * 2, z * 2
    xx, xy, xz = x * x2, x * y2, x * z2
    yy, yz, zz = y * y2, y * z2, z * z2
    wx, wy, wz = w * x2, w * y2, w * z2

    # Rotation matrix columns (pre-scale)
    r = [
        [(1 - (yy + zz)) * s[0], (xy + wz) * s[0], (xz - wy) * s[0]],
        [(xy - wz) * s[1], (1 - (xx + zz)) * s[1], (yz + wx) * s[1]],
        [(xz + wy) * s[2], (yz - wx) * s[2], (1 - (xx + yy)) * s[2]],
    ]
    return (
        (r[0][0], r[1][0], r[2][0], t[0]),
        (r[0][1], r[1][1], r[2][1], t[1]),
        (r[0][2], r[1][2], r[2][2], t[2]),
        (0.0, 0.0, 0.0, 1.0),
    )


def _mat4_mul(a, b):
    """Multiply two 4×4 matrices (row-major nested tuples)."""
    result = []
    for i in range(4):
        row = []
        for j in range(4):
            row.append(sum(a[i][k] * b[k][j] for k in range(4)))
        result.append(tuple(row))
    return tuple(result)


def _mat4_pos(m):
    """Extract translation from a 4×4 row-major matrix."""
    return (m[0][3], m[1][3], m[2][3])


def test_hierarchy_world_position():
    """Parent(converted) @ child(converted) gives correct Z-up world pos.

    Y-up scene:
      /World: translate (0, 10, 0)   — 10 units up
      /Teapot: local translate (3, 5, 0) — 3 right, 5 up from parent

    Expected Z-up world position of Teapot: (3, 0, 15)
    """
    identity_q = (1.0, 0.0, 0.0, 0.0)
    unit_s = (1.0, 1.0, 1.0)

    # Y-up transforms
    parent_t_yup = (0.0, 10.0, 0.0)
    child_t_yup = (3.0, 5.0, 0.0)

    # Convert each prim's TRS
    parent_t_zup = yup_to_zup_vec(*parent_t_yup)
    parent_q_zup = yup_to_zup_quat(*identity_q)
    parent_s_zup = yup_to_zup_scale(*unit_s)

    child_t_zup = yup_to_zup_vec(*child_t_yup)
    child_q_zup = yup_to_zup_quat(*identity_q)
    child_s_zup = yup_to_zup_scale(*unit_s)

    parent_mat = _mat4_from_trs(parent_t_zup, parent_q_zup, parent_s_zup)
    child_mat = _mat4_from_trs(child_t_zup, child_q_zup, child_s_zup)
    world = _mat4_mul(parent_mat, child_mat)

    _assert_vec_close(_mat4_pos(world), (3.0, 0.0, 15.0))


def test_hierarchy_with_rotation():
    """Parent has Ry(90°) in Y-up. Child at local (1, 0, 0).

    In Y-up, Ry(90°) rotates around the up axis (Y).
    After parent rotation, child at (1,0,0) becomes (0,0,-1) in parent-world Y-up.
    Parent is at (0, 5, 0).  World Y-up = (0, 5, -1).
    Converted to Z-up: (0, 1, 5).
    """
    # Parent: Ry(90°) at (0, 5, 0)
    angle = math.radians(90)
    c = math.cos(angle / 2)
    s = math.sin(angle / 2)
    parent_q_yup = (c, 0, s, 0)
    parent_t_yup = (0.0, 5.0, 0.0)

    # Child: identity rotation at (1, 0, 0)
    child_q_yup = (1.0, 0.0, 0.0, 0.0)
    child_t_yup = (1.0, 0.0, 0.0)

    unit_s = (1.0, 1.0, 1.0)

    parent_mat = _mat4_from_trs(
        yup_to_zup_vec(*parent_t_yup),
        yup_to_zup_quat(*parent_q_yup),
        yup_to_zup_scale(*unit_s),
    )
    child_mat = _mat4_from_trs(
        yup_to_zup_vec(*child_t_yup),
        yup_to_zup_quat(*child_q_yup),
        yup_to_zup_scale(*unit_s),
    )
    world = _mat4_mul(parent_mat, child_mat)

    _assert_vec_close(_mat4_pos(world), (0.0, 1.0, 5.0))


# ---------------------------------------------------------------------------
# compose_axis_rotation / strip_axis_rotation
# ---------------------------------------------------------------------------

def test_strip_then_compose_roundtrip():
    """strip → compose gives back the original quaternion."""
    # Rx(90°) @ Rz(45°) — a typical imported object rotation
    angle = math.radians(45)
    c = math.cos(angle / 2)
    s = math.sin(angle / 2)
    q_rz = (c, 0, 0, s)

    # Compose: Rx(90°) * Rz(45°)
    composed = compose_axis_rotation(*q_rz)
    # Strip: Rx(-90°) * composed = Rz(45°)
    stripped = strip_axis_rotation(*composed)
    _assert_vec_close(stripped, q_rz)


def test_compose_then_strip_roundtrip():
    """compose → strip gives back the original quaternion."""
    q = (0.5, 0.5, 0.5, 0.5)  # arbitrary unit quaternion
    composed = compose_axis_rotation(*q)
    stripped = strip_axis_rotation(*composed)
    _assert_vec_close(stripped, q)


def test_strip_identity_gives_rx_neg90():
    """Stripping from identity gives Rx(-90°).

    This is what happens when the emitter processes an imported root
    that still has Rx(90°) — strip recovers identity for USD.
    But if the root's Rx(90°) was already removed (parent handles axis),
    stripping from identity gives Rx(-90°) — the emitter must not do this.
    """
    rx_neg90 = strip_axis_rotation(1, 0, 0, 0)
    # Rx(-90°) quaternion = (cos(-45°), sin(-45°), 0, 0) = (√2/2, -√2/2, 0, 0)
    expected = (math.sqrt(2) / 2, -math.sqrt(2) / 2, 0, 0)
    _assert_vec_close(rx_neg90, expected)


def test_strip_rx90_gives_identity():
    """Stripping Rx(90°) recovers identity — the correct USD rotation."""
    rx90 = (math.sqrt(2) / 2, math.sqrt(2) / 2, 0, 0)
    result = strip_axis_rotation(*rx90)
    _assert_vec_close(result, (1, 0, 0, 0))


def test_compose_identity_gives_rx90():
    """Composing onto identity gives Rx(90°) — adds axis conversion."""
    result = compose_axis_rotation(1, 0, 0, 0)
    expected = (math.sqrt(2) / 2, math.sqrt(2) / 2, 0, 0)
    _assert_vec_close(result, expected)


def test_strip_preserves_user_rotation():
    """User rotates teapot 45° around Z on top of Rx(90°).

    Blender rotation = Rx(90°) @ Rz(45°).
    Strip should recover Rz(45°) for USD.
    """
    angle = math.radians(45)
    c45 = math.cos(angle / 2)
    s45 = math.sin(angle / 2)

    # Rx(90°) quaternion
    a = math.sqrt(2) / 2
    q_rx90 = (a, a, 0, 0)
    q_rz45 = (c45, 0, 0, s45)

    # Compose Rx(90°) @ Rz(45°) via quaternion multiply
    from openusdconnect.axis_conversion import _quat_mul

    blender_rot = _quat_mul(q_rx90, q_rz45)

    # Strip should give back Rz(45°)
    stripped = strip_axis_rotation(*blender_rot)
    _assert_vec_close(stripped, q_rz45)


# ---------------------------------------------------------------------------
# Emitter/receiver axis skip for parent chain with axis rotation
# ---------------------------------------------------------------------------

def test_parent_with_rx90_child_passthrough():
    """When parent has Rx(90°), child local values are already Y-up.

    Scene: World[Rx(90°)] → Teapot[identity] → geo[identity]
    User moves geo to local (0, 2, 0) in Blender.
    Since parent chain has Rx(90°), the value (0, 2, 0) is already Y-up
    (local Y = world Z = up).  The emitter should NOT convert it.

    If incorrectly converted Z→Y: (0, 2, 0) → (0, 0, -2) — WRONG.
    """
    # The local value is (0, 2, 0) in parent's rotated frame
    local_val = (0.0, 2.0, 0.0)

    # Incorrect: treating as Z-up and converting
    wrong = zup_to_yup_vec(*local_val)
    assert wrong == (0.0, 0.0, -2.0), "Should be the wrong conversion"

    # Correct: pass through (parent handles axis)
    correct = local_val
    assert correct == (0.0, 2.0, 0.0)

    # Verify: the Y-up value (0, 2, 0) converted by receiver → (0, 0, 2) Z-up
    # which in the parent's rotated frame with Rx(90°) maps local Y→world Z.
    # So the receiver should also pass through when parent handles axis.


def test_no_double_rx90():
    """World[Rx(90°)] → merged_teapot should have identity rotation.

    If both World and teapot have Rx(90°), the total is Rx(180°) which
    makes geometry lie on its side.  After merge, the teapot's Rx(90°)
    must be stripped when the parent already handles axis conversion.
    """
    # Rx(90°) @ Rx(90°) = Rx(180°) — the bug
    a = math.sqrt(2) / 2
    q_rx90 = (a, a, 0, 0)
    from openusdconnect.axis_conversion import _quat_mul

    q_rx180 = _quat_mul(q_rx90, q_rx90)
    # Rx(180°) = (0, 1, 0, 0)
    _assert_vec_close(q_rx180, (0, 1, 0, 0))

    # After fix: parent has Rx(90°), child has identity
    # Total rotation applied to geometry = Rx(90°) — correct
    identity = (1.0, 0.0, 0.0, 0.0)
    total = _quat_mul(q_rx90, identity)
    _assert_vec_close(total, q_rx90)


def test_axis_conversion_ancestry_uses_import_provenance_not_rotation():
    """An authored rotated parent is not Blender's display-basis node."""
    from integrations.blender.blender_adapter import (
        _PROP_USD_AXIS_CONVERSION,
        _has_axis_rotation,
    )

    class Obj(dict):
        def __init__(self, parent=None):
            super().__init__()
            self.parent = parent
            self.original = self

    authored_rotated_parent = Obj()
    child = Obj(authored_rotated_parent)
    assert not _has_axis_rotation(child)

    authored_rotated_parent[_PROP_USD_AXIS_CONVERSION] = True
    assert _has_axis_rotation(child)
