"""Driver-level keyboard + mouse backend (Interception).

Why this exists: some games (Ghost of Tsushima and other AAA console ports with
anti-tamper) reject ALL user-mode injected input — pynput, SendInput scancodes
(see sendinput_backend.py), keybd_event, PostMessage. They read the keyboard in
a way that distinguishes real hardware from software injection.

The Interception driver is a kernel-mode filter driver that injects keystrokes
at the driver level, indistinguishable from a physical keyboard, so those games
accept it. This module wraps the `interception-python` package.

Requirements (one-time, done by the user — a kernel driver + reboot):
    1. Download Interception:  https://github.com/oblitum/Interception/releases
    2. In an ADMIN terminal:   install-interception.exe /install
    3. Reboot.
    4. pip install interception-python   (already a Macronaut dependency)

`InterceptionKeyboard` mirrors the pynput keyboard.Controller press/release
surface used in flow_exec.py / recorder.py and is keyed by Macronaut's key names
(lowercase: "w", "space", "shift_l", "up", ...) — a drop-in for ScancodeKeyboard.

`InterceptionMouse` does the same for the pynput mouse.Controller surface the
engines use (`position` get/set, `press`, `release`, `click`), keyed by
Macronaut's button names ("left", "right", "middle"). It matters more than the
keyboard for this app: an autoclicker's core action is a *click*, so on a game
that rejects user-mode injection a keyboard-only backend delivers keystrokes and
silently drops every click.

Two deliberate departures from the `interception` package's own helpers:

- **Reading the cursor position always goes through Win32** (`GetCursorPos`, via
  the package's `mouse_position()`). The driver is a send-only path; the OS
  cursor is the single truth both we and the game read.
- **We build button strokes ourselves instead of calling `mouse_down()` /
  `mouse_up()`.** Those sleep `delay or MOUSE_BUTTON_DELAY` per call, and since
  `0` is falsy you cannot opt out by passing `delay=0` — you would get 30 ms
  twice per click, capping the clicker near 16 CPS. Macronaut times its own
  clicks (interval / hold / human mode), so the backend must not add sleeps.

IMPORTANT — device slots: Interception sends input *as* one of 20 device slots
(keyboards 0-9, mice 10-19). The package's auto-detection just picks the FIRST
slot that looks like the right device class, and on most machines that is a
VIRTUAL device (RGB software, touchpad driver, wireless dongle) — strokes sent
there vanish silently, everywhere. Run `--identify` / `--identify-mouse` once
and press a key / click: the driver reports which slot your REAL hardware lives
on, and the choice is saved to ~/.macronaut/interception.json and used
automatically from then on. Slot numbers follow attach order and shuffle on
reboot/replug, so startup re-locates the device by its stable hardware ID.

Standalone test (walk forward 3s in the focused game):
    python interception_backend.py --identify   # ONCE: press a key -> find & save
                                                # your real keyboard's device slot
    python interception_backend.py --identify-mouse  # ONCE: click -> save your
                                                     # real mouse's device slot
    python interception_backend.py --list       # all keyboard + mouse slots/HWIDs
    python interception_backend.py            # 2s to focus the game, then hold W 3s
    python interception_backend.py w a s d     # tap each key once, in order
    python interception_backend.py --hold w 3   # hold W for 3 seconds
    python interception_backend.py --click 10   # 10 left clicks at the cursor
    python interception_backend.py --click 5 right   # 5 right clicks
    python interception_backend.py --cps 20     # click as fast as possible for 3s
                                                # and report the achieved rate

Unlike SendInput, Interception injects below UIPI, so the game being elevated
does not block WRITING. (Macronaut's hotkey LISTENER still needs admin to READ
your keys while an elevated game is focused — run Macronaut as admin.)
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Optional

# Optional dependency — degrade gracefully like Macronaut's other optional deps
# (opencv, pywin32, winsdk). The package is pure-Python; it only needs the kernel
# driver present at *runtime*, not at import.
try:
    import interception as _icept
except Exception:  # pragma: no cover - import guard
    _icept = None


# ── Macronaut key name -> interception key name ───────────────────────────────
# Only names that differ are listed; everything else (single chars, "space",
# "tab", "up", "f5", digits, ...) passes through unchanged and is understood by
# interception directly.
_NAME_MAP: dict = {
    "ctrl_l": "ctrlleft", "ctrl_r": "ctrlright",
    "alt_l": "altleft",   "alt_r": "altright",
    "shift_l": "shiftleft", "shift_r": "shiftright",
    "cmd": "win", "meta": "win",
    "page_up": "pageup",     "page_down": "pagedown",
    "caps_lock": "capslock", "num_lock": "numlock", "scroll_lock": "scrolllock",
    "print_screen": "printscreen", "prtsc": "printscreen",
    "del": "delete", "escape": "esc",
    "media_play_pause": "playpause", "media_next": "nexttrack",
    "media_previous": "prevtrack",   "media_prev": "prevtrack",
    "media_volume_up": "volumeup",   "media_volume_down": "volumedown",
    "media_mute": "volumemute",
}


def _to_iname(name: str) -> str:
    n = name.lower().strip()
    return _NAME_MAP.get(n, n)


# ── Macronaut button name -> interception button name ─────────────────────────
_BUTTON_MAP: dict = {
    "left": "left", "right": "right", "middle": "middle",
    "x1": "mouse4", "x2": "mouse5", "mouse4": "mouse4", "mouse5": "mouse5",
}

# Slot ranges the driver reserves per device class, and the config keys each
# class persists under. Keyboard keeps the original unprefixed names so configs
# written before mouse support keep working untouched.
_SLOTS: dict = {"keyboard": range(0, 10), "mouse": range(10, 20)}
_CFG_KEYS: dict = {
    "keyboard": ("keyboard_device", "hwid"),
    "mouse": ("mouse_device", "mouse_hwid"),
}
# CLI flag that (re-)identifies each class, for error messages.
_IDENTIFY_FLAG: dict = {"keyboard": "--identify", "mouse": "--identify-mouse"}

_NOT_INSTALLED = (
    "interception-python is not installed. Run: pip install interception-python"
)

# Smallest delay that suppresses the package's own per-button sleep on the
# fallback path. It does `time.sleep(delay or MOUSE_BUTTON_DELAY)`, and 0 is
# falsy — so passing 0 would give you the full 30 ms. A negligible truthy value
# is the only way to opt out through the public helpers.
_MIN_DELAY = 1e-9
# One detent of a real wheel, same unit Windows uses (see sendinput_backend).
_WHEEL_DELTA = 120

# Per-class lazy-init state: has the send device been chosen, and which slot.
_ready: dict = {"keyboard": False, "mouse": False}
_active_slot: dict = {"keyboard": None, "mouse": None}

_CFG_DIR = Path.home() / ".macronaut"
_CFG_FILE = _CFG_DIR / "interception.json"


def _read_cfg() -> dict:
    """The whole config dict, {} if absent/corrupt."""
    try:
        data = json.loads(_CFG_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _load_saved(kind: str = "keyboard") -> tuple:
    """-> (slot, hwid) from --identify's config, (None, None) if absent/corrupt."""
    slot_key, hwid_key = _CFG_KEYS[kind]
    data = _read_cfg()
    try:
        slot = int(data[slot_key])
    except Exception:
        return None, None
    if slot not in _SLOTS[kind]:
        return None, None
    return slot, data.get(hwid_key) or None


