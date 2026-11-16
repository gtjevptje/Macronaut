"""
Macronaut flow engine — the visual automation graph model + interpreter.

This module is intentionally PURE PYTHON: it imports nothing from PyQt, pynput,
OpenCV or the OS so the whole control-flow brain can be unit-tested headlessly.
All real-world side effects (clicking, typing, image/text/pixel detection) are
delegated to an injected *executor* object that the interpreter calls back into.

Concepts
--------
A sequence is now a directed graph of nodes connected by edges, instead of a
flat list of steps. This is what powers the node-canvas editor and the new
control-flow features (If/Else, loops, goto/labels, on-error recovery).

Node types:
    start     – single entry point. Port: "out".
    action    – a unit of work (click/key/text/wait/wait_image/wait_text).
                Wraps a legacy SeqStep dict in data["step"].
                Ports: "out" (success), "error" (recovery path, optional).
    if        – evaluates a condition. Ports: "true", "false".
    loop      – repeat a body. Ports: "body" (enter), "done" (exit). The last
                node of the body connects BACK to the loop node (the visible
                cycle). Modes: repeat_n / while / until / forever, all capped by
                max_iters.
    set_var   – mutate a variable/counter. Port: "out".
    label     – a named jump target. Port: "out".
    goto      – jump to a label/node by id or name. (no functional out port)
    end       – terminate the run.
    comment   – ignored by the interpreter; documentation only. Port: "out".

Backward compatibility (Phase-2 item 8): saved files carry a "version" field.
A v1 file ({"version":1,"steps":[...]}) is migrated on load into an equivalent
linear graph, so old sequences keep working unchanged.
"""

from __future__ import annotations

import time
import json
import math
import os
import tempfile
import copy
from collections import namedtuple
from typing import Optional, List, Dict, Any, Callable

VERSION = 2

# ── Node type constants ───────────────────────────────────────────────────────
N_START   = "start"
N_ACTION  = "action"
N_IF      = "if"
N_LOOP    = "loop"
N_SETVAR  = "set_var"
N_LABEL   = "label"
N_GOTO    = "goto"
N_END     = "end"
N_COMMENT = "comment"
# A bend in a wire and nothing else: one input, one output, no settings, no
# runtime cost. It exists because a graph's wires have to double back on
# themselves — every loop in Macronaut has a backward edge — and a long return
# wire either cuts across the nodes it passes or bows so far out that you lose
# track of where it lands. A reroute lets the author say where the wire goes.
N_REROUTE = "reroute"
# A titled box drawn behind the graph that carries the nodes standing on it
# when it is dragged. Every other node-graph tool has one — Unreal comments,
# Blender frames, n8n sticky notes, ComfyUI groups — because a graph past a
# certain size stops being readable as a picture of itself and starts needing
# to be told what its regions are for.
#
# ⚠ Deliberately NOT a re-use of N_COMMENT, which is the obvious cheap route
# and is wrong. A comment node has an "out" port and is a live pass-through in
# FlowInterpreter._step, so a saved flow can have one wired into the middle of
# its chain. Redefining that type as a port-less box would cut such a flow in
# half on load. N_COMMENT stays exactly as it is; a *wireless* one is migrated
# into a frame on load (see migrate_loose_comments), which cannot change what
# any flow does because a comment with no wires never ran anything.
N_FRAME = "frame"

# A frame small enough to be useless is a frame you have to fix before you can
# use it. These are also what a brand-new one gets.
FRAME_MIN_W, FRAME_MIN_H = 180.0, 120.0
FRAME_DEF_W, FRAME_DEF_H = 420.0, 260.0

# The three action kinds that look for something and can genuinely come up
# empty. Everything else — a click, a keystroke, a wait — either happens or the
# run is already broken, so only these get a second, "didn't find it" output.
DETECT_KINDS = ("wait_image", "wait_text", "wait_pixel")

# How a key step presses. The first two begin and end inside the node; the last
# two are *state* — a Hold-down leaves the key down and the flow carries on, so
# W stays pressed while the next nodes click, detect and branch. That is the
# only way to express "run forward while doing something else", which is most
# of what movement in a game is.
KEY_TAP  = "tap"    # press, hold long enough to be seen, release
KEY_HOLD = "hold"   # press, wait hold_ms, release — all within this node
KEY_DOWN = "down"   # press and move on; released by a KEY_UP or at run end
KEY_UP   = "up"     # take keys back up (no keys captured = everything held)
KEY_MODES = (KEY_TAP, KEY_HOLD, KEY_DOWN, KEY_UP)


# ── Scrolling ────────────────────────────────────────────────────────────────
# A wheel notch is a detent, not a distance: how far one moves the page is the
# receiving app's business (Windows' own default is three lines). So a scroll
# step counts notches, which is the only unit that means the same thing to the
# user, to Windows and to the driver.
SCROLL_UP, SCROLL_DOWN, SCROLL_LEFT, SCROLL_RIGHT = "up", "down", "left", "right"
SCROLL_DIRECTIONS = (SCROLL_UP, SCROLL_DOWN, SCROLL_LEFT, SCROLL_RIGHT)
# Signs are pynput's, which are also Windows' and Interception's: +y is up,
# +x is right. Keeping one convention end to end is why nothing in the chain
# has to remember to flip anything.
_SCROLL_VECTORS = {SCROLL_UP:    (0, 1),  SCROLL_DOWN:  (0, -1),
                   SCROLL_RIGHT: (1, 0),  SCROLL_LEFT: (-1, 0)}
MAX_SCROLL_CPS = 200.0


def scroll_direction(d: dict) -> str:
    v = str(d.get("direction", SCROLL_DOWN)).lower().strip()
    return v if v in SCROLL_DIRECTIONS else SCROLL_DOWN


def scroll_notches(d: dict) -> int:
    return max(1, int(d.get("amount", 3) or 1))


