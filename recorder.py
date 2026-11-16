"""Sequence recorder (captures live input) and sequence playback engine."""
import time
import json
import random
from typing import List, Optional, Tuple
from PySide6.QtCore import Qt, QObject, Signal, Slot, QThread
from pynput.mouse import Button, Controller as MouseCtrl
from pynput import mouse as _pm, keyboard as _pk

from keystrokes import parse_key
import flow
from flow import color_tuple as _color_tuple
import input_backends

try:
    import pyautogui
    pyautogui.FAILSAFE = False
    _PYAUTOGUI = True
except Exception:
    _PYAUTOGUI = False

try:
    import matcher
    _HAS_MATCHER = True
except Exception:
    _HAS_MATCHER = False

try:
    import ocr
    _HAS_OCR = True
except Exception:
    _HAS_OCR = False


# ── Step data model ───────────────────────────────────────────────────────────

class SeqStep:
    CLICK      = "click"
    MOVE       = "move"
    SCROLL     = "scroll"
    DRAG       = "drag"
    KEY        = "key"
    COMBO      = "combo"
    TEXT       = "text"
    WAIT       = "wait"
    WAIT_IMAGE  = "wait_image"
    WAIT_TEXT   = "wait_text"
    WAIT_PIXEL  = "wait_pixel"

    def __init__(self, kind: str, data: dict, delay_ms: float = 0.0):
        self.kind     = kind
        self.data     = data
        self.delay_ms = max(0.0, delay_ms)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "data": self.data, "delay_ms": self.delay_ms}

    @classmethod
    def from_dict(cls, d: dict) -> "SeqStep":
        return cls(d["kind"], d["data"], float(d.get("delay_ms", 0)))

    def _hold_tail(self) -> str:
        hold_ms = int(self.data.get("hold_ms", 0) or 0)
        return f" · hold {hold_ms / 1000:g} s" if hold_ms else ""

    def description(self) -> str:
        d = self.data
        if self.kind == self.CLICK:
            import flow
            # ⚠ The modifier prefix goes in front of EVERYTHING, not in
            # front of the button: "Double-" + "Ctrl+Shift+" + "Forward"
            # renders as "Double-Ctrl+Shift+Forward click", which reads as
            # a doubled Ctrl. `flow._action_summary` draws the same step on
            # the canvas and always had this right, so the two disagreed.
            btn    = flow.button_label(d.get("button", "left"))
            x, y   = d.get("x", 0), d.get("y", 0)
            clicks = d.get("clicks", 1)
            prefix = "Double-" if clicks == 2 else ""
            return f"{flow._mod_prefix(d)}{prefix}{btn} click at ({x}, {y})"
        if self.kind == self.MOVE:
            return f"Move to ({d.get('x',0)}, {d.get('y',0)})"
        if self.kind in (self.SCROLL, self.DRAG):
            # One description, one definition of what these steps say — flow.py
            # owns it, and this list must not drift from the canvas.
            import flow
            return flow._action_summary(self.to_dict())
        if self.kind == self.KEY:
            keys = d.get("keys", [])
            return "Key: " + "+".join(k.upper() for k in keys) + self._hold_tail()
        if self.kind == self.COMBO:
            keys = d.get("keys", [])
            return "Combo: " + "+".join(k.upper() for k in keys) + self._hold_tail()
        if self.kind == self.TEXT:
            t = d.get("text", "")
            preview = t[:28] + ("…" if len(t) > 28 else "")
            return f'Type: "{preview}"'
        if self.kind == self.WAIT:
            return f"Wait {d.get('ms',0)} ms"
        if self.kind == self.WAIT_IMAGE:
            import os
            name    = os.path.basename(d.get("image_path", "")) or "no image"
            conf    = d.get("confidence", 0.8)
            timeout = d.get("timeout_s", 0)
            t       = f"  timeout {timeout}s" if timeout else ""
            if d.get("click"):
                import flow as _f
                btn    = _f.button_label(d.get("button", "left"))
                clicks = d.get("clicks", 1)
                ox, oy = d.get("offset_x", 0), d.get("offset_y", 0)
                prefix = "Double-" if clicks == 2 else ""
                off    = f" +({ox},{oy})" if (ox or oy) else " center"
                return f'{prefix}{btn} click on image: {name}{off}  (conf {conf}){t}'
            return f'Image: {name}  (conf {conf}){t}'
        if self.kind == self.WAIT_TEXT:
            txt     = d.get("text", "")
            preview = (txt[:24] + "…") if len(txt) > 24 else txt
            timeout = d.get("timeout_s", 0)
            t       = f"  timeout {timeout}s" if timeout else ""
            if d.get("click"):
                import flow as _f
                btn    = _f.button_label(d.get("button", "left"))
                clicks = d.get("clicks", 1)
                prefix = "Double-" if clicks == 2 else ""
                return f'{prefix}{btn} click on text: "{preview}"{t}'
            return f'Text: "{preview}"{t}'
        if self.kind == self.WAIT_PIXEL:
            x, y    = d.get("x", 0), d.get("y", 0)
            color   = d.get("color", "?")
            timeout = d.get("timeout_s", 0)
            t       = f"  timeout {timeout}s" if timeout else ""
            if d.get("click"):
                return f'Click pixel ({x},{y})={color}{t}'
            return f'Pixel ({x},{y})={color}{t}'
        return self.kind

    def icon(self) -> str:
        return {
            self.CLICK:      "🖱",
            self.MOVE:       "↗",
            self.DRAG:       "🖱",
            self.KEY:        "⌨",
            self.COMBO:      "🔑",
            self.TEXT:       "📝",
            self.WAIT:       "⏱",
            self.WAIT_IMAGE:  "🔍",
            self.WAIT_TEXT:   "🔤",
            self.WAIT_PIXEL:  "🎯",
        }.get(self.kind, "?")


# ── Live recorder ─────────────────────────────────────────────────────────────

