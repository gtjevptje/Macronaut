"""
What the recorder writes down about *when* things happened.

Every duration in a recording — the gap before a step, how long a key was
held, how long a swipe took — was measured with `time.monotonic()`, which on
Windows under Python <= 3.12 is `GetTickCount64` at **15.625 ms**. So every
recorded time was rounded to a multiple of that tick before anything could
replay it, and the error is baked into the saved flow: no amount of accuracy
in the engine can recover a number that was wrong when it was written.

This is the measuring half of the trap `flow_exec.sleep` documents on the
pacing side, and it is the older of the two. Measured 8 September 2026 by
driving the real recorder with real clicks (`_on_click` at a controlled rate,
truth taken from `perf_counter`):

    real gap    recorded              worst error
      30 ms     15 / 31               -15.4 ms
      60 ms     47 / 62               -13.3 ms
     120 ms     109 / 110 / 125       -11.4 ms
     500 ms     500 / 515             +14.5 ms

Every recorded value is a tick multiple, and at a 30 ms gap — ordinary fast
clicking — that is a 50% error on the step. Driving the same 23 ms gap through
both clocks: median error **8.00 ms** on monotonic with 0 of 11 values off a
tick boundary, against **0.00 ms** and 11 of 11 on `perf_counter`.

Errors do not compound (each delay is a difference of two timestamps from the
same clock, so the total is out by at most one tick however long the recording
is) — it is the individual steps that are wrong, which is exactly what a
recording's rhythm is made of.
"""
import ast
import os
import statistics
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import recorder as R

TICK_MS = 15.625            # GetTickCount64's resolution, in milliseconds


@pytest.fixture
def rec(monkeypatch):
    """A recorder in 'recording' state with no pynput listener behind it —
    `start()` is what creates those, and it is never called.

    ⚠ Unlike every other recorder test, the clock is NOT faked here. These
    tests are about the resolution of the real one, so a stubbed clock would
    measure nothing.
    """
    monkeypatch.setattr(R.SequenceRecorder, "_key_to_str",
                        staticmethod(lambda key: key))
    r = R.SequenceRecorder()
    r._recording = True
    r._t_last = time.perf_counter()
    r._mods_down = set()
    r._down = {}
    r._keys_down = {}
    r._mods_info = {}
    return r


def _spin(seconds):
    """Busy-wait. `time.sleep` has its own overshoot and would blur exactly the
    thing being measured."""
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        pass


def _on_tick(ms, tol=1.0):
    return abs(ms - round(ms / TICK_MS) * TICK_MS) <= tol


# ── the clock itself ─────────────────────────────────────────────────────────

