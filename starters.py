"""The flows a brand-new user finds in their library.

Downloading this and opening it gives you one green Start node on an empty
canvas and an empty Script Library. Someone who searched for "auto clicker"
now has to learn a node graph before they can click anything — which is a
long way to travel for the thing they thought they were downloading, and
most of them do not travel it.

So the library is seeded once, on first run, with flows that work with no
configuration at all: no coordinates to pick, no image to capture, nothing
to fill in. Press Play and something happens. The five that do the clicking
and typing are inside the free tier on purpose — this is the path to a first
success, and putting a paywall on it would defeat the point of having one.

⚠ The sixth, `PRO_EXAMPLE`, is the deliberate exception and it is not an
oversight. The paid half is otherwise only ever met by *refusal*, which
requires having already built a flow out of steps whose point you would have
to know in advance — so someone perfectly happy with the clicker never finds
out the rest of the product exists. That one is a readable example of it.
See `_pro_example`.

⚠ It currently runs, like everything else, because `entitlements.ENFORCED` is
False. Its frame is written to be true either way: it says which half those
steps belong to and never what they cost.

Anything counting "automations that arrive needing nothing filled in" counts
`free_starters()`, never `STARTERS` — the README says five because the
five clicking-and-typing ones are what that claim is about.

Each carries a Frame (the box drawn behind the graph) that says what it
does, how to stop it, and which node to double-click to change it. That is
the only documentation a first-time user reliably reads, because it is
already on the screen they are looking at.

⚠ Seeded exactly once, and never into a library that already has something
in it — see `seed`. Re-creating a starter somebody deleted would make them
undeletable, which is worse than never shipping them.
"""
from __future__ import annotations

from pathlib import Path
from typing import List

import flow

# Frames sit behind the nodes. Wide enough to hold the longest row (a Start
# and two action nodes), and tall enough that the note has its own space above
# them — text that runs under the nodes standing on it is worse than no text,
# because the reader cannot tell whether they have finished the sentence.
_FRAME_W, _FRAME_H = 780.0, 310.0
_FRAME_X, _FRAME_Y = -40.0, -150.0
_ROW_Y = 40.0
_COL = 250.0


def _autoclick(interval_ms: int, *, limit: int = 0) -> dict:
    """An Auto-Click step in the shape `AutoClickDialog.result_data()` writes.

    ⚠ Both `interval_ms` and `cps` are set. The executor reads the interval and
    the node's own label reads the rate, so writing only one gives a node that
    runs correctly and describes itself wrongly (or the reverse).
    """
    return {
        "kind": "autoclick",
        "data": {
            "button": "left",
            "click_type": "single",
            "hold_duration_ms": 100,
            "max_speed": False,
            "interval_ms": interval_ms,
            "cps": (1000.0 / interval_ms) if interval_ms else 0.0,
            # The unit the node labels itself in. Anything slower than one a
            # second is a period, not a rate: "0.0333333 CPS" is the same
            # number as "30s" and nobody can read it.
            "unit": "sec" if interval_ms >= 1000 else "cps",
            "use_fixed": False,
            "fixed_x": 0,
            "fixed_y": 0,
            "click_limit": limit,
            "stop_after_secs": 0.0,
            "pause_on_focus": False,
            "focus_window": "",
        },
        "delay_ms": 0.0,
    }


def _wait(ms: int) -> dict:
    return {"kind": "wait", "data": {"ms": ms}, "delay_ms": 0.0}


def _text(s: str) -> dict:
    return {"kind": "text", "data": {"text": s, "speed_cps": 12.0},
            "delay_ms": 0.0}


def _key(name: str, *, repeat: int = 1) -> dict:
    return {"kind": "key",
            "data": {"keys": [name], "mode": flow.KEY_TAP, "repeat": repeat},
            "delay_ms": 0.0}


def _build(note: str, steps: List[tuple]) -> flow.FlowGraph:
    """A frame, a Start, and one row of action nodes wired left to right.

    `steps` is [(node name, step dict), ...]. `note` is the frame's text: its
    first line becomes the frame's title bar, the rest its body.
    """
    g = flow.FlowGraph()
    g.add_node(flow.N_FRAME, {"text": note, "w": _FRAME_W, "h": _FRAME_H},
               _FRAME_X, _FRAME_Y)
    prev = g.add_node(flow.N_START, {}, 0.0, _ROW_Y)
    for i, (name, step) in enumerate(steps, start=1):
        node = g.add_node(flow.N_ACTION, {"name": name, "step": step},
                          i * _COL, _ROW_Y)
        g.add_edge(prev.id, node.id)
        prev = node
    return g


# The stop keys are the shipped defaults (settings.AppSettings). Naming them
# in the frame is the whole safety story for a clicker with no limit: the
# first thing anyone needs from an auto-clicker is a way to turn it off.
_STOP = "Esc stops it; F8 starts and stops it too."


def _clicker() -> flow.FlowGraph:
    return _build(
        "Auto-clicker\n"
        "Put the mouse where you want it to click, then press Play. Ten "
        f"clicks a second, wherever the pointer is. {_STOP}\n\n"
        "Double-click the node to change the speed or the button, or to pin it "
        "to one spot instead of following the pointer.",
        [("ten a second", _autoclick(100))])


