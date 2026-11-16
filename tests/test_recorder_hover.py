"""Headless tests for recorder.py's hover capture.

The recorder listened to clicks and the wheel and nothing else, so a recording
was a list of places the buttons went down with no account of how the pointer
got between them. `SeqStep.MOVE` existed, the engine ran it, and no recording
could ever contain one.

The case that costs a user an afternoon: hover over a menu to open it, click an
item inside it. The recording holds the click alone, the replay clicks where
the item would have been with the menu still shut, and nothing reports an
error — a click cannot fail. Feeds _on_move/_on_click/_on_scroll directly with
a controlled clock; no real pynput listener is started.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import recorder
from recorder import SequenceRecorder, SeqStep


def _make_recorder(monkeypatch, clock: dict):
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
    monkeypatch.setattr(SequenceRecorder, "_key_to_str", staticmethod(lambda key: key))
    return rec


def _drift(rec, clock, points, dt=0.02):
    """Move the pointer through `points`, one callback each, dt apart."""
    for x, y in points:
        clock["t"] += dt
        rec._on_move(x, y)


def _kinds(rec):
    return [s.kind for s in rec.steps]


# ── (1) the case the whole thing exists for ──────────────────────────────────
def test_a_hover_on_the_way_to_a_click_becomes_a_move_step(monkeypatch):
    """Park over the menu, then click the item that appears under it."""
    clock = {"t": 0.0}
    rec = _make_recorder(monkeypatch, clock)

    _drift(rec, clock, [(300, 80), (400, 100)])     # arrive at the menu
    clock["t"] += 0.6                                # ...and dwell there
    _drift(rec, clock, [(410, 300), (420, 420)])     # move down into the menu
    rec._on_click(420, 420, "left", True)

    assert _kinds(rec) == [SeqStep.MOVE, SeqStep.CLICK]
    assert (rec.steps[0].data["x"], rec.steps[0].data["y"]) == (400, 100)
    assert (rec.steps[1].data["x"], rec.steps[1].data["y"]) == (420, 420)


# ── (2) the noise this would otherwise generate on every single click ────────
def test_the_pointer_leaving_what_it_just_clicked_is_not_a_hover(monkeypatch):
    """A rest is only visible from the far side of it, and the pointer always
    leaves the thing it just clicked — so every click looks like a hover on top
    of itself unless the step before it is taken into account."""
    clock = {"t": 0.0}
    rec = _make_recorder(monkeypatch, clock)

    rec._on_click(100, 100, "left", True)
    rec._on_click(100, 100, "left", False)
    _drift(rec, clock, [(102, 101)])                 # a hand resting on a mouse
    clock["t"] += 0.9                                # a long think, in place
    _drift(rec, clock, [(600, 600)])
    rec._on_click(600, 600, "left", True)

    assert _kinds(rec) == [SeqStep.CLICK, SeqStep.CLICK]


# ── (3) a wheel step aims itself with the pointer, and nothing else ──────────
def test_a_hover_before_a_scroll_is_recorded(monkeypatch):
    """Wheel steps are recorded `at_cursor`, so a replay scrolls whatever the
    pointer is over. Move over a pane and spin the wheel with no click in
    between and the movement IS the aim of the step."""
    clock = {"t": 0.0}
    rec = _make_recorder(monkeypatch, clock)

    _drift(rec, clock, [(700, 400), (760, 480)])
    clock["t"] += 0.5
    _drift(rec, clock, [(762, 482)])
    rec._on_scroll(762, 482, 0, -1)

    assert _kinds(rec) == [SeqStep.MOVE, SeqStep.SCROLL]
    assert (rec.steps[0].data["x"], rec.steps[0].data["y"]) == (760, 480)


# ── (4) a hover is only a hover because of what follows it ───────────────────
def test_a_hover_with_nothing_after_it_is_not_a_step(monkeypatch):
    """The hand on its way to the Stop button rests too."""
    clock = {"t": 0.0}
    rec = _make_recorder(monkeypatch, clock)

    rec._on_click(50, 50, "left", True)
    rec._on_click(50, 50, "left", False)
    _drift(rec, clock, [(500, 500)])
    clock["t"] += 0.8
    _drift(rec, clock, [(900, 20)])                  # off towards Stop
    clock["t"] += 1.0
    rec.stop()

    assert _kinds(rec) == [SeqStep.CLICK]


# ── (5) a pause that is not a pause ──────────────────────────────────────────
def test_a_pointer_that_never_stops_records_no_hover(monkeypatch):
    clock = {"t": 0.0}
    rec = _make_recorder(monkeypatch, clock)

    _drift(rec, clock, [(x, x) for x in range(100, 600, 25)], dt=0.05)
    rec._on_click(600, 600, "left", True)

    assert _kinds(rec) == [SeqStep.CLICK]


# ── (6) the two delays divide the same stretch of time ───────────────────────
def test_the_dwell_is_kept_as_the_following_steps_delay(monkeypatch):
    """The move carries the journey to the hover; the step after it carries the
    dwell plus the journey on from there. Getting this wrong does not lose a
    step, it loses the pause — and the pause is what the menu needed."""
    clock = {"t": 0.0}
    rec = _make_recorder(monkeypatch, clock)

    clock["t"] = 0.2
    rec._on_move(400, 100)
    clock["t"] = 0.9                                 # dwelt 0.7 s
    rec._on_move(420, 420)
    clock["t"] = 1.0
    rec._on_click(420, 420, "left", True)

    move, click = rec.steps
    assert abs(move.delay_ms - 200) < 1, "the journey to the hover"
    assert abs(click.delay_ms - 800) < 1, "the dwell, plus the journey on"
    # And the whole thing still takes as long to replay as it took to record.
    assert abs((move.delay_ms + click.delay_ms) - 1000) < 1


# ── (7) a hover that ends in a keystroke ─────────────────────────────────────
def test_a_hover_before_a_keystroke_is_recorded(monkeypatch):
    """The menu the hover opened is what the keystroke is aimed at."""
    clock = {"t": 0.0}
    rec = _make_recorder(monkeypatch, clock)

    _drift(rec, clock, [(300, 300)])
    clock["t"] += 0.6
    _drift(rec, clock, [(305, 305)])
    rec._on_key_press("a")
    clock["t"] += 0.05
    rec._on_key_release("a")

    assert _kinds(rec) == [SeqStep.MOVE, SeqStep.KEY]


def test_stopping_on_a_stop_key_does_not_flush_a_hover(monkeypatch):
    """f8 ends the recording; it is not a step the hover was leading up to.

    Asserted on the live `step_recorded` signal rather than on `steps`, and
    that is the whole point of the test. stop() already trims anything
    recorded inside its grace window, so a hover flushed by f8 is deleted a
    moment later and the final list looks right either way — but it has been
    announced to the step list by then, and the guard that stops it is not
    the trim. Checking `steps` here passes with the guard removed."""
    clock = {"t": 0.0}
    rec = _make_recorder(monkeypatch, clock)
    announced = []
    rec.step_recorded.connect(announced.append)

    rec._on_click(50, 50, "left", True)
    rec._on_click(50, 50, "left", False)
    _drift(rec, clock, [(500, 500)])
    clock["t"] += 0.8
    _drift(rec, clock, [(505, 505)])
    clock["t"] += 1.0                                # past the stop grace
    rec._on_key_press("f8")

    assert _kinds(rec) == [SeqStep.CLICK]
    assert [s.kind for s in announced] == [SeqStep.CLICK]


def test_a_hover_that_is_then_clicked_is_not_recorded_twice(monkeypatch):
    """Stopping on a button and then pressing it is one step, not two. The
    click already puts the pointer there — and puts it there by travelling,
    which is what the hover was for."""
    clock = {"t": 0.0}
    rec = _make_recorder(monkeypatch, clock)

    _drift(rec, clock, [(500, 500)])
    clock["t"] += 0.7
    _drift(rec, clock, [(503, 501)])                 # settling on the button
    rec._on_click(503, 501, "left", True)

    assert _kinds(rec) == [SeqStep.CLICK]


def test_a_slow_drag_is_one_gesture_and_not_a_string_of_hovers(monkeypatch):
    """Dragging a scrollbar or a map pauses constantly. Those pauses are
    inside a gesture already being recorded as a single step, and reading them
    as hovers strands a move after the drag aimed at somewhere the hand only
    passed through on its way."""
    clock = {"t": 0.0}
    rec = _make_recorder(monkeypatch, clock)

    rec._on_click(200, 200, "left", True)
    _drift(rec, clock, [(300, 200)])
    clock["t"] += 0.9                                # a long pause mid-drag
    _drift(rec, clock, [(500, 200)])
    clock["t"] += 0.8                                # and another
    _drift(rec, clock, [(700, 200)])
    rec._on_click(700, 200, "left", False)           # released -> a drag

    assert _kinds(rec) == [SeqStep.DRAG]

    # ...and the pointer is now at the far END of it, not where the press
    # went down. A drag is promoted by rewriting a step that is already in the
    # list, so nothing else in the recorder learns that. Rest at the far end,
    # then act somewhere unrelated: the hand is leaving a place the drag
    # already put it, which is not a hover, and the click it ends at is too
    # far away to suppress the move on its own.
    _drift(rec, clock, [(702, 201)])
    clock["t"] += 0.7
    _drift(rec, clock, [(400, 400)])
    rec._on_click(100, 100, "left", True)

    assert _kinds(rec) == [SeqStep.DRAG, SeqStep.CLICK]


def test_the_listener_is_actually_asked_for_movement(monkeypatch):
    """The whole feature is one keyword argument, and everything else here
    tests the handler rather than whether anything calls it. Without this, a
    dropped `on_move=` leaves nine green tests and a recorder that has gone
    back to not seeing the mouse move."""
    seen = {}

    class _FakeListener:
        def __init__(self, **kw):
            seen.update(kw)

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(recorder._pm, "Listener", _FakeListener)
    monkeypatch.setattr(recorder._pk, "Listener", _FakeListener)
    rec = SequenceRecorder()
    rec.start()
    try:
        assert seen.get("on_move") == rec._on_move
    finally:
        rec._recording = False


# ── (8) the seam: what is recorded is what the engine is handed ──────────────
def test_a_recorded_hover_survives_the_trip_into_a_node(monkeypatch):
    """`_on_recording_stopped` drops SeqStep.to_dict() straight into a node for
    flow_exec to read back, so the recorder's shape and the engine's are the
    same shape or the step quietly does nothing."""
    clock = {"t": 0.0}
    rec = _make_recorder(monkeypatch, clock)

    _drift(rec, clock, [(640, 200)])
    clock["t"] += 0.7
    _drift(rec, clock, [(640, 500)])
    rec._on_click(640, 500, "left", True)

    step = rec.steps[0].to_dict()
    assert step["kind"] == "move"

    import flow
    import flow_exec

    class _Mouse:
        def __init__(self):
            self.position = (0, 0)

    fw = flow_exec.FlowWorker(flow.FlowGraph())
    fw._mouse = _Mouse()
    fw._running = True
    fw.sleep = lambda s: None
    fw._smooth_mouse = False          # the destination, not the journey

    assert fw.do_action(step, {}) is True
    assert fw._mouse.position == (640, 200), \
        "the engine has to read back the keys the recorder wrote"


# ── the drag takes as long to replay as it took to make ──────────────────────
def test_a_recorded_drag_replays_at_the_speed_it_was_made(monkeypatch):
    """`duration_ms` is the time the pointer spends TRAVELLING — that is what
    flow.drag_duration_ms means by it, what the editor's "Travel time" row
    edits, and what a hand-built drag says. The recorder was writing the whole
    press-to-release into it, and the engine adds a settle either side of the
    travel, so every recorded swipe replayed 160 ms slower than it was made.

    On a gesture whose speed is frequently the thing the receiver measures.
    """
    import flow
    clock = {"t": 0.0}
    rec = _make_recorder(monkeypatch, clock)

    rec._on_click(100, 100, "left", True)
    clock["t"] = 0.9                                 # 900 ms, press to release
    rec._on_click(600, 400, "left", False)

    step = rec.steps[0]
    assert step.kind == SeqStep.DRAG
    assert abs(flow.drag_total_ms(step.data) - 900) < 1, \
        "press-to-release on replay has to match press-to-release as recorded"


def test_a_flick_too_short_for_its_own_settles_is_floored(monkeypatch):
    """The settles are load-bearing — a receiver reads the gesture from where
    the pointer has been, and a press and a first movement in the same frame
    read as a click at the far end. So a 60 ms flick cannot be replayed in
    60 ms, and asking for a negative travel time would be worse than slow."""
    clock = {"t": 0.0}
    rec = _make_recorder(monkeypatch, clock)

    rec._on_click(100, 100, "left", True)
    clock["t"] = 0.06
    rec._on_click(400, 100, "left", False)

    assert rec.steps[0].data["duration_ms"] == 0
