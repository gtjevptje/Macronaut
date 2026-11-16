"""The RFC 8032 test vectors, and the ways a forger would try to get past them.

`ed25519.py` is hand-rolled, which is only defensible because this file exists.
It is the whole argument that the licence gate is real: if these pass, that
module implements Ed25519 as specified, and a Pro unlock cannot be minted
without Macronaut's private key. If one ever fails, the gate is decorative.

⚠ Do not "fix" a failure here by relaxing the assertion. A verifier that
accepts a signature it should reject fails silently in production — every
forged key works, and nothing anywhere reports a problem.
"""
import hashlib

import pytest

import ed25519


def _h(s: str) -> bytes:
    return bytes.fromhex(s)


# RFC 8032 §7.1, verbatim: (public key, message, signature).
# The secret keys are in the RFC too and are deliberately not here — nothing in
# the shipped app signs anything, so there is nothing for them to test.
RFC_8032 = [
    (
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e0652249015"
        "55fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    ),
    (
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
        "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    ),
    (
        "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
        "af82",
        "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac"
        "18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a",
    ),
    (   # "SHA(abc)" — a 64-byte message, so the hash spans more than one block
        "ec172b93ad5e563bf4932c70e1245034c35467ef2efd4d64ebf819683467e2bf",
        "ddaf35a193617abacc417349ae20413112e6fa4e89a97ea20a9eeee64b55d39a"
        "2192992a274fc1a836ba3c23a3feebbd454d4423643ce80e2a9ac94fa54ca49f",
        "dc2a4459e7369633a52b1bf277839a00201009a3efbf3ecb69bea2186c26b589"
        "09351fc9ac90b3ecfdfbc7c66431e0303dca179c138ac17ad9bef1177331a704",
    ),
]


@pytest.mark.parametrize("pub,msg,sig", RFC_8032)
def test_rfc_8032_vectors_verify(pub, msg, sig):
    assert ed25519.verify(_h(sig), _h(msg), _h(pub)) is True


@pytest.mark.parametrize("pub,msg,sig", RFC_8032)
def test_a_flipped_message_bit_fails(pub, msg, sig):
    """The signature covers the message, not merely accompanies it."""
    m = bytearray(_h(msg)) or bytearray(b"\x00")
    m[0] ^= 0x01
    assert ed25519.verify(_h(sig), bytes(m), _h(pub)) is False


@pytest.mark.parametrize("pub,msg,sig", RFC_8032)
def test_a_flipped_signature_bit_fails(pub, msg, sig):
    for index in (0, 31, 32, 63):        # both halves, both ends: R and S
        s = bytearray(_h(sig))
        s[index] ^= 0x01
        assert ed25519.verify(bytes(s), _h(msg), _h(pub)) is False


@pytest.mark.parametrize("pub,msg,sig", RFC_8032)
def test_the_wrong_public_key_fails(pub, msg, sig):
    """The signature from one keypair must not verify under another — this is
    the property the entire licence scheme rests on."""
    other = RFC_8032[(RFC_8032.index((pub, msg, sig)) + 1) % len(RFC_8032)][0]
    assert ed25519.verify(_h(sig), _h(msg), _h(other)) is False


def test_wrong_lengths_are_rejected_not_raised():
    """A user pastes rubbish into the activation box; that is a False, not a
    traceback in front of someone who is trying to give us money."""
    pub, msg, sig = RFC_8032[1]
    assert ed25519.verify(b"", _h(msg), _h(pub)) is False
    assert ed25519.verify(_h(sig)[:63], _h(msg), _h(pub)) is False
    assert ed25519.verify(_h(sig) + b"\x00", _h(msg), _h(pub)) is False
    assert ed25519.verify(_h(sig), _h(msg), _h(pub)[:31]) is False
    assert ed25519.verify(_h(sig), _h(msg), b"\xff" * 32) is False


def test_an_out_of_range_scalar_is_rejected():
    """S must be reduced mod L. Accepting S + L as well would make every
    signature exist in two forms, which is free malleability for no benefit."""
    pub, msg, sig = RFC_8032[1]
    raw = _h(sig)
    s = int.from_bytes(raw[32:], "little")
    mangled = raw[:32] + (s + ed25519.L).to_bytes(32, "little")
    assert ed25519.verify(mangled, _h(msg), _h(pub)) is False


def test_a_non_canonical_y_is_rejected():
    """y ≥ p re-encodes an existing point. Reducing it instead of refusing it
    is how implementations end up with two valid encodings of one key."""
    # p itself encodes y = 0 once reduced; a strict decoder returns None.
    assert ed25519._decode_point(ed25519.P.to_bytes(32, "little")) is None


def test_base_point_is_on_the_curve_and_has_the_right_order():
    """The two constants that would break everything downstream if mistyped,
    checked against the curve equation rather than against themselves."""
    x, y, z, t = ed25519.B
    assert z == 1 and t == x * y % ed25519.P
    assert (-x * x + y * y - 1 - ed25519.D * x * x * y * y) % ed25519.P == 0
    # [L]B is the identity: B generates a subgroup of exactly order L.
    assert ed25519._equal(ed25519._mul(ed25519.B, ed25519.L), ed25519.IDENTITY)


def test_addition_and_doubling_agree():
    """_double is a separate formula from _add for speed; if they ever disagree
    the scalar ladder silently computes the wrong point."""
    p = ed25519._mul(ed25519.B, 12345)
    assert ed25519._equal(ed25519._double(p), ed25519._add(p, p))


def test_scalar_multiplication_is_linear():
    """[a]B + [b]B == [a+b]B — catches an off-by-one in the ladder's bit loop,
    which the vectors alone can pass by luck on short scalars."""
    a, b = 0xDEADBEEF, 0x1234567890ABCDEF
    lhs = ed25519._add(ed25519._mul(ed25519.B, a), ed25519._mul(ed25519.B, b))
    assert ed25519._equal(lhs, ed25519._mul(ed25519.B, a + b))


def test_sha512_is_what_the_spec_says():
    """A sanity check on the hash, because a wrong digest would still produce a
    verifier that is perfectly self-consistent and rejects every real key."""
    assert hashlib.sha512(b"abc").hexdigest().startswith("ddaf35a1")


# ── the writing side that stayed here ──────────────────────────────────
#
# ⚠ `encode_point` is the one piece of the signing side kept in `ed25519.py`,
# and its docstring says why: it is curve arithmetic, it belongs beside the
# decoder it must round-trip with, and "test_ed25519.py can only check that
# round trip if both halves are in one module". Until 12 September 2026 this
# file did not check it.
#
# ⚠ That was not only a coverage gap. Nothing in the program calls
# `encode_point` — it is the signer's half of the round trip — so without
# these two tests it is a public function that nothing in this repository
# references at all.
#
# So these two tests are the fix for both: real coverage of a signer
# primitive, and a reference that exists in the tree people actually clone.


def test_encode_point_derives_the_rfc_8032_public_key():
    """The encoder agrees with the spec, not merely with our own decoder.

    A round trip alone cannot catch a consistently wrong encoding — flip the
    sign bit in both halves and every round trip still passes while every key
    minted is unverifiable by anything else. RFC 8032 §7.1 vector 1 gives a
    secret key and the public key it must produce, so this pins the encoder to
    the standard.
    """
    seed = _h("9d61b19deffd5a60ba844af492ec2cc4"
             "4449c5697b326919703bac031cae7f60")
    expected = _h("d75a980182b10ab7d54bfed3c964073a"
                 "0ee172f3daa62325af021a68f707511a")

    # The standard derivation: SHA-512 the seed, clamp the low half.
    a = int.from_bytes(hashlib.sha512(seed).digest()[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254

    assert ed25519.encode_point(ed25519._mul(ed25519.B, a)) == expected


@pytest.mark.parametrize("k", [1, 2, 12345, 0xDEADBEEF, ed25519.L - 1])
def test_encode_and_decode_are_inverses(k):
    """[k]B survives the compressed form and comes back the same point.

    `L - 1` is in the list on purpose: it is the largest scalar in the
    subgroup, and an encoder that mishandles the sign bit packed into the top
    bit of y tends to fail at the edges rather than in the middle.
    """
    point = ed25519._mul(ed25519.B, k)
    decoded = ed25519._decode_point(ed25519.encode_point(point))
    assert decoded is not None, f"[{k}]B did not survive encode/decode"
    assert ed25519._equal(decoded, point)
