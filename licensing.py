"""Licence keys: what a Pro key is, and whether this copy has a valid one.

Macronaut is free to download and free to use as an auto-clicker. The Pro tier
unlocks the parts that turn it into automation — seeing the screen, branching on
what it sees, and flows longer than a handful of steps. `entitlements.py` owns
*which* features those are; this module owns only the question "is this copy
licensed?", so the policy can be argued about without anyone touching crypto.

## The shape of the scheme, and why

**Offline, signed, perpetual.** A key is a small blob of facts — tier, buyer's
e-mail, order id, issue date — with an Ed25519 signature over them. Macronaut
carries the *public* key and can only check; the signer holds the private half
and is not part of the program.

Each of those words was a decision:

- **Offline** because a licence server is a monthly bill, a privacy question and
  a thing that can be down at the exact moment a paying customer is trying to
  work. It also means no telemetry: Macronaut never learns that you activated,
  never mind when or how often.
- **Signed, not hashed.** The tempting cheap version is an HMAC of the e-mail
  with a secret baked into the app — a 20-character key instead of this long
  one. It is worthless: the secret has to ship in order to be checked, so the
  first person to open the .exe in a hex editor can mint unlimited keys. With
  Ed25519 the app holds nothing worth stealing.
- **Perpetual, with no expiry field at all.** It removes the clock from the
  trust model. A licence that expires must be
  checked against a date, the date comes from the user's own machine, and a
  wrong clock (or a deliberately wrong one) then either locks out someone who
  paid or extends someone who didn't. Neither is a problem worth inventing.
- **The buyer's e-mail is inside the key and shown in the UI.** It is not
  verified against anything and is not meant to be: it is there so that posting
  your key on a forum posts your e-mail address with it. That is the whole
  anti-sharing mechanism, and it is deliberately social rather than technical,
  because every technical one (machine fingerprints, activation counts) mostly
  succeeds at punishing the honest customer who bought a new laptop.

## What this scheme does not do

It does not stop a determined person from patching the check out of the .exe.
Nothing that runs on the customer's computer can. The goal is that *paying is
easier than not paying* for the ordinary person, which this achieves, while the
one thing that would actually cost money — a key generator anyone can run —
stays impossible without the private key.

## Key format

    MN1-XXXXXXXX-XXXXXXXX-…

`MN1` is the format generation (bump it only for a genuinely incompatible
payload; a new *field* does not need one, see `decode_payload`). The rest is
RFC 4648 base32 of `payload ‖ signature`, unpadded, in groups of eight.

Base32 rather than base64 because a licence key gets read aloud, retyped off a
phone screen and pasted out of an e-mail that helpfully capitalised it: base32's
alphabet has no case to lose and omits 0/1/8/9, so there is no 0-vs-O or 1-vs-l
to get wrong. The cost is length — a 64-byte signature is 104 characters before
anything else — so the UI is built around pasting, and `parse_key` accepts the
key with any spacing, casing or line breaks it arrives in.
"""
from __future__ import annotations

import base64
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date
from typing import Optional, Tuple

import ed25519

APP_TIER_FREE = "free"
APP_TIER_PRO = "pro"

KEY_PREFIX = "MN1"
_GROUP = 8

# Macronaut's licence-signing public key, hex. Its private half lives only on
# the developer's machine, outside any repository, and is the one
# irreplaceable secret in the project: lose it and no new key can ever be minted
# for the thousands of copies already carrying this constant; leak it and anyone
# can mint their own.
#
# ⚠ Changing this value invalidates every licence already sold. There is no
# migration path — an old build verifies against the key baked into its own
# .exe. Treat it exactly as permanently as `version.UPDATE_REPO`.
PUBLIC_KEY_HEX = "07e0d1b6e21daebcee6f29ab2e646a6fd591dda1120698e6b6404f029dafa5e6"

_FORMAT = 1                       # payload byte 0
_TIERS = {1: APP_TIER_PRO}        # payload byte 1
_TIER_BYTES = {v: k for k, v in _TIERS.items()}
_EPOCH = date(2026, 1, 1)         # issue dates are days from here, in 2 bytes
_SIG_LEN = 64
# How many times a paste may repeat the prefix before we stop looking. A
# forwarded thread can carry the same key several times over; a thousand
# offsets is somebody probing, and each one costs a signature check.
_MAX_CANDIDATES = 8


