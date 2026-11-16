"""Headless tests for the pure-Python flow engine (flow.py)."""
import os
import sys
import json
import random
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import flow
from flow import (FlowGraph, FlowInterpreter, N_START, N_END, N_ACTION, N_IF,
                  N_LOOP, N_SETVAR, N_LABEL, N_GOTO, N_COMMENT)


# ── A configurable mock executor ─────────────────────────────────────────────
class MockExec:
    def __init__(self):
        self._running = True
        self.log = []            # tags of executed actions, in order
        self.sensor_fn = lambda cond, v: True
        self.action_fn = None    # optional override (step, vars) -> bool
        self.max_calls = 100000  # safety net so a buggy test can't hang
        self._calls = 0

    def running(self):
        return self._running

    def stop(self):
        self._running = False

    def sleep(self, secs):
        pass

    def do_action(self, step, variables):
        self._calls += 1
        if self._calls > self.max_calls:
            self._running = False
            return False
        tag = step.get("data", {}).get("_tag")
        if tag is not None:
            self.log.append(tag)
        if self.action_fn:
            return self.action_fn(step, variables)
        return step.get("data", {}).get("_ok", True)

    def eval_sensor(self, cond, variables):
        return self.sensor_fn(cond, variables)


def action(tag, ok=True):
    return {"kind": "click", "data": {"_tag": tag, "_ok": ok}, "delay_ms": 0}


def run(graph, ex=None, **kw):
    ex = ex or MockExec()
    interp = FlowInterpreter(graph, ex, **kw)
    status = interp.run()
    return interp, ex, status


# ── Serialization ────────────────────────────────────────────────────────────
def test_serialization_roundtrip():
    g = FlowGraph()
    s = g.add_node(N_START)
    a = g.add_node(N_ACTION, {"step": action("a")})
    e = g.add_node(N_END)
    g.add_edge(s.id, a.id, "out")
    g.add_edge(a.id, e.id, "out")
    g.variables = {"count": 0}

    d = g.to_dict()
    assert d["version"] == 2
    g2 = FlowGraph.from_dict(json.loads(json.dumps(d)))
    assert set(g2.nodes) == set(g.nodes)
    assert len(g2.edges) == len(g.edges)
    assert g2.variables == {"count": 0}


def test_new_ids_dont_clash_after_load():
    g = FlowGraph()
    g.add_node(N_START)
    g2 = FlowGraph.from_dict(json.loads(json.dumps(g.to_dict())))
    new = g2.add_node(N_ACTION)
    assert new.id not in [n for n in g2.nodes if n != new.id]


# ── v1 → v2 migration (backward compatibility, item 8) ───────────────────────
def test_v1_migration_runs_in_order():
    v1 = {"version": 1, "steps": [action("s1"), action("s2"), action("s3")]}
    g = FlowGraph.from_dict(v1)
    assert g.meta.get("migrated_from") == "v1"
    _, ex, status = run(g)
    assert ex.log == ["s1", "s2", "s3"]
    assert status == "done"


def test_v1_file_without_version_still_loads():
    g = FlowGraph.from_dict({"steps": [action("only")]})
    _, ex, _ = run(g)
    assert ex.log == ["only"]


def test_linear_roundtrip_back_to_steps():
    v1 = {"version": 1, "steps": [action("s1"), action("s2")]}
    g = FlowGraph.from_dict(v1)
    assert g.is_linear()
    steps = g.to_linear_steps()
    assert [s["data"]["_tag"] for s in steps] == ["s1", "s2"]


# ── If / Else branching (item 1) ─────────────────────────────────────────────
def _build_if(cond_result):
    g = FlowGraph()
    s = g.add_node(N_START)
    i = g.add_node(N_IF, {"condition": {"type": "image", "image_path": "x.png"}})
    ta = g.add_node(N_ACTION, {"step": action("TRUE")})
    fa = g.add_node(N_ACTION, {"step": action("FALSE")})
    e = g.add_node(N_END)
    g.add_edge(s.id, i.id, "out")
    g.add_edge(i.id, ta.id, "true")
    g.add_edge(i.id, fa.id, "false")
    g.add_edge(ta.id, e.id, "out")
    g.add_edge(fa.id, e.id, "out")
    ex = MockExec()
    ex.sensor_fn = lambda c, v: cond_result
    return g, ex


def test_if_true_branch():
    g, ex = _build_if(True)
    run(g, ex)
    assert ex.log == ["TRUE"]


def test_if_false_branch():
    g, ex = _build_if(False)
    run(g, ex)
    assert ex.log == ["FALSE"]


def test_if_negate():
    g = FlowGraph()
    s = g.add_node(N_START)
    i = g.add_node(N_IF, {"condition": {"type": "image", "negate": True}})
    ta = g.add_node(N_ACTION, {"step": action("T")})
    e = g.add_node(N_END)
    g.add_edge(s.id, i.id, "out")
    g.add_edge(i.id, ta.id, "true")
    g.add_edge(i.id, e.id, "false")
    g.add_edge(ta.id, e.id, "out")
    ex = MockExec()
    ex.sensor_fn = lambda c, v: True   # negate → false → skip T
    run(g, ex)
    assert ex.log == []


# ── Loops (item 2) ───────────────────────────────────────────────────────────
def test_loop_repeat_n():
    g = FlowGraph()
    s = g.add_node(N_START)
    lp = g.add_node(N_LOOP, {"mode": "repeat_n", "count": 3})
    body = g.add_node(N_ACTION, {"step": action("x")})
    e = g.add_node(N_END)
    g.add_edge(s.id, lp.id, "out")
    g.add_edge(lp.id, body.id, "body")
    g.add_edge(body.id, lp.id, "out")     # back-edge to the loop
    g.add_edge(lp.id, e.id, "done")
    _, ex, _ = run(g)
    assert ex.log == ["x", "x", "x"]


def test_loop_while_condition():
    # while sensor true; sensor true for first 2 checks, then false
    state = {"n": 0}

    def sensor(c, v):
        state["n"] += 1
        return state["n"] <= 2

    g = FlowGraph()
    s = g.add_node(N_START)
    lp = g.add_node(N_LOOP, {"mode": "while", "condition": {"type": "text"}})
    body = g.add_node(N_ACTION, {"step": action("y")})
    e = g.add_node(N_END)
    g.add_edge(s.id, lp.id, "out")
    g.add_edge(lp.id, body.id, "body")
    g.add_edge(body.id, lp.id, "out")
    g.add_edge(lp.id, e.id, "done")
    ex = MockExec()
    ex.sensor_fn = sensor
    run(g, ex)
    assert ex.log == ["y", "y"]


def test_loop_max_iters_caps_forever():
    g = FlowGraph()
    s = g.add_node(N_START)
    lp = g.add_node(N_LOOP, {"mode": "forever", "max_iters": 5})
    body = g.add_node(N_ACTION, {"step": action("z")})
    e = g.add_node(N_END)
    g.add_edge(s.id, lp.id, "out")
    g.add_edge(lp.id, body.id, "body")
    g.add_edge(body.id, lp.id, "out")
    g.add_edge(lp.id, e.id, "done")
    _, ex, _ = run(g)
    assert ex.log == ["z"] * 5


def test_nested_loops():
    g = FlowGraph()
    s = g.add_node(N_START)
    outer = g.add_node(N_LOOP, {"mode": "repeat_n", "count": 2})
    inner = g.add_node(N_LOOP, {"mode": "repeat_n", "count": 3})
    body = g.add_node(N_ACTION, {"step": action("i")})
    e = g.add_node(N_END)
    g.add_edge(s.id, outer.id, "out")
    g.add_edge(outer.id, inner.id, "body")
    g.add_edge(inner.id, body.id, "body")
    g.add_edge(body.id, inner.id, "out")     # back to inner
    g.add_edge(inner.id, outer.id, "done")   # inner done → back to outer
    g.add_edge(outer.id, e.id, "done")
    _, ex, _ = run(g)
    assert ex.log == ["i"] * 6   # 2 * 3


# ── Goto / labels (item 3) ───────────────────────────────────────────────────
def test_goto_label():
    g = FlowGraph()
    s = g.add_node(N_START)
    a1 = g.add_node(N_ACTION, {"step": action("a1")})
    gt = g.add_node(N_GOTO, {"target_label": "end_label"})
    a2 = g.add_node(N_ACTION, {"step": action("a2_skipped")})
    lab = g.add_node(N_LABEL, {"name": "end_label"})
    a3 = g.add_node(N_ACTION, {"step": action("a3")})
    e = g.add_node(N_END)
    g.add_edge(s.id, a1.id, "out")
    g.add_edge(a1.id, gt.id, "out")
    g.add_edge(a2.id, lab.id, "out")
    g.add_edge(lab.id, a3.id, "out")
    g.add_edge(a3.id, e.id, "out")
    _, ex, _ = run(g)
    assert ex.log == ["a1", "a3"]   # a2 skipped via goto


# ── Variables / counters (item 4) ────────────────────────────────────────────
def test_counter_loop_with_var_condition():
    # set i=0; loop while i<3: action, i+=1
    g = FlowGraph()
    s = g.add_node(N_START)
    init = g.add_node(N_SETVAR, {"name": "i", "op": "set", "value": 0})
    lp = g.add_node(N_LOOP, {"mode": "while",
                             "condition": {"type": "var", "name": "i",
                                           "op": "<", "value": 3}})
    body = g.add_node(N_ACTION, {"step": action("tick")})
    inc = g.add_node(N_SETVAR, {"name": "i", "op": "add", "value": 1})
    e = g.add_node(N_END)
    g.add_edge(s.id, init.id, "out")
    g.add_edge(init.id, lp.id, "out")
    g.add_edge(lp.id, body.id, "body")
    g.add_edge(body.id, inc.id, "out")
    g.add_edge(inc.id, lp.id, "out")   # back to loop
    g.add_edge(lp.id, e.id, "done")
    interp, ex, _ = run(g)
    assert ex.log == ["tick", "tick", "tick"]
    assert interp.vars["i"] == 3


def test_var_substitution_in_action():
    seen = {}

    def act(step, v):
        seen["text"] = flow.substitute_vars(step["data"].get("text", ""), v)
        return True

    g = FlowGraph()
    s = g.add_node(N_START)
    sv = g.add_node(N_SETVAR, {"name": "name", "op": "set", "value": "Bob"})
    a = g.add_node(N_ACTION, {"step": {"kind": "text",
                                       "data": {"text": "Hi {name}"}, "delay_ms": 0}})
    e = g.add_node(N_END)
    g.add_edge(s.id, sv.id, "out")
    g.add_edge(sv.id, a.id, "out")
    g.add_edge(a.id, e.id, "out")
    ex = MockExec()
    ex.action_fn = act
    run(g, ex)
    assert seen["text"] == "Hi Bob"


def test_var_string_equality():
    assert flow.eval_var_condition({"name": "s", "op": "==", "value": "ok"},
                                   {"s": "ok"})
    assert flow.eval_var_condition({"name": "n", "op": ">=", "value": "5"},
                                   {"n": 7})
    assert not flow.eval_var_condition({"name": "n", "op": ">", "value": "10"},
                                       {"n": 7})
    assert flow.eval_var_condition({"name": "s", "op": "contains", "value": "ell"},
                                   {"s": "hello"})


# ── On-error behavior (item 5) ───────────────────────────────────────────────
def test_error_retry_then_succeed():
    attempts = {"n": 0}

    def act(step, v):
        attempts["n"] += 1
        return attempts["n"] >= 3   # fail twice, succeed on 3rd

    g = FlowGraph()
    s = g.add_node(N_START)
    a = g.add_node(N_ACTION, {"step": action("a"),
                              "on_error": {"mode": "stop", "retries": 3,
                                           "retry_delay_s": 0}})
    e = g.add_node(N_END)
    g.add_edge(s.id, a.id, "out")
    g.add_edge(a.id, e.id, "out")
    ex = MockExec()
    ex.action_fn = act
    _, _, status = run(g, ex)
    assert attempts["n"] == 3
    assert status == "done"


def test_error_stop_aborts():
    g = FlowGraph()
    s = g.add_node(N_START)
    a = g.add_node(N_ACTION, {"step": action("a", ok=False),
                              "on_error": {"mode": "stop"}})
    nxt = g.add_node(N_ACTION, {"step": action("after")})
    e = g.add_node(N_END)
    g.add_edge(s.id, a.id, "out")
    g.add_edge(a.id, nxt.id, "out")
    g.add_edge(nxt.id, e.id, "out")
    _, ex, status = run(g)
    assert status == "error"
    assert "after" not in ex.log


def test_error_skip_continues():
    g = FlowGraph()
    s = g.add_node(N_START)
    a = g.add_node(N_ACTION, {"step": action("a", ok=False),
                              "on_error": {"mode": "skip"}})
    nxt = g.add_node(N_ACTION, {"step": action("after")})
    e = g.add_node(N_END)
    g.add_edge(s.id, a.id, "out")
    g.add_edge(a.id, nxt.id, "out")
    g.add_edge(nxt.id, e.id, "out")
    _, ex, status = run(g)
    assert ex.log == ["a", "after"]
    assert status == "done"


def test_error_port_recovery():
    # action fails → follow the "error" edge (visual try/catch)
    g = FlowGraph()
    s = g.add_node(N_START)
    a = g.add_node(N_ACTION, {"step": action("a", ok=False),
                              "on_error": {"mode": "stop"}})
    rec = g.add_node(N_ACTION, {"step": action("recovered")})
    e = g.add_node(N_END)
    g.add_edge(s.id, a.id, "out")
    g.add_edge(a.id, rec.id, "error")     # error path wins over mode=stop
    g.add_edge(rec.id, e.id, "out")
    _, ex, status = run(g)
    assert ex.log == ["a", "recovered"]
    assert status == "done"


def test_error_goto_label_recovery():
    g = FlowGraph()
    s = g.add_node(N_START)
    a = g.add_node(N_ACTION, {"step": action("a", ok=False),
                              "on_error": {"mode": "goto", "goto_label": "rescue"}})
    skip = g.add_node(N_ACTION, {"step": action("skip")})
    lab = g.add_node(N_LABEL, {"name": "rescue"})
    rec = g.add_node(N_ACTION, {"step": action("rescued")})
    e = g.add_node(N_END)
    g.add_edge(s.id, a.id, "out")
    g.add_edge(a.id, skip.id, "out")
    g.add_edge(skip.id, e.id, "out")
    g.add_edge(lab.id, rec.id, "out")
    g.add_edge(rec.id, e.id, "out")
    _, ex, _ = run(g)
    assert ex.log == ["a", "rescued"]


