"""Nothing that was not signed may unlock Pro.

⚠ Nothing here mints anything, because the signer is not part of the program.
It only forges, so it runs
everywhere, including exactly the checkout where somebody is reading the
verifier and wondering how solid it is.

The failure mode is asymmetric: a wrongly *rejected* key
produces a complaint within the hour, and a wrongly *accepted* one produces
silence. So this asks the question from the attacker's side: keys that are correct in
every way except the signature, built with the module's own `encode_payload`
and `format_key` — which needs no private key, and is exactly the position an
attacker is in. None of them may parse, raise, or move `is_pro()`.
"""
import os
import random
import string
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import licensing

SEED = 5
ALPHABET = (string.ascii_letters + string.digits
            + "-_.=+/ \t\n:@{}[]\"'" + chr(92) + "%$#&")


def _noise(rng):
    """Junk. Exercises the parser's front door and nothing deeper."""
    return "".join(rng.choice(ALPHABET) for _ in range(rng.randint(0, 300)))


def _structurally_valid_forgery(rng):
    """A key that is right in every way except the signature.

    ⚠⚠ This is the whole test, and the first version of this file did not have
    it. Those forgeries were `base64 + "." + base64`, a shape I invented; real
    keys are `KEY_PREFIX` + base32 of a blob whose last 64 bytes are the
    signature. So every forgery died at `_candidates` — before `decode_payload`
    and long before `ed25519.verify` — and **the tests passed with the
    signature check commented out**, which is how that was found.

    This one is built with the module's own `encode_payload` and `format_key`,
    so it survives candidate extraction, decodes to a real payload, passes the
    length checks, and arrives at the one line that matters carrying 64 random
    bytes where the signature belongs. Building it needs no private key —
    that is exactly the attacker's position.
    """
    # ⚠ Tiers come from the module, not from a list written here. Inventing
    # "PRO" made `encode_payload` raise `unknown tier` — a ValueError from the
    # *test helper*, which reads at a glance like the code under test blowing
    # up on a forgery. It is stricter than it looks, and rightly so.
    payload = licensing.encode_payload(
        tier=rng.choice(sorted(licensing._TIER_BYTES)),
        email=rng.choice(["a@b.c", "buyer@example.com", ""]),
        order_id=rng.choice(["1", "ord_123", ""]),
        issued=date(2026, rng.randint(1, 12), rng.randint(1, 28)),
    )
    sig = bytes(rng.getrandbits(8) for _ in range(licensing._SIG_LEN))
    return licensing.format_key(payload + sig)


def _forged(rng):
    """Half noise, half a structurally valid forgery."""
    return (_noise(rng) if rng.random() < 0.5
            else _structurally_valid_forgery(rng))


def test_no_forged_key_parses_and_none_of_them_raise():
    """20,000 at the bench, 4,000 here. `parse_key` must answer None every
    time — never a licence, and never an exception either: a traceback out of
    the verifier would reach the user as a crash on the paid path."""
    rng = random.Random(SEED)
    accepted, raised = [], []
    for _ in range(4000):
        key = _forged(rng)
        try:
            if licensing.parse_key(key) is not None:
                accepted.append(key)
        except Exception as exc:                # noqa: BLE001 - that is the test
            raised.append(f"{type(exc).__name__}: {exc} on {key[:40]!r}")

    assert not accepted, f"{len(accepted)} forged keys parsed: {accepted[:3]}"
    assert not raised, f"{len(raised)} raised: {raised[:3]}"


def test_the_forgeries_really_reach_the_signature_check(monkeypatch):
    """⚠⚠ The assertion that makes every other one in this file mean something.

    Proven necessary rather than assumed: with `ed25519.verify` stubbed to
    always succeed, `parse_key` must start accepting these. If it does not,
    the forgeries are dying earlier — in candidate extraction or payload
    decoding — and the file is testing the parser's front door while reporting
    that the lock is sound. The earlier version of this file did exactly that,
    and passed with the signature check disabled.
    """
    rng = random.Random(SEED)
    keys = [_structurally_valid_forgery(rng) for _ in range(20)]

    # Sanity: rejected right now, as they must be.
    assert all(licensing.parse_key(k) is None for k in keys)

    monkeypatch.setattr(licensing.ed25519, "verify",
                        lambda sig, msg, pub: True)
    reached = sum(1 for k in keys if licensing.parse_key(k) is not None)
    assert reached == len(keys), (
        f"only {reached}/{len(keys)} forgeries reached ed25519.verify — the "
        "rest are being rejected before the signature is ever checked, so "
        "this file proves nothing about the cryptography")


def test_activate_refuses_a_forgery_without_changing_anything(monkeypatch,
                                                              tmp_path):
    """`activate` writes to disk, so this is the lighter pass — but it is the
    one that matters, because it is the function the paywall actually calls."""
    import settings as st
    monkeypatch.setattr(st, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(st, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(st, "data_dir", lambda: tmp_path)

    before = licensing.is_pro()
    rng = random.Random(SEED)
    for _ in range(200):
        ok, msg = licensing.activate(_forged(rng))
        assert ok is False, f"activate accepted a forgery: {msg}"
        assert isinstance(msg, str) and msg, "refused without saying why"

    assert licensing.is_pro() is before, (
        "a forged key changed whether this copy is licensed")


def test_an_empty_or_whitespace_key_is_refused_politely():
    """The commonest real input after a mis-paste. It must not be an exception,
    and `activate` must still explain itself."""
    for key in ("", "   ", "\n\t", None):
        assert licensing.parse_key(key or "") is None
        ok, msg = licensing.activate(key or "")
        assert ok is False and isinstance(msg, str) and msg