# Modifier key names (pynput emits left/right variants) → canonical base name.
def _button_name(button):
    """The name a step stores this pynput button under, or None for one that
    cannot be replayed.

    ⚠ Returning None matters more than the mapping does. This was
    `{left, right, middle}.get(button, "left")`, so a thumb button recorded as
    a LEFT CLICK at the same coordinates and replayed as one — a wrong action
    in the place a right answer would have gone, rather than a missing
    feature. Anything Macronaut cannot send is now dropped instead, which is
    the one thing left that is honest.

    Resolved by NAME rather than by identity, and it takes a plain string too.
    pynput's Button is a platform-specific enum — `Button.x1` exists on
    Windows and not everywhere — so matching members by identity is a
    dependency on which import of pynput built the event.
    """
    name = getattr(button, "name", None)
    if name is None and isinstance(button, str):
        name = button
    return name if name in flow.BUTTONS else None


_MODIFIERS = {
    "ctrl": "ctrl", "ctrl_l": "ctrl", "ctrl_r": "ctrl",
    "alt": "alt", "alt_l": "alt", "alt_r": "alt", "alt_gr": "alt",
    "shift": "shift", "shift_l": "shift", "shift_r": "shift",
    "cmd": "win", "cmd_l": "win", "cmd_r": "win", "win": "win",
}

# A key held at least this long is a deliberate hold (movement/sprint keys in
# games); below it: taps; at/above: deliberate game-style holds.
HOLD_MIN_MS = 300