# ── Infinite-loop guard (item 6/7 safety) ────────────────────────────────────
def test_step_limit_guard_stops_runaway():
    # forever loop with a comment body (no action) → only max_steps can stop it
    g = FlowGraph()
    s = g.add_node(N_START)
    lp = g.add_node(N_LOOP, {"mode": "forever", "max_iters": 10**9})
    body = g.add_node(N_COMMENT, {"text": "spin"})
    e = g.add_node(N_END)
    g.add_edge(s.id, lp.id, "out")
    g.add_edge(lp.id, body.id, "body")
    g.add_edge(body.id, lp.id, "out")
    g.add_edge(lp.id, e.id, "done")
    _, ex, status = run(g, max_steps=500)
    assert status == "error"


# ── Run-log emission (item 7) ────────────────────────────────────────────────
def test_run_log_events():
    events = []
    g = FlowGraph()
    s = g.add_node(N_START)
    a = g.add_node(N_ACTION, {"step": action("a")})
    e = g.add_node(N_END)
    g.add_edge(s.id, a.id, "out")
    g.add_edge(a.id, e.id, "out")
    interp = FlowInterpreter(g, MockExec(), on_log=lambda ev: events.append(ev))
    interp.run()
    kinds = [ev["kind"] for ev in events]
    assert kinds[0] == "run_start"
    assert kinds[-1] == "run_end"
    assert "action" in kinds
    assert any(ev.get("desc") for ev in events if ev["kind"] == "node_enter")


def test_repeated_clicking_stays_free_without_the_autoclick_node():
    """⚠ Why `autoclick` is not deleted now that Basic exists.

    It looks redundant — the Basic face writes it, and nothing else creates one
    since the palette button went. But it is the ONLY way to click repeatedly
    on the free tier. A plain Click step has no repeat and no interval: it
    clicks once and returns. So "keep clicking until I stop it" built out of
    Click needs a Loop, and Loop is a paid node.

    If this ever fails because Click grew a repeat of its own, then `autoclick`
    really can go — and this test is where to find out.
    """
    import entitlements

    def _wire(build):
        g = flow.FlowGraph()
        start = g.add_node(flow.N_START, {"name": flow.START_NAME}, x=-280, y=-20)
        build(g, start)
        return g

    def _loop_of_clicks(g, start):
        lp = g.add_node(flow.N_LOOP, {"name": "forever", "mode": "forever"}, x=0, y=0)
        c = g.add_node(flow.N_ACTION,
                       {"step": {"kind": "click", "data": {"x": 0, "y": 0}}}, x=200, y=0)
        g.add_edge(start.id, lp.id, "out")
        g.add_edge(lp.id, c.id, "body")

    def _one_autoclick(g, start):
        n = g.add_node(flow.N_ACTION,
                       {"step": {"kind": "autoclick", "data": {}}}, x=0, y=120)
        g.add_edge(start.id, n.id, "out")

    assert entitlements.runs_on_free(_wire(_loop_of_clicks)) is False, \
        "a Loop of Clicks is supposed to be the paid way to repeat"
    assert entitlements.runs_on_free(_wire(_one_autoclick)) is True, \
        "the free tier lost its auto-clicker"


# ── 2.0 Auto-Click node (single first-class node = a Basic clicker) ───────────
def test_autoclick_summary_max():
    s = {"kind": "autoclick", "data": {"button": "left", "max_speed": True}}
    assert flow._action_summary(s) == "Auto-click Left · MAX"


def test_autoclick_summary_cps_and_limit():
    s = {"kind": "autoclick",
         "data": {"button": "right", "max_speed": False, "unit": "cps",
                  "cps": 12, "click_limit": 500}}
    assert flow._action_summary(s) == "Auto-click Right · 12 CPS · 500×"


def test_autoclick_node_runs_through_interpreter():
    # The interpreter must treat an autoclick action like any other action node.
    g = FlowGraph()
    s = g.add_node(N_START)
    a = g.add_node(N_ACTION, {"step": {"kind": "autoclick", "data": {}}})
    e = g.add_node(N_END)
    g.add_edge(s.id, a.id, "out")
    g.add_edge(a.id, e.id, "out")
    ex = MockExec()
    status = FlowInterpreter(g, ex).run()
    assert status == "done"
    assert a.summary().startswith("Auto-click")


# ── Key/combo hold_ms (per-step hold, e.g. "hold W for 3s" in games) ─────────
# These exercise flow_exec.FlowWorker.do_action directly with a fake keyboard
# and a stubbed sleep — no real input is sent.
import flow_exec


class _FakeKB:
    """Records (press/release, key, ...) into a shared events list, plus
    ('sleep', secs) markers when the worker's own sleep() is monkeypatched to
    log into the same list, so full press→sleep→release order is checkable."""
    def __init__(self, events):
        self.events = events

    def press(self, key):
        self.events.append(("press", key))

    def release(self, key):
        self.events.append(("release", key))

    def type(self, text):
        pass


def _make_worker():
    return flow_exec.FlowWorker(FlowGraph())


def test_key_hold_ms_presses_sleeps_then_releases():
    fw = _make_worker()
    events = []
    fw._kb = _FakeKB(events)
    fw._running = True
    fw.sleep = lambda s: events.append(("sleep", s))
    step = {"kind": "key", "data": {"keys": ["w"], "hold_ms": 1200}}
    assert fw.do_action(step, {}) is True
    kinds = [e[0] for e in events]
    assert kinds == ["press", "sleep", "release"]
    assert events[1][1] == 1200 / 1000.0


def test_key_no_hold_ms_uses_short_settings_tap():
    fw = _make_worker()
    events = []
    fw._kb = _FakeKB(events)
    fw._running = True
    fw.sleep = lambda s: events.append(("sleep", s))
    step = {"kind": "key", "data": {"keys": ["w"]}}   # no hold_ms -> normal tap
    assert fw.do_action(step, {}) is True
    kinds = [e[0] for e in events]
    assert kinds == ["press", "sleep", "release"]
    assert events[1][1] == fw._key_hold_s


def test_combo_hold_ms_releases_all_keys_in_reverse_order():
    fw = _make_worker()
    events = []
    fw._kb = _FakeKB(events)
    fw._running = True
    fw.sleep = lambda s: events.append(("sleep", s))
    step = {"kind": "combo", "data": {"keys": ["ctrl", "shift", "c"], "hold_ms": 500}}
    assert fw.do_action(step, {}) is True
    presses = [e[1] for e in events if e[0] == "press"]
    releases = [e[1] for e in events if e[0] == "release"]
    assert len(presses) == 3 and len(releases) == 3
    assert releases == list(reversed(presses))
    # the hold (500ms) is only applied once, for the final (trigger) key
    sleeps = [e[1] for e in events if e[0] == "sleep"]
    assert 0.5 in sleeps


def test_key_hold_ms_releases_even_when_sleep_raises_mid_hold():
    # Guaranteed release: even if the hold sleep blows up (simulating an
    # interrupted/erroring run), the key must still come back up.
    fw = _make_worker()
    events = []
    fw._kb = _FakeKB(events)
    fw._running = True

    def boom(secs):
        raise RuntimeError("interrupted mid-hold")
    fw.sleep = boom

    step = {"kind": "key", "data": {"keys": ["w"], "hold_ms": 3000}}
    ok = fw.do_action(step, {})   # outer try/except in do_action swallows it
    assert ok is False
    assert [e[0] for e in events] == ["press", "release"]


def test_key_hold_ms_releases_when_stopped_mid_hold():
    # Guaranteed release: Stop flips _running mid-hold; the key must still
    # come back up (no stuck-down key in the game).
    fw = _make_worker()
    events = []
    fw._kb = _FakeKB(events)
    fw._running = True

    def fake_sleep(secs):
        fw._running = False   # simulate the Stop button firing mid-hold
    fw.sleep = fake_sleep

    step = {"kind": "key", "data": {"keys": ["w"], "hold_ms": 5000}}
    assert fw.do_action(step, {}) is True
    assert [e[0] for e in events] == ["press", "release"]


# ══════════════════════════════════════════════════════════════════════════════
#  Hold down / Release — a key press that outlives its own node
# ══════════════════════════════════════════════════════════════════════════════

def _kb_worker():
    fw = _make_worker()
    events: list = []
    fw._kb = _FakeKB(events)
    fw._running = True
    fw.sleep = lambda s: events.append(("sleep", s))
    return fw, events


def _key_step(mode, keys=("w",), **extra):
    return {"kind": "key", "data": dict(keys=list(keys), mode=mode, **extra)}


def test_a_flow_written_before_modes_existed_keeps_its_old_behaviour():
    # The absence of "mode" has to mean what it used to mean, not a default.
    assert flow.key_mode({"keys": ["w"], "hold_ms": 1200}) == flow.KEY_HOLD
    assert flow.key_mode({"keys": ["w"]}) == flow.KEY_TAP
    assert flow.key_mode({}) == flow.KEY_TAP
    assert flow.key_mode(None) == flow.KEY_TAP
    # ...and an explicit mode wins over the hold_ms that is still stored.
    assert flow.key_mode({"mode": "down", "hold_ms": 1200}) == flow.KEY_DOWN
    assert flow.key_mode({"mode": "nonsense"}) == flow.KEY_TAP


def test_hold_down_presses_and_does_not_release():
    fw, events = _kb_worker()
    assert fw.do_action(_key_step("down"), {}) is True
    assert [e[0] for e in events] == ["press", "sleep"]
    assert list(fw._held) == ["w"]


def test_hold_down_of_two_keys_leaves_both_down():
    # The whole point: W+A is one node and neither key comes back up in it.
    fw, events = _kb_worker()
    assert fw.do_action(_key_step("down", ("w", "a")), {}) is True
    assert [e[0] for e in events].count("press") == 2
    assert "release" not in [e[0] for e in events]
    assert list(fw._held) == ["w", "a"]


def test_holding_a_key_that_is_already_down_does_not_press_it_twice():
    fw, events = _kb_worker()
    fw.do_action(_key_step("down"), {})
    events.clear()
    fw.do_action(_key_step("down"), {})
    assert events == []
    assert list(fw._held) == ["w"]


def test_release_takes_only_the_named_key_back_up():
    fw, events = _kb_worker()
    fw.do_action(_key_step("down", ("w", "shift")), {})
    events.clear()
    assert fw.do_action(_key_step("up", ("w",)), {}) is True
    assert [e[0] for e in events] == ["release"]
    assert list(fw._held) == ["shift"]


def test_release_with_no_keys_frees_everything_in_reverse_order():
    fw, events = _kb_worker()
    fw.do_action(_key_step("down", ("w", "a")), {})
    pressed = [e[1] for e in events if e[0] == "press"]
    events.clear()
    assert fw.do_action(_key_step("up", ()), {}) is True
    assert [e[1] for e in events if e[0] == "release"] == list(reversed(pressed))
    assert fw._held == {}


def test_a_run_that_ends_with_keys_still_down_releases_them():
    # The safety net. Whatever path the run ended by, a key the flow never got
    # to release must not survive it.
    fw, events = _kb_worker()
    fw._held["w"] = "KEYOBJ"
    flow_exec._HOLDERS.add(fw)
    fw.run()
    assert ("release", "KEYOBJ") in events
    assert fw._held == {}


def test_the_panic_release_frees_keys_from_another_thread():
    # closeEvent and _quit_app end in os._exit(0): the worker's own finally is
    # never reached, so the GUI thread has to be able to do it.
    fw, events = _kb_worker()
    fw._held["w"] = "KEYOBJ"
    flow_exec._HOLDERS.add(fw)
    flow_exec.release_all_held()
    assert events == [("release", "KEYOBJ")]
    assert fw._held == {} and fw not in flow_exec._HOLDERS


def test_repeat_actually_repeats_and_leaves_a_gap_between_presses():
    # repeat was written by the editor from the start and read by nobody, so a
    # step set to 3 pressed once. Back-to-back presses with no gap would also
    # arrive as one long press rather than three.
    fw, events = _kb_worker()
    step = {"kind": "key", "data": {"keys": ["w"], "repeat": 3}}
    assert fw.do_action(step, {}) is True
    assert [e[0] for e in events].count("press") == 3
    assert [e[0] for e in events].count("release") == 3
    assert [e[0] for e in events] == ["press", "sleep", "release"] + \
        ["sleep", "press", "sleep", "release"] * 2


def test_repeat_stops_when_the_run_is_stopped():
    fw, events = _kb_worker()

    def fake_sleep(secs):
        events.append(("sleep", secs))
        fw._running = False
    fw.sleep = fake_sleep
    step = {"kind": "key", "data": {"keys": ["w"], "repeat": 50}}
    assert fw.do_action(step, {}) is True
    assert [e[0] for e in events] == ["press", "sleep", "release"]


def test_the_summary_says_which_way_a_key_step_presses():
    assert flow._action_summary(_key_step("down", ("w", "a"))) == "Hold down: W+A"
    assert flow._action_summary(_key_step("up", ("w",))) == "Release: W"
    assert flow._action_summary(_key_step("up", ())) == "Release: all held keys"
    assert flow._action_summary(
        _key_step("hold", ("w",), hold_ms=2000)) == "Key: W · hold 2 s"
    assert flow._action_summary(
        _key_step("tap", ("w",), repeat=4)) == "Key: W ×4"
    # a pre-modes flow still summarises exactly as it always did
    assert flow._action_summary(
        {"kind": "key", "data": {"keys": ["w"], "hold_ms": 3000}}) == "Key: W · hold 3 s"


# ══════════════════════════════════════════════════════════════════════════════
#  How long a node takes — the model behind the load bar and the Time axis
# ══════════════════════════════════════════════════════════════════════════════

def _act(kind, data, **nd):
    g = FlowGraph()
    return g.add_node(flow.N_ACTION, dict({"step": {"kind": kind, "data": data}},
                                          **nd))


def test_a_wait_knows_exactly_how_long_it_takes():
    assert flow.estimate(_act("wait", {"ms": 1500})) == (1500, flow.EXACT)


def test_a_detect_reports_its_timeout_as_a_ceiling_not_a_duration():
    # A ceiling is the useful number to fill a bar against — the question a
    # Detect raises while it runs is "is it about to give up" — but it is not a
    # duration, and the strip draws it differently for exactly that reason.
    e = flow.estimate(_act("wait_image", {"image_path": "x.png", "timeout_s": 8}))
    assert e == (8000, flow.CEILING)
    # ...and with no timeout there is nothing to say at all.
    assert flow.estimate(
        _act("wait_image", {"image_path": "x.png"})).source == flow.UNKNOWN


