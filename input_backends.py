"""Selectable input-send backends: pynput (default), SendInput scancodes,
Interception driver — for both the keyboard and the mouse.

`flow_exec.py` is the engine that sends input, and it goes through a pynput
`keyboard.Controller` (`self._kb`) and a pynput `mouse.Controller`
(`self._mouse`) obtained from `make_keyboard()` / `make_mouse()` here.

⚠ This line named "clicker.py, flow_exec.py, recorder.py" as the three engines
until 4 September 2026, and two of those were wrong. `clicker.py` is dead and
does not import this module at all — its own banner says so, and that it talks
to pynput directly, "cannot reach a game that ignores message-queue input", and
so is unusable for the main reason anyone installs this. `recorder.py` calls
`make_mouse()`/`make_keyboard()` only inside `PlaybackWorker`, which is also
dead; the live recorder records and does not send.

Games differ in what injected input they accept:

- "pynput"       — virtual-key codes via the message queue. Fine for normal apps,
                   ignored by many games.
- "sendinput"    — SendInput with hardware scancodes (sendinput_backend.py).
                   Works for games on raw input / DirectInput.
- "interception" — kernel-driver injection (interception_backend.py). For games
                   that reject all user-mode input (e.g. Ghost of Tsushima).
                   Needs the Interception driver installed + a one-time
                   `python interception_backend.py --identify`.

`make_keyboard()` / `make_mouse()` return objects with the pynput Controller
surface the engines already use — `press` / `release` / `type` taking pynput
Key/KeyCode objects, and `position` / `press` / `release` / `click` taking pynput
Button objects — so swapping backends is a one-line change at the construction
site. One setting, `input_backend`, drives both.

Fallback policy: if the chosen backend is unavailable (driver not installed,
import failure), you get pynput plus a human-readable warning string — callers
surface it in the run log.

Text typing (`type`) goes out as real key presses on *every* backend, pynput
included. It used to be KEYEVENTF_UNICODE, on the reasoning that scancodes fight
the keyboard layout — but VK_PACKET carries a codepoint rather than a key
position, so it only ever produces WM_CHAR, and a game reading raw input or
DirectInput never sees it. That is why a flow could press a key and press Enter
successfully and type nothing in between.

⚠ pynput's own `type()` is a *mixture*, and that mixture is what made the bug so
hard to name: it resolves each character with VkKeyScan and sends a real key when
no modifier is needed, but a bare Unicode packet when one is — never pressing the
modifier. On a Belgian layout that splits a sentence in half. Lowercase letters
and space arrive; capitals, digits and "." do not. It reads as "only capitals are
broken" when the real fault line is "characters that need a modifier".

`key_positions` decides which keyboard the target is assumed to be reading (see
settings.type_key_positions).

Reading the cursor position always resolves to the real OS cursor — every
backend is a write-only path.
"""
from __future__ import annotations

import time
from typing import Optional, Tuple

from pynput.keyboard import Controller as _PynputController, Key, KeyCode
from pynput.mouse import Button, Controller as _PynputMouse

# Layout translation for single-character key names — see translate_name()
# below. Guarded so importing this module stays safe on non-Windows / if
# sendinput_backend fails to load (its ctypes setup is Windows-only).
try:
    from sendinput_backend import layout_scancode, qwerty_name_for_scancode
    from sendinput_backend import layout_keystroke as _layout_keystroke
    from sendinput_backend import type_text as _sendinput_type
except Exception:  # pragma: no cover - non-Windows / import edge
    layout_scancode = None
    qwerty_name_for_scancode = None
    _layout_keystroke = None
    _sendinput_type = None

# Per-character timing for scancode typing. A game polls its input once a
# frame, so a key that goes down and up inside one frame can be missed
# entirely — the hold has to straddle a frame boundary at 60 Hz (16.7 ms).
TYPE_KEY_HOLD_S = 0.02
TYPE_KEY_GAP_S = 0.01
# And a modifier change needs its own frame before the key that depends on it.
# Pressing SHIFT and the letter with nothing in between is the single most
# likely reason a capital arrives lowercase or not at all: a receiver that
# samples modifier state when it processes the keypress can read the state from
# *before* the SHIFT landed. Same going the other way — a lowercase letter
# straight after a capital needs the SHIFT release to have registered first.
TYPE_MOD_SETTLE_S = 0.02


