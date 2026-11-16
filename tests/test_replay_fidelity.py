"""
A recording should play back in the time it took to make.

Recorded steps carry a gap, and `do_action` already spends that gap gliding
rather than sleeping through it and then gliding on top (see "the glide is
spent inside the recorded gap" in test_flow.py). What was left is the other
half of the same arithmetic: what happens when the glide is *longer* than the
gap.

The pointer glides at a fixed `mouse_travel_pps` — 3000 by default. A hand does
not. Measured 8 September 2026, driving the real recorder with clicks 180 ms
apart at ten scattered points, then replaying the recorded steps through a real
FlowWorker:

    smooth_mouse off    1806 ms replayed against 1623 ms recorded   (+3 ms)
    smooth_mouse on     2385 ms replayed against 1623 ms recorded   (+590 ms)

and smooth movement is on by default. Per step that is +81 ms; the long jumps
are the ones that pay it. 854 px at 3000 pps is 285 ms, but the recording is
evidence that the pointer covered those 854 px in 180 ms, because that is when
the next click landed. Gliding it in 285 ms is not caution, it is slower than
what actually happened -- and the error is per-step, so a longer recording
drifts further.

So a glide is capped by the gap it is spending. The floor is `MIN_TRAVEL_MS`:
below that the compression would be a teleport, which is the thing the glide
exists to avoid, and at that size a receiver cannot tell the difference anyway.
"""
import os
import statistics
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import flow
import flow_exec
import recorder as R


# ── the rule, on its own ─────────────────────────────────────────────────────

def test_a_glide_is_not_slowed_down_by_a_gap_it_already_fits_in():
    """The common case must be untouched: a short hop inside a long pause
    glides at the settings' speed and the rest of the gap is a pause."""
    natural = flow.travel_duration_ms(300.0, flow.DEFAULT_TRAVEL_PPS)
    assert flow.travel_within(natural, 5000.0) == natural


def test_a_glide_never_outlasts_the_gap_it_is_spending():
    natural = flow.travel_duration_ms(854.0, flow.DEFAULT_TRAVEL_PPS)
    assert natural > 180.0, "pick a jump the default speed cannot make in time"
    assert flow.travel_within(natural, 180.0) == 180.0


def test_a_gap_too_short_to_move_in_still_gets_a_movement():
    """⚠ The floor, and why it is not zero. Compressing a 900 px jump into the
    4 ms between two fast clicks is a teleport with extra steps — and a
    teleport is the exact failure the glide was built to fix."""
    natural = flow.travel_duration_ms(900.0, flow.DEFAULT_TRAVEL_PPS)
    assert flow.travel_within(natural, 4.0) == flow.MIN_TRAVEL_MS
    assert flow.travel_within(natural, 0.0) == natural, (
        "no gap recorded is not the same as a gap of zero — a step with no "
        "delay has nothing to fit into and keeps its natural speed")


def test_the_cap_never_invents_time():
    """Whatever the inputs, the answer is a real duration between the floor
    and what the glide would have taken unaided."""
    for dist in (30.0, 200.0, 854.0, 4000.0):
        natural = flow.travel_duration_ms(dist, flow.DEFAULT_TRAVEL_PPS)
        for gap in (0.0, 1.0, 40.0, 180.0, 5000.0, -12.0):
            got = flow.travel_within(natural, gap)
            assert flow.MIN_TRAVEL_MS <= got <= natural, (dist, gap, got)


# ── through the engine ───────────────────────────────────────────────────────

class _StubMouse:
    def __init__(self, at=(0, 0)):
        self.position = at
        self.clicks = []

    def click(self, btn, n=1):
        self.clicks.append(time.perf_counter())

    def press(self, b):
        pass

    def release(self, b):
        pass

    def scroll(self, dx, dy):
        pass


def _worker(mouse, smooth=True):
    fw = flow_exec.FlowWorker(flow.FlowGraph())
    fw._mouse = mouse
    fw._running = True
    fw._smooth_mouse = smooth
    fw._travel_pps = flow.DEFAULT_TRAVEL_PPS
    fw._travel_human = False
    fw._travel_rng = None
    fw.speed_factor = 1.0
    return fw