def scroll_cps(d: dict) -> float:
    """Notches per second, or 0 for "as fast as the backend will send them".

    Same shape and same default as Type-text's speed, on purpose: it is the
    same question (how fast may this arrive before the receiver stops keeping
    up?) and an answer of 0 means the same thing in both places.
    """
    try:
        cps = float(d.get("speed_nps", 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(MAX_SCROLL_CPS, cps))


def scroll_vector(d: dict) -> tuple:
    """(dx, dy) for one notch of this step's direction."""
    return _SCROLL_VECTORS[scroll_direction(d)]


# ── Dragging ─────────────────────────────────────────────────────────────────
# Press, move while the button is down, release somewhere else. The one mouse
# gesture a click cannot express: `click` with `hold` presses and releases at
# the *same* point, so a swipe-to-confirm control — the reason this exists —
# sees a press that never went anywhere and does nothing at all.
#
# ⚠ The intermediate moves ARE the feature, not politeness. A receiver samples
# the pointer once a frame and decides from where it has *been*; teleporting the
# cursor from A to B between the press and the release gives it one sample at
# each end and no gesture in between, which it reads as a click. This is the
# same lesson as one-event-per-wheel-notch and paced typing, and it is the third
# place in this codebase where "Windows accepted it" was not "the target read
# it". Never collapse a drag into press → set position → release.
DRAG_HZ = 60.0            # one move per frame; more is sampled away unread
MIN_DRAG_MOVES = 2        # a start and an end is the least that is still a path
MAX_DRAG_MOVES = 900      # a bound, not a budget: 15 s of moves at DRAG_HZ
DEFAULT_DRAG_MS = 400
MAX_DRAG_MS = 60000
# Spent once after the press and once before the release. A button-down and the
# first movement landing in the same sample is read as a click at the *far* end
# rather than a drag towards it, so the two have to fall in different frames.
DRAG_SETTLE_MS = 80.0


def drag_duration_ms(d: dict) -> float:
    """How long the pointer spends travelling, in ms.

    Real time, like a key's hold_ms and unlike a pre-delay: "swipe over half a
    second" is a statement about the gesture the receiver has to recognise, so
    the run's speed multiplier does not stretch or shrink it.
    """
    try:
        ms = float((d or {}).get("duration_ms", DEFAULT_DRAG_MS))
    except (TypeError, ValueError):
        return float(DEFAULT_DRAG_MS)
    return max(0.0, min(float(MAX_DRAG_MS), ms))


def drag_moves(d: dict) -> int:
    """How many move events the travel is cut into.

    ⚠ Derived, never a field, and that is deliberate. One move per frame is not
    a compromise between smooth and cheap — it is the most the receiver can
    read, because it samples the pointer once a frame and takes what is there.
    Fewer is a gesture with gaps in it; more is thrown away unread. Exposing it
    would only let someone set it to 1 and rebuild the teleport this step kind
    exists to avoid.
    """
    n = int(round(drag_duration_ms(d) / 1000.0 * DRAG_HZ))
    return max(MIN_DRAG_MOVES, min(MAX_DRAG_MOVES, n))


def drag_total_ms(d: dict) -> float:
    """The gesture: press to release. The travel plus the two settles inside it.

    ⚠ NOT everything the node spends, which is what this used to claim. The
    engine is settle-press-settle-travel-settle-release — a *third* settle, on
    arrival, before the button goes down. That one belongs to getting there
    rather than to the gesture, and it is budgeted against the step's gap by
    `flow_exec._travel_budget`.

    Press-to-release is the span that matters here because it is what the
    recorder reproduces: it subtracts exactly these two settles from a captured
    swipe so the replayed gesture takes the time the real one did, which is
    frequently the thing the receiver is measuring.
    """
    return drag_duration_ms(d) + 2 * DRAG_SETTLE_MS


def drag_path(d: dict) -> List[tuple]:
    """The (x, y) points the pointer visits, start excluded, end included.

    Straight-line, evenly spaced. A curved or jittered path is what
    ``human_mode`` is for on the auto-click node; a swipe control wants the
    gesture it was drawn for, not a plausible-looking wander.
    """
    d = d or {}
    x0, y0 = int(d.get("x", 0) or 0), int(d.get("y", 0) or 0)
    x1, y1 = int(d.get("to_x", 0) or 0), int(d.get("to_y", 0) or 0)
    n = drag_moves(d)
    return [(int(round(x0 + (x1 - x0) * (i / n))),
             int(round(y0 + (y1 - y0) * (i / n))))
            for i in range(1, n + 1)]


# ── Pointer travel ─────────────────────────────────────────────────
# How the pointer GETS to the place a step clicks. Every click, move, scroll-at
# and detect-then-click used to set the cursor position in one assignment, which
# is a teleport: the pointer is at A in one sample and B in the next, and it was
# never anywhere in between.
#
# ⚠ This is the same lesson as `drag_path` above, one step earlier in the
# gesture. A receiver that samples the pointer once a frame learns where it is
# from where it has *been*, and plenty of them act on the journey rather than
# the destination: hover states never fire, a menu that opens on mouse-enter is
# not open by the time the click lands, and anything watching for a cursor that
# behaves like a hand sees the one motion a hand cannot make.
#
# The shape is a straight line walked with an ease-in-out (`smoothstep`), which
# is what a hand does — accelerate off the mark, coast, decelerate onto the
# target — with a shallow bow across it and a little tremor along the way.
#
# ⚠ There are two amplitudes, not two behaviours. Until 8 September 2026 a
# glide was either a mathematically exact line or the full human-mode wander,
# and the exact line is its own tell: a hand does not travel 900 px without
# deviating from the ruler by so much as a pixel. The *standard* amplitudes
# below are deliberately tiny — a bow of a percent and a half, capped at six
# pixels, and under a pixel of tremor — small enough that the path still goes
# where a straight one would (it must not sweep across a menu it was not aimed
# at) and large enough that no two glides between the same two points are the
# same. `human=True` opens both up to the wander that the auto-click node's
# human mode has always meant.
#
# Curvature and tremor still require an `rng`: with `rng=None` the path is the
# clean eased line, which is what keeps it deterministic and testable.
TRAVEL_HZ = 120.0          # moves per second; above a frame, cheap, and smooth
MIN_TRAVEL_MOVES = 2       # a start and an end is the least that is still a path
MAX_TRAVEL_MOVES = 400     # a bound, not a budget
DEFAULT_TRAVEL_PPS = 3000.0    # pixels per second: a brisk but visible glide
MIN_TRAVEL_PPS = 200.0
MAX_TRAVEL_PPS = 40000.0
MIN_TRAVEL_MS = 40.0       # a 30 px hop still has to be a movement, not a jump
MAX_TRAVEL_MS = 1500.0     # crossing a 4K desktop must not become a pause
# Under this, one move and be done: a two-pixel glide is six identical points,
# which costs a frame and looks like nothing at all.
TRAVEL_SNAP_PX = 4.0
# How far the bow can push the path off the straight line, as a fraction of the
# distance and in absolute pixels. Small: the click must still land where it
# was aimed, and a wide arc across a menu opens submenus on the way past.
# The standard pair is the one every glide gets; the human pair is what the
# opt-in widens them to.
TRAVEL_BOW_FRAC = 0.015
TRAVEL_BOW_MAX_PX = 6.0
HUMAN_BOW_FRAC = 0.06
HUMAN_BOW_MAX_PX = 28.0
# Tremor is applied ACROSS the direction of travel only, never along it. A hand
# wobbles sideways; what it does along the line is vary its speed, and adding
# noise there can make a step go backwards — a pointer that moves 2 px towards
# the target and then 1 px away from it is not more human, it is a cursor
# stuttering, and it costs the monotonic progress a receiver reads direction
# from. Sub-pixel by default, so after rounding most points sit exactly where
# the clean line put them and the rest are a pixel off it.
TRAVEL_TREMOR_PX = 0.8
HUMAN_TREMOR_PX = 2.0


def travel_pps(pps: Optional[float] = None) -> float:
    """The travel speed to use, clamped. None/0/nonsense → the default."""
    try:
        v = float(DEFAULT_TRAVEL_PPS if pps is None else pps)
    except (TypeError, ValueError):
        return float(DEFAULT_TRAVEL_PPS)
    if v <= 0:
        return float(DEFAULT_TRAVEL_PPS)
    return max(MIN_TRAVEL_PPS, min(MAX_TRAVEL_PPS, v))


def travel_duration_ms(distance_px: float, pps: Optional[float] = None) -> float:
    """How long a glide of this length takes, in ms.

    Real time, like a drag's travel and a key's hold: it is a statement about
    the motion the receiver has to see, so the run's speed multiplier does not
    stretch or shrink it.
    """
    try:
        dist = abs(float(distance_px))
    except (TypeError, ValueError):
        dist = 0.0
    ms = dist / travel_pps(pps) * 1000.0
    return max(MIN_TRAVEL_MS, min(MAX_TRAVEL_MS, ms))


def travel_within(natural_ms: float, gap_ms: float) -> float:
    """The glide takes `natural_ms`, but it has `gap_ms` to do it in.

    ⚠ The pointer glides at a fixed `mouse_travel_pps`; a hand does not. When a
    recorded gap is shorter than the glide the settings would make, the
    recording is *evidence* the pointer covered that distance in that time —
    the next click landed. Taking longer is not caution, it is slower than what
    actually happened, and the error is per-step, so it compounds over exactly
    the recordings people make. Measured 8 September 2026 on ten clicks 180 ms
    apart at scattered points: 1623 ms recorded, 2385 ms replayed.

    ⚠ The floor is `MIN_TRAVEL_MS`, and it is not a rounding detail. Squeezing
    a 900 px jump into the 4 ms between two fast clicks is a teleport with
    extra steps, which is the exact failure the glide exists to fix.

    A gap of zero is "no gap recorded", not "a gap of no time": a step with no
    delay has nothing to fit into and keeps its natural speed.
    """
    try:
        natural = float(natural_ms)
        gap = float(gap_ms)
    except (TypeError, ValueError):
        return float(MIN_TRAVEL_MS)
    if gap <= 0 or gap >= natural:
        return natural
    return max(float(MIN_TRAVEL_MS), gap)


def travel_moves(distance_px: float, pps: Optional[float] = None) -> int:
    """How many move events the glide is cut into. Derived, never a field —
    same reasoning as `drag_moves`: exposing it only lets someone set it to 1
    and rebuild the teleport this exists to avoid."""
    n = int(round(travel_duration_ms(distance_px, pps) / 1000.0 * TRAVEL_HZ))
    return max(MIN_TRAVEL_MOVES, min(MAX_TRAVEL_MOVES, n))


def travel_path(start, end, pps: Optional[float] = None, rng=None,
                human: bool = False) -> List[tuple]:
    """The (x, y) points the pointer visits on its way, start excluded, end
    included — same convention as `drag_path`, so both can be walked the same
    way by the engine.

    `rng` is an optional `random.Random`. Without one the path is a clean eased
    line and identical every time (which is what the tests read); with one it
    gets a shallow bow and a little sideways tremor. `human=True` widens both
    to the deliberate wander that the auto-click node's human mode means — the
    engine passes an rng on every glide and reserves the flag for that setting,
    so the difference between the two is amplitude, not presence.

    Neither may move where the last point lands. The end point is always
    exactly `end`: a click that misses by a pixel because a jitter landed on
    the final sample is a bug that would show up once in fifty runs.
    """
    x0, y0 = float(start[0]), float(start[1])
    x1, y1 = float(end[0]), float(end[1])
    dx, dy = x1 - x0, y1 - y0
    dist = (dx * dx + dy * dy) ** 0.5
    if dist <= TRAVEL_SNAP_PX:
        return [(int(round(x1)), int(round(y1)))]

    n = travel_moves(dist, pps)
    bow = 0.0
    tremor = 0.0
    if rng is not None:
        frac = HUMAN_BOW_FRAC if human else TRAVEL_BOW_FRAC
        cap = HUMAN_BOW_MAX_PX if human else TRAVEL_BOW_MAX_PX
        bow = rng.uniform(-1.0, 1.0) * min(dist * frac, cap)
        tremor = HUMAN_TREMOR_PX if human else TRAVEL_TREMOR_PX
    # Unit normal to the line — what the bow and the tremor both push along, so
    # progress towards the target stays monotonic however either lands.
    nx, ny = (-dy / dist, dx / dist)

    pts = []
    for i in range(1, n + 1):
        t = i / n
        e = t * t * (3.0 - 2.0 * t)          # smoothstep: ease in, ease out
        px = x0 + dx * e
        py = y0 + dy * e
        if i < n:
            off = 0.0
            if bow:
                # sin(pi*t): zero at both ends, widest in the middle, so the
                # path leaves and arrives on the line it was aimed along.
                off += bow * math.sin(math.pi * t)
            if tremor:
                # Faded out towards the target the same way, so the last few
                # samples before the click are on the line the click is on.
                off += rng.uniform(-tremor, tremor) * math.sin(math.pi * t)
            px += nx * off
            py += ny * off
        pts.append((int(round(px)), int(round(py))))
    pts[-1] = (int(round(x1)), int(round(y1)))
    return pts


# ── How typed text is delivered ──────────────────────────────────────────────
# The one question four releases of typing bugs kept asking under other names.
#
# A **character** goes out as a Unicode packet: it carries the codepoint itself,
# so it is layout-independent and arrives as itself in any ordinary window. It
# produces WM_CHAR and nothing else, so a target reading raw input or DirectInput
# — which is most games — never sees a word of it.
# A **key press** goes out as a scancode: a key *position*, which reaches both
# kinds of receiver, but leaves it to the receiver to decide which character that
# position makes. On AZERTY, a target running scancodes through its own US table
# reads `a` as `q` (that is what `key_positions` then answers).
#
# ⚠ Both are right, for different targets, and this used to be decided by the
# *input backend* — a global setting that also governs keys and clicks. So 2.0.17
# made pynput send key presses and broke ordinary windows; the revert made it
# send characters again and broke game chat. Neither is a bug in the other's
# target, and swapping the global default just moves who is broken. The question
# belongs to the step, because it is a property of what that step is typing into.
SEND_AUTO  = "auto"     # whatever the input backend is: pynput -> chars, else keys
SEND_CHARS = "chars"    # Unicode packets — any layout, ordinary windows only
SEND_KEYS  = "keys"     # real scancodes — games, at the mercy of their key table
SEND_MODES = (SEND_AUTO, SEND_CHARS, SEND_KEYS)


def send_as(d: dict) -> str:
    """How a Type-text step's data dict wants its text delivered.

    ⚠ The absence of the field is `SEND_AUTO`, which is exactly today's
    behaviour — the backend decides. Every flow saved before this existed
    therefore types the way it always did, and nothing needs migrating. Never
    default this to `SEND_CHARS` or `SEND_KEYS`: that would silently retarget
    every existing Type step at one kind of receiver.
    """
    v = str((d or {}).get("send_as", "") or "").lower().strip()
    return v if v in SEND_MODES else SEND_AUTO


# ── Mouse buttons ────────────────────────────────────────────────────────────
# ⚠ The two side buttons were recorded as LEFT CLICKS. `_on_click` mapped
# pynput's Button enum through a three-entry dict with `.get(button, "left")`,
# so a thumb button — Back and Forward in Windows, Mouse 4 and Mouse 5 to
# anyone who plays games with them — came out as a left click at the same
# coordinates and replayed as one. Not a missing feature: a wrong action,
# silently, in the place a right answer would have gone.
#
# Stored as "x1"/"x2" (Windows' XBUTTON1/XBUTTON2) and shown as Back/Forward,
# which is what the OS calls them and what they do in a browser or a folder.
BUTTONS = ("left", "right", "middle", "x1", "x2")
BUTTON_LABELS = {"left": "Left", "right": "Right", "middle": "Middle",
                 "x1": "Back", "x2": "Forward"}


def button_label(name: str) -> str:
    """What to call a button in a step summary or a control."""
    return BUTTON_LABELS.get(str(name or "left").lower(), "Left")


# ── Modifiers held over a pointer action ─────────────────────────────────────
# Ctrl-click to add to a selection, Shift-click to extend one, Alt-drag to
# copy, Shift-scroll to go sideways. All of them are one gesture, and until
# 8 September 2026 a click step could not express any of them.
#
# ⚠ This is the chord lesson from `recorder._flush_chord`, one step over. A
# recording captured the click and the modifier separately and put the
# modifier LAST, because a modifier only became a step when it came back up:
# a ctrl-click multi-select replayed as two plain clicks — the second undoing
# the first — followed by a stray ctrl held down for a second with nothing
# under it. The overlap was the entire content of the gesture and the saved
# JSON has no record that it happened.
#
# The absence of the field means no modifiers, which is exactly what every
# flow saved before this did, so nothing needs migrating.
MOD_ORDER = ("ctrl", "alt", "shift", "win")


def pointer_mods(d: dict) -> List[str]:
    """The modifiers a click/drag/scroll step is held under, canonically
    ordered so that two steps meaning the same thing read the same way."""
    raw = (d or {}).get("mods") or []
    if isinstance(raw, str):
        raw = [raw]
    # ⚠ A number or a bool here is not iterable, and this used to raise
    # TypeError straight out of the comprehension — out of `do_action`,
    # past no guard, and into `run()`, which ends the whole run on an
    # exception from an action. A malformed field has to cost its own
    # step, never the run. Every accessor beside this one (`key_mode`,
    # `scroll_notches`, `travel_pps`) was already written that way.
    try:
        have = {str(v).lower().strip() for v in raw}
    except TypeError:
        return []
    return [m for m in MOD_ORDER if m in have]


def key_mode(d: dict) -> str:
    """The press/release mode of a key step's data dict.

    Flows written before Hold-down existed carry no "mode" at all, and both of
    their behaviours have to survive untouched: a step with hold_ms held for
    that long, and a step without one tapped. So the absence of the field is
    read as the mode it used to imply rather than defaulted to a constant.
    """
    d = d or {}
    m = str(d.get("mode", "") or "").lower()
    if m in KEY_MODES:
        return m
    return KEY_HOLD if int(d.get("hold_ms", 0) or 0) > 0 else KEY_TAP

# Which output ports each node type exposes (used by the canvas + validation).
# N_ACTION is the exception: see FlowNode.ports().
NODE_PORTS = {
    N_START:   ["out"],
    N_ACTION:  ["out"],
    N_IF:      ["true", "false"],
    N_LOOP:    ["body", "done"],
    N_SETVAR:  ["out"],
    N_LABEL:   ["out"],
    N_GOTO:    [],
    N_END:     [],
    N_COMMENT: ["out"],
    N_REROUTE: ["out"],
    # No ports at all, and the only node type with none that isn't an ending.
    # A frame is not in the flow; it is drawn behind it.
    N_FRAME:   [],
}


# Node types that actually *do* something at runtime. Start, End, Label and
# Comment are scaffolding — a graph made only of those runs to completion
# without touching the mouse, the keyboard or the screen.
WORK_TYPES = (N_ACTION, N_IF, N_LOOP, N_SETVAR, N_GOTO)


# Node types that are drawing, not flow. They may sit in the middle of a wire
# (a reroute does) but they are not a step anyone is waiting for, so the
# timeline strip leaves them out — a lane of boxes that says "reroute" four
# times is describing the picture instead of the run.
ANNOTATION_TYPES = (N_REROUTE, N_COMMENT, N_FRAME)


def has_work(graph) -> bool:
    """True when the flow contains at least one node that does something.

    "Is there an action node?" is the wrong question and used to be the one
    asked: promoting a flow's only Detect node to an If/Else left zero action
    nodes, so Play refused to run a flow that was plainly full of work. A
    branch, a loop, a variable and a jump are all work too.
    """
    return any(n.type in WORK_TYPES for n in graph.nodes.values())


# The Start node carries this name by default so a Go to can jump back to the
# beginning without the user having to name it first — which is what almost
# every loop-back actually wants.
START_NAME = "start"


def action_kind(node: "FlowNode") -> str:
    """The action kind a node represents, '' for non-action nodes.

    Falls back to ``preset_kind``: the family picked in the palette, recorded on
    the node the moment it appears so the canvas can draw a Detect node as a
    Detect node while its editor is still open. It is dropped the instant a real
    step is saved, so it never survives into a saved flow.
    """
    if node.type != N_ACTION:
        return ""
    step = node.data.get("step") or {}
    return step.get("kind") or node.data.get("preset_kind") or ""


def delay_applies(node: "FlowNode") -> bool:
    """Returns True if a delay_before_ms on this node will actually fire at runtime."""
    if node.type == N_START:
        return False
    if node.type == N_ACTION:
        if action_kind(node) in ("click", "autoclick", "move"):
            return False
    return True


# ── How long a node takes ─────────────────────────────────────────────────────
# Three answers, and telling them apart is the whole point of drawing them.
#
#   EXACT     the node's own settings decide it. A Wait of 1.5 s takes 1.5 s,
#             on any machine, before it has ever been run.
#   MEASURED  nothing in the settings says, but this machine has timed it. A
#             sample, not a promise — the median of what it did last.
#   CEILING   the settings give an upper bound rather than a duration. A Detect
#             with a 10 s timeout will take *at most* that, and "is it about to
#             give up" is the question worth filling a bar against.
#   UNKNOWN   a Detect with no timeout, a while-loop, a node never yet run.
#             Anything drawn on a time axis after one of these is a guess, and
#             a timeline that hides that is worse than no timeline.
EXACT, MEASURED, CEILING, UNKNOWN = "exact", "measured", "ceiling", "unknown"
Estimate = namedtuple("Estimate", "ms source")

# The engine's per-key settle comes from settings.key_hold_ms; this mirrors its
# default rather than importing settings into the data model. Only ever used to
# predict the width of a progress bar, which caps short of full and snaps when
# the node really ends — so being a little wrong here costs nothing.
KEY_SETTLE_MS = 60.0
SAFE_TYPE_CPS = 33.0


# Nodes that take no time by construction. Not "we don't know" — we do know,
# and the difference matters: every flow has a Start and an End, so calling
# them unbounded would put "+ (unbounded)" on the timeline of every flow ever
# written and make the warning meaningless.
INSTANT_TYPES = (N_START, N_END, N_LABEL, N_COMMENT, N_GOTO, N_SETVAR,
                 N_REROUTE, N_FRAME)


def estimate(node, speed: float = 1.0,
             measured: Optional[dict] = None) -> "Estimate":
    """How long ``node`` should take, and how much that number is worth."""
    pre = 0.0
    if delay_applies(node):
        # The pre-delay is the one part of a node that speed_factor scales.
        pre = float(node.data.get("delay_before_ms", 0) or 0) * speed

    if node.type in INSTANT_TYPES:
        return Estimate(int(pre), EXACT)

    ms = _exact_step_ms(node, speed)
    if ms is not None:
        return Estimate(int(pre + ms), EXACT)

    m = (measured or {}).get(node.id)
    if m:
        return Estimate(int(pre + float(m)), MEASURED)

    ms = _ceiling_step_ms(node)
    if ms is not None:
        return Estimate(int(pre + ms), CEILING)

    return Estimate(int(pre), UNKNOWN if pre <= 0 else EXACT)


def expected_ms(node, speed: float = 1.0, measured: Optional[dict] = None) -> int:
    return estimate(node, speed, measured).ms


def _key_settle_ms() -> float:
    """How long the engine really holds each key, not what we assume.

    ⚠ `KEY_SETTLE_MS` is 60.0 and `settings.key_hold_ms` defaults to 60, so the
    two agreed until somebody changed the setting — and then a bar labelled
    EXACT was wrong by the ratio. Measured on a machine with it set to 5: a tap
    predicted 60 ms against 5 actual, a three-key combo 180 against 16, a
    five-times repeat **540 against 49**. A hold is unaffected, because
    `hold_ms` dominates and is real time either way.

    Same shape as the `text` branch below, which already asks `input_backends`
    for the real rate instead of trusting its own constant, and for the same
    reason: `flow` sits under settings and cannot import them at module level,
    so it asks lazily and falls back to the constant when there is nobody to
    ask.

    ⚠ Cheap where it matters. `_live_settings()` returns the app's registered
    in-memory manager (`main.set_active`), so this is a lookup rather than a
    file read on the path that draws one bar per node.
    """
    try:
        import input_backends
        v = getattr(input_backends._live_settings(), "key_hold_ms", None)
        if v is not None:
            return max(0.0, float(v))
    except Exception:
        pass
    return KEY_SETTLE_MS


def _exact_step_ms(node, speed: float) -> Optional[float]:
    """The step's own duration when its settings fully determine it."""
    if node.type != N_ACTION:
        return None
    step = node.data.get("step") or {}
    kind = step.get("kind") or ""
    d = step.get("data") or {}

    if kind == "wait":
        return float(d.get("ms", 0) or 0)

    if kind in ("key", "combo"):
        settle = _key_settle_ms()
        mode = key_mode(d)
        if mode in (KEY_DOWN, KEY_UP):
            # A state change. The press itself settles, but the node is over
            # long before anything a bar could show.
            return 0.0
        n = max(1, len(d.get("keys", [])))
        rep = max(1, int(d.get("repeat", 1) or 1))
        if mode == KEY_HOLD:
            per = (n - 1) * settle + float(d.get("hold_ms", 0) or 0)
        else:
            per = n * settle
        return per * rep + (rep - 1) * settle

    if kind == "text":
        cps = float(d.get("speed_cps", 0) or 0)
        if cps <= 0:
            try:
                import input_backends
                cps = float(input_backends.safe_type_cps())
            except Exception:
                cps = SAFE_TYPE_CPS
        n = len(str(d.get("text", "")))
        return (n / cps) * 1000.0 if cps > 0 else None

    if kind == "click":
        return float(d.get("hold_ms", 0) or 0) if d.get("hold") else 0.0

    if kind == "move":
        return 0.0

    if kind == "drag":
        # Exact, and worth a bar: a drag is the one mouse step long enough to
        # watch. All THREE settles — the gesture's two, plus the one spent on
        # arrival before the press. Counting `drag_total_ms` alone left every
        # drag's bar one settle short of what the node really takes.
        return drag_total_ms(d) + DRAG_SETTLE_MS

    if kind == "scroll":
        # Paced scrolling is exactly as long as it says it is; a burst at full
        # speed is one SendInput per notch and rounds to nothing.
        #
        # ⚠ (n-1)/cps, not n/cps — the mirror of the drag's third settle, and
        # found the same way: by comparing the prediction against what the
        # engine really spends. `_do_scroll` sends the first notch at once and
        # waits only *between* the rest, so n notches span n-1 intervals. The
        # error was one whole period whatever n is, which as a fraction is
        # 1/n: 9% on a twelve-notch flick and **100% on a two-notch scroll**.
        #
        # ⚠ Latent until the recorder started measuring a spin's real rate:
        # a recorded scroll used to carry `speed_nps: 0`, which takes the
        # burst branch below and returns 0, so no recorded scroll ever
        # reached this line.
        #
        # The engine is the one that is right. A rate is about the gaps
        # between notches, and a trailing pause after the last one buys
        # nothing — unlike a drag's final settle, which is what lets a
        # receiver see the button come back up.
        n, cps = scroll_notches(d), scroll_cps(d)
        return (max(0, n - 1) / cps) * 1000.0 if cps > 0 else 0.0

    if kind == "autoclick":
        lim = int(d.get("click_limit", 0) or 0)
        cps = float(d.get("cps", 10) or 10)
        # No limit means it runs until something stops it, which is not a
        # duration. speed_factor scales the click period, so it scales this.
        return (lim / cps) * 1000.0 * speed if lim and cps > 0 else None

    return None


def _ceiling_step_ms(node) -> Optional[float]:
    """An upper bound from the settings, for nodes that wait for the world."""
    if node.type != N_ACTION:
        return None
    step = node.data.get("step") or {}
    if step.get("kind") in DETECT_KINDS:
        t = float((step.get("data") or {}).get("timeout_s", 0) or 0)
        return t * 1000.0 if t > 0 else None
    return None


def linearise(graph) -> List[str]:
    """Node ids in the order a reader would walk them, Start first.

    Depth-first along the ports in the order a node lists them, which is the
    order the canvas draws them in, so the strip reads left-to-right the same
    way the graph does. Nodes no wire reaches are appended rather than dropped —
    an unreachable node is a thing the author wants to see, not hide.
    """
    order: List[str] = []
    seen = set()
    start = graph.start_node()
    stack = [start.id] if start is not None else []
    out_by_node: Dict[str, List[str]] = {}
    for nid, node in graph.nodes.items():
        ports = list(node.ports())
        outs = [e for e in graph.edges if e.src == nid]
        outs.sort(key=lambda e: ports.index(e.src_port)
                  if e.src_port in ports else len(ports))
        if outs:
            out_by_node[nid] = [e.dst for e in outs]
    while stack:
        nid = stack.pop()
        if nid in seen or nid not in graph.nodes:
            continue
        seen.add(nid)
        order.append(nid)
        stack.extend(reversed(out_by_node.get(nid, [])))
    order.extend(nid for nid in graph.nodes if nid not in seen)
    return order


def color_tuple(c) -> Optional[tuple]:
    """Parse a colour value (hex string or RGB list/tuple) to an (R, G, B) tuple."""
    if isinstance(c, (list, tuple)) and len(c) >= 3:
        return (int(c[0]), int(c[1]), int(c[2]))
    if isinstance(c, str):
        s = c.strip().lstrip("#")
        if len(s) == 6:
            try:
                return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
            except ValueError:
                return None
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  Data model
# ══════════════════════════════════════════════════════════════════════════════

class FlowNode:
    def __init__(self, nid: str, ntype: str, data: Optional[dict] = None,
                 x: float = 0.0, y: float = 0.0):
        self.id   = nid
        self.type = ntype
        self.data = data if data is not None else {}
        self.x    = float(x)
        self.y    = float(y)

    def ports(self) -> List[str]:
        """The output ports this node offers.

        Action nodes are kind-dependent: only a detect step gets an "error"
        port, because only a detect step has a failure a user can meaningfully
        wire around. The interpreter still honours an "error" edge on any node
        if an old flow has one — this decides what can be drawn, not what runs.
        """
        if self.type == N_ACTION:
            return ["out", "error"] if action_kind(self) in DETECT_KINDS else ["out"]
        return NODE_PORTS.get(self.type, ["out"])

    def to_dict(self) -> dict:
        return {"id": self.id, "type": self.type, "data": self.data,
                "x": self.x, "y": self.y}

    @classmethod
    def from_dict(cls, d: dict) -> "FlowNode":
        return cls(d["id"], d["type"], d.get("data", {}),
                   d.get("x", 0.0), d.get("y", 0.0))

    # Human-readable one-liner for logs and the canvas body text.
    def summary(self) -> str:
        return summarize_node(self)


class FlowEdge:
    def __init__(self, eid: str, src: str, dst: str, src_port: str = "out"):
        self.id       = eid
        self.src      = src
        self.dst      = dst
        self.src_port = src_port

    def to_dict(self) -> dict:
        return {"id": self.id, "src": self.src, "dst": self.dst,
                "src_port": self.src_port}

    @classmethod
    def from_dict(cls, d: dict) -> "FlowEdge":
        return cls(d["id"], d["src"], d["dst"], d.get("src_port", "out"))


class FlowGraph:
    """A serializable automation graph."""

    def __init__(self):
        self.nodes: Dict[str, FlowNode] = {}
        self.edges: List[FlowEdge] = []
        self.variables: Dict[str, Any] = {}   # name -> initial value
        self.meta: Dict[str, Any] = {}
        self._counter = 0

    # ── id helpers ────────────────────────────────────────────────────
    def new_node_id(self) -> str:
        while True:
            self._counter += 1
            nid = f"n{self._counter}"
            if nid not in self.nodes:
                return nid

    def new_edge_id(self) -> str:
        self._counter += 1
        return f"e{self._counter}"

    # ── mutation ──────────────────────────────────────────────────────
    def add_node(self, ntype: str, data: Optional[dict] = None,
                 x: float = 0.0, y: float = 0.0, nid: Optional[str] = None) -> FlowNode:
        nid = nid or self.new_node_id()
        node = FlowNode(nid, ntype, data, x, y)
        self.nodes[nid] = node
        return node

    def remove_node(self, nid: str):
        self.nodes.pop(nid, None)
        self.edges = [e for e in self.edges if e.src != nid and e.dst != nid]

    def add_edge(self, src: str, dst: str, src_port: str = "out") -> FlowEdge:
        # A given output port can only feed one target — replace any existing.
        self.edges = [e for e in self.edges
                      if not (e.src == src and e.src_port == src_port)]
        edge = FlowEdge(self.new_edge_id(), src, dst, src_port)
        self.edges.append(edge)
        return edge

    def remove_edge(self, eid: str):
        self.edges = [e for e in self.edges if e.id != eid]

    # ── queries ───────────────────────────────────────────────────────
    def out_edge(self, nid: str, port: str) -> Optional[FlowEdge]:
        for e in self.edges:
            if e.src == nid and e.src_port == port:
                return e
        return None

    def out_edges(self, nid: str) -> List[FlowEdge]:
        return [e for e in self.edges if e.src == nid]

    def in_edges(self, nid: str) -> List[FlowEdge]:
        return [e for e in self.edges if e.dst == nid]

    def start_node(self) -> Optional[FlowNode]:
        for n in self.nodes.values():
            if n.type == N_START:
                return n
        # Fall back to any node with no incoming edges.
        targets = {e.dst for e in self.edges}
        for n in self.nodes.values():
            if n.id not in targets:
                return n
        return next(iter(self.nodes.values()), None)

    def find_label(self, name: str) -> Optional[FlowNode]:
        for n in self.nodes.values():
            if n.type == N_LABEL and n.data.get("name", "") == name:
                return n
        return None

    def find_node_by_name(self, name: str) -> Optional[FlowNode]:
        """Find any node by its user-given name (used by name-based Goto)."""
        if not name:
            return None
        for n in self.nodes.values():
            if n.data.get("name", "") == name:
                return n
        return None

    # ── serialization ─────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "version": VERSION,
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "edges": [e.to_dict() for e in self.edges],
            "variables": dict(self.variables),
            "meta": dict(self.meta),
        }

    def clone(self) -> "FlowGraph":
        """A deep, fully-isolated copy. Handed to the background run worker so the
        live graph can keep being edited on the UI thread without racing the
        interpreter (which only reads nodes/edges)."""
        return FlowGraph.from_dict(copy.deepcopy(self.to_dict()))

    def save(self, path: str):
        """Write the flow, atomically.

        ⚠ `open(path, "w")` truncates the destination *before* a single byte is
        written, so anything that interrupts the write leaves the user with an
        empty file and no original to fall back on: a non-serialisable value in
        a node's data, a full disk, antivirus holding the handle, the machine
        losing power. This file **is** the work — flows are plain JSON the user
        keeps, there is no undo, and nothing else holds a copy.

        So it goes to a temporary file beside the target and is then moved over
        it. `os.replace` is atomic on Windows for a same-volume move, which is
        why the temporary is created in the destination's own directory rather
        than in %TEMP% — across volumes it degrades to a copy and the guarantee
        is gone.

        A failure now leaves the previous version of the flow exactly where it
        was, which is the whole point.
        """
        target = os.path.abspath(path)
        directory = os.path.dirname(target) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".flow-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, indent=2)
                # The bytes have to be with the OS before the rename, or a
                # crash can leave the rename done and the content not.
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
        except BaseException:
            # Never leave a .flow-*.tmp behind for the user to wonder about,
            # and never let the failure take the existing file with it.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @classmethod
    def from_dict(cls, payload: dict) -> "FlowGraph":
        version = payload.get("version", 1)
        # Old linear format → migrate. (Either explicit v1, or a "steps" list
        # with no "nodes" key.)
        if "nodes" not in payload or (version < 2 and "steps" in payload):
            return cls.migrate_linear(payload.get("steps", []))

        g = cls()
        for nd in payload.get("nodes", []):
            node = FlowNode.from_dict(nd)
            g.nodes[node.id] = node
        for ed in payload.get("edges", []):
            g.edges.append(FlowEdge.from_dict(ed))
        g.variables = dict(payload.get("variables", {}))
        g.meta = dict(payload.get("meta", {}))
        # Bump the id counter past anything already used so new ids never clash.
        mx = 0
        for nid in list(g.nodes.keys()) + [e.id for e in g.edges]:
            try:
                mx = max(mx, int("".join(ch for ch in nid if ch.isdigit()) or 0))
            except ValueError:
                pass
        g._counter = mx
        g.name_start_node()
        # Before prune_orphan_edges, which would otherwise be looking at a
        # comment node that is about to stop having an "out" port. (It is a
        # no-op either way — a loose comment has no wires to prune — but the
        # order that is obviously right should be the one written down.)
        migrate_loose_comments(g)
        prune_orphan_edges(g)
        return g

    def name_start_node(self):
        """Give the Start node its default name if nothing else claims it.

        Backfilled on load as well as on creation, so flows saved before this
        existed can also be a Go to target. Skipped when another node is already
        called "start", because find_node_by_name returns the first match and
        two of them would make the jump ambiguous."""
        start = next((n for n in self.nodes.values() if n.type == N_START), None)
        if start is None or start.data.get("name"):
            return
        if any(n.data.get("name") == START_NAME for n in self.nodes.values()):
            return
        start.data["name"] = START_NAME

    @classmethod
    def load(cls, path: str) -> "FlowGraph":
        """Read a flow, and say something useful when the file is damaged.

        ⚠ Every caller shows this exception to the user — a message box, or a
        tray notification for a launcher key. A raw `json` error reads
        "Expecting value: line 1 column 20 (char 19)", which tells somebody
        neither what is wrong nor that the problem is their *file* rather than
        the program. They are as likely to conclude Macronaut is broken.

        Worth doing now specifically: `save` was not atomic until 4 September
        2026, so an interrupted write could leave a half-written file, and
        anyone it happened to still has that file sitting in their library.
        """
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            name = os.path.basename(path)
            if not text.strip():
                raise ValueError(
                    f"“{name}” is empty. The file is still there, but there is "
                    "nothing in it to open.") from exc
            raise ValueError(
                f"“{name}” is not readable as a script — it looks like the file "
                f"was cut short while being saved ({exc.msg} at character "
                f"{exc.pos} of {len(text)}). Nothing has been changed; the "
                "file is still on disk if you want to look at it."
            ) from exc
        if not isinstance(payload, dict):
            # ⚠ This said only "does not contain a flow." until 4 September
            # 2026 — the one message on this path that named the file and then
            # stopped, while every other one says what is wrong and that the
            # file is untouched. Found by fuzzing the loader: 4,006 malformed
            # documents raised nothing unexpected, and the only complaint was
            # that this reply is nine words long. A top-level list is what you
            # get from pasting an array, or from a tool that exported the
            # *steps* rather than the flow around them.
            kind = type(payload).__name__
            raise ValueError(
                f"“{os.path.basename(path)}” is valid JSON, but the whole file "
                f"is a {kind} where a script has to be an object — it should "
                "start with a “{” and have \"nodes\" and \"edges\" inside it. "
                "If this is a list of steps from somewhere else, it is not a "
                "script file. Nothing has been changed.")
        try:
            return cls.from_dict(payload)
        except (TypeError, KeyError, ValueError, AttributeError) as exc:
            # ⚠ The other way a flow breaks, and the one this project invites:
            # the website tells people their macros are "plain JSON you can
            # read, edit, copy and delete yourself". Someone who takes that up
            # and mistypes a key used to get `KeyError: 'type'` in a message
            # box — which names nothing, suggests nothing, and does not say the
            # file is theirs to fix.
            detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            raise ValueError(
                f"“{os.path.basename(path)}” is valid JSON but is not shaped "
                f"like a script ({detail}). If you have edited it by hand, that "
                "is where to look — a node needs an \"id\" and a \"type\", and "
                "an edge needs \"src\" and \"dst\". Nothing has been changed."
            ) from exc

    # ── v1 → v2 migration (backward compatibility) ─────────────────────
    @classmethod
    def migrate_linear(cls, steps: List[dict]) -> "FlowGraph":
        """Convert a flat list of legacy SeqStep dicts into a linear graph."""
        g = cls()
        start = g.add_node(N_START, {"name": START_NAME}, x=0, y=0)
        prev_id, prev_port = start.id, "out"
        y = 120.0
        for st in steps:
            node = g.add_node(N_ACTION, {"step": st}, x=0, y=y)
            g.add_edge(prev_id, node.id, prev_port)
            prev_id, prev_port = node.id, "out"
            y += 120.0
        end = g.add_node(N_END, x=0, y=y)
        g.add_edge(prev_id, end.id, prev_port)
        g.meta["migrated_from"] = "v1"
        return g

    def is_linear(self) -> bool:
        """True if this is a simple top-to-bottom chain (no control flow)."""
        for n in self.nodes.values():
            if n.type in (N_IF, N_LOOP, N_GOTO, N_LABEL):
                return False
        for n in self.nodes.values():
            if len(self.out_edges(n.id)) > 1:
                return False
        return True

    def to_linear_steps(self) -> Optional[List[dict]]:
        """If linear, return the legacy step list (for v1-compatible export)."""
        if not self.is_linear():
            return None
        start = self.start_node()
        steps, cur, seen = [], start, set()
        while cur is not None and cur.id not in seen:
            seen.add(cur.id)
            if cur.type == N_ACTION and "step" in cur.data:
                steps.append(cur.data["step"])
            e = self.out_edge(cur.id, "out")
            cur = self.nodes.get(e.dst) if e else None
        return steps