# The fastest rate anything here will attempt. Above the safe pace the key
# timing is squeezed to fit, so this is where "the user said go faster" stops
# being a request and starts being an ordering problem: at 200 ch/s a key is
# held 3 ms, which is a fifth of a 60 Hz frame.
MAX_TYPE_CPS = 200.0
# Never squeeze below this, whatever is asked for. A press and its release still
# have to be two distinguishable events at the far end.
MIN_KEY_HOLD_S = 0.002

# How a Type step wants its text delivered, when it says. `flow.SEND_*` owns the
# vocabulary — these are copies so this module keeps its "no app imports" rule
# (it is imported by the backends themselves, and flow.py imports nothing local).
# `tests/test_input_backends.py` pins the two spellings together, because a typo
# here would not raise: it would silently mean "auto" and be read as the setting
# not working.
SEND_AUTO, SEND_CHARS, SEND_KEYS = "auto", "chars", "keys"


def safe_type_cps() -> float:
    """The fastest rate that keeps the full per-key timing, in characters/sec.

    Derived from the timing rather than written down beside it, so the number
    the UI offers can never drift from the number the engine delivers — which
    is exactly what made every selected speed a lie: the engine spent HOLD+GAP
    per character and the caller then slept 1/cps *on top* of it.

    ~33 ch/s, and what "as fast as possible" means. A capital costs more (a
    modifier settle each way), so it is a ceiling rather than a promise.
    """
    return 1.0 / (TYPE_KEY_HOLD_S + TYPE_KEY_GAP_S)


def key_timing(cps: Optional[float]) -> tuple:
    """Requested rate -> (hold, gap, settle) seconds for one keystroke.

    At or below the safe pace this is the full timing and the caller waits out
    the rest of the period. Above it the three are scaled down proportionally
    to fit — the only way to type faster is to hold each key for less time, and
    a shorter hold is exactly what a game polling once a frame can miss. That
    is the trade the speed box is offering, and it is the user's to make: a
    dropped character is visible and the fix is to lower the number.
    """
    full = (TYPE_KEY_HOLD_S, TYPE_KEY_GAP_S, TYPE_MOD_SETTLE_S)
    if not cps or cps <= 0:
        return full
    period = 1.0 / min(float(cps), MAX_TYPE_CPS)
    budget = TYPE_KEY_HOLD_S + TYPE_KEY_GAP_S
    if period >= budget:
        return full
    scale = period / budget
    return (max(MIN_KEY_HOLD_S, TYPE_KEY_HOLD_S * scale),
            max(0.0, TYPE_KEY_GAP_S * scale),
            max(0.0, TYPE_MOD_SETTLE_S * scale))


def type_timing(cps: Optional[float]) -> tuple:
    """(hold, gap, settle) for a typist that paces a whole string itself.

    `key_timing` only ever *shortens*, because it is the answer to "how fast can
    one keystroke be". This is the answer to "how long is one character's slot",
    which is the same thing above the safe pace and a longer gap below it — the
    idle time between keystrokes stretches, the key press never does.

    ⚠ Pacing a slow rate belongs here, in one call for the whole string, and not
    in a caller that invokes the typist once per character. Each call ends by
    releasing its modifiers, so a per-character caller taps SHIFT for every
    character instead of holding it across a run: it pays a settle each time (so
    the selected rate never arrives) and puts a modifier release between every
    pair of characters, which is the shape that lost 2.0.16 its text.
    """
    hold, gap, settle = key_timing(cps)
    if cps and cps > 0:
        gap = max(gap, (1.0 / float(cps)) - hold)
    return hold, gap, settle


# Back-compat: this used to be the one ceiling, before the dial could exceed it.
max_type_cps = safe_type_cps

# character -> (US key name, needs shift), for type_key_positions == "us".
# Hardcoded on purpose: this mode exists for targets that consult no layout at
# all, so asking Windows which key produces a character would defeat the point.
_US_UNSHIFTED = "1234567890-=[]\\;',./`"
_US_SHIFTED = "!@#$%^&*()_+{}|:\"<>?~"
_US_KEYS: dict = {" ": ("space", False)}
for _c in "abcdefghijklmnopqrstuvwxyz":
    _US_KEYS[_c] = (_c, False)
    _US_KEYS[_c.upper()] = (_c, True)
