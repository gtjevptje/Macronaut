"""Adversarial graphs through the interpreter.

Flows are plain JSON on purpose and the README invites you to open one, so the
interpreter's real input is not "whatever the canvas produced" — it is
anything. These build graphs the canvas would never make: labels named `None`,
a loop counting to `"seven"`, an op called `"??"`, ports that do not belong on
the node they leave, Go tos pointing at nothing, and cycles.

`run()` already catches broadly, so "does it raise" is not the question. These
are the questions:

  * does it always terminate, and quickly?
  * is the status always one of the documented ones?
  * do the variables survive as a dict?

⚠ Deterministic by seed, so a failure is reproducible. The same generator
with the iteration count turned up —
8,000 graphs — found nothing on 4 September 2026, which is what earned this a
place in the suite rather than a place in the bin.

⚠ Two ways this was silently vacuous before it worked, both worth remembering
because neither failed:

  * the edge helper is `add_edge`, not `connect`, and the call sat inside a
    bare `except Exception: pass` — so no graph had any wires, every run was
    one step long, and it reported "3000 graphs, 0 problems". The tell was the
    clock: 0.1 s for 3,000 graphs.
  * ports were then chosen uniformly at random, so a Start node's `out` port
    was wired 1 time in 7 and the median run was still 1 step. A fuzzer that
    does not reach the code cannot find anything in it.

Coverage is asserted below for exactly that reason.
"""
import os
import random
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import flow

SEED = 20260904
GRAPHS = 800
STATUSES = {"idle", "running", "done", "stopped", "error"}

NODE_TYPES = [flow.N_START, flow.N_ACTION, flow.N_IF, flow.N_LOOP,
              flow.N_SETVAR, flow.N_LABEL, flow.N_GOTO, flow.N_END,
              flow.N_COMMENT, flow.N_REROUTE, flow.N_FRAME]

PORTS = ["out", "true", "false", "body", "done", "err", ""]


class _Stub:
    """Does nothing, quickly, and stops when its budget runs out.

    `sleep` is a no-op: a fuzzed graph can ask for a ten-minute wait, and what
    is under test is control flow, not time passing.
    """

    def __init__(self, budget=8000):
        self.calls = 0
        self.budget = budget
        self.actions = 0
        self.rng = random.Random(SEED)

    def running(self) -> bool:
        self.calls += 1
        return self.calls < self.budget

    def sleep(self, secs):
        pass

    def do_action(self, step, variables):
        self.actions += 1
        return self.rng.random() < 0.8          # actions fail sometimes

    def eval_sensor(self, cond, variables):
        return self.rng.random() < 0.5


def _hostile(rng, ntype):
    if ntype == flow.N_LOOP:
        return {"mode": rng.choice(["count", "forever", "while", ""]),
                "count": rng.choice([0, -5, 1, 3, 10 ** 9, "seven", None])}
    if ntype == flow.N_GOTO:
        # The interpreter reads target_name / target_label / target. `label`
        # is included precisely because it is the shape nothing reads.
        return rng.choice([
            {"target_label": rng.choice(["a", "b", "missing", "", None])},
            {"target_name": rng.choice(["a", "start", "missing", None])},
            {"target": rng.choice(["n1", "n99", None])},
            {"label": "a"},
            {},
        ])
    if ntype == flow.N_LABEL:
        return {"name": rng.choice(["a", "b", "", None])}
    if ntype == flow.N_SETVAR:
        return {"name": rng.choice(["x", "", None, "y"]),
                "op": rng.choice(["set", "add", "sub", "mul", "div", "??"]),
                "value": rng.choice([0, 1, -3, "abc", None, 2.5, 10 ** 9])}
    if ntype == flow.N_IF:
        return {"cond": rng.choice([
            {"type": "always"}, {"type": "never"}, {"type": "image"},
            {"type": "var", "name": "x", "op": "==", "value": 1},
            {"type": "nonsense"}, {}, None])}
    if ntype == flow.N_ACTION:
        return {"kind": rng.choice(["click", "type", "wait", "key", "drag",
                                    "scroll", "???"]),
                "retries": rng.choice([0, 1, 3, -1, "two"]),
                "retry_delay_ms": rng.choice([0, 10, -5]),
                "text": "x" * rng.randint(0, 5)}
    return {}


