"""
A step's data is whatever is in the file.

Flows are plain JSON and the README invites people to edit one, which is why
`FlowInterpreter` was fuzzed: its real input is anything. The same argument
applies one level down, to the `data` dict of a single step — and that half was
never fuzzed.

Doing it (8 September 2026, 4000 adversarial steps through the real
`do_action`) found two ways to raise, both of which fail the WHOLE RUN rather
than the step, because `FlowWorker.run` treats an exception out of an action as
a run error:

  * `delay_ms` that is not a number — `float(step.get("delay_ms", 0))` with
    None, "", "x", [] or {} raises before anything is guarded. `None` is the
    plausible one: any generator that emits JSON null puts it there.
  * `mods` that is not iterable — `pointer_mods` handles a string and handles
    absence, but a number or a bool raises TypeError out of a set comprehension.

Neither is exotic enough to dismiss, and the accessors beside them
(`key_mode`, `scroll_notches`, `travel_pps`) are all written defensively
already, so the intent is plainly tolerance — these two were just missed.

⚠ The rule this file encodes: a malformed field makes its own step do nothing,
never takes the run down. A run that dies on step 40 of 200 leaves the mouse
and keyboard wherever they were.
"""
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import flow
import flow_exec


class _Kb:
    def __init__(self):
        self.events = []

    def press(self, k):
        self.events.append(("down", str(k)))

    def release(self, k):
        self.events.append(("up", str(k)))

    def type(self, t, **kw):
        self.events.append(("type", t))


class _Mouse:
    def __init__(self):
        self.position = (0, 0)
        self.events = []

    def click(self, b, n=1):
        self.events.append(("click", n))

    def press(self, b):
        self.events.append(("press", None))

    def release(self, b):
        self.events.append(("release", None))

    def scroll(self, dx, dy):
        self.events.append(("scroll", (dx, dy)))


@pytest.fixture
def worker():
    fw = flow_exec.FlowWorker(flow.FlowGraph())
    fw._mouse = _Mouse()
    fw._kb = _Kb()
    fw._running = True
    fw._travel_rng = None
    fw.sleep = lambda s: None
    return fw


JUNK = [None, "", "x", [], {}, True, float("nan"), float("inf"),
        float("-inf"), -99999]


# ── the delay in front of a step ─────────────────────────────────────────────

@pytest.mark.parametrize("bad", JUNK)
def test_a_nonsense_delay_does_not_kill_the_run(worker, bad):
    worker.do_action({"kind": "click", "data": {"x": 10, "y": 10},
                      "delay_ms": bad}, {})
    assert worker._mouse.events, f"delay_ms={bad!r} stopped the click happening"


def test_an_infinite_delay_does_not_wait_forever(worker):
    """⚠ `float("inf")` does not raise, which is what makes it the dangerous
    one: it sails through to `sleep(inf)`, whose deadline is never reached, and
    the flow stops dead with no error and nothing to see."""
    slept = []
    worker.sleep = lambda s: slept.append(s)
    worker.do_action({"kind": "click", "data": {"x": 10, "y": 10},
                      "delay_ms": float("inf")}, {})
    assert all(math.isfinite(s) for s in slept), f"asked to sleep {slept}"


def test_a_real_delay_still_works(worker):
    """The guard must not swallow the ordinary case — a delay is the single
    most common field in a recorded flow."""
    slept = []
    worker.sleep = lambda s: slept.append(s)
    worker.do_action({"kind": "click", "data": {"x": 1, "y": 1},
                      "delay_ms": 250}, {})
    assert any(abs(s - 0.25) < 0.02 for s in slept), slept


def test_a_delay_written_as_a_string_is_honoured(worker):
    """"250" is what a hand-edited file or a naive generator produces, and it
    is unambiguous. Tolerance means reading it, not discarding it."""
    slept = []
    worker.sleep = lambda s: slept.append(s)
    worker.do_action({"kind": "click", "data": {"x": 1, "y": 1},
                      "delay_ms": "250"}, {})
    assert any(abs(s - 0.25) < 0.02 for s in slept), slept


# ── the modifiers a pointer step is held under ───────────────────────────────

@pytest.mark.parametrize("bad", [1, 1.5, True, None, 0])
def test_nonsense_modifiers_read_as_none(bad):
    assert flow.pointer_mods({"mods": bad}) == []


def test_a_single_modifier_as_a_bare_string_still_works():
    """The tolerance that already existed and must survive the fix."""
    assert flow.pointer_mods({"mods": "ctrl"}) == ["ctrl"]
    assert flow.pointer_mods({"mods": [" CTRL ", "shift"]}) == ["ctrl", "shift"]


def test_absent_modifiers_are_no_modifiers():
    assert flow.pointer_mods({}) == []
    assert flow.pointer_mods(None) == []


@pytest.mark.parametrize("bad", [1, True, 1.5])
def test_a_step_with_nonsense_modifiers_still_runs(worker, bad):
    worker.do_action({"kind": "click", "data": {"x": 5, "y": 5, "mods": bad},
                      "delay_ms": 0}, {})
    assert worker._mouse.events, f"mods={bad!r} took the step out"
    assert worker._kb.events == [], "a junk modifier was pressed as a key"


# ── the sweep that found them ────────────────────────────────────────────────

def test_no_step_data_takes_the_run_down(worker):
    """⚠ The general claim, kept small enough to live in the suite. Every field
    crossed with every junk value, on every kind the recorder can produce.

    `autoclick` is excluded deliberately: with no click limit it runs until
    something stops it, which is what it is for, and this harness never clears
    `_running`.
    """
    import itertools
    import random

    rng = random.Random(20260908)
    kinds = ["click", "move", "drag", "scroll", "key", "combo", "text", "??"]
    fields = ["x", "y", "to_x", "to_y", "button", "clicks", "hold", "hold_ms",
              "mods", "duration_ms", "direction", "amount", "speed_nps",
              "at_cursor", "keys", "mode", "repeat", "text", "speed_cps",
              "send_as"]
    raised = []
    for _ in range(1500):
        data = {f: rng.choice(JUNK) for f in rng.sample(fields, rng.randint(0, 6))}
        step = {"kind": rng.choice(kinds), "data": data,
                "delay_ms": rng.choice(JUNK)}
        worker._running = True
        try:
            worker.do_action(step, {})
        except Exception as exc:
            raised.append((type(exc).__name__, str(exc)[:60], step))
    assert not raised, (
        f"{len(raised)} of 1500 malformed steps raised; first: {raised[0]}")