# ══════════════════════════════════════════════════════════════════════════════
#  Node summaries (used by logs + canvas)
# ══════════════════════════════════════════════════════════════════════════════

def _cond_summary(cond: dict) -> str:
    if not cond:
        return "always"
    neg = "NOT " if cond.get("negate") else ""
    t = cond.get("type", "always")
    if t == "always":
        return "always"
    if t == "never":
        return "never"
    if t == "image":
        import os
        name = os.path.basename(cond.get("image_path", "")) or "image"
        return f"{neg}image «{name}»"
    if t == "text":
        txt = cond.get("text", "")
        return f'{neg}text "{txt[:20]}"'
    if t == "pixel":
        return f"{neg}pixel ({cond.get('x',0)},{cond.get('y',0)})={cond.get('color','?')}"
    if t == "var":
        return f"{neg}{cond.get('name','?')} {cond.get('op','==')} {cond.get('value','')}"
    return neg + t


def format_duration(ms) -> str:
    """A duration written the way a person would say it.

    600 -> "600 ms", 1500 -> "1.5 s", 90000 -> "1 min 30 s", 600000 -> "10 min".

    ⚠ Node labels printed the raw millisecond count, so a ten-minute wait read
    "Wait 600000 ms" — six digits to convert in your head, on the one node whose
    entire content *is* a duration. The unit has to follow the magnitude.

    Deliberately not what the editor's spin box shows: that is a field you type
    into, so it stays on one unit you can type back (ms, or seconds). This is
    prose, and prose can compound.
    """
    try:
        ms = float(ms or 0)
    except (TypeError, ValueError):
        return "0 ms"
    ms = max(0.0, ms)
    if ms < 1000:
        return f"{int(round(ms))} ms"
    secs = ms / 1000.0
    if secs < 60:
        # Trim to whole seconds when it is whole: "30 s", not "30.00 s".
        return f"{secs:.2f}".rstrip("0").rstrip(".") + " s"
    total = int(round(secs))
    if total < 3600:
        m, s = divmod(total, 60)
        return f"{m} min" if not s else f"{m} min {s} s"
    h, rem = divmod(total, 3600)
    m = rem // 60
    return f"{h} h" if not m else f"{h} h {m} min"