def _load_saved_slot(kind: str = "keyboard") -> Optional[int]:
    return _load_saved(kind)[0]


def _find_slot_by_hwid(hwid: str, kind: str = "keyboard") -> Optional[int]:
    """Scan this class's slots for the device with this hardware ID. Slot
    numbers follow attach order and shuffle on reboot/replug — the HWID is
    the stable identity."""
    ctx = _icept.Interception()
    try:
        for slot in _SLOTS[kind]:
            if ctx.devices[slot].get_HWID() == hwid:
                return slot
    finally:
        ctx.destroy()
    return None


def _save_slot(slot: int, hwid: Optional[str], kind: str = "keyboard") -> None:
    """Persist one class's slot. Merges into the existing config so that
    identifying the mouse does not wipe the saved keyboard, or vice versa."""
    slot_key, hwid_key = _CFG_KEYS[kind]
    data = _read_cfg()
    data[slot_key] = slot
    data[hwid_key] = hwid
    try:
        _CFG_DIR.mkdir(parents=True, exist_ok=True)
        _CFG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass  # persistence is best-effort; the slot is still active this run


def _list_slots(kind: str) -> list:
    if _icept is None:
        raise RuntimeError(_NOT_INSTALLED)
    ctx = _icept.Interception()
    try:
        return [
            (slot, ctx.devices[slot].get_HWID())
            for slot in _SLOTS[kind]
            if ctx.devices[slot].get_HWID()
        ]
    finally:
        ctx.destroy()