for _plain, _shift in zip(_US_UNSHIFTED, _US_SHIFTED):
    _US_KEYS[_plain] = (_plain, False)
    _US_KEYS[_shift] = (_plain, True)
del _c, _plain, _shift

# ── Backend ids (persisted in settings — do not rename) ──────────────────────
BACKEND_PYNPUT = "pynput"
BACKEND_SENDINPUT = "sendinput"
BACKEND_INTERCEPTION = "interception"
BACKENDS = (BACKEND_PYNPUT, BACKEND_SENDINPUT, BACKEND_INTERCEPTION)

# Combo-box labels for the Settings UI (id -> label), in display order.
BACKEND_LABELS = {
    BACKEND_PYNPUT: "Standard (pynput)",
    BACKEND_SENDINPUT: "SendInput scancodes — raw-input games",
    BACKEND_INTERCEPTION: "Interception driver — games that block injection",
}


def _key_to_name(key) -> Optional[str]:
    """pynput Key/KeyCode -> Macronaut key-name string.

    pynput's Key enum member names ("space", "ctrl_l", "page_up", "f5", ...)
    are exactly Macronaut's key names (see keystrokes._KEY_MAP), so this is the
    straight inverse of keystrokes.parse_key().
    """
    if isinstance(key, Key):
        return key.name
    if isinstance(key, KeyCode) and key.char:
        return key.char
    return None


def _button_to_name(button) -> Optional[str]:
    """pynput Button -> Macronaut button-name string ("left"/"right"/...).

    Button member names are already Macronaut's button names, so this is just a
    guarded `.name`. `Button.unknown` maps to None so it routes to pynput rather
    than being sent as a literal "unknown".
    """
    if isinstance(button, Button):
        return None if button is Button.unknown else button.name
    if isinstance(button, str):
        return button
    return None


def translate_name(name: str) -> str:
    """Captured key name -> QWERTY-position name, for the scancode-keyed
    backend tables (sendinput_backend.SCANCODES, and interception-python's
    own internal table, which is also QWERTY-keyed).

    Key capture is character-based (pynput reports the typed *character*
    under the user's active layout), but sendinput/interception address keys
    by fixed QWERTY *position*. Those are the same string on a QWERTY layout,
    but not on AZERTY/QWERTZ/etc — e.g. on Belgian AZERTY the key labelled
    "Z" reports as the character "z", yet physically sits where a QWERTY
    keyboard has "W". Resolve the captured character to its physical scancode
    under the active layout, then map that scancode back to the QWERTY name
    for that physical key, so backend tables address the key the user
    actually pressed.

    Only single-character names are candidates — multi-character names
    ("space", "f5", "shift_l", "up", ...) are already position-based
    identifiers, not typed characters, so they pass through unchanged. If
    layout resolution is unavailable (non-Windows, API failure) or yields
    nothing, the name also passes through unchanged (status quo: the QWERTY
    table is looked up directly, as before this fix).
    """
    if layout_scancode is None or qwerty_name_for_scancode is None:
        return name
    if not isinstance(name, str) or len(name) != 1:
        return name
    sc = layout_scancode(name)
    if sc is None:
        return name
    qname = qwerty_name_for_scancode(sc)
    if qname is None:
        return name
    return qname