def summarize_node(node: FlowNode) -> str:
    d = node.data
    t = node.type
    if t == N_START:
        return "Start"
    if t == N_END:
        return "End"
    if t == N_COMMENT:
        return d.get("text", "Comment")
    if t == N_FRAME:
        return frame_title(node) or "Comment"
    if t == N_REROUTE:
        return ""
    if t == N_LABEL:
        return f"Label: {d.get('name','')}"
    if t == N_GOTO:
        return f"Go to ➜ {d.get('target_name') or d.get('target_label') or d.get('target','')}"
    if t == N_SETVAR:
        op = d.get("op", "set")
        sym = {"set": "=", "add": "+=", "sub": "-=", "mul": "*=", "div": "/="}.get(op, "=")
        return f"{d.get('name','?')} {sym} {d.get('value','')}"
    if t == N_IF:
        return f"If {_cond_summary(d.get('condition', {}))}"
    if t == N_LOOP:
        mode = d.get("mode", "repeat_n")
        if mode == "repeat_n":
            return f"Repeat {d.get('count', 1)}×"
        if mode == "forever":
            return "Loop forever"
        if mode == "while":
            return f"While {_cond_summary(d.get('condition', {}))}"
        if mode == "until":
            return f"Until {_cond_summary(d.get('condition', {}))}"
        return "Loop"
    if t == N_ACTION:
        step = d.get("step")
        # An action node exists on the canvas before its editor is confirmed.
        # Say so, rather than describing a step that isn't there yet.
        return _action_summary(step) if step else "not set yet"
    return t