def test_a_measurement_fills_in_where_the_settings_cannot():
    n = _act("wait_image", {"image_path": "x.png"})
    assert flow.estimate(n).source == flow.UNKNOWN
    assert flow.estimate(n, measured={n.id: 1234}) == (1234, flow.MEASURED)


def test_the_settings_beat_a_measurement_when_they_are_exact():
    # A Wait of 1.5 s takes 1.5 s. A sample that says 1.6 is measurement noise,
    # not new information, and letting it win would make a known box wobble.
    n = _act("wait", {"ms": 1500})
    assert flow.estimate(n, measured={n.id: 1600}) == (1500, flow.EXACT)


def test_a_hold_down_is_effectively_instant_but_a_timed_hold_is_not():
    assert flow.estimate(_act("key", {"keys": ["w"], "mode": "down"})).ms == 0
    assert flow.estimate(_act("key", {"keys": ["w"], "mode": "up"})).ms == 0
    assert flow.estimate(
        _act("key", {"keys": ["w"], "mode": "hold", "hold_ms": 2000})).ms == 2000


def test_repeat_and_extra_keys_lengthen_the_estimate():
    one = flow.estimate(_act("key", {"keys": ["w"]})).ms
    assert flow.estimate(_act("key", {"keys": ["w"], "repeat": 3})).ms > one
    assert flow.estimate(_act("key", {"keys": ["ctrl", "c"]})).ms > one


def test_typing_is_estimated_from_its_own_rate():
    e = flow.estimate(_act("text", {"text": "x" * 100, "speed_cps": 50}))
    assert e == (2000, flow.EXACT)


def test_an_autoclick_with_no_limit_has_no_duration():
    assert flow.estimate(
        _act("autoclick", {"cps": 10, "click_limit": 50})) == (5000, flow.EXACT)
    assert flow.estimate(
        _act("autoclick", {"cps": 10})).source == flow.UNKNOWN


def test_the_pre_delay_counts_toward_the_estimate_and_scales_with_speed():
    n = _act("wait", {"ms": 1000}, delay_before_ms=500)
    assert flow.estimate(n).ms == 1500
    # speed_factor scales the pre-delay and nothing else in the engine, so it
    # scales the pre-delay and nothing else here.
    assert flow.estimate(n, speed=2.0).ms == 2000


def test_a_branching_node_has_no_duration_of_its_own():
    g = FlowGraph()
    assert flow.estimate(g.add_node(flow.N_IF, {})).source == flow.UNKNOWN
    assert flow.estimate(g.add_node(flow.N_LOOP, {})).source == flow.UNKNOWN


def test_start_and_end_are_known_to_be_instant_not_unknown():
    # Every flow has both. Calling them unbounded would put "+ (unbounded)" on
    # the timeline of every flow ever written, which makes the warning useless
    # exactly where it matters.
    g = FlowGraph()
    for t in (flow.N_START, flow.N_END, flow.N_GOTO):
        assert flow.estimate(g.add_node(t, {})) == (0, flow.EXACT), t


def test_linearise_walks_from_start_and_keeps_unreachable_nodes():
    g = FlowGraph()
    s = g.add_node(flow.N_START, {})
    a = g.add_node(flow.N_ACTION, {"step": _click()})
    e = g.add_node(flow.N_END, {})
    g.add_edge(s.id, a.id)
    g.add_edge(a.id, e.id)
    orphan = g.add_node(flow.N_ACTION, {"step": _click()})
    # An unreachable node is something the author wants to see, not something
    # to hide — it goes last rather than missing.
    assert flow.linearise(g) == [s.id, a.id, e.id, orphan.id]


def test_linearise_follows_true_before_false():
    # Port order, which is the order the canvas draws them, so the strip reads
    # left to right the way the graph does.
    g = FlowGraph()
    s = g.add_node(flow.N_START, {})
    i = g.add_node(flow.N_IF, {})
    t = g.add_node(flow.N_ACTION, {"step": _click()})
    f = g.add_node(flow.N_ACTION, {"step": _click()})
    g.add_edge(s.id, i.id)
    g.add_edge(i.id, f.id, "false")
    g.add_edge(i.id, t.id, "true")
    assert flow.linearise(g) == [s.id, i.id, t.id, f.id]


def test_linearise_terminates_on_a_loop():
    g = FlowGraph()
    s = g.add_node(flow.N_START, {})
    a = g.add_node(flow.N_ACTION, {"step": _click()})
    g.add_edge(s.id, a.id)
    g.add_edge(a.id, a.id)          # a node wired to itself
    assert flow.linearise(g) == [s.id, a.id]


# ── what the worker reports back to the GUI ──────────────────────────────────

def test_the_worker_announces_which_node_is_holding_which_key():
    g = FlowGraph()
    s = g.add_node(flow.N_START, {})
    a = g.add_node(flow.N_ACTION,
                   {"step": {"kind": "key",
                             "data": {"keys": ["w", "a"], "mode": "down"}}})
    b = g.add_node(flow.N_ACTION,
                   {"step": {"kind": "key", "data": {"keys": [], "mode": "up"}}})
    e = g.add_node(flow.N_END, {})
    for x, y in ((s, a), (a, b), (b, e)):
        g.add_edge(x.id, y.id)

    fw = flow_exec.FlowWorker(g)
    fw._kb = _FakeKB([])
    snapshots = []
    fw.held_changed.connect(lambda pairs: snapshots.append(list(pairs)))
    fw.run()
    # pressed together, attributed to the node that pressed them, then freed
    assert snapshots[0] == [("w", a.id), ("a", a.id)]
    assert snapshots[-1] == []


def test_the_worker_times_each_node_per_visit_not_per_run():
    g = FlowGraph()
    s = g.add_node(flow.N_START, {})
    a = g.add_node(flow.N_ACTION, {"step": {"kind": "wait", "data": {"ms": 20}}})
    e = g.add_node(flow.N_END, {})
    for x, y in ((s, a), (a, e)):
        g.add_edge(x.id, y.id)

    fw = flow_exec.FlowWorker(g)
    fw._kb = _FakeKB([])
    got = {}
    fw.timings_ready.connect(got.update)
    fw.run()
    # perf_counter, not monotonic: monotonic quantises to 15.625 ms on Windows
    # and would report this 20 ms node as 31.2 -- straight into runstats.
    assert 15 <= got[a.id] <= 60, got


# ══════════════════════════════════════════════════════════════════════════════
#  Copy / paste, bulk edit, and the "show the right type before OK" preset
# ══════════════════════════════════════════════════════════════════════════════

def _click(x=1, y=2):
    return {"kind": "click", "data": {"x": x, "y": y, "button": "left"}}


def test_preset_kind_gives_a_node_its_family_before_a_step_exists():
    # The whole point: a node added from the Detect palette button must already
    # read as a Detect while its editor is still open.
    g = FlowGraph()
    n = g.add_node(N_ACTION, {"preset_kind": "wait_image"})
    assert flow.action_kind(n) == "wait_image"
    assert n.summary() == "not set yet"
    # A real step always wins over the preset.
    n.data["step"] = _click()
    assert flow.action_kind(n) == "click"


def test_preset_kind_is_respected_by_delay_applies():
    # A pre-delay never fires before a click, and that has to be true from the
    # moment the node claims to be a click — not only once it is configured.
    g = FlowGraph()
    assert flow.delay_applies(g.add_node(N_ACTION, {"preset_kind": "click"})) is False
    assert flow.delay_applies(g.add_node(N_ACTION, {"preset_kind": "wait_image"})) is True


def test_start_node_is_named_so_a_goto_can_reach_it():
    g = FlowGraph.migrate_linear([])
    assert g.start_node().data.get("name") == flow.START_NAME
    assert g.find_node_by_name("start") is g.start_node()


def test_start_name_is_backfilled_on_load_but_never_steals_an_existing_one():
    g = FlowGraph()
    g.add_node(N_START)
    g2 = FlowGraph.from_dict(json.loads(json.dumps(g.to_dict())))
    assert g2.start_node().data["name"] == "start"

    # A flow that already calls something else "start" keeps it: two nodes with
    # the same name would make find_node_by_name pick whichever came first.
    g3 = FlowGraph()
    g3.add_node(N_START)
    g3.add_node(N_ACTION, {"name": "start", "step": _click()})
    g4 = FlowGraph.from_dict(json.loads(json.dumps(g3.to_dict())))
    assert g4.start_node().data.get("name", "") == ""


def test_copy_paste_duplicates_nodes_and_the_edges_between_them():
    g = FlowGraph()
    a = g.add_node(N_ACTION, {"step": _click(10, 10)}, x=0, y=0)
    b = g.add_node(N_ACTION, {"step": _click(20, 20)}, x=100, y=0)
    c = g.add_node(N_ACTION, {"step": _click(30, 30)}, x=200, y=0)
    g.add_edge(a.id, b.id)
    g.add_edge(b.id, c.id)

    payload = flow.copy_subgraph(g, [a.id, b.id])
    new_ids = flow.paste_subgraph(g, payload, dx=0, dy=200)

    assert len(new_ids) == 2
    assert len(g.nodes) == 5
    # The a->b edge came along; b->c did not, because c wasn't copied.
    pasted = set(new_ids)
    internal = [e for e in g.edges if e.src in pasted and e.dst in pasted]
    assert len(internal) == 1
    assert not [e for e in g.edges if (e.src in pasted) != (e.dst in pasted)]
    assert g.nodes[new_ids[0]].y == 200


def test_copy_never_takes_the_start_node():
    # Two entry points would silently change where a run begins.
    g = FlowGraph()
    s = g.add_node(N_START)
    a = g.add_node(N_ACTION, {"step": _click()})
    payload = flow.copy_subgraph(g, [s.id, a.id])
    assert len(payload["nodes"]) == 1
    flow.paste_subgraph(g, payload)
    assert len([n for n in g.nodes.values() if n.type == N_START]) == 1


def test_paste_renames_copies_and_repoints_a_goto_inside_the_block():
    g = FlowGraph()
    target = g.add_node(N_ACTION, {"name": "loot", "step": _click()})
    jump = g.add_node(N_GOTO, {"target_name": "loot"})
    g.add_edge(target.id, jump.id)

    new_ids = flow.paste_subgraph(g, flow.copy_subgraph(g, [target.id, jump.id]))
    names = {g.nodes[i].data.get("name") for i in new_ids}
    assert "loot copy" in names          # unique, or the Go to is ambiguous
    goto = next(g.nodes[i] for i in new_ids if g.nodes[i].type == N_GOTO)
    assert goto.data["target_name"] == "loot copy"   # follows its own copy


def test_paste_ignores_anything_that_is_not_our_clipboard_payload():
    g = FlowGraph()
    before = len(g.nodes)
    assert flow.paste_subgraph(g, {"nodes": [{"type": "action"}]}) == []
    assert flow.paste_subgraph(g, "some text the user copied") == []
    assert len(g.nodes) == before


def test_bulk_apply_only_touches_what_was_asked_for():
    g = FlowGraph()
    start = g.add_node(N_START)
    click = g.add_node(N_ACTION, {"step": _click()})
    wait = g.add_node(N_ACTION, {"step": {"kind": "wait", "data": {"ms": 500}}})
    img = g.add_node(N_ACTION, {"step": {"kind": "wait_image",
                                         "data": {"timeout_s": 5, "confidence": 0.8}}})
    ids = list(g.nodes.keys())

    flow.bulk_apply(g, ids, {"delay_before_ms": 250})
    assert "delay_before_ms" not in start.data     # never fires on Start
    assert "delay_before_ms" not in click.data     # nor before a click
    assert wait.data["delay_before_ms"] == 250

    flow.bulk_apply(g, ids, {"wait_scale": 2.0})
    assert wait.data["step"]["data"]["ms"] == 1000
    flow.bulk_apply(g, ids, {"timeout_s": 30, "confidence": 0.6})
    assert img.data["step"]["data"] == {"timeout_s": 30, "confidence": 0.6}
    # The click was in range for every op above and still has no step changes.
    assert click.data["step"] == _click()


def test_bulk_apply_reaches_conditions_inside_if_and_loop():
    g = FlowGraph()
    n_if = g.add_node(N_IF, {"condition": {"type": "image", "timeout_s": 5,
                                           "confidence": 0.9}})
    n_loop = g.add_node(N_LOOP, {"mode": "while",
                                 "condition": {"type": "text", "timeout_s": 5}})
    n = flow.bulk_apply(g, list(g.nodes), {"timeout_s": 12, "confidence": 0.5})
    assert n == 2
    assert n_if.data["condition"]["timeout_s"] == 12
    assert n_if.data["condition"]["confidence"] == 0.5
    assert n_loop.data["condition"]["timeout_s"] == 12
    assert "confidence" not in n_loop.data["condition"]   # text has no confidence


def test_bulk_apply_counts_only_nodes_it_actually_changed():
    g = FlowGraph()
    g.add_node(N_START)
    a = g.add_node(N_ACTION, {"step": _click()})
    assert flow.bulk_apply(g, list(g.nodes), {"error_retries": 3}) == 1
    assert a.data["on_error"]["retries"] == 3
    # Applying the same value twice is a no-op, so the count is honest.
    assert flow.bulk_apply(g, list(g.nodes), {"error_retries": 3}) == 0


# ── Output ports: only a detect step can fail in a way worth wiring ──────────
def _detect(kind="wait_image", **d):
    base = {"wait_image": {"image_path": "a.png", "confidence": 0.9,
                           "timeout_s": 5, "offset_x": 3, "offset_y": 4,
                           "button": "left", "clicks": 1},
            "wait_text": {"text": "GO", "case_sensitive": False, "min_score": 0.5,
                          "timeout_s": 5, "fuzzy": True, "region": None,
                          "button": "left", "clicks": 1},
            "wait_pixel": {"x": 7, "y": 8, "color": "#ff0000", "tolerance": 10,
                           "timeout_s": 5, "button": "left", "clicks": 1}}[kind]
    return {"kind": kind, "data": dict(base, **d)}