def test_a_step_does_not_run_longer_than_its_recorded_gap():
    """⚠ Measured through the engine rather than asserted about the helper:
    the helper being right is worth nothing if `_travel_to` does not consult
    it.

    ⚠ And measured on the wall clock, not by summing a stubbed `sleep`. The
    glide paces itself against a deadline, so a `sleep` that returns instantly
    lets that deadline run ahead of real time and every later request grows to
    chase it — the stub reports 4589 ms for a step that takes 180.
    """
    m = _StubMouse((0, 0))
    fw = _worker(m)

    # 1300 px away, and a 180 ms gap. At 3000 pps the glide alone is 433 ms.
    t0 = time.perf_counter()
    fw.do_action({"kind": "click", "data": {"x": 1300, "y": 0},
                  "delay_ms": 180}, {})
    took = (time.perf_counter() - t0) * 1000.0

    assert took <= 180.0 + 40.0, (
        f"the step took {took:.0f} ms against a 180 ms recorded gap")
    assert took >= flow.MIN_TRAVEL_MS * 0.5, "the pointer did not move at all"
    assert len(m.clicks) == 1


def test_a_generous_gap_is_still_spent_as_a_pause(monkeypatch):
    """The cap must not turn every gap into a glide. A click a short hop away
    after a long pause is mostly pause, and stretching the glide to fill it
    would be its own infidelity."""
    m = _StubMouse((0, 0))
    fw = _worker(m)
    slept = []
    monkeypatch.setattr(fw, "sleep", lambda s: slept.append(s))

    fw.do_action({"kind": "click", "data": {"x": 120, "y": 0},
                  "delay_ms": 1000}, {})

    glide = flow.travel_duration_ms(120.0, flow.DEFAULT_TRAVEL_PPS)
    assert slept, "nothing waited"
    assert abs(slept[0] * 1000.0 - (1000.0 - glide)) < 1.0, (
        "the pre-step pause is no longer the gap minus the glide")


# ── the whole round trip, on the real clock ──────────────────────────────────

def _spin(s):
    end = time.perf_counter() + s
    while time.perf_counter() < end:
        pass


@pytest.fixture
def rec(monkeypatch):
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


POINTS = [(200, 200), (700, 300), (400, 800), (1200, 500), (300, 250),
          (900, 900), (150, 600), (1100, 200), (500, 500), (800, 400)]


def test_a_recording_replays_in_about_the_time_it_took_to_make(rec):
    """⚠ The one that describes the complaint. Ten clicks 180 ms apart at
    scattered points: 1623 ms to record, and 2385 ms to replay before this
    change — half a second of drift on a flow lasting a second and a half."""
    truth = []
    last = time.perf_counter()
    for x, y in POINTS:
        _spin(0.18)
        now = time.perf_counter()
        truth.append((now - last) * 1000.0)
        last = now
        rec._on_click(x, y, "left", True)
        rec._on_click(x, y, "left", False)

    steps = rec.steps
    assert len(steps) == len(POINTS), f"recorded {len(steps)} of {len(POINTS)}"
    recorded_span = sum(truth[1:])

    m = _StubMouse((200, 200))
    fw = _worker(m)
    for st in steps:
        fw.do_action(st.to_dict(), {})

    assert len(m.clicks) == len(POINTS)
    # ⚠ First click to last click on both sides. Timing the replay from before
    # the first do_action would include that step's own delay, which `truth`
    # measures from the start of the recording rather than between clicks —
    # an off-by-one-step comparison that reads as 180 ms of drift.
    replay_span = (m.clicks[-1] - m.clicks[0]) * 1000.0
    drift = replay_span - recorded_span
    assert drift <= max(120.0, recorded_span * 0.10), (
        f"replayed in {replay_span:.0f} ms what took {recorded_span:.0f} ms "
        f"to record — {drift:+.0f} ms of drift over {len(POINTS)} clicks")


def test_the_replayed_clicks_keep_the_recorded_rhythm(rec):
    """Total duration can be right while the individual steps are wrong — a
    fast step and a slow one average out. The rhythm is what a recording is."""
    for x, y in POINTS:
        _spin(0.18)
        rec._on_click(x, y, "left", True)
        rec._on_click(x, y, "left", False)

    m = _StubMouse((200, 200))
    fw = _worker(m)
    for st in rec.steps:
        fw.do_action(st.to_dict(), {})

    gaps = [(b - a) * 1000.0 for a, b in zip(m.clicks, m.clicks[1:])]
    assert gaps
    worst = max(abs(g - 180.0) for g in gaps)
    assert worst <= 60.0, (
        f"replayed intervals {[round(g) for g in gaps]} against a steady "
        "180 ms recording")
    assert statistics.median(gaps) <= 200.0