def _mod_prefix(d: dict) -> str:
    """"Ctrl+Shift+" for a pointer step held under modifiers, else ""."""
    mods = pointer_mods(d)
    return "".join(m.capitalize() + "+" for m in mods)


def _action_summary(step: dict) -> str:
    """Mirror of SeqStep.description() but works on a plain dict."""
    kind = step.get("kind", "?")
    d = step.get("data", {})
    if kind == "autoclick":
        btn = button_label(d.get("button", "left"))
        if d.get("max_speed"):
            spd = "MAX"
        elif d.get("unit", "cps") == "sec":
            spd = f"{d.get('interval_ms', 1000) / 1000:g}s"
        else:
            spd = f"{d.get('cps', 10):g} CPS"
        lim = int(d.get("click_limit", 0) or 0)
        tail = f" · {lim}×" if lim else ""
        return f"Auto-click {btn} · {spd}{tail}"
    if kind == "click":
        btn = button_label(d.get("button", "left"))
        pre = "Double-" if d.get("clicks", 1) == 2 else ""
        return (f"{_mod_prefix(d)}{pre}{btn} click "
                f"({d.get('x',0)},{d.get('y',0)})")
    if kind == "move":
        return f"Move to ({d.get('x',0)},{d.get('y',0)})"
    if kind == "drag":
        btn = button_label(d.get("button", "left"))
        lead = "Drag" if btn == "Left" else f"{btn} drag"
        return (f"{_mod_prefix(d)}{lead} ({d.get('x',0)},{d.get('y',0)}) → "
                f"({d.get('to_x',0)},{d.get('to_y',0)}) · "
                f"{drag_duration_ms(d) / 1000:g} s")
    if kind in ("key", "combo"):
        combo = "+".join(k.upper() for k in d.get("keys", []))
        mode = key_mode(d)
        if mode == KEY_DOWN:
            return f"Hold down: {combo}"
        if mode == KEY_UP:
            return f"Release: {combo}" if combo else "Release: all held keys"
        s = "Key: " + combo
        hold_ms = int(d.get("hold_ms", 0) or 0)
        if mode == KEY_HOLD and hold_ms:
            s += f" · hold {hold_ms / 1000:g} s"
        rep = int(d.get("repeat", 1) or 1)
        if rep > 1:
            s += f" ×{rep}"
        return s
    if kind == "text":
        t = d.get("text", "")
        return f'Type: "{t[:24]}"' + ("…" if len(t) > 24 else "")
    if kind == "scroll":
        arrow = {SCROLL_UP: "↑", SCROLL_DOWN: "↓",
                 SCROLL_LEFT: "←", SCROLL_RIGHT: "→"}[scroll_direction(d)]
        s = f"{_mod_prefix(d)}Scroll {arrow} {scroll_notches(d)}"
        if not d.get("at_cursor", True):
            s += f" at ({d.get('x', 0)},{d.get('y', 0)})"
        # The rate is a recorded measurement now, not just a dial: a flick
        # and a slow drag of the wheel are different gestures and used to
        # render identically. 0 means "as fast as the backend will take
        # them", which is the default, so it prints nothing rather than
        # putting noise on every hand-built scroll.
        cps = scroll_cps(d)
        if cps > 0:
            rate = f"{cps:.1f}".rstrip("0").rstrip(".")
            s += f" · {rate}/s"
        return s
    if kind == "wait":
        return f"Wait {format_duration(d.get('ms', 0))}"
    # A Detect step is named for what it watches. "Wait for" was on all three
    # and said only what the whole Detect family already says.
    if kind == "wait_image":
        import os
        name = os.path.basename(d.get("image_path", "")) or "image"
        return (f"Click image «{name}»" if d.get("click") else f"Image «{name}»")
    if kind == "wait_text":
        t = d.get("text", "")
        return (f'Click text "{t[:18]}"' if d.get("click") else f'Text "{t[:18]}"')
    if kind == "wait_pixel":
        px = f"({d.get('x',0)},{d.get('y',0)})={d.get('color','?')}"
        return (f"Click pixel {px}" if d.get("click") else f"Pixel {px}")
    return kind