class SequenceRecorder(QObject):
    """Listens to real user input and builds a SeqStep list."""

    step_recorded     = Signal(object)   # SeqStep
    recording_stopped = Signal()

    # Keys that stop recording and are NOT added to the sequence
    _STOP_KEYS = {"f8", "escape"}

    # Two clicks of the same button within this window at (nearly) the same spot
    # are merged into a single double-click step.
    _DBLCLICK_S  = 0.40
    _DBLCLICK_PX = 6
    # Further than a hand shakes while clicking, closer than any real drag. It
    # is deliberately larger than _DBLCLICK_PX: mistaking a wobbly click for a
    # drag would move the cursor away from the thing that was clicked, which is
    # a worse failure than recording a very short drag as a click.
    _DRAG_PX     = 12

    # A press held down at least this long (and not part of a double-click) is
    # captured as a press-and-hold rather than a discrete click.
    _HOLD_S = 0.35

    # ── Hovering ──────────────────────────────────────────────────────
    # ⚠ Until 8 September 2026 this class did not listen to mouse movement at
    # all — `on_click` and `on_scroll`, nothing else — so a recording was a
    # list of places the buttons went down and the journey between them did
    # not exist. `SeqStep.MOVE` was a step you could add by hand and the
    # engine could run, and nothing could ever record one.
    #
    # That is the gap behind "it does not do what I did". Open a menu by
    # hovering over it and then click an item inside: the recording holds only
    # the click on the item, so the replay clicks where the item *would* be
    # with the menu still shut. Nothing reports an error, because a click
    # cannot fail. A wheel step has the same hole from the other end — it is
    # recorded `at_cursor` (see `_on_scroll`), so a replay scrolls whatever the
    # pointer happens to be over, and what put it over the right pane was a
    # hover that was never written down.
    #
    # A pointer that stops for this long has been put somewhere deliberately.
    _DWELL_S = 0.40
    # ...and it only counts as somewhere ELSE if it is this far from where the
    # last recorded step already leaves the pointer. Without this, every click
    # records a hover on top of itself: the rest is noticed when the pointer
    # LEAVES, and the pointer always leaves the thing it just clicked.
    _REST_PX = 24

    def __init__(self):
        super().__init__()
        self._recording    = False
        self._t_last: float = 0.0
        self._steps: List[SeqStep] = []
        self._mouse_lst    = None
        self._kb_lst       = None
        self._mods_down: set = set()           # canonical modifiers held now
        self._down: dict = {}                  # button name -> (press_time, step)
        self._keys_down: dict = {}             # key_str -> {down_t, delay_ms, mods}
        self._mods_info: dict = {}             # modifier -> {down_t, used}
        self._chord: List[dict] = []           # released keys awaiting the rest
                                               # of their overlapping group
        self._last_move = None                 # (x, y, t) of the last movement
        self._rest = None                      # (x, y, arrived_t) pending hover
        self._last_pos = None                  # where the last step left it

    # How long before stop() to strip trailing steps (catches the stop-button click).
    _STOP_GRACE_S = 0.25

    def start(self):
        self._steps    = []
        self._t_last   = time.perf_counter()
        self._recording = True
        self._mods_down = set()
        self._down      = {}
        self._keys_down = {}
        self._mods_info = {}
        self._chord     = []
        self._last_move = None
        self._rest      = None
        self._last_pos  = None

        self._mouse_lst = _pm.Listener(on_click=self._on_click,
                                       on_scroll=self._on_scroll,
                                       on_move=self._on_move)
        self._kb_lst    = _pk.Listener(on_press=self._on_key_press,
                                       on_release=self._on_key_release)
        self._mouse_lst.start()
        self._kb_lst.start()

    def stop(self):
        self._recording = False
        t_stop = time.perf_counter()
        # A hover with nothing after it — the hand on its way to the Stop
        # button — needs no clearing here: a rest only ever becomes a step in
        # `_flush_rest`, every caller of which is a handler that has already
        # returned on `self._recording`, and `start()` resets it. Clearing it
        # here as well reads as load-bearing and is not; it was written, found
        # to survive its own mutation, and removed.
        if self._mouse_lst:
            self._mouse_lst.stop()
            self._mouse_lst = None
        if self._kb_lst:
            self._kb_lst.stop()
            self._kb_lst = None
        # Drop any steps that arrived in the last grace window — these are
        # almost certainly the click/key that triggered the Stop button itself.
        while self._steps and (t_stop - self._steps[-1]._recorded_at) < self._STOP_GRACE_S:
            self._steps.pop()
        # A key still held when Stop fires (e.g. mid-sprint) must not lose its
        # hold — flush it as a step now, measured up to this moment. This runs
        # AFTER the grace-window trim above so the freshly-flushed step (whose
        # timestamp IS "now") isn't immediately stripped as stop-button noise.
        # ⚠ Pop as we go. _finish_key_hold defers a key while any other is
        # still down, so leaving them in the dict would defer every one of
        # them and the chord would never be flushed — a sprint still held at
        # Stop would vanish rather than merely lose its overlap.
        for key_str, entry in sorted(self._keys_down.items(),
                                     key=lambda kv: kv[1]["down_t"]):
            self._keys_down.pop(key_str, None)
            self._finish_key_hold(key_str, entry, t_stop)
        self._keys_down = {}
        self._flush_chord()          # no-op unless a modifier path left one
        self.recording_stopped.emit()

    @property
    def steps(self) -> List[SeqStep]:
        return list(self._steps)

    @property
    def is_recording(self) -> bool:
        return self._recording

    # ── Internal callbacks ────────────────────────────────────────────
    def _delay_ms(self) -> float:
        now = time.perf_counter()
        ms  = (now - self._t_last) * 1000.0
        self._t_last = now
        return ms

    def _append(self, step: SeqStep):
        step._recorded_at = time.perf_counter()
        self._steps.append(step)
        if step.kind in (SeqStep.CLICK, SeqStep.MOVE):
            self._last_pos = (step.data.get("x", 0), step.data.get("y", 0))
        elif step.kind == SeqStep.DRAG:
            self._last_pos = (step.data.get("to_x", 0), step.data.get("to_y", 0))
        self.step_recorded.emit(step)

    # ── Modifiers held over a pointer action ──────────────────────────
    def _claim_mods(self) -> List[str]:
        """The modifiers held right now, marked as belonging to this step.

        ⚠ Marking them `used` is half the fix. A modifier only became a step
        of its own when it came back UP, and only if nothing had used it — so
        a ctrl-click recorded the click, and then, a second later, a separate
        "hold ctrl for 1.1 s" with nothing under it. The gesture came out as
        two plain clicks in the wrong relationship followed by a stray hold.
        Claiming them here says the modifier was part of this, and the
        release path leaves it alone. `flow.pointer_mods` orders them.
        """
        if not self._mods_down:
            return []
        for m in self._mods_down:
            info = self._mods_info.get(m)
            if info:
                info["used"] = True
        order = ("ctrl", "alt", "shift", "win")
        return [m for m in order if m in self._mods_down]

    # ── Hovering ──────────────────────────────────────────────────────
    def _on_move(self, x, y):
        """Notice that the pointer came to rest somewhere. See `_DWELL_S`.

        Runs on pynput's listener thread for every movement the mouse makes —
        which with a gaming mouse is a thousand a second — so it does nothing
        but two subtractions and a tuple assignment. A rest can only be seen
        from the far side of it: the pointer is standing still precisely when
        no callback is arriving, so the gap is measured when the next one does.
        """
        if not self._recording:
            return
        # A pointer moving with a button down is a drag, and a slow drag pauses
        # constantly. Those pauses are inside a gesture that is already being
        # recorded as one step; reading them as hovers puts a stray move
        # *after* the drag, aimed at somewhere the hand only passed through.
        if self._down:
            self._last_move = None
            return
        now = time.perf_counter()
        last = self._last_move
        self._last_move = (x, y, now)
        if last is None or now - last[2] < self._DWELL_S:
            return
        lx, ly, lt = last
        # The pointer always leaves the thing it just clicked, and that
        # departure is a rest by this measure. It is not a hover — the step
        # before it already puts the pointer there.
        if self._last_pos is not None:
            if (abs(lx - self._last_pos[0]) <= self._REST_PX
                    and abs(ly - self._last_pos[1]) <= self._REST_PX):
                return
        self._rest = (lx, ly, lt)

    def _flush_rest(self, at=None):
        """Write out a pending hover, as the step before whatever comes next.

        ⚠ Called at the top of every handler, BEFORE that handler measures its
        own delay, because the two delays have to divide the same stretch of
        time between them. The move carries the journey to the hover; the step
        that follows carries the dwell plus the journey on from it, which is
        what makes a replay wait there as long as the hand did.

        `at` is where the following step is about to act, if it acts anywhere:
        a hover is only a hover if the next thing happens somewhere else.
        """
        rest, self._rest = self._rest, None
        if rest is None:
            return
        rx, ry, rt = rest
        if at is not None and (abs(rx - at[0]) <= self._REST_PX
                               and abs(ry - at[1]) <= self._REST_PX):
            return
        self._append(SeqStep(SeqStep.MOVE, {"x": int(rx), "y": int(ry)},
                             max(0.0, (rt - self._t_last) * 1000.0)))
        self._t_last = rt

    def _on_click(self, x, y, button, pressed):
        if not self._recording:
            return
        # ⚠ `.get(button, "left")` used to be the whole of this, and a thumb
        # button therefore recorded as a LEFT CLICK at the same coordinates —
        # a wrong action in the place a right answer would have gone, rather
        # than a missing feature. `getattr` because a pynput without the X
        # buttons would raise on the attribute rather than simply not match.
        btn_name = _button_name(button)
        if btn_name is None:
            # A button Macronaut cannot send. Recording it as a left click,
            # which is what happened until now, is the one thing not to do.
            return
        now = time.perf_counter()

        # ── Release: promote the press into whatever it turned out to be ──
        if not pressed:
            info = self._down.pop(btn_name, None)
            if info:
                press_t, step = info
                held = now - press_t
                # A press that MOVED is a drag, and this is checked before the
                # hold promotion because a drag is nearly always held past
                # _HOLD_S too. Without it, recording a swipe produced a click at
                # the point the swipe started — a step that presses and releases
                # in one place and therefore does nothing at all to the control
                # the user was dragging.
                if (step in self._steps and step.kind == SeqStep.CLICK
                        and step.data.get("clicks", 1) == 1
                        and not step.data.get("hold")
                        and (abs(step.data.get("x", 0) - x) > self._DRAG_PX
                             or abs(step.data.get("y", 0) - y) > self._DRAG_PX)):
                    step.kind = SeqStep.DRAG
                    step.data["to_x"] = x
                    step.data["to_y"] = y
                    # What they actually did, not a default: the speed of a
                    # swipe is frequently the thing the receiver is measuring.
                    #
                    # ⚠ Minus the settles, because `duration_ms` is the time
                    # the pointer spends TRAVELLING — that is what the field
                    # means to `flow.drag_duration_ms`, to the editor's
                    # "Travel time" row, and to a drag somebody built by hand.
                    # `held` is the whole press-to-release, and the engine adds
                    # a settle either side of the travel (`flow.drag_total_ms`
                    # says so), so writing `held` straight in made every
                    # recorded swipe replay 160 ms slower than it was made —
                    # on a gesture whose speed is frequently the thing the
                    # receiver is measuring.
                    import flow as _flow
                    step.data["duration_ms"] = max(0, int(
                        held * 1000 - 2 * _flow.DRAG_SETTLE_MS))
                    step._recorded_at = now
                    # A drag leaves the pointer at its far end, not where it
                    # started. `_append` cannot know that — the promotion
                    # rewrites a step that is already in the list.
                    self._last_pos = (x, y)
                    self.step_recorded.emit(step)
                    self._t_last = now
                    return
                if (held >= self._HOLD_S and step in self._steps
                        and step.kind == SeqStep.CLICK
                        and step.data.get("clicks", 1) == 1):
                    step.data["hold"] = True
                    step.data["hold_ms"] = int(held * 1000)
                    step._recorded_at = now
                    self.step_recorded.emit(step)
                    self._t_last = now
            return

        # Anything the pointer paused over on the way here is its own step,
        # and it has to be written before this one measures its delay.
        self._flush_rest((x, y))

        # Merge a quick second press on the same spot into a double-click.
        if self._steps:
            last = self._steps[-1]
            if (last.kind == SeqStep.CLICK
                    and last.data.get("button") == btn_name
                    and last.data.get("clicks", 1) == 1
                    and not last.data.get("hold")
                    and (now - getattr(last, "_recorded_at", 0)) <= self._DBLCLICK_S
                    and abs(last.data.get("x", 0) - x) <= self._DBLCLICK_PX
                    and abs(last.data.get("y", 0) - y) <= self._DBLCLICK_PX):
                last.data["clicks"] = 2
                last._recorded_at = now
                self._down[btn_name] = (now, last)
                self.step_recorded.emit(last)
                self._t_last = now
                return

        delay = self._delay_ms()
        data = {"x": x, "y": y, "button": btn_name, "clicks": 1}
        mods = self._claim_mods()
        if mods:
            data["mods"] = mods
        step = SeqStep(SeqStep.CLICK, data, delay)
        self._append(step)
        self._down[btn_name] = (now, step)

    # Notches this close together were one spin of the wheel, not a decision.
    _SCROLL_MERGE_S = 0.4

    def _on_scroll(self, x, y, dx, dy):
        """Record wheel notches, merging a spin into one step.

        pynput reports one callback per detent, so a single flick of the wheel
        would otherwise become a dozen identical nodes — unreadable, and the
        thing that makes a recording not worth keeping. Consecutive notches in
        the same direction inside the merge window raise the existing step's
        count instead, which is the same shape as the double-click merge above.
        """
        if not self._recording:
            return
        if dx:
            direction, n = ("right" if dx > 0 else "left"), abs(int(dx))
        elif dy:
            direction, n = ("up" if dy > 0 else "down"), abs(int(dy))
        else:
            return
        n = max(1, n)
        # ⚠ A wheel step is recorded `at_cursor` (see below), so what the
        # replay scrolls is decided entirely by where the pointer is when it
        # gets here. If the hand moved over a pane and spun the wheel without
        # clicking anything, that movement IS the aim of the step.
        #
        # No `at` — deliberately, and it is the difference between this working
        # and not. Every other step says "I already go there, do not write a
        # move to the same place"; a wheel step says the opposite, because it
        # goes wherever the pointer was left and the hover is what left it
        # there. Passing (x, y) here suppresses the one move that matters.
        self._flush_rest()
        now = time.perf_counter()
        last = self._steps[-1] if self._steps else None
        if (last is not None and last.kind == SeqStep.SCROLL
                and last.data.get("direction") == direction
                and (now - getattr(last, "_recorded_at", 0)) <= self._SCROLL_MERGE_S):
            last.data["amount"] = int(last.data.get("amount", 1) or 1) + n
            # ⚠ The speed of the spin, which used to be thrown away: the step
            # was written `speed_nps: 0` — "as fast as the backend will send
            # them" — however long the flick really took. Twelve detents spun
            # over 360 ms replayed as twelve notches in 0.0 ms. That is a
            # timing fault and, worse, the batched-input fault this codebase
            # has now paid for three times: a receiver reads once a frame and
            # takes a bounded amount per pass, so one big roll arrives partly
            # missing. See `_do_scroll`.
            #
            # ⚠ (n-1)/elapsed, not n/elapsed. `_do_scroll` sends the first
            # notch immediately and waits only *between* the rest, so it spends
            # (n-1)/cps — and n detents span n-1 intervals. Counting the
            # notches rather than the gaps replays every spin slightly short.
            started = getattr(last, "_scroll_started", None)
            total = int(last.data.get("amount", 1) or 1)
            if started is not None and total > 1 and now > started:
                last.data["speed_nps"] = (total - 1) / (now - started)
            last._recorded_at = now
            self.step_recorded.emit(last)
            self._t_last = now
            return
        # at_cursor, deliberately: the wheel acts on whatever is under the
        # pointer, and by the time this fires the pointer is already there —
        # usually because the click step just before it put it there. Pinning
        # the recorded coordinates would make the replay fight the click.
        data = {"direction": direction, "amount": n,
                "speed_nps": 0, "at_cursor": True}
        mods = self._claim_mods()
        if mods:
            # Shift-scroll is sideways in most things that scroll, and
            # ctrl-scroll is zoom. Without the modifier the replay does
            # neither — it scrolls the list it was meant to zoom.
            data["mods"] = mods
        step = SeqStep(SeqStep.SCROLL, data, self._delay_ms())
        # When the first notch of a spin arrived, so a merge can work out how
        # fast the wheel was turning. One notch on its own is instantaneous and
        # keeps `speed_nps: 0`: there is no interval to measure, and inventing
        # a rate would pace a step with nothing to pace.
        step._scroll_started = now
        self._append(step)

    def _on_key_press(self, key):
        if not self._recording:
            return
        key_str = self._key_to_str(key)

        # A hover that ends in a keystroke is still a hover — the menu it
        # opened is what the keystroke is aimed at. Not for the stop keys:
        # those end the recording rather than belonging to it.
        if key_str not in self._STOP_KEYS:
            self._flush_rest()

        # Track modifiers so the next real key can be recorded as a combo, and
        # so a modifier held ALONE (no other key pressed while it's down) can
        # become its own hold step — e.g. holding Shift to sprint in a game.
        if key_str in _MODIFIERS:
            mod = _MODIFIERS[key_str]
            if mod not in self._mods_down:
                self._mods_down.add(mod)
                self._mods_info[mod] = {"down_t": time.perf_counter(), "used": False}
            return

        if key_str in self._STOP_KEYS:
            self.stop()
            return

        # OS auto-repeat re-fires on_press with no release in between while a
        # key is held down — ignore repeats; the single step is recorded once,
        # at release, so a long hold doesn't become a flood of tap steps.
        if key_str in self._keys_down:
            return

        delay = self._delay_ms()
        mods_snapshot = set(self._mods_down)
        for m in mods_snapshot:
            info = self._mods_info.get(m)
            if info:
                info["used"] = True
        self._keys_down[key_str] = {"down_t": time.perf_counter(),
                                    "delay_ms": delay,
                                    "mods": mods_snapshot}

    def _on_key_release(self, key):
        if not self._recording:
            return
        key_str = self._key_to_str(key)

        if key_str in _MODIFIERS:
            mod = _MODIFIERS[key_str]
            self._mods_down.discard(mod)
            info = self._mods_info.pop(mod, None)
            # Only a modifier held ALONE (never combined with another key
            # while down) and held long enough becomes its own hold step.
            if info and not info["used"]:
                now = time.perf_counter()
                held_ms = (now - info["down_t"]) * 1000.0
                if held_ms >= HOLD_MIN_MS:
                    delay = (info["down_t"] - self._t_last) * 1000.0
                    step = SeqStep(SeqStep.KEY,
                                   {"keys": [mod], "hold_ms": int(held_ms)}, delay)
                    self._append(step)
                    self._t_last = now  # anchor: next delay measures from here
            return

        entry = self._keys_down.pop(key_str, None)
        if entry is None:
            return
        self._finish_key_hold(key_str, entry, time.perf_counter())

    # ── Chords: keys that were down at the same time ──────────────────
    #
    # ⚠ A step is built at RELEASE, and for a long time it was built for one
    # key alone. Two keys held together — the diagonal in any WASD/ZQSD game,
    # "forward+left to hug the wall" — therefore came out as two sequential
    # nodes, and the engine replayed them as forward THEN left. The overlap,
    # which was the entire point of the input, was dropped on the floor: the
    # recorded JSON is the same whether the second key went down before or
    # after the first came up, so it cannot be recovered afterwards either.
    #
    # So a key whose release leaves another key still down is not emitted yet.
    # It waits in `_chord` until the last of the overlapping group is up, and
    # then the group is cut at every press and release and emitted as one step
    # per interval, each carrying the keys that were down right through it.
    # `flow_exec` presses every key of a multi-key step a settle apart and then
    # holds them all together, which is exactly this shape.
    #
    # Cutting on the edges rather than merging the whole group keeps the
    # staggered ends honest — you rarely press or release two keys on the same
    # millisecond, and rounding a 250 ms lead-in into the chord would send the
    # character off at the wrong angle for a quarter-second at both ends.

    def _finish_key_hold(self, key_str: str, entry: dict, now: float):
        """Retire one completed press-and-release. It is emitted immediately
        only if nothing else is still down; otherwise it joins the pending
        chord and the whole group is emitted when the last key comes up.
        Shared by _on_key_release and stop()'s mid-hold flush."""
        self._chord.append({"key": key_str, "down_t": entry["down_t"],
                            "up_t": now, "delay_ms": entry["delay_ms"],
                            "mods": entry["mods"]})
        # Anything still down was pressed before this release, so it overlapped
        # this key and belongs to the same chord. Wait for it.
        if not self._keys_down:
            self._flush_chord()

    def _flush_chord(self):
        """Emit the pending chord: one step per interval of constant down-set."""
        recs, self._chord = self._chord, []
        if not recs:
            return

        if len(recs) == 1:                      # the common case: one key alone
            r = recs[0]
            self._emit_keys([r["key"]], r["mods"],
                            (r["up_t"] - r["down_t"]) * 1000.0, r["delay_ms"])
            self._t_last = r["up_t"]
            return

        edges = sorted({r["down_t"] for r in recs} | {r["up_t"] for r in recs})
        # The first interval inherits the delay of the earliest press; the rest
        # are contiguous with it, so they follow with no gap. A dropped sliver
        # hands its duration to the next step's delay, so the group still takes
        # as long to replay as it took to record.
        gap_ms = min(recs, key=lambda r: r["down_t"])["delay_ms"]
        for t0, t1 in zip(edges, edges[1:]):
            held_ms = (t1 - t0) * 1000.0
            down = [r for r in recs if r["down_t"] <= t0 and r["up_t"] >= t1]
            if not down or held_ms < HOLD_MIN_MS:
                # A sliver: the few ms between two keys meant to go down
                # together, or a tail shorter than a deliberate hold.
                gap_ms += held_ms
                continue
            down.sort(key=lambda r: r["down_t"])
            mods = set().union(*(r["mods"] for r in down))
            self._emit_keys([r["key"] for r in down], mods, held_ms, gap_ms)
            gap_ms = 0.0
        self._t_last = edges[-1]

    def _emit_keys(self, keys: List[str], mods: set, held_ms: float,
                   delay_ms: float):
        """Append one key/combo step. `keys` is in press order."""
        if mods:
            keys = sorted(mods, key=lambda m: ("ctrl", "alt", "shift", "win").index(m)
                          if m in ("ctrl", "alt", "shift", "win") else 9) + keys
        data = {"keys": keys}
        kind = SeqStep.COMBO if len(keys) > 1 else SeqStep.KEY
        if held_ms >= HOLD_MIN_MS:
            data["hold_ms"] = int(held_ms)
        self._append(SeqStep(kind, data, delay_ms))

    @staticmethod
    def _key_to_str(key) -> str:
        try:
            if hasattr(key, "char") and key.char:
                return key.char.lower()
        except Exception:
            pass
        return str(key).replace("Key.", "").lower()


