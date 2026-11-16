"""Application settings management — persisted as JSON in the user's home directory."""
import json
import os
import shutil
import tempfile
from pathlib import Path
from dataclasses import dataclass, asdict, field, fields
from typing import Dict, List, get_args, get_origin

APP_DIR_NAME    = ".macronaut"
LEGACY_DIR_NAME = ".autoclicker_pro"


def data_dir() -> Path:
    """Return Macronaut's per-user data directory (~/.macronaut), migrating any
    legacy ~/.autoclicker_pro contents into it on first run. The legacy folder
    is left in place as a fallback/backup and never deleted."""
    new    = Path.home() / APP_DIR_NAME
    legacy = Path.home() / LEGACY_DIR_NAME
    if not new.exists() and legacy.exists():
        try:
            shutil.copytree(legacy, new)
        except Exception:
            pass
    try:
        new.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return new


def scripts_dir() -> Path:
    """Folder where Macronaut keeps the user's saved scripts (the in-app
    library scans this). Created on demand."""
    d = data_dir() / "scripts"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


# The theme the app opens on when nobody has picked one. Lives here rather
# than in main.py because `_load` has to apply it and settings cannot import
# main; main takes its own DEFAULT_THEME from this.
DEFAULT_THEME = "cosmic"
VALID_THEMES = ("cosmic", "mission", "graphite", "daylight")

SETTINGS_DIR  = data_dir()
SETTINGS_FILE = SETTINGS_DIR / "settings.json"