# ── the glide takes the time it says it takes ────────────────────────────────
#
# ⚠ `do_action` subtracts `_travel_budget` from the recorded gap and then
# glides, so the two numbers have to be the same number. They were not: the
# loop slept *after* each move and skipped the sleep after the last one, so
# every glide finished one period early — a flat ~8 ms at TRAVEL_HZ 120,
# whatever the distance. Measured 8 September 2026:
#
#     300 px   predicted 100.0 ms   actual  92.0 ms
#     900 px   predicted 300.0 ms   actual 292.0 ms
#    1500 px   predicted 500.0 ms   actual 492.1 ms
#
# Small, constant, and in the same direction every time, so it accumulates:
# 8 ms off every travelling step is most of a second across a hundred clicks,
# and it is subtracted from gaps that were measured correctly.

@pytest.mark.parametrize("dist", [300, 900, 1500])
def test_a_glide_lasts_as_long_as_the_budget_said_it_would(dist):
    m = _StubMouse((0, 0))
    fw = _worker(m)
    predicted = flow.travel_duration_ms(dist, flow.DEFAULT_TRAVEL_PPS)

    t0 = time.perf_counter()
    fw._travel_to(dist, 0)
    actual = (time.perf_counter() - t0) * 1000.0

    assert abs(actual - predicted) <= 5.0, (
        f"{dist}px glide: budget said {predicted:.0f} ms, took {actual:.0f} ms")
    assert m.position == (dist, 0), "a glide must still land on its target"


def test_a_short_hop_inside_a_long_gap_keeps_the_whole_gap():
    """The plain case, end to end: eight clicks a small hop apart with 200 ms
    between them. Nothing here is capped — this is purely whether the gap that
    was asked for is the gap that happens."""
    m = _StubMouse((0, 0))
    fw = _worker(m)
    for x in range(100, 100 + 8 * 60, 60):
        fw.do_action({"kind": "click", "data": {"x": x, "y": 0},
                      "delay_ms": 200}, {})

    gaps = [(b - a) * 1000.0 for a, b in zip(m.clicks, m.clicks[1:])]
    assert gaps
    assert abs(statistics.median(gaps) - 200.0) <= 5.0, (
        f"200 ms gaps replayed as {[round(g, 1) for g in gaps]}")


# ── the drag's third settle ──────────────────────────────────────────────────
#
# `_do_drag` is settle-press-settle-travel-settle-release: THREE settles, and
# `flow.drag_total_ms` counts two. That is not a bug in `drag_total_ms` -- the
# two it counts are the two inside the gesture, which is what the recorder
# reproduces when it turns a press-to-release into `duration_ms`. The third is
# spent on *arrival*, before the button goes down, and nothing accounted for it:
#
#   * `_travel_budget` returned the glide alone, so the settle was added on top
#     of a gap that had already been fully spent -- measured at +82 ms on a
#     recorded drag whose gap was 150 ms.
#   * `flow.estimate` read `drag_total_ms` as the node's whole cost, so every
#     drag's timeline bar was one settle short.

def test_the_gap_before_a_drag_covers_its_arrival_settle():
    """⚠ The fidelity half. A recorded drag replays late by exactly one settle
    otherwise, on every drag in the recording."""
    m = _StubMouse((200, 200))
    fw = _worker(m)
    d = {"x": 400, "y": 400, "to_x": 900, "to_y": 700, "duration_ms": 90}
    budget = fw._travel_budget("drag", d)
    glide = flow.travel_duration_ms(
        ((400 - 200) ** 2 + (400 - 200) ** 2) ** 0.5, flow.DEFAULT_TRAVEL_PPS)
    assert budget == pytest.approx(glide + flow.DRAG_SETTLE_MS, abs=1.0), (
        f"budget {budget:.0f} ms for a {glide:.0f} ms glide that is always "
        f"followed by a {flow.DRAG_SETTLE_MS} ms settle before the press")


