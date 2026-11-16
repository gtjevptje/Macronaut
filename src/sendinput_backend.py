"""Scancode input backend (Windows SendInput, KEYEVENTF_SCANCODE).

Why this exists: pynput / pyautogui send keyboard input as virtual-key codes
through the Windows message queue. Many games (Ghost of Tsushima, most titles on
DirectInput / Raw Input) read the keyboard at the scancode level and ignore VK
input — so the normal Macronaut path does nothing in-game.

This module talks to `SendInput` directly with hardware **scancodes** (fixed
physical key positions), which those games do honour.

Key names match Macronaut's convention (lowercase strings: "w", "space",
"shift_l", "up", ...), so a `ScancodeKeyboard` can stand in for the pynput
`keyboard.Controller` used in flow_exec.py / recorder.py — but see the layout
note below the SCANCODES table: those names are QWERTY key-*position* names,
not necessarily what a non-QWERTY user typed.

Layout translation (`layout_scancode` / `qwerty_name_for_scancode`): key
capture elsewhere in Macronaut is character-based (pynput reports the typed
character under the user's active layout). On a Belgian AZERTY layout, typing
the letter printed "Z" reports the character "z" — but "z" in SCANCODES below
means the QWERTY-Z *position* (0x2C), which on AZERTY is physically the key
labelled "W". Left alone, that node would send the wrong physical key.
`layout_scancode(char)` resolves a captured character to the physical
scancode of the key that produces it under the *active* keyboard layout;
`qwerty_name_for_scancode(sc)` maps that scancode back to the QWERTY-position
name SCANCODES expects. `input_backends._NameBackendAdapter` chains the two
before handing a name to this module (and to interception_backend.py, which
also keys off QWERTY names) — this module's own table and CLI stay pure
QWERTY-position lookups with no translation of their own.

Standalone test (walk forward 3s in the focused game) — raw QWERTY-position
names, no layout translation applied:
    python sendinput_backend.py            # 2s to focus the game, then hold W 3s
    python sendinput_backend.py w a s d     # tap each key once, in order
    python sendinput_backend.py --hold w 3   # hold W for 3 seconds

If SendInput is blocked by UIPI (game running elevated, script not) it raises
PermissionError — run the script as administrator. If it returns success but
nothing moves in-game, the title is filtering injected input (rarer case).
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from typing import Optional

user32 = ctypes.WinDLL("user32", use_last_error=True)

# ── Keyboard-layout functions (used by layout_scancode) ───────────────────────
# HKL (keyboard layout handle) is a pointer-sized opaque handle.
HKL = wintypes.HANDLE

user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetKeyboardLayout.argtypes = [wintypes.DWORD]
user32.GetKeyboardLayout.restype = HKL
user32.VkKeyScanExW.argtypes = [wintypes.WCHAR, HKL]
user32.VkKeyScanExW.restype = ctypes.c_short  # SHORT: low byte VK, high byte shift state
user32.MapVirtualKeyExW.argtypes = [wintypes.UINT, wintypes.UINT, HKL]
user32.MapVirtualKeyExW.restype = wintypes.UINT

# ── Cursor / screen geometry (used by ScancodeMouse) ──────────────────────────
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int

MAPVK_VK_TO_VSC = 0

# ── SendInput flags ───────────────────────────────────────────────────────────
INPUT_MOUSE           = 0
INPUT_KEYBOARD        = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP       = 0x0002
KEYEVENTF_UNICODE     = 0x0004
KEYEVENTF_SCANCODE    = 0x0008

VK_RETURN = 0x0D
VK_SHIFT = 0x10
SHIFT_SCAN = 0x2A       # left shift

# One SendInput call carries at most this many events. Windows accepts far more,
# but a bounded chunk keeps a very long text step interruptible by Stop.
# Only the unpaced path uses it; the paced one is already one call per event.
TYPE_CHUNK_EVENTS = 512

# ── Per-character timing for Unicode typing ──────────────────────────────────
# The same reason the scancode path has it. A game reads its input once a frame
# (16.7 ms at 60 Hz) and takes a bounded amount of it per pass, so a whole
# string handed to Windows as one SendInput array lands inside a single frame
# and most of it is never looked at. Injection is exact either way — a
# low-level hook sees every event — but "sent" is not "read".
#
# ⚠ Do not batch this again to make typing fast. Handing the array over in one
# call was 2.0.14's regression: the text arrived complete in Notepad and mostly
# missing in a game chat, which is the split that names a receiver problem.
# ~33 ch/s is the price of being seen at all.
TYPE_CHAR_HOLD_S = 0.02
TYPE_CHAR_GAP_S = 0.01
# A modifier change needs its own frame before the key that depends on it: a
# receiver that samples SHIFT while processing a character would otherwise read
# the state from before the change landed, which is a capital arriving
# lowercase. Same going back down — a lowercase letter straight after a capital
# needs the release to have registered first.
TYPE_MOD_SETTLE_S = 0.02

MOUSEEVENTF_MOVE        = 0x0001
MOUSEEVENTF_LEFTDOWN    = 0x0002
MOUSEEVENTF_LEFTUP      = 0x0004
MOUSEEVENTF_RIGHTDOWN   = 0x0008
MOUSEEVENTF_RIGHTUP     = 0x0010
MOUSEEVENTF_MIDDLEDOWN  = 0x0020
MOUSEEVENTF_MIDDLEUP    = 0x0040
MOUSEEVENTF_XDOWN       = 0x0080
MOUSEEVENTF_XUP         = 0x0100
MOUSEEVENTF_WHEEL       = 0x0800
MOUSEEVENTF_HWHEEL      = 0x1000
MOUSEEVENTF_ABSOLUTE    = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000  # absolute coords span ALL monitors, not just the primary
XBUTTON1 = 0x0001
XBUTTON2 = 0x0002
# One detent of a real wheel. Windows measures the wheel in these, and an app
# that scrolls by "lines" is dividing by it — so sending 3 * WHEEL_DELTA in one
# event is not the same thing as three notches, and some receivers clamp it.
WHEEL_DELTA = 120

# GetSystemMetrics indices for the virtual screen (the bounding box of every
# monitor). Needed to normalise absolute moves — see ScancodeMouse.position.
SM_XVIRTUALSCREEN  = 76
SM_YVIRTUALSCREEN  = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

ULONG_PTR = ctypes.c_size_t  # pointer-sized integer (8 bytes on 64-bit)


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG),
                ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD),
                ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUTunion(ctypes.Union):
    # The union must be sized on its LARGEST member (MOUSEINPUT), or sizeof(INPUT)
    # is too small and SendInput silently rejects every call (cbSize mismatch).
    _fields_ = [("mi", MOUSEINPUT),
                ("ki", KEYBDINPUT),
                ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD),
                ("u", _INPUTunion)]


# ── Scancode table (Set 1), keyed by QWERTY key *position* names ─────────────
# value = scancode, or (scancode, extended=True) for keys that need the 0xE0
# prefix. Scancodes themselves are physical positions, but the *names* this
# table is indexed by are QWERTY names ("z" -> the QWERTY-Z position, 0x2C) —
# NOT the character a non-QWERTY user typed. A captured key name is the typed
# *character* under the user's active layout (pynput is character-based), so
# on Belgian AZERTY the letter printed "W" reports as "z" — looked up directly
# here that would send the QWERTY-Z key, physically the wrong key in-game.
# `layout_scancode()` / `qwerty_name_for_scancode()` below translate a captured
# character to the QWERTY-position name this table actually wants;
# input_backends._NameBackendAdapter does that translation before calling into
# this module (and before interception_backend.py, which is also keyed by
# QWERTY names). This module's own table/CLI never translate on their own.
_E = True  # extended-key marker for readability

SCANCODES: dict = {
    # Letters
    "a": 0x1E, "b": 0x30, "c": 0x2E, "d": 0x20, "e": 0x12, "f": 0x21,
    "g": 0x22, "h": 0x23, "i": 0x17, "j": 0x24, "k": 0x25, "l": 0x26,
    "m": 0x32, "n": 0x31, "o": 0x18, "p": 0x19, "q": 0x10, "r": 0x13,
    "s": 0x1F, "t": 0x14, "u": 0x16, "v": 0x2F, "w": 0x11, "x": 0x2D,
    "y": 0x15, "z": 0x2C,
    # Number row
    "1": 0x02, "2": 0x03, "3": 0x04, "4": 0x05, "5": 0x06,
    "6": 0x07, "7": 0x08, "8": 0x09, "9": 0x0A, "0": 0x0B,
    "-": 0x0C, "=": 0x0D, "[": 0x1A, "]": 0x1B, "\\": 0x2B,
    ";": 0x27, "'": 0x28, "`": 0x29, ",": 0x33, ".": 0x34, "/": 0x35,
    # Whitespace / editing
    "space": 0x39, "enter": 0x1C, "return": 0x1C, "tab": 0x0F,
    "esc": 0x01, "escape": 0x01, "backspace": 0x0E,
    # Modifiers
    "shift": 0x2A, "shift_l": 0x2A, "shift_r": 0x36,
    "ctrl": 0x1D, "ctrl_l": 0x1D, "ctrl_r": (0x1D, _E),
    "alt": 0x38, "alt_l": 0x38, "alt_r": (0x38, _E),
    "caps_lock": 0x3A, "capslock": 0x3A,
    # Function keys
    "f1": 0x3B, "f2": 0x3C, "f3": 0x3D, "f4": 0x3E, "f5": 0x3F, "f6": 0x40,
    "f7": 0x41, "f8": 0x42, "f9": 0x43, "f10": 0x44, "f11": 0x57, "f12": 0x58,
    # Extended navigation (require the 0xE0 prefix)
    "up": (0x48, _E), "down": (0x50, _E), "left": (0x4B, _E), "right": (0x4D, _E),
    "home": (0x47, _E), "end": (0x4F, _E),
    "page_up": (0x49, _E), "pageup": (0x49, _E),
    "page_down": (0x51, _E), "pagedown": (0x51, _E),
    "insert": (0x52, _E), "delete": (0x53, _E), "del": (0x53, _E),
    "win": (0x5B, _E), "cmd": (0x5B, _E),
}


# ── Reverse table: scancode -> QWERTY-position name ───────────────────────────
# Built once, over plain-int entries only (tuple/extended entries have no
# single unambiguous "name" that matters for the layout-translation use case).
# When several names share a scancode (e.g. "enter"/"return" both 0x1C),
# single-character names win — that's what qwerty_name_for_scancode() is for —
# and the first name of a kind (single-char, then any) wins ties.
_REVERSE_SCANCODES: dict = {}
for _name, _entry in SCANCODES.items():
    if isinstance(_entry, tuple):
        continue
    if len(_name) == 1 and _entry not in _REVERSE_SCANCODES:
        _REVERSE_SCANCODES[_entry] = _name
for _name, _entry in SCANCODES.items():
    if isinstance(_entry, tuple):
        continue
    if _entry not in _REVERSE_SCANCODES:
        _REVERSE_SCANCODES[_entry] = _name
del _name, _entry


_layout_scancode_cache: dict = {}
_layout_keystroke_cache: dict = {}


def _active_hkl() -> int:
    """Keyboard layout handle of the foreground window's thread, falling back
    to the calling thread's own layout if there's no foreground window."""
    hkl = 0
    hwnd = user32.GetForegroundWindow()
    if hwnd:
        pid = wintypes.DWORD(0)
        tid = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if tid:
            hkl = user32.GetKeyboardLayout(tid) or 0
    if not hkl:
        hkl = user32.GetKeyboardLayout(0) or 0
    return hkl


def layout_scancode(char: str) -> Optional[int]:
    """Physical scancode of the key that produces `char` under the *active*
    keyboard layout (the layout of the foreground window, i.e. the game).

    Returns None if `char` isn't a single character, this isn't Windows, or
    the layout API can't resolve it — callers fall back to the QWERTY
    SCANCODES table (status quo behaviour) in that case.
    """
    if sys.platform != "win32" or not isinstance(char, str) or len(char) != 1:
        return None
    try:
        hkl = _active_hkl()
        cache_key = (char, hkl)
        if cache_key in _layout_scancode_cache:
            return _layout_scancode_cache[cache_key]

        vkres = user32.VkKeyScanExW(ctypes.c_wchar(char), hkl)
        # VkKeyScanExW returns -1 (0xFFFF as an unsigned WORD) if the layout
        # has no key for this character.
        if vkres == -1 or (vkres & 0xFFFF) == 0xFFFF:
            _layout_scancode_cache[cache_key] = None
            return None

        vk = vkres & 0xFF  # low byte: the VK code; high byte is shift state, ignored
        sc = user32.MapVirtualKeyExW(vk, MAPVK_VK_TO_VSC, hkl)
        result = sc or None
        _layout_scancode_cache[cache_key] = result
        return result
    except Exception:
        return None


def layout_keystroke(char: str) -> Optional[tuple]:
    """`char` -> (scancode, shift, ctrl, alt) on the *active* layout, or None.

    layout_scancode() throws the shift state away, because its callers only
    need the key position. Typing needs it: on AZERTY a digit is a *shifted*
    key, so pressing the scancode alone types the punctuation printed on it
    instead. Ctrl+Alt together is how Windows spells AltGr.

    None means the layout has no single keystroke for this character — an
    unavailable character, or one that needs a dead-key sequence. Callers fall
    back to Unicode injection for it.
    """
    if sys.platform != "win32" or not isinstance(char, str) or len(char) != 1:
        return None
    try:
        hkl = _active_hkl()
        cache_key = (char, hkl)
        if cache_key in _layout_keystroke_cache:
            return _layout_keystroke_cache[cache_key]

        vkres = user32.VkKeyScanExW(ctypes.c_wchar(char), hkl)
        if vkres == -1 or (vkres & 0xFFFF) == 0xFFFF:
            _layout_keystroke_cache[cache_key] = None
            return None

        vk, shift_state = vkres & 0xFF, (vkres >> 8) & 0xFF
        sc = user32.MapVirtualKeyExW(vk, MAPVK_VK_TO_VSC, hkl)
        result = (sc, bool(shift_state & 1), bool(shift_state & 2),
                  bool(shift_state & 4)) if sc else None
        _layout_keystroke_cache[cache_key] = result
        return result
    except Exception:
        return None


def layout_family() -> str:
    """Name the active layout by where its letters physically sit.

    Read from the layout itself rather than from the locale, because that is
    the property that actually matters here: two layouts with different
    language ids can put the letters in the same places. Returns "QWERTY",
    "AZERTY", "QWERTZ", or "" when it cannot be determined.
    """
    a, z, y = layout_scancode("a"), layout_scancode("z"), layout_scancode("y")
    if a == 0x10:
        return "AZERTY"          # A where QWERTY has Q
    if a == 0x1E:
        if z == 0x15:            # Z where QWERTY has Y
            return "QWERTZ"
        if z == 0x2C and y == 0x15:
            return "QWERTY"
    return ""


def qwerty_name_for_scancode(sc: int) -> Optional[str]:
    """Physical scancode -> QWERTY-position key name (reverse of SCANCODES,
    over plain-int entries only). None if no QWERTY name maps to this
    scancode (extended/navigation keys aren't in the reverse table)."""
    return _REVERSE_SCANCODES.get(sc)


def _resolve(name: str):
    """name -> (scancode, extended). Raises KeyError for unmapped names."""
    entry = SCANCODES[name.lower().strip()]
    if isinstance(entry, tuple):
        return entry[0], True
    return entry, False


def _send_scan(scancode: int, keyup: bool = False, extended: bool = False) -> None:
    flags = KEYEVENTF_SCANCODE
    if keyup:
        flags |= KEYEVENTF_KEYUP
    if extended:
        flags |= KEYEVENTF_EXTENDEDKEY
    ki = KEYBDINPUT(0, scancode, flags, 0, 0)
    inp = INPUT(INPUT_KEYBOARD, _INPUTunion(ki=ki))
    n = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    if n != 1:  # 0 == the OS refused the injection
        raise ctypes.WinError(ctypes.get_last_error())


def _needs_shift(ch: str) -> bool:
    """Should a real SHIFT be held while this character is injected?

    Uppercase letters only — deliberately narrower than "which keys would a
    person press". These are Unicode packets: the receiver is handed the
    character itself, so the only thing SHIFT can still change is a receiver
    that overrides *case* from GetKeyState(VK_SHIFT), and case is a property of
    letters. Mirroring the layout's full shift state instead would hold SHIFT
    for AZERTY digits and punctuation, where it buys nothing and risks a game
    reading Shift+1 as a bound shortcut.

    The scancode path is the opposite case and keeps using `layout_keystroke`:
    it presses real key positions, so it needs the real shift state to produce
    the right character at all.
    """
    return ch.isupper()


def _shift_events(down: bool) -> list:
    """A real SHIFT press or release, carrying both its vk and its scancode so
    that message-queue readers and raw-input readers both see it."""
    flags = 0 if down else KEYEVENTF_KEYUP
    ki = KEYBDINPUT(VK_SHIFT, SHIFT_SCAN, flags, 0, 0)
    return [INPUT(INPUT_KEYBOARD, _INPUTunion(ki=ki))]


def _text_groups(text: str, shift_for_capitals: bool = False,
                 timing: tuple = None) -> list:
    """`text` -> [(events, pause_after_seconds), ...], in order.

    One group is one thing the receiver has to notice: a SHIFT transition, a
    key going down, a key coming up. Each carries the pause that must follow it
    before the next group is sent, so a frame-polled target gets a chance to
    read every one. `_text_events` flattens this back for callers that only
    care about what is sent, not when.

    Characters go out as KEYEVENTF_UNICODE, which carries the codepoint itself
    rather than a key position — so it is layout-independent and needs no
    AltGr dance. That matters on AZERTY, where digits and most punctuation are
    shifted keys.

    ⚠ `shift_for_capitals` is **off by default, and that default is a scar.**
    A receiver that decides case from GetKeyState(VK_SHIFT) instead of from the
    character it was handed lowercases every capital, and holding a real SHIFT
    fixes that — it is free for a receiver that reads the codepoint, since the
    codepoint already says 'A'. But injecting SHIFT is the one thing 2.0.14 and
    2.0.15 both added, and both of those lost text in the *same* game chat that
    plain Unicode typing had always filled correctly. Delivering the whole line
    with the wrong case beats delivering two characters of it, so the capitals
    workaround is opt-in (Settings → typing) and delivery is the default.

    Only SHIFT is ever mirrored, never CTRL or ALT. SHIFT cannot turn a
    character into a command; Ctrl+key can, so mirroring AltGr would fire
    shortcuts. The scancode path is unaffected either way: it presses real key
    positions, so it needs the real shift state to produce a capital at all.

    Line breaks are the other exception and must be a real VK_RETURN. Windows
    maps the *character* U+000A to Ctrl+Enter (VkKeyScanExW('\\n') -> 0x020D on
    a Belgian layout: vk 0x0D with the CTRL bit), and apps read Ctrl+Enter as
    "send", not "new line".
    """
    # `timing` is (hold, gap, settle) — the slot the caller wants each character
    # to occupy, from input_backends.type_timing. Defaulted rather than required
    # so this stays usable on its own; the app always passes one.
    hold, gap, settle = timing or (TYPE_CHAR_HOLD_S, TYPE_CHAR_GAP_S,
                                   TYPE_MOD_SETTLE_S)
    groups = []
    shift_held = False

    def _shift(down: bool) -> None:
        nonlocal shift_held
        groups.append((_shift_events(down), settle))
        shift_held = down

    def _tap(ki_down, ki_up) -> None:
        groups.append(([INPUT(INPUT_KEYBOARD, _INPUTunion(ki=ki_down))], hold))
        groups.append(([INPUT(INPUT_KEYBOARD, _INPUTunion(ki=ki_up))], gap))

    for ch in text.replace("\r\n", "\n"):
        if ch in ("\n", "\r"):
            if shift_held:
                _shift(False)
            _tap(KEYBDINPUT(VK_RETURN, 0, 0, 0, 0),
                 KEYBDINPUT(VK_RETURN, 0, KEYEVENTF_KEYUP, 0, 0))
            continue
        want = shift_for_capitals and _needs_shift(ch)
        if want != shift_held:
            _shift(want)
        # Astral characters need their two UTF-16 surrogates sent in order.
        buf = ch.encode("utf-16-le")
        for i in range(0, len(buf), 2):
            unit = buf[i] | (buf[i + 1] << 8)
            _tap(KEYBDINPUT(0, unit, KEYEVENTF_UNICODE, 0, 0),
                 KEYBDINPUT(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0))
    if shift_held:
        _shift(False)
    return groups


def _text_events(text: str, shift_for_capitals: bool = False) -> list:
    """`_text_groups` flattened — every INPUT struct, in order, no timing."""
    return [ev
            for events, _pause in _text_groups(text, shift_for_capitals)
            for ev in events]


def type_text(text: str, should_continue=None, paced: bool = True,
              shift_for_capitals: bool = False, timing: tuple = None) -> int:
    """Type `text` through SendInput. Returns the number of events accepted.

    Paced by default: one event per call, with the pause each group asks for.
    That is ~33 ch/s, and it is the rate at which a game actually *reads* what
    is sent — see TYPE_CHAR_HOLD_S. Anything faster is delivered to Windows
    perfectly and then dropped by the target, which looks identical to a bug in
    this file and is not one.

    `paced=False` restores the old single-array injection for a receiver known
    to drain its whole queue (a Win32 EDIT, Qt, a terminal). Nothing in the app
    asks for it; it exists so a bench can measure the difference.

    `should_continue` is polled before every group, so Stop cuts a long text
    short within one character rather than waiting out the string.
    """
    groups = _text_groups(text, shift_for_capitals, timing)
    # Only worth a safety release if this call could have pressed SHIFT at all.
    # It used to run unconditionally, which is invisible for one call and awful
    # for the per-character path: typing 156 characters one call at a time
    # sprayed 156 SHIFT releases at the target for no reason.
    pressed_shift = shift_for_capitals and any(_needs_shift(c) for c in text)
    sent = 0
    try:
        if paced:
            # Each pause is spent as "wait until this group's slot is up", not
            # as a fixed sleep. time.sleep overshoots by a few tenths of a
            # millisecond and there are two per character — invisible at 33 ch/s
            # and a 10% shortfall at 200, all of it in the same direction.
            due = time.perf_counter()
            for events, pause in groups:
                if should_continue is not None and not should_continue():
                    break
                sent += _send_all(events)
                if pause:
                    due += pause
                    remaining = due - time.perf_counter()
                    if remaining > 0:
                        time.sleep(remaining)
                    else:
                        # Overran its slot — reset rather than making the groups
                        # after it race to catch up.
                        due = time.perf_counter()
        else:
            events = [ev for evs, _p in groups for ev in evs]
            for i in range(0, len(events), TYPE_CHUNK_EVENTS):
                if should_continue is not None and not should_continue():
                    break
                sent += _send_all(events[i:i + TYPE_CHUNK_EVENTS])
    finally:
        # Only when this call actually pressed SHIFT: stopping part-way — or
        # failing mid-array — can leave it down. A stuck SHIFT
        # capitalises everything the user types next and is far worse than the
        # unfinished text. Still unconditional *within* such a call — releasing
        # a key already up is harmless, and tracking whether it survived an
        # aborted chunk is not worth being wrong about.
        if pressed_shift:
            _release_shift()
    return sent


def _send_all(events: list) -> int:
    """Hand `events` to SendInput as one array. Returns how many it accepted."""
    if not events:
        return 0
    arr = (INPUT * len(events))(*events)
    n = user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))
    if n != len(events):
        raise ctypes.WinError(ctypes.get_last_error())
    return n