def node_icon(ntype: str) -> str:
    return {
        N_START: "▶", N_END: "⏹", N_ACTION: "⚙", N_IF: "❓",
        N_LOOP: "🔁", N_SETVAR: "🔢", N_LABEL: "🏷",
        # U+FE0F: without it Segoe UI supplies a small text arrow while every
        # other icon here falls through to the emoji font, so Go to alone came
        # out thin and short. The selector forces the same font as its peers.
        N_GOTO: "↪️",
        N_COMMENT: "💬", N_FRAME: "💬",
    }.get(ntype, "•")


# ══════════════════════════════════════════════════════════════════════════════
#  Port hygiene / Detect → If promotion
# ══════════════════════════════════════════════════════════════════════════════

def insert_reroute(graph: "FlowGraph", edge_id: str,
                   x: float, y: float) -> Optional["FlowNode"]:
    """Split the wire `edge_id` around a new reroute node at (x, y).

    The original edge is *reused* for the first half rather than deleted and
    re-added, so its id survives. Anything holding on to that id — a selected
    EdgeItem, a menu closure built a moment ago — keeps pointing at the wire the
    user clicked, which is the half they clicked on.
    """
    edge = next((e for e in graph.edges if e.id == edge_id), None)
    if edge is None or edge.dst not in graph.nodes:
        return None
    node = graph.add_node(N_REROUTE, {}, x, y)
    dst, edge.dst = edge.dst, node.id
    # Straight to the list: add_edge() would evict any other wire leaving this
    # port, and a brand-new node has none to evict.
    graph.edges.append(FlowEdge(graph.new_edge_id(), node.id, dst, "out"))
    return node


def dissolve_reroute(graph: "FlowGraph", nid: str) -> bool:
    """Remove a reroute and rejoin the wire that ran through it.

    Deleting it like any other node would leave the two ends dangling, and
    "delete the bend" plainly means "put the wire back", not "cut it".
    """
    node = graph.nodes.get(nid)
    if node is None or node.type != N_REROUTE:
        return False
    incoming = [e for e in graph.edges if e.dst == nid]
    onward = next((e for e in graph.edges if e.src == nid), None)
    if onward is not None:
        for e in incoming:
            e.dst = onward.dst
    graph.remove_node(nid)
    return True


# ══════════════════════════════════════════════════════════════════════════════
#  Frames (comment boxes)
# ══════════════════════════════════════════════════════════════════════════════

def frame_size(node: "FlowNode") -> tuple:
    """`(w, h)` for a frame, never below the minimum.

    Read through here rather than off node.data: the size is the one part of a
    frame a hand-edited file can make nonsense of, and a 0x0 frame is invisible
    and therefore unfixable — you cannot grab what you cannot see.
    """
    try:
        w = float(node.data.get("w", FRAME_DEF_W))
        h = float(node.data.get("h", FRAME_DEF_H))
    except (TypeError, ValueError):
        w, h = FRAME_DEF_W, FRAME_DEF_H
    return max(FRAME_MIN_W, w), max(FRAME_MIN_H, h)


def set_frame_size(node: "FlowNode", w: float, h: float) -> None:
    node.data["w"] = max(FRAME_MIN_W, float(w))
    node.data["h"] = max(FRAME_MIN_H, float(h))


def frame_title(node: "FlowNode") -> str:
    """The first line of a frame's text — what goes in its bar."""
    return (node.data.get("text", "") or "").split("\n", 1)[0].strip()


def frame_body(node: "FlowNode") -> str:
    """Everything after the first line, drawn inside the box."""
    parts = (node.data.get("text", "") or "").split("\n", 1)
    return parts[1].strip() if len(parts) > 1 else ""


def frames(graph: "FlowGraph") -> List["FlowNode"]:
    return [n for n in graph.nodes.values() if n.type == N_FRAME]


