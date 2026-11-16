"""
Generate "Node feature test.json" — a single flow that exercises EVERY node
type and every setting, for manual feature/QA testing.

Run:  python make_node_test_script.py
Output: <scripts_dir>/Node feature test.json  (loadable from the compact
        script dropdown or Advanced ▸ Load), plus a copy in examples/.

Coverage:
  • Auto-Click node (full Basic-clicker config)
  • Click  — left/single, right/double, hold
  • Move · Key · Combo · Type-Text · Wait
  • Detect — Wait-for-Image, Wait-for-Text (with region + click-on-find)
  • If/Else — Always, Pixel, Image(timeout), Text(timeout, negated)
  • Loop   — Repeat N, While, Until, Forever (all max-iters capped)
  • Go to  — name-based jump
  • On-error — skip, goto+retries, and a wired red error port (visual try/catch)
  • Named nodes + an End node

The flow is wired so a real run TERMINATES safely (bounded loops, short detect
timeouts, forward-only goto). It still performs real clicks/keystrokes — run it
over a throwaway window (e.g. Notepad), not over anything that matters.
"""
import os
import shutil
import flow
from settings import scripts_dir

g = flow.FlowGraph()

DX, DY = 280, 140          # spine spacing
BX = 300                   # branch offset


def step(kind, data, delay_ms=0):
    return {"kind": kind, "data": data, "delay_ms": delay_ms}


def action(st, name="", on_error=None, x=0, y=0):
    d = {"step": st}
    if name:
        d["name"] = name
    if on_error:
        d["on_error"] = on_error
    return g.add_node(flow.N_ACTION, d, x=x, y=y)


def node(ntype, data=None, x=0, y=0):
    return g.add_node(ntype, data or {}, x=x, y=y)


def link(a, b, port="out"):
    g.add_edge(a.id, b.id, port)


y = 0
start = node(flow.N_START, x=0, y=y); y += DY

# ── action family ────────────────────────────────────────────────────────────
a_auto = action(step("autoclick", {
    "button": "left", "click_type": "single", "hold_duration_ms": 120,
    "max_speed": False, "unit": "cps", "cps": 8, "interval_ms": 125,
    "click_limit": 3, "stop_after_secs": 0.0,
    "human_mode": True, "jitter_px": 4,
    "randomize": False, "random_range_ms": 50,
    "use_fixed": False, "fixed_x": 0, "fixed_y": 0,
    "use_region": False, "region": [0, 0, 1920, 1080],
    "pause_on_focus": False, "focus_window": "",
    "wait_for_image": False, "image_path": "", "image_confidence": 0.8,
}), name="autoclick-demo", on_error={"mode": "skip", "retries": 0, "retry_delay_s": 0.5},
   x=0, y=y); y += DY
link(start, a_auto)

a_click = action(step("click", {"button": "left", "x": 400, "y": 400,
                                 "clicks": 1, "hold": False, "hold_ms": 1000}, 50),
                 name="click-left", on_error={"mode": "skip", "retries": 2,
                                              "retry_delay_s": 0.4}, x=0, y=y); y += DY
link(a_auto, a_click)

a_dbl = action(step("click", {"button": "right", "x": 200, "y": 200,
                              "clicks": 2, "hold": False, "hold_ms": 1000}),
               name="click-right-double", x=0, y=y); y += DY
link(a_click, a_dbl)

a_hold = action(step("click", {"button": "left", "x": 300, "y": 300,
                               "clicks": 1, "hold": True, "hold_ms": 800}),
                name="click-hold", x=0, y=y); y += DY
link(a_dbl, a_hold)

a_move = action(step("move", {"x": 600, "y": 350}), name="move", x=0, y=y); y += DY
link(a_hold, a_move)

a_key = action(step("key", {"keys": ["f5"], "repeat": 1}), name="key-f5", x=0, y=y); y += DY
link(a_move, a_key)

a_combo = action(step("combo", {"keys": ["ctrl", "a"], "repeat": 1}),
                 name="combo-ctrl-a", x=0, y=y); y += DY
link(a_key, a_combo)

a_text = action(step("text", {"text": "Macronaut node test ✔",
                              "speed_cps": 20}, 100),
                name="type-text", x=0, y=y); y += DY