def test_a_recorded_drag_replays_in_the_time_it_was_drawn():
    """End to end on the real clock: a 150 ms gap and a 250 ms press-to-release
    is a 400 ms step, and it replayed as 482."""
    m = _StubMouse((200, 200))
    fw = _worker(m)
    step = {"kind": "drag", "delay_ms": 150,
            "data": {"x": 400, "y": 400, "to_x": 900, "to_y": 700,
                     "button": "left",
                     "duration_ms": 250 - 2 * flow.DRAG_SETTLE_MS}}
    t0 = time.perf_counter()
    fw.do_action(step, {})
    took = (time.perf_counter() - t0) * 1000.0
    assert abs(took - 400.0) <= 30.0, f"the drag step took {took:.0f} ms of 400"


def test_the_press_to_release_span_is_still_what_was_recorded():
    """⚠ The half that must NOT move. `drag_total_ms` is the gesture, and the
    recorder subtracts exactly those two settles to reproduce a swipe's speed
    — the thing a receiver is usually measuring."""
    d = {"duration_ms": 500 - 2 * flow.DRAG_SETTLE_MS}
    assert flow.drag_total_ms(d) == pytest.approx(500.0, abs=1.0)


def test_a_drags_timeline_bar_counts_every_settle_it_spends():
    d = {"x": 0, "y": 0, "to_x": 400, "to_y": 0, "duration_ms": 300}
    n = flow.FlowNode("d", flow.N_ACTION, {"step": {"kind": "drag", "data": d}})
    est = flow.estimate(n)
    assert est.ms == pytest.approx(
        flow.drag_total_ms(d) + flow.DRAG_SETTLE_MS, abs=1.0), (
        "the bar is one settle short of what _do_drag actually spends")


def test_a_far_drag_start_reached_in_a_short_gap_still_fits():
    """⚠ The case that makes the settle *subtraction* observable, and without
    it a mutation removing that subtraction survives: the drag above starts
    only 283 px away, so its glide already fits the gap and the cap never
    engages.

    Here the start is 1500 px off with a 200 ms gap — a 500 ms glide at the
    default speed. The approach and the arrival settle share the gap between
    them; handing the whole gap to the glide puts the settle on top of it and
    the press lands one settle late.
    """
    m = _StubMouse((0, 0))
    fw = _worker(m)
    step = {"kind": "drag", "delay_ms": 200,
            "data": {"x": 1500, "y": 0, "to_x": 1600, "to_y": 0,
                     "button": "left", "duration_ms": 40}}
    presses = []
    real_press = m.press
    m.press = lambda b: (presses.append(time.perf_counter()), real_press(b))[1]

    t0 = time.perf_counter()
    fw.do_action(step, {})
    to_press = (presses[0] - t0) * 1000.0 if presses else -1.0

    assert presses, "the drag never pressed"
    assert abs(to_press - 200.0) <= 25.0, (
        f"the button went down {to_press:.0f} ms into a 200 ms gap — the "
        "approach and its settle have to share it, not follow one another")


# ── the modifier press is part of the gap too ────────────────────────────────
#
# `_press_mods` sleeps `key_hold_ms` after each modifier it presses, so a
# receiver polling once a frame sees the modifier before the button arrives.
# That sleep happened *after* the gap had been spent, so a recorded ctrl-click
# replayed one key-hold late and a ctrl+shift-click two.
#
# `key_hold_ms` defaults to 60, so that is 60 ms and 120 ms per step on an
# ordinary install — seconds across the twenty ctrl-clicks that a multi-select
# recording is made of. The hand pressed ctrl *during* the gap before the
# click, which is precisely where the recorder measured it, so the gap is where
# it belongs.

def _mod_worker(hold_ms=60):
    m = _StubMouse((300, 300))
    fw = _worker(m)
    fw._key_hold_s = hold_ms / 1000.0

    class _Kb:
        def press(self, k):
            pass

        def release(self, k):
            pass

    fw._kb = _Kb()
    return fw, m


@pytest.mark.parametrize("mods", [["ctrl"], ["ctrl", "shift"]])
def test_a_recorded_modifier_click_does_not_land_late(mods):
    fw, m = _mod_worker()
    d = {"x": 320, "y": 300, "mods": mods}

    t0 = time.perf_counter()
    fw.do_action({"kind": "click", "data": d, "delay_ms": 300}, {})
    took = (time.perf_counter() - t0) * 1000.0

    assert abs(took - 300.0) <= 25.0, (
        f"a {'+'.join(mods)}-click took {took:.0f} ms of a 300 ms gap — the "
        f"{len(mods)} modifier press(es) were added on top of it")