def list_keyboard_slots() -> list:
    """[(slot, hwid), ...] for keyboard slots 0-9 with hardware attached."""
    return _list_slots("keyboard")


def list_mouse_slots() -> list:
    """[(slot, hwid), ...] for mouse slots 10-19 with hardware attached."""
    return _list_slots("mouse")


def _identify(kind: str, timeout: float) -> Optional[dict]:
    """Capture ONE real event from this device class -> {'device', 'hwid'}.

    The driver reports which device a physical event came from — no guessing.
    Only the down-event is filtered and each captured stroke is re-sent
    immediately, so the keyboard/mouse keeps working while this listens.
    """
    if _icept is None:
        raise RuntimeError(_NOT_INSTALLED)
    ctx = _icept.Interception()
    if kind == "keyboard":
        ctx.set_filter(ctx.is_keyboard, _icept.FilterKeyFlag.FILTER_KEY_DOWN)
    else:
        ctx.set_filter(
            ctx.is_mouse,
            _icept.FilterMouseButtonFlag.FILTER_MOUSE_LEFT_BUTTON_DOWN,
        )
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            device = ctx.await_input()
            if device is None:
                continue
            stroke = ctx.devices[device].receive()
            if stroke is None:
                continue
            ctx.send(device, stroke)  # pass through FIRST — never eat the event
            if device not in _SLOTS[kind]:
                continue  # wrong class for this filter — ignore, keep listening
            return {"device": device, "hwid": ctx.devices[device].get_HWID()}
    except KeyboardInterrupt:
        pass
    finally:
        ctx.destroy()
    return None


def identify_device(timeout: float = 20.0) -> Optional[dict]:
    """Capture ONE real key-down and return {'device': slot, 'hwid': ...}."""
    return _identify("keyboard", timeout)


def identify_mouse_device(timeout: float = 20.0) -> Optional[dict]:
    """Capture ONE real left-click and return {'device': slot, 'hwid': ...}."""
    return _identify("mouse", timeout)


def _ensure_ready(kind: str = "keyboard") -> None:
    """Lazily pick the send device for this device class. Uses the slot saved by
    --identify / --identify-mouse when present; otherwise falls back to the
    package's auto-detection. Raises a clear RuntimeError if the package or the
    kernel driver isn't available.

    Keyboard and mouse initialise independently: a machine can have a saved,
    working keyboard slot and no mouse slot yet (or the reverse), and choosing
    one must not force the other to resolve.
    """
    if _ready[kind]:
        return
    if _icept is None:
        raise RuntimeError(_NOT_INSTALLED)
    try:
        saved, saved_hwid = _load_saved(kind)
        if saved is not None:
            slot = saved
            if saved_hwid:
                found = _find_slot_by_hwid(saved_hwid, kind)
                if found is None:
                    raise RuntimeError(
                        f"The {kind} saved by {_IDENTIFY_FLAG[kind]} is not "
                        "attached (slots shuffle on reboot/replug and the "
                        "hardware ID matched no device). Re-run: python "
                        f"interception_backend.py {_IDENTIFY_FLAG[kind]}"
                    )
                if found != slot:
                    slot = found
                    _save_slot(slot, saved_hwid, kind)  # keep the config truthful
            _icept.set_devices(**{kind: slot})
            _active_slot[kind] = slot
        else:
            _icept.auto_capture_devices(keyboard=(kind == "keyboard"),
                                        mouse=(kind == "mouse"))
            try:
                _active_slot[kind] = (_icept.get_keyboard() if kind == "keyboard"
                                      else _icept.get_mouse())
            except Exception:
                _active_slot[kind] = None
    except _icept.exceptions.DriverNotFoundError as e:
        raise RuntimeError(
            "Interception driver not installed/loaded. Install it "
            "(admin: install-interception.exe /install) and REBOOT, then retry."
        ) from e
    _ready[kind] = True


