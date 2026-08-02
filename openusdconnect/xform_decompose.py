"""Matrix decompose helpers shared by the emitter's xform paths.

The wire format carries every animated transform as canonical TRS regardless
of how the source authored its op stack, so both the default-time snapshot
and the sampled-non-canonical-op path need a 4x4-to-(translate, quat, scale)
decompose. The scalar path uses ``Gf.Transform`` for one-off prims; the
batched path uses a vectorized Shepperd extraction for animated stacks where
the per-call ``Gf`` overhead dominates.
"""

from __future__ import annotations

import numpy as np
from pxr import Gf


def as_matrix(ret):
    """Handle USD binding variants: matrix or (matrix, resets...) tuple."""
    return ret[0] if isinstance(ret, tuple) else ret


def decompose_trs_from_matrix(m: Gf.Matrix4d):
    """Decompose a 4x4 matrix into translation, quaternion rotation, and scale.

    Returns:
        (t, r, s) where:
        - t = [x, y, z] translation
        - r = [w, x, y, z] quaternion rotation
        - s = [x, y, z] scale
    """
    tr = Gf.Transform()
    tr.SetMatrix(m)
    t = Gf.Vec3d(tr.GetTranslation())
    rot = tr.GetRotation()
    s = Gf.Vec3d(tr.GetScale())

    qd = rot.GetQuat()
    w = float(qd.GetReal())
    iv = qd.GetImaginary()
    x, y, z = float(iv[0]), float(iv[1]), float(iv[2])

    return (
        [float(t[0]), float(t[1]), float(t[2])],
        [w, x, y, z],
        [float(s[0]), float(s[1]), float(s[2])],
    )


def decompose_trs_batch(mats):
    """Vectorized TRS decompose of ``(N, 4, 4)`` row-vector matrices.

    Per-row contract matches ``decompose_trs_from_matrix``: translation,
    quaternion ``[w, x, y, z]``, scale, with reflections absorbed into the
    x scale. Shear and zero-scale rows are unrepresentable in TRS here and
    in the Gf path alike (the wire format carries no scale orientation).

    Algorithm: scale comes from row norms (in row-vector convention each
    row of the upper 3x3 is a basis vector after rotation, so its length is
    the scale on that axis), reflections fold into a negative x scale via
    the determinant sign, and the residual rotation matrix runs through
    Shepperd's four-candidate quaternion extraction. Shepperd evaluates
    four candidate components (w, x, y, z), picks the largest per row,
    and reconstructs the other three from that component's off-diagonal
    expressions; selecting the largest avoids dividing by a near-zero
    term near rotation singularities.
    """
    # Translation: bottom row of the affine matrix.
    t = mats[:, 3, :3]

    # Scale: row norms of the upper 3x3; flip x for reflections.
    m3 = mats[:, :3, :3]
    s = np.linalg.norm(m3, axis=2)
    s[np.linalg.det(m3) < 0, 0] *= -1
    # Zero-scale rows leave rotation undefined; divide by 1 to avoid
    # NaNs in the residual matrix (the rotation values are unused later
    # in those rows but must remain finite for the trace arithmetic).
    safe = np.where(np.abs(s) < 1e-12, 1.0, s)
    r = m3 / safe[:, :, None]
    R00, R01, R02 = r[:, 0, 0], r[:, 0, 1], r[:, 0, 2]
    R10, R11, R12 = r[:, 1, 0], r[:, 1, 1], r[:, 1, 2]
    R20, R21, R22 = r[:, 2, 0], r[:, 2, 1], r[:, 2, 2]

    # Candidate magnitudes for w, x, y, z. Each row's largest candidate
    # is numerically safest to use as the divisor for the other three.
    tr = R00 + R11 + R22
    c0 = np.sqrt(np.maximum(1.0 + tr, 0.0)) / 2.0
    c1 = np.sqrt(np.maximum(1.0 + R00 - R11 - R22, 0.0)) / 2.0
    c2 = np.sqrt(np.maximum(1.0 + R11 - R00 - R22, 0.0)) / 2.0
    c3 = np.sqrt(np.maximum(1.0 + R22 - R00 - R11, 0.0)) / 2.0
    sel = np.argmax(np.stack([c0, c1, c2, c3]), axis=0)

    q = np.empty((len(mats), 4))
    # Branch 0: w is the largest; x, y, z come from antisymmetric pairs.
    m = sel == 0
    d = 4.0 * c0[m]
    q[m, 0] = c0[m]
    q[m, 1] = (R12[m] - R21[m]) / d
    q[m, 2] = (R20[m] - R02[m]) / d
    q[m, 3] = (R01[m] - R10[m]) / d
    # Branch 1: x is largest; w from yz-pair, y/z from symmetric pairs.
    m = sel == 1
    d = 4.0 * c1[m]
    q[m, 0] = (R12[m] - R21[m]) / d
    q[m, 1] = c1[m]
    q[m, 2] = (R10[m] + R01[m]) / d
    q[m, 3] = (R20[m] + R02[m]) / d
    # Branch 2: y is largest.
    m = sel == 2
    d = 4.0 * c2[m]
    q[m, 0] = (R20[m] - R02[m]) / d
    q[m, 1] = (R10[m] + R01[m]) / d
    q[m, 2] = c2[m]
    q[m, 3] = (R12[m] + R21[m]) / d
    # Branch 3: z is largest.
    m = sel == 3
    d = 4.0 * c3[m]
    q[m, 0] = (R01[m] - R10[m]) / d
    q[m, 1] = (R20[m] + R02[m]) / d
    q[m, 2] = (R12[m] + R21[m]) / d
    q[m, 3] = c3[m]
    q /= np.linalg.norm(q, axis=1, keepdims=True)
    return t, q, s


def xform_sample_value(val):
    """Sample-diff value converter for transform attrs (matrices included).

    Generic value conversion intentionally drops ``Gf.Matrix4d``/``Matrix4f``
    to keep them out of the gprim scan, since matrices have no wire-typed
    encoding. The xform sample path needs them as a hashable fingerprint
    only (samples are folded through the matrix decompose at emit time),
    so this converter returns a numpy view for matrices and defers to the
    generic converter for everything else.
    """
    if isinstance(val, (Gf.Matrix4d, Gf.Matrix4f)):
        return np.array(val)
    from .usd_state import usd_value_to_python

    return usd_value_to_python(val)