class _NameBackendAdapter:
    """Puts the pynput Controller surface on a name-string backend
    (ScancodeKeyboard / InterceptionKeyboard).

    Keys the backend has no mapping for (e.g. media keys on the scancode
    table) fall back to pynput — best-effort delivery beats dropping them.
    press/release resolve the same way for the same key, so a key held via
    the fallback is also released via the fallback.
    """

    def __init__(self, impl, key_positions: Optional[str] = None):
        self._impl = impl
        self._pynput = _PynputController()
        # "layout" | "us" — see settings.type_key_positions. Read once here so
        # a long text does not hit the settings file per character.
        self._key_positions = key_positions or _settings_key_positions()

    def _route(self, key) -> Tuple[object, object]:
        """-> (target, payload): (impl, name) or (pynput controller, key)."""
        name = _key_to_name(key)
        if name is not None:
            name = translate_name(name)
            if self._maps(name):
                return self._impl, name
        return self._pynput, key

    def _maps(self, name: str) -> bool:
        try:
            probe = getattr(self._impl, "maps_key", None)
            if probe is not None:
                return bool(probe(name))
        except Exception:
            pass
        return True  # backend without a probe: let press() raise and fall back

    def press(self, key) -> None:
        target, payload = self._route(key)
        try:
            target.press(payload)
        except (KeyError, ValueError):
            if target is not self._pynput:
                self._pynput.press(key)

    def release(self, key) -> None:
        target, payload = self._route(key)
        try:
            target.release(payload)
        except (KeyError, ValueError):
            if target is not self._pynput:
                self._pynput.release(key)

    def _keystroke_for(self, ch: str, mode: Optional[str] = None):
        """`ch` -> (backend key name, [modifier names]), or (None, []).

        Two answers are possible and they differ on a non-US layout, which is
        why this is a setting rather than a constant. A scancode is a key
        *position*; the character it becomes is decided by whoever receives it.

        "layout"  ask the active layout which position produces `ch`. Correct
                  for anything that honours Windows layouts — a Belgian user
                  types "a" and gets "a".
        "us"      use the position a US keyboard has `ch` on. Correct for games
                  that ignore Windows layouts and run every scancode through
                  their own US table, where "layout" delivers "q" for "a".
        """
        if (mode or self._key_positions) == "us":
            hit = _US_KEYS.get(ch)
            if hit is None:
                return None, []
            name, shifted = hit
            return name, (["shift"] if shifted else [])
        stroke = _layout_keystroke(ch) if _layout_keystroke else None
        if not stroke or qwerty_name_for_scancode is None:
            return None, []
        name = qwerty_name_for_scancode(stroke[0])
        if name is None:
            return None, []
        mods = []
        if stroke[1]:
            mods.append("shift")
        if stroke[2]:
            mods.append("ctrl")
        if stroke[3]:
            mods.append("alt")
        return name, mods

    def _type_scancodes(self, text: str, should_continue=None,
                        mode: Optional[str] = None,
                        cps: Optional[float] = None) -> None:
        """Type by pressing physical keys through the backend, as a real
        keyboard does — modifiers included.

        `cps` is the rate the user asked for, and this handles every rate — see
        `type_timing`. Above the safe pace the key hold shortens; below it the
        gap stretches instead, so a slow rate is idle time between keystrokes
        rather than an unnaturally long key press. ⚠ Do not pace this by calling
        it once per character; the `finally` below releases the modifiers, so a
        per-character caller taps SHIFT for every character.

        A character the active layout cannot produce in one keystroke (not on
        the layout at all, or needing a dead-key sequence) goes out as Unicode
        on its own. That character was never going to reach a raw-input target
        anyway, and sending the rest correctly beats refusing the whole string.
        """
        hold, gap, settle = type_timing(cps)
        # The gap is spent as "wait until this character's slot is up" rather
        # than as a fixed pause. time.sleep overshoots by a few tenths of a
        # millisecond and there are two per character, which is invisible at
        # 33 ch/s and a 10% shortfall at 200. Carrying a deadline lets the
        # overshoot on the hold come out of the gap, so the *average* lands on
        # the period instead of drifting under it.
        period = hold + gap
        due = time.perf_counter()
        held: list = []
        try:
            for ch in text.replace("\r\n", "\n"):
                if should_continue is not None and not should_continue():
                    return
                if ch in ("\n", "\r"):
                    name, mods = "enter", []
                else:
                    name, mods = self._keystroke_for(ch, mode)
                if name is None or not self._maps(name):
                    # Unicode injection carries no modifier state, so anything
                    # still held would be wrong for it — and would stay wrong
                    # for every character after it.
                    held = self._set_modifiers(held, [], settle)
                    if _sendinput_type is not None:
                        _sendinput_type(ch)
                    continue
                held = self._set_modifiers(held, mods, settle)
                self._impl.press(name)
                time.sleep(hold)
                self._impl.release(name)
                due += period
                remaining = due - time.perf_counter()
                if remaining > 0:
                    time.sleep(remaining)
                else:
                    # A character that overran its slot (a modifier settle) must
                    # not make the next ones race to catch up — the deadline
                    # resets rather than carrying a debt forward.
                    due = time.perf_counter()
        finally:
            # A modifier left down is the worst failure mode here — every
            # keystroke the user made afterwards would be shifted.
            for m in reversed(held):
                try:
                    self._impl.release(m)
                except Exception:
                    pass

    def _set_modifiers(self, held: list, wanted,
                       settle: float = TYPE_MOD_SETTLE_S) -> list:
        """Move the held modifiers to `wanted`, and return the new held list.

        Only the difference is sent, so a run of capitals holds SHIFT down once
        rather than tapping it per letter — which is both what a real typist
        does and far fewer events for the receiver to keep up with.

        Any change is followed by a settle pause. A receiver that samples
        modifier state when it processes a keypress can otherwise read the
        state from before the change landed, which turns a capital into a
        lowercase letter or drops it outright.
        """
        wanted = list(wanted)
        if wanted == held:
            return held
        for m in reversed([m for m in held if m not in wanted]):
            if self._maps(m):
                self._impl.release(m)
        for m in [m for m in wanted if m not in held]:
            if self._maps(m):
                self._impl.press(m)
        if settle:
            time.sleep(settle)
        return wanted

    def type(self, text: str, should_continue=None,
             key_positions: Optional[str] = None,
             cps: Optional[float] = None,
             send_as: Optional[str] = None) -> None:
        # An explicit "characters" on the step overrides the backend's nature.
        # Rare but real: a driver backend chosen for a game's *keys*, typing one
        # line into a launcher or a browser field that reads WM_CHAR.
        if send_as == SEND_CHARS and _sendinput_type is not None:
            try:
                _sendinput_type(text, should_continue, timing=type_timing(cps))
                return
            except OSError:
                pass       # injection refused (UIPI, elevated target)
        # A driver-level backend is selected precisely because the target
        # ignores message-queue input — and KEYEVENTF_UNICODE *is* message-queue
        # input. VK_PACKET carries a codepoint rather than a key position, so it
        # produces WM_CHAR and nothing else: Notepad reads WM_CHAR and works, a
        # game reading raw input or DirectInput never sees it. Typing therefore
        # has to travel the same road press()/release() already take, or a flow
        # that presses T and Enter successfully still types nothing in between.
        if getattr(self._impl, "sends_scancodes", False):
            try:
                self._type_scancodes(text, should_continue, key_positions, cps)
                return
            except (KeyError, ValueError, OSError):
                pass   # backend refused a key — fall through to the old path
        # Still message-queue, still layout-independent — but paced per
        # character and holding a real SHIFT across capitals, neither of which
        # pynput's own type() does. Newlines become a real VK_RETURN there; as
        # a character they resolve to Ctrl+Enter.
        if _sendinput_type is not None:
            try:
                _sendinput_type(text, should_continue)
                return
            except OSError:
                pass   # injection refused (UIPI, elevated target) — fall back
        self._pynput.type(text)