def _class_available(kind: str) -> bool:
    if _icept is None:
        return False
    try:
        _ensure_ready(kind)
        return True
    except RuntimeError:
        return False


def is_available() -> bool:
    """True if the package is importable AND the driver is loaded (no reboot
    pending) for keyboard sending. Cheap enough to gate a UI backend selector."""
    return _class_available("keyboard")


def mouse_available() -> bool:
    """Same check for the mouse device class."""
    return _class_available("mouse")


class InterceptionKeyboard:
    """Driver-level keyboard. Same press/release/tap surface as
    sendinput_backend.ScancodeKeyboard, keyed by Macronaut key names."""

    # See ScancodeKeyboard.sends_scancodes — this backend exists precisely
    # because the target ignores message-queue input, so typing must not use it.
    sends_scancodes = True

    def maps_key(self, name: str) -> bool:
        """True if this backend can send the key (input_backends routing probe)."""
        if _icept is None:
            return False
        try:
            from interception import _keycodes
            _keycodes.get_key_information(_to_iname(name))
            return True
        except Exception:
            return False

    def press(self, name: str) -> None:
        _ensure_ready()
        _icept.key_down(_to_iname(name))

    def release(self, name: str) -> None:
        _ensure_ready()
        _icept.key_up(_to_iname(name))

    def tap(self, name: str, hold: float = 0.05) -> None:
        """Press, hold briefly, release. The hold lets the game's input polling
        see the key for a few frames; an instant down+up is often missed."""
        _ensure_ready()
        iname = _to_iname(name)
        _icept.key_down(iname)
        time.sleep(hold)
        _icept.key_up(iname)