def migrate_loose_comments(graph: "FlowGraph") -> int:
    """Turn wire-less legacy `comment` nodes into frames. Returns how many.

    The comment type was in flow.py from the start, was never creatable from
    the UI, and rendered as an ordinary card that opened nothing when clicked.
    A frame is what it was always trying to be.

    ⚠ Only the ones with *no wires at all*. A comment node has an "out" port
    and FlowInterpreter walks straight through it, so one wired into the middle
    of a chain is load-bearing — converting that would cut the flow in half. A
    comment nothing connects to cannot be doing anything, so becoming a box it
    can never change what a flow does.
    """
    wired = {e.src for e in graph.edges} | {e.dst for e in graph.edges}
    moved = 0
    for node in list(graph.nodes.values()):
        if node.type != N_COMMENT or node.id in wired:
            continue
        node.type = N_FRAME
        node.data.setdefault("text", node.data.get("text", ""))
        node.data.setdefault("w", FRAME_DEF_W)
        node.data.setdefault("h", FRAME_DEF_H)
        moved += 1
    return moved


def prune_orphan_edges(graph: "FlowGraph") -> int:
    """Drop edges leaving a port their source node no longer offers.

    An action node's outputs depend on what it does, so re-editing a Detect step
    into a Click strips the "error" port out from under any wire hanging off it.
    Such an edge draws from nowhere but would still route at runtime, which is
    the worst of both. Runs on load too, for flows saved when every action node
    had an error port.
    """
    doomed = [e.id for e in graph.edges
              if (n := graph.nodes.get(e.src)) is not None
              and e.src_port not in n.ports()]
    for eid in doomed:
        graph.remove_edge(eid)
    return len(doomed)


# A detect step and an If condition describe the same check with the same keys,
# so promoting one to the other is a rename, not a translation. Click-related
# keys (button, clicks, offset_*) are deliberately absent: an If node cannot
# click, which is also why a clicking Detect node is never promoted.
_COND_TYPE_OF_KIND = {"wait_image": "image", "wait_text": "text",
                      "wait_pixel": "pixel"}
_COND_KEYS = {
    "image": ("image_path", "confidence", "timeout_s", "region"),
    "text":  ("text", "case_sensitive", "min_score", "fuzzy", "region",
              "store_var", "timeout_s"),
    "pixel": ("x", "y", "color", "tolerance", "timeout_s"),
}


def detect_condition(node: FlowNode) -> Optional[dict]:
    """The If-node condition equivalent to this node's detect step, or None.

    None when the node isn't a detect action, or when its step also *clicks*
    what it finds — that is an action an If node cannot perform, so converting
    would quietly throw it away.
    """
    ctype = _COND_TYPE_OF_KIND.get(action_kind(node))
    if ctype is None:
        return None
    d = (node.data.get("step") or {}).get("data") or {}
    if d.get("click"):
        return None
    cond = {"type": ctype}
    for k in _COND_KEYS[ctype]:
        if k in d:
            cond[k] = d[k]
    return cond


def convert_detect_to_if(graph: "FlowGraph", node_id: str) -> bool:
    """Turn a Detect action node into the If/Else node it is trying to be.

    Wiring both outputs of a Detect node *is* a two-way branch, and a two-way
    branch is what an If node is for. The node keeps its id, name, position and
    pre-delay, so every wire into it survives untouched; the two wires out of it
    are re-labelled found → true, not-found → false.

    Returns False (changing nothing) when the node cannot be promoted.
    """
    node = graph.nodes.get(node_id)
    if node is None:
        return False
    cond = detect_condition(node)
    if cond is None:
        return False
    data = {"condition": cond}
    for k in ("name", "delay_before_ms"):
        if node.data.get(k):
            data[k] = node.data[k]
    node.type = N_IF
    node.data = data
    for e in graph.edges:
        if e.src != node_id:
            continue
        if e.src_port == "out":
            e.src_port = "true"
        elif e.src_port == "error":
            e.src_port = "false"
    return True


# ══════════════════════════════════════════════════════════════════════════════
#  Copy / paste
# ══════════════════════════════════════════════════════════════════════════════

# Marks a clipboard payload as ours. Copy puts JSON on the system clipboard so a
# block of nodes survives being pasted into a second window (or a text file);
# this key is how paste tells that JSON apart from any other text sitting there.
CLIP_KEY = "macronaut_flow_clip"


def copy_subgraph(graph: FlowGraph, node_ids) -> dict:
    """A clipboard payload for `node_ids` plus every edge *between* them.

    Edges with one end outside the selection are dropped — the copy has nothing
    to attach them to. The Start node is never copied: a graph has exactly one
    entry point, and a second would silently change where a run begins.
    """
    ids = [nid for nid in node_ids
           if nid in graph.nodes and graph.nodes[nid].type != N_START]
    keep = set(ids)
    return {
        CLIP_KEY: VERSION,
        "nodes": [copy.deepcopy(graph.nodes[nid].to_dict()) for nid in ids],
        "edges": [e.to_dict() for e in graph.edges
                  if e.src in keep and e.dst in keep],
    }


def _copy_name(graph: FlowGraph, base: str) -> str:
    """`base` if free, else "base copy", "base copy 2", … — names have to stay
    unique or a name-based Go to would land on whichever copy came first."""
    taken = {n.data.get("name", "") for n in graph.nodes.values()}
    if base not in taken:
        return base
    stem = f"{base} copy"
    if stem not in taken:
        return stem
    i = 2
    while f"{stem} {i}" in taken:
        i += 1
    return f"{stem} {i}"


def paste_subgraph(graph: FlowGraph, payload: dict,
                   dx: float = 0.0, dy: float = 0.0) -> List[str]:
    """Add a copied block to `graph`, offset by (dx, dy). Returns the new ids.

    Anything that isn't one of our payloads is ignored and returns [] — paste
    reads the system clipboard, which can hold anything at all.
    """
    if not isinstance(payload, dict) or CLIP_KEY not in payload:
        return []
    remap, renamed, new_ids = {}, {}, []
    for nd in payload.get("nodes", []):
        ntype = nd.get("type", N_ACTION)
        if ntype == N_START:
            continue
        data = copy.deepcopy(nd.get("data", {}))
        old_name = data.get("name", "")
        if old_name:
            data["name"] = _copy_name(graph, old_name)
            renamed[old_name] = data["name"]
        node = graph.add_node(ntype, data,
                              float(nd.get("x", 0.0)) + dx,
                              float(nd.get("y", 0.0)) + dy)
        remap[nd.get("id")] = node.id
        new_ids.append(node.id)
    for ed in payload.get("edges", []):
        src, dst = remap.get(ed.get("src")), remap.get(ed.get("dst"))
        if src and dst:
            graph.add_edge(src, dst, ed.get("src_port", "out"))
    # A Go to inside the copied block that pointed at another copied node should
    # follow the copy. One that pointed outside still means the original.
    for nid in new_ids:
        node = graph.nodes[nid]
        if node.type != N_GOTO:
            continue
        tgt = node.data.get("target_name") or node.data.get("target_label")
        if tgt in renamed:
            node.data["target_name"] = renamed[tgt]
            node.data.pop("target_label", None)
    return new_ids


# ══════════════════════════════════════════════════════════════════════════════
#  Bulk edit ("overall settings")
# ══════════════════════════════════════════════════════════════════════════════

# Steps and conditions that actually look at the screen — the ones a timeout or
# a match confidence means anything for.
_DETECT_KINDS = DETECT_KINDS


def _conditions_of(node: FlowNode):
    """Every condition dict reachable from a node (If has one, Loop has one in
    while/until mode). Yielded live so callers can edit them in place."""
    cond = node.data.get("condition")
    if isinstance(cond, dict):
        yield cond


def bulk_apply(graph: FlowGraph, node_ids, ops: dict) -> int:
    """Apply `ops` to `node_ids`; returns how many nodes actually changed.

    Only the keys present in `ops` are touched, so a dialog can offer a dozen
    settings and change exactly the one the user ticked. Every op is a no-op on
    nodes it doesn't apply to (a click has no timeout), which is what makes
    "apply to everything" safe.
    """
    changed = 0
    for nid in node_ids:
        node = graph.nodes.get(nid)
        if node is None:
            continue
        before = json.dumps(node.data, sort_keys=True, default=str)

        if "delay_before_ms" in ops and delay_applies(node):
            ms = int(ops["delay_before_ms"])
            if ms > 0:
                node.data["delay_before_ms"] = ms
            else:
                node.data.pop("delay_before_ms", None)

        if "delay_add_ms" in ops and delay_applies(node):
            ms = int(node.data.get("delay_before_ms", 0) or 0) + int(ops["delay_add_ms"])
            if ms > 0:
                node.data["delay_before_ms"] = ms
            else:
                node.data.pop("delay_before_ms", None)

        step = node.data.get("step") or {}
        kind = step.get("kind", "")
        sdata = step.get("data") if isinstance(step.get("data"), dict) else None

        if "wait_ms" in ops and kind == "wait" and sdata is not None:
            sdata["ms"] = max(0, int(ops["wait_ms"]))

        if "wait_scale" in ops and kind == "wait" and sdata is not None:
            sdata["ms"] = max(0, int(round(int(sdata.get("ms", 0) or 0)
                                           * float(ops["wait_scale"]))))

        if "timeout_s" in ops:
            t = max(0, int(ops["timeout_s"]))
            if kind in _DETECT_KINDS and sdata is not None:
                sdata["timeout_s"] = t
            for cond in _conditions_of(node):
                if cond.get("type") in ("image", "text"):
                    cond["timeout_s"] = t

        if "confidence" in ops:
            c = float(ops["confidence"])
            if kind == "wait_image" and sdata is not None:
                sdata["confidence"] = c
            for cond in _conditions_of(node):
                if cond.get("type") == "image":
                    cond["confidence"] = c

        err_keys = ("error_retries", "error_retry_delay_s", "error_mode")
        if node.type == N_ACTION and any(k in ops for k in err_keys):
            oe = dict(node.data.get("on_error", {}))
            if "error_retries" in ops:
                oe["retries"] = max(0, int(ops["error_retries"]))
            if "error_retry_delay_s" in ops:
                oe["retry_delay_s"] = max(0.0, float(ops["error_retry_delay_s"]))
            if "error_mode" in ops:
                oe["mode"] = str(ops["error_mode"])
            node.data["on_error"] = oe

        if json.dumps(node.data, sort_keys=True, default=str) != before:
            changed += 1
    return changed