link(a_combo, a_text)

a_wait = action(step("wait", {"ms": 400}), name="wait-400ms", x=0, y=y); y += DY
link(a_text, a_wait)

# ── Loop · Repeat N ──────────────────────────────────────────────────────────
loop1 = node(flow.N_LOOP, {"name": "loop-repeat", "mode": "repeat_n",
                           "count": 3, "max_iters": 1000}, x=0, y=y)
lb1 = action(step("wait", {"ms": 150}), name="loop-body-wait", x=BX, y=y)
link(a_wait, loop1); link(loop1, lb1, "body"); link(lb1, loop1)   # body cycles back
y += DY

# ── If · Always (true/false both populated, converge) ────────────────────────
if1 = node(flow.N_IF, {"name": "if-always",
                       "condition": {"type": "always", "negate": False}}, x=0, y=y)
link(loop1, if1, "done")
t1 = action(step("text", {"text": "true branch", "speed_cps": 30}),
            name="if-true", x=-BX, y=y + 40)
f1 = action(step("text", {"text": "false branch", "speed_cps": 30}),
            name="if-false", x=BX, y=y + 40)
link(if1, t1, "true"); link(if1, f1, "false")
y += DY

# ── If · Pixel ───────────────────────────────────────────────────────────────
if2 = node(flow.N_IF, {"name": "if-pixel",
                       "condition": {"type": "pixel", "x": 5, "y": 5,
                                     "color": "#101010", "tolerance": 30,
                                     "negate": False}}, x=0, y=y)
link(t1, if2); link(f1, if2)
y += DY

# ── If · Image (waits up to 2s) ──────────────────────────────────────────────
if3 = node(flow.N_IF, {"name": "if-image",
                       "condition": {"type": "image", "image_path": "",
                                     "confidence": 0.85, "region": None,
                                     "timeout_s": 2, "negate": False}}, x=0, y=y)
link(if2, if3, "true"); link(if2, if3, "false")
y += DY

# ── If · Text (waits up to 2s, NOT-inverted) ─────────────────────────────────
if4 = node(flow.N_IF, {"name": "if-text",
                       "condition": {"type": "text", "text": "hello",
                                     "case_sensitive": False, "fuzzy": True,
                                     "min_score": 0.5, "region": None,
                                     "timeout_s": 2, "negate": True}}, x=0, y=y)
link(if3, if4, "true"); link(if3, if4, "false")
y += DY

# ── Detect · Wait-for-Image (on_error = skip) ────────────────────────────────
d1 = action(step("wait_image", {"image_path": "", "confidence": 0.8,
                                "timeout_s": 2, "click": False, "button": "left",
                                "clicks": 1, "offset_x": 0, "offset_y": 0}),
            name="detect-image", on_error={"mode": "skip", "retries": 0,
                                           "retry_delay_s": 0.5}, x=0, y=y)
link(if4, d1, "true"); link(if4, d1, "false")
y += DY

# ── Detect · Wait-for-Text (region + click-on-find) ──────────────────────────
d2 = action(step("wait_text", {"text": "Start", "case_sensitive": False,
                               "min_score": 0.6, "timeout_s": 2, "click": True,
                               "button": "left", "clicks": 1,
                               "region": [100, 100, 400, 200], "fuzzy": True}),
            name="detect-text", on_error={"mode": "skip", "retries": 0,
                                          "retry_delay_s": 0.5}, x=0, y=y)
link(d1, d2)
y += DY

# ── Error PORT demo (visual try/catch): failure routes out the red port ──────
ep = action(step("wait_image", {"image_path": "", "confidence": 0.9,
                                "timeout_s": 2, "click": False, "button": "left",
                                "clicks": 1, "offset_x": 0, "offset_y": 0}),
            name="errport-source", x=0, y=y)
ep_catch = action(step("text", {"text": "caught via error port", "speed_cps": 30}),
                  name="errport-catch", x=BX, y=y + 40)
link(d2, ep)
link(ep, ep_catch, "error")        # red error port → recovery
y += DY

# ── Loop · While (pixel that won't match → exits fast) ───────────────────────
loop2 = node(flow.N_LOOP, {"name": "loop-while", "mode": "while",
                           "condition": {"type": "pixel", "x": 9, "y": 9,
                                         "color": "#abcdef", "tolerance": 5,
                                         "negate": False},
                           "max_iters": 2}, x=0, y=y)
