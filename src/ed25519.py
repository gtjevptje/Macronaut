"""Ed25519 signature verification, in pure Python, with no dependencies.

This exists to answer exactly one question at runtime: *did the holder of
Macronaut's private key sign this licence?* Nothing else. There is no signing
here — the signer is not part of the program.

## Why hand-rolled rather than `cryptography`

Three reasons, in order of weight:

1. **Build size.** `cryptography` is ~8 MB of wheel plus a PyInstaller hook and a
   bundled OpenSSL. It took a 352 MB → 105.7 MB fight to get the
   download to a size that suits an app which updates weekly; adding a megabyte
   per licence check would be spending the winnings on nothing.
2. **No new failure mode in the frozen build.** Every dependency added so far
   has broken *once* inside PyInstaller and worked fine from source — OCR read
   nothing in the .exe, numpy was excluded while cv2 was bundled. Code with no
   imports beyond `hashlib` cannot fail that way.
3. **Verification has no secrets in it.** The usual argument against writing
   your own crypto is side channels leaking a key. There is no key here to
   leak: the public key is baked into the .exe and printed on the website. A
   timing attack against a public value recovers a public value.

What is genuinely risky about hand-rolled crypto is getting the *maths* subtly
wrong, so that some signatures verify and others don't — or worse, that forged
ones pass. That is settled by `tests/test_ed25519.py`, which runs the official
RFC 8032 §7.1 test vectors plus the corruption cases. If those pass, this is
Ed25519; if they ever stop passing, this file is wrong and the licence gate is
worthless. Do not weaken that test.

## What is implemented

RFC 8032 Ed25519 (PureEdDSA over edwards25519, SHA-512). Verification only,
using the standard extended-coordinate formulas (Hisil–Wong–Carter–Dawson) so a
check costs milliseconds rather than the seconds a naïve affine implementation
takes. It runs once per app launch, so even that would have been survivable —
but the signer reuses these primitives to *generate* keys, and
keygen does the same scalar multiplication.

Deliberately strict where RFC 8032 allows a choice: non-canonical point
encodings (y ≥ p) and out-of-range scalars (S ≥ L) are rejected rather than
reduced. A licence key is not a network protocol that has to interoperate with
someone else's encoder — the only thing on earth that produces these is
Macronaut's own signer — so there is no compatibility to buy by being lenient, and
malleability is exactly what a forger would go looking for.
"""
from __future__ import annotations

import hashlib