def _release_shift() -> None:
    try:
        arr = (INPUT * 1)(*_shift_events(False))
        user32.SendInput(1, arr, ctypes.sizeof(INPUT))
    except Exception:
        pass


class ScancodeKeyboard:
    """Drop-in-ish keyboard that sends hardware scancodes via SendInput.

    Mirrors the pynput keyboard.Controller surface used in the codebase
    (`press`/`release`), but takes Macronaut key-name strings.
    """

    # Every key leaves here as a key *position*, so a target reading raw input
    # or DirectInput sees it. input_backends reads this to decide that typing
    # must travel the same road, rather than as KEYEVENTF_UNICODE.
    sends_scancodes = True

    def maps_key(self, name: str) -> bool:
        """True if this backend can send the key (input_backends routing probe)."""
        return name.lower().strip() in SCANCODES

    def press(self, name: str) -> None:
        sc, ext = _resolve(name)
        _send_scan(sc, keyup=False, extended=ext)

    def release(self, name: str) -> None:
        sc, ext = _resolve(name)
        _send_scan(sc, keyup=True, extended=ext)

    def tap(self, name: str, hold: float = 0.05) -> None:
        """Press, hold briefly, release. The hold matters: an instant down+up is
        often missed by a game's input polling — 50ms lets it see a few frames."""
        sc, ext = _resolve(name)
        _send_scan(sc, keyup=False, extended=ext)
        time.sleep(hold)
        _send_scan(sc, keyup=True, extended=ext)