class _RealKeyTypingController(_PynputController):
    """pynput for keys; text typed as real key presses rather than characters.

    ⚠ **A game does not read characters.** `KEYEVENTF_UNICODE` delivers a
    codepoint with no key behind it, which produces `WM_CHAR` and nothing else,
    so a target reading raw input or DirectInput never sees it. Real key events
    reach both kinds of receiver, which is why typing has to travel the same
    road press()/release() already take.

    This is also, exactly, why the original bug looked like a capitals bug.
    pynput's own `type()` resolves each character with `VkKeyScan` and sends a
    **real key** when the character needs no modifier — but falls back to a
    Unicode packet when it does, *without pressing the modifier*. On a Belgian
    layout that splits a sentence in two: lowercase letters, space and `!` are
    real keys and arrive; capitals, digits and `.` are Unicode and vanish. The
    reported "everything types except capitals" was that split, not a case bug.

    Three releases were then spent moving *more* of the text onto the Unicode
    path — batched (2.0.14), paced (2.0.15), unmodified (2.0.16) — each one
    strictly further from a receiver that was never reading characters at all,
    ending at "it doesn't type anything". The direction was backwards the whole
    time.

    Only `type` changes; press/release are pynput's, unchanged.
    """

    def __init__(self):
        super().__init__()
        self._typist = None
        try:
            from sendinput_backend import ScancodeKeyboard
            # Reuses the adapter's typing path wholesale: real keystrokes with
            # real modifiers, per-key pacing, the "type as if on US" setting,
            # and a Unicode fallback for a character the layout cannot produce
            # in one keystroke.
            self._typist = _NameBackendAdapter(ScancodeKeyboard())
        except Exception:
            pass        # not Windows, or SendInput unavailable — use pynput

    def type(self, text: str, should_continue=None,
             key_positions: Optional[str] = None,
             cps: Optional[float] = None) -> None:
        if self._typist is not None:
            try:
                self._typist.type(text, should_continue, key_positions, cps)
                return
            except OSError:
                pass       # injection refused (UIPI, elevated target)
        super().type(text)