@dataclass
class AppSettings:
    # ── Basic clicking ──────────────────────────────────────────────
    button: str = "left"                  # left | right | middle
    click_type: str = "single"            # single | double | hold
    hold_duration_ms: int = 100
    fixed_x: int = 0
    fixed_y: int = 0
    interval_ms: int = 1000
    randomize_interval: bool = False
    random_range_ms: int = 100
    # "Click as fast as the machine can" — the Basic face's Max speed tick.
    # When on, `interval_ms` is ignored rather than overwritten, so unticking it
    # gives back the interval that was there before instead of a zero.
    max_speed: bool = False

    # ── Stop conditions ─────────────────────────────────────────────
    limit_mode: str = "infinite"          # infinite | count
    limit_count: int = 100

    # ── Startup ─────────────────────────────────────────────────────
    start_delay_seconds: int = 0
    # Whether the starter flows have ever been offered. Recorded even when
    # nothing was written, so clearing the library later does not bring them
    # back — see starters.seed_once.
    starters_seeded: bool = False

    # ── Hotkeys ─────────────────────────────────────────────────────
    start_stop_hotkey: str = "f8"
    trigger_key: str = ""                 # empty = disabled

    # Launcher keys: hotkey -> script name (the file stem in scripts_dir(), so
    # the library stays portable and a renamed *file* is the only thing that can
    # break a binding). Kept here rather than inside each flow's JSON: which key
    # launches what is a property of this keyboard, not of the script, and a
    # shared script should not arrive carrying someone else's bindings.
    script_hotkeys: Dict[str, str] = field(default_factory=dict)

    # ── Failsafe (Phase 2) ──────────────────────────────────────────
    panic_enabled: bool = True
    panic_hotkey: str = "esc"             # always aborts any automation
    guard_enabled: bool = False           # abort if an unexpected window appears
    guard_mode: str = "deny"              # deny = abort if title matches; allow = abort if not
    guard_titles: List[str] = field(default_factory=list)

    # ── Human mode ──────────────────────────────────────────────────
    human_mode: bool = False
    cursor_jitter_px: int = 5

    # ── Region constraint ────────────────────────────────────────────
    use_region: bool = False
    region_x: int = 0
    region_y: int = 0
    region_w: int = 800
    region_h: int = 600

    # ── Window focus auto-pause ──────────────────────────────────────
    pause_on_focus_loss: bool = False
    focus_window_title: str = ""

    # ── Image recognition trigger ────────────────────────────────────
    image_trigger_confidence: float = 0.8

    # ── Keystroke automation ─────────────────────────────────────────
    typing_speed_cps: float = 10.0
    keystroke_blacklist: List[str] = field(default_factory=list)
    keystroke_interval_ms: int = 50
    input_backend: str = "pynput"         # pynput | sendinput | interception
    key_hold_ms: int = 60                 # how long a tapped key stays down;
                                          # games poll per frame, miss 0ms taps
    # Which keyboard the *target* thinks it is reading. A scancode is a key
    # POSITION, and the character it becomes is decided by whoever receives it.
    # Windows apps use the active layout, so a Belgian AZERTY user types "a" and
    # we send the position a Belgian keyboard has "a" on — correct. Some games
    # ignore Windows layouts and run every scancode through their own US table,
    # which turns that same keystroke into "q". "us" sends the position a US
    # keyboard would use instead, so those targets read what was written.
    # Only affects scancode backends; the Unicode path carries codepoints.
    type_key_positions: str = "layout"    # layout | us
    # Dead as of 2.0.17 — kept so loading a 2.0.16 settings.json does not warn.
    # It asked whether to hold SHIFT while injecting *characters*, which was a
    # fix for the wrong model: typing now presses real keys, so the modifier is
    # part of producing the character and there is nothing to choose.
    type_shift_for_capitals: bool = False

    # ── Pointer travel ───────────────────────────────────────────────
    # Whether the cursor GLIDES to a click's target or is teleported there in
    # one assignment. On by default, and the default is the point: a receiver
    # that samples the pointer once a frame reads a teleport as a cursor that
    # was never on the way, so hover states never fire and menus that open on
    # mouse-enter are not open when the click lands. See flow.travel_path.
    smooth_mouse: bool = True
    # Pixels per second. Clamped by flow.travel_pps, and the glide is floored
    # and capped in *time* as well, so neither a nudge nor a 4K diagonal turns
    # into something silly.
    mouse_travel_pps: int = 3000
    # Bow the path slightly and add a pixel of tremor. Follows the same
    # human_mode the auto-clicker's jitter does rather than being a switch of
    # its own — "move like a hand" is one preference, not two.

    # ── Updates ──────────────────────────────────────────────────────
    auto_check_updates: bool = True       # check on startup (never auto-applies)
    auto_download_updates: bool = True    # stage the download once one is found
    last_update_check: float = 0.0        # unix time; throttles the startup check
    skip_version: str = ""                # "remind me never about this one"
    update_manifest_url: str = ""         # override the built-in URL (staging)
    pending_update: str = ""              # version staged and ready to apply

    # ── Crash reporting ──────────────────────────────────────────────
    # "ask" until the user has answered once, then "on" / "off". The tri-state
    # is the point: it distinguishes "hasn't been asked" from "said no", so a
    # declined prompt is never re-asked.
    crash_reports: str = "ask"

    # The one-time "would you star it?" question — see `star_ui.py`.
    # `launch_count` is counted from the release that introduced it, so nobody
    # is asked on the first launch after an update. `star_asked` is written
    # before the box is shown: once, whatever happens to that box.
    launch_count: int = 0
    star_asked: bool = False

    # ── Appearance ───────────────────────────────────────────────────
    dark_mode: bool = True                # legacy; kept for migration
    theme: str = DEFAULT_THEME            # see VALID_THEMES
    # Whether the user has ever actually PICKED a theme.
    #
    # ⚠ Without this, changing the default does nothing for anybody who has
    # already run the app once: `save_to_settings` writes `theme` on every
    # save whether or not it was touched, so every existing settings.json
    # already pins whatever the default was the day it was written. When
    # Cosmic became the default that would have left the entire installed
    # base -- including the developer -- on the navy theme they were asking
    # to be rid of. False means "this is just the default", and the current
    # default is applied on load instead.
    theme_chosen: bool = False

    # ── Sequence playback ────────────────────────────────────────────
    seq_speed: float = 1.0
    # Whether the timeline strip under the canvas is unfolded. Remembered
    # because folding it is a deliberate "give me the canvas back" choice, and
    # having to make it again every launch is the papercut a setting removes.
    #
    # ⚠ The timeline strip deliberately has NO such setting. `timeline_open`
    # used to live here, and remembering it meant one look at a run's timing
    # left the strip unfolded in every session afterwards. It is the one
    # run-state view that shows nothing you cannot already see — the canvas
    # highlights the running node — so opening it is a thing you do for the run
    # in front of you, not a preference you hold. It starts folded, always.
    # (An old settings.json may still carry the key; unknown keys are ignored
    # on load, which is exactly why removing a field here is safe.)

    # Keep Macronaut above the window it is automating. Was the compact face's
    # pin; it outlived that face because an autoclicker you cannot see behind
    # your game is no use. Lives in Settings → Appearance.
    #
    # ⚠ Defaults to False, and the default is the point. It used to be True,
    # so a first launch planted the window over everything else on the desktop
    # before the user had asked for anything — which reads as an aggressive
    # app, and was reported as one. It is also the wrong state for the mode
    # people are in most of the time: staying on top helps while a flow RUNS
    # and is purely in the way while one is being built.
    always_on_top: bool = False

    # ── Persistence ──────────────────────────────────────────────────
    last_sequence_path: str = ""
    window_x: int = -1
    window_y: int = -1
    window_w: int = 900
    window_h: int = 680
    # Which face the app opens on. Written whenever the user switches, so
    # closing in Basic reopens in Basic and closing in Advanced reopens in
    # Advanced — the app comes back the way it was left rather than to whichever
    # face the developer decided was the front door.
    #
    # ⚠ The default is the FIRST-RUN default, and it is "basic" deliberately:
    # somebody who has just searched "auto clicker" and downloaded 78 MB should
    # meet an auto-clicker, not an empty canvas with a palette of nodes. That is
    # the whole reason this face came back.
    #
    # ⚠⚠ An EXISTING user must not be moved. `_load` sends anyone whose
    # settings.json predates this key to "advanced" — they have been using the
    # canvas for twenty-odd releases, and a first launch after an update that
    # silently swaps their window for a different one is the update reading as
    # a broken update.
    #
    # ⚠ Validated on load like `theme` is: an unknown value here would leave
    # `_init_window` with no face to show at all.
    last_face: str = "basic"             # basic | advanced

    # Each face keeps its OWN geometry, and that is the whole point of having
    # two of them: Basic is a small pop-up parked beside the window it is
    # clicking, Advanced is a large canvas. One shared rectangle meant switching
    # face resized the other one, so neither ever stayed where it was put.
    #
    # (`advanced_*` kept its name through the release that had only one face,
    # specifically so a returning user's canvas is where they left it. It is
    # accurate again now.)
    advanced_x: int = -1
    advanced_y: int = -1
    advanced_w: int = 980
    advanced_h: int = 700
    # -1 width/height means "has never been sized" — Basic then opens fitted to
    # its own content, which is the tight OP-style footprint. Once the user
    # resizes it, their size is what comes back. Position follows the same rule.
    basic_x: int = -1
    basic_y: int = -1
    basic_w: int = -1
    basic_h: int = -1