def _clicker_limited() -> flow.FlowGraph:
    return _build(
        "Click 100 times, then stop\n"
        "The same clicker with a finish line, so you do not have to catch it. "
        f"{_STOP}\n\n"
        "Double-click the node and change “Stop after” to pick a "
        "different number of clicks, or to stop after a number of seconds.",
        [("100 clicks", _autoclick(100, limit=100))])


def _keep_awake() -> flow.FlowGraph:
    return _build(
        "Click once every 30 seconds\n"
        "Slow enough to keep a screen or a session from going idle without "
        f"getting in your way. {_STOP}\n\n"
        "Park the pointer somewhere harmless first — empty desktop, or the "
        "title bar of a window you do not mind focusing.",
        [("every 30 s", _autoclick(30_000))])


def _typer() -> flow.FlowGraph:
    return _build(
        "Type a block of text\n"
        "Press Play, then click into the box you want it typed into. You get "
        "three seconds to get there before it starts.\n\n"
        "Double-click the Type node to put your own text in — an address, "
        "a signature, a reply you send twenty times a day.",
        [("three seconds to get there", _wait(3000)),
         ("your text goes here",
          _text("Double-click this node and type what you want it to say."))])


def _key_repeat() -> flow.FlowGraph:
    return _build(
        "Press a key 50 times\n"
        "For stepping through a list, a form, or a folder of files. Press "
        "Play, then click the window you want the keys to go to — you "
        "get three seconds.\n\n"
        "Double-click the key node to change which key it presses and how "
        f"many times. {_STOP}",
        [("three seconds to get there", _wait(3000)),
         ("down arrow × 50", _key("down", repeat=50))])


def _pro_example() -> flow.FlowGraph:
    """The one starter built out of the paid half, and the reason it is here.

    Every other starter exists so a new user succeeds at something; this one
    exists so they find out what the other half of the product *is*. Nothing
    else in the app tells them — the paid half is otherwise met only by being
    refused, and being refused requires having already built a flow out of
    steps whose point you would have to know in advance.

    So: a flow that watches the screen and reacts to what it sees, sitting in
    the library, readable, editable, in plain language.

    ⚠ Today it also *runs*, because `entitlements.ENFORCED` is False. When
    that changes, pressing Play on it opens the upgrade dialog naming exactly
    the steps it uses — which is the designed path rather than an ambush. Its
    frame is therefore written to say which half these steps belong to and
    never what they cost, so it stays true across the switch. Do not put a
    price or the word "needs" back into it.

    ⚠ It watches and never touches. A Detect step with `click` off sends no
    input at all, so someone pressing Play on the example straight away gets
    a flow that reads the screen and stops, not one that clicks somewhere
    nobody asked for. That is also what makes it a clean demonstration: the
    free half is all the input and the paid half is all the watching, and
    this isolates the one being shown.
    """
    # ⚠ Written to be true whether or not `entitlements.ENFORCED` is on. The
    # first version said "this one needs Pro", which stopped being true the
    # moment the tier was switched off — and a flow that announces it is
    # blocked and then runs perfectly teaches the reader to disbelieve the
    # next thing the app tells them. Saying which half these steps *belong to*
    # is true in both worlds; saying what they cost is not.
    note = "\n".join((
        "Example — watching the screen",
        "Clicking and typing happen on a timer. Steps that look at the screen "
        "and decide what to do — here Wait for text, and the Loop around it — "
        "are what make Macronaut more than a clicker.",
        "",
        "This one watches for the word “Done” for two minutes and stops when "
        "it sees it. It only looks; it clicks nothing. Double-click it to "
        "watch for something else.",
    ))

    g = flow.FlowGraph()
    g.add_node(flow.N_FRAME, {"text": note, "w": 1030.0, "h": 560.0},
               _FRAME_X, _FRAME_Y)

    start = g.add_node(flow.N_START, {}, 0.0, _ROW_Y)
    # repeat_n x 20 at a six-second look each — two minutes, said in a way the
    # canvas can show. A `forever` loop would read as "and then what?".
    loop = g.add_node(flow.N_LOOP,
                      # ⚠ A PRO chip takes its width out of the title's, leaving
                      # ~132 px on a paid node -- so a name a free node wears
                      # comfortably comes back here as "keeplook...", which reads
                      # as a rendering fault rather than as a long name. Measured
                      # with the real platform plugin; the offscreen one has no
                      # font database and every metric it reports is fiction.
                      {"name": "keep at it",
                       "mode": "repeat_n", "count": 20, "max_iters": 100000},
                      _COL, _ROW_Y)
    watch = g.add_node(flow.N_ACTION,
                       {"name": "seen it?",
                        "step": {"kind": "wait_text",
                                 "data": {"text": "Done", "timeout_s": 6,
                                          "click": False, "fuzzy": True,
                                          "case_sensitive": False,
                                          "min_score": 0.5},
                                 "delay_ms": 0.0}},
                       2 * _COL, _ROW_Y)
    found = g.add_node(flow.N_END, {"name": "there it is"}, 3 * _COL, _ROW_Y)
    # Straight down from the Loop that feeds it, per the house layout rule:
    # the body is one row left to right, and an End drops out of the node
    # whose port sends it there.
    gave_up = g.add_node(flow.N_END, {"name": "two minutes, nothing"},
                         _COL, _ROW_Y + 260.0)

    g.add_edge(start.id, loop.id)
    # ⚠ A Loop has "body" and "done", never "out". An edge on the wrong port
    # still draws, so this would look wired while being a flow that could not
    # run — the worst thing to ship inside something whose whole job is to be
    # read and believed.
    g.add_edge(loop.id, watch.id, "body")
    g.add_edge(watch.id, found.id, "out")
    # A Detect's "error" port is where it goes when the thing never showed up.
    # Here that is not a failure, it is "not yet" — so it is the loop-back.
    g.add_edge(watch.id, loop.id, "error")
    g.add_edge(loop.id, gave_up.id, "done")
    return g