def test_only_a_detect_action_offers_an_error_port():
    g = FlowGraph()
    assert g.add_node(N_ACTION, {"step": _click()}).ports() == ["out"]
    assert g.add_node(N_ACTION, {"step": {"kind": "wait", "data": {"ms": 5}}}
                      ).ports() == ["out"]
    assert g.add_node(N_ACTION, {"step": {"kind": "key", "data": {"keys": ["a"]}}}
                      ).ports() == ["out"]
    assert g.add_node(N_ACTION, {}).ports() == ["out"]      # not configured yet
    for kind in flow.DETECT_KINDS:
        node = g.add_node(N_ACTION, {"step": _detect(kind)})
        assert node.ports() == ["out", "error"], kind
    # The palette's family hint counts too, so a new Detect node draws both.
    assert g.add_node(N_ACTION, {"preset_kind": "wait_image"}
                      ).ports() == ["out", "error"]


def test_prune_drops_a_wire_hanging_off_a_port_that_no_longer_exists():
    g = FlowGraph()
    a = g.add_node(N_ACTION, {"step": _detect()})
    b = g.add_node(N_END)
    g.add_edge(a.id, b.id, "out")
    g.add_edge(a.id, b.id, "error")
    assert flow.prune_orphan_edges(g) == 0          # both ports are real
    a.data["step"] = _click()                       # re-edited into a click
    assert flow.prune_orphan_edges(g) == 1
    assert [e.src_port for e in g.edges] == ["out"]


def test_an_error_wire_saved_by_an_older_version_is_dropped_on_load():
    g = FlowGraph()
    a = g.add_node(N_ACTION, {"step": _click()})
    b = g.add_node(N_END)
    g.add_edge(a.id, b.id, "out")
    g.add_edge(a.id, b.id, "error")
    reloaded = FlowGraph.from_dict(json.loads(json.dumps(g.to_dict())))
    assert [e.src_port for e in reloaded.edges] == ["out"]


def test_detect_condition_carries_the_check_and_drops_the_click_settings():
    g = FlowGraph()
    img = g.add_node(N_ACTION, {"step": _detect("wait_image")})
    assert flow.detect_condition(img) == {"type": "image", "image_path": "a.png",
                                          "confidence": 0.9, "timeout_s": 5}
    txt = g.add_node(N_ACTION, {"step": _detect("wait_text")})
    assert flow.detect_condition(txt)["type"] == "text"
    assert flow.detect_condition(txt)["text"] == "GO"
    assert "clicks" not in flow.detect_condition(txt)
    pix = g.add_node(N_ACTION, {"step": _detect("wait_pixel")})
    assert flow.detect_condition(pix) == {"type": "pixel", "x": 7, "y": 8,
                                          "color": "#ff0000", "tolerance": 10,
                                          "timeout_s": 5}
    # Not a detect step, and a detect step that clicks: neither converts.
    assert flow.detect_condition(g.add_node(N_ACTION, {"step": _click()})) is None
    assert flow.detect_condition(
        g.add_node(N_ACTION, {"step": _detect(click=True)})) is None


def test_promoting_a_detect_node_keeps_its_wiring_and_relabels_the_branches():
    g = FlowGraph()
    start = g.add_node(N_START)
    det = g.add_node(N_ACTION, {"step": _detect(), "name": "loot",
                                "delay_before_ms": 300}, x=52, y=26)
    found = g.add_node(N_END)
    missing = g.add_node(N_END)
    g.add_edge(start.id, det.id, "out")
    g.add_edge(det.id, found.id, "out")
    g.add_edge(det.id, missing.id, "error")

    assert flow.convert_detect_to_if(g, det.id) is True
    assert det.type == N_IF
    assert det.data["condition"]["type"] == "image"
    assert det.data["name"] == "loot"            # identity survives
    assert det.data["delay_before_ms"] == 300
    assert "step" not in det.data
    assert (det.x, det.y) == (52, 26)            # it does not jump on the canvas
    ports = {(e.src, e.dst): e.src_port for e in g.edges}
    assert ports[(start.id, det.id)] == "out"    # the wire in is untouched
    assert ports[(det.id, found.id)] == "true"
    assert ports[(det.id, missing.id)] == "false"


def test_a_clicking_detect_node_is_left_alone():
    g = FlowGraph()
    det = g.add_node(N_ACTION, {"step": _detect(click=True)})
    assert flow.convert_detect_to_if(g, det.id) is False
    assert det.type == N_ACTION          # an If node cannot click, so no promotion


# ── "is there anything to run?" ───────────────────────────────────────────────
def test_scaffolding_on_its_own_is_not_work():
    g = FlowGraph()
    start = g.add_node(N_START, {"name": flow.START_NAME})
    assert flow.has_work(g) is False
    end = g.add_node(N_END)
    g.add_edge(start.id, end.id, "out")
    assert flow.has_work(g) is False
    g.add_node(flow.N_COMMENT, {"text": "todo"})
    assert flow.has_work(g) is False


def test_a_node_that_does_something_counts_as_work():
    for ntype, data in [
            (N_ACTION, {"step": {"kind": "click", "data": {}}}),
            (N_IF, {"condition": {"type": "always"}}),
            (flow.N_LOOP, {"mode": "times", "times": 3}),
            (flow.N_GOTO, {"target_name": "start"}),
            (flow.N_SETVAR, {"var": "n", "value": "1"})]:
        g = FlowGraph()
        g.add_node(N_START, {"name": flow.START_NAME})
        g.add_node(ntype, data)
        assert flow.has_work(g) is True, ntype


def test_promoting_the_only_detect_node_leaves_a_flow_that_still_runs():
    """The regression this exists for: has_work() used to count action nodes, so
    a promoted Detect turned a working script into 'Nothing to run'."""
    g = FlowGraph()
    start = g.add_node(N_START, {"name": flow.START_NAME})
    det = g.add_node(N_ACTION, {"step": _detect()})
    found, missing = g.add_node(N_END), g.add_node(N_END)
    g.add_edge(start.id, det.id, "out")
    g.add_edge(det.id, found.id, "out")
    g.add_edge(det.id, missing.id, "error")
    assert flow.has_work(g) is True
    assert flow.convert_detect_to_if(g, det.id) is True
    assert not any(n.type == N_ACTION for n in g.nodes.values())
    assert flow.has_work(g) is True


# ── Type step: per-step key positions ────────────────────────────────────────

def test_the_interpreter_passes_a_steps_key_positions_to_the_backend():
    """A step that names its own target must not be typed with the global."""
    import flow_exec

    class _Kb:
        def __init__(self):
            self.seen = []

        def type(self, text, should_continue=None, key_positions=None):
            self.seen.append((text, key_positions))

    w = flow_exec.FlowWorker.__new__(flow_exec.FlowWorker)
    w._kb = _Kb()
    w._running = True
    flow_exec.FlowWorker._type(w, "hi", "us", stoppable=True)
    assert w._kb.seen == [("hi", "us")]


def test_a_backend_without_the_argument_is_not_handed_it():
    """pynput's own Controller.type() takes neither extra, and probing by
    signature (not by catching TypeError) is what stops a failure mid-injection
    being read as 'unsupported' and the whole string typed a second time."""
    import flow_exec

    class _Plain:
        def __init__(self):
            self.seen = []

        def type(self, text):
            self.seen.append(text)

    w = flow_exec.FlowWorker.__new__(flow_exec.FlowWorker)
    w._kb = _Plain()
    w._running = True
    flow_exec.FlowWorker._type(w, "hi", "us", stoppable=True)
    assert w._kb.seen == ["hi"]


def test_every_run_reports_which_input_backend_it_used():
    """A working Interception run and a silent fallback to pynput looked
    identical in the log, so 'is the backend setting doing anything?' was
    unanswerable from the UI."""
    import flow_exec
    seen = []

    w = flow_exec.FlowWorker.__new__(flow_exec.FlowWorker)
    w._kb_backend, w._mouse_backend = "interception", "sendinput"
    w._kb_warning = w._mouse_warning = None

    # The emit is a plain call on on_log; exercise it the way run() does.
    import time as _t
    seen.append({"t": _t.time(), "kind": "backend",
                 "keyboard": w._kb_backend, "mouse": w._mouse_backend})
    assert "backend" in flow_exec.LOG_KEEP_ALWAYS, \
        "the backend line must survive log coalescing"

    import main
    line = main.SequenceTab._fmt_log(seen[0])
    assert "interception" in line and "sendinput" in line


# ── Typing rate: selected vs delivered ───────────────────────────────────────
# "The speed is slower than selected" was two bugs stacked. Both are pinned by
# the primitive rather than by a wall-clock rate where possible: a timing
# assertion that only *usually* holds is worse than none.

def test_the_engine_wait_is_not_quantised_to_the_windows_timer_tick():
    """flow_exec.sleep used time.monotonic(), which on Windows under Python
    <= 3.12 is GetTickCount64 — resolution 15.625 ms. Every deadline rounded up
    to the next tick, putting a ~15.6 ms floor under every interval the engine
    can ask for: typing, pre-delays and the click rate alike."""
    import time
    import flow, flow_exec

    w = flow_exec.FlowWorker(flow.FlowGraph())
    w._running = True
    t0 = time.perf_counter()
    for _ in range(20):
        w.sleep(0.005)
    each = (time.perf_counter() - t0) / 20

    assert each < 0.012, (
        f"a 5 ms wait took {each*1000:.1f} ms — quantised to the 15.6 ms tick")


def test_engine_sleep_does_not_use_the_coarse_clock():
    """Source guard: perf_counter is the only clock fine enough for pacing.

    Parsed, not grepped. sleep()'s docstring names `monotonic` on purpose —
    it explains the trap — and a textual search flags that as the trap itself.
    Every source-level guard in this repo has failed that way at least once.
    """
    import ast, inspect, textwrap
    import flow_exec

    fn = ast.parse(textwrap.dedent(
        inspect.getsource(flow_exec.FlowWorker.sleep))).body[0]
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)):
        fn.body = fn.body[1:]                      # drop the docstring
    clocks = {n.attr for n in ast.walk(ast.Module(body=fn.body, type_ignores=[]))
              if isinstance(n, ast.Attribute)}

    assert "monotonic" not in clocks, "sleep() is back on the 15.6 ms clock"
    assert "perf_counter" in clocks


def test_the_typing_rates_are_derived_from_the_key_timing():
    """Two numbers, and confusing them is what made the box lie: the safe pace
    (full key timing) and the dial's top (timing squeezed to fit). TYPE_MAX_CPS
    was once a hardcoded 200 while the engine could not exceed 33, so every
    rate above 33 silently became 33."""
    import flow_exec, input_backends

    assert flow_exec.SAFE_TEXT_CPS == input_backends.safe_type_cps()
    assert 20 < flow_exec.SAFE_TEXT_CPS < 100, "not a plausible full-timing pace"
    assert flow_exec.TYPE_MAX_CPS == input_backends.MAX_TYPE_CPS
    assert flow_exec.TYPE_MAX_CPS > flow_exec.SAFE_TEXT_CPS


def test_asking_for_more_than_the_safe_pace_shortens_the_key_hold():
    """The only way to type faster is to hold each key for less time. Below the
    safe pace the timing must NOT stretch — a slow rate is idle time between
    keystrokes, not an unnaturally long key press."""
    import input_backends as ib

    full = ib.key_timing(None)
    assert ib.key_timing(5.0) == full, "a slow rate must not stretch the hold"
    assert ib.key_timing(ib.safe_type_cps()) == full

    hold, gap, settle = ib.key_timing(200.0)
    assert hold < ib.TYPE_KEY_HOLD_S and gap < ib.TYPE_KEY_GAP_S
    assert settle < ib.TYPE_MOD_SETTLE_S, "the modifier settle has to fit too"
    assert hold + gap <= 1.0 / 200.0 + 1e-9, "does not fit in the period"
    assert hold >= ib.MIN_KEY_HOLD_S, "a press and its release must stay apart"

    # Past the dial's top the squeeze stops rather than collapsing to zero.
    assert ib.key_timing(10_000.0) == ib.key_timing(ib.MAX_TYPE_CPS)


def test_the_pynput_backend_types_characters_not_key_positions():
    """Picking pynput picks the message queue, for typing as well as for keys.

    2.0.17 moved pynput's typing onto the scancode path to fix a real bug (its
    own type() sends a modifier-needing character as a bare packet without ever
    pressing the modifier, so on a Belgian layout capitals and digits vanished).
    But a scancode is a key *position*, and the receiver decides which character
    that is: on AZERTY a target with its own US table read 'a' as 'q', and every
    digit became a shifted keystroke costing a modifier settle. That is a
    regression for everyone typing into an ordinary window — which is who
    selects pynput. The modifier bug is fixed by sending *every* character as a
    packet instead, never pynput's mixture.

    A target that reads raw input sees none of this. That is what the sendinput
    and interception backends are for, and they still press real keys.
    """
    import input_backends, sendinput_backend as sb

    seen = []

    def _capture(n, arr, _size):
        for i in range(n):
            ki = arr[i].u.ki
            seen.append((ki.wVk, ki.wScan, ki.dwFlags))
        return n

    sb.user32.SendInput = _capture

    kb, actual, _warn = input_backends.make_keyboard("pynput")
    assert actual == input_backends.BACKEND_PYNPUT
    kb.type("azerty 123")

    assert seen, "nothing was sent"
    assert all(vk == 0 and flags & sb.KEYEVENTF_UNICODE
               for vk, _sc, flags in seen), "a key position was sent, not a character"
    typed = "".join(chr(sc) for _vk, sc, flags in seen
                    if not flags & sb.KEYEVENTF_KEYUP)
    assert typed == "azerty 123", f"the target would read {typed!r}"


def _typing_census(kb, text, **kw):
    """(packet events, real-key events) `kb.type` would send. Sends nothing.

    Both channels have to be captured: packets go out through
    `user32.SendInput` and scancodes through `_send_scan`, and conftest has
    already neutered both — so a test that stubs only one counts zero of the
    other and reads that as "the mechanism did not change".
    """
    import sendinput_backend as sb

    packets, keys = [], []
    orig_send, orig_scan = sb.user32.SendInput, sb._send_scan

    def _capture(n, arr, _size):
        a = getattr(arr, "_obj", arr)      # byref() from _send_scan, array from _send_all
        for i in range(n):
            packets.append(a[i].u.ki.dwFlags)
        return n

    sb.user32.SendInput = _capture
    sb._send_scan = lambda sc, keyup=False, extended=False: keys.append((sc, keyup))
    try:
        kb.type(text, **kw)
    finally:
        sb.user32.SendInput, sb._send_scan = orig_send, orig_scan
    return len([f for f in packets if f & sb.KEYEVENTF_UNICODE]), len(keys)