class _UnicodeTypingController(_PynputController):
    """pynput for keys; text as Unicode packets — what picking pynput means.

    A packet carries the **character**; a scancode carries a **key position**
    and lets the receiver decide which character that is. Both are right, for
    different targets, and the backend selector is already the choice between
    them: pynput *is* the message-queue backend, and `sendinput` / `interception`
    exist precisely for targets that ignore the message queue.

    ⚠ 2.0.17 moved this class's job onto the scancode path, which put
    driver-backend behaviour inside the message-queue backend. It fixed a real
    bug — pynput's own `type()` sends a modifier-needing character as a bare
    packet *without pressing the modifier*, so on a Belgian layout capitals,
    digits and `.` never arrived — but it did so by taking away the one property
    that made this backend worth choosing. On an AZERTY board a scancode target
    reads `a` as `q`, and every digit becomes a shifted keystroke costing a
    modifier settle. That is a regression for everyone typing into an ordinary
    window, which is who selects pynput.

    So the modifier bug is fixed here the other way: `sendinput_backend.type_text`
    sends every character as a packet, never pynput's mixture, so nothing depends
    on a modifier being pressed. A target that reads raw input still sees none of
    it — that target is what the other two backends are for.

    Only `type` changes; press/release are pynput's, unchanged.
    """

    def __init__(self, shift_for_capitals: Optional[bool] = None):
        super().__init__()
        self._shift_for_capitals = (_settings_shift_for_capitals()
                                    if shift_for_capitals is None
                                    else bool(shift_for_capitals))
        self._keys = None       # lazy real-key typist, for send_as == "keys"

    def _real_keys(self):
        """The scancode typist, built on first use. None if unavailable.

        Lazy rather than built in __init__ because the overwhelmingly common
        case is a flow that never asks for it, and this controller is
        constructed on every run.
        """
        if self._keys is None:
            try:
                from sendinput_backend import ScancodeKeyboard
                # The same real-key path the driver backends use: real modifiers,
                # per-key pacing, the "type as if on US" setting, and a Unicode
                # fallback for a character the layout cannot make in one press.
                self._keys = _NameBackendAdapter(ScancodeKeyboard())
            except Exception:
                self._keys = False      # not Windows / SendInput unavailable
        return self._keys or None

    def type(self, text: str, should_continue=None,
             key_positions: Optional[str] = None,
             cps: Optional[float] = None,
             send_as: Optional[str] = None) -> None:
        # ⚠ A step asking for key presses gets them, on this backend too. The
        # mechanism is a property of the *target* — a game reads key events and
        # ignores packets — and the backend selector cannot express it, because
        # it is one global switch that also governs keys and clicks. Bolting the
        # two together is what made 2.0.17 and its revert each break the other's
        # user. Steps that say nothing still get packets, which is what picking
        # pynput has always meant.
        if send_as == SEND_KEYS:
            typist = self._real_keys()
            if typist is not None:
                try:
                    typist.type(text, should_continue, key_positions, cps)
                    return
                except OSError:
                    pass   # injection refused — fall through to packets
        # `key_positions` is deliberately ignored below. It answers "which
        # keyboard is the target reading positions on", and a packet has no
        # position — the character arrives as itself on any layout. It stays in
        # the signature because a Type step may carry one for whichever backend
        # runs it (and for the real-key path just above, which does use it).
        if _sendinput_type is not None:
            try:
                _sendinput_type(text, should_continue,
                                shift_for_capitals=self._shift_for_capitals,
                                timing=type_timing(cps))
                return
            except OSError:
                pass       # injection refused (UIPI, elevated target)
        super().type(text)