def test_a_modifier_already_held_costs_the_gap_nothing():
    """⚠ `_press_mods` skips a modifier the flow is already holding, so the
    budget has to skip it too — charging for a press that never happens would
    make the step land *early*, which is the same bug facing the other way."""
    fw, m = _mod_worker()
    fw._held["ctrl"] = object()             # a Hold-down node is holding it

    t0 = time.perf_counter()
    fw.do_action({"kind": "click", "data": {"x": 320, "y": 300,
                                            "mods": ["ctrl"]},
                  "delay_ms": 300}, {})
    took = (time.perf_counter() - t0) * 1000.0
    assert abs(took - 300.0) <= 25.0, f"{took:.0f} ms of a 300 ms gap"


def test_a_plain_click_is_charged_nothing_for_modifiers():
    fw, _m = _mod_worker()
    assert fw._mods_budget("click", {"x": 10, "y": 10}) == 0.0
    assert fw._mods_budget("key", {"mods": ["ctrl"]}) == 0.0, (
        "only the pointer kinds press modifiers this way — see "
        "flow_exec._MOD_POINTER_KINDS")


def test_the_modifier_budget_is_charged_with_smooth_movement_off():
    """⚠ Not a travel cost. `_travel_budget` returns 0 when smooth movement is
    switched off, and folding the modifiers into it would have made them free
    there — on the setting chosen by people who care most about the rate."""
    fw, _m = _mod_worker()
    fw._smooth_mouse = False
    assert fw._travel_budget("click", {"x": 900, "y": 900}) == 0.0
    assert fw._mods_budget("click", {"mods": ["ctrl"]}) == pytest.approx(60.0)


# ── the speed multiplier actually multiplies speed ───────────────────────────
#
# ⚠ `speed_factor` is a DELAY multiplier, not a speed one: `main` passes
# `1.0 / speed`, so the UI's "2×" arrives here as 0.5. Reading it the other way
# makes every measurement of this come out backwards.
#
# Capping the glide by the gap fixed this as a side effect, and the size of it
# is worth recording. Six clicks 180 ms apart at scattered points, measured
# 8 September 2026:
#
#     UI speed      with the cap      without it
#       1x            1082 ms          1576 ms   (+46%)
#       2x             542 ms          1485 ms  (+175%)
#       4x             272 ms          1440 ms  (+433%)
#
# Without it the glides ran at their own fixed speed whatever the multiplier
# said, so they came to dominate the run and "4× faster" delivered 1.07×. The
# setting was very nearly inert on any recording with movement in it — which is
# every recording made by moving a mouse.

_SCATTERED = [(200, 200), (900, 700), (300, 250), (1200, 500), (400, 800),
              (1000, 300)]


@pytest.mark.parametrize("ui_speed", [1.0, 2.0, 4.0])
def test_playing_a_recording_faster_actually_plays_it_faster(ui_speed):
    m = _StubMouse((200, 200))
    fw = _worker(m)
    fw.speed_factor = 1.0 / ui_speed        # what main.py passes

    t0 = time.perf_counter()
    for x, y in _SCATTERED:
        fw.do_action({"kind": "click", "data": {"x": x, "y": y},
                      "delay_ms": 180}, {})
    took = (time.perf_counter() - t0) * 1000.0

    ideal = 180.0 * len(_SCATTERED) / ui_speed
    assert took <= ideal * 1.20 + 30.0, (
        f"{ui_speed:g}x speed took {took:.0f} ms against an ideal {ideal:.0f} — "
        "the glides are not being compressed with the gaps")


