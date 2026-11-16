"""Ctrl-click, Shift-click, Alt-drag, Shift-scroll.

One gesture, and until now a click step could not express it. The recorder
captured the click and the modifier separately, and put the modifier LAST —
because a modifier only became a step when it came back up. So a ctrl-click
multi-select recorded as two plain clicks followed by a stray "hold ctrl for
1.1 s" with nothing under it, and replayed as two plain clicks, the second
undoing the selection made by the first.

This is `recorder._flush_chord`'s lesson one step over: the overlap is the
entire content of the gesture, and the saved JSON keeps no record that it
happened, so it cannot be recovered afterwards either.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import flow
import flow_exec
import recorder
from flow import FlowGraph
from recorder import SequenceRecorder, SeqStep


# ── the model ────────────────────────────────────────────────────────────────
def test_a_step_with_no_modifiers_is_every_flow_ever_saved():
    """The absence of the field means none, so nothing needs migrating."""
    assert flow.pointer_mods({}) == []
    assert flow.pointer_mods({"mods": []}) == []
    assert flow.pointer_mods(None) == []


def test_modifiers_come_back_in_a_canonical_order():
    """Two steps meaning the same thing have to read the same way."""
    assert flow.pointer_mods({"mods": ["shift", "ctrl"]}) == ["ctrl", "shift"]
    assert flow.pointer_mods({"mods": ["ctrl", "shift"]}) == ["ctrl", "shift"]
    assert flow.pointer_mods({"mods": "ctrl"}) == ["ctrl"]
    assert flow.pointer_mods({"mods": ["CTRL", " Alt "]}) == ["ctrl", "alt"]
    assert flow.pointer_mods({"mods": ["banana"]}) == []


def test_the_summary_says_which_modifiers():
    s = flow._action_summary({"kind": "click",
                              "data": {"x": 1, "y": 2, "mods": ["ctrl"]}})
    assert s.startswith("Ctrl+"), s
    plain = flow._action_summary({"kind": "click", "data": {"x": 1, "y": 2}})
    assert not plain.startswith("Ctrl+")
    sc = flow._action_summary({"kind": "scroll",
                               "data": {"direction": "down", "amount": 3,
                                        "mods": ["shift"]}})
    assert sc.startswith("Shift+"), sc


# ── recording ────────────────────────────────────────────────────────────────
def _make_recorder(monkeypatch, clock):
    rec = SequenceRecorder()
    rec._recording = True
    rec._t_last = clock["t"]
    rec._mods_down = set()
    rec._down = {}
    rec._keys_down = {}
    rec._mods_info = {}
    rec._chord = []
    rec._last_move = None
    rec._rest = None
    rec._last_pos = None
    # ⚠ perf_counter, not monotonic: the recorder measures on the fine
    # clock (monotonic is GetTickCount64, 15.625 ms, which quantised
    # every recorded duration). Patching the old name here would leave
    # these tests reading the real clock and timing out or drifting.
    monkeypatch.setattr(recorder.time, "perf_counter", lambda: clock["t"])
    monkeypatch.setattr(SequenceRecorder, "_key_to_str", staticmethod(lambda k: k))
    return rec


def test_a_ctrl_click_multi_select_records_as_two_ctrl_clicks(monkeypatch):
    clock = {"t": 0.0}
    rec = _make_recorder(monkeypatch, clock)

    clock["t"] = 0.1; rec._on_key_press("ctrl")
    clock["t"] = 0.3; rec._on_click(100, 100, "left", True)
    clock["t"] = 0.35; rec._on_click(100, 100, "left", False)
    clock["t"] = 0.8; rec._on_click(400, 300, "left", True)
    clock["t"] = 0.85; rec._on_click(400, 300, "left", False)
    clock["t"] = 1.2; rec._on_key_release("ctrl")

    assert [s.kind for s in rec.steps] == [SeqStep.CLICK, SeqStep.CLICK], \
        "the modifier must not also arrive as a step of its own, afterwards"
    assert all(s.data.get("mods") == ["ctrl"] for s in rec.steps)


def test_a_modifier_held_alone_is_still_a_hold_of_its_own(monkeypatch):
    """Holding Shift to sprint is a step. Claiming a modifier for a click must
    not take that away from every modifier that no click ever used."""
    clock = {"t": 0.0}
    rec = _make_recorder(monkeypatch, clock)

    rec._on_key_press("shift")
    clock["t"] = 1.5
    rec._on_key_release("shift")

    assert [s.kind for s in rec.steps] == [SeqStep.KEY]
    assert rec.steps[0].data["keys"] == ["shift"]
    assert rec.steps[0].data["hold_ms"] >= 1400


def test_a_shift_scroll_keeps_its_modifier(monkeypatch):
    """Shift-scroll is sideways in most things that scroll and ctrl-scroll is
    zoom; without the modifier the replay scrolls the list it meant to zoom."""
    clock = {"t": 0.0}
    rec = _make_recorder(monkeypatch, clock)

    rec._on_key_press("shift")
    clock["t"] = 0.4
    rec._on_scroll(500, 500, 0, -1)
    clock["t"] = 0.9
    rec._on_key_release("shift")

    assert [s.kind for s in rec.steps] == [SeqStep.SCROLL]
    assert rec.steps[0].data["mods"] == ["shift"]


def test_an_alt_drag_carries_the_modifier_through_the_promotion(monkeypatch):
    """A drag is a click step rewritten at release, so it inherits whatever
    the press captured."""
    clock = {"t": 0.0}
    rec = _make_recorder(monkeypatch, clock)

    rec._on_key_press("alt")
    clock["t"] = 0.2; rec._on_click(100, 100, "left", True)
    clock["t"] = 0.9; rec._on_click(500, 400, "left", False)
    clock["t"] = 1.1; rec._on_key_release("alt")

    assert [s.kind for s in rec.steps] == [SeqStep.DRAG]
    assert rec.steps[0].data["mods"] == ["alt"]


def test_two_modifiers_at_once(monkeypatch):
    clock = {"t": 0.0}
    rec = _make_recorder(monkeypatch, clock)

    rec._on_key_press("ctrl")
    rec._on_key_press("shift")
    clock["t"] = 0.3; rec._on_click(10, 10, "left", True)
    clock["t"] = 0.35; rec._on_click(10, 10, "left", False)
    clock["t"] = 0.9; rec._on_key_release("shift")
    clock["t"] = 1.0; rec._on_key_release("ctrl")

    assert [s.kind for s in rec.steps] == [SeqStep.CLICK]
    assert rec.steps[0].data["mods"] == ["ctrl", "shift"]


# ── playing it back ──────────────────────────────────────────────────────────
class _FakeKB:
    def __init__(self, events):
        self.events = events

    def press(self, key):
        self.events.append(("press", str(key)))

    def release(self, key):
        self.events.append(("release", str(key)))


class _FakeMouse:
    def __init__(self, events):
        self.events = events
        self._pos = (0, 0)

    @property
    def position(self):
        return self._pos

    @position.setter
    def position(self, xy):
        self._pos = tuple(xy)

    def click(self, b, n=1):
        self.events.append(("click", str(b), n))

    def press(self, b):
        self.events.append(("mdown", str(b)))

    def release(self, b):
        self.events.append(("mup", str(b)))

    def scroll(self, dx, dy):
        self.events.append(("scroll", dx, dy))


def _worker(events):
    fw = flow_exec.FlowWorker(FlowGraph())
    fw._kb = _FakeKB(events)
    fw._mouse = _FakeMouse(events)
    fw._running = True
    fw.sleep = lambda s: None
    fw._smooth_mouse = False        # the modifier, not the journey
    return fw


def test_the_modifier_is_down_before_the_button_and_up_after():
    """A receiver reads the gesture from the order the events arrive in. A
    modifier pressed after the button, or released before it, is not a
    ctrl-click — it is a click and, separately, a Ctrl."""
    events = []
    fw = _worker(events)
    fw.do_action({"kind": "click",
                  "data": {"x": 5, "y": 5, "mods": ["ctrl"]}}, {})
    kinds = [e[0] for e in events]
    assert kinds == ["press", "click", "release"], events
    assert "ctrl" in events[0][1].lower()


def test_a_plain_click_touches_no_keys():
    events = []
    fw = _worker(events)
    fw.do_action({"kind": "click", "data": {"x": 5, "y": 5}}, {})
    assert [e[0] for e in events] == ["click"]


def test_the_modifier_comes_back_up_even_when_the_step_fails():
    """⚠ Ctrl left down turns every later click in the flow into a ctrl-click
    and every keystroke the user makes afterwards into a shortcut. That is
    worse than the step failing, so the release is a `finally`."""
    events = []
    fw = _worker(events)

    def boom(b, n=1):
        raise RuntimeError("the backend died")

    fw._mouse.click = boom
    assert fw.do_action({"kind": "click",
                         "data": {"x": 5, "y": 5, "mods": ["ctrl"]}}, {}) is False
    assert [e[0] for e in events] == ["press", "release"]
    assert not fw._held, "and nothing is left recorded as held"


def test_a_modifier_the_flow_was_already_holding_is_left_alone():
    """A Hold-down node holding Shift for a sprint must not be cancelled by a
    shift-click that happens during it."""
    events = []
    fw = _worker(events)
    fw._cur_node = "the-hold-node"
    fw.do_action({"kind": "key",
                  "data": {"keys": ["shift"], "mode": flow.KEY_DOWN}}, {})
    assert "shift" in fw._held
    events.clear()

    fw._cur_node = "the-click-node"
    fw.do_action({"kind": "click",
                  "data": {"x": 5, "y": 5, "mods": ["shift"]}}, {})
    assert [e[0] for e in events] == ["click"], \
        "it was already down: neither pressed again nor released underneath"
    assert "shift" in fw._held


def test_a_scroll_and_a_drag_get_their_modifiers_too():
    events = []
    fw = _worker(events)
    fw.do_action({"kind": "scroll",
                  "data": {"direction": "down", "amount": 1,
                           "at_cursor": True, "mods": ["shift"]}}, {})
    assert [e[0] for e in events] == ["press", "scroll", "release"], events


# ── the seam ─────────────────────────────────────────────────────────────────
def test_a_recorded_ctrl_click_survives_into_the_engine(monkeypatch):
    """`_on_recording_stopped` drops SeqStep.to_dict() straight into a node."""
    clock = {"t": 0.0}
    rec = _make_recorder(monkeypatch, clock)
    rec._on_key_press("ctrl")
    clock["t"] = 0.3; rec._on_click(700, 400, "left", True)
    clock["t"] = 0.35; rec._on_click(700, 400, "left", False)
    clock["t"] = 0.8; rec._on_key_release("ctrl")

    step = rec.steps[0].to_dict()
    events = []
    fw = _worker(events)
    assert fw.do_action(step, {}) is True
    assert [e[0] for e in events] == ["press", "click", "release"], events
    assert fw._mouse.position == (700, 400)


# ── the two side buttons ─────────────────────────────────────────────────────
# ⚠ `{left, right, middle}.get(button, "left")` was the whole of the recorder's
# button mapping, so a thumb button — Back and Forward to Windows, Mouse 4 and
# Mouse 5 to anyone who games with them — recorded as a LEFT CLICK at the same
# coordinates and replayed as one. A wrong action in the place a right answer
# would have gone, rather than a missing feature.

def test_a_side_button_is_not_recorded_as_a_left_click(monkeypatch):
    from pynput.mouse import Button
    clock = {"t": 0.0}
    rec = _make_recorder(monkeypatch, clock)

    rec._on_click(300, 300, Button.x1, True)
    clock["t"] = 0.05
    rec._on_click(300, 300, Button.x1, False)

    assert [s.data["button"] for s in rec.steps] == ["x1"]


def test_a_button_that_cannot_be_replayed_is_dropped_not_guessed(monkeypatch):
    """Dropping it loses a step, which is visible. Recording it as a left
    click puts a wrong action where a right one would have gone, which is not.
    """
    from pynput.mouse import Button
    clock = {"t": 0.0}
    rec = _make_recorder(monkeypatch, clock)

    rec._on_click(300, 300, Button.unknown, True)
    rec._on_click(300, 300, Button.unknown, False)

    assert rec.steps == []


def test_the_recorder_and_the_engine_agree_on_every_button():
    """Two lists of buttons in two files is how one of them ends up shorter.
    A name the recorder can write and the engine cannot send is a step that
    does nothing, silently."""
    import recorder as _rec
    for name in flow.BUTTONS:
        assert _rec._button_name(name) == name, name
        assert name in flow_exec._BTN, f"the engine cannot send {name}"
        assert flow.button_label(name)


def test_a_side_button_click_reaches_the_backend():
    events = []
    fw = _worker(events)
    fw.do_action({"kind": "click", "data": {"x": 5, "y": 5, "button": "x2"}}, {})
    assert [e[0] for e in events] == ["click"]
    assert "x2" in events[0][1]


def test_the_summary_calls_the_side_buttons_what_windows_calls_them():
    assert flow.button_label("x1") == "Back"
    assert flow.button_label("x2") == "Forward"
    s = flow._action_summary({"kind": "click",
                              "data": {"x": 1, "y": 2, "button": "x1"}})
    assert "Back" in s and "X1" not in s, s


def test_a_detected_side_button_click_says_which_side_button():
    """⚠ The two X buttons share ONE flag pair; which of them is being pressed
    travels in `mouseData`, not in the flag. Sending XDOWN with mouseData left
    at 0 presses neither — the event goes out, Windows accepts it, and nothing
    happens. Silently, on the path that clicks what a Detect step found.
    """
    import ctypes as _ct
    from unittest import mock
    from PIL import Image

    sent = []
    metrics = {76: 0, 77: 0, 78: 1920, 79: 1080}

    class _U32:
        def GetSystemMetrics(self, i):
            return metrics[i]

        def GetCursorPos(self, ref):
            arr = _ct.cast(ref, _ct.POINTER(_ct.c_long))
            arr[0], arr[1] = (0, 0)
            return 1

        def SendInput(self, n, ref, size):
            arr = _ct.cast(ref, _ct.POINTER(_ct.c_long))
            # INPUT is {type, pad, MOUSEINPUT{dx, dy, mouseData, dwFlags, …}}
            sent.append((arr[4], arr[5] & 0xFFFFFFFF))
            return 1

    class _Windll:
        user32 = _U32()

    events = []
    fw = _worker(events)
    with mock.patch.object(flow_exec.ctypes, "windll", _Windll()), \
         mock.patch.object(flow_exec.time, "sleep", lambda s: None):
        fw._click_physical(800, 400, Image.new("RGB", (1920, 1080)), "x1")

    XDOWN, XUP = 0x0080, 0x0100
    downs = [data for data, flags in sent if flags & XDOWN]
    ups = [data for data, flags in sent if flags & XUP]
    assert downs == [1], f"XBUTTON1 has to be named in mouseData: {downs}"
    assert ups == [1]

    sent.clear()
    with mock.patch.object(flow_exec.ctypes, "windll", _Windll()), \
         mock.patch.object(flow_exec.time, "sleep", lambda s: None):
        fw._click_physical(800, 400, Image.new("RGB", (1920, 1080)), "x2")
    assert [d for d, f in sent if f & XDOWN] == [2]


def test_a_detected_ordinary_click_carries_no_button_data():
    """mouseData is only meaningful for the X buttons and the wheel; setting
    it on a left click would be a value Windows is entitled to read."""
    import ctypes as _ct
    from unittest import mock
    from PIL import Image

    sent = []

    class _U32:
        def GetSystemMetrics(self, i):
            return {76: 0, 77: 0, 78: 1920, 79: 1080}[i]

        def GetCursorPos(self, ref):
            arr = _ct.cast(ref, _ct.POINTER(_ct.c_long))
            arr[0], arr[1] = (0, 0)
            return 1

        def SendInput(self, n, ref, size):
            arr = _ct.cast(ref, _ct.POINTER(_ct.c_long))
            sent.append((arr[4], arr[5] & 0xFFFFFFFF))
            return 1

    class _Windll:
        user32 = _U32()

    fw = _worker([])
    with mock.patch.object(flow_exec.ctypes, "windll", _Windll()), \
         mock.patch.object(flow_exec.time, "sleep", lambda s: None):
        fw._click_physical(800, 400, Image.new("RGB", (1920, 1080)), "left")
    assert all(data == 0 for data, _ in sent)