def test_the_recorder_does_not_time_anything_with_the_coarse_clock():
    """⚠ Wiring, read with `ast` — and the reason it is read rather than
    grepped is that the module's prose names `monotonic` in order to explain
    why it must not be used.

    Scoped to `SequenceRecorder`. `PlaybackWorker` below it is dead code and
    uses the clock for multi-second timeouts, where a 15 ms tick is nothing.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "recorder.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())

    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "SequenceRecorder")
    bad = []
    for node in ast.walk(cls):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "monotonic"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "time"):
            bad.append(node.lineno)
    assert not bad, (
        "recorder.py lines " + ", ".join(map(str, bad))
        + ": SequenceRecorder timed something with GetTickCount64 (15.625 ms). "
          "Every duration it writes is rounded to a tick before playback ever "
          "sees it. Use time.perf_counter().")


# ── what that means for a recording ──────────────────────────────────────────

def test_a_recorded_gap_is_the_gap_that_happened(rec):
    """⚠ 23 ms is chosen, not arbitrary: it sits between two tick boundaries,
    so a coarse clock has to round it to 15.6 or 31.2 whatever the phase. A gap
    that happens to land near a boundary would be recorded correctly by
    accident and the test would pass against the bug."""
    gaps = []
    last = time.perf_counter()
    for i in range(14):
        _spin(0.023)
        now = time.perf_counter()
        gaps.append((now - last) * 1000.0)
        last = now
        rec._on_click(60 + i * 40, 80, "left", True)
        rec._on_click(60 + i * 40, 80, "left", False)

    got = [s.delay_ms for s in rec.steps if s.kind == R.SeqStep.CLICK][1:]
    truth = gaps[1:len(got) + 1]
    assert len(got) >= 8, f"only {len(got)} clicks recorded"

    err = statistics.median(abs(g - t) for g, t in zip(got, truth))
    assert err <= 3.0, (
        f"median error {err:.2f} ms against a {TICK_MS} ms clock tick — "
        f"recorded {[round(g, 1) for g in got[:6]]} for real gaps "
        f"{[round(t, 1) for t in truth[:6]]}")


def test_recorded_gaps_are_not_all_multiples_of_one_tick(rec):
    """The same fact stated so it cannot pass by luck: a coarse clock can only
    ever produce tick multiples, so finding values *between* them is proof the
    resolution is real rather than proof this run was lucky."""
    last = time.perf_counter()
    for i in range(16):
        _spin(0.023)
        last = time.perf_counter()
        rec._on_click(60 + i * 40, 80, "left", True)
        rec._on_click(60 + i * 40, 80, "left", False)

    got = [s.delay_ms for s in rec.steps if s.kind == R.SeqStep.CLICK][1:]
    off = [g for g in got if not _on_tick(g)]
    assert len(off) >= len(got) // 2, (
        f"{len(off)} of {len(got)} recorded gaps sit off a {TICK_MS} ms "
        f"boundary; the rest are quantised: {[round(g, 1) for g in got[:8]]}")


def test_a_short_gap_is_not_rounded_away_to_nothing(rec):
    """The worst case, and the one people actually record: clicking faster
    than the clock ticks. Under GetTickCount64 a 6 ms gap is 0 ms or 15.6 ms —
    a steady rhythm replayed as a stutter."""
    for i in range(12):
        _spin(0.006)
        rec._on_click(40 + i * 40, 50, "left", True)
        rec._on_click(40 + i * 40, 50, "left", False)

    got = [s.delay_ms for s in rec.steps if s.kind == R.SeqStep.CLICK][1:]
    assert got, "nothing recorded"
    med = statistics.median(got)
    assert 2.0 <= med <= 12.0, (
        f"median recorded gap {med:.1f} ms for a real 6 ms rhythm: "
        f"{[round(g, 1) for g in got[:8]]}")


def test_a_key_hold_lasts_as_long_as_it_was_held(rec):
    """`hold_ms` is replayed literally and is not scaled by speed_factor — it
    is gameplay-semantic real time — so a tick of error goes straight into the
    game."""
    # Past HOLD_MIN_MS, or this records as a tap and carries no duration at
    # all. 351.5625 ms is the exact midpoint between two tick boundaries
    # (22 ticks is 343.75, 23 is 359.375), so a coarse clock is ~7.8 ms out
    # whatever its phase. ⚠ 355 ms was the first choice and it quantises to
    # 359.375 -- 4.0 ms out, which slipped through a `<= 4.0` bound and let
    # this test pass against the bug it was written for.
    rec._on_key_press("a")
    t0 = time.perf_counter()
    _spin(0.3515625)
    held_real = (time.perf_counter() - t0) * 1000.0
    rec._on_key_release("a")

    keys = [s for s in rec.steps if s.kind in (R.SeqStep.KEY, R.SeqStep.COMBO)]
    assert keys, "a 355 ms press recorded no key step at all"
    hold = keys[-1].data.get("hold_ms")
    assert hold, f"no hold_ms on a {held_real:.0f} ms press: {keys[-1].data}"
    assert abs(hold - held_real) <= 3.0, (
        f"recorded {hold} ms for a {held_real:.1f} ms hold — a tick of error "
        "goes straight into the game, since hold_ms is replayed literally")


def test_a_whole_recording_does_not_drift(rec):
    """Each delay is a difference of two readings of the same clock, so the
    errors must not accumulate however many steps there are. This holds under
    the coarse clock too — it is here so that fixing the resolution does not
    quietly introduce drift, which would be the worse bug."""
    t0 = time.perf_counter()
    for i in range(20):
        _spin(0.012)
        rec._on_click(30 + i * 40, 40, "left", True)
        rec._on_click(30 + i * 40, 40, "left", False)
    span = (time.perf_counter() - t0) * 1000.0

    got = [s.delay_ms for s in rec.steps if s.kind == R.SeqStep.CLICK]
    total = sum(got[1:])
    assert abs(total - span) <= max(20.0, span * 0.06), (
        f"recorded delays total {total:.0f} ms over a {span:.0f} ms recording")


# ── a spin has a speed, and it was thrown away ───────────────────────────────
#
# The recorder merges consecutive wheel notches into one step — pynput fires
# once per detent, so a single flick would otherwise be a dozen identical
# nodes. But it wrote `speed_nps: 0`, which means "as fast as the backend will
# send them", however long the flick actually took.
#
# Measured 8 September 2026: twelve detents spun over 360 ms replayed as
# **twelve notches in 0.0 ms**, and the whole recording came back 231 ms
# against 563 ms — 59% short, the largest single-step error found in this pass.
#
# ⚠ And it is not only a timing fault. "One event per notch, never one big
# roll" is written down three times in this codebase — for typing, for the
# wheel, and for a drag's intermediate moves — because a receiver reads input
# once a frame and takes a bounded amount per pass. Twelve notches in one
# instant is the batched-typing bug wearing a different hat.
#
# The recorder knows the rate: it is merging against a timestamp already.

def test_a_spun_wheel_records_the_speed_it_was_spun_at(rec):
    for i in range(12):
        rec._on_scroll(400, 400, 0, -1)
        if i < 11:
            _spin(0.03)

    steps = [s for s in rec.steps if s.kind == R.SeqStep.SCROLL]
    assert len(steps) == 1, f"the flick came out as {len(steps)} steps"
    st = steps[0]
    assert st.data["amount"] == 12
    nps = st.data.get("speed_nps", 0)
    assert nps, "the spin's speed was recorded as 0 — 'as fast as possible'"
    # 11 intervals of 30 ms is 33.3 notches/second.
    # 11 gaps of 30 ms is 33.3. Counting notches instead would say 36.4.
    assert 29.0 <= nps <= 35.0, f"recorded {nps:.1f} notches/second for ~33"


def test_the_recorded_rate_replays_the_spin_in_the_time_it_took(rec):
    """⚠ The rate is `(n-1) / elapsed`, not `n / elapsed`, and that is what
    makes it land: `_do_scroll` sends the first notch immediately and waits
    between the rest, so it spends exactly `(n-1)/cps`. Twelve detents 30 ms
    apart span 330 ms, not 360."""
    import flow
    import flow_exec

    # ⚠ First notch to LAST notch, not the whole loop: that span is what the
    # replay reproduces, and measuring it loosely is what let a mutation using
    # n/elapsed instead of (n-1)/elapsed survive. The two differ by exactly one
    # period — 27 ms here — so the bound has to be tighter than that.
    fired = []
    for i in range(12):
        fired.append(time.perf_counter())
        rec._on_scroll(400, 400, 0, -1)
        if i < 11:
            _spin(0.03)
    spun_ms = (fired[-1] - fired[0]) * 1000.0

    st = [s for s in rec.steps if s.kind == R.SeqStep.SCROLL][0]

    class _M:
        position = (400, 400)

        def __init__(self):
            self.at = []

        def scroll(self, dx, dy):
            self.at.append(time.perf_counter())

        def click(self, b, n=1):
            pass

        def press(self, b):
            pass

        def release(self, b):
            pass

    m = _M()
    fw = flow_exec.FlowWorker(flow.FlowGraph())
    fw._mouse = m
    fw._running = True
    fw._smooth_mouse = True
    fw._travel_rng = None
    fw.speed_factor = 1.0

    fw.do_action({"kind": "scroll", "data": st.data, "delay_ms": 0}, {})

    assert len(m.at) == 12, f"{len(m.at)} notches sent of 12"
    replayed_ms = (m.at[-1] - m.at[0]) * 1000.0
    assert abs(replayed_ms - spun_ms) <= 15.0, (
        f"spun over {spun_ms:.0f} ms, replayed over {replayed_ms:.0f} ms")


def test_a_single_notch_has_no_speed_to_record(rec):
    """One detent is instantaneous — there is no interval to measure, and
    inventing a rate for it would pace a step that has nothing to pace."""
    rec._on_scroll(400, 400, 0, -1)
    st = [s for s in rec.steps if s.kind == R.SeqStep.SCROLL][0]
    assert st.data["amount"] == 1
    assert st.data.get("speed_nps", 0) == 0


def test_a_leisurely_spin_is_not_recorded_as_a_flick(rec):
    """Three notches spread over 300 ms is a different gesture from three in
    30 ms, and a list being scrolled slowly cares about the difference."""
    for i in range(3):
        rec._on_scroll(400, 400, 0, 1)
        if i < 2:
            _spin(0.15)
    st = [s for s in rec.steps if s.kind == R.SeqStep.SCROLL][0]
    nps = st.data.get("speed_nps", 0)
    assert nps, "no speed recorded"
    # 2 gaps of 150 ms is 6.7 notches/second. Counting the notches rather
    # than the gaps would say 10, so the bound has to exclude it.
    assert 5.5 <= nps <= 8.0, f"recorded {nps:.1f} notches/second for ~6.7"


# ── how the new steps read ───────────────────────────────────────────────────

def test_a_double_click_with_modifiers_reads_in_the_right_order():
    """⚠ `SeqStep.description()` built "Double-" + the modifier prefix, giving
    **Double-Ctrl+Shift+Forward click** — which reads as a doubled Ctrl. The
    canvas renderer beside it (`flow._action_summary`) had it right, so the two
    disagreed about the same step."""
    import flow
    st = R.SeqStep(R.SeqStep.CLICK,
                   {"x": 100, "y": 200, "button": "x2", "clicks": 2,
                    "mods": ["ctrl", "shift"]}, 0.0)
    text = st.description()
    assert text.startswith("Ctrl+Shift+"), text
    assert "Double-Ctrl" not in text, f"reads as a doubled modifier: {text}"
    assert "Double" in text and "Forward" in text, text
    # And the two renderers agree on the order they put things in.
    canvas = flow._action_summary({"kind": "click", "data": st.data})
    assert canvas.startswith("Ctrl+Shift+"), canvas


def test_a_scroll_says_how_fast_it_was_spun():
    """The rate is a recorded measurement now, so a flick and a slow drag of
    the wheel are different steps — and they rendered identically."""
    import flow
    flick = flow._action_summary({"kind": "scroll", "data": {
        "direction": "down", "amount": 12, "speed_nps": 33.06,
        "at_cursor": True}})
    slow = flow._action_summary({"kind": "scroll", "data": {
        "direction": "down", "amount": 12, "speed_nps": 6.7,
        "at_cursor": True}})
    assert flick != slow, f"a flick and a slow spin both read {flick!r}"
    assert "33" in flick, flick
    assert "6.7" in slow or "6,7" in slow, slow


def test_a_scroll_with_no_pacing_says_nothing_extra():
    """0 means "as fast as the backend will take them", which is the default
    and the common case; printing a rate for it would be noise on every
    hand-built scroll."""
    import flow
    s = flow._action_summary({"kind": "scroll", "data": {
        "direction": "up", "amount": 3, "speed_nps": 0, "at_cursor": True}})
    assert s == "Scroll ↑ 3", s