def test_the_glide_floor_bounds_how_fast_a_recording_can_be_played():
    """⚠ Not a defect, and worth stating so it is not read as one. The speed box
    goes to 50×, and at some point the gap is shorter than `MIN_TRAVEL_MS`;
    below that the pointer would be teleporting, which is the failure the glide
    exists to prevent. So a recording full of long jumps has a floor, and the
    floor is per step rather than a fixed fraction of the run."""
    m = _StubMouse((200, 200))
    fw = _worker(m)
    fw.speed_factor = 1.0 / 50.0

    t0 = time.perf_counter()
    for x, y in _SCATTERED:
        fw.do_action({"kind": "click", "data": {"x": x, "y": y},
                      "delay_ms": 180}, {})
    took = (time.perf_counter() - t0) * 1000.0

    floor = flow.MIN_TRAVEL_MS * len(_SCATTERED)
    assert took >= floor * 0.8, f"{took:.0f} ms is below the glide floor"
    assert took <= floor * 1.6 + 40.0, (
        f"{took:.0f} ms at 50x, floor is {floor:.0f} — something other than "
        "the floor is holding it up")


# ── a scroll's bar says what the scroll costs ────────────────────────────────
#
# ⚠ The mirror of the drag's third settle, found the same way: compare what
# `flow.estimate` predicts against what the engine actually spends.
#
# `_do_scroll` sends the first notch immediately and waits only *between* the
# rest — `if i < n - 1` — so n notches at `cps` cost (n-1)/cps. `estimate`
# returned n/cps. The error is exactly one period whatever n is, so as a
# fraction it is 1/n:
#
#     notches  rate   estimate   actual   over
#        2     10.0     200 ms   100 ms   100%
#        3      6.7     448 ms   299 ms    50%
#       12     33.1     363 ms   333 ms     9%
#      100     50.0    2000 ms  1980 ms     1%
#
# ⚠ It was latent until 8 September 2026 and this session made it live: every
# *recorded* scroll used to carry `speed_nps: 0`, which takes the unpaced
# branch and returns 0. Measuring the spin's real rate put every recorded
# flick onto the paced branch.
#
# The engine is the one that is right, and deliberately: a rate is about the
# intervals between notches, and a trailing pause after the last one serves
# nothing — unlike a drag's final settle, which is what lets a receiver see the
# release.

@pytest.mark.parametrize("notches,cps", [(2, 10.0), (3, 6.7), (12, 33.06)])
def test_a_paced_scrolls_bar_matches_what_it_spends(notches, cps):
    d = {"direction": "down", "amount": notches, "speed_nps": cps,
         "at_cursor": True}
    n = flow.FlowNode("s", flow.N_ACTION,
                      {"step": {"kind": "scroll", "data": d}})
    est = flow.estimate(n)
    assert est.source == flow.EXACT
    assert est.ms == pytest.approx(((notches - 1) / cps) * 1000.0, abs=2), (
        f"{notches} notches at {cps}/s predicted {est.ms} ms")


def test_the_prediction_is_measured_against_the_engine():
    """⚠ Not an assertion about arithmetic: the point is that the number the
    timeline draws is the number the run takes."""
    m = _StubMouse((0, 0))
    fw = _worker(m)
    d = {"direction": "down", "amount": 12, "speed_nps": 33.06,
         "at_cursor": True}
    n = flow.FlowNode("s", flow.N_ACTION,
                      {"step": {"kind": "scroll", "data": d}})
    predicted = flow.estimate(n).ms

    t0 = time.perf_counter()
    fw.do_action({"kind": "scroll", "data": d, "delay_ms": 0}, {})
    actual = (time.perf_counter() - t0) * 1000.0

    assert abs(actual - predicted) <= 25.0, (
        f"bar says {predicted} ms, the scroll took {actual:.0f} ms")


def test_an_unpaced_scroll_still_costs_nothing():
    """speed 0 is a burst — one event per notch, as fast as the backend takes
    them — and rounds to nothing. It must not become (n-1)/0."""
    d = {"direction": "up", "amount": 8, "speed_nps": 0, "at_cursor": True}
    n = flow.FlowNode("s", flow.N_ACTION,
                      {"step": {"kind": "scroll", "data": d}})
    assert flow.estimate(n).ms == 0


def test_a_single_notch_costs_nothing_even_when_paced():
    """One notch has no interval to wait through. n-1 is 0, and the arithmetic
    has to survive that rather than going negative."""
    d = {"direction": "up", "amount": 1, "speed_nps": 5.0, "at_cursor": True}
    n = flow.FlowNode("s", flow.N_ACTION,
                      {"step": {"kind": "scroll", "data": d}})
    assert flow.estimate(n).ms == 0