# The library sorts these by name, so this order is only the order they are
# written in. The names are what a first-time user reads, and each one is a
# job rather than a feature: nobody arrives wanting a "Loop node".
STARTERS = (
    ("Auto-clicker", _clicker),
    ("Click 100 times then stop", _clicker_limited),
    ("Click once every 30 seconds", _keep_awake),
    ("Type a block of text", _typer),
    ("Press a key 50 times", _key_repeat),
    ("Example - watch the screen", _pro_example),
)

# The one starter outside the free tier, named once so a test, the library and
# the README cannot disagree about which it is. Anything counting
# "automations you can run" counts `free_starters()`, never `STARTERS`.
PRO_EXAMPLE = "Example - watch the screen"


def free_starters() -> tuple:
    """The starter names that sit inside the free feature set.

    ⚠ A statement about the *policy*, not about what this build will run — with
    `entitlements.ENFORCED` off, every starter runs. It stays policy-shaped
    because it is what the page counts, and the page's claim is that five
    automations arrive needing nothing filled in, which is true either way.

    ⚠ Derived from `entitlements`, and deliberately not from
    `entitlements.check` — `check` consults the licence *and* the enforcement
    switch, so it would report every starter as free and quietly put a 6 on a
    page that should say 5.
    """
    import entitlements
    return tuple(name for name, build in STARTERS
                 if entitlements.runs_on_free(build()))


def build_all() -> dict:
    """{name: FlowGraph} — every starter, built. Used by the tests."""
    return {name: build() for name, build in STARTERS}


def seed(scripts_dir: Path) -> List[str]:
    """Write the starters into an *empty* library. Returns the names written.

    ⚠ Does nothing if the folder already holds a .json. Someone who has saved
    their own work should not find five files they did not make sitting next
    to it, and someone who deleted a starter should not get it back. The
    caller is responsible for only asking once (see `seed_once`).
    """
    try:
        scripts_dir = Path(scripts_dir)
        scripts_dir.mkdir(parents=True, exist_ok=True)
        if any(scripts_dir.glob("*.json")):
            return []
        written = []
        for name, build in STARTERS:
            build().save(str(scripts_dir / f"{name}.json"))
            written.append(name)
        return written
    except Exception:
        # A library that could not be seeded is a cosmetic loss. Nothing here
        # is allowed to stop the app from opening.
        return []


def add_missing(scripts_dir: Path) -> List[str]:
    """Write any starter this library does not already have. Returns the names.

    The manual counterpart to `seed`, and the two must not be collapsed into
    one function however similar they look. `seed` runs on its own and so may
    never touch a library with anything in it. This one runs because somebody
    pressed a button, which is the only thing that makes writing into an
    occupied folder acceptable.

    It exists for two people. The first is everyone who already had Macronaut
    before the starters did: their library is not empty, so the automatic path
    correctly skips them forever — and they are the people who have used it
    longest and are likeliest to want Pro, so the example flow reaching only
    brand-new installs is the wrong way round. The second is anyone who
    deleted a starter and wants it back, which until now was impossible on
    purpose and reads as the app having lost something.

    ⚠ Never overwrites. A file with a starter's name is assumed to be that
    starter after somebody edited it, and replacing their work with the
    factory copy would be the single worst thing this module could do.
    """
    try:
        scripts_dir = Path(scripts_dir)
        scripts_dir.mkdir(parents=True, exist_ok=True)
        written = []
        for name, build in STARTERS:
            path = scripts_dir / f"{name}.json"
            if path.exists():
                continue
            build().save(str(path))
            written.append(name)
        return written
    except Exception:
        return []


def seed_once(settings_manager, scripts_dir: Path) -> List[str]:
    """Seed on the first run that ever asks, and never again.

    The flag is recorded whether or not anything was written, so a user whose
    library was already occupied at 2.2.0 is not offered the starters every
    time they clear it out later.
    """
    try:
        if getattr(settings_manager.s, "starters_seeded", False):
            return []
        written = seed(scripts_dir)
        settings_manager.set("starters_seeded", True)
        return written
    except Exception:
        return []
