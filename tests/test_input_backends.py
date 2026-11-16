"""Headless tests for input_backends.py — the selectable input-send backends.

NOTE these tests never send real input. Anything that would reach the OS
(clicks, moves, keystrokes) is replaced with a recording fake — including the
adapters' internal pynput fallback controller, which would otherwise click the
developer's actual desktop when a fallback path is exercised.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from pynput.keyboard import Key, KeyCode, Controller as PynputController
from pynput.mouse import Button, Controller as PynputMouse

import input_backends as ib
import interception_backend
import sendinput_backend as sb
import settings


class _FakeMouse:
    """Records calls instead of sending input. Stands in for both a name-string
    backend (ScancodeMouse / InterceptionMouse) and the pynput fallback."""

    def __init__(self, maps=("left", "right", "middle"), fail=False):
        self.calls = []
        self._maps = set(maps)
        self._fail = fail
        self._pos = (7, 9)

    def maps_button(self, name):
        return name in self._maps

    @property
    def position(self):
        if self._fail:
            raise OSError("simulated backend failure")
        return self._pos

    @position.setter
    def position(self, xy):
        if self._fail:
            raise OSError("simulated backend failure")
        self._pos = xy
        self.calls.append(("move", xy))

    def press(self, name):
        self.calls.append(("press", name))

    def release(self, name):
        self.calls.append(("release", name))

    def click(self, name, count=1):
        self.calls.append(("click", name, count))


def _adapter(impl=None, fallback=None):
    """A _MouseBackendAdapter with BOTH sides faked — nothing reaches the OS."""
    a = ib._MouseBackendAdapter(impl if impl is not None else _FakeMouse())
    a._pynput = fallback if fallback is not None else _FakeMouse()
    return a


# ── _key_to_name ──────────────────────────────────────────────────────────────
def test_key_to_name_named_key():
    assert ib._key_to_name(Key.space) == "space"
    assert ib._key_to_name(Key.ctrl_l) == "ctrl_l"


def test_key_to_name_char_keycode():
    assert ib._key_to_name(KeyCode.from_char("w")) == "w"


def test_key_to_name_none_char_keycode():
    kc = KeyCode(vk=96)  # numpad-style vk with no printable char
    assert kc.char is None
    assert ib._key_to_name(kc) is None


# ── make_keyboard ──────────────────────────────────────────────────────────────
def test_make_keyboard_pynput():
    kb, actual, warn = ib.make_keyboard("pynput")
    assert isinstance(kb, PynputController)
    assert actual == "pynput"
    assert warn is None


def test_make_keyboard_sendinput():
    kb, actual, warn = ib.make_keyboard("sendinput")
    assert actual == "sendinput"
    assert kb._impl.maps_key("w") is True
    assert kb._impl.maps_key("media_mute") is False


def test_make_keyboard_unknown_falls_back_to_pynput():
    kb, actual, warn = ib.make_keyboard("some-garbage-backend")
    assert isinstance(kb, PynputController)
    assert actual == "pynput"


def test_make_keyboard_interception_unavailable_falls_back(monkeypatch):
    monkeypatch.setattr(interception_backend, "is_available", lambda: False)
    kb, actual, warn = ib.make_keyboard("interception")
    assert isinstance(kb, PynputController)
    assert actual == "pynput"
    assert isinstance(warn, str) and warn


# ── _button_to_name ───────────────────────────────────────────────────────────
def test_button_to_name_known_buttons():
    assert ib._button_to_name(Button.left) == "left"
    assert ib._button_to_name(Button.right) == "right"
    assert ib._button_to_name(Button.middle) == "middle"


def test_button_to_name_unknown_button_is_none():
    # Button.unknown must route to pynput, not be sent as a literal "unknown".
    assert ib._button_to_name(Button.unknown) is None


def test_button_to_name_passes_strings_through():
    assert ib._button_to_name("left") == "left"
    assert ib._button_to_name(object()) is None


# ── make_mouse ────────────────────────────────────────────────────────────────
def test_make_mouse_pynput():
    ms, actual, warn = ib.make_mouse("pynput")
    assert isinstance(ms, PynputMouse)
    assert actual == "pynput"
    assert warn is None


def test_make_mouse_sendinput():
    ms, actual, warn = ib.make_mouse("sendinput")
    assert actual == "sendinput"
    assert ms._impl.maps_button("left") is True
    assert ms._impl.maps_button("scrollwheel") is False


def test_make_mouse_unknown_falls_back_to_pynput():
    ms, actual, warn = ib.make_mouse("some-garbage-backend")
    assert isinstance(ms, PynputMouse)
    assert actual == "pynput"


def test_make_mouse_interception_unavailable_falls_back(monkeypatch):
    monkeypatch.setattr(interception_backend, "mouse_available", lambda: False)
    ms, actual, warn = ib.make_mouse("interception")
    assert isinstance(ms, PynputMouse)
    assert actual == "pynput"
    assert isinstance(warn, str) and warn


def test_make_mouse_interception_warns_when_slot_not_identified(monkeypatch):
    # The silent-failure case worth shouting about: driver works, but the mouse
    # device slot was never identified, so auto-detection might pick a virtual
    # mouse and every click vanishes. Backend still engages; warning is advisory.
    monkeypatch.setattr(interception_backend, "mouse_available", lambda: True)
    monkeypatch.setattr(interception_backend, "_load_saved_slot",
                        lambda kind="keyboard": None)
    ms, actual, warn = ib.make_mouse("interception")
    assert actual == "interception"
    assert warn and "--identify-mouse" in warn


def test_make_mouse_interception_no_warning_once_identified(monkeypatch):
    monkeypatch.setattr(interception_backend, "mouse_available", lambda: True)
    monkeypatch.setattr(interception_backend, "_load_saved_slot",
                        lambda kind="keyboard": 10)
    ms, actual, warn = ib.make_mouse("interception")
    assert actual == "interception"
    assert warn is None


# ── _MouseBackendAdapter routing ──────────────────────────────────────────────
def test_mouse_adapter_routes_mapped_button_to_backend():
    impl, fallback = _FakeMouse(), _FakeMouse()
    a = _adapter(impl, fallback)
    a.press(Button.left)
    a.release(Button.left)
    assert impl.calls == [("press", "left"), ("release", "left")]
    assert fallback.calls == []


def test_mouse_adapter_click_passes_count_through():
    impl = _FakeMouse()
    _adapter(impl).click(Button.left, 2)
    assert impl.calls == [("click", "left", 2)]


def test_mouse_adapter_unmapped_button_falls_back_to_pynput():
    impl = _FakeMouse(maps=("left",))          # backend can't do middle-click
    fallback = _FakeMouse()
    a = _adapter(impl, fallback)
    a.press(Button.middle)
    assert impl.calls == []
    assert fallback.calls == [("press", Button.middle)]


def test_mouse_adapter_press_release_route_consistently():
    # A button held via the fallback must be RELEASED via the fallback, or it
    # stays stuck down.
    impl = _FakeMouse(maps=("left",))
    fallback = _FakeMouse()
    a = _adapter(impl, fallback)
    a.press(Button.right)
    a.release(Button.right)
    assert impl.calls == []
    assert [c[0] for c in fallback.calls] == ["press", "release"]


def test_mouse_adapter_move_failure_falls_back():
    # A failed move must still land the cursor: a click at a stale position is
    # a click in the wrong place.
    impl, fallback = _FakeMouse(fail=True), _FakeMouse()
    a = _adapter(impl, fallback)
    a.position = (300, 400)
    assert fallback.calls == [("move", (300, 400))]


def test_mouse_adapter_position_read_falls_back():
    impl, fallback = _FakeMouse(fail=True), _FakeMouse()
    assert _adapter(impl, fallback).position == fallback._pos


def test_mouse_adapter_backend_error_falls_back_to_pynput():
    class _Broken(_FakeMouse):
        def press(self, name):
            raise OSError("injection refused")

    impl, fallback = _Broken(), _FakeMouse()
    a = _adapter(impl, fallback)
    a.press(Button.left)
    assert fallback.calls == [("press", Button.left)]


# ── interception device-slot config ───────────────────────────────────────────
def _use_tmp_cfg(monkeypatch, tmp_path):
    monkeypatch.setattr(interception_backend, "_CFG_DIR", tmp_path)
    monkeypatch.setattr(interception_backend, "_CFG_FILE",
                        tmp_path / "interception.json")


def test_save_mouse_slot_preserves_saved_keyboard(monkeypatch, tmp_path):
    # The bug this guards: identifying the mouse used to rewrite the whole file
    # and silently drop the keyboard slot the user had already identified.
    _use_tmp_cfg(monkeypatch, tmp_path)
    interception_backend._save_slot(1, "KB-HWID", "keyboard")
    interception_backend._save_slot(10, "MS-HWID", "mouse")
    assert interception_backend._load_saved("keyboard") == (1, "KB-HWID")
    assert interception_backend._load_saved("mouse") == (10, "MS-HWID")


def test_legacy_keyboard_only_config_still_loads(monkeypatch, tmp_path):
    # Configs written before mouse support have no mouse keys at all.
    _use_tmp_cfg(monkeypatch, tmp_path)
    (tmp_path / "interception.json").write_text(
        json.dumps({"keyboard_device": 1, "hwid": "KB-HWID"}), encoding="utf-8")
    assert interception_backend._load_saved("keyboard") == (1, "KB-HWID")
    assert interception_backend._load_saved("mouse") == (None, None)


def test_load_saved_rejects_out_of_class_slot(monkeypatch, tmp_path):
    # Mice live in slots 10-19; a keyboard-range number in the mouse field is
    # corrupt, and using it would send clicks to a keyboard device.
    _use_tmp_cfg(monkeypatch, tmp_path)
    (tmp_path / "interception.json").write_text(
        json.dumps({"mouse_device": 3, "mouse_hwid": "X"}), encoding="utf-8")
    assert interception_backend._load_saved("mouse") == (None, None)


def test_load_saved_tolerates_corrupt_config(monkeypatch, tmp_path):
    _use_tmp_cfg(monkeypatch, tmp_path)
    (tmp_path / "interception.json").write_text("{not json", encoding="utf-8")
    assert interception_backend._load_saved("keyboard") == (None, None)
    assert interception_backend._read_cfg() == {}


# ── settings integration ────────────────────────────────────────────────────────
def test_app_settings_default_input_backend():
    s = settings.AppSettings()
    assert s.input_backend == "pynput"


# ── AZERTY layout bug: character capture vs QWERTY-position scancode tables ──
# See sendinput_backend.py / input_backends.translate_name() for the full
# writeup: capture is character-based (pynput), but the scancode backends
# (SendInput, Interception) address keys by fixed QWERTY position — those
# aren't the same string on non-QWERTY layouts.

def test_qwerty_name_for_scancode_known_positions():
    # 0x11 / 0x2C are the physical QWERTY-W / QWERTY-Z scancodes (Set 1).
    assert sb.qwerty_name_for_scancode(0x11) == "w"
    assert sb.qwerty_name_for_scancode(0x2C) == "z"


def test_translate_name_multi_char_names_pass_through():
    # Multi-character names are position identifiers already, never typed
    # characters — translate_name() must never touch them, on any platform.
    for name in ("space", "f5", "shift_l", "up", "page_down", "enter"):
        assert ib.translate_name(name) == name


def test_translate_name_unavailable_passes_single_char_through(monkeypatch):
    # If layout resolution isn't available (non-Windows / API failure), a
    # single-character name must still pass through unchanged (status quo).
    monkeypatch.setattr(ib, "layout_scancode", None)
    monkeypatch.setattr(ib, "qwerty_name_for_scancode", None)
    assert ib.translate_name("z") == "z"


if sys.platform == "win32":
    def test_layout_scancode_resolves_a_on_windows():
        sc = sb.layout_scancode("a")
        assert isinstance(sc, int) and sc != 0

    def test_layout_scancode_rejects_multi_char():
        assert sb.layout_scancode("ab") is None
        assert sb.layout_scancode("space") is None

    # ── absolute-move normalisation (multi-monitor) ────────────────────────────
    def test_to_absolute_is_in_range_and_monotonic():
        # Whatever the monitor arrangement, output must stay inside the 16-bit
        # field and grow with x — an overflow here wraps clicks to the far edge.
        lo = sb._to_absolute(0, 0)
        hi = sb._to_absolute(10_000, 10_000)
        for pair in (lo, hi):
            assert all(0 <= v <= 65535 for v in pair)
        assert hi[0] >= lo[0] and hi[1] >= lo[1]

    def test_to_absolute_clamps_offscreen_targets():
        assert sb._to_absolute(-100_000, -100_000) == (0, 0)
        assert sb._to_absolute(100_000, 100_000) == (65535, 65535)

    def test_to_absolute_maps_virtual_screen_origin_to_zero():
        # The virtual screen's own top-left is normalised coordinate (0, 0),
        # NOT the primary monitor's — a left-hand second monitor has negative
        # x, and normalising against the primary would clamp it away.
        vx = sb.user32.GetSystemMetrics(sb.SM_XVIRTUALSCREEN)
        vy = sb.user32.GetSystemMetrics(sb.SM_YVIRTUALSCREEN)
        assert sb._to_absolute(vx, vy) == (0, 0)

    def test_scancode_mouse_reads_real_cursor_position():
        # Reading is a safe, side-effect-free OS call (no injection).
        x, y = sb.ScancodeMouse().position
        assert isinstance(x, int) and isinstance(y, int)


# ── Batched text injection (2.0.13) ──────────────────────────────────────────
def _ki(events):
    """(vk, scan, flags) for each INPUT struct, for readable assertions."""
    return [(e.u.ki.wVk, e.u.ki.wScan, e.u.ki.dwFlags) for e in events]


def test_a_newline_is_a_real_enter_key_not_a_character():
    """VkKeyScanExW('\\n') is 0x020D on a Belgian layout -- vk 0x0D with the CTRL
    bit -- so a newline sent as a *character* arrives as Ctrl+Enter, which apps
    read as "send", not "new line"."""
    ev = _ki(sb._text_events("\n"))
    assert len(ev) == 2, "one key down + one key up"
    assert all(vk == sb.VK_RETURN for vk, _s, _f in ev)
    assert not any(f & sb.KEYEVENTF_UNICODE for _v, _s, f in ev), \
        "a line break must not travel as a Unicode character"
    assert ev[1][2] & sb.KEYEVENTF_KEYUP


def test_crlf_is_one_enter_not_two():
    assert len(sb._text_events("a\r\nb")) == 6   # a, Enter, b


def test_characters_carry_their_codepoint_and_no_altgr():
    """KEYEVENTF_UNICODE carries the codepoint, so no AltGr dance is needed --
    which is what made AZERTY punctuation fragile when it went by position.
    SHIFT is mirrored for real now, but CTRL and ALT never are: SHIFT cannot
    turn a character into a command, Ctrl+key can."""
    ev = _ki(sb._text_events("B\u00a3"))
    chars = [(v, sc, f) for v, sc, f in ev if f & sb.KEYEVENTF_UNICODE]
    assert [sc for _v, sc, _f in chars] == [ord("B"), ord("B"), 0xA3, 0xA3]
    assert all(v == 0 for v, _sc, _f in chars)
    assert not any(v in (0x11, 0x12) for v, _sc, _f in ev), "no CTRL/ALT mirroring"


def test_repeated_characters_stay_two_separate_events():
    """The '££' in the field test lost one of its pair."""
    ev = [e for e in _ki(sb._text_events("\u00a3\u00a3"))
          if e[2] & sb.KEYEVENTF_UNICODE]
    assert len(ev) == 4


def test_astral_characters_are_sent_as_utf16_surrogates():
    assert len(sb._text_events("\U0001F600")) == 4


def _recording_send(monkeypatch):
    """Records (event_count, pause_that_followed) for every SendInput call."""
    calls, sleeps = [], []
    monkeypatch.setattr(sb.user32, "SendInput",
                        lambda n, a, s: (calls.append(n), n)[1])
    monkeypatch.setattr(sb.time, "sleep", lambda s: sleeps.append(s))
    return calls, sleeps


def test_typing_is_paced_per_character_not_handed_over_in_one_array(monkeypatch):
    """The 2.0.14 regression: one SendInput array typed the whole string into a
    single frame, so a game read a few characters of it and missed the rest.
    Injection was flawless and the text still arrived mostly missing."""
    calls, sleeps = _recording_send(monkeypatch)
    sent = sb.type_text("x" * 100)

    assert sent == 200, "every character still has to go out"
    assert calls == [1] * 200, f"expected one call per event, got {calls[:5]}..."
    assert len(sleeps) == 200, "every event must be followed by a pause"
    # The pause has to straddle a 60 Hz frame, or the pacing buys nothing.
    assert min(sleeps) >= 1 / 60.0 / 2, f"pauses too short to be seen: {min(sleeps)}"


def test_unpaced_typing_is_still_available_for_a_receiver_that_drains(monkeypatch):
    calls, sleeps = _recording_send(monkeypatch)
    sent = sb.type_text("x" * 100, paced=False)
    assert calls == [200], f"expected one batched call, got {calls}"
    assert sent == 200
    assert sleeps == [], "the unpaced path must not pace"


def test_type_text_stops_within_one_character_when_asked(monkeypatch):
    calls, _sleeps = _recording_send(monkeypatch)
    sb.type_text("x" * 2000, should_continue=lambda: not calls)
    assert calls == [1], "Stop must cut after one event"


# ── SHIFT for capitals is opt-in ─────────────────────────────────────────────
# It fixes a chat box that lowercases injected capitals, and it has twice cost
# whole lines of text in a game that plain Unicode typing filled correctly.
# Delivering the line in the wrong case beats delivering two characters of it.

def _shift_events_in(text, **kw):
    """How many SHIFT presses/releases `text` would produce."""
    n = 0
    for events, _pause in sb._text_groups(text, **kw):
        for _ev in events:
            n += 1
    chars = sum(2 for c in text)      # every character is a down + an up
    return n - chars


def test_no_shift_is_injected_by_default_even_for_capitals(monkeypatch):
    calls, _sleeps = _recording_send(monkeypatch)
    sb.type_text("Hello World")
    assert _shift_events_in("Hello World") == 0, \
        "the default path must send characters and nothing else"
    assert calls == [1] * 22, "11 characters, two events each, no modifiers"


def test_shift_brackets_capitals_when_asked_for(monkeypatch):
    _recording_send(monkeypatch)
    assert _shift_events_in("Hello", shift_for_capitals=True) == 2, \
        "one press before the capital and one release after it"
    assert _shift_events_in("HELLO", shift_for_capitals=True) == 2, \
        "a run of capitals holds SHIFT once rather than tapping it per letter"


def test_typing_one_character_at_a_time_does_not_spray_shift_releases(monkeypatch):
    """The per-character speed path calls type_text once per character. An
    unconditional release in its `finally` meant a 156-character line put 156
    SHIFT releases into the target -- real modifier events, at a receiver that
    was only supposed to be given text."""
    calls, _sleeps = _recording_send(monkeypatch)
    for ch in "hello world":
        sb.type_text(ch)
    assert calls == [1] * 22, f"expected two events per character, got {len(calls)}"


# ── Typing on a scancode backend ─────────────────────────────────────────────
# A driver-level backend is chosen because the target ignores message-queue
# input. KEYEVENTF_UNICODE *is* message-queue input, so typing that way meant a
# flow could press T and Enter successfully in a game and type nothing between
# them -- while working perfectly in Notepad, which reads WM_CHAR.

class _FakeScancodeKeyboard:
    """Name-string keyboard that records instead of sending. Stands in for
    ScancodeKeyboard / InterceptionKeyboard."""

    sends_scancodes = True

    def __init__(self, maps=None):
        self.calls = []
        self._maps = maps

    def maps_key(self, name):
        return True if self._maps is None else name in self._maps

    def press(self, name):
        self.calls.append(("press", name))

    def release(self, name):
        self.calls.append(("release", name))


def _kb_adapter(impl, monkeypatch, key_positions="layout"):
    """Adapter with its pynput fallback and its per-key sleeps neutered.

    key_positions is pinned rather than left to default: the default reads the
    developer's own settings.json, so a machine with "us" selected silently
    changed what these tests were asserting.
    """
    a = ib._NameBackendAdapter(impl, key_positions=key_positions)
    a._pynput = _FakeMouse()          # records; never touches the real desktop
    monkeypatch.setattr(ib.time, "sleep", lambda _s: None)
    return a


def test_typing_on_a_scancode_backend_presses_real_keys(monkeypatch):
    """The bug: this went out as VK_PACKET no matter which backend was picked,
    so a raw-input game saw nothing at all."""
    monkeypatch.setattr(ib, "_layout_keystroke",
                        lambda ch: {"a": (0x1E, False, False, False)}.get(ch))
    monkeypatch.setattr(ib, "qwerty_name_for_scancode", lambda sc: "a")
    sent = []
    monkeypatch.setattr(ib, "_sendinput_type", lambda t, *a, **k: sent.append(t))

    kb = _FakeScancodeKeyboard()
    _kb_adapter(kb, monkeypatch).type("a")

    assert kb.calls == [("press", "a"), ("release", "a")]
    assert sent == [], "nothing should have gone out as Unicode"


def test_typing_applies_the_shift_the_layout_asks_for(monkeypatch):
    """On AZERTY a digit is a shifted key, so the scancode alone types the
    punctuation printed on it instead."""
    monkeypatch.setattr(ib, "_layout_keystroke", lambda ch: (0x02, True, False, False))
    monkeypatch.setattr(ib, "qwerty_name_for_scancode", lambda sc: "1")

    kb = _FakeScancodeKeyboard()
    _kb_adapter(kb, monkeypatch).type("1")

    assert kb.calls == [("press", "shift"), ("press", "1"),
                        ("release", "1"), ("release", "shift")]


def test_a_run_of_capitals_holds_shift_down_once(monkeypatch):
    """What a real typist does, and far fewer events for the receiver to keep
    up with than tapping SHIFT per letter."""
    monkeypatch.setattr(ib, "_layout_keystroke", lambda ch: (0x1E, ch.isupper(), False, False))
    monkeypatch.setattr(ib, "qwerty_name_for_scancode", lambda sc: "a")

    kb = _FakeScancodeKeyboard()
    _kb_adapter(kb, monkeypatch).type("AAA")

    assert kb.calls.count(("press", "shift")) == 1
    assert kb.calls.count(("release", "shift")) == 1
    assert kb.calls[0] == ("press", "shift")
    assert kb.calls[-1] == ("release", "shift")


def test_shift_is_released_before_a_following_lowercase_letter(monkeypatch):
    """Otherwise the receiver reads the stale modifier state and capitalises
    the character after a capital."""
    monkeypatch.setattr(ib, "_layout_keystroke", lambda ch: (0x1E, ch.isupper(), False, False))
    monkeypatch.setattr(ib, "qwerty_name_for_scancode", lambda sc: "a")

    kb = _FakeScancodeKeyboard()
    _kb_adapter(kb, monkeypatch).type("Aa")

    i_rel = kb.calls.index(("release", "shift"))
    presses = [i for i, c in enumerate(kb.calls) if c == ("press", "a")]
    assert presses[1] > i_rel, "second (lowercase) letter must follow the shift release"


def test_every_modifier_change_is_followed_by_a_settle_pause(monkeypatch):
    """A receiver that samples modifier state when it processes the keypress
    can otherwise read the state from before the change landed."""
    monkeypatch.setattr(ib, "_layout_keystroke", lambda ch: (0x1E, ch.isupper(), False, False))
    monkeypatch.setattr(ib, "qwerty_name_for_scancode", lambda sc: "a")
    naps = []
    a = ib._NameBackendAdapter(_FakeScancodeKeyboard(), key_positions="layout")
    a._pynput = _FakeMouse()
    # A distinctive value: the settle and the key hold are both 0.02 in
    # production, so counting by value would conflate them.
    monkeypatch.setattr(ib, "TYPE_MOD_SETTLE_S", 0.077)
    monkeypatch.setattr(ib.time, "sleep", lambda s: naps.append(s))
    a.type("Aa")

    assert naps.count(0.077) == 2, "one settle per modifier change"


def test_altgr_is_sent_as_ctrl_plus_alt(monkeypatch):
    monkeypatch.setattr(ib, "_layout_keystroke", lambda ch: (0x10, False, True, True))
    monkeypatch.setattr(ib, "qwerty_name_for_scancode", lambda sc: "q")

    kb = _FakeScancodeKeyboard()
    _kb_adapter(kb, monkeypatch).type("@")

    assert kb.calls == [("press", "ctrl"), ("press", "alt"), ("press", "q"),
                        ("release", "q"), ("release", "alt"), ("release", "ctrl")]


def test_modifiers_are_dropped_before_a_unicode_fallback_character(monkeypatch):
    """Unicode injection carries no modifier state, so anything still held
    would be wrong for it -- and stay wrong for everything after it."""
    monkeypatch.setattr(ib, "_layout_keystroke",
                        lambda ch: None if ch == "中" else (0x1E, True, False, False))
    monkeypatch.setattr(ib, "qwerty_name_for_scancode", lambda sc: "a")
    monkeypatch.setattr(ib, "_sendinput_type", lambda t, *a, **k: None)

    kb = _FakeScancodeKeyboard()
    _kb_adapter(kb, monkeypatch).type("A中")

    assert ("release", "shift") in kb.calls
    assert kb.calls.index(("release", "shift")) == len(kb.calls) - 1


def test_a_newline_is_the_enter_key_on_the_scancode_path_too(monkeypatch):
    """U+000A resolves to Ctrl+Enter through the layout, which apps read as
    'send'. It must never travel as a character on either path."""
    monkeypatch.setattr(ib, "_layout_keystroke", lambda ch: None)

    kb = _FakeScancodeKeyboard()
    _kb_adapter(kb, monkeypatch).type("\n")

    assert kb.calls == [("press", "enter"), ("release", "enter")]


def test_a_character_the_layout_cannot_reach_still_goes_out_as_unicode(monkeypatch):
    """Dropping it would be worse: the rest of the string is still deliverable."""
    monkeypatch.setattr(ib, "_layout_keystroke",
                        lambda ch: None if ch == "\u4e2d" else (0x1E, False, False, False))
    monkeypatch.setattr(ib, "qwerty_name_for_scancode", lambda sc: "a")
    sent = []
    monkeypatch.setattr(ib, "_sendinput_type", lambda t, *a, **k: sent.append(t))

    kb = _FakeScancodeKeyboard()
    _kb_adapter(kb, monkeypatch).type("a\u4e2da")

    assert sent == ["\u4e2d"]
    assert kb.calls.count(("press", "a")) == 2


def test_a_key_the_backend_cannot_send_falls_back_per_character(monkeypatch):
    monkeypatch.setattr(ib, "_layout_keystroke", lambda ch: (0x1E, False, False, False))
    monkeypatch.setattr(ib, "qwerty_name_for_scancode", lambda sc: "a")
    sent = []
    monkeypatch.setattr(ib, "_sendinput_type", lambda t, *a, **k: sent.append(t))

    kb = _FakeScancodeKeyboard(maps=set())      # maps nothing
    _kb_adapter(kb, monkeypatch).type("ab")

    assert kb.calls == []
    assert sent == ["a", "b"]


def test_stop_cuts_scancode_typing_short(monkeypatch):
    """Scancode typing is a real per-key loop capped near 33 ch/s, so a long
    string is seconds of work -- Stop has to reach into it."""
    monkeypatch.setattr(ib, "_layout_keystroke", lambda ch: (0x1E, False, False, False))
    monkeypatch.setattr(ib, "qwerty_name_for_scancode", lambda sc: "a")

    kb = _FakeScancodeKeyboard()
    _kb_adapter(kb, monkeypatch).type("a" * 50, should_continue=lambda: not kb.calls)

    assert kb.calls == [("press", "a"), ("release", "a")]


def test_an_interrupted_run_never_leaves_a_modifier_held(monkeypatch):
    """A stuck SHIFT would capitalise everything the user typed afterwards."""
    monkeypatch.setattr(ib, "_layout_keystroke", lambda ch: (0x02, True, False, False))
    monkeypatch.setattr(ib, "qwerty_name_for_scancode", lambda sc: "1")

    kb = _FakeScancodeKeyboard()
    orig = kb.press

    def explode(name):
        orig(name)
        if name == "1":
            raise OSError("driver went away mid-keystroke")

    kb.press = explode
    try:
        _kb_adapter(kb, monkeypatch).type("1")
    except OSError:
        pass
    assert ("release", "shift") in kb.calls


def test_pynput_backend_typing_is_unchanged(monkeypatch):
    """Only driver-level backends switch paths -- the default must not."""
    sent = []
    monkeypatch.setattr(ib, "_sendinput_type", lambda t, *a, **k: sent.append(t))

    kb = _FakeScancodeKeyboard()
    kb.sends_scancodes = False
    _kb_adapter(kb, monkeypatch).type("hello")

    assert kb.calls == []
    assert sent == ["hello"]


def test_both_driver_backends_declare_themselves_scancode_senders():
    """input_backends routes on this flag; a backend that forgot it would
    silently go back to typing VK_PACKET into a game."""
    assert sb.ScancodeKeyboard.sends_scancodes is True
    assert interception_backend.InterceptionKeyboard.sends_scancodes is True


def test_layout_keystroke_reports_the_shift_state_layout_scancode_drops():
    """Same key position, different shift state -- that difference is the whole
    point of the second function."""
    monkeypatch_free = sb.layout_keystroke("A")
    if monkeypatch_free is None:        # non-Windows / no layout API
        return
    lower = sb.layout_keystroke("a")
    assert lower[0] == monkeypatch_free[0]
    assert lower[1] is False and monkeypatch_free[1] is True


# ── "Type text as if on" ─────────────────────────────────────────────────────
# A scancode is a key POSITION; which character it becomes is decided by the
# receiver. Windows apps use the active layout; some games run every scancode
# through their own US table, which turns "azerty" into "qwerty" on AZERTY.
# The two answers differ on a non-US layout, so this is a setting, not a
# constant -- and the round-trip through a layout-honouring receiver was
# measured correct before the setting was added.

def _us_adapter(impl, monkeypatch):
    a = ib._NameBackendAdapter(impl, key_positions="us")
    a._pynput = _FakeMouse()
    monkeypatch.setattr(ib.time, "sleep", lambda _s: None)
    return a


def test_us_mode_ignores_the_active_layout(monkeypatch):
    """The whole point: the target consults no layout, so neither may we."""
    monkeypatch.setattr(ib, "_layout_keystroke",
                        lambda ch: (0x10, False, False, False))  # AZERTY 'a'
    kb = _FakeScancodeKeyboard()
    _us_adapter(kb, monkeypatch).type("a")
    assert kb.calls == [("press", "a"), ("release", "a")], \
        "us mode must send the US position, not the layout's"


def test_us_mode_shifts_capitals_and_symbols(monkeypatch):
    kb = _FakeScancodeKeyboard()
    _us_adapter(kb, monkeypatch).type("A!")
    # Both want SHIFT, so it stays down across the pair.
    assert kb.calls == [
        ("press", "shift"),
        ("press", "a"), ("release", "a"),
        ("press", "1"), ("release", "1"),
        ("release", "shift"),
    ]


def test_us_mode_maps_space_and_falls_back_for_the_unreachable(monkeypatch):
    sent = []
    monkeypatch.setattr(ib, "_sendinput_type", lambda t, *a, **k: sent.append(t))
    kb = _FakeScancodeKeyboard()
    _us_adapter(kb, monkeypatch).type(" \u00e9")
    assert ("press", "space") in kb.calls
    assert sent == ["\u00e9"], "a character not on a US keyboard goes out as Unicode"


def test_every_us_key_name_exists_in_the_scancode_table():
    """A name the backend cannot resolve would silently fall back to Unicode --
    which is exactly the failure this mode exists to avoid."""
    missing = sorted({n for n, _sh in ib._US_KEYS.values()} - set(sb.SCANCODES))
    assert missing == [], f"US key names with no scancode: {missing}"


def test_layout_mode_is_the_default():
    assert settings.AppSettings().type_key_positions == "layout"


# ── Per-step override ────────────────────────────────────────────────────────
# Which keyboard the target reads is a property of the TARGET, not the machine:
# one flow types into a game that ignores layouts, the next into a browser that
# honours them. A single global switch is wrong for one of them either way.

def test_a_per_call_mode_overrides_the_adapter_default(monkeypatch):
    monkeypatch.setattr(ib, "_layout_keystroke", lambda ch: (0x10, False, False, False))
    monkeypatch.setattr(ib, "qwerty_name_for_scancode", lambda sc: "q")

    kb = _FakeScancodeKeyboard()
    _kb_adapter(kb, monkeypatch, key_positions="layout").type("a", key_positions="us")
    assert kb.calls == [("press", "a"), ("release", "a")], \
        "the per-call mode must win over the adapter's default"


def test_no_per_call_mode_keeps_the_adapter_default(monkeypatch):
    monkeypatch.setattr(ib, "_layout_keystroke", lambda ch: (0x10, False, False, False))
    monkeypatch.setattr(ib, "qwerty_name_for_scancode", lambda sc: "q")

    kb = _FakeScancodeKeyboard()
    _kb_adapter(kb, monkeypatch, key_positions="layout").type("a")
    assert kb.calls == [("press", "q"), ("release", "q")]


# ── Layout detection ─────────────────────────────────────────────────────────
# Read from where the letters physically sit, not from the locale id: two
# layouts with different language ids can share key positions, and positions
# are the only thing that matters to this code.

def test_layout_family_names_the_active_layout():
    fam = sb.layout_family()
    assert fam in ("QWERTY", "AZERTY", "QWERTZ", "")
    if fam:
        assert sb.layout_scancode("a") in (0x10, 0x1E)


def test_layout_family_classifies_by_key_position(monkeypatch):
    table = {}
    monkeypatch.setattr(sb, "layout_scancode", lambda ch: table.get(ch))

    table.update({"a": 0x10, "z": 0x11, "y": 0x15})
    assert sb.layout_family() == "AZERTY"

    table.update({"a": 0x1E, "z": 0x15, "y": 0x2C})
    assert sb.layout_family() == "QWERTZ"

    table.update({"a": 0x1E, "z": 0x2C, "y": 0x15})
    assert sb.layout_family() == "QWERTY"

    table.update({"a": None, "z": None, "y": None})
    assert sb.layout_family() == ""


def test_on_a_qwerty_layout_both_modes_send_the_same_keys(monkeypatch):
    """Which is why the setting is a no-op for a US user and the UI disables
    it: 'my layout' and 'US positions' are the same positions there."""
    monkeypatch.setattr(ib, "_layout_keystroke",
                        lambda ch: (sb.SCANCODES[ch], False, False, False))
    monkeypatch.setattr(ib, "qwerty_name_for_scancode", sb.qwerty_name_for_scancode)

    out = {}
    for mode in ("layout", "us"):
        kb = _FakeScancodeKeyboard()
        _kb_adapter(kb, monkeypatch, key_positions=mode).type("azerty")
        out[mode] = kb.calls
    assert out["layout"] == out["us"]


# ── the wheel ─────────────────────────────────────────────────────────────────
def test_a_negative_wheel_delta_fits_in_an_unsigned_field():
    """⚠ MOUSEINPUT.mouseData is a DWORD — unsigned. ctypes refuses a negative
    int for one, so scrolling DOWN (the commonest direction there is) would
    raise instead of scrolling. Windows reads the field back signed, so two's
    complement is what it wants."""
    import ctypes
    import sendinput_backend as sb
    assert sb._wheel_data(sb.WHEEL_DELTA) == 120
    assert sb._wheel_data(-sb.WHEEL_DELTA) == 0xFFFFFF88
    # The real check: the struct has to accept it.
    mi = sb.MOUSEINPUT(0, 0, sb._wheel_data(-sb.WHEEL_DELTA), sb.MOUSEEVENTF_WHEEL, 0, 0)
    assert ctypes.c_int32(mi.mouseData).value == -120, "Windows must read it back as -120"


def test_the_scancode_wheel_sends_one_event_per_notch(monkeypatch):
    """A real wheel sends one WM_MOUSEWHEEL per detent, and a receiver that
    reads input once a frame takes a bounded amount per pass — the lesson typed
    text paid for four times over. Rolling three notches into one event is not
    the same thing as three notches."""
    import sendinput_backend as sb
    sent = []
    monkeypatch.setattr(sb, "_send_mouse",
                        lambda flags, dx=0, dy=0, data=0: sent.append((flags, data)))
    sb.ScancodeMouse().scroll(0, -3)
    assert sent == [(sb.MOUSEEVENTF_WHEEL, sb._wheel_data(-120))] * 3

    sent.clear()
    sb.ScancodeMouse().scroll(2, 0)
    assert sent == [(sb.MOUSEEVENTF_HWHEEL, 120)] * 2, "sideways uses HWHEEL"

    sent.clear()
    sb.ScancodeMouse().scroll(0, 0)
    assert sent == [], "a scroll of nothing sends nothing"


def test_the_mouse_adapter_falls_back_to_pynput_for_the_wheel():
    """A backend with no wheel still scrolls. A scroll that silently does
    nothing is the worst outcome — the flow carries on as if the list moved."""
    import input_backends as ib

    class _NoWheel:
        def maps_button(self, name):
            return True

    class _FakePynput:
        def __init__(self):
            self.calls = []

        def scroll(self, dx, dy):
            self.calls.append((dx, dy))

    ad = ib._MouseBackendAdapter(_NoWheel())
    ad._pynput = _FakePynput()
    ad.scroll(0, -2)
    assert ad._pynput.calls == [(0, -2)]

    class _Broken(_NoWheel):
        def scroll(self, dx, dy):
            raise OSError("driver went away")

    ad2 = ib._MouseBackendAdapter(_Broken())
    ad2._pynput = _FakePynput()
    ad2.scroll(1, 0)
    assert ad2._pynput.calls == [(1, 0)], "a raising backend must not eat the scroll"


def test_every_mouse_backend_offers_a_wheel():
    """The adapter's fallback is for old backends, not for ours. If one of these
    quietly loses its scroll method, every scroll silently reroutes through
    pynput — which is exactly the bug that made typing work in Notepad and
    vanish in a game."""
    import interception_backend as ic
    import sendinput_backend as sb
    for cls in (sb.ScancodeMouse, ic.InterceptionMouse):
        assert callable(getattr(cls, "scroll", None)), f"{cls.__name__} has no wheel"


# ── absolute moves across more than one monitor ───────────────────────────────
#
# ⚠ Untested until 3 September 2026, and the failure it guards is one this
# machine actually has: a second monitor to the LEFT of the primary, whose
# pixels are at negative x. SendInput's absolute mode takes 0..65535 normalised
# coordinates, and normalising those against the primary monitor instead of the
# virtual desktop clamps every click onto the primary — the second monitor
# becomes unreachable, silently, with the cursor landing somewhere plausible.

def _fake_metrics(monkeypatch, x, y, w, h):
    """Pretend the virtual desktop is (x, y, w, h)."""
    import sendinput_backend as sb
    table = {sb.SM_XVIRTUALSCREEN: x, sb.SM_YVIRTUALSCREEN: y,
             sb.SM_CXVIRTUALSCREEN: w, sb.SM_CYVIRTUALSCREEN: h}
    monkeypatch.setattr(sb.user32, "GetSystemMetrics",
                        lambda which: table[which], raising=False)


def test_absolute_coordinates_span_the_whole_virtual_desktop(monkeypatch):
    """A single 1920x1080 monitor at the origin maps corner to corner."""
    import sendinput_backend as sb
    _fake_metrics(monkeypatch, 0, 0, 1920, 1080)

    assert sb._to_absolute(0, 0) == (0, 0)
    assert sb._to_absolute(1919, 1079) == (65535, 65535)
    mid_x, mid_y = sb._to_absolute(960, 540)
    assert abs(mid_x - 32767) <= 40 and abs(mid_y - 32767) <= 40


def test_a_monitor_at_negative_x_is_reachable(monkeypatch):
    """⚠ The bug this exists for.

    Two 1920-wide monitors with the secondary on the LEFT: the virtual desktop
    starts at x=-1920 and is 3840 wide. A point on that left monitor has a
    negative x, and normalising against the *primary* would produce a negative
    normalised value — which clamps to 0 at best and wraps in the 16-bit field
    at worst. Either way the click lands on the wrong screen and nothing
    reports an error.
    """
    import sendinput_backend as sb
    _fake_metrics(monkeypatch, -1920, 0, 3840, 1080)

    left_edge = sb._to_absolute(-1920, 0)
    assert left_edge == (0, 0), (
        f"the far edge of a left-hand monitor normalised to {left_edge}, not "
        "the origin — it is being measured from the primary monitor")

    # The seam between the two monitors sits at half of a 3840-wide desktop.
    seam_x, _ = sb._to_absolute(0, 0)
    assert abs(seam_x - 32767) <= 40, (
        f"x=0 normalised to {seam_x}; on this desktop it is the midpoint, not "
        "the left edge — the primary monitor is being used as the frame")

    right_edge, _ = sb._to_absolute(1919, 0)
    assert right_edge == 65535


def test_an_off_screen_target_is_clamped_not_wrapped(monkeypatch):
    """The normalised fields are 16-bit. A coordinate past the edge would wrap
    to the opposite side of the desktop, which reads as the cursor teleporting
    rather than as an out-of-range request."""
    import sendinput_backend as sb
    _fake_metrics(monkeypatch, 0, 0, 1920, 1080)

    assert sb._to_absolute(-5000, -5000) == (0, 0)
    assert sb._to_absolute(99999, 99999) == (65535, 65535)


def test_a_degenerate_desktop_does_not_divide_by_zero(monkeypatch):
    """GetSystemMetrics can answer 0 while a display is being reconfigured, and
    the width appears in a denominator."""
    import sendinput_backend as sb
    _fake_metrics(monkeypatch, 0, 0, 0, 0)
    assert sb._to_absolute(100, 100) == (0, 0)


def test_the_input_struct_is_the_size_windows_expects(monkeypatch):
    """⚠ Sized on the union's LARGEST member or SendInput rejects everything.

    `cbSize` is passed as `sizeof(INPUT)`. If the union were sized on
    KEYBDINPUT instead of MOUSEINPUT the struct comes out too small, and
    SendInput answers 0 for every call — no error, no exception, no input. On
    x64 the correct size is 40 bytes.
    """
    import ctypes, struct
    import sendinput_backend as sb
    if struct.calcsize("P") != 8:
        pytest.skip("the 40-byte figure is the x64 layout")
    assert ctypes.sizeof(sb.INPUT) == 40, (
        f"sizeof(INPUT) is {ctypes.sizeof(sb.INPUT)}, not 40 — SendInput will "
        "reject every call and report nothing")
    assert ctypes.sizeof(sb._INPUTunion) == ctypes.sizeof(sb.MOUSEINPUT), (
        "the union is not sized on MOUSEINPUT, its largest member")


# ── settings: a hand-edited file with the wrong type ──────────────────────────

def _settings_from(tmp_path, monkeypatch, payload):
    import json
    import settings as st
    monkeypatch.setattr(st, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(st, "SETTINGS_FILE", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text(json.dumps(payload), encoding="utf-8")
    return st.SettingsManager().s


def test_a_setting_of_the_wrong_type_keeps_its_default(tmp_path, monkeypatch):
    """⚠ Unknown keys were ignored; wrong *types* on known keys were not.

    `{"seq_speed": "fast"}` used to be stored as the string, and
    `{"script_hotkeys": "f13"}` as a string where a dict is expected — both
    accepted at load and then failing somewhere else entirely, in a widget or
    a `.items()` call, with nothing pointing back at the file.

    People do edit this file; a hotkey collision is something only a
    hand-edited settings.json can create.
    """
    s = _settings_from(tmp_path, monkeypatch,
                       {"seq_speed": "fast", "script_hotkeys": "f13"})
    assert s.seq_speed == 1.0, f"a string reached a float setting: {s.seq_speed!r}"
    assert s.script_hotkeys == {}, (
        f"a string reached a dict setting: {s.script_hotkeys!r}")

    # A dict of the wrong shape is rejected too — the values must be names.
    s2 = _settings_from(tmp_path, monkeypatch,
                        {"script_hotkeys": {"f13": {"nested": 1}}})
    assert s2.script_hotkeys == {}


def test_a_number_is_not_a_flag_and_a_flag_is_not_a_number(tmp_path, monkeypatch):
    """⚠ In Python `True` is an int, so a plain isinstance check accepts
    `{"seq_speed": true}` as a number and `{"always_on_top": 1}` as a flag.

    The first is nonsense and is refused. The second is a plausible hand-edit,
    so 0 and 1 are accepted — silently ignoring a reasonable edit is worse than
    taking it — while `2` or `"yes"` is a mistake and taking it as True would
    hide one.
    """
    assert _settings_from(tmp_path, monkeypatch, {"seq_speed": True}).seq_speed == 1.0

    assert _settings_from(tmp_path, monkeypatch,
                          {"always_on_top": 1}).always_on_top is True
    assert _settings_from(tmp_path, monkeypatch,
                          {"always_on_top": 0}).always_on_top is False
    assert _settings_from(tmp_path, monkeypatch,
                          {"always_on_top": 2}).always_on_top is False
    assert _settings_from(tmp_path, monkeypatch,
                          {"always_on_top": "yes"}).always_on_top is False


def test_good_settings_still_load(tmp_path, monkeypatch):
    """⚠ The half that matters most. A guard on a read path is exactly the kind
    that quietly rejects the values it was not written for, and every setting in
    this app would then silently revert to its default."""
    s = _settings_from(tmp_path, monkeypatch, {
        "seq_speed": 2.5,
        "always_on_top": True,
        "script_hotkeys": {"f13": "beta", "f14": "gamma"},
        "input_backend": "sendinput",
    })
    assert s.seq_speed == 2.5
    assert s.always_on_top is True
    assert s.script_hotkeys == {"f13": "beta", "f14": "gamma"}
    assert s.input_backend == "sendinput"
    # An int for a float field is fine and is stored as a float.
    assert _settings_from(tmp_path, monkeypatch, {"seq_speed": 2}).seq_speed == 2.0


def test_an_old_settings_file_still_loads_after_a_field_is_removed(tmp_path,
                                                                   monkeypatch):
    """⚠ The direction that makes removing a setting safe, pinned.

    Eight fields were removed on 4 September 2026 — leftovers from the pre-2.0
    Basic tab that nothing read: `use_fixed_position`, `limit_seconds`,
    `limit_time`, `use_image_trigger`, `image_trigger_path`,
    `keystroke_randomize`, `keystroke_random_range_ms`, `seq_loop_count`.

    That is safe only because `_load` ignores a key it does not recognise, so
    every settings.json already in the wild keeps opening and simply drops the
    extras on its next save. ⚠ *Renaming* a field is the dangerous operation
    and always was: the old name is ignored and the new one takes its default,
    which silently resets whatever the user had chosen.
    """
    gone = {
        "use_fixed_position": True, "limit_seconds": 30,
        "limit_time": "09:00:00", "use_image_trigger": True,
        "image_trigger_path": "C:/old.png", "keystroke_randomize": True,
        "keystroke_random_range_ms": 99, "seq_loop_count": 7,
    }
    s = _settings_from(tmp_path, monkeypatch, {**gone, "seq_speed": 3.0,
                                               "always_on_top": True})
    for name in gone:
        assert not hasattr(s, name), f"{name} is still a setting"
    # And the keys that survive are unaffected by the ones that did not.
    assert s.seq_speed == 3.0
    assert s.always_on_top is True


def test_a_locked_settings_file_is_not_emptied(tmp_path, monkeypatch):
    """⚠ This is the case that changed my mind about leaving settings alone.

    `settings.save` was kept as a plain write on the reasoning that settings
    are regenerable and `_load` falls back to defaults on a corrupt file. That
    reasoning was wrong: opening for write **truncates first**, so a
    destination locked by antivirus, a sync client or an open editor is emptied
    to zero bytes and *then* raises — and `save` swallows the exception. The
    next launch finds an empty file, takes every default, and says nothing.

    So the user silently loses every launcher-key binding, their input backend
    and their theme. The tolerant load does not protect them from that; it is
    what makes it silent.
    """
    import json
    import msvcrt
    import settings as st

    monkeypatch.setattr(st, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(st, "SETTINGS_FILE", tmp_path / "settings.json")

    sm = st.SettingsManager()
    sm.s.script_hotkeys = {"f13": "beta", "f14": "gamma"}
    sm.s.input_backend = "sendinput"
    sm.save()
    original = st.SETTINGS_FILE.read_bytes()
    assert b"f13" in original, "nothing was saved, so this test proves nothing"

    holder = open(st.SETTINGS_FILE, "r+b")
    msvcrt.locking(holder.fileno(), msvcrt.LK_NBLCK, 1)
    try:
        sm.s.input_backend = "interception"
        sm.save()          # swallows the failure, by design
    finally:
        msvcrt.locking(holder.fileno(), msvcrt.LK_UNLCK, 1)
        holder.close()

    assert st.SETTINGS_FILE.read_bytes() == original, (
        "a locked settings file was emptied — every binding the user set is "
        "gone and nothing said so")
    back = json.loads(st.SETTINGS_FILE.read_text(encoding="utf-8"))
    assert back["script_hotkeys"] == {"f13": "beta", "f14": "gamma"}
    assert not [p for p in tmp_path.iterdir()
                if p.name.startswith(".settings-")], "a temporary was left behind"


def test_no_corrupt_settings_file_can_put_a_wrong_type_into_a_field(tmp_path,
                                                                    monkeypatch):
    """⚠ Property-based sweep of every field, not a handful of examples.

    The named cases above check the types someone plausibly *mistypes*. This
    checks the whole surface: 400 documents built from arbitrary junk — lists,
    dicts, None, booleans, "\x00", 10**12, floats where ints belong — spread
    across all of `AppSettings`. Every field must still hold its declared type
    afterwards, because the app reads these without checking: a list that
    reached `theme` would be looked up as a stylesheet key, and a bool that
    reached an int field would be arithmetic on True.

    Run at 4,000 documents on 4 September 2026: zero wrong-type outcomes across
    all 64 fields. 400 is the version that belongs in a suite.

    ⚠ `bool` is checked before `int` on purpose — in Python `True` *is* an int,
    so `isinstance(True, int)` passes and a boolean leaking into a numeric
    field would go unnoticed by the obvious assertion.
    """
    import json
    import random
    from dataclasses import fields as dc_fields

    import settings as st

    declared = {f.name: f.type for f in dc_fields(st.AppSettings)}
    junk = [0, 1, -1, 999999, 3.5, "", "x", None, True, False, [], {},
            [1, 2], {"a": 1}, "true", "9", 10 ** 12, "\x00"]
    rng = random.Random(11)

    def wanted(t):
        return {int: int, "int": int, float: float, "float": float,
                bool: bool, "bool": bool, str: str, "str": str}.get(t)

    problems = []
    for _ in range(400):
        doc = {rng.choice(list(declared)): rng.choice(junk)
               for _ in range(rng.randint(1, 8))}
        s = _settings_from(tmp_path, monkeypatch, doc)
        for name, t in declared.items():
            exp = wanted(t)
            if exp is None:
                continue
            val = getattr(s, name)
            if exp is bool:
                ok = isinstance(val, bool)
            elif exp is int:
                ok = isinstance(val, int) and not isinstance(val, bool)
            elif exp is float:
                ok = isinstance(val, (int, float)) and not isinstance(val, bool)
            else:
                ok = isinstance(val, exp)
            if not ok:
                problems.append(f"{name}: {type(val).__name__} {val!r} "
                                f"in a {exp.__name__} field, from {doc}")
    assert not problems, ("a corrupt settings file put wrong types into these:\n  "
                          + "\n  ".join(problems[:8]))