class _MouseBackendAdapter:
    """Puts the pynput mouse.Controller surface on a name-string backend
    (ScancodeMouse / InterceptionMouse).

    Buttons the backend has no mapping for fall back to pynput — best-effort
    delivery beats dropping the click. press/release resolve the same way for
    the same button, so a button held via the fallback is released via it too.

    Moves also fall back: if the backend's move fails, the pynput move still
    puts the cursor in the right place, which matters more than which path
    delivered it — a click at a stale position is a wrong click.
    """

    _FALLBACK_ERRORS = (OSError, RuntimeError, ValueError, KeyError)

    def __init__(self, impl):
        self._impl = impl
        self._pynput = _PynputMouse()

    def _route(self, button) -> Tuple[object, object]:
        """-> (target, payload): (impl, name) or (pynput controller, Button)."""
        name = _button_to_name(button)
        if name is not None and self._maps(name):
            return self._impl, name
        return self._pynput, button

    def _maps(self, name: str) -> bool:
        try:
            probe = getattr(self._impl, "maps_button", None)
            if probe is not None:
                return bool(probe(name))
        except Exception:
            pass
        return True  # backend without a probe: let press() raise and fall back

    # ── position ──────────────────────────────────────────────────────
    @property
    def position(self) -> tuple:
        try:
            return self._impl.position
        except self._FALLBACK_ERRORS:
            return self._pynput.position

    @position.setter
    def position(self, xy) -> None:
        try:
            self._impl.position = xy
        except self._FALLBACK_ERRORS:
            self._pynput.position = xy

    # ── buttons ───────────────────────────────────────────────────────
    def press(self, button) -> None:
        target, payload = self._route(button)
        try:
            target.press(payload)
        except self._FALLBACK_ERRORS:
            if target is not self._pynput:
                self._pynput.press(button)

    def release(self, button) -> None:
        target, payload = self._route(button)
        try:
            target.release(payload)
        except self._FALLBACK_ERRORS:
            if target is not self._pynput:
                self._pynput.release(button)

    def click(self, button, count: int = 1) -> None:
        target, payload = self._route(button)
        try:
            target.click(payload, count)
        except self._FALLBACK_ERRORS:
            if target is not self._pynput:
                self._pynput.click(button, count)

    # ── wheel ─────────────────────────────────────────────────────────
    def scroll(self, dx: int = 0, dy: int = 0) -> None:
        """Wheel by whole notches, +dy up / +dx right (pynput's convention).

        Falls back like everything else here: a backend too old to have a wheel
        still scrolls, via pynput. A scroll that silently does nothing is the
        worst outcome, because the flow carries on as if the list had moved.
        """
        fn = getattr(self._impl, "scroll", None)
        if fn is not None:
            try:
                fn(dx, dy)
                return
            except self._FALLBACK_ERRORS:
                pass
        self._pynput.scroll(dx, dy)


def interception_available() -> bool:
    """True if the Interception package + kernel driver are usable (for UI hints)."""
    try:
        import interception_backend
        return interception_backend.is_available()
    except Exception:
        return False


def _live_settings():
    """The app's settings if it registered any, else a fresh read of the file.

    ⚠ Never construct `SettingsManager()` directly for a *runtime* decision.
    Its `__init__` re-reads the JSON, so a backend built during a run saw the
    file on disk while the Settings window was showing the app's unsaved
    in-memory copy. Picking pynput and pressing Play still ran the previously
    persisted backend — which is how "I have been on pynput the entire time"
    coexisted with every run going through the Interception driver, typing key
    positions and turning AZERTY 'a' into 'q'.
    """
    from settings import SettingsManager, active
    return active() or SettingsManager()