# The app's live settings, registered by MainWindow at startup.
#
# ⚠ Without this, `SettingsManager()` re-reads the JSON file on **every**
# construction, and the engines construct one per run
# (`input_backends._settings_backend` and friends). So a run read the file on
# disk while the Settings window showed and edited the app's in-memory copy, and
# the two disagree for as long as the change is unsaved. Selecting pynput and
# pressing Play still ran whatever the file last held — reported as "I have been
# on pynput the entire time" while every run used the Interception driver, which
# types key positions and turned AZERTY 'a' into 'q'.
#
# Deliberately an explicit registration rather than a singleton in __init__:
# tests, the CLI entry points and `--selftest` all construct managers of their
# own and must keep getting an isolated read of the file.
_ACTIVE: "Optional[SettingsManager]" = None


def set_active(manager: "SettingsManager") -> None:
    """Register the app's settings as the ones every component should read."""
    global _ACTIVE
    _ACTIVE = manager


def active() -> "Optional[SettingsManager]":
    """The app's live settings, or None outside the app (tests, CLI)."""
    return _ACTIVE


_FIELD_TYPES = {f.name: f.type for f in fields(AppSettings)}


def _coerce(key: str, value):
    """(accept?, value) for one setting read off disk.

    ⚠ Driven by `AppSettings`'s own annotations rather than a list kept beside
    them — a hand-maintained copy of 72 field types would be wrong within a
    month, and wrong in the direction of silently rejecting a good value.

    Rejecting means "keep the default", never raise: one bad line in
    settings.json must not stop the app opening, which is the same posture the
    surrounding `except` has always taken for a corrupt file.
    """
    want = _FIELD_TYPES.get(key)
    if want is None:
        return False, None

    # ⚠ bool before int, and bool excluded from int. In Python `True` *is* an
    # int, so a plain isinstance check accepts `{"seq_speed": true}` as a
    # number and `{"always_on_top": 1}` as a flag. Both then behave almost
    # correctly, which is worse than either working or failing.
    if want is bool:
        if isinstance(value, bool):
            return True, value
        # 0 and 1 only. Somebody hand-editing a flag may well write `1`, and
        # silently ignoring a plausible edit is worse than accepting it — but
        # `2` or `"yes"` is a mistake, and taking it as True would hide one.
        if isinstance(value, int) and value in (0, 1):
            return True, bool(value)
        return False, None
    if want is int:
        return (True, value) if isinstance(value, int) and not isinstance(value, bool) \
            else (False, None)
    if want is float:
        return (True, float(value)) if isinstance(value, (int, float)) \
            and not isinstance(value, bool) else (False, None)
    if want is str:
        return (True, value) if isinstance(value, str) else (False, None)

    origin = get_origin(want)
    if origin is list:
        (inner,) = get_args(want) or (str,)
        return (True, value) if isinstance(value, list) \
            and all(isinstance(x, inner) for x in value) else (False, None)
    if origin is dict:
        kt, vt = get_args(want) or (str, str)
        return (True, value) if isinstance(value, dict) \
            and all(isinstance(a, kt) and isinstance(b, vt)
                    for a, b in value.items()) else (False, None)
    # An annotation this does not understand is not a reason to drop the value.
    return True, value