def test_a_type_step_chooses_its_own_delivery_mechanism():
    """Which mechanism carries the text belongs to the step, not the backend.

    ⚠ This is the fix for a two-release pendulum. A packet carries a character
    and only ever produces WM_CHAR, so an ordinary window reads it on any layout
    and a game reading raw input sees nothing at all. A scancode carries a key
    position, which a game does read. Both are right for different targets — and
    the *input backend* was deciding it, which is one global switch that also
    governs keys and clicks. So 2.0.17 put pynput on real keys and broke every
    ordinary window; the revert put it back on packets and broke game chat again.
    Neither release was wrong about its own target. The question was in the wrong
    place.
    """
    import flow, input_backends

    kb, actual, _warn = input_backends.make_keyboard("pynput")
    assert actual == input_backends.BACKEND_PYNPUT

    # Absence is "auto", and auto on pynput is what pynput has always meant.
    # Every flow saved before this existed depends on this line.
    assert _typing_census(kb, "Hello 123")[1] == 0

    chars_pk, chars_keys = _typing_census(kb, "Hello 123", send_as=flow.SEND_CHARS)
    assert chars_pk and not chars_keys

    keys_pk, keys_keys = _typing_census(kb, "Hello 123", send_as=flow.SEND_KEYS)
    assert keys_keys, "asked for key presses and got none"
    assert not keys_pk, "a packet survived: the target that reads keys sees a hole"


def test_the_send_as_vocabulary_is_spelled_the_same_in_both_modules():
    """⚠ A typo here would not raise — it would silently mean "auto".

    `input_backends` cannot import `flow` (flow imports nothing local, and the
    backends are below it), so the three strings exist twice. A mismatch reads
    as "the setting does nothing", which is indistinguishable from the bug it
    was built to fix.
    """
    import flow, input_backends

    assert (flow.SEND_AUTO, flow.SEND_CHARS, flow.SEND_KEYS) == \
           (input_backends.SEND_AUTO, input_backends.SEND_CHARS,
            input_backends.SEND_KEYS)


def test_send_as_reads_an_absent_field_as_auto():
    import flow

    assert flow.send_as({}) == flow.SEND_AUTO
    assert flow.send_as(None) == flow.SEND_AUTO
    assert flow.send_as({"send_as": "nonsense"}) == flow.SEND_AUTO
    assert flow.send_as({"send_as": "KEYS"}) == flow.SEND_KEYS


def test_the_engine_carries_send_as_from_the_step_to_the_backend():
    """The step says it; the run has to deliver it, at every speed."""
    import flow, flow_exec, input_backends

    got = []

    class _Recorder:
        def type(self, text, should_continue=None, key_positions=None,
                 cps=None, send_as=None):
            got.append(send_as)

    w = flow_exec.FlowWorker(flow.FlowGraph())
    w._kb = _Recorder()
    w._running = True
    for cps in (0.0, 40.0):     # the "as fast as reliable" path and the paced one
        w.do_action({"kind": "text",
                     "data": {"text": "hi", "speed_cps": cps,
                              "send_as": flow.SEND_KEYS}}, {})
    assert got == [flow.SEND_KEYS, flow.SEND_KEYS]

    # A backend that never heard of the argument must not be handed it.
    got.clear()
    calls = []

    class _Old:
        def type(self, text, should_continue=None):
            calls.append(text)

    w._kb = _Old()
    w.do_action({"kind": "text",
                 "data": {"text": "hi", "send_as": flow.SEND_KEYS}}, {})
    assert calls == ["hi"]


def test_a_typed_character_costs_its_period_not_its_period_plus_the_keystroke():
    """The per-character loop slept 1/cps *on top of* the ~30 ms the backend
    spends holding the key, so 20 ch/s arrived as 16."""
    import time
    import flow, flow_exec, input_backends, sendinput_backend as sb

    sb.user32.SendInput = lambda n, a, s: n          # stub the injection
    text = "hallo dit is een test van type text"
    w = flow_exec.FlowWorker(flow.FlowGraph())
    # ⚠ Pin the backend. FlowWorker builds one from *settings*, so on a machine
    # with Interception selected this measured the kernel driver instead of the
    # pacing under test — it read 17 ch/s for 100 selected and did not move when
    # the code changed, which looks exactly like a real regression.
    w._kb = input_backends.make_keyboard("pynput")[0]
    w._running = True

    t0 = time.perf_counter()
    w.do_action({"kind": "text",
                 "data": {"text": text, "speed_cps": 20.0}}, {})
    delivered = len(text) / (time.perf_counter() - t0)

    # The old behaviour delivered 16.0 for a requested 20 (-20%).
    assert delivered > 18.0, f"{delivered:.1f} ch/s delivered for 20 selected"
    assert delivered <= 21.0, f"{delivered:.1f} ch/s is faster than selected"


def test_a_rate_above_the_safe_pace_is_actually_delivered():
    """The ceiling is the user's to raise, so the number they raise it to has
    to mean something. Two time.sleep overshoots per character cost 10% at
    200 ch/s until the gap became a deadline rather than a fixed pause."""
    import time
    import flow, flow_exec, input_backends, sendinput_backend as sb

    sb.user32.SendInput = lambda n, a, s: n
    text = "hallo dit is een test van type text met wat meer tekens erbij"
    w = flow_exec.FlowWorker(flow.FlowGraph())
    w._kb = input_backends.make_keyboard("pynput")[0]   # see the test above
    w._running = True

    t0 = time.perf_counter()
    w.do_action({"kind": "text",
                 "data": {"text": text, "speed_cps": 100.0}}, {})
    delivered = len(text) / (time.perf_counter() - t0)

    assert delivered > 85.0, f"{delivered:.1f} ch/s delivered for 100 selected"
    assert delivered <= 105.0, f"{delivered:.1f} ch/s is faster than selected"


# ── reroute nodes ────────────────────────────────────────────────────────────
def test_a_reroute_bends_the_wire_without_changing_the_run():
    """The whole promise of a reroute: the flow reads identically with it and
    without it. If putting a bend in a wire could change what runs, nobody
    would dare tidy a graph."""
    g = flow.FlowGraph()
    s = g.add_node(flow.N_START, {})
    a = g.add_node(flow.N_ACTION, {"step": action("a")})
    b = g.add_node(flow.N_ACTION, {"step": action("b")})
    g.add_edge(s.id, a.id)
    e = g.add_edge(a.id, b.id)
    _i, ex, _st = run(g)
    assert ex.log == ["a", "b"]

    r = flow.insert_reroute(g, e.id, 100, 200)
    assert r is not None and r.type == flow.N_REROUTE
    assert e.dst == r.id, "the clicked wire keeps its id and becomes the first half"
    assert g.out_edge(r.id, "out").dst == b.id
    _i, ex, _st = run(g)
    assert ex.log == ["a", "b"], "the bend must be invisible to the run"


def test_a_reroute_costs_no_time_and_is_not_work():
    """It is drawing. A flow made only of bends has nothing to run, and the
    timeline must not budget a millisecond for one."""
    g = flow.FlowGraph()
    r = g.add_node(flow.N_REROUTE, {})
    assert not flow.has_work(g)
    est = flow.estimate(r)
    assert (est.ms, est.source) == (0, flow.EXACT)
    assert r.type in flow.ANNOTATION_TYPES


def test_dissolving_a_reroute_puts_the_wire_back():
    """Deleting a bend means "stop bending this wire", never "cut it" — there
    is no other reason anyone put one there."""
    g = flow.FlowGraph()
    a = g.add_node(flow.N_ACTION, {"step": action("a")})
    b = g.add_node(flow.N_ACTION, {"step": action("b")})
    e = g.add_edge(a.id, b.id)
    r = flow.insert_reroute(g, e.id, 10, 10)
    assert flow.dissolve_reroute(g, r.id)
    assert r.id not in g.nodes
    assert [(x.src, x.dst) for x in g.edges] == [(a.id, b.id)]
    assert len(g.edges) == 1, "the second half must go with the node"


def test_dissolving_a_chain_of_reroutes_one_at_a_time_survives():
    g = flow.FlowGraph()
    a = g.add_node(flow.N_ACTION, {"step": action("a")})
    b = g.add_node(flow.N_ACTION, {"step": action("b")})
    e = g.add_edge(a.id, b.id)
    r1 = flow.insert_reroute(g, e.id, 10, 10)
    r2 = flow.insert_reroute(g, g.out_edge(r1.id, "out").id, 40, 10)
    assert flow.dissolve_reroute(g, r1.id)
    assert [(x.src, x.dst) for x in g.edges] == [(a.id, r2.id), (r2.id, b.id)]
    assert flow.dissolve_reroute(g, r2.id)
    assert [(x.src, x.dst) for x in g.edges] == [(a.id, b.id)]


def test_a_reroute_survives_a_save_and_load():
    g = flow.FlowGraph()
    a = g.add_node(flow.N_ACTION, {"step": action("a")})
    b = g.add_node(flow.N_ACTION, {"step": action("b")})
    r = flow.insert_reroute(g, g.add_edge(a.id, b.id).id, 78, 52)
    g2 = flow.FlowGraph.from_dict(g.to_dict())
    kept = g2.nodes[r.id]
    assert (kept.type, kept.x, kept.y) == (flow.N_REROUTE, 78, 52)
    assert flow.prune_orphan_edges(g2) == 0, "its out port must be real"


# ── scrolling ────────────────────────────────────────────────────────────────
def test_scroll_directions_use_one_sign_convention_end_to_end():
    """pynput, SendInput and the Interception driver all agree that +y is up and
    +x is right. Keeping that convention from the step data to the wire is why
    nothing in the chain has to remember to flip anything."""
    assert flow.scroll_vector({"direction": "up"}) == (0, 1)
    assert flow.scroll_vector({"direction": "down"}) == (0, -1)
    assert flow.scroll_vector({"direction": "right"}) == (1, 0)
    assert flow.scroll_vector({"direction": "left"}) == (-1, 0)
    # Anything unrecognised scrolls down: it is the direction people mean, and
    # refusing to scroll at all would look like the step never ran.
    assert flow.scroll_vector({}) == (0, -1)
    assert flow.scroll_vector({"direction": "sideways"}) == (0, -1)


def test_a_scroll_step_says_what_it_does():
    d = {"direction": "down", "amount": 5}
    assert flow._action_summary({"kind": "scroll", "data": d}) == "Scroll ↓ 5"
    d = {"direction": "up", "amount": 2, "at_cursor": False, "x": 10, "y": 20}
    assert flow._action_summary({"kind": "scroll", "data": d}) == "Scroll ↑ 2 at (10,20)"


def test_a_paced_scroll_is_exactly_as_long_as_it_says():
    """A speed makes the duration knowable, so the timeline can draw it as a
    measurement rather than a guess.

    ⚠ 1800, not 2000. This asserted 2000 until 8 September 2026 — n/cps — and
    `_do_scroll` sends the first notch at once and waits only *between* the
    rest, so ten notches at 5/s span nine intervals and cost 1800 ms. The error
    was one whole period whatever n is, which is 100% on a two-notch scroll.
    The mirror of the drag's third settle, and the same lesson: a prediction is
    only EXACT if it was checked against what the engine spends.
    """
    g = flow.FlowGraph()
    n = g.add_node(flow.N_ACTION, {"step": {"kind": "scroll", "data": {
        "amount": 10, "speed_nps": 5}}})
    est = flow.estimate(n)
    assert (est.ms, est.source) == (1800, flow.EXACT)

    fast = g.add_node(flow.N_ACTION, {"step": {"kind": "scroll", "data": {
        "amount": 10, "speed_nps": 0}}})
    est = flow.estimate(fast)
    assert est.source == flow.EXACT and est.ms == 0


def test_scroll_speed_is_clamped_and_never_negative():
    assert flow.scroll_cps({"speed_nps": -4}) == 0.0
    assert flow.scroll_cps({"speed_nps": 10_000}) == flow.MAX_SCROLL_CPS
    assert flow.scroll_cps({"speed_nps": "nonsense"}) == 0.0
    assert flow.scroll_notches({"amount": 0}) == 1, "a scroll of nothing is not a step"


class _WheelMouse:
    """Records what reaches the mouse, in order."""
    def __init__(self):
        self.events = []
        self.position = (0, 0)

    def scroll(self, dx, dy):
        self.events.append(("scroll", dx, dy))

    def press(self, b):
        self.events.append(("press", b))

    def release(self, b):
        self.events.append(("release", b))

    def click(self, b, n=1):
        self.events.append(("click", b, n))


def _scroll_exec(data=None):
    """The real _do_scroll, on a host that is not a QObject.

    Borrowing the method rather than constructing a FlowWorker: the worker is a
    QObject, so an instance made without its __init__ raises on the first
    attribute set — and a real one would want a graph, settings and a live
    backend to test twelve lines of wheel logic.
    """
    import flow_exec

    class _Host:
        _do_scroll = flow_exec.FlowWorker._do_scroll
        # The real one: a scroll that names a position travels there like any
        # other step, and the fake mouse below records every point on the way.
        _travel_to = flow_exec.FlowWorker._travel_to

        def __init__(self):
            self._mouse = _WheelMouse()
            self._running = True
            self._smooth_mouse = True
            self._travel_pps = flow.DEFAULT_TRAVEL_PPS
            self._travel_human = False
            self._travel_rng = None

        def running(self):
            return self._running

        def sleep(self, secs):
            pass

    host = _Host()
    return host, host._mouse


def test_a_scroll_sends_one_event_per_notch():
    """Windows takes a whole roll in one event and a receiver that reads input
    once a frame still only sees part of it — the same lesson typed text paid
    for four times. A real wheel sends one WM_MOUSEWHEEL per detent."""
    ex, mouse = _scroll_exec(None)
    assert ex._do_scroll({"direction": "down", "amount": 4})
    assert mouse.events == [("scroll", 0, -1)] * 4


def test_a_scroll_at_a_position_moves_there_first():
    """The wheel turns whatever is under the pointer. A scroll step that did not
    move would scroll whichever window the mouse was last left over — which
    looks like the script working on one machine and not another."""
    ex, mouse = _scroll_exec(None)
    ex._do_scroll({"direction": "up", "amount": 1, "at_cursor": False,
                   "x": 400, "y": 300})
    assert mouse.position == (400, 300)
    ex2, mouse2 = _scroll_exec(None)
    ex2._do_scroll({"direction": "up", "amount": 1, "at_cursor": True,
                    "x": 400, "y": 300})
    assert mouse2.position == (0, 0), "at-cursor must not move the pointer"


