"""Headless tests for recorder.py's CHORD capture: two keys held at the same
time must come out as ONE multi-key step, not two sequential ones.

This is the shape every diagonal in a game has -- forward+left to hug a wall --
and until now the recorder emitted a step per key at that key's own release, so
the engine replayed "forward, THEN left". The overlap is not recoverable from
the saved JSON afterwards (the numbers are identical whether the second key
went down before or after the first came up), so it has to be captured here.

Same harness as test_recorder_hold.py: feed _on_key_press/_on_key_release with
fake key strings and a controlled clock, no real pynput listener.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import recorder
from recorder import SequenceRecorder, SeqStep, HOLD_MIN_MS


def _make_recorder(monkeypatch, clock: dict):
    rec = SequenceRecorder()
    rec._recording = True
    rec._t_last = clock["t"]
    # ⚠ perf_counter, not monotonic: the recorder measures on the fine
    # clock (monotonic is GetTickCount64, 15.625 ms, which quantised
    # every recorded duration).
    monkeypatch.setattr(recorder.time, "perf_counter", lambda: clock["t"])
    monkeypatch.setattr(SequenceRecorder, "_key_to_str", staticmethod(lambda key: key))
    return rec


def _at(rec, clock, t, fn, key):
    clock["t"] = t
    fn(key)


# -- (1) the reported bug, with henwalk's own numbers -------------------------
def test_two_keys_held_together_become_one_step(monkeypatch):
    """z+q for the forward-left diagonal, timings lifted from henwalk.json:
    z down, q down 250ms later, z up at 7.530s, q up 657ms after that."""
    clock = {"t": 0.0}
    rec = _make_recorder(monkeypatch, clock)

    _at(rec, clock, 0.000, rec._on_key_press,   "z")
    _at(rec, clock, 0.250, rec._on_key_press,   "q")
    _at(rec, clock, 7.530, rec._on_key_release, "z")
    _at(rec, clock, 8.187, rec._on_key_release, "q")

    # The 250ms of z alone is under a deliberate hold and is folded into the
    # delay; the shared window is one step; q's tail outlives it and is its own.
    assert [s.data["keys"] for s in rec.steps] == [["z", "q"], ["q"]]
    assert rec.steps[0].kind == SeqStep.COMBO
    assert rec.steps[0].data["hold_ms"] == 7280
    assert rec.steps[1].data["hold_ms"] == 656   # int(), as holds have always been
    assert round(rec.steps[0].delay_ms) == 250     # the dropped sliver's time
    assert rec.steps[1].delay_ms == 0.0            # contiguous with the chord


# -- (2) released together: exactly one step, no tail ------------------------
def test_a_clean_diagonal_is_a_single_step(monkeypatch):
    clock = {"t": 0.0}
    rec = _make_recorder(monkeypatch, clock)

    _at(rec, clock, 0.000, rec._on_key_press,   "q")
    _at(rec, clock, 0.016, rec._on_key_press,   "z")
    _at(rec, clock, 3.000, rec._on_key_release, "q")
    _at(rec, clock, 3.016, rec._on_key_release, "z")

    assert len(rec.steps) == 1
    assert rec.steps[0].data["keys"] == ["q", "z"]
    assert rec.steps[0].data["hold_ms"] == 2984


# -- (3) keys that never overlap are untouched -------------------------------
def test_sequential_holds_stay_two_steps(monkeypatch):
    clock = {"t": 0.0}
    rec = _make_recorder(monkeypatch, clock)

    _at(rec, clock, 0.000, rec._on_key_press,   "q")
    _at(rec, clock, 1.000, rec._on_key_release, "q")
    _at(rec, clock, 1.500, rec._on_key_press,   "z")
    _at(rec, clock, 2.500, rec._on_key_release, "z")

    assert [s.data["keys"] for s in rec.steps] == [["q"], ["z"]]
    assert all(s.kind == SeqStep.KEY for s in rec.steps)
    assert round(rec.steps[1].delay_ms) == 500     # measured from q's release


# -- (4) a chain must never invent a chord that was never held ---------------
def test_a_chain_never_pairs_keys_that_did_not_overlap(monkeypatch):
    """a overlaps b, b overlaps c, but a and c are never down together."""
    clock = {"t": 0.0}
    rec = _make_recorder(monkeypatch, clock)

    _at(rec, clock, 0.000, rec._on_key_press,   "a")
    _at(rec, clock, 1.000, rec._on_key_press,   "b")
    _at(rec, clock, 2.000, rec._on_key_release, "a")
    _at(rec, clock, 2.500, rec._on_key_press,   "c")
    _at(rec, clock, 4.000, rec._on_key_release, "b")
    _at(rec, clock, 5.000, rec._on_key_release, "c")

    keysets = [s.data["keys"] for s in rec.steps]
    assert keysets == [["a"], ["a", "b"], ["b"], ["b", "c"], ["c"]]
    assert ["a", "c"] not in keysets


# -- (5) a chord still held when Stop fires is not lost ----------------------
def test_stop_flushes_a_chord_that_is_still_held(monkeypatch):
    clock = {"t": 0.0}
    rec = _make_recorder(monkeypatch, clock)

    _at(rec, clock, 0.000, rec._on_key_press, "q")
    _at(rec, clock, 0.050, rec._on_key_press, "z")
    clock["t"] = 4.000
    rec.stop()

    assert len(rec.steps) == 1
    assert rec.steps[0].data["keys"] == ["q", "z"]
    assert rec.steps[0].data["hold_ms"] == 3950


# -- (6) a chord shorter than a deliberate hold is still not a tap storm -----
def test_a_brief_chord_does_not_split_into_slivers(monkeypatch):
    clock = {"t": 0.0}
    rec = _make_recorder(monkeypatch, clock)

    _at(rec, clock, 0.000, rec._on_key_press,   "q")
    _at(rec, clock, 0.010, rec._on_key_press,   "z")
    _at(rec, clock, 0.100, rec._on_key_release, "q")
    _at(rec, clock, 0.110, rec._on_key_release, "z")

    # Every interval is under HOLD_MIN_MS, so nothing claims a hold, but the
    # chord must not emit three sliver steps either.
    assert len(rec.steps) <= 1
    for s in rec.steps:
        assert "hold_ms" not in s.data


# -- (7) the seam: a recorded chord survives into a flow node ----------------
def test_a_recorded_chord_reaches_the_engine_as_one_held_step(monkeypatch):
    """Recording and playback are separate halves and the join between them is
    where a multi-key step could quietly lose a key: _on_recording_stopped
    drops SeqStep.to_dict() straight into a node, and flow_exec reads it back.
    Both halves were covered and this seam was not."""
    import flow

    clock = {"t": 0.0}
    rec = _make_recorder(monkeypatch, clock)
    _at(rec, clock, 0.000, rec._on_key_press,   "z")
    _at(rec, clock, 0.250, rec._on_key_press,   "q")
    _at(rec, clock, 7.530, rec._on_key_release, "z")
    _at(rec, clock, 8.187, rec._on_key_release, "q")

    node_data = rec.steps[0].to_dict()          # exactly what the canvas stores
    assert node_data["kind"] in ("key", "combo")
    step = node_data["data"]
    # flow_exec presses every key in this list a settle apart, then holds them
    # all for hold_ms -- the diagonal. Losing either key loses the gesture.
    assert step["keys"] == ["z", "q"]
    assert flow.key_mode(step) == flow.KEY_HOLD
    assert step["hold_ms"] == 7280