# ── Playback ──────────────────────────────────────────────────────────────────

class PlaybackWorker(QObject):
    """⚰ The pre-2.0 linear playback engine. Nothing in the app reaches it.

    Read this before spending time here. It replays a flat `List[SeqStep]`,
    which is what Macronaut was before the node canvas. The live engine is
    **`flow_exec.FlowWorker`**, and a fix belongs there.

    Its only constructor is `SequenceManager.play()` below, and nothing calls
    that: `main.py` imported `SequenceManager` and never used it, which is the
    kind of dead import that reads as "this is how playback works" to anyone
    following the imports. That import is gone as of 3 September 2026.

    ⚠ It is *not* a copy of the live path, so do not read it as documentation
    of one. It predates the selectable input backends, the per-step `send_as`
    choice, hold/release keys, drag and scroll — `_exec` below handles a
    smaller set of step kinds than `flow_exec.do_action` does today.

    Kept rather than deleted because this mount refuses deletes and because
    removing a public class from a published module is a decision rather than
    a tidy-up. `SeqStep` above is emphatically **live** — `main.py` uses it in
    44 places, and a flow's `data["step"]` is exactly `SeqStep.to_dict()`.
    """

    step_executed  = Signal(int)   # index of completed step
    status_changed = Signal(str)
    finished       = Signal()
    error_occurred = Signal(str)

    steps: List[SeqStep]  = []
    loop_count: int        = 1    # 0 = infinite
    speed_factor: float    = 1.0  # multiplier for delays (0.5 = 2× faster)
    blacklist: List[str]   = []   # key names that must never be sent

    # Image-recognition trigger (optional)
    wait_for_image: bool    = False
    image_path: str         = ""
    image_confidence: float = 0.8

    def __init__(self):
        super().__init__()
        self._mouse, self._mouse_backend, self._mouse_warning = \
            input_backends.make_mouse()
        self._kb, self._kb_backend, self._kb_warning = input_backends.make_keyboard()
        for _what, _warn in (("keyboard", self._kb_warning),
                             ("mouse", self._mouse_warning)):
            if _warn:
                # No run-log channel here that doesn't also trigger an error
                # dialog / stop playback (error_occurred is wired that way in
                # main.py) — this is a soft fallback notice, not a failure.
                print(f"{_what} backend: {_warn}")
        try:
            from settings import SettingsManager
            self._key_hold_s = max(0, int(getattr(SettingsManager(), "key_hold_ms", 60))) / 1000.0
        except Exception:
            self._key_hold_s = 0.06
        self._running = False
        self._blockset: set = set()

    def request_stop(self):
        self._running = False

    def _blocked(self, key_str: str) -> bool:
        return key_str.lower() in self._blockset

    def _image_present(self) -> bool:
        if not self.wait_for_image or not self.image_path:
            return True
        # If matching is unavailable we can't check — don't block playback.
        if not _HAS_MATCHER or not matcher.ENABLED:
            return True
        try:
            # Multi-scale + grayscale-aware match across the full virtual desktop.
            return matcher.present(self.image_path, self.image_confidence)
        except Exception as exc:
            self.error_occurred.emit(f"Image trigger error: {exc}")
            return False

    def _sleep(self, secs: float):
        """⚰ Dead with the rest of PlaybackWorker, but corrected anyway.

        It carried the same coarse-clock bug as `clicker._sleep` — see the long
        note there. Left as it was, it is a working example of the mistake
        sitting in the file most likely to be copied from when someone adds a
        pacing loop. Fixing two lines of code that never runs is cheaper than
        the next instance of the bug.
        """
        deadline = time.perf_counter() + max(0.0, secs)
        while self._running:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            time.sleep(min(0.01, remaining))

    def _click_physical(self, phys_x, phys_y, screenshot, btn_str="left", clicks=1):
        """
        Click at a point given in full virtual-desktop PHYSICAL pixel coords
        (as produced by an all_screens grab). Converts physical→logical and
        clicks via SendInput with absolute virtual-desktop coordinates, so it is
        correct on multi-monitor / mixed-DPI setups. Shared by the
        Wait-for-Image and Wait-for-Text steps.
        """
        import ctypes
        u32 = ctypes.windll.user32
        # SM_X/Y/CX/CYVIRTUALSCREEN = logical origin & size of the virtual desktop.
        vd_x = u32.GetSystemMetrics(76)
        vd_y = u32.GetSystemMetrics(77)
        vd_w = u32.GetSystemMetrics(78)
        vd_h = u32.GetSystemMetrics(79)
        # all_screens grab is PHYSICAL px; GetSystemMetrics is LOGICAL px.
        scale_x = screenshot.width  / vd_w
        scale_y = screenshot.height / vd_h
        lx = vd_x + phys_x / scale_x
        ly = vd_y + phys_y / scale_y
        norm_x = int((lx - vd_x) * 65535 / (vd_w - 1))
        norm_y = int((ly - vd_y) * 65535 / (vd_h - 1))

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                        ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                        ("time", ctypes.c_ulong),
                        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", ctypes.c_ulong), ("mi", MOUSEINPUT)]

        MOVE  = 0x0001 | 0x8000 | 0x4000  # MOVE|ABSOLUTE|VIRTUALDESK
        _DOWN = {"left": 0x0002, "right": 0x0008, "middle": 0x0020}
        _UP   = {"left": 0x0004, "right": 0x0010, "middle": 0x0040}
        d_flag = _DOWN.get(btn_str, 0x0002) | 0x8000 | 0x4000
        u_flag = _UP.get(btn_str,   0x0004) | 0x8000 | 0x4000

        def make_input(flags, x=norm_x, y=norm_y):
            inp = INPUT()
            inp.type = 0  # INPUT_MOUSE
            inp.mi.dx = x
            inp.mi.dy = y
            inp.mi.mouseData = 0
            inp.mi.dwFlags = flags
            inp.mi.time = 0
            inp.mi.dwExtraInfo = None
            return inp

        move_inp = make_input(MOVE)
        u32.SendInput(1, ctypes.byref(move_inp), ctypes.sizeof(INPUT))
        time.sleep(0.05)
        for _ in range(max(1, clicks)):
            down_inp = make_input(d_flag)
            up_inp   = make_input(u_flag)
            u32.SendInput(1, ctypes.byref(down_inp), ctypes.sizeof(INPUT))
            u32.SendInput(1, ctypes.byref(up_inp),   ctypes.sizeof(INPUT))

    def _exec(self, step: SeqStep, idx: int):
        # Skip disabled steps entirely, but keep the highlight advancing.
        if not step.data.get("enabled", True):
            self.step_executed.emit(idx)
            return

        # Pre-step delay
        delay = (step.delay_ms / 1000.0) * self.speed_factor
        if delay > 0:
            self._sleep(delay)

        if not self._running:
            return

        d = step.data
        if step.kind == SeqStep.CLICK:
            x, y   = d.get("x", 0), d.get("y", 0)
            clicks  = d.get("clicks", 1)
            btn_map = {"left": Button.left, "right": Button.right, "middle": Button.middle}
            btn     = btn_map.get(d.get("button", "left"), Button.left)
            self._mouse.position = (x, y)
            if d.get("hold"):
                self._mouse.press(btn)
                self._sleep(max(0, d.get("hold_ms", 1000)) / 1000.0)
                self._mouse.release(btn)
            else:
                self._mouse.click(btn, clicks)

        elif step.kind == SeqStep.MOVE:
            self._mouse.position = (d.get("x", 0), d.get("y", 0))

        elif step.kind in (SeqStep.KEY, SeqStep.COMBO):
            keys = d.get("keys", [])
            # Never send a blacklisted key (or combo containing one).
            if any(self._blocked(k) for k in keys):
                self.step_executed.emit(idx)
                return
            # hold_ms is gameplay-semantic real time ("hold W for 3s") and is
            # NOT scaled by speed_factor, unlike other delays in this player.
            hold_ms = int(d.get("hold_ms", 0) or 0)
            held = []
            try:
                for k in keys[:-1]:
                    key = parse_key(k)
                    if key:
                        self._kb.press(key)
                        held.append(key)
                        # let the game's per-frame poll see the modifier
                        self._sleep(self._key_hold_s)
                if keys:
                    last = parse_key(keys[-1])
                    if last:
                        self._kb.press(last)
                        held.append(last)
                        if hold_ms > 0:
                            self._sleep(hold_ms / 1000.0)
                        else:
                            # a 0ms tap falls between the game's frame polls;
                            # hold so it is seen (settings.key_hold_ms)
                            self._sleep(self._key_hold_s)
            finally:
                # Guaranteed release even if the hold is interrupted mid-way —
                # a stuck-down key in a game is the worst failure mode here.
                for k in reversed(held):
                    self._kb.release(k)

        elif step.kind == SeqStep.TEXT:
            try:
                for ch in d.get("text", ""):
                    if self._blocked(ch):
                        continue
                    self._kb.type(ch)
            except Exception:
                pass

        elif step.kind == SeqStep.WAIT:
            self._sleep(d.get("ms", 0) / 1000.0)

        elif step.kind == SeqStep.WAIT_IMAGE:
            if _PYAUTOGUI:
                from PIL import ImageGrab, Image
                image_path = d.get("image_path", "")
                confidence = d.get("confidence", 0.8)
                timeout_s  = d.get("timeout_s", 0)
                do_click   = d.get("click", False)
                deadline   = (time.monotonic() + timeout_s) if timeout_s > 0 else None
                while self._running:
                    try:
                        screenshot = ImageGrab.grab(all_screens=True).convert("RGB")
                        if _HAS_MATCHER and matcher.ENABLED:
                            # Multi-scale + grayscale-aware match (box in physical px).
                            box = matcher.find(image_path, confidence, screenshot=screenshot)
                        else:
                            needle = Image.open(image_path).convert("RGB")
                            box    = pyautogui.locate(needle, screenshot, confidence=confidence)
                        if box is not None:
                            if do_click:
                                phys_x = box.left + box.width  // 2 + d.get("offset_x", 0)
                                phys_y = box.top  + box.height // 2 + d.get("offset_y", 0)
                                self._click_physical(phys_x, phys_y, screenshot,
                                                     d.get("button", "left"),
                                                     d.get("clicks", 1))
                            break
                    except pyautogui.ImageNotFoundException:
                        pass
                    except Exception as exc:
                        self.error_occurred.emit(f"Image step error: {exc}")
                        break
                    if deadline and time.monotonic() >= deadline:
                        break
                    self._sleep(0.3)

        elif step.kind == SeqStep.WAIT_TEXT:
            if _HAS_OCR and ocr.available():
                from PIL import ImageGrab
                engine = ocr.get_engine()
                target     = d.get("text", "")
                case_sens  = d.get("case_sensitive", False)
                min_score  = d.get("min_score", 0.5)
                timeout_s  = d.get("timeout_s", 0)
                do_click   = d.get("click", False)
                region_logical = d.get("region")   # per-step search area (logical coords)
                fuzzy      = d.get("fuzzy", True)   # tolerate OCR misreads
                deadline   = (time.monotonic() + timeout_s) if timeout_s > 0 else None
                while self._running and target:
                    try:
                        screenshot = ImageGrab.grab(all_screens=True).convert("RGB")
                        region_phys = ocr.to_physical_region(region_logical, screenshot)
                        tm = engine.find_text(target, screenshot, region=region_phys,
                                              case_sensitive=case_sens, min_score=min_score,
                                              fuzzy=fuzzy)
                        if tm is not None:
                            if do_click:
                                phys_x = tm.left + tm.width  // 2 + d.get("offset_x", 0)
                                phys_y = tm.top  + tm.height // 2 + d.get("offset_y", 0)
                                self._click_physical(phys_x, phys_y, screenshot,
                                                     d.get("button", "left"),
                                                     d.get("clicks", 1))
                            break
                    except Exception as exc:
                        self.error_occurred.emit(f"Text step error: {exc}")
                        break
                    if deadline and time.monotonic() >= deadline:
                        break
                    self._sleep(0.5)   # OCR is heavier than image matching

        elif step.kind == SeqStep.WAIT_PIXEL:
            try:
                from PIL import ImageGrab
                timeout_s = d.get("timeout_s", 0)
                do_click  = d.get("click", False)
                deadline  = (time.monotonic() + timeout_s) if timeout_s > 0 else None
                tol       = int(d.get("tolerance", 10))
                px, py    = int(d.get("x", 0)), int(d.get("y", 0))
                want      = _color_tuple(d.get("color"))
                while self._running and want is not None:
                    try:
                        screenshot = ImageGrab.grab(all_screens=True).convert("RGB")
                        if 0 <= px < screenshot.width and 0 <= py < screenshot.height:
                            r, g, b = screenshot.getpixel((px, py))
                            if (abs(r - want[0]) <= tol and
                                    abs(g - want[1]) <= tol and
                                    abs(b - want[2]) <= tol):
                                if do_click:
                                    self._click_physical(px, py, screenshot,
                                                         d.get("button", "left"),
                                                         d.get("clicks", 1))
                                break
                    except Exception:
                        pass
                    if deadline and time.monotonic() >= deadline:
                        break
                    self._sleep(0.2)
            except Exception as exc:
                self.error_occurred.emit(f"Pixel step error: {exc}")

        self.step_executed.emit(idx)

    @Slot()
    def run(self):
        self._running = True
        self._blockset = {b.lower() for b in (self.blacklist or [])}
        self.status_changed.emit("running")
        try:
            # Wait for image trigger before starting the sequence
            if self.wait_for_image and self.image_path:
                self.status_changed.emit("waiting for image…")
                while self._running and not self._image_present():
                    self._sleep(0.3)
                if not self._running:
                    return
                self.status_changed.emit("running")

            iteration = 0
            while self._running:
                for i, step in enumerate(self.steps):
                    if not self._running:
                        break
                    self._exec(step, i)
                iteration += 1
                if self.loop_count > 0 and iteration >= self.loop_count:
                    break
        except Exception as exc:
            self.error_occurred.emit(str(exc))
        finally:
            self.status_changed.emit("idle")
            self.finished.emit()