class InterceptionMouse:
    """Driver-level mouse with the pynput mouse.Controller surface the engines
    use (`position` get/set, `press`, `release`, `click`), keyed by Macronaut
    button names ("left", "right", "middle").

    No method sleeps: Macronaut owns click timing (interval, hold, human mode),
    so press/release return as soon as the stroke reaches the driver. See the
    module docstring for why this bypasses the package's mouse_down/mouse_up.
    """

    def maps_button(self, name: str) -> bool:
        """True if this backend can send the button (routing probe, mirrors
        InterceptionKeyboard.maps_key)."""
        if _icept is None:
            return False
        return str(name).lower().strip() in _BUTTON_MAP

    # ── position: read from the OS, write through the driver ───────────
    @property
    def position(self) -> tuple:
        """The real cursor position (Win32 GetCursorPos). Send-only driver:
        there is nothing to read back from it, and the OS cursor is the single
        truth both Macronaut and the game observe."""
        if _icept is None:
            raise RuntimeError(_NOT_INSTALLED)
        return _icept.mouse_position()

    @position.setter
    def position(self, xy) -> None:
        _ensure_ready("mouse")
        x, y = xy
        # allow_global_params=False pins this to the instant absolute-move path.
        # The default would silently divert to a bezier "human" curve if global
        # curve params are ever set — slow, and Macronaut has its own jitter.
        _icept.move_to(int(x), int(y), allow_global_params=False)

    # ── buttons ───────────────────────────────────────────────────────
    def _send(self, name, down: bool) -> None:
        _ensure_ready("mouse")
        iname = _BUTTON_MAP.get(str(name).lower().strip())
        if iname is None:
            raise ValueError(f"unknown mouse button: {name!r}")
        states = _icept.MouseButtonFlag.from_string(iname)
        state = states[0] if down else states[1]  # (down, up)
        ctx = getattr(_icept.inputs, "_g_context", None)
        if ctx is None:  # package internals moved — public path, costs a sleep
            fn = _icept.mouse_down if down else _icept.mouse_up
            fn(iname, delay=_MIN_DELAY)
            return
        # MOUSE_MOVE_RELATIVE (== 0) with x=y=0 means "no movement", which is
        # what a pure button event should carry. The package's own
        # mouse_down/mouse_up use MOUSE_MOVE_ABSOLUTE with zeroed coordinates,
        # which literally reads as "move to absolute (0, 0)" — harmless if you
        # always move before clicking, but Macronaut's commonest case is
        # clicking wherever the cursor already is, and a teleport to the
        # top-left corner on every click would break exactly that.
        stroke = _icept.MouseStroke(
            _icept.MouseFlag.MOUSE_MOVE_RELATIVE, state, 0, 0, 0
        )
        ctx.send(ctx.mouse, stroke)

    def press(self, name) -> None:
        self._send(name, True)

    def release(self, name) -> None:
        self._send(name, False)

    def scroll(self, dx: int = 0, dy: int = 0) -> None:
        """Wheel by whole notches. Signs follow pynput: +dy up, +dx right.

        One stroke per notch, matching real hardware and the SendInput backend —
        see ScancodeMouse.scroll for why a single big roll is not the same thing.

        ⚠ MouseStroke packs button_data with struct format "H" — an *unsigned*
        short — so a negative roll (scrolling down, which is most of them) has
        to go in as two's complement or struct.error kills the stroke. The
        driver reads it back signed.
        """
        _ensure_ready("mouse")
        ctx = getattr(_icept.inputs, "_g_context", None)
        if ctx is None:      # package internals moved; nothing safe to send
            raise RuntimeError("interception context unavailable")
        for flag, n in ((_icept.MouseButtonFlag.MOUSE_WHEEL, int(dy)),
                        (_icept.MouseButtonFlag.MOUSE_HWHEEL, int(dx))):
            roll = (_WHEEL_DELTA if n > 0 else -_WHEEL_DELTA) & 0xFFFF
            for _ in range(abs(n)):
                ctx.send(ctx.mouse, _icept.MouseStroke(
                    _icept.MouseFlag.MOUSE_MOVE_RELATIVE, flag, roll, 0, 0))

    def click(self, name, count: int = 1) -> None:
        """Press/release `count` times back-to-back. Callers that need a gap
        between clicks (double-click timing, CPS pacing) space them themselves."""
        for _ in range(max(1, int(count))):
            self._send(name, True)
            self._send(name, False)