def _settings_backend() -> str:
    try:
        return getattr(_live_settings(), "input_backend", BACKEND_PYNPUT)
    except Exception:
        return BACKEND_PYNPUT


def _settings_shift_for_capitals() -> bool:
    try:
        return bool(getattr(_live_settings(), "type_shift_for_capitals", False))
    except Exception:
        return False


def _settings_key_positions() -> str:
    try:
        return getattr(_live_settings(), "type_key_positions", "layout")
    except Exception:
        return "layout"


def make_mouse(backend: Optional[str] = None):
    """Build the mouse controller for the given backend id.

    Same contract as make_keyboard: backend=None reads `input_backend` from
    settings, and the return is (controller, actual_backend_id, warning).

    This matters more than the keyboard for an autoclicker — a click IS the
    product — so the failure modes are worth spelling out:

    - Interception addresses the mouse as one of device slots 10-19 and needs
      its own one-time `--identify-mouse`; the keyboard's `--identify` does not
      cover it. With no saved mouse slot we still proceed on the package's
      auto-detection, but return an advisory warning: auto-detection picks the
      first mouse-ish slot, and on a machine with a virtual one (RGB software,
      wireless dongle) every click would vanish silently.
    - So `warning` here is not strictly "we fell back" — it can also be "this is
      working but is a known silent-failure risk". Callers log it either way.
    """
    if backend is None:
        backend = _settings_backend()

    if backend == BACKEND_INTERCEPTION:
        try:
            import interception_backend as ib
            if ib.mouse_available():
                warn = None
                if ib._load_saved_slot("mouse") is None:
                    warn = ("Interception mouse slot not identified — using "
                            "auto-detection. If clicks do nothing in-game, run: "
                            "python interception_backend.py --identify-mouse")
                return (_MouseBackendAdapter(ib.InterceptionMouse()),
                        BACKEND_INTERCEPTION, warn)
            warn = ("Interception driver not available for the mouse (install + "
                    "reboot, then run --identify-mouse) — using pynput; games "
                    "may ignore clicks.")
        except Exception as e:  # pragma: no cover - import/driver edge
            warn = f"Interception mouse backend failed ({e}) — using pynput."
        return _PynputMouse(), BACKEND_PYNPUT, warn

    if backend == BACKEND_SENDINPUT:
        try:
            import sendinput_backend as sb
            return (_MouseBackendAdapter(sb.ScancodeMouse()),
                    BACKEND_SENDINPUT, None)
        except Exception as e:  # pragma: no cover - non-Windows / import edge
            return (_PynputMouse(), BACKEND_PYNPUT,
                    f"SendInput mouse backend failed ({e}) — using pynput.")

    return _PynputMouse(), BACKEND_PYNPUT, None


def make_keyboard(backend: Optional[str] = None):
    """Build the keyboard controller for the given backend id.

    backend=None reads `input_backend` from settings (missing/unknown -> pynput).
    Returns (controller, actual_backend_id, warning). warning is None, or a
    human-readable string explaining a fallback to pynput.
    """
    if backend is None:
        backend = _settings_backend()

    if backend == BACKEND_INTERCEPTION:
        try:
            import interception_backend as ib
            if ib.is_available():
                return (_NameBackendAdapter(ib.InterceptionKeyboard()),
                        BACKEND_INTERCEPTION, None)
            warn = ("Interception driver not available (install + reboot, then "
                    "run --identify) — using pynput; games may ignore input.")
        except Exception as e:  # pragma: no cover - import/driver edge
            warn = f"Interception backend failed ({e}) — using pynput."
        return _UnicodeTypingController(), BACKEND_PYNPUT, warn

    if backend == BACKEND_SENDINPUT:
        try:
            import sendinput_backend as sb
            return (_NameBackendAdapter(sb.ScancodeKeyboard()),
                    BACKEND_SENDINPUT, None)
        except Exception as e:  # pragma: no cover - non-Windows / import edge
            return (_UnicodeTypingController(), BACKEND_PYNPUT,
                    f"SendInput backend failed ({e}) — using pynput.")

    return _UnicodeTypingController(), BACKEND_PYNPUT, None