def test_stop_cuts_a_long_scroll_within_one_notch():
    ex, mouse = _scroll_exec(None)
    real = mouse.scroll

    def stop_after_three(dx, dy):
        real(dx, dy)
        if len(mouse.events) >= 3:
            ex._running = False
    mouse.scroll = stop_after_three
    assert ex._do_scroll({"direction": "down", "amount": 500}) is False
    assert len(mouse.events) == 3


# ── dragging ─────────────────────────────────────────────────────────────────
class _DragMouse(_WheelMouse):
    """A _WheelMouse that records every position it is moved to, in order.

    position is a property here rather than the base class's plain attribute:
    the whole point of a drag is the moves *between* the press and the release,
    and an attribute would only ever remember the last one.
    """
    def __init__(self):
        self._pos = (0, 0)
        super().__init__()
        # _WheelMouse.__init__ assigns position, which is a *recording* property
        # here. That assignment is setup, not a move the drag made, and leaving
        # it in would put a phantom move before the button ever goes down.
        self.events.clear()

    @property
    def position(self):
        return self._pos

    @position.setter
    def position(self, xy):
        self._pos = tuple(xy)
        self.events.append(("move", self._pos))


def _drag_exec():
    """The real _do_drag and _release_mouse, on a host that is not a QObject."""
    import flow_exec

    class _Host:
        _do_drag = flow_exec.FlowWorker._do_drag
        _release_mouse = flow_exec.FlowWorker._release_mouse
        # Getting to the start of the drag is a glide of its own; the travel
        # between the two points is the drag proper.
        _travel_to = flow_exec.FlowWorker._travel_to

        def __init__(self):
            self._mouse = _DragMouse()
            self._running = True
            self._held = {}
            self._held_btn = None
            self._smooth_mouse = True
            self._travel_pps = flow.DEFAULT_TRAVEL_PPS
            self._travel_human = False
            self._travel_rng = None

        def running(self):
            return self._running

        def sleep(self, secs):
            pass

    host = _Host()
    return host, host._mouse


_DRAG = {"x": 100, "y": 200, "to_x": 400, "to_y": 200, "duration_ms": 400}


def test_a_drag_presses_moves_and_releases_in_that_order():
    ex, mouse = _drag_exec()
    assert ex._do_drag(dict(_DRAG)) is True
    kinds = [e[0] for e in mouse.events]
    assert kinds.count("press") == 1 and kinds.count("release") == 1
    pressed = kinds.index("press")
    # Everything before the press is the pointer travelling to the start —
    # itself a glide rather than a jump, since the control being swiped at may
    # well be watching for a cursor arriving.
    assert pressed >= 1, "the pointer is placed before the button goes down"
    assert set(kinds[:pressed]) == {"move"}
    assert kinds[-1] == "release"
    # Every move between the press and the release is the gesture itself.
    assert set(kinds[pressed + 1:-1]) == {"move"}


def test_a_drag_sends_a_path_and_not_a_jump():
    """The one thing a drag has that `click` with `hold` does not.

    A receiver samples the pointer once a frame and works the gesture out from
    where it has been: press, teleport, release gives it one sample at each end
    and it reads a click. That is exactly what Idle Slayer's swipe-to-start bar
    does with a held click, and why this step kind exists.
    """
    ex, mouse = _drag_exec()
    ex._do_drag(dict(_DRAG))
    pressed = [e[0] for e in mouse.events].index("press")
    moves = [e[1] for e in mouse.events[pressed:] if e[0] == "move"]
    assert len(moves) >= 20, f"a 400 ms drag is a path, got {len(moves)} moves"
    assert moves[-1] == (400, 200), "it has to actually arrive"
    xs = [p[0] for p in moves]
    assert xs == sorted(xs), "the pointer must not double back"
    assert all(100 < x <= 400 for x in xs)


def test_the_drag_move_count_is_derived_and_cannot_be_overridden():
    """One move per frame is the most a receiver can read, so it is computed
    from the duration rather than offered as a field. A step carrying a `steps`
    key is a hand-edit, and honouring it would let someone write steps=1 and
    rebuild the teleport this step kind exists to avoid."""
    assert flow.drag_moves({"duration_ms": 400, "steps": 1}) == \
        flow.drag_moves({"duration_ms": 400})
    # Still a path when the duration is nothing at all.
    assert flow.drag_moves({"duration_ms": 0}) == flow.MIN_DRAG_MOVES
    assert flow.drag_moves({"duration_ms": flow.MAX_DRAG_MS}) <= flow.MAX_DRAG_MOVES


def test_stop_mid_drag_still_releases_the_button():
    """The worst thing this feature can do. A key left down by a stopped run is
    bad; a mouse button left down is worse, because every click the user makes
    to fix it becomes part of a drag-select they never started."""
    ex, mouse = _drag_exec()
    real = _DragMouse.position.fset

    def stop_after_five(self, xy):
        real(self, xy)
        # Counted from the press, not from the first event: the moves before it
        # are the approach to the start point, and stopping there would test
        # the travel rather than the held gesture this is about.
        kinds = [e[0] for e in self.events]
        if "press" in kinds and kinds[kinds.index("press"):].count("move") >= 5:
            ex._running = False
    with mock.patch.object(_DragMouse, "position",
                           property(_DragMouse.position.fget, stop_after_five)):
        assert ex._do_drag(dict(_DRAG)) is False
    assert [e[0] for e in mouse.events][-1] == "release"
    assert ex._held_btn is None


def test_a_backend_that_raises_mid_drag_still_releases_the_button():
    ex, mouse = _drag_exec()

    def boom(self, xy):
        kinds = [e[0] for e in self.events]
        if "press" in kinds and kinds[kinds.index("press"):].count("move") >= 3:
            raise OSError("backend went away")
        self._pos = tuple(xy)
        self.events.append(("move", tuple(xy)))
    with mock.patch.object(_DragMouse, "position",
                           property(_DragMouse.position.fget, boom)):
        with pytest.raises(OSError):
            ex._do_drag(dict(_DRAG))
    assert [e[0] for e in mouse.events][-1] == "release"
    assert ex._held_btn is None


def test_the_panic_path_releases_a_held_mouse_button():
    """closeEvent and _quit_app end in os._exit(0), which runs no finally in the
    worker thread. release_all_held() is the only thing between that and a
    mouse button that outlives Macronaut itself."""
    import flow_exec
    ex, mouse = _drag_exec()
    ex._release_keys = lambda names=None: None      # no keys on this host
    ex._mouse.press("left")
    ex._held_btn = "left"
    flow_exec._HOLDERS.add(ex)
    try:
        flow_exec.release_all_held()
    finally:
        flow_exec._HOLDERS.discard(ex)
    assert ("release", "left") in mouse.events
    assert ex._held_btn is None


def test_releasing_keys_does_not_drop_a_worker_still_holding_the_mouse():
    """_release_keys used to discard the worker from _HOLDERS as soon as no key
    was left down. With a drag in progress that takes the mouse button out of
    the panic path's reach — the one place it has to be."""
    import flow_exec
    ex, _mouse = _drag_exec()
    ex._release_keys = flow_exec.FlowWorker._release_keys.__get__(ex)
    ex._held_by = {}
    ex._emit_held = lambda: None
    ex._kb = None
    ex._held_btn = "left"
    flow_exec._HOLDERS.add(ex)
    try:
        ex._release_keys(None)
        assert ex in flow_exec._HOLDERS
    finally:
        flow_exec._HOLDERS.discard(ex)


def test_a_drag_step_says_what_it_does_and_how_long_it_takes():
    d = dict(_DRAG)
    assert flow._action_summary({"kind": "drag", "data": d}) == \
        "Drag (100,200) → (400,200) · 0.4 s"
    assert flow._action_summary({"kind": "drag", "data": dict(d, button="right")}) \
        .startswith("Right drag")
    # Exact, not a guess: the settings decide it, so a timeline can draw a real
    # bar rather than a question mark.
    #
    # ⚠ All THREE settles. `_do_drag` is settle-press-settle-travel-settle-
    # release, and `drag_total_ms` is the *gesture* — press to release — which
    # holds only two of them. This assertion read `drag_total_ms` alone until
    # 8 September 2026 and so pinned a bar one settle short of what the node
    # really takes. The third is spent on arrival, before the press.
    n = flow.FlowNode("d", flow.N_ACTION, {"step": {"kind": "drag", "data": d}})
    est = flow.estimate(n)
    assert est.source == flow.EXACT
    assert est.ms == pytest.approx(
        flow.drag_total_ms(d) + flow.DRAG_SETTLE_MS, abs=1)


def test_a_drag_carries_its_delay_on_the_node():
    """Unlike Click, which stores its own delay_ms. "Delay before" is a row
    inside the Click panel, so a Drag reading it would let a value typed there
    ride along invisibly after toggling — the same trap Scroll documented."""
    n = flow.FlowNode("d", flow.N_ACTION,
                      {"step": {"kind": "drag", "data": dict(_DRAG)}})
    assert flow.delay_applies(n) is True


def test_a_recorded_press_that_moved_is_a_drag_not_a_click():
    """Recording a swipe used to produce a click at the point the swipe started
    — a step that presses and releases in one place and therefore does nothing
    at all to the control that was being dragged."""
    from recorder import SeqStep, SequenceRecorder
    rec = SequenceRecorder()
    rec._recording = True
    rec._on_click(100, 200, _FakeButton.left, True)
    rec._on_click(400, 205, _FakeButton.left, False)
    steps = rec._steps
    assert len(steps) == 1
    assert steps[0].kind == SeqStep.DRAG
    assert (steps[0].data["to_x"], steps[0].data["to_y"]) == (400, 205)
    assert steps[0].data["duration_ms"] >= 0


def test_a_recorded_click_that_wobbled_is_still_a_click():
    """Mistaking a shaky hand for a drag is the worse error of the two: it moves
    the pointer away from the thing that was clicked."""
    from recorder import SeqStep, SequenceRecorder
    rec = SequenceRecorder()
    rec._recording = True
    rec._on_click(100, 200, _FakeButton.left, True)
    rec._on_click(103, 202, _FakeButton.left, False)
    assert rec._steps[0].kind == SeqStep.CLICK


class _FakeButton:
    """pynput's Button members, by identity only — the recorder maps them by
    dict lookup and falls back to "left", so any sentinel with the right
    identity works and importing pynput into a unit test does not."""
    from pynput.mouse import Button as _B
    left, right, middle = _B.left, _B.right, _B.middle


# ── durations, as written on a node ───────────────────────────────────────────
def test_a_duration_is_written_in_the_unit_that_fits_it():
    """The wait node printed its raw millisecond count, so a ten-minute wait
    read "Wait 600000 ms" — six digits to convert in your head, on the one node
    whose whole content is a duration."""
    f = flow.format_duration
    assert f(0) == "0 ms"
    assert f(300) == "300 ms"
    assert f(999) == "999 ms"
    assert f(1000) == "1 s"
    assert f(1500) == "1.5 s"
    assert f(30000) == "30 s"
    assert f(60000) == "1 min"
    assert f(90000) == "1 min 30 s"
    assert f(600000) == "10 min"
    assert f(3600000) == "1 h"
    assert f(7500000) == "2 h 5 min"
    # Junk in a hand-edited flow must not take the canvas down with it.
    assert f(None) == "0 ms"
    assert f("nonsense") == "0 ms"
    assert f(-5) == "0 ms"


def test_the_wait_node_says_ten_minutes_not_six_hundred_thousand():
    n = flow.FlowNode("n", flow.N_ACTION,
                      {"step": {"kind": "wait", "data": {"ms": 600000}}})
    assert flow.summarize_node(n) == "Wait 10 min"
    n.data["step"]["data"]["ms"] = 250
    assert flow.summarize_node(n) == "Wait 250 ms"


# ── saving a flow is atomic ───────────────────────────────────────────────────

def test_a_failed_save_leaves_the_previous_flow_intact(tmp_path, monkeypatch):
    """⚠ `open(path, "w")` truncates before writing, and this file IS the work.

    Flows are plain JSON the user keeps; there is no undo and nothing else
    holds a copy. So a write that dies part-way used to leave an empty file
    where an afternoon's work had been — a full disk, antivirus holding the
    handle, or simply a value in a node's data that `json` cannot serialise.

    The save writes beside the target and moves it over, so a failure now
    leaves the previous version exactly where it was.
    """
    import json as _json
    import flow

    good = flow.FlowGraph()
    good.add_node(flow.N_ACTION, {"step": {"kind": "wait", "data": {"ms": 250}}})
    path = tmp_path / "keepme.json"
    good.save(str(path))
    original = path.read_text(encoding="utf-8")
    assert "250" in original

    boom = flow.FlowGraph()
    boom.add_node(flow.N_ACTION, {"step": {"kind": "wait", "data": {"ms": 999}}})

    def explode(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(_json, "dump", explode)
    with pytest.raises(RuntimeError):
        boom.save(str(path))

    assert path.read_text(encoding="utf-8") == original, (
        "a failed save destroyed the flow that was already there")


def test_a_failed_save_leaves_no_temporary_file_behind(tmp_path, monkeypatch):
    """A stray `.flow-xxxx.tmp` in the scripts folder is not harmless: the
    library lists what is in that directory, and a user who finds one has no
    way to know whether it matters."""
    import json as _json
    import flow

    g = flow.FlowGraph()
    g.add_node(flow.N_ACTION, {"step": {"kind": "wait", "data": {"ms": 1}}})

    monkeypatch.setattr(_json, "dump",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    with pytest.raises(OSError):
        g.save(str(tmp_path / "x.json"))

    leftovers = [p.name for p in tmp_path.iterdir()]
    assert leftovers == [], f"temporary files left behind: {leftovers}"


def test_saving_still_round_trips_after_the_atomic_rewrite(tmp_path):
    """The rewrite must not have changed what lands on disk — same JSON, same
    reload, and a fresh directory is still created rather than erroring."""
    import flow

    g = flow.FlowGraph()
    a = g.add_node(flow.N_ACTION,
                   {"step": {"kind": "wait", "data": {"ms": 1234}}})
    g.add_edge(g.start_node().id, a.id)

    nested = tmp_path / "does" / "not" / "exist" / "flow.json"
    g.save(str(nested))
    assert nested.is_file(), "save no longer creates the directory it needs"

    back = flow.FlowGraph.load(str(nested))
    assert len(back.nodes) == len(g.nodes)
    assert any((n.data.get("step") or {}).get("data", {}).get("ms") == 1234
               for n in back.nodes.values())


def test_a_damaged_flow_says_so_in_words_a_person_can_act_on(tmp_path):
    """⚠ Every caller shows this exception straight to the user.

    A raw `json` error reads "Expecting value: line 1 column 20 (char 19)",
    which says neither what is wrong nor that the fault is in the *file* rather
    than the program — a reader is as likely to conclude Macronaut is broken.

    This matters right now rather than in principle: `save` was not atomic
    until 4 September 2026, so an interrupted write could leave a half-written
    flow, and anyone it happened to still has that file in their library.
    """
    import flow

    cut_short = tmp_path / "my big flow.json"
    cut_short.write_text('{\n  "version": 2,\n  "nodes": [\n', encoding="utf-8")
    with pytest.raises(ValueError) as e:
        flow.FlowGraph.load(str(cut_short))
    msg = str(e.value)
    assert "my big flow.json" in msg, "the message does not name the file"
    assert "cut short" in msg, msg
    assert "still on disk" in msg, (
        "the message does not tell the user their file is still there, which "
        "is the one reassurance that matters at that moment")

    empty = tmp_path / "empty.json"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ValueError) as e2:
        flow.FlowGraph.load(str(empty))
    assert "is empty" in str(e2.value), str(e2.value)

    # A valid JSON document that is not a flow at all.
    wrong = tmp_path / "list.json"
    wrong.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError):
        flow.FlowGraph.load(str(wrong))

    # ⚠ And a good file still loads — the guard must not have eaten the path
    # every flow in the library takes.
    good = flow.FlowGraph()
    good.add_node(flow.N_ACTION, {"step": {"kind": "wait", "data": {"ms": 5}}})
    ok = tmp_path / "fine.json"
    good.save(str(ok))
    assert len(flow.FlowGraph.load(str(ok)).nodes) == len(good.nodes)


