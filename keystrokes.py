"""Key name tables and helpers: name<->pynput key conversion and display labels."""
from typing import List, Optional, Union
from pynput.keyboard import Key, KeyCode

# ── Key mapping tables ────────────────────────────────────────────────────────

_KEY_MAP: dict = {
    # Modifiers
    "ctrl": Key.ctrl, "ctrl_l": Key.ctrl_l, "ctrl_r": Key.ctrl_r,
    "alt": Key.alt,   "alt_l": Key.alt_l,   "alt_r": Key.alt_r,
    "shift": Key.shift, "shift_l": Key.shift_l, "shift_r": Key.shift_r,
    "win": Key.cmd,   "cmd": Key.cmd,        "meta": Key.cmd,
    # Navigation & editing
    "enter": Key.enter,   "return": Key.enter,
    "tab": Key.tab,       "space": Key.space,
    "backspace": Key.backspace, "delete": Key.delete, "del": Key.delete,
    "esc": Key.esc,       "escape": Key.esc,
    "up": Key.up,         "down": Key.down,
    "left": Key.left,     "right": Key.right,
    "home": Key.home,     "end": Key.end,
    "page_up": Key.page_up,   "pageup": Key.page_up,
    "page_down": Key.page_down, "pagedown": Key.page_down,
    "insert": Key.insert,
    # Locks
    "caps_lock": Key.caps_lock,   "capslock": Key.caps_lock,
    "num_lock": Key.num_lock,     "numlock": Key.num_lock,
    "scroll_lock": Key.scroll_lock,
    # System
    "print_screen": Key.print_screen,  "prtsc": Key.print_screen,
    "pause": Key.pause,
    # Function keys
    **{f"f{i}": getattr(Key, f"f{i}") for i in range(1, 21)},
    # Media keys
    "media_play_pause": Key.media_play_pause,
    "media_next":       Key.media_next,
    "media_previous":   Key.media_previous,
    "media_prev":       Key.media_previous,
    "media_volume_up":  Key.media_volume_up,
    "media_volume_down":Key.media_volume_down,
    "media_mute":       Key.media_volume_mute,
}

# Human-readable display labels
KEY_DISPLAY: dict = {
    "ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win",
    "enter": "Enter", "tab": "Tab", "space": "Space",
    "backspace": "Backspace", "delete": "Delete", "esc": "Esc",
    "up": "↑", "down": "↓", "left": "←", "right": "→",
    "home": "Home", "end": "End", "page_up": "PgUp", "page_down": "PgDn",
    "insert": "Insert", "caps_lock": "CapsLk", "num_lock": "NumLk",
    "print_screen": "PrtSc", "pause": "Pause",
    "media_play_pause": "⏯ Play/Pause", "media_next": "⏭ Next",
    "media_previous": "⏮ Prev", "media_volume_up": "🔊+",
    "media_volume_down": "🔊−", "media_mute": "🔇 Mute",
}

def parse_key(name: str) -> Optional[Union[Key, KeyCode]]:
    """Convert a string name to a pynput key object."""
    n = name.lower().strip()
    if n in _KEY_MAP:
        return _KEY_MAP[n]
    if len(n) == 1:
        return KeyCode.from_char(n)
    return None


def display_key(name: str) -> str:
    return KEY_DISPLAY.get(name.lower(), name.upper())


def display_combo(keys: List[str]) -> str:
    return "+".join(display_key(k) for k in keys)
