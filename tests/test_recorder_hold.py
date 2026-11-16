"""Headless tests for recorder.py's key-hold capture (Change 3):
a held key becomes ONE step, not one per OS auto-repeat, and long holds carry
hold_ms. Feeds _on_key_press/_on_key_release directly with fake key strings
and a controlled clock — no real pynput listener is started.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import recorder
from recorder import SequenceRecorder, SeqStep, HOLD_MIN_MS


def _make_recorder(monkeypatch, clock: dict):
    """A recorder in 'recording' state without starting real pynput listeners.
    `clock` is a mutable {"t": float} the test advances between calls."""
    rec = SequenceRecorder()
    rec._recording = True
    rec._t_last = clock["t"]
    rec._mods_down = set()
    rec._down = {}
    rec._keys_down = {}
    rec._mods_info = {}
    # ⚠ perf_counter, not monotonic: the recorder measures on the fine
    # clock (monotonic is GetTickCount64, 15.625 ms, which quantised
    # every recorded duration). Patching the old name here would leave
    # these tests reading the real clock and timing out or drifting.
    monkeypatch.setattr(recorder.time, "perf_counter", lambda: clock["t"])
    # Fake keys are plain strings; make _key_to_str the identity so tests can
    # feed "w", "shift", "c" etc. directly.
    monkeypatch.setattr(SequenceRecorder, "_key_to_str", staticmethod(lambda key: key))
    return rec


# ── (1) auto-repeat coalesces into exactly one step ──────────────────────────
def test_press_repeat_repeat_release_is_one_step(monkeypatch):
    clock = {"t": 0.0}
    rec = _make_recorder(monkeypatch, clock)

    rec._on_key_press("w")
    clock["t"] = 0.05
    rec._on_key_press("w")             # OS auto-repeat — ignored
    clock["t"] = 0.10
    rec._on_key_press("w")             # OS auto-repeat — ignored
    clock["t"] = 0.15                  # released after 150ms (a tap)
    rec._on_key_release("w")

    assert len(rec.steps) == 1
    step = rec.steps[0]
    assert step.kind == SeqStep.KEY
    assert step.data["keys"] == ["w"]


# ── (2) short press -> tap, no hold_ms ───────────────────────────────────────
def test_short_press_is_a_tap_without_hold_ms(monkeypatch):
    clock = {"t": 0.0}
    rec = _make_recorder(monkeypatch, clock)

    rec._on_key_press("w")
    clock["t"] = (HOLD_MIN_MS - 100) / 1000.0   # well under the hold threshold
    rec._on_key_release("w")

    assert len(rec.steps) == 1
    assert "hold_ms" not in rec.steps[0].data


# ── (3) long press -> hold_ms ≈ held duration ────────────────────────────────
def test_long_press_records_hold_ms(monkeypatch):
    clock = {"t": 0.0}
    rec = _make_recorder(monkeypatch, clock)

    rec._on_key_press("w")
    clock["t"] = 1.5   # held 1500ms
    rec._on_key_release("w")

    assert len(rec.steps) == 1
    step = rec.steps[0]
    assert step.data.get("hold_ms") is not None
    assert abs(step.data["hold_ms"] - 1500) <= 2


# ── (4) lone modifier held long -> one KEY hold step; combo -> no separate
#        modifier step ────────────────────────────────────────────────────────
def test_lone_modifier_held_long_is_one_key_hold_step(monkeypatch):
    clock = {"t": 0.0}
    rec = _make_recorder(monkeypatch, clock)

    rec._on_key_press("shift")
    clock["t"] = 0.6
    rec._on_key_release("shift")

    assert len(rec.steps) == 1
    step = rec.steps[0]
    assert step.kind == SeqStep.KEY
    assert step.data["keys"] == ["shift"]
    assert step.data.get("hold_ms") is not None


def test_lone_modifier_short_tap_emits_nothing(monkeypatch):
    clock = {"t": 0.0}
    rec = _make_recorder(monkeypatch, clock)

    rec._on_key_press("shift")
    clock["t"] = 0.05
    rec._on_key_release("shift")

    assert rec.steps == []


def test_modifier_combined_with_key_emits_only_the_combo(monkeypatch):
    clock = {"t": 0.0}
    rec = _make_recorder(monkeypatch, clock)

    rec._on_key_press("shift")
    clock["t"] = 0.1
    rec._on_key_press("c")
    clock["t"] = 0.15
    rec._on_key_release("c")
    clock["t"] = 0.9          # shift stays held long after the combo
    rec._on_key_release("shift")

    assert len(rec.steps) == 1
    step = rec.steps[0]
    assert step.kind == SeqStep.COMBO
    assert step.data["keys"] == ["shift", "c"]


# ── stop() flushes a key still held mid-hold ─────────────────────────────────
def test_stop_flushes_key_still_held(monkeypatch):
    clock = {"t": 0.0}
    rec = _make_recorder(monkeypatch, clock)

    rec._on_key_press("w")
    clock["t"] = 2.0   # never released — Stop fires mid-hold
    rec.stop()

    assert len(rec.steps) == 1
    step = rec.steps[0]
    assert step.kind == SeqStep.KEY
    assert step.data["keys"] == ["w"]
    assert step.data.get("hold_ms") is not None
    assert abs(step.data["hold_ms"] - 2000) <= 2


# ── Stopping playback without killing the app ────────────────────────────────
# SequencePlayer.stop() used to wait 3 s and then clear both references
# regardless — the same bug SequenceTab.stop_playback was fixed for in 2.0.8.
# Destroying a running QThread is a Qt qFatal (an abort() in C, not an
# exception), so it takes the whole process down with no traceback. The wait
# times out routinely, because a playback step can be inside an image match or
# a screen grab and neither is interruptible.

import pytest
from PySide6.QtCore import QThread


class _StuckThread(QThread):
    """A thread that ignores quit() — stands in for one blocked in a grab."""

    def __init__(self):
        super().__init__()
        self._running = True

    def run(self):
        while self._running:
            self.msleep(10)

    def let_go(self):
        self._running = False


def _player_with(thread, worker):
    player = recorder.SequenceManager()
    player._thread, player._worker = thread, worker
    return player


class _FakeWorker:
    def __init__(self):
        self.stopped = 0

    def request_stop(self):
        self.stopped += 1

    def deleteLater(self):
        pass


def test_stop_keeps_a_thread_that_would_not_die(qapp_or_skip):
    """The reference must outlive stop(); dropping it here is the qFatal."""
    t, w = _StuckThread(), _FakeWorker()
    t.start()
    try:
        player = _player_with(t, w)
        player._retired = []
        player.stop()
        assert w.stopped >= 1, "the worker must be asked to stop"
        assert any(pair[0] is t for pair in player._retired), \
            "a still-running thread must be retired, not dropped"
    finally:
        t.let_go()
        t.quit()
        t.wait(2000)


def test_stop_does_not_wait_on_its_own_thread(monkeypatch, qapp_or_skip):
    """A thread waiting on itself returns instantly and Qt only warns, so the
    timeout path would silently become the always path."""
    t, w = _StuckThread(), _FakeWorker()
    t.start()
    waited = []
    monkeypatch.setattr(QThread, "wait", lambda self, *a, **k: waited.append(self) or True)
    monkeypatch.setattr(QThread, "currentThread", staticmethod(lambda: t))
    try:
        player = _player_with(t, w)
        player._retired = []
        player.stop()
        assert waited == [], "must not wait on the thread we are running on"
    finally:
        monkeypatch.undo()
        t.let_go()
        t.quit()
        t.wait(2000)


def test_a_finished_thread_is_not_retained(qapp_or_skip):
    t, w = _StuckThread(), _FakeWorker()
    player = _player_with(t, w)      # never started: not running
    player._retired = []
    player.stop()
    assert player._retired == []
    assert player._thread is None and player._worker is None


# ── recording the wheel ───────────────────────────────────────────────────────
def test_one_flick_of_the_wheel_records_as_one_step():
    """pynput reports one callback per detent, so a single flick would become a
    dozen identical nodes — unreadable, and the thing that makes a recording not
    worth keeping."""
    from recorder import SequenceRecorder, SeqStep
    r = SequenceRecorder()
    r._recording = True
    r._steps = []
    r._t_last = 0.0
    for _ in range(5):
        r._on_scroll(100, 100, 0, -1)
    assert len(r._steps) == 1
    s = r._steps[0]
    assert s.kind == SeqStep.SCROLL
    assert s.data["direction"] == "down"
    assert s.data["amount"] == 5
    assert s.data["at_cursor"] is True, "the pointer is already where it belongs"


def test_a_change_of_direction_starts_a_new_scroll_step():
    from recorder import SequenceRecorder
    r = SequenceRecorder()
    r._recording = True
    r._steps = []
    r._t_last = 0.0
    r._on_scroll(0, 0, 0, -2)
    r._on_scroll(0, 0, 0, 3)
    r._on_scroll(0, 0, 1, 0)
    assert [(s.data["direction"], s.data["amount"]) for s in r._steps] == [
        ("down", 2), ("up", 3), ("right", 1)]


def test_a_scroll_that_moved_nothing_records_nothing():
    from recorder import SequenceRecorder
    r = SequenceRecorder()
    r._recording = True
    r._steps = []
    r._t_last = 0.0
    r._on_scroll(0, 0, 0, 0)
    assert r._steps == []


# ── mouse: click vs hold vs drag ──────────────────────────────────────────────
#
# ⚠ Drag promotion had no tests at all until 3 September 2026, while the scroll
# merge beside it did. It is the newest of the three (15 August 2026) and the
# one with a recorded past failure: a swipe used to be recorded as a *click at
# the point the swipe started* — a step that presses and releases in one place
# and therefore does nothing whatever to the control being dragged. Idle
# Slayer's "Swipe to Start" pauses the game, so that silently stranded a
# 25-minute run.

from pynput.mouse import Button          # noqa: E402  (after the sys.path fix)


def _press(rec, x, y, button=Button.left):
    rec._on_click(x, y, button, True)


def _release(rec, x, y, button=Button.left):
    rec._on_click(x, y, button, False)


def test_a_press_that_moved_is_recorded_as_a_drag(monkeypatch):
    """The gesture is press → travel → release, and where it ended is the
    whole point. `to_x`/`to_y` must be the release position."""
    clock = {"t": 100.0}
    rec = _make_recorder(monkeypatch, clock)

    _press(rec, 500, 400)
    clock["t"] += 0.5                       # a swipe takes time
    _release(rec, 900, 405)

    assert len(rec._steps) == 1
    step = rec._steps[0]
    assert step.kind == SeqStep.DRAG, (
        "a press that travelled 400px was not promoted to a drag — it would "
        "replay as a click at the point the swipe started")
    assert (step.data["x"], step.data["y"]) == (500, 400)
    assert (step.data["to_x"], step.data["to_y"]) == (900, 405)
    # Recorded, not defaulted: the speed of a swipe is often the thing the
    # receiver is measuring.
    #
    # ⚠ Asserted as the press-to-release the REPLAY will take, not as the raw
    # 500 this used to check. `duration_ms` is the time the pointer spends
    # travelling — flow.drag_duration_ms's meaning, and the editor's "Travel
    # time" row — and the engine adds a settle either side of it. Storing the
    # whole 500 there made the replayed swipe 160 ms slower than the one that
    # was made, which is the number the receiver is measuring.
    import flow
    assert abs(flow.drag_total_ms(step.data) - 500) < 1
    assert step.data["duration_ms"] == 500 - 2 * flow.DRAG_SETTLE_MS


def test_a_press_that_did_not_move_stays_a_click(monkeypatch):
    """Hand jitter is not a gesture. Movement within _DRAG_PX is still a click."""
    clock = {"t": 100.0}
    rec = _make_recorder(monkeypatch, clock)

    jitter = SequenceRecorder._DRAG_PX - 1
    _press(rec, 500, 400)
    clock["t"] += 0.05
    _release(rec, 500 + jitter, 400 + jitter)

    assert rec._steps[0].kind == SeqStep.CLICK
    assert "to_x" not in rec._steps[0].data


def test_a_drag_beats_a_hold_even_though_it_was_also_held(monkeypatch):
    """⚠ The precedence, and the reason the code checks drag *first*.

    A swipe is nearly always held past `_HOLD_S` as well, so both promotions
    match. If the hold test ran first, every drag in the app would be recorded
    as a press-and-hold in one place — which is the original bug wearing a
    different hat, and it would look correct in the step list.
    """
    clock = {"t": 100.0}
    rec = _make_recorder(monkeypatch, clock)

    _press(rec, 100, 100)
    clock["t"] += SequenceRecorder._HOLD_S * 3     # comfortably a "hold" too
    _release(rec, 600, 100)

    step = rec._steps[0]
    assert step.kind == SeqStep.DRAG, (
        "a slow swipe was recorded as a hold — drag must be checked before "
        "hold, because a drag is nearly always held as well")
    assert not step.data.get("hold")


def test_a_press_held_in_one_place_is_a_hold(monkeypatch):
    """The other half of that precedence: no movement, so hold still wins."""
    clock = {"t": 100.0}
    rec = _make_recorder(monkeypatch, clock)

    _press(rec, 300, 300)
    clock["t"] += SequenceRecorder._HOLD_S * 2
    _release(rec, 300, 300)

    step = rec._steps[0]
    assert step.kind == SeqStep.CLICK
    assert step.data.get("hold") is True
    assert step.data["hold_ms"] >= SequenceRecorder._HOLD_S * 1000


def test_a_double_click_is_not_turned_into_a_drag(monkeypatch):
    """A merged double-click carries clicks=2, and the drag promotion
    deliberately refuses those — dragging is a single-press gesture."""
    clock = {"t": 100.0}
    rec = _make_recorder(monkeypatch, clock)

    _press(rec, 200, 200)
    clock["t"] += 0.02
    _release(rec, 200, 200)
    clock["t"] += 0.05                      # inside _DBLCLICK_S
    _press(rec, 201, 200)
    clock["t"] += 0.4
    _release(rec, 700, 200)                 # far enough to look like a drag

    assert len(rec._steps) == 1
    step = rec._steps[0]
    assert step.data.get("clicks") == 2
    assert step.kind == SeqStep.CLICK, (
        "a double-click whose second release drifted was promoted to a drag")