# ── Mouse ─────────────────────────────────────────────────────────────────────
# name -> (down flag, up flag, mouseData). The X buttons share one flag pair and
# are told apart by mouseData, unlike every other button.
_MOUSE_BUTTONS: dict = {
    "left":   (MOUSEEVENTF_LEFTDOWN,   MOUSEEVENTF_LEFTUP,   0),
    "right":  (MOUSEEVENTF_RIGHTDOWN,  MOUSEEVENTF_RIGHTUP,  0),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP, 0),
    "x1":     (MOUSEEVENTF_XDOWN,      MOUSEEVENTF_XUP,      XBUTTON1),
    "x2":     (MOUSEEVENTF_XDOWN,      MOUSEEVENTF_XUP,      XBUTTON2),
}


def _wheel_data(delta: int) -> int:
    """A signed wheel delta as MOUSEINPUT.mouseData wants it.

    ⚠ mouseData is a DWORD — *unsigned*. ctypes refuses a negative int for one,
    so scrolling down (the commonest direction there is) would raise rather than
    scroll. Windows reads the field back as a signed short, so two's complement
    is what it wants.
    """
    return int(delta) & 0xFFFFFFFF


def _send_mouse(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> None:
    mi = MOUSEINPUT(dx, dy, data, flags, 0, 0)
    inp = INPUT(INPUT_MOUSE, _INPUTunion(mi=mi))
    n = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    if n != 1:  # 0 == the OS refused the injection
        raise ctypes.WinError(ctypes.get_last_error())


def _cursor_pos() -> tuple:
    pt = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(pt)):
        raise ctypes.WinError(ctypes.get_last_error())
    return pt.x, pt.y