@dataclass(frozen=True)
class License:
    """A verified licence, or the free tier. Never constructed from an
    unverified key — `parse_key` is the only way to get a Pro one."""

    tier: str = APP_TIER_FREE
    email: str = ""
    order_id: str = ""
    issued: Optional[date] = None
    key: str = ""

    @property
    def is_pro(self) -> bool:
        return self.tier == APP_TIER_PRO

    def describe(self) -> str:
        """One line for the Settings pane."""
        if not self.is_pro:
            return "Free"
        who = self.email or "this copy"
        when = f" · {self.issued.isoformat()}" if self.issued else ""
        return f"Pro — licensed to {who}{when}"


FREE = License()


# ── The payload codec ─────────────────────────────────────────────────────────

def encode_payload(tier: str, email: str, order_id: str,
                   issued: Optional[date] = None) -> bytes:
    """The signed part of a key. The signer calls this, then signs
    exactly these bytes — so a change here is a change to what a signature
    means, and old keys must still decode. See `decode_payload`."""
    if tier not in _TIER_BYTES:
        raise ValueError(f"unknown tier {tier!r}")
    issued = issued or date.today()
    days = (issued - _EPOCH).days
    if not 0 <= days <= 0xFFFF:
        raise ValueError("issue date outside the representable range")
    out = bytearray([_FORMAT, _TIER_BYTES[tier]])
    out += days.to_bytes(2, "big")
    for field in (order_id, email):
        raw = field.encode("utf-8")
        if len(raw) > 255:
            raise ValueError(f"field too long to encode: {field!r}")
        out.append(len(raw))
        out += raw
    return bytes(out)


def decode_payload(blob: bytes) -> Optional[Tuple[str, str, str, date, int]]:
    """(tier, email, order_id, issued, bytes consumed), or None if malformed.

    Returns how much it consumed rather than requiring it to be the whole blob,
    which is what lets a future format append fields without breaking today's
    keys: an older build reads the fields it knows, ignores the tail, and the
    signature still covers everything. That only works while the *existing*
    fields keep their meaning and order — so append, never insert.
    """
    try:
        if len(blob) < 6 or blob[0] != _FORMAT:
            return None
        tier = _TIERS.get(blob[1])
        if tier is None:
            return None
        issued = date.fromordinal(_EPOCH.toordinal()
                                  + int.from_bytes(blob[2:4], "big"))
        pos = 4
        fields = []
        for _ in range(2):
            n = blob[pos]
            pos += 1
            if pos + n > len(blob):
                return None
            fields.append(blob[pos:pos + n].decode("utf-8"))
            pos += n
        order_id, email = fields
        return tier, email, order_id, issued, pos
    except (IndexError, UnicodeDecodeError, ValueError, OverflowError):
        return None


# ── The wire format ───────────────────────────────────────────────────────────

def format_key(raw: bytes) -> str:
    """`payload ‖ signature` → the dashed string a customer receives."""
    body = base64.b32encode(raw).decode("ascii").rstrip("=")
    groups = [body[i:i + _GROUP] for i in range(0, len(body), _GROUP)]
    return "-".join([KEY_PREFIX] + groups)


def _candidates(text: str):
    """Every plausible reading of a pasted key, as raw bytes.

    A list rather than one answer, because the key does not arrive alone. It
    arrives as "Your key:
 MN1-…", or inside a forwarded e-mail, or with the
    order number above it — and once the separators are stripped so that
    spacing and line breaks stop mattering, the surrounding words have run into
    the key. So the prefix is *searched for* rather than required at position
    zero, and every occurrence of it is offered, because a label can perfectly
    well contain the letters MN1 before the real key does.

    ⚠ Requiring the string to start with the prefix is the obvious version and
    it rejects the single commonest way a key is actually pasted. That was
    caught by a test, not by a customer, which is the only reason it is cheap.
    """
    if not isinstance(text, str):
        return []
    cleaned = re.sub(r"[^0-9A-Za-z]", "", text).upper()
    out = []
    at = cleaned.find(KEY_PREFIX)
    while at != -1 and len(out) < _MAX_CANDIDATES:
        body = cleaned[at + len(KEY_PREFIX):]
        # b32decode insists on the padding that format_key strips.
        try:
            out.append(base64.b32decode(body + "=" * (-len(body) % 8),
                                        casefold=False))
        except Exception:
            pass                  # not a key at this offset; try the next one
        at = cleaned.find(KEY_PREFIX, at + 1)
    return out