lb2 = action(step("wait", {"ms": 100}), name="while-body", x=BX, y=y)
link(ep, loop2)                    # success path
link(ep_catch, loop2)              # recovery rejoins
link(loop2, lb2, "body"); link(lb2, loop2)
y += DY

# ── Loop · Until (text never matches → capped at 2) ──────────────────────────
loop3 = node(flow.N_LOOP, {"name": "loop-until", "mode": "until",
                           "condition": {"type": "text", "text": "NEVERMATCH",
                                         "case_sensitive": False, "fuzzy": True,
                                         "min_score": 0.95, "region": None,
                                         "negate": False},
                           "max_iters": 2}, x=0, y=y)
lb3 = action(step("wait", {"ms": 100}), name="until-body", x=BX, y=y)
link(loop2, loop3, "done")
link(loop3, lb3, "body"); link(lb3, loop3)
y += DY

# ── Loop · Forever (safety-capped at 2) ──────────────────────────────────────
loop4 = node(flow.N_LOOP, {"name": "loop-forever", "mode": "forever",
                           "max_iters": 2}, x=0, y=y)
lb4 = action(step("wait", {"ms": 100}), name="forever-body", x=BX, y=y)
link(loop3, loop4, "done")
link(loop4, lb4, "body"); link(lb4, loop4)
y += DY

# ── On-error = GOTO (a guaranteed-failing detect retries, then jumps) ────────
errgoto = action(step("wait_text", {"text": "WILL_NOT_BE_FOUND",
                                    "case_sensitive": False, "min_score": 0.9,
                                    "timeout_s": 1, "click": False,
                                    "button": "left", "clicks": 1,
                                    "region": None, "fuzzy": True}),
                 name="onerror-goto",
                 on_error={"mode": "goto", "retries": 1, "retry_delay_s": 0.3,
                           "goto_name": "recovery"}, x=0, y=y)
link(loop4, errgoto, "done")
y += DY

# ── Recovery node (target of the goto fallback) ──────────────────────────────
rec = action(step("text", {"text": "recovered", "speed_cps": 30}),
             name="recovery", x=0, y=y); y += DY
link(errgoto, rec)                 # success path (won't fire — it always fails)

# ── Go to (name-based, forward jump → wrap-up) ───────────────────────────────
goto1 = node(flow.N_GOTO, {"target_name": "wrap-up"}, x=0, y=y); y += DY
link(rec, goto1)

wrap = action(step("text", {"text": "all nodes visited", "speed_cps": 30}),
              name="wrap-up", x=0, y=y); y += DY
# (goto jumps here by name)

end = node(flow.N_END, x=0, y=y)
link(wrap, end)

g.meta["title"] = "Node feature test"
g.meta["note"] = ("Exercises every node type + setting. Safe to RUN over a "
                  "throwaway window (Notepad) — it performs real clicks/keys "
                  "and takes ~20s (several 1-2s detect timeouts). Bounded loops, "
                  "forward-only goto → it always terminates.")

# ── save ─────────────────────────────────────────────────────────────────────
out_lib = scripts_dir() / "Node feature test.json"
g.save(str(out_lib))
# ⚠ examples/, not next to this file. The copy used to land in the repo root,
# where a generated artefact with a space in its name sat beside the generator
# that makes it -- which is a confusing thing to meet first in a public source
# repository. examples/ is where the other flows already live.
local = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "examples", "Node feature test.json")
shutil.copy(str(out_lib), local)

# sanity: reload + count
g2 = flow.FlowGraph.load(str(out_lib))
n_act = sum(1 for n in g2.nodes.values() if n.type == flow.N_ACTION)
kinds = sorted({(n.data.get("step") or {}).get("kind") for n in g2.nodes.values()
                if n.type == flow.N_ACTION})
print(f"Saved: {out_lib}")
print(f"Nodes: {len(g2.nodes)}  edges: {len(g2.edges)}  action-nodes: {n_act}")
print(f"Action kinds: {kinds}")
print(f"Node types: {sorted({n.type for n in g2.nodes.values()})}")