def _to_absolute(x: int, y: int) -> tuple:
    """Screen pixels -> the 0..65535 normalised coordinates SendInput wants.

    Normalised against the whole *virtual* screen (paired with
    MOUSEEVENTF_VIRTUALDESK), not the primary monitor: on a multi-monitor setup
    normalising against the primary would clamp every click onto it, and
    secondary-monitor coordinates are negative on a left-hand monitor.
    """
    vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    vw = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    vh = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    nx = 0 if vw <= 1 else round((x - vx) * 65535 / (vw - 1))
    ny = 0 if vh <= 1 else round((y - vy) * 65535 / (vh - 1))
    # Clamp: an off-screen target would otherwise wrap in the 16-bit field.
    return max(0, min(65535, nx)), max(0, min(65535, ny))


class ScancodeMouse:
    """Mouse that sends hardware-style events via SendInput.

    Mirrors the pynput mouse.Controller surface the engines use (`position`
    get/set, `press`, `release`, `click`) but takes Macronaut button-name
    strings. Reading `position` goes through GetCursorPos — SendInput is a
    write-only path, and the OS cursor is what the game reads too.

    No method sleeps: Macronaut owns click timing (interval, hold, human mode).
    """

    def maps_button(self, name: str) -> bool:
        """True if this backend can send the button (routing probe)."""
        return str(name).lower().strip() in _MOUSE_BUTTONS

    @property
    def position(self) -> tuple:
        return _cursor_pos()

    @position.setter
    def position(self, xy) -> None:
        x, y = xy
        nx, ny = _to_absolute(int(x), int(y))
        _send_mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
                    | MOUSEEVENTF_VIRTUALDESK, nx, ny)

    def _resolve(self, name) -> tuple:
        spec = _MOUSE_BUTTONS.get(str(name).lower().strip())
        if spec is None:
            raise ValueError(f"unknown mouse button: {name!r}")
        return spec

    def press(self, name) -> None:
        down, _up, data = self._resolve(name)
        _send_mouse(down, data=data)

    def release(self, name) -> None:
        _down, up, data = self._resolve(name)
        _send_mouse(up, data=data)

    def click(self, name, count: int = 1) -> None:
        """Press/release `count` times back-to-back. Callers needing a gap
        between clicks (double-click timing, CPS pacing) space them themselves."""
        down, up, data = self._resolve(name)
        for _ in range(max(1, int(count))):
            _send_mouse(down, data=data)
            _send_mouse(up, data=data)

    def scroll(self, dx: int = 0, dy: int = 0) -> None:
        """Wheel by whole notches. Signs follow pynput: +dy up, +dx right.

        One event per notch, because that is what a real wheel produces — each
        detent is its own WM_MOUSEWHEEL. Rolling the whole distance into one
        event is the same mistake batching made with typed text: it is accepted
        by Windows either way, and a receiver that reads input once a frame and
        takes a bounded amount per pass sees a fraction of it.
        """
        for flags, n in ((MOUSEEVENTF_WHEEL, int(dy)),
                         (MOUSEEVENTF_HWHEEL, int(dx))):
            step = WHEEL_DELTA if n > 0 else -WHEEL_DELTA
            for _ in range(abs(n)):
                _send_mouse(flags, data=_wheel_data(step))