def parse_key(text: str, public_key_hex: Optional[str] = None) -> Optional[License]:
    """A pasted key → a verified `License`, or None.

    None covers every kind of "no": mistyped, truncated, signed by the wrong
    key, or a perfectly well-formed forgery. The caller deliberately cannot
    tell those apart — an attacker learns nothing from the error message, and a
    customer would not be helped by the distinction anyway.
    """
    try:
        pub = bytes.fromhex(public_key_hex or PUBLIC_KEY_HEX)
    except ValueError:
        return None
    for raw in _candidates(text or ""):
        if len(raw) <= _SIG_LEN:
            continue
        payload = raw[:-_SIG_LEN]
        decoded = decode_payload(payload)
        if decoded is None:
            continue
        tier, email, order_id, issued, consumed = decoded
        if consumed > len(payload):
            continue
        if not ed25519.verify(raw[-_SIG_LEN:], payload, pub):
            continue
        return License(tier=tier, email=email, order_id=order_id,
                       issued=issued, key=format_key(raw))
    return None


# ── Where the activated key is kept ───────────────────────────────────────────

def _license_file():
    """Deferred import: `settings` builds its directory at import time, and a
    test that wants an isolated data dir must be able to patch it first."""
    import settings
    return settings.data_dir() / "license.json"


_cached: Optional[License] = None


def current() -> License:
    """The licence this copy is running under. Cached after the first read.

    Verification is ~5 ms of pure-Python scalar multiplication, which is nothing
    once but is called from the canvas as it draws each node, so it is answered
    from memory after the first time. `refresh()` drops the cache.
    """
    global _cached
    if _cached is None:
        _cached = _load()
    return _cached


def is_pro() -> bool:
    return current().is_pro


def refresh() -> None:
    """Forget the cached licence; the next `current()` re-reads and re-verifies."""
    global _cached
    _cached = None


def _load() -> License:
    """Read and verify the stored key. Any problem at all is the free tier —
    a corrupt file must never be able to stop the app from starting."""
    try:
        path = _license_file()
        if not path.exists():
            return FREE
        with open(path, "r", encoding="utf-8") as f:
            stored = json.load(f)
        lic = parse_key(stored.get("key", ""))
        return lic or FREE
    except Exception:
        return FREE


def activate(text: str) -> Tuple[bool, str]:
    """Verify a pasted key and, if it is good, remember it.

    Returns (ok, message) with the message written for the person in front of
    the dialog, not for a log.
    """
    lic = parse_key(text)
    if lic is None:
        return False, ("That key wasn't recognised. Check it was copied whole — "
                       "it starts with MN1- and is one long line.")
    try:
        path = _license_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        # ⚠ Written beside the target and moved over it, for the same reason
        # `FlowGraph.save` is — `open(path, "w")` truncates before writing, so
        # an interrupted write leaves a licensed copy silently unlicensed.
        # Recoverable (they still have the key) but it would happen at no
        # obvious moment.
        # Deliberately duplicated rather than shared: `licensing` sits below
        # everything and importing another module to save nine lines would be a
        # worse trade than the duplication.
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".lic-",
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"key": lic.key,
                           "activated": date.today().isoformat()}, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, str(path))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as exc:
        return False, f"The key is valid but couldn't be saved: {exc}"
    refresh()
    return True, f"Pro unlocked. Thank you — licensed to {lic.email or 'you'}."


def deactivate() -> bool:
    """Remove the stored key from this machine. Used when moving a licence to
    another computer; the key itself keeps working, because nothing about it
    was tied to this machine in the first place."""
    try:
        path = _license_file()
        if path.exists():
            path.unlink()
    except Exception:
        return False
    refresh()
    return True