# ══════════════════════════════════════════════════════════════════════════════
#  Pure helpers: variable substitution + variable conditions
# ══════════════════════════════════════════════════════════════════════════════

def substitute_vars(text: Any, variables: Dict[str, Any]) -> Any:
    """Replace {name} tokens in a string with variable values."""
    if not isinstance(text, str) or "{" not in text:
        return text
    out = text
    for k, v in variables.items():
        out = out.replace("{" + str(k) + "}", str(v))
    return out


def _coerce_num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def eval_var_condition(cond: dict, variables: Dict[str, Any]) -> bool:
    name = cond.get("name", "")
    op   = cond.get("op", "==")
    raw  = substitute_vars(cond.get("value", ""), variables)
    cur  = variables.get(name, "")

    cn, rn = _coerce_num(cur), _coerce_num(raw)
    if op in ("<", ">", "<=", ">="):
        if cn is None or rn is None:
            return False
        return {"<": cn < rn, ">": cn > rn, "<=": cn <= rn, ">=": cn >= rn}[op]
    if op == "contains":
        return str(raw) in str(cur)
    # equality: prefer numeric comparison when both look numeric
    if cn is not None and rn is not None:
        eq = cn == rn
    else:
        eq = str(cur) == str(raw)
    return eq if op == "==" else (not eq)


def apply_set_var(node_data: dict, variables: Dict[str, Any]):
    """Mutate `variables` in place per a set_var node's data."""
    name = node_data.get("name", "")
    if not name:
        return
    op  = node_data.get("op", "set")
    val = substitute_vars(node_data.get("value", ""), variables)
    if op == "set":
        # keep ints tidy when possible
        n = _coerce_num(val)
        variables[name] = (int(n) if n is not None and float(n).is_integer()
                           else (n if n is not None else val))
        return
    cur = _coerce_num(variables.get(name, 0)) or 0.0
    other = _coerce_num(val)
    if other is None:
        return
    res = {"add": cur + other, "sub": cur - other,
           "mul": cur * other,
           "div": (cur / other if other != 0 else cur)}.get(op, cur)
    variables[name] = int(res) if float(res).is_integer() else res


# ══════════════════════════════════════════════════════════════════════════════
#  Interpreter
# ══════════════════════════════════════════════════════════════════════════════

class ExecutorProtocol:
    """
    Reference of the methods the interpreter expects from an executor.
    Real executor lives in flow_exec.py; tests use a mock.
    """
    def running(self) -> bool: ...
    def sleep(self, secs: float): ...
    def do_action(self, step: dict, variables: Dict[str, Any]) -> bool: ...
    def eval_sensor(self, cond: dict, variables: Dict[str, Any]) -> bool: ...


class FlowInterpreter:
    """Walks a FlowGraph, driving an executor and emitting a run log."""

    def __init__(self, graph: FlowGraph, executor,
                 on_log: Optional[Callable[[dict], None]] = None,
                 max_steps: int = 2_000_000):
        self.graph     = graph
        self.executor  = executor
        self.on_log    = on_log
        self.max_steps = max_steps
        self.vars: Dict[str, Any] = dict(graph.variables)
        self._loop_iter: Dict[str, int] = {}
        self.status = "idle"

    # ── logging ───────────────────────────────────────────────────────
    def _emit(self, kind: str, **fields):
        if self.on_log:
            evt = {"t": time.time(), "kind": kind}
            evt.update(fields)
            try:
                self.on_log(evt)
            except Exception:
                pass

    # ── condition evaluation ──────────────────────────────────────────
    def _eval(self, cond: dict) -> bool:
        cond = cond or {"type": "always"}
        t = cond.get("type", "always")
        if t == "always":
            res = True
        elif t == "never":
            res = False
        elif t == "var":
            res = eval_var_condition(cond, self.vars)
        else:
            # image / text / pixel → delegate to the executor (real I/O)
            try:
                res = bool(self.executor.eval_sensor(cond, self.vars))
            except Exception as exc:
                self._emit("error", msg=f"condition error: {exc}")
                res = False
        if cond.get("negate"):
            res = not res
        return res

    # ── port routing ──────────────────────────────────────────────────
    def _target(self, nid: str, port: str) -> Optional[str]:
        e = self.graph.out_edge(nid, port)
        return e.dst if e else None

    # ── per-node execution; returns next node id (or None to stop) ─────
    def _apply_pre_delay(self, node: FlowNode):
        """Per-node 'delay before' — runs on every non-click node that carries an
        explicit delay_before_ms. Nodes without the key (old saved files) keep
        their original timing."""
        if not delay_applies(node):
            return
        ms = node.data.get("delay_before_ms")
        if not ms:
            return
        try:
            secs = float(ms) / 1000.0
        except (TypeError, ValueError):
            return
        if secs > 0:
            sleep = getattr(self.executor, "sleep", None)
            if sleep is None:
                return  # no interruptible sleep — skip rather than block with time.sleep
            sleep(secs)

    def _step(self, node: FlowNode) -> Optional[str]:
        t = node.type
        self._emit("node_enter", id=node.id, type=t, desc=node.summary(),
                   name=node.data.get("name", ""))
        self._apply_pre_delay(node)

        if t in (N_START, N_LABEL, N_COMMENT, N_REROUTE):
            return self._target(node.id, "out")

        if t == N_END:
            return None

        if t == N_GOTO:
            tgt_name = node.data.get("target_name")
            if tgt_name:
                n = self.graph.find_node_by_name(tgt_name)
                dst = n.id if n else None
            else:
                # legacy: jump by label name, then by raw node id
                tgt = node.data.get("target_label")
                if tgt:
                    lab = self.graph.find_label(tgt)
                    dst = lab.id if lab else None
                else:
                    dst = node.data.get("target")
            self._emit("goto", target=dst)
            return dst

        if t == N_SETVAR:
            apply_set_var(node.data, self.vars)
            self._emit("set_var", name=node.data.get("name"),
                       value=self.vars.get(node.data.get("name")))
            return self._target(node.id, "out")

        if t == N_IF:
            res = self._eval(node.data.get("condition", {}))
            self._emit("branch", id=node.id, result=res,
                       port=("true" if res else "false"))
            return self._target(node.id, "true" if res else "false")

        if t == N_LOOP:
            return self._step_loop(node)

        if t == N_ACTION:
            return self._step_action(node)

        return self._target(node.id, "out")

    def _step_loop(self, node: FlowNode) -> Optional[str]:
        mode      = node.data.get("mode", "repeat_n")
        max_iters = int(node.data.get("max_iters", 100000) or 100000)
        it = self._loop_iter.get(node.id, 0)

        cont = it < max_iters
        if cont:
            if mode == "repeat_n":
                cont = it < int(node.data.get("count", 1) or 0)
            elif mode == "forever":
                cont = True
            elif mode == "while":
                cont = self._eval(node.data.get("condition", {}))
            elif mode == "until":
                cont = not self._eval(node.data.get("condition", {}))

        if cont:
            self._loop_iter[node.id] = it + 1
            self._emit("loop_iter", id=node.id, iter=it + 1, mode=mode)
            return self._target(node.id, "body")
        else:
            # Exited — reset so a future re-entry (outer loop / goto) starts fresh.
            self._loop_iter.pop(node.id, None)
            self._emit("loop_done", id=node.id, iters=it)
            return self._target(node.id, "done")

    def _step_action(self, node: FlowNode) -> Optional[str]:
        step    = node.data.get("step", {})
        on_err  = node.data.get("on_error", {})
        retries = int(on_err.get("retries", 0) or 0)
        delay_s = float(on_err.get("retry_delay_s", 0.5) or 0.0)

        ok = False
        attempt = 0
        while True:
            attempt += 1
            try:
                ok = bool(self.executor.do_action(step, self.vars))
            except Exception as exc:
                self._emit("error", id=node.id, msg=str(exc))
                ok = False
            self._emit("action", id=node.id, ok=ok, attempt=attempt,
                       name=node.data.get("name", ""), desc=node.summary())
            if ok or attempt > retries or not self.executor.running():
                break
            self._emit("retry", id=node.id, attempt=attempt, of=retries)
            self.executor.sleep(delay_s)

        if ok:
            return self._target(node.id, "out")

        # Failure routing. A connected "error" port always wins (visual try/catch).
        err_target = self._target(node.id, "error")
        if err_target is not None:
            self._emit("recover", id=node.id, via="error_port")
            return err_target

        mode = on_err.get("mode", "stop")
        if mode in ("skip", "continue"):
            self._emit("recover", id=node.id, via="skip")
            return self._target(node.id, "out")
        if mode == "goto":
            tgt = on_err.get("goto_name") or on_err.get("goto_label")
            n = (self.graph.find_node_by_name(tgt)
                 or self.graph.find_label(tgt)) if tgt else None
            self._emit("recover", id=node.id, via=f"goto:{tgt}")
            return n.id if n else None
        # mode == "stop" (default): abort the run.
        self.status = "error"
        self._emit("abort", id=node.id, reason="step failed")
        raise FlowAbort(f"Step failed: {node.summary()}")

    # ── main loop ─────────────────────────────────────────────────────
    def run(self) -> str:
        self.status = "running"
        self._emit("run_start", vars=dict(self.vars))
        cur = self.graph.start_node()
        steps = 0
        try:
            while cur is not None and self.executor.running():
                steps += 1
                if steps > self.max_steps:
                    self.status = "error"
                    self._emit("abort", reason="step limit exceeded "
                               "(possible infinite loop)")
                    break
                nxt = self._step(cur)
                cur = self.graph.nodes.get(nxt) if nxt else None
            if self.status == "running":
                self.status = "stopped" if not self.executor.running() else "done"
        except FlowAbort:
            self.status = "error"
        except Exception as exc:
            self.status = "error"
            self._emit("error", msg=str(exc))
        self._emit("run_end", status=self.status, steps=steps,
                   vars=dict(self.vars))
        return self.status


class FlowAbort(Exception):
    """Raised internally to unwind the interpreter on a fatal step failure."""