# ── Sequence manager ──────────────────────────────────────────────────────────

class SequenceManager:
    """⚰ Save/load and the playback thread for the pre-2.0 linear path.

    Nothing in the app constructs this — see `PlaybackWorker` above. Flows are
    saved and loaded by `flow.FlowGraph.save` / `.load`, and run by
    `flow_exec.FlowWorker`.

    ⚠ `stop()` is still worth reading even though it is unreachable, and
    `tests/test_recorder_hold.py` still exercises it. It holds the
    retire-the-thread logic that `SequenceTab.stop_playback` had to learn the
    hard way: destroying a QThread that is still running is a Qt-level fatal
    error rather than an exception, and the wait times out routinely because
    OCR and image matching are not interruptible. That reasoning is live even
    where this class is not.
    """

    VERSION = 1

    def __init__(self):
        self._thread: Optional[QThread] = None
        self._worker: Optional[PlaybackWorker] = None
        # Threads whose wait() timed out. Held until they genuinely finish —
        # see stop() for why dropping them instead is fatal.
        self._retired: list = []

    @property
    def playing(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def save(self, steps: List[SeqStep], path: str):
        payload = {
            "version": self.VERSION,
            "steps": [s.to_dict() for s in steps],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

    def load(self, path: str) -> List[SeqStep]:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        return [SeqStep.from_dict(s) for s in payload.get("steps", [])]

    def play(self, steps: List[SeqStep], loop_count: int = 1,
             speed_factor: float = 1.0,
             wait_for_image: bool = False,
             image_path: str = "",
             image_confidence: float = 0.8,
             blacklist: Optional[List[str]] = None) -> PlaybackWorker:
        self.stop()
        w = PlaybackWorker()
        w.steps            = steps
        w.loop_count       = loop_count
        w.speed_factor     = speed_factor
        w.wait_for_image   = wait_for_image
        w.image_path       = image_path
        w.image_confidence = image_confidence
        w.blacklist        = list(blacklist or [])
        t = QThread()
        w.moveToThread(t)
        t.started.connect(w.run)
        w.finished.connect(t.quit)
        t.start()
        self._thread, self._worker = t, w
        return w

    def stop(self):
        """Ask playback to stop, and never drop a thread that is still running.

        This used to wait 3 s and then clear both references regardless — the
        same bug `SequenceTab.stop_playback` was fixed for in 2.0.8. Destroying
        a running QThread is a Qt `qFatal`, an `abort()` in C rather than an
        exception, so it takes the whole app with it and leaves no traceback.
        The wait times out routinely: a playback step can be sitting in an image
        match or a screen grab, and neither is interruptible.

        The wait is also skipped when this is called *from* the playback thread.
        A thread waiting on itself returns immediately, which would turn the
        timeout path into the always path without saying so — Qt prints
        "QThread: Destroyed while thread is still running" and nothing else.
        """
        if self._worker:
            self._worker.request_stop()
        for w in list(self._retired_workers()):
            w.request_stop()
        t, w = self._thread, self._worker
        if t is not None and t.isRunning():
            t.quit()
            if QThread.currentThread() is not t:
                t.wait(3000)
            if t.isRunning():
                self._retire(t, w)
        self._thread = self._worker = None
        self._reap()

    def _retired_workers(self):
        return [w for _t, w in self._retired if w is not None]

    def _retire(self, thread: QThread, worker) -> None:
        """Hold a still-running pair until the thread reports finished."""
        pair = (thread, worker)
        self._retired.append(pair)

        def _release():
            try:
                self._retired.remove(pair)
            except ValueError:
                pass
            if worker is not None:
                worker.deleteLater()

        # Queued so the release lands on the thread that owns this recorder,
        # not on the worker thread that is in the middle of dying.
        thread.finished.connect(_release, Qt.QueuedConnection)

    def _reap(self) -> None:
        """Drop pairs whose thread has finished but whose signal never arrived
        (a thread that was quit before it ever started emits nothing)."""
        self._retired = [(t, w) for t, w in self._retired
                         if t is not None and t.isRunning()]