def _cli(argv: list) -> int:
    kb = InterceptionKeyboard()
    ms = InterceptionMouse()

    def _run(action, kind: str = "keyboard"):
        try:
            action()
        except RuntimeError as e:
            print(f"NOT READY: {e}")
            return 1
        flag = _IDENTIFY_FLAG[kind]
        origin = f"saved by {flag}" if _load_saved_slot(kind) is not None else \
                 f"auto-detected — if the game saw nothing, run {flag}"
        print(f"Done — sent via driver on {kind} slot "
              f"{_active_slot[kind]} ({origin}).")
        return 0

    if argv and argv[0] == "--list":
        try:
            slots = list_keyboard_slots()
            mouse_slots = list_mouse_slots()
        except RuntimeError as e:
            print(f"NOT READY: {e}")
            return 1
        for kind, rows in (("Keyboard", slots), ("Mouse", mouse_slots)):
            lower = kind.lower()
            saved = _load_saved_slot(lower)
            flag = _IDENTIFY_FLAG[lower]
            print(f"{kind} device slots with hardware attached:")
            for slot, hwid in rows:
                mark = f"  <-- saved ({flag})" if slot == saved else ""
                print(f"  slot {slot}: {hwid}{mark}")
            if not rows:
                print("  (none)")
            if saved is None:
                print(f"No {lower} slot saved yet — run: "
                      f"python interception_backend.py {flag}")
            print()
        return 0

    if argv and argv[0] == "--identify-mouse":
        try:
            slots = list_mouse_slots()
            print("Mouse device slots with hardware attached:")
            for slot, hwid in slots:
                print(f"  slot {slot}: {hwid}")
            print()
            print("CLICK once with the mouse you play the game with... (20s)")
            info = identify_mouse_device()
        except RuntimeError as e:
            print(f"NOT READY: {e}")
            return 1
        if info is None:
            print("No click captured. Is the driver loaded (did you reboot "
                  "after install)? Try --list to see the slots.")
            return 1
        _save_slot(info["device"], info.get("hwid"), "mouse")
        print(f"Your mouse is device slot {info['device']} — saved to {_CFG_FILE}")
        print(f"  HWID: {info['hwid']}")
        print("Now test:  python interception_backend.py --click 5")
        print("  (focus Notepad or an empty desktop area first, then the game)")
        return 0

    if argv and argv[0] == "--click":
        count = int(argv[1]) if len(argv) > 1 else 5
        button = argv[2] if len(argv) > 2 else "left"
        print(f"Focus the target... {count} {button} click(s) at the cursor in 2s")
        time.sleep(2)

        def click_n():
            for _ in range(count):
                ms.click(button)
                time.sleep(0.1)
        return _run(click_n, "mouse")

    if argv and argv[0] == "--cps":
        seconds = float(argv[1]) if len(argv) > 1 else 3.0
        print(f"Focus the target... clicking flat-out for {seconds}s in 2s")
        print("  (measures the backend's own ceiling — no interval pacing)")
        time.sleep(2)
        sent = [0]

        def burst():
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                ms.click("left")
                sent[0] += 1
        rc = _run(burst, "mouse")
        if rc == 0:
            print(f"  {sent[0]} clicks in {seconds}s = "
                  f"{sent[0] / seconds:.0f} CPS")
        return rc

    if argv and argv[0] == "--identify":
        try:
            slots = list_keyboard_slots()
            print("Keyboard device slots with hardware attached:")
            for slot, hwid in slots:
                print(f"  slot {slot}: {hwid}")
            print()
            print("Press ONE key on the keyboard you play the game with... (20s)")
            info = identify_device()
        except RuntimeError as e:
            print(f"NOT READY: {e}")
            return 1
        if info is None:
            print("No keypress captured. Is the driver loaded (did you reboot "
                  "after install)? Try --list to see the slots.")
            return 1
        _save_slot(info["device"], info.get("hwid"))
        print(f"Your keyboard is device slot {info['device']} — saved to {_CFG_FILE}")
        print(f"  HWID: {info['hwid']}")
        print("Now test:  python interception_backend.py --hold w 3")
        print("  (focus Notepad first as a sanity check, then the game)")
        return 0

    if argv and argv[0] == "--hold":
        if len(argv) < 2:
            print("usage: --hold <key> [seconds]")
            return 2
        name, seconds = argv[1], (float(argv[2]) if len(argv) > 2 else 3.0)
        print(f"Focus the game... holding {name!r} for {seconds}s in 2s")
        time.sleep(2)
        return _run(lambda: (kb.press(name), time.sleep(seconds), kb.release(name)))

    keys = argv or ["w"]
    default = not argv
    print(f"Focus the game... {'holding W 3s' if default else 'tapping ' + ' '.join(keys)} in 2s")
    time.sleep(2)
    if default:
        return _run(lambda: (kb.press("w"), time.sleep(3), kb.release("w")))

    def tap_all():
        for name in keys:
            kb.tap(name)
            time.sleep(0.15)
    return _run(tap_all)


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