# Module-level convenience singleton + functions.
_kb = ScancodeKeyboard()
press = _kb.press
release = _kb.release
tap = _kb.tap


def _cli(argv: list) -> int:
    kb = ScancodeKeyboard()

    if argv and argv[0] == "--hold":
        if len(argv) < 2:
            print("usage: --hold <key> [seconds]")
            return 2
        name = argv[1]
        seconds = float(argv[2]) if len(argv) > 2 else 3.0
        print(f"Focus the game... holding {name!r} for {seconds}s in 2s")
        time.sleep(2)
        try:
            kb.press(name)
            time.sleep(seconds)
            kb.release(name)
        except PermissionError as e:
            print(f"BLOCKED by UIPI ({e}) -> run this script as administrator.")
            return 1
        print("Done (SendInput accepted). If nothing moved in-game: injection filtered.")
        return 0

    keys = argv or ["w"]
    default = not argv
    print(f"Focus the game... {'holding W 3s' if default else 'tapping ' + ' '.join(keys)} in 2s")
    time.sleep(2)
    try:
        if default:
            kb.press("w")
            time.sleep(3)
            kb.release("w")
        else:
            for name in keys:
                if name.lower() not in SCANCODES:
                    print(f"  (skip: no scancode for {name!r})")
                    continue
                kb.tap(name)
                time.sleep(0.15)
    except PermissionError as e:
        print(f"BLOCKED by UIPI ({e}) -> run this script as administrator.")
        return 1
    print("Done (SendInput accepted). If nothing happened in-game: injection filtered.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