class SettingsManager:
    def __init__(self):
        self.s = AppSettings()
        self._load()

    def _load(self):
        """Read settings.json, keeping the default for anything ill-typed.

        ⚠ Unknown keys are ignored deliberately — that is what makes removing a
        setting safe, and renaming one dangerous. What was *not* checked until 4 September 2026 was the
        **type** of a known key. `{"seq_speed": "fast"}` in a hand-edited file
        used to be stored as the string, and `{"script_hotkeys": "f13"}` as a
        string where a dict is expected — both accepted here and then failing
        somewhere else entirely, in a widget or a `.items()` call, with nothing
        pointing back at the file.

        People do edit this file: a hotkey collision is something only a
        hand-edited settings.json can create, so it is a real path rather than
        a theoretical one.
        """
        if not SETTINGS_FILE.exists():
            return
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in data.items():
                if hasattr(self.s, k):
                    ok, value = _coerce(k, v)
                    if ok:
                        setattr(self.s, k, value)
            # Migrate old dark_mode flag to a named theme.
            if "theme" not in data:
                self.s.theme = DEFAULT_THEME if data.get("dark_mode", True) else "daylight"
                self.s.theme_chosen = "dark_mode" in data
            if self.s.theme not in VALID_THEMES:
                self.s.theme = DEFAULT_THEME
            # A theme nobody chose follows the default wherever it moves.
            if not self.s.theme_chosen:
                self.s.theme = DEFAULT_THEME
            # We are reading a file, so this is not a first run — and a file
            # with no `last_face` in it was written by a release that had only
            # the canvas. That user has been on the canvas for twenty-odd
            # releases; the "basic" first-run default is for people who have
            # never opened the app, not for them.
            if "last_face" not in data:
                self.s.last_face = "advanced"
            # A settings.json carrying anything else here — hand-edited, or from
            # a future version — must not leave the app with no face to open on.
            if self.s.last_face not in ("basic", "advanced"):
                self.s.last_face = "advanced"
        except Exception:
            pass  # corrupted file → fall back to defaults

    def save(self):
        """Write settings.json, never truncating the copy already there.

        ⚠ This was left as a plain `open(..., "w")` on 4 September 2026 on the
        reasoning that settings are regenerable and `_load` falls back to
        defaults on a corrupt file. That reasoning was wrong, and measuring the
        failure is what showed it: opening for write **truncates first**, so a
        destination locked by antivirus, a sync client or an open editor is
        emptied to zero bytes and *then* raises. `_load` finds an empty file,
        returns every default, and says nothing — so the user silently loses
        every launcher-key binding, their input backend and their theme, with
        no error anywhere. The tolerant load does not protect them; it hides it.

        ⚠ Deliberately **no fsync**, unlike `flow.FlowGraph.save`. This runs on
        every settings change and on every close, and the guarantee worth
        having here is "the previous file survives a failed write", which the
        rename gives on its own. Durability against power loss is the flow's
        problem, not this one's.

        Failures stay silent, as before. A modal on a settings write is noise,
        and by the time this matters the app is usually closing.
        """
        try:
            SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(SETTINGS_DIR), prefix=".settings-",
                                       suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(asdict(self.s), f, indent=2)
                os.replace(tmp, str(SETTINGS_FILE))
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception:
            pass

    # ── Attribute proxy so callers can do settings.dark_mode ──────────
    def __getattr__(self, name: str):
        if name in ("s",):
            raise AttributeError(name)
        return getattr(self.s, name)

    def set(self, key: str, value):
        if hasattr(self.s, key):
            setattr(self.s, key, value)
            self.save()