def _random_graph(rng):
    g = flow.FlowGraph()
    n = rng.randint(1, 12)
    ids = [g.add_node(flow.N_START if i == 0 else rng.choice(NODE_TYPES),
                      _hostile(rng, flow.N_START if i == 0
                               else NODE_TYPES[0]), x=i * 26, y=0).id
           for i in range(n)]
    # Re-stamp the data now that the type is known (kept simple on purpose).
    for nid in ids:
        node = g.nodes[nid]
        node.data = _hostile(rng, node.type) or node.data

    def port_for(node_id):
        t = g.nodes[node_id].type
        if rng.random() < 0.2:
            return rng.choice(PORTS)          # a port that does not belong
        if t == flow.N_IF:
            return rng.choice(["true", "false"])
        if t == flow.N_LOOP:
            return rng.choice(["body", "done"])
        return "out"

    g.add_edge(ids[0], rng.choice(ids), "out")   # Start always goes somewhere
    for _ in range(rng.randint(0, n * 2)):
        src = rng.choice(ids)
        g.add_edge(src, rng.choice(ids), port_for(src))
    if rng.random() < 0.2:
        g.variables = {"x": rng.choice([0, 1, "a", None])}
    return g


def _run_all():
    rng = random.Random(SEED)
    results = []
    for _ in range(GRAPHS):
        g = _random_graph(rng)
        ex = _Stub()
        interp = flow.FlowInterpreter(g, ex, max_steps=4000)
        log = []
        interp.on_log = log.append
        t0 = time.perf_counter()
        status = interp.run()
        results.append({
            "status": status,
            "secs": time.perf_counter() - t0,
            "vars": interp.vars,
            "actions": ex.actions,
            "steps": next((e.get("steps", 0) for e in reversed(log)
                           if e.get("kind") == "run_end"), 0),
        })
    return results


@pytest.fixture(scope="module")
def results():
    """One pass, shared. Deterministic by seed, so both tests see the same
    graphs and a failure in either is reproducible from the same run."""
    return _run_all()


def test_a_hand_edited_flow_cannot_break_the_interpreter(results):
    """No exception escapes, every status is one it documents, and every run
    ends. `run()` catching broadly is what makes this true — the point is to
    prove it stays true against input the canvas would never produce."""
    for i, r in enumerate(results):
        assert r["status"] in STATUSES, f"graph {i}: status {r['status']!r}"
        assert isinstance(r["vars"], dict), f"graph {i}: vars became {r['vars']!r}"
        assert r["secs"] < 5.0, f"graph {i}: took {r['secs']:.1f}s"