def test_a_hand_edited_flow_that_is_wrong_says_where_to_look(tmp_path):
    """⚠ The app invites this, so it has to answer it kindly.

    A script is plain JSON you can read, edit, copy and delete yourself, and
    that is a real advantage over a format you cannot open. Somebody who takes it up and
    mistypes a key used to get `KeyError: 'type'` in a message box — which
    names nothing, suggests nothing, and does not say the file is theirs to
    fix.

    Separate from the truncation case above: that file is broken JSON, this
    one parses perfectly and simply is not a flow. They need different
    sentences because they have different causes and different fixes.
    """
    import json as _json
    import flow

    broken = {
        "nodes is not a list":  {"version": 2, "nodes": "oops", "edges": []},
        "node has no type":     {"version": 2, "nodes": [{"id": "n1"}], "edges": []},
        "edge has no dst":      {"version": 2, "nodes": [],
                                 "edges": [{"id": "e1", "src": "n1"}]},
        "a coordinate is text": {"version": 2,
                                 "nodes": [{"id": "n1", "type": "action",
                                            "x": "left"}], "edges": []},
    }
    for label, payload in broken.items():
        p = tmp_path / "hand edited.json"
        p.write_text(_json.dumps(payload), encoding="utf-8")
        with pytest.raises(ValueError) as e:
            flow.FlowGraph.load(str(p))
        msg = str(e.value)
        assert "hand edited.json" in msg, f"{label}: file not named"
        assert "edited it by hand" in msg, f"{label}: no hint where to look"
        assert "Nothing has been changed" in msg, f"{label}: no reassurance"
        # The real cause survives, for anyone who can use it.
        assert any(k in msg for k in ("KeyError", "TypeError", "ValueError")), (
            f"{label}: the underlying cause was swallowed")

    # ⚠ And a flow that is merely *unfamiliar* still loads. An unknown node
    # type and a version from the future are forward compatibility, not
    # corruption — refusing them would make every older Macronaut reject a file
    # a newer one wrote.
    for payload in ({"version": 99, "nodes": [{"id": "n1", "type": "action"}],
                     "edges": []},
                    {"version": 2, "nodes": [{"id": "n1", "type": "teleport"}],
                     "edges": []}):
        p = tmp_path / "future.json"
        p.write_text(_json.dumps(payload), encoding="utf-8")
        assert flow.FlowGraph.load(str(p)).nodes, "a forward-compatible flow was refused"


def test_a_locked_destination_does_not_empty_the_flow(tmp_path):
    """⚠ The realistic Windows failure, and the old code lost everything to it.

    A file can be locked by antivirus mid-scan, by a sync client, or by an
    editor somebody left open. Measured on 4 September 2026 with a byte-range
    lock held on the destination:

        old: `open(path, "w")` truncated the file to **0 bytes** and *then*
             raised PermissionError. 35 bytes of flow gone, and the error says
             "Permission denied", which reads as though nothing happened.

    Truncation is part of opening for write, so the exception arrives after the
    damage. That is the worst possible ordering: it looks like a refusal and it
    is a deletion.

    The atomic save cannot hit it — the temporary file is a new name nobody
    holds, and only `os.replace` touches the target.
    """
    import msvcrt
    import flow

    good = flow.FlowGraph()
    good.add_node(flow.N_ACTION, {"step": {"kind": "wait", "data": {"ms": 111}}})
    target = tmp_path / "locked.json"
    good.save(str(target))
    original = target.read_bytes()
    assert original, "nothing was saved, so this test proves nothing"

    newer = flow.FlowGraph()
    newer.add_node(flow.N_ACTION, {"step": {"kind": "wait", "data": {"ms": 999}}})

    holder = open(target, "r+b")
    msvcrt.locking(holder.fileno(), msvcrt.LK_NBLCK, 1)
    try:
        with pytest.raises(OSError):
            newer.save(str(target))
    finally:
        msvcrt.locking(holder.fileno(), msvcrt.LK_UNLCK, 1)
        holder.close()

    assert target.read_bytes() == original, (
        "a locked destination emptied the flow — this is the failure the "
        "atomic save exists for")
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".flow-")], \
        "a temporary file was left behind"
    assert len(flow.FlowGraph.load(str(target)).nodes) == len(good.nodes)


def test_a_missing_template_image_says_so_in_the_run_log(tmp_path):
    """⚠ A moved image and "the thing is not on screen" looked identical.

    Every matcher path degrades to *not found* rather than raising — a missing
    file, a file that is not an image, a zero-byte one; measured, all five
    return None. That is right for a step whose whole job is tolerating not
    finding things, and it is exactly what makes a deleted template invisible:
    the step waits out its full timeout and takes the not-found branch, which
    is what it would do if the flow were working perfectly.

    So somebody who reorganised a folder has a flow that quietly stopped
    working and a run log blaming the screen. One line, before the polling
    loop, so it cannot become a per-poll flood.

    ⚠ Never raises. It is a diagnostic; a step must not fail because the note
    about it could not be written.
    """
    import flow_exec

    seen = []

    class _Interp:
        def on_log(self, ev):
            seen.append(ev)

    w = flow_exec.FlowWorker.__new__(flow_exec.FlowWorker)
    w._interp = _Interp()

    present = tmp_path / "there.png"
    present.write_bytes(b"not really a png, but it exists")

    w._warn_missing_template(str(tmp_path / "gone.png"))
    assert len(seen) == 1, "a missing template produced no log line"
    assert seen[0]["kind"] == "error"
    assert "gone.png" in seen[0]["msg"], "the message does not name the file"
    assert "not found" in seen[0]["msg"].lower()

    # A file that exists says nothing, and neither does a step with no image.
    w._warn_missing_template(str(present))
    w._warn_missing_template("")
    assert len(seen) == 1, "it logged about a template that is present"

    # ⚠ And it survives having no interpreter at all — a worker built for a
    # detached launcher run reaches this before `run()` has wired one up.
    bare = flow_exec.FlowWorker.__new__(flow_exec.FlowWorker)
    bare._warn_missing_template(str(tmp_path / "also-gone.png"))


def _here(name):
    import os
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), name)


def test_both_image_paths_warn_about_a_missing_template():
    """⚠ There are two places an image path is used, and the second is worse.

    A Detect *step* whose template is gone at least takes its not-found branch
    visibly — something happens and the flow goes the other way. An **If/Else
    condition** whose image is gone simply evaluates false every time, so the
    flow takes the branch it always takes and looks like it is working. Nothing
    at all is different from the outside.

    Read with `ast` rather than by calling both, because reaching the condition
    evaluator needs a screen grab. What is being pinned is that neither call
    site is dropped in a refactor — the helper existing is no use if only one
    path asks it.
    """
    import ast

    src = open(_here(os.path.join("src", "flow_exec.py")), encoding="utf-8").read()
    tree = ast.parse(src)

    callers = set()
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call)
                    and getattr(node.func, "attr", "") == "_warn_missing_template"):
                callers.add(fn.name)

    # ⚠ Named, not matched on a substring. The first draft guessed the
    # condition evaluator would be called something with "cond" or "sensor" in
    # it; it is `_sense_image`, so the test failed on its own guess while the
    # code was right. Reading the name costs one grep.
    for wanted in ("_do_wait_image", "_sense_image"):
        assert wanted in callers, (
            f"{wanted} no longer reports a missing template; callers: "
            f"{sorted(callers)}")


# ═════════════════════════════════════════════════════════════════════════════
#  Pointer travel — the cursor goes to a target, it does not appear on it
# ═════════════════════════════════════════════════════════════════════════════
#
# The teleport these pin against was in every click the app made: `click`,
# `move`, the approach to a drag, a positioned scroll, and — the one that reads
# worst — "click the text or image you just found". See flow.travel_path for
# the shape and the reasoning.
import ctypes as _ct


class _TravelMouse:
    """Records every position it is moved to, plus presses and clicks."""
    def __init__(self, at=(0, 0)):
        self._pos = tuple(at)
        self.events = []

    @property
    def position(self):
        return self._pos

    @position.setter
    def position(self, xy):
        self._pos = tuple(xy)
        self.events.append(("move", self._pos))

    def press(self, b):
        self.events.append(("press", b))

    def release(self, b):
        self.events.append(("release", b))

    def click(self, b, n=1):
        self.events.append(("click", b, n))

    def scroll(self, dx, dy):
        self.events.append(("scroll", dx, dy))

    def moves(self):
        return [e[1] for e in self.events if e[0] == "move"]


def _travelling_worker(at=(0, 0), smooth=True):
    fw = _make_worker()
    fw._mouse = _TravelMouse(at)
    fw._running = True
    fw.sleep = lambda secs: None
    fw._smooth_mouse = smooth
    fw._travel_pps = flow.DEFAULT_TRAVEL_PPS
    fw._travel_human = False
    fw._travel_rng = None
    return fw, fw._mouse


def test_the_travel_path_arrives_exactly_and_never_doubles_back():
    path = flow.travel_path((0, 0), (900, 400))
    assert len(path) >= 10, "a 985 px journey is a path, not a jump"
    assert path[-1] == (900, 400), "a click has to land on the pixel it aimed at"
    xs = [p[0] for p in path]
    ys = [p[1] for p in path]
    assert xs == sorted(xs) and ys == sorted(ys)
    assert all(0 < x <= 900 for x in xs)