# ── a key step's bar reads the setting the engine reads ──────────────────────
#
# ⚠ Third instance of the same question, and the first two (the drag's third
# settle, the scroll's missing period) are why it got asked: does what the
# timeline draws match what the engine spends?
#
# `flow.KEY_SETTLE_MS` is a hardcoded 60.0. The engine spends
# `settings.key_hold_ms`, which defaults to 60 — so they agree until somebody
# changes it, and then the bar is wrong by the ratio while still labelled
# EXACT. Measured on a machine with it set to 5:
#
#     key step             bar     actual
#     one key, tap        60 ms      5 ms
#     three-key combo    180 ms     16 ms
#     one key x5         540 ms     49 ms
#
# A hold is unaffected — `hold_ms` dominates and is real time either way.
#
# The fix follows the `text` branch beside it, which already asks
# `input_backends` for the real rate rather than trusting its own constant.
# ⚠ Cheap in the app: `_live_settings()` returns the registered in-memory
# manager (`main.py` calls `set_active`), so this is a lookup and not a file
# read on the path that draws a bar per node.

def _key_worker(hold_ms):
    fw = flow_exec.FlowWorker(flow.FlowGraph())

    class _Kb:
        def press(self, k):
            pass

        def release(self, k):
            pass

    fw._kb = _Kb()
    fw._mouse = _StubMouse((0, 0))
    fw._running = True
    fw._travel_rng = None
    fw._key_hold_s = hold_ms / 1000.0
    return fw


@pytest.mark.parametrize("data,label", [
    ({"keys": ["a"]}, "one key"),
    ({"keys": ["ctrl", "shift", "a"]}, "three-key combo"),
    ({"keys": ["a"], "repeat": 5}, "one key repeated"),
])
def test_a_key_steps_bar_uses_the_hold_the_engine_uses(monkeypatch, data, label):
    import input_backends

    class _S:
        key_hold_ms = 5

    monkeypatch.setattr(input_backends, "_live_settings", lambda: _S())

    n = flow.FlowNode("k", flow.N_ACTION,
                      {"step": {"kind": "key", "data": data}})
    est = flow.estimate(n)
    assert est.source == flow.EXACT

    fw = _key_worker(5)
    t0 = time.perf_counter()
    fw.do_action({"kind": "key", "data": data, "delay_ms": 0}, {})
    actual = (time.perf_counter() - t0) * 1000.0

    assert abs(est.ms - actual) <= 25.0, (
        f"{label}: bar says {est.ms} ms, the step took {actual:.0f} ms")


def test_the_default_hold_still_gives_the_old_answer(monkeypatch):
    """⚠ The setting defaults to 60 and `KEY_SETTLE_MS` is 60, so every flow
    built before this change must estimate exactly as it did. A fix that moved
    the common case would be a worse bug than the one it corrects."""
    import input_backends

    class _S:
        key_hold_ms = 60

    monkeypatch.setattr(input_backends, "_live_settings", lambda: _S())
    n = flow.FlowNode("k", flow.N_ACTION,
                      {"step": {"kind": "key", "data": {"keys": ["a"]}}})
    assert flow.estimate(n).ms == pytest.approx(flow.KEY_SETTLE_MS, abs=1)


def test_an_unreadable_setting_falls_back_to_the_constant(monkeypatch):
    """`flow` sits under settings and must keep working without them — the
    same reason the text branch wraps its lookup."""
    import input_backends

    def boom():
        raise RuntimeError("no settings here")

    monkeypatch.setattr(input_backends, "_live_settings", boom)
    n = flow.FlowNode("k", flow.N_ACTION,
                      {"step": {"kind": "key", "data": {"keys": ["a"]}}})
    assert flow.estimate(n).ms == pytest.approx(flow.KEY_SETTLE_MS, abs=1)


def test_a_held_key_is_unaffected_by_the_settle(monkeypatch):
    import input_backends

    class _S:
        key_hold_ms = 5

    monkeypatch.setattr(input_backends, "_live_settings", lambda: _S())
    n = flow.FlowNode("k", flow.N_ACTION, {"step": {"kind": "key", "data": {
        "keys": ["w"], "hold_ms": 300, "mode": flow.KEY_HOLD}}})
    assert flow.estimate(n).ms == pytest.approx(300, abs=2)