# ── The curve ─────────────────────────────────────────────────────────────────
# edwards25519: -x² + y² = 1 + d·x²·y² over GF(2²⁵⁵ - 19).
P = 2 ** 255 - 19
# Order of the base point's prime-order subgroup.
L = 2 ** 252 + 27742317777372353535851937790883648493
D = -121665 * pow(121666, P - 2, P) % P
# A square root of -1, used to pick the right branch when recovering x.
SQRT_M1 = pow(2, (P - 1) // 4, P)

# The base point B, as constants rather than derived, so a bug in _recover_x
# cannot quietly relocate the whole curve and still look self-consistent.
_BY = 4 * pow(5, P - 2, P) % P
_BX = 15112221349535400772501151409588531511454012693041857206046113283949847762202

# Points are (X, Y, Z, T) in extended coordinates, where x = X/Z, y = Y/Z and
# T = X·Y/Z. The redundant T is what makes addition cost no inversions.
B = (_BX, _BY, 1, _BX * _BY % P)
IDENTITY = (0, 1, 1, 0)


def _add(p, q):
    """Add two points. add-2008-hwcd-3, valid for every input pair (a = -1)."""
    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = q
    a = (y1 - x1) * (y2 - x2) % P
    b = (y1 + x1) * (y2 + x2) % P
    c = 2 * t1 * t2 * D % P
    dd = 2 * z1 * z2 % P
    e, f, g, h = b - a, dd - c, dd + c, b + a
    return (e * f % P, g * h % P, f * g % P, e * h % P)


def _double(p):
    """Double a point. dbl-2008-hwcd — cheaper than _add(p, p), and the inner
    loop of every scalar multiplication, so it is worth the separate formula."""
    x1, y1, z1, _ = p
    a = x1 * x1 % P
    b = y1 * y1 % P
    c = 2 * z1 * z1 % P
    h = (a + b) % P
    e = (h - (x1 + y1) * (x1 + y1)) % P
    g = (a - b) % P
    f = (c + g) % P
    return (e * f % P, g * h % P, f * g % P, e * h % P)


def _mul(p, s: int):
    """[s]p, by double-and-add over the bits of s, low to high.

    Not constant-time. It does not need to be: every scalar this is called with
    here is public (a signature's S, a hash, or — in the minting tool, on the
    developer's own machine — a freshly generated secret that never travels).
    """
    r = IDENTITY
    while s > 0:
        if s & 1:
            r = _add(r, p)
        p = _double(p)
        s >>= 1
    return r


def _equal(p, q) -> bool:
    """Projective equality: X1·Z2 == X2·Z1 and Y1·Z2 == Y2·Z1.

    ⚠ Comparing the tuples directly is wrong and is the classic bug here — the
    same point has infinitely many (X, Y, Z) representations, and the two sides
    of a verification arrive by different routes, so they are essentially never
    numerically equal even when the signature is perfectly valid.
    """
    x1, y1, z1, _ = p
    x2, y2, z2, _ = q
    return (x1 * z2 - x2 * z1) % P == 0 and (y1 * z2 - y2 * z1) % P == 0


def _recover_x(y: int, sign: int):
    """The x coordinate matching y, with the requested low bit. None if there
    is no such point — which is most 32-byte strings, and the reason a random
    'key' does not decode into something that can be reasoned about."""
    if y >= P:
        return None                       # non-canonical encoding, rejected
    x2 = (y * y - 1) * pow(D * y * y + 1, P - 2, P) % P
    if x2 == 0:
        # x = 0 is only a point at all if the sign bit agrees; a signature
        # claiming the other sign for it is malformed.
        return None if sign else 0
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P != 0:
        x = x * SQRT_M1 % P
    if (x * x - x2) % P != 0:
        return None                       # y was not on the curve
    if x & 1 != sign:
        x = P - x
    return x


def _decode_point(b: bytes):
    """A 32-byte compressed point → extended coordinates, or None if it is not
    a valid encoding of a curve point."""
    if len(b) != 32:
        return None
    n = int.from_bytes(b, "little")
    sign = n >> 255
    y = n & ((1 << 255) - 1)
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % P)


def verify(signature: bytes, message: bytes, public_key: bytes) -> bool:
    """True if `signature` is a valid Ed25519 signature over `message`.

    Never raises. Every malformed input — wrong length, a point that is not on
    the curve, an out-of-range scalar — is a False, because every caller wants
    the same answer for "forged" and "corrupt": not licensed.
    """
    if len(signature) != 64 or len(public_key) != 32:
        return False
    a = _decode_point(public_key)
    if a is None:
        return False
    r = _decode_point(signature[:32])
    if r is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= L:
        return False                      # malleable / non-canonical scalar
    h = int.from_bytes(
        hashlib.sha512(signature[:32] + public_key + message).digest(), "little"
    ) % L
    # [s]B == R + [h]A
    return _equal(_mul(B, s), _add(r, _mul(a, h)))


def encode_point(p) -> bytes:
    """Extended coordinates → the 32-byte compressed form.

    The one piece of the *writing* side kept here rather than in
    the signer: it is curve arithmetic, it belongs beside the
    decoder it must round-trip with, and `test_ed25519.py` can only check that
    round trip if both halves are in one module. The tool imports it to derive
    a public key from a fresh secret and to emit R; the signing itself — the
    only part that touches a private scalar — stays in the tool and never ships.
    """
    x, y, z, _ = p
    zi = pow(z, P - 2, P)
    x, y = x * zi % P, y * zi % P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")