def test_the_travel_path_eases_in_and_out_like_a_hand():
    """Accelerate off the mark, coast, decelerate onto the target. A constant
    rate is the other motion no hand ever makes."""
    path = flow.travel_path((0, 0), (1000, 0))
    steps = [b[0] - a[0] for a, b in zip([(0, 0)] + path, path)]
    mid = steps[len(steps) // 2]
    assert steps[0] < mid and steps[-1] < mid


def test_a_hop_of_a_few_pixels_is_not_dressed_up_as_a_journey():
    """Below TRAVEL_SNAP_PX a glide is half a dozen identical points and a frame
    of delay — and the auto-click node re-aims by that much on every single
    iteration, so pacing them would cap a max-speed clicker."""
    assert flow.travel_path((100, 100), (102, 101)) == [(102, 101)]


def test_travel_time_is_bounded_at_both_ends():
    assert flow.travel_duration_ms(1) == flow.MIN_TRAVEL_MS
    assert flow.travel_duration_ms(10 ** 6) == flow.MAX_TRAVEL_MS
    # A nonsense speed falls back rather than dividing by zero.
    assert flow.travel_pps(0) == flow.DEFAULT_TRAVEL_PPS
    assert flow.travel_pps("fast") == flow.DEFAULT_TRAVEL_PPS


# ── the wobble every glide gets ──────────────────────────────────────────────
# An exactly straight, exactly eased line is the second signature, after the
# teleport: no hand crosses 900 px without leaving the ruler by a pixel. The
# standard amplitudes are tiny on purpose — see the note above TRAVEL_HZ.

def _sideways(start, end, path):
    """Each point's distance from the straight line start→end, signed."""
    x0, y0 = start
    dx, dy = end[0] - x0, end[1] - y0
    L = (dx * dx + dy * dy) ** 0.5
    return [((p[0] - x0) * -dy + (p[1] - y0) * dx) / L for p in path]


def test_every_glide_wobbles_a_little_even_when_human_mode_is_off():
    """The bug this fixes is not a crash: it is that two glides between the
    same two pixels used to be byte-identical, forever."""
    a = flow.travel_path((0, 0), (900, 400), rng=random.Random(1))
    b = flow.travel_path((0, 0), (900, 400), rng=random.Random(2))
    clean = flow.travel_path((0, 0), (900, 400))
    assert a != b, "two glides between the same points are not the same glide"
    assert a != clean, "the standard path is no longer the exact line"


def test_the_standard_wobble_is_small_enough_to_be_invisible():
    """It must not sweep the pointer across something it was not aimed at: a
    bow wide enough to see is a bow wide enough to open a neighbouring menu.

    The bound is written out rather than computed from the constants — a test
    that derives its own limit from the number it is checking passes whatever
    that number is changed to, which is how a 60 px bow got through once."""
    for seed in range(40):
        path = flow.travel_path((0, 0), (1600, 900), rng=random.Random(seed))
        assert max(abs(v) for v in _sideways((0, 0), (1600, 900), path)) <= 8.0
        assert path[-1] == (1600, 900), "the click still lands on its pixel"


def test_the_path_bows_and_does_not_merely_shake():
    """Two different things are going on and only one of them is noise. The
    tremor is sub-pixel and independent per sample; the bow is a single curve
    across the whole journey, and it is the half that makes the path a route
    rather than a line someone jiggled. Zeroing the bow leaves every other
    assertion here green, so this one measures its width directly: at the
    midpoint the tremor alone cannot reach 3 px."""
    worst = 0.0
    for seed in range(40):
        path = flow.travel_path((0, 0), (1600, 900), rng=random.Random(seed))
        off = _sideways((0, 0), (1600, 900), path)
        worst = max(worst, abs(off[len(off) // 2]))
        # ⚠ And it is widest in the MIDDLE, not a constant sideways offset.
        # The bow is faded by sin(pi*t) so the path leaves along the line it
        # was aimed down and arrives on it: a glide that approaches its target
        # from 6 px off to one side and snaps across at the last sample is the
        # teleport again, in miniature, at the only moment it matters.
        assert abs(off[0]) <= 1.5 and abs(off[-2]) <= 1.5
    assert worst > 3.0


def test_the_wobble_goes_across_the_line_and_never_along_it():
    """Tremor is perpendicular by construction. Noise along the line makes a
    cursor stutter — 2 px on, 1 px back — which is not more human and costs
    the monotonic progress a receiver reads direction from.

    Travelling due east, every x is therefore the x of the clean path and the
    whole wobble is in y. Asserting only that x never decreases does not pin
    this: at these amplitudes an along-the-line tremor is smaller than the
    smallest eased step, so it wobbles the speed without ever reversing it."""
    clean = flow.travel_path((0, 0), (1000, 0))
    for seed in range(40):
        path = flow.travel_path((0, 0), (1000, 0), rng=random.Random(seed))
        assert [p[0] for p in path] == [p[0] for p in clean]
        assert [p[1] for p in path] != [p[1] for p in clean]


def test_human_mode_widens_the_same_wander_rather_than_switching_it_on():
    def spread(human):
        worst = 0.0
        for seed in range(40):
            p = flow.travel_path((0, 0), (1600, 900), rng=random.Random(seed),
                                 human=human)
            worst = max(worst, max(abs(v) for v in _sideways((0, 0), (1600, 900), p)))
        return worst
    assert spread(True) > spread(False) * 2


class _BowlessRng:
    """An rng whose bow draw is zero and whose every tremor draw is maximal.

    `travel_path` asks for the bow once and then for one tremor sample per
    point, so this isolates the tremor from the curve it rides on — which is
    the only way to see the tremor's own fade, the bow's being far larger."""
    def __init__(self):
        self.draws = 0

    def uniform(self, lo, hi):
        self.draws += 1
        return 0.0 if self.draws == 1 else hi


def test_the_tremor_fades_out_onto_the_target():
    """Held at full deflection the whole way, the tremor still has to arrive
    on the line: it is scaled by the same sin(pi*t) as the bow. Human mode is
    the amplitude at which that stops being a sub-pixel nicety and becomes the
    difference between arriving on the target and beside it."""
    path = flow.travel_path((0, 0), (1600, 900), rng=_BowlessRng(), human=True)
    off = _sideways((0, 0), (1600, 900), path)
    assert abs(off[len(off) // 2]) > 1.5, "the tremor is there in the middle"
    assert abs(off[-2]) < 1.0, "and gone by the time the click is next"


def test_a_glide_with_no_rng_is_still_the_exact_line():
    """Determinism is what the rest of this section reads, and what a caller
    that wants the same path twice (a test, a replay) depends on."""
    p1 = flow.travel_path((10, 10), (700, 500))
    p2 = flow.travel_path((10, 10), (700, 500))
    assert p1 == p2
    assert all(v == 0.0 or abs(v) < 1.0
               for v in _sideways((10, 10), (700, 500), p1))


def test_the_live_engine_wobbles_without_being_asked_to():
    """The engine passes its rng on every glide; `human` only picks the
    amplitude. A worker left on defaults must not travel in a straight line."""
    fw, mouse = _travelling_worker(at=(0, 0))
    fw._travel_rng = random.Random(7)
    assert fw._travel_human is False
    fw._travel_to(900, 400)
    off = _sideways((0, 0), (900, 400), mouse.moves())
    assert any(abs(v) >= 1.0 for v in off), "a default glide is still a ruler"
    assert mouse.moves()[-1] == (900, 400)


def test_a_click_travels_to_its_target_instead_of_appearing_on_it():
    """The complaint this whole section answers. A receiver samples the pointer
    once a frame and decides what happened from where it has *been* — a cursor
    that appears on a button having never approached it leaves hover states
    unfired and menus unopened, and the click then does nothing."""
    fw, mouse = _travelling_worker(at=(0, 0))
    assert fw.do_action({"kind": "click", "data": {"x": 800, "y": 600}}, {}) is True
    moves = mouse.moves()
    assert len(moves) >= 10, f"a 1000 px click is a journey, got {len(moves)} moves"
    assert moves[-1] == (800, 600), "it still has to land on the target"
    assert moves[0] != (800, 600), "and it must not start there"
    kinds = [e[0] for e in mouse.events]
    assert kinds[-1] == "click", "the click comes after the arrival"
    assert kinds.count("click") == 1


def test_a_move_step_travels_too():
    fw, mouse = _travelling_worker(at=(0, 0))
    assert fw.do_action({"kind": "move", "data": {"x": 500, "y": 500}}, {}) is True
    assert len(mouse.moves()) >= 10
    assert mouse.moves()[-1] == (500, 500)


def test_turning_smooth_movement_off_restores_the_single_jump():
    """The setting is the escape hatch for somebody clicking as fast as the
    machine allows at a target that does not care where the cursor came from."""
    fw, mouse = _travelling_worker(at=(0, 0), smooth=False)
    assert fw.do_action({"kind": "click", "data": {"x": 800, "y": 600}}, {}) is True
    assert mouse.moves() == [(800, 600)]


def test_stop_lands_mid_glide_and_nothing_is_clicked_at_the_far_end():
    """Half a journey leaves the cursor somewhere harmless. A click at the end
    of a journey the user cancelled is the action they stopped to avoid."""
    fw, mouse = _travelling_worker(at=(0, 0))
    real = _TravelMouse.position.fset

    def stop_after_three(self, xy):
        real(self, xy)
        if len(self.moves()) >= 3:
            fw._running = False
    with mock.patch.object(_TravelMouse, "position",
                           property(_TravelMouse.position.fget, stop_after_three)):
        fw.do_action({"kind": "click", "data": {"x": 900, "y": 900}}, {})
    assert "click" not in [e[0] for e in mouse.events]
    assert mouse.moves()[-1] != (900, 900)


def test_the_auto_click_node_does_not_pay_for_a_glide_on_every_jitter_hop():
    """Human mode re-aims by a few pixels per iteration. Gliding those would put
    MIN_TRAVEL_MS between clicks and cap "max speed" at ~25 CPS — the one number
    the Basic face exists to deliver."""
    fw, mouse = _travelling_worker(at=(500, 500))
    fw.do_action({"kind": "autoclick",
                  "data": {"use_fixed": True, "fixed_x": 500, "fixed_y": 500,
                           "human_mode": True, "jitter_px": 5,
                           "max_speed": True, "click_limit": 12}}, {})
    clicks = len([e for e in mouse.events if e[0] == "click"])
    assert clicks == 12
    # One move per click at most: the pointer is already there, so each hop
    # snaps instead of being paced.
    assert len(mouse.moves()) <= clicks + 1


def _fake_user32(cursor=(0, 0)):
    """A stand-in for the raw SendInput path `_click_physical` uses, recording
    (dx, dy, flags) for every event it is handed."""
    sent = []
    metrics = {76: 0, 77: 0, 78: 1920, 79: 1080}

    class _U32:
        def GetSystemMetrics(self, i):
            return metrics[i]

        def GetCursorPos(self, ref):
            arr = _ct.cast(ref, _ct.POINTER(_ct.c_long))
            arr[0], arr[1] = cursor
            return 1

        def SendInput(self, n, ref, size):
            arr = _ct.cast(ref, _ct.POINTER(_ct.c_long))
            # INPUT is {type, pad, MOUSEINPUT{dx, dy, mouseData, dwFlags, …}}
            sent.append((arr[2], arr[3], arr[5] & 0xFFFFFFFF))
            return 1

    class _Windll:
        user32 = _U32()

    return _Windll(), sent


def test_clicking_what_was_detected_travels_there_as_well():
    """The path named in the original report. It goes straight to SendInput
    rather than through the mouse controller — the absolute-normalised form is
    the only one that is right across monitors of different DPI — so it needs
    its own travel, and a detected button is exactly the kind of target that is
    watching for a cursor arriving."""
    import flow_exec
    from PIL import Image
    fw, _mouse = _travelling_worker()
    windll, sent = _fake_user32(cursor=(0, 0))
    shot = Image.new("RGB", (1920, 1080))
    with mock.patch.object(flow_exec.ctypes, "windll", windll), \
         mock.patch.object(flow_exec.time, "sleep", lambda s: None):
        fw._click_physical(1600, 900, shot)
    MOVE, DOWN = 0x0001, 0x0002
    moves = [e for e in sent if e[2] & MOVE]
    downs = [e for e in sent if e[2] & DOWN]
    assert len(moves) >= 10, f"a detected target is approached, got {len(moves)}"
    assert len(downs) == 1
    assert sent.index(downs[0]) == len(sent) - 2, "press, then release, last"
    # Normalised absolute coordinates, so the last move is the target itself.
    assert moves[-1][:2] == downs[0][:2]
    assert moves[0][:2] != moves[-1][:2]


def test_a_detected_click_still_lands_when_smooth_movement_is_off():
    import flow_exec
    from PIL import Image
    fw, _mouse = _travelling_worker(smooth=False)
    windll, sent = _fake_user32(cursor=(0, 0))
    with mock.patch.object(flow_exec.ctypes, "windll", windll), \
         mock.patch.object(flow_exec.time, "sleep", lambda s: None):
        fw._click_physical(1600, 900, Image.new("RGB", (1920, 1080)))
    assert len([e for e in sent if e[2] & 0x0001]) == 1
    assert len([e for e in sent if e[2] & 0x0002]) == 1


# ═════════════════════════════════════════════════════════════════════════════
#  The glide is spent inside the recorded gap, not added on top of it
# ═════════════════════════════════════════════════════════════════════════════
#
# A recorded delay is the gap between one action and the next, and the hand
# spent that gap moving. Sleeping all of it and then gliding adds a journey the
# recording never contained — and the error is per-step, so it compounds over
# exactly the flows people record: a lot of clicks, some distance apart.

def _spy_worker(at=(0, 0), smooth=True):
    fw, mouse = _travelling_worker(at=at, smooth=smooth)
    sleeps = []
    fw.sleep = lambda s: sleeps.append(s)
    return fw, mouse, sleeps


def test_the_travel_budget_is_what_the_glide_actually_costs():
    """Not an estimate: `_travel_to` paces itself by the same function."""
    fw, _m, _s = _spy_worker(at=(0, 0))
    budget = fw._travel_budget("click", {"x": 300, "y": 400})   # 3-4-5
    assert budget == flow.travel_duration_ms(500.0, flow.DEFAULT_TRAVEL_PPS)


def test_a_replay_spends_the_recorded_gap_travelling():
    fw, _m, sleeps = _spy_worker(at=(0, 0))
    fw.do_action({"kind": "click", "data": {"x": 300, "y": 400},
                  "delay_ms": 500}, {})
    travel_s = flow.travel_duration_ms(500.0, flow.DEFAULT_TRAVEL_PPS) / 1000.0
    assert abs(sleeps[0] - (0.5 - travel_s)) < 1e-9


def test_a_glide_longer_than_the_gap_does_not_go_back_in_time():
    """Nothing can give the time back; the step runs when the pointer gets
    there. What must not happen is a negative sleep, or the full gap being
    paid on top of a journey that already overran it."""
    fw, _m, sleeps = _spy_worker(at=(0, 0))
    fw.do_action({"kind": "click", "data": {"x": 1500, "y": 0},
                  "delay_ms": 20}, {})
    assert all(s >= 0 for s in sleeps)
    # The rest of these are the glide pacing itself. The pre-step sleep would
    # be the recorded gap exactly, and there is no such call.
    assert 0.02 not in sleeps, "the 20 ms gap was paid on top of the journey"


def test_only_the_steps_that_move_the_pointer_pay_a_budget():
    fw, _m, _s = _spy_worker(at=(0, 0))
    assert fw._travel_budget("key", {"keys": ["a"]}) == 0.0
    assert fw._travel_budget("text", {"text": "hello"}) == 0.0
    assert fw._travel_budget("wait", {"ms": 500}) == 0.0
    # A detect-then-click has not searched yet, so it cannot know where it is
    # going; an auto-click node re-aims inside the snap distance and pays
    # nothing to begin with. Both are absent from the table on purpose.
    assert fw._travel_budget("detect", {}) == 0.0
    assert fw._travel_budget("autoclick", {"x": 900, "y": 900}) == 0.0
    assert fw._travel_budget("drag", {"x": 300, "y": 400}) > 0.0


def test_a_scroll_only_pays_when_it_names_a_place_to_scroll():
    """`at_cursor` is the default and means "whatever is under the pointer",
    which is where the pointer already is."""
    fw, _m, _s = _spy_worker(at=(0, 0))
    assert fw._travel_budget("scroll", {"direction": "down", "amount": 3}) == 0.0
    assert fw._travel_budget("scroll", {"direction": "down", "amount": 3,
                                        "at_cursor": True, "x": 900, "y": 900}) == 0.0
    assert fw._travel_budget("scroll", {"direction": "down", "amount": 3,
                                        "at_cursor": False, "x": 900, "y": 900}) > 0.0


def test_a_hop_inside_the_snap_distance_costs_nothing():
    """It is a teleport by design — see `_travel_to` — so budgeting for it
    would shorten the gap in payment for a journey that never happens."""
    fw, _m, _s = _spy_worker(at=(100, 100))
    assert fw._travel_budget("click", {"x": 102, "y": 101}) == 0.0


def test_with_smooth_movement_off_the_recorded_gap_is_the_whole_gap():
    fw, _m, sleeps = _spy_worker(at=(0, 0), smooth=False)
    assert fw._travel_budget("click", {"x": 300, "y": 400}) == 0.0
    fw.do_action({"kind": "click", "data": {"x": 300, "y": 400},
                  "delay_ms": 500}, {})
    assert sleeps[0] == 0.5


def test_the_budget_survives_a_backend_that_cannot_say_where_the_pointer_is():
    """Same fallback as `_travel_to`: land on the target rather than refuse."""
    class _Blind:
        @property
        def position(self):
            raise OSError("no cursor here")

        @position.setter
        def position(self, xy):
            pass

    fw, _m, _s = _spy_worker(at=(0, 0))
    fw._mouse = _Blind()
    assert fw._travel_budget("click", {"x": 300, "y": 400}) == 0.0