def test_the_fuzzer_actually_reaches_the_interpreter(results):
    """⚠ The assertion that keeps the one above honest.

    Both earlier versions of this passed while executing essentially nothing —
    once because no edges were created, once because a Start node's `out` port
    was picked at random from seven. Green means nothing unless the graphs run.
    """
    steps = sorted(r["steps"] for r in results)
    median = steps[len(steps) // 2]
    deep = sum(1 for s in steps if s > 1)
    actions = sum(r["actions"] for r in results)

    assert median >= 2, f"median run is {median} step(s) — the graphs stop at once"
    assert deep > GRAPHS * 0.9, f"only {deep}/{GRAPHS} runs got past step one"
    assert actions > 50, f"only {actions} actions executed across {GRAPHS} graphs"


def test_a_no_op_jump_ring_aborts_instead_of_hanging():
    """The accident a user can make: drop a Label and a Go to, wire them in a
    ring, press Play. It has to end, and say why.

    Measured at the real 2,000,000-step limit: 3.5 s, then "step limit
    exceeded". `executor.running()` is checked every iteration, so Stop works
    throughout.
    """
    g = flow.FlowGraph()
    s = g.add_node(flow.N_START, {"name": flow.START_NAME}, x=0, y=0)
    lab = g.add_node(flow.N_LABEL, {"name": "top"}, x=26, y=0)
    goto = g.add_node(flow.N_GOTO, {"target_label": "top"}, x=52, y=0)
    g.add_edge(s.id, lab.id, "out")
    g.add_edge(lab.id, goto.id, "out")

    class _Ex:
        def running(self): return True
        def sleep(self, secs): pass
        def do_action(self, step, v): return True
        def eval_sensor(self, cond, v): return False

    log = []
    interp = flow.FlowInterpreter(g, _Ex(), on_log=log.append, max_steps=5000)
    assert interp.run() == "error"
    assert any(e.get("kind") == "abort" and "step limit" in str(e.get("reason"))
               for e in log), "aborted without saying why"


# ── the loader ───────────────────────────────────────────────────────────────
#
# `FlowGraph.load` is the other surface that takes input nobody designed: a
# hand-edited file, a truncated one, a file from a future version, or something
# that is not a flow at all. On 4 September 2026 its error messages were
# rewritten to name the file and say the original is untouched — which only
# helps if every malformed document actually produces one of those errors
# rather than a raw KeyError in a dialog.
#
# Measured over 4,006 documents: 1,279 loaded (the deliberate permissiveness —
# unknown node types and future versions still open), 2,727 raised ValueError,
# and nothing else was raised at all.

def _junk(rng, depth=0):
    r = rng.random()
    if depth > 3 or r < 0.30:
        return rng.choice([0, 1, -1, 3.5, "", "x", None, True, False,
                           10 ** 12, "n1", [], {}])
    if r < 0.65:
        return [_junk(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    keys = ["version", "nodes", "edges", "variables", "meta", "id", "type",
            "data", "x", "y", "src", "dst", "src_port", "steps", "name"]
    return {rng.choice(keys): _junk(rng, depth + 1)
            for _ in range(rng.randint(0, 5))}


def test_no_file_makes_the_loader_raise_something_unplanned(tmp_path):
    """Either a FlowGraph or a ValueError. Never a KeyError in a dialog.

    ⚠ `main` shows `str(e)` from a bare `except Exception`, so an unplanned
    exception type does not crash the app — it just puts something like
    `KeyError: 'type'` in front of somebody, which is what the rewritten
    messages exist to replace.
    """
    import json

    rng = random.Random(7)
    path = tmp_path / "fuzz.json"
    loaded = refused = 0
    for _ in range(1200):
        try:
            text = json.dumps(_junk(rng))
        except Exception:
            continue
        path.write_text(text, encoding="utf-8")
        try:
            assert isinstance(flow.FlowGraph.load(str(path)), flow.FlowGraph)
            loaded += 1
        except ValueError:
            refused += 1

    assert loaded > 100, f"only {loaded} documents loaded — is the generator dead?"
    assert refused > 100, f"only {refused} were refused — is the generator dead?"


@pytest.mark.parametrize("text,label", [
    ("", "empty file"),
    ("{", "truncated mid-object"),
    ("]", "not an object at all"),
    ("not json at all", "plain prose"),
    ('{"nodes":', "cut off after a key"),
    ('{"nodes": [{"x": 1}]}', "a node with no id or type"),
    ('[1, 2, 3]', "valid JSON, wrong shape"),
])
def test_a_damaged_file_is_refused_by_name(tmp_path, text, label):
    """⚠ The message is the feature. A flow that will not open used to report
    `Expecting value: line 1 column 20 (char 19)`, which says neither what is
    wrong nor that the file rather than the program is the problem."""
    path = tmp_path / "my flow.json"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        flow.FlowGraph.load(str(path))

    msg = str(excinfo.value)
    assert "my flow.json" in msg, f"{label}: message does not name the file: {msg}"
    assert len(msg) > 40, f"{label}: message is too terse to help: {msg}"


def test_a_flow_from_a_newer_version_still_opens(tmp_path):
    """Deliberate: refusing it would be a worse problem than the one solved.
    The release notes promise this in as many words."""
    import json

    path = tmp_path / "from the future.json"
    path.write_text(json.dumps({
        "version": 99,
        "nodes": [{"id": "n1", "type": "start", "data": {}, "x": 0, "y": 0},
                  {"id": "n2", "type": "teleport", "data": {"whither": "?"},
                   "x": 26, "y": 0}],
        "edges": [{"id": "e1", "src": "n1", "dst": "n2", "src_port": "out"}],
        "variables": {}, "meta": {},
    }), encoding="utf-8")

    g = flow.FlowGraph.load(str(path))
    assert set(g.nodes) == {"n1", "n2"}
    assert g.nodes["n2"].type == "teleport", "an unknown node type was dropped"
