"""Window behaviour that can be checked without a human watching.

A GUI is usually checked by hand, on the grounds that no automated
check can see a window. That is true of *appearance* — flicker, whether the
compact face looks right at 150% scaling, always-on-top over a borderless game.
It is not true of everything a manual pass covers. Qt's `offscreen` platform builds
real widgets with real geometry and real window flags, and the multi-monitor
cases can be simulated by handing the code a fake screen.

What that buys, specifically:

  * whether MainWindow constructs at all under PySide6 — the Qt binding changed,
    and nothing else in this suite ever builds the main window;
  * whether the overlays span the whole virtual desktop, which is the bug this
    file was written for and which no single-monitor test could ever see;
  * whether repeated compact <-> Advanced switching keeps its flags.

Still needs eyes, and deliberately not attempted here: flag *flicker* during the
toggle, real DPI scaling, always-on-top over a fullscreen game, and whether any
of it looks right.
"""
import json
import re
import sys

import pytest
from PySide6.QtCore import QPoint, QRect, Qt

import recovery as _recovery
# Captured before `_no_recovery_prompt` below replaces it, so the one test
# that proves the mechanism can opt back in to the real thing.
_REAL_RECOVERY_READ = _recovery.read


def code_only(src: str) -> str:
    """Strip whole-line comments before searching source for a forbidden name.

    Every source-level guard in this repo has, at least once, failed on the
    comment explaining why the thing it forbids is wrong — the PyQt5 guard, the
    `version_file=` guard, and twice in this file. Good code documents the trap
    it avoids, which means the trap's name is present in the file by design.
    Search the code, not the prose.

    Whole lines only: a trailing comment after real code is rare here, and
    stripping those properly needs tokenize, not str.split.
    """
    return "\n".join(ln for ln in src.splitlines()
                     if not ln.strip().startswith("#"))


@pytest.fixture(autouse=True)
def _no_recovery_prompt():
    """⚠ Stop the suite hanging on a modal nobody can click.

    THE flake this file has been blamed for. Diagnosed 9 September 2026 with
    `-o faulthandler_timeout=45`, which finally caught it:

        Timeout (0:00:45)!
        Thread 0x00005628 (most recent call first):
          File "main.py", line 5564 in _offer_recovery
          File "tests/test_gui_offscreen.py", line 4384 in
              test_the_library_buttons_fit_at_the_dialogs_own_minimum_width

    `MainWindow.__init__` does `QTimer.singleShot(0, self._offer_recovery)`,
    and `_offer_recovery` raises a **modal** `QMessageBox.question` when there
    is a recovery payload to offer. Offscreen, nobody can answer it, so the run
    stops dead — no failure, no test name, just a process that never exits.

    ⚠ And the payload is written by the suite itself, which is why it looked
    random. Every `SequenceTab` starts a 20-second `QTimer` that writes one
    (`_write_recovery`). This file's run is longer than twenty seconds, so a
    timer fires part-way through, and the next `MainWindow` built after that
    finds a payload and asks about it. Whether that happens depends on how far
    the run gets in twenty seconds — "roughly one run in two", and reliably
    once the file grew past it.

    ⚠ It is NOT the two causes previously measured and recorded (stray
    processes holding pynput hooks; parentless dialogs being garbage
    collected). Those were tried and neither fixed it, which is exactly what
    you would expect of a modal in a different mechanism entirely.

    `read` rather than `write`: the app's own behaviour is left alone, the
    offer simply finds nothing. `tests/test_recovery.py` owns this feature and
    drives `_offer_recovery()` directly with `QMessageBox.question` stubbed, so
    nothing here is the only cover for it.
    """
    import recovery
    real_read = recovery.read
    recovery.read = lambda *a, **kw: None
    try:
        yield
    finally:
        recovery.read = real_read
        try:
            recovery.clear()
        except Exception:
            pass


@pytest.fixture(scope="module")
def main_mod(qapp):
    import main
    return main


# ── multi-monitor overlays ────────────────────────────────────────────────────
# A second monitor placed to the LEFT of the primary starts at a negative x.
# That is the case that breaks anything assuming the primary screen's origin,
# and it is this machine's actual setup.
VIRTUAL = QRect(-1920, 0, 3840, 1080)
PRIMARY = QRect(0, 0, 1920, 1080)


class _FakeScreen:
    def geometry(self):
        return PRIMARY

    def virtualGeometry(self):
        return VIRTUAL


class _FakeApp:
    @staticmethod
    def primaryScreen():
        return _FakeScreen()


@pytest.fixture
def two_monitors(main_mod, monkeypatch):
    """Make the code under test believe in a second monitor at negative x.

    Patching the name in main's globals rather than QApplication itself: the
    class is looked up at call time, and PySide6's real static methods cannot be
    reassigned.
    """
    monkeypatch.setattr(main_mod, "QApplication", _FakeApp)


@pytest.mark.parametrize("cls_name", ["RegionSelector", "ScreenshotSelector"])
def test_overlays_span_every_monitor(main_mod, two_monitors, cls_name):
    """Both full-screen overlays must cover the virtual desktop, not one screen.

    `primaryScreen().geometry()` looks correct on a single-monitor machine,
    because the primary screen starts at (0,0) and local coordinates therefore
    equal global ones. Add a monitor to the left and the overlay never appears
    there at all — the region simply cannot be selected. RegionSelector shipped
    that way from the initial commit.
    """
    w = getattr(main_mod, cls_name)()
    try:
        assert w.geometry() == VIRTUAL, (
            f"{cls_name} covers {w.geometry()}, not the whole virtual desktop "
            f"{VIRTUAL} — a monitor left of the primary is unreachable")
    finally:
        w.close()


class _FakeMouseEvent:
    """Enough of QMouseEvent for the handlers under test."""
    def __init__(self, gx, gy):
        self._g = QPoint(gx, gy)

    def button(self):
        return Qt.LeftButton

    def globalPos(self):
        return self._g

    def pos(self):  # local coords — wrong for these handlers, see the test
        return QPoint(0, 0)


@pytest.mark.parametrize("cls_name", ["RegionSelector", "ScreenshotSelector"])
def test_overlays_report_global_coordinates(main_mod, two_monitors, cls_name):
    """The emitted rectangle is consumed as screen coordinates.

    Local widget coordinates coincide with global ones only while the overlay
    sits at the origin, so a local-coordinate bug is invisible on the primary
    monitor and silently wrong everywhere else. `_FakeMouseEvent.pos()` returns
    (0,0) precisely so that a handler reading the wrong one fails loudly.
    """
    w = getattr(main_mod, cls_name)()
    got = []
    w.region_selected.connect(lambda x, y, ww, hh: got.append((x, y, ww, hh)))
    try:
        w.mousePressEvent(_FakeMouseEvent(-1800, 100))
        w.mouseMoveEvent(_FakeMouseEvent(-1500, 400))
        w.mouseReleaseEvent(_FakeMouseEvent(-1500, 400))
    finally:
        w.close()

    assert got == [(-1800, 100, 300, 300)], (
        f"{cls_name} emitted {got}, expected the selection in global "
        "coordinates on the negative-x monitor")


# ── the main window ───────────────────────────────────────────────────────────
@pytest.fixture
def window(main_mod):
    """A MainWindow that is never close()d.

    MainWindow.closeEvent ends the process with os._exit(0) on purpose — that is
    what guarantees no orphaned Macronaut is left holding a global hotkey hook
    after the window goes away. It also means a test calling close() terminates
    *pytest*, silently and with exit status 0, which reads as "the run stopped"
    rather than as a failure. hide() instead; the widget dies with the process.

    ⚠ It must still be SHUT DOWN, which is not the same thing. Every MainWindow
    constructs pynput listeners, and those install real low-level Windows
    keyboard hooks on threads of their own. hide() leaves them installed, so
    each test using this fixture leaked a few more — and at roughly seventy live
    hooks the suite died with a bare `Windows fatal exception: access violation`
    inside pynput's message loop, pointing at no test in particular. Adding two
    ordinary tests was enough to cross it.

    `_shutdown()` is the app's own teardown, is idempotent, and deliberately
    does NOT call os._exit — that is `closeEvent`'s doing, not its own.
    """
    w = main_mod.MainWindow()
    yield w
    try:
        w._shutdown()
    except Exception:
        pass
    w.hide()


def test_main_window_constructs(window):
    """The cheapest possible guard against a Qt-port regression.

    A binding change can break window construction outright, and the failure
    mode is the app not starting — which `--selftest` cannot see, because it
    never builds a window.
    """
    assert window.windowTitle()
    assert window.centralWidget() is not None


def test_closing_the_main_window_is_wired_to_a_hard_exit():
    """Pin the behaviour that makes the fixture above necessary.

    Deliberate: a background input listener or worker thread can outlive Qt's
    event loop, and a lingering process would keep a global hotkey registered.
    State is persisted before the exit. Recorded here because it is surprising
    enough to be 'tidied up' by someone who has not hit it.
    """
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "main.py"), "r", encoding="utf-8") as fh:
        src = code_only(fh.read())
    close_body = src.split("def closeEvent(self, event):", 1)[1][:600]
    assert "_save_state()" in close_body, "state must be saved before exiting"
    assert "_os._exit(0)" in close_body


def test_the_wheel_scrolls_settings_instead_of_editing_it(window):
    """Rolling over a control must not retune it.

    Qt delivers a wheel event to the widget under the pointer, and a combo, a
    spin box and a slider all read one as "change my value" — so scrolling past
    them silently changed the input backend, the typing speed and which script a
    launcher key runs, while the reader was looking further down the page.
    """
    from PySide6.QtWidgets import (QApplication, QComboBox, QAbstractSpinBox,
                                   QWidget)
    from PySide6.QtCore import QPoint, QPointF
    from PySide6.QtGui import QWheelEvent

    tab = window._settings_tab
    victims = [w for w in tab.findChildren(QWidget)
               if isinstance(w, (QComboBox, QAbstractSpinBox))]
    assert victims, "no controls found on the settings page"

    def _roll(w):
        ev = QWheelEvent(QPointF(5, 5), w.mapToGlobal(QPoint(5, 5)),
                         QPoint(0, -120), QPoint(0, -120), Qt.NoButton,
                         Qt.NoModifier, Qt.NoScrollPhase, False)
        QApplication.sendEvent(w, ev)

    for w in victims:
        w.clearFocus()
        before = (w.currentIndex() if isinstance(w, QComboBox) else w.text())
        _roll(w)
        after = (w.currentIndex() if isinstance(w, QComboBox) else w.text())
        assert before == after, \
            "%s changed from %r to %r on a wheel notch" % (
                type(w).__name__, before, after)


def test_no_palette_button_clips_its_label(window):
    """The Add-node sidebar has to fit the longest label, not the longest one
    that existed when its width was typed in.

    The width was a fixed 132 px chosen for eight buttons; adding "Comment" as
    the ninth clipped it. Nothing failed — Qt just drew a narrower button — so
    only a human looking at the sidebar could see it.
    """
    from PySide6.QtWidgets import QApplication, QPushButton

    btns = [b for b in window.findChildren(QPushButton)
            if b.objectName() == "palette_btn"]
    assert len(btns) >= 9, "expected the whole palette, got %d" % len(btns)
    # ⚠ Must be the width the *layout* gave it. Calling adjustSize() first would
    # resize each button to its own hint and make the assertion below true by
    # construction — which is a test that passes on the bug it is named for.
    window.show()
    QApplication.processEvents()
    try:
        for b in btns:
            assert b.width() >= b.sizeHint().width(), \
                "%r needs %d px and has %d" % (b.text(), b.sizeHint().width(),
                                               b.width())
    finally:
        window.hide()


def test_the_timeline_starts_folded(window):
    """Folded is the default. The strip is the one run-state view that shows
    nothing the canvas does not already show (it highlights the running node),
    so it should cost no space until someone asks for it."""
    assert window._sequence_tab._timeline.is_collapsed()


def test_unfolding_the_timeline_is_not_remembered(window):
    """It used to be, as settings.timeline_open — so one look at a run's timing
    left the strip unfolded in every later session. Opening it is a thing you do
    for the run in front of you, not a preference you hold.

    The assertion is on the *setting store*, not on the widget: a test that only
    re-read the widget would pass on the bug, since the strip does of course
    stay open for the rest of the session it was opened in.
    """
    import settings

    tab = window._sequence_tab
    assert not hasattr(settings.AppSettings(), "timeline_open"), \
        "the timeline fold is back in settings — it must not persist"

    before = dict(tab._settings.s.__dict__)
    tab._timeline.set_collapsed(False)
    assert not tab._timeline.is_collapsed()
    assert dict(tab._settings.s.__dict__) == before, \
        "unfolding the strip wrote something to settings"
    tab._timeline.set_collapsed(True)


def test_always_on_top_toggles_without_losing_the_window(window):
    """setWindowFlags() destroys and recreates the native window, so this is
    where a frameless window loses its frameless-ness -- or its position, since
    a re-shown window can land back at the default spot.

    There used to be two faces and this ran on every switch between them, which
    is what the frameless *flicker* was. One face means it only
    runs when the user asks for it, from Settings -> Appearance. The flicker
    itself still needs a human; there is just far less of it left to see.
    """
    from PySide6.QtCore import QRect
    window.setGeometry(QRect(120, 90, 900, 620))
    for cycle in range(3):
        for on in (True, False):
            window._apply_always_on_top(on)
            f = window.windowFlags()
            assert f & Qt.FramelessWindowHint, "cycle %d: lost frameless" % cycle
            assert bool(f & Qt.WindowStaysOnTopHint) is on, \
                "cycle %d: always-on-top did not follow the setting" % cycle
            assert window._settings.s.always_on_top is on
            assert window.geometry() == QRect(120, 90, 900, 620), \
                "cycle %d: the window moved when its flags changed" % cycle
    assert window.minimumSize() != window.maximumSize(), "must stay resizable"


def test_the_orb_is_still_an_orphan(qapp):
    """The Basic face came back on 28 August 2026; the orb did not.

    `orb.py` was never finished (FIXES #14 set `_ORB_ENABLED = False`) and every
    call site went with the 13 August removal. Importing it would put it back in
    the frozen build and resurrect a half-built feature nobody asked for.
    `compact.py` is deliberately absent from this list — it is a live module
    again, and `test_the_basic_face_is_wired_to_the_window` is what pins that.
    """
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("main.py", "compact.py", "flow_canvas.py", "flow_dialogs.py",
                 "settings.py"):
        with open(os.path.join(root, name), encoding="utf-8") as fh:
            src = code_only(fh.read())
        for bad in ("import orb", "from orb"):
            assert bad not in src, "%s has '%s'" % (name, bad)


# ── the Basic face ────────────────────────────────────────────────────────────
def test_the_basic_face_is_wired_to_the_window(window):
    """Both faces exist, and the window can actually be small on Basic.

    ⚠ Asserts the window's own minimum, not the stack's size hint. The hint is
    an implementation detail that turned out not to decide anything -- see
    `_FaceStack` -- and a test written against it would pass while the window
    that a user resizes stayed stuck at the canvas's floor.
    """
    import compact
    assert isinstance(window._compact, compact.CompactFace)
    assert window._stack.count() == 2

    window._show_advanced()
    assert window._stack.currentIndex() == window._stack.ADVANCED
    adv_floor = window.minimumSize().width()

    window._show_basic()
    assert window._stack.currentIndex() == window._stack.BASIC
    basic_floor = window.minimumSize().width()

    assert basic_floor < adv_floor, (
        "Basic cannot get smaller than the canvas (%d vs %d) — it is a pop-up "
        "meant to sit beside the window it is clicking" % (basic_floor, adv_floor))


def test_switching_face_never_touches_the_window_flags(window):
    """⚠ The regression this whole restoration had to avoid.

    `setWindowFlags` on a visible window is a destroy-and-recreate of the native
    window on Windows -- the frameless flicker. The old
    two-face shell called it on every switch, via `_apply_face_flags`, because
    Basic was always-on-top and Advanced was not. That distinction is gone:
    always-on-top is one setting for the whole window now. If a future change
    reintroduces per-face flags, this fails.
    """
    window._show_advanced()
    before = window.windowFlags()
    for _ in range(3):
        window._show_basic()
        assert window.windowFlags() == before, "showing Basic re-applied flags"
        window._show_advanced()
        assert window.windowFlags() == before, "showing Advanced re-applied flags"


def test_basic_opens_at_its_own_size_on_a_fresh_install(window):
    """⚠ Regression. A fresh QStackedWidget reports index 0, so the first
    `_show_advanced()` on startup believed it was leaving Basic and wrote the
    window's untouched 640x480 default into `basic_*`. Because a saved size
    beats the content fit, Basic then opened 140 px too wide forever after — at
    a size the user never chose, on the face that is somebody's first
    impression of the app.

    Nothing may record a face's geometry before that face has been shown.
    """
    s = window._settings.s
    s.basic_x = s.basic_y = s.basic_w = s.basic_h = -1
    window._live_face = None

    # Exactly what startup does when `last_face` is the default.
    window._show_advanced()
    assert (s.basic_w, s.basic_h) == (-1, -1), \
        "showing Advanced recorded a geometry for a face that was never shown"

    window._show_basic()
    assert window.width() == window._compact.sizeHint().width(), \
        "Basic did not open at its own content width"


def test_a_brand_new_install_opens_on_basic(tmp_path):
    """⚠ This is a promise the README makes in its first sentence: "It
    opens as a plain auto-clicker." Somebody who searched "auto clicker" and
    downloaded 78 MB must not be met by an empty canvas and a palette of nodes.

    Pinned at the settings layer rather than through a window, because the
    default and the migration beside it are the whole mechanism.
    """
    import json, settings as settings_mod
    old_dir, old_file = settings_mod.SETTINGS_DIR, settings_mod.SETTINGS_FILE
    try:
        settings_mod.SETTINGS_DIR = tmp_path
        settings_mod.SETTINGS_FILE = tmp_path / "settings.json"   # does not exist
        fresh = settings_mod.SettingsManager()
        assert fresh.s.last_face == "basic", \
            "a new install would open on the canvas, which the page denies"

        # ⚠ And an EXISTING user must not be moved. A settings.json with no
        # `last_face` was written by a release that had only the canvas; that
        # person has been using it for twenty-odd releases, and a first launch
        # after an update that silently swaps their window reads as a broken
        # update.
        (tmp_path / "settings.json").write_text(
            json.dumps({"theme": "mission", "advanced_w": 1200}),
            encoding="utf-8")
        upgraded = settings_mod.SettingsManager()
        assert upgraded.s.last_face == "advanced", \
            "an existing user was moved to Basic by an update"
        assert upgraded.s.advanced_w == 1200, "their canvas geometry was lost"
    finally:
        settings_mod.SETTINGS_DIR, settings_mod.SETTINGS_FILE = old_dir, old_file


def test_the_app_reopens_on_the_face_it_was_closed_on(window):
    """Closing in Basic reopens in Basic; closing in Advanced reopens in
    Advanced. `last_face` is written by the switch itself, so it is already
    correct whenever the app is asked to save."""
    window._show_basic()
    assert window._settings.s.last_face == "basic"
    window._show_advanced()
    assert window._settings.s.last_face == "advanced"


def test_each_face_keeps_its_own_geometry(window):
    """⚠ One shared rectangle meant switching face resized the other one, so
    neither ever stayed where it was put. Basic is a small pop-up parked beside
    the window it is clicking; Advanced is a large canvas."""
    from PySide6.QtCore import QRect
    from PySide6.QtWidgets import QApplication
    # ⚠ Sized to the screen the test is actually on. Both restore paths clamp
    # to `availableGeometry` -- 800x800 offscreen, a real monitor on a developer's
    # machine -- so a hardcoded 900 wide passes on one and silently clamps on the
    # other, which reads as the face having lost its geometry.
    scr = QApplication.primaryScreen().availableGeometry()
    adv = QRect(60, 40, min(700, scr.width() - 80), min(560, scr.height() - 80))
    # ⚠ And above the face's own floor. `CompactFace._PREF_W` is a deliberate
    # compression target (the layout's natural minimum is wider), so a rectangle
    # narrower than it comes back widened -- which looks like lost geometry.
    floor = window._compact.minimumSizeHint()
    bas = QRect(200, 100, floor.width() + 20, floor.height() + 20)

    window._show_advanced()
    window.setGeometry(adv)
    window._show_basic()
    window.setGeometry(bas)
    # Back to Advanced: its own rectangle, not the one Basic was just given.
    window._show_advanced()
    assert window.geometry() == adv
    # ...and back again.
    window._show_basic()
    assert window.geometry() == bas


def test_the_basic_face_saves_its_size_not_only_its_position(window):
    """The 2.0 face was `setFixedSize`d to its content and saved only x/y, so a
    user could not make it bigger and nothing would have remembered if they
    could."""
    from PySide6.QtCore import QRect
    window._show_basic()
    floor = window._compact.minimumSizeHint()
    w, h = floor.width() + 40, floor.height() + 30
    window.setGeometry(QRect(180, 90, w, h))
    window._save_state()
    s = window._settings.s
    assert (s.basic_x, s.basic_y) == (180, 90)
    assert (s.basic_w, s.basic_h) == (w, h)
    assert window.minimumSize() != window.maximumSize(), \
        "Basic must be resizable, not pinned to its content"


def test_saving_records_the_face_on_screen_not_the_one_in_settings(window):
    """⚠ `_show_*` writes `last_face` as PART of switching, so by the time
    anything else reads the setting it already names the face being switched
    TO. Saving off that setting writes the outgoing face's geometry into the
    incoming face's keys. The stack index is the only honest source."""
    from PySide6.QtCore import QRect
    window._show_advanced()
    window.setGeometry(QRect(60, 60, 700, 560))
    window._save_state()
    s = window._settings.s
    assert (s.advanced_x, s.advanced_y, s.advanced_w, s.advanced_h) == \
        (60, 60, 700, 560)


def test_the_basic_face_is_free_forever(qapp):
    """⚠ Basic must always run. A plain auto-clicker is what most people come for, so
    a Basic-shaped flow must run under FULL enforcement -- not merely today,
    while `entitlements.ENFORCED` happens to be False.

    It holds by construction rather than by exception: `autoclick` is in neither
    `PRO_ACTION_KINDS` nor `PRO_NODE_TYPES`, and one working step is far inside
    `FREE_MAX_STEPS`. If a future tier change swallows it, this is the alarm.
    """
    import flow, entitlements
    g = flow.FlowGraph()
    start = g.add_node(flow.N_START, {"name": flow.START_NAME}, x=-280, y=-20)
    node = g.add_node(flow.N_ACTION,
                      {"step": {"kind": "autoclick", "data": {}}}, x=0, y=120)
    g.add_edge(start.id, node.id, "out")

    assert entitlements.pro_features_used(g) == []
    assert entitlements.is_node_pro(node) is False
    assert entitlements.runs_on_free(g) is True

    import unittest.mock as _m
    with _m.patch.object(entitlements, "ENFORCED", True), \
            _m.patch.object(entitlements.licensing, "is_pro", lambda: False):
        ok, _msg, _feats = entitlements.check(g)
    assert ok, "the free auto-clicker was refused under enforcement"


def test_the_basic_face_follows_the_app_theme(window, main_mod):
    """It carried a hardcoded 2.0 palette until 28 August 2026, which is why it
    looked like a different application under Graphite or Daylight.

    ⚠ A widget stylesheet beats the application one, so this face does NOT
    follow the app sheet the way every other widget does -- `_apply_theme` has
    to hand it the palette explicitly.
    """
    # ⚠ `_apply_theme` sets the APPLICATION stylesheet and the module-global
    # CURRENT_THEME, so leaving it on Daylight silently re-measures every widget
    # in every test that runs after this one -- which is how this test first
    # broke `test_the_speed_box_is_wide_enough_for_its_largest_value`, 400 tests
    # later and nowhere near anything to do with themes.
    # ⚠ Restore the RAW application stylesheet, not just the theme name.
    # `_apply_theme` sets the app-wide sheet, and most of this suite runs with
    # none set at all -- so "put the theme back" still leaves every later widget
    # being built and measured under a stylesheet that was not there before.
    # That is the documented stylesheet-before-widget trap, and it broke
    # `test_the_speed_box_is_wide_enough_for_its_largest_value` 400 tests later,
    # nowhere near anything to do with themes.
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    was_sheet, was_theme = app.styleSheet(), main_mod.CURRENT_THEME
    try:
        sheets = {}
        for name in ("mission", "daylight"):
            window._apply_theme(name)
            sheets[name] = window._compact.styleSheet()
            pal = main_mod.PALETTES[name]
            assert pal["bg"].lower() in sheets[name].lower(), \
                "%s: the face is not painted in the theme's background" % name
        assert sheets["mission"] != sheets["daylight"]
    finally:
        main_mod.CURRENT_THEME = was_theme
        app.setStyleSheet(was_sheet)


def test_the_basic_face_writes_one_autoclick_node_into_an_empty_flow(window):
    """Basic and Advanced are two views of ONE document: Start pushes the face's
    settings into a single Auto-Click node and runs the same engine."""
    import flow
    window._sequence_tab._graph = window._sequence_tab._new_graph()
    assert window._is_basic_shaped() is True
    window._sync_autoclick_node()
    node = window._autoclick_node()
    assert node is not None, "no Auto-Click node was created"
    g = window._script()
    assert g.out_edge(g.start_node().id, "out") is not None, \
        "the node was created but never wired to Start"
    assert flow.action_kind(node) == "autoclick"


def test_pressing_start_on_an_untouched_basic_face_has_something_to_run(window):
    """⚠ A fresh launch straight into Basic has had no edit, so the
    config-changed signal that normally builds the Auto-Click node has never
    fired. Without a sync immediately before running, Start on a fully
    configured-looking clicker answers "Nothing to run"."""
    window._sequence_tab._graph = window._sequence_tab._new_graph()
    window._show_basic()
    assert window._sequence_tab.has_content() is False, "test set-up is stale"

    # Exactly what _do_start does before it checks for content.
    assert window._live_face == "basic"
    if window._live_face == "basic" and window._is_basic_shaped():
        window._sync_autoclick_node()

    assert window._sequence_tab.has_content() is True, \
        "Start on an untouched Basic face would say there is nothing to run"


def test_a_branching_flow_never_gets_a_click_injected_into_it(window):
    """⚠ 2.0.7's bug, and it must not come back with the face.

    A flow whose work lives in If / Loop / Go to holds no action node -- and
    reading "no action nodes" as "empty" made the old `_sync_autoclick_node`
    inject a click into somebody's real script. `flow.has_work` is the one
    definition of empty.
    """
    import flow
    g = window._sequence_tab._new_graph()
    g.add_node(flow.N_IF, {"name": "decide"}, x=0, y=120)
    window._sequence_tab._graph = g
    assert window._is_basic_shaped() is False
    window._sync_autoclick_node()
    assert window._autoclick_node() is None, \
        "an Auto-Click node was injected into a branching flow"


# ── themes ────────────────────────────────────────────────────────────────────
def test_a_theme_nobody_picked_follows_the_default(tmp_path, monkeypatch):
    """⚠ The migration that makes changing the default mean anything.

    `SettingsTab.save_to_settings` writes `theme` on every save whether or not
    anyone touched the picker, so every settings.json that has ever been written
    already pins whatever the default was that day. Without `theme_chosen`,
    moving the default to Cosmic would have left the entire installed base --
    including the developer, whose file said "mission" -- on the old theme they
    were asking to be rid of.
    """
    import json
    import settings as s

    def _load(data):
        f = tmp_path / "settings.json"
        f.write_text(json.dumps(data), encoding="utf-8")
        monkeypatch.setattr(s, "SETTINGS_FILE", f)
        return s.SettingsManager().s

    # Never picked: follows the default wherever it moves.
    assert _load({"theme": "mission"}).theme == s.DEFAULT_THEME
    # Picked on purpose: kept, even when it is the old default.
    assert _load({"theme": "mission", "theme_chosen": True}).theme == "mission"
    assert _load({"theme": "graphite", "theme_chosen": True}).theme == "graphite"
    # Hand-edited nonsense falls back rather than leaving no theme at all.
    assert _load({"theme": "banana", "theme_chosen": True}).theme == s.DEFAULT_THEME


def test_every_theme_is_registered_everywhere(main_mod):
    """A theme lives in four places — the order, the labels, the stylesheets and
    the raw palettes — and a picker that reads one while a painter reads another
    is how a half-themed window happens. They have to agree."""
    order = main_mod.THEME_ORDER
    assert set(order) == set(main_mod.THEMES), "THEMES disagrees with THEME_ORDER"
    assert set(order) == set(main_mod.PALETTES), "PALETTES disagrees with THEME_ORDER"
    assert set(order) == set(main_mod.THEME_LABELS), "labels disagree with THEME_ORDER"
    import settings as s
    assert set(order) == set(s.VALID_THEMES), \
        "settings.VALID_THEMES disagrees with the app's registry"
    assert main_mod.DEFAULT_THEME in order
    assert main_mod.DEFAULT_THEME == s.DEFAULT_THEME

    # Every palette must carry every token the stylesheet substitutes, or
    # Template.substitute raises at import for one theme and not the others.
    keys = [set(p) for p in main_mod.PALETTES.values()]
    assert all(k == keys[0] for k in keys), "the palettes do not share a key set"


def test_the_window_chrome_follows_the_theme(window, main_mod):
    """⚠ The canvas header, its grip and the settings drawer each set their OWN
    stylesheet, so none of them follows `app.setStyleSheet` the way an ordinary
    widget does. They were hardcoded to the 2.0 cosmic palette, which showed as
    a dark purple title bar sitting on top of the light theme."""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    was_sheet, was_theme = app.styleSheet(), main_mod.CURRENT_THEME
    try:
        for name in ("cosmic", "daylight"):
            window._apply_theme(name)
            pal = main_mod.PALETTES[name]
            hdr = window._adv_bar.styleSheet()
            assert pal["panel2"].lower() in hdr.lower(), \
                "%s: the canvas header is not wearing the theme" % name
            assert "#1d1b3f" not in hdr.lower(), \
                "%s: the hardcoded 2.0 header colour is back" % name
    finally:
        main_mod.CURRENT_THEME = was_theme
        app.setStyleSheet(was_sheet)

# ── system tray ───────────────────────────────────────────────────────────────
def test_tray_menu_has_its_actions(qapp):
    """QAction moved modules in Qt6, making this a likely silent breakage.

    Whether Windows actually draws the icon still needs a human; whether the
    menu was built and wired does not.
    """
    import tray
    t = tray.SystemTray()
    labels = [a.text() for a in t._tray.contextMenu().actions() if a.text()]
    joined = " ".join(labels)
    for expected in ("Start", "Stop", "Show", "Quit"):
        assert expected in joined, f"tray menu is missing {expected}: {labels}"
    t.hide()


def test_tray_reflects_running_state(qapp):
    """The status line is the only feedback when the window is hidden."""
    import tray
    t = tray.SystemTray()
    try:
        t.set_state(True)
        running = t._status_act.text()
        t.set_state(False)
        idle = t._status_act.text()
        assert running != idle, "the tray status text never changes"
        assert not t._start_act.isEnabled() or not t._stop_act.isEnabled(), \
            "Start and Stop should not both be available at once"
    finally:
        t.hide()


# ── the step editor opens on the family you asked for ─────────────────────────
# palette family -> (combo index it must land on, attribute of the visible panel)
_FAMILY_PANEL = {
    "click":  (0, "_stack_click"),
    "type":   (1, "_stack_key"),
    "wait":   (3, "_stack_wait"),
    "detect": (4, "_stack_imgwait"),
}


@pytest.mark.parametrize("family", sorted(_FAMILY_PANEL))
def test_step_dialog_opens_on_the_requested_family(main_mod, family):
    """Clicking Detect in the palette must open the Detect editor.

    This is a regression test for a PySide6 port bug that reached four published
    releases. `QButtonGroup.buttonClicked[int]` was a PyQt5-only overload; Qt6
    removed it, so the old spelling raised IndexError *while constructing the
    dialog*. Both families with a segmented toggle — Type and Detect — therefore
    opened nothing, leaving an unconfigured action node on the canvas that
    renders as a Click, so the symptom read as "Detect gives me a Click node I
    have to convert by hand".

    Constructing the dialog at all is most of the value here; asserting the
    panel keeps it honest about *which* editor appeared.
    """
    want_index, panel_attr = _FAMILY_PANEL[family]
    dlg = main_mod.StepDialog(None, default_text_cps=20, family=family)
    try:
        dlg.show()
        assert dlg._type_combo.currentIndex() == want_index
        assert getattr(dlg, panel_attr).isVisible(), \
            f"{family} opened, but not on its own editor"
    finally:
        dlg.hide()


def test_detect_toggle_switches_between_image_text_and_pixel(main_mod):
    """The segmented toggle is the thing the broken signal was wired to."""
    dlg = main_mod.StepDialog(None, default_text_cps=20, family="detect")
    try:
        dlg.show()
        dlg._fam_group.button(1).click()
        assert dlg._type_combo.currentIndex() == 5, "Wait for text not selected"
        assert dlg._stack_textwait.isVisible()
        dlg._fam_group.button(2).click()
        assert dlg._type_combo.currentIndex() == 6, "Wait for pixel not selected"
    finally:
        dlg.hide()


def _key_ev(qtkey, press=True, autorep=False):
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QKeyEvent
    return QKeyEvent(QEvent.KeyPress if press else QEvent.KeyRelease,
                     qtkey, Qt.NoModifier, "", autorep)


def _chord(dlg, *qtkeys):
    """Press every key, then let go of them in reverse — one hand, one chord."""
    from PySide6.QtWidgets import QApplication
    dlg._start_key_capture()
    for k in qtkeys:
        QApplication.sendEvent(dlg, _key_ev(k, press=True))
    for k in reversed(qtkeys):
        QApplication.sendEvent(dlg, _key_ev(k, press=False))


def test_the_key_editor_captures_two_plain_keys_at_once(main_mod):
    """W+A must be expressible in one node.

    Capture used to return early on Ctrl/Shift/Alt/Meta "waiting for a real
    key" and then take exactly one non-modifier from a QKeySequence — so a
    chord of two ordinary keys, which is what movement in a game is, could not
    be typed into a node at all.
    """
    dlg = main_mod.StepDialog(None, default_text_cps=20, family="type")
    try:
        dlg.show()
        _chord(dlg, Qt.Key_W, Qt.Key_A)
        assert dlg._captured_keys == ["w", "a"]
        assert dlg._capturing_key is False, "letting go must end the capture"
    finally:
        dlg.hide()


def test_the_type_editor_round_trips_how_the_text_is_sent(main_mod):
    """The Send-as segment is stored as flow's constant, not a button index.

    And a Type step written before it existed has to reopen on Automatic — it
    has been typing that way all along, and anything else would silently
    retarget every saved flow at one kind of receiver.
    """
    import flow
    from recorder import SeqStep

    dlg = main_mod.StepDialog(SeqStep(SeqStep.TEXT, {"text": "hi"}, 0),
                              default_text_cps=20, family="type")
    try:
        assert dlg._text_send_grp.checkedId() == 0          # Automatic
        dlg._text_send_grp.button(2).setChecked(True)       # Key presses
        dlg._on_ok()
        assert dlg._result_step.data["send_as"] == flow.SEND_KEYS
    finally:
        dlg.hide()

    dlg = main_mod.StepDialog(
        SeqStep(SeqStep.TEXT, {"text": "hi", "send_as": flow.SEND_CHARS}, 0),
        default_text_cps=20, family="type")
    try:
        assert dlg._text_send_grp.checkedId() == 1
    finally:
        dlg.hide()


def test_capturing_a_chord_still_puts_modifiers_first(main_mod):
    # The engine presses keys[:-1] before keys[-1], so Ctrl+C only works if
    # ctrl is not last — regardless of which key the fingers hit first.
    dlg = main_mod.StepDialog(None, default_text_cps=20, family="type")
    try:
        dlg.show()
        _chord(dlg, Qt.Key_C, Qt.Key_Control)
        assert dlg._captured_keys == ["ctrl", "c"]
    finally:
        dlg.hide()


def test_a_bare_modifier_is_now_a_capturable_key(main_mod):
    # "Hold down shift" is a real thing to want, and the old guard made it
    # impossible to even record.
    dlg = main_mod.StepDialog(None, default_text_cps=20, family="type")
    try:
        dlg.show()
        _chord(dlg, Qt.Key_Shift)
        assert dlg._captured_keys == ["shift"]
    finally:
        dlg.hide()


def test_auto_repeat_does_not_end_a_capture_early(main_mod):
    # A key held long enough to add a second one emits release/press pairs.
    # Acting on those would end the capture while it is still being made.
    from PySide6.QtWidgets import QApplication
    dlg = main_mod.StepDialog(None, default_text_cps=20, family="type")
    try:
        dlg.show()
        dlg._start_key_capture()
        QApplication.sendEvent(dlg, _key_ev(Qt.Key_W, press=True))
        QApplication.sendEvent(dlg, _key_ev(Qt.Key_W, press=False, autorep=True))
        QApplication.sendEvent(dlg, _key_ev(Qt.Key_W, press=True, autorep=True))
        assert dlg._capturing_key is True
        QApplication.sendEvent(dlg, _key_ev(Qt.Key_A, press=True))
        QApplication.sendEvent(dlg, _key_ev(Qt.Key_A, press=False))
        QApplication.sendEvent(dlg, _key_ev(Qt.Key_W, press=False))
        assert dlg._captured_keys == ["w", "a"]
    finally:
        dlg.hide()


def test_escape_cancels_a_capture_and_keeps_the_old_keys(main_mod):
    from PySide6.QtWidgets import QApplication
    dlg = main_mod.StepDialog(None, default_text_cps=20, family="type")
    try:
        dlg.show()
        _chord(dlg, Qt.Key_W)
        dlg._start_key_capture()
        QApplication.sendEvent(dlg, _key_ev(Qt.Key_A, press=True))
        QApplication.sendEvent(dlg, _key_ev(Qt.Key_Escape, press=True))
        assert dlg._capturing_key is False
        assert dlg._captured_keys == ["w"]
    finally:
        dlg.hide()


def test_the_key_editor_hides_the_rows_a_mode_cannot_use(main_mod):
    """Repeat is meaningless for Hold down, and so is a hold time."""
    import flow
    dlg = main_mod.StepDialog(None, default_text_cps=20, family="type")
    try:
        dlg.show()
        for mode, hold, repeat in ((flow.KEY_TAP,  False, True),
                                   (flow.KEY_HOLD, True,  True),
                                   (flow.KEY_DOWN, False, False),
                                   (flow.KEY_UP,   False, False)):
            dlg._key_mode.setCurrentIndex(dlg.KEY_MODES.index(mode))
            assert dlg._key_hold_row.isVisible() is hold, f"hold row, {mode}"
            assert dlg._key_repeat_row.isVisible() is repeat, f"repeat row, {mode}"
    finally:
        dlg.hide()


def test_a_pre_modes_key_step_reopens_as_the_mode_it_always_was(main_mod):
    import flow
    from recorder import SeqStep
    dlg = main_mod.StepDialog(SeqStep(SeqStep.KEY, {"keys": ["w"], "hold_ms": 2500}),
                              default_text_cps=20, family="type")
    try:
        dlg.show()
        assert dlg.KEY_MODES[dlg._key_mode.currentIndex()] == flow.KEY_HOLD
        assert dlg._key_hold_ms.value() == 2500
    finally:
        dlg.hide()


# ══════════════════════════════════════════════════════════════════════════════
#  The timeline strip and the run-state drawing on the canvas
# ══════════════════════════════════════════════════════════════════════════════

def _chain(*kinds):
    """Start -> one action per kind -> End, wired in a line."""
    import flow
    g = flow.FlowGraph()
    prev = g.add_node(flow.N_START, {})
    made = []
    for kind, data in kinds:
        n = g.add_node(flow.N_ACTION, {"step": {"kind": kind, "data": data}})
        g.add_edge(prev.id, n.id)
        prev = n
        made.append(n)
    end = g.add_node(flow.N_END, {})
    g.add_edge(prev.id, end.id)
    return g, made


def test_a_long_flow_overflows_the_lane_and_says_so(qapp):
    """The strip used to normalise its widths so it could never overflow.

    The objection was that overflow hides the end of the flow, which is where
    End is and where an unreleased key's bar would be. That is answered by an
    affordance rather than a squeeze now: MIN_SEG_W is a hard floor, the lane
    pans, and max_scroll() is positive exactly when there is something off the
    edge. So a short flow still fits — nothing gained a scrollbar it does not
    need — and a 60-node flow gets boxes you can read instead of sixty slivers.
    """
    import flow_timeline
    for n in (1, 20, 60):
        g, _ = _chain(*[("wait", {"ms": 100 * (i % 7 + 1)}) for i in range(n)])
        strip = flow_timeline.TimelineStrip(g)
        strip.resize(900, 90)
        for mode in (strip.ORDER, strip.TIME):
            strip.set_mode(mode)
            boxes = strip._layout()
            assert len(boxes) == n + 2, f"{n} nodes, {mode}"
            assert min(r.width() for _, r in boxes) >= flow_timeline.MIN_SEG_W - 0.01, \
                f"{n} nodes went under the floor in {mode}"
            over = strip._content_w > strip._avail() + 0.5
            assert (strip.max_scroll() > 0.5) == over, \
                f"{n} nodes in {mode}: overflow {over}, max_scroll {strip.max_scroll()}"
            if n == 1:
                assert not over, "a one-node flow should still fit"


def test_every_node_can_be_scrolled_into_the_lane(qapp):
    """Overflow is only acceptable because the end is reachable."""
    import flow_timeline
    g, _ = _chain(*[("wait", {"ms": 100}) for _ in range(60)])
    strip = flow_timeline.TimelineStrip(g)
    strip.resize(900, 90)
    strip._layout()                      # max_scroll() reads what _layout measures
    assert strip.max_scroll() > 0.5

    seen = set()
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        strip._set_scroll(strip.max_scroll() * frac)
        vp = strip._viewport()
        for nid, r in strip._layout():
            if r.left() >= vp.left() - 0.5 and r.right() <= vp.right() + 0.5:
                seen.add(nid)
    assert seen == set(strip._order), \
        f"{len(set(strip._order)) - len(seen)} node(s) unreachable by scrolling"

    # Panned to the end, the last node — End — is against the right edge, and
    # the pan can never overshoot into empty lane.
    strip._set_scroll(1e9)
    assert abs(strip._scroll - strip.max_scroll()) < 0.01
    last = strip._layout()[-1][1]
    assert abs(last.right() - strip._viewport().right()) < 1.0


def test_the_strip_folds_to_its_header_and_remembers_nothing_else(qapp):
    """Collapsing is the run log's bargain: give the canvas back, keep the row."""
    import flow_timeline
    g, _ = _chain(("wait", {"ms": 100}), ("wait", {"ms": 100}))
    strip = flow_timeline.TimelineStrip(g)
    strip.resize(900, 90)
    open_h = strip.height()

    got = []
    strip.collapsed_changed.connect(got.append)
    strip.set_collapsed(True)
    assert strip.is_collapsed() and got == [True]
    assert strip.height() < open_h
    # Folded, it hit-tests nothing: no boxes, so no click can land on a node
    # that is not drawn.
    assert strip._hit_boxes() == []

    strip.set_collapsed(True)            # idempotent — no second signal
    assert got == [True]
    strip.set_collapsed(False)
    assert not strip.is_collapsed() and got == [True, False]
    assert strip.height() == open_h


def test_the_time_axis_is_proportional_but_order_is_not(qapp):
    import flow_timeline
    g, made = _chain(("wait", {"ms": 100}), ("wait", {"ms": 900}))
    strip = flow_timeline.TimelineStrip(g)
    strip.resize(900, 90)

    strip.set_mode(strip.ORDER)
    w = strip._widths(800.0)
    assert w[made[0].id] == pytest.approx(w[made[1].id]), "order must be even"

    strip.set_mode(strip.TIME)
    w = strip._widths(800.0)
    assert w[made[1].id] > w[made[0].id] * 4, "900 ms must dwarf 100 ms"


def test_an_unbounded_node_cannot_squeeze_the_known_ones(qapp):
    # A Detect with no timeout contributes no weight. Giving it a share of the
    # axis would mean inventing a duration for the one node that has none.
    import flow_timeline
    g, made = _chain(("wait", {"ms": 1000}),
                     ("wait_image", {"image_path": "x.png"}))
    strip = flow_timeline.TimelineStrip(g)
    strip.resize(900, 90)
    strip.set_mode(strip.TIME)
    w = strip._widths(800.0)
    assert w[made[1].id] <= flow_timeline.MIN_SEG_W + 0.5
    assert w[made[0].id] > w[made[1].id] * 5


def test_a_measurement_changes_the_width_of_an_unknown_node(qapp):
    """The whole point of 'more accurate after the first run'."""
    import flow_timeline
    g, made = _chain(("wait", {"ms": 1000}),
                     ("wait_image", {"image_path": "x.png"}))
    strip = flow_timeline.TimelineStrip(g)
    strip.resize(900, 90)
    strip.set_mode(strip.TIME)
    before = strip._widths(800.0)[made[1].id]
    strip.set_measured({made[1].id: 4000})
    after = strip._widths(800.0)[made[1].id]
    assert after > before * 3


def test_clicking_a_box_in_the_strip_asks_for_that_node(qapp):
    import flow_timeline
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication
    g, made = _chain(("wait", {"ms": 100}), ("wait", {"ms": 100}))
    strip = flow_timeline.TimelineStrip(g)
    strip.resize(900, 90)
    strip.show()
    got = []
    strip.node_clicked.connect(got.append)
    boxes = dict(strip._layout())
    c = boxes[made[1].id].center()
    ev = QMouseEvent(QEvent.MouseButtonPress, QPointF(c), Qt.LeftButton,
                     Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(strip, ev)
    assert got == [made[1].id]
    strip.hide()


def test_the_wheel_pans_the_lane_and_stops_at_both_ends(qapp):
    """A plain vertical wheel pans: the strip has one axis, so nothing else it
    could mean, and there is nothing to pan on a flow that already fits."""
    import flow_timeline
    from PySide6.QtCore import QEvent, QPoint, QPointF
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication

    def wheel(strip, notches):
        pos = strip._viewport().center()
        ev = QWheelEvent(pos, strip.mapToGlobal(QPoint(int(pos.x()), int(pos.y()))),
                         QPoint(0, 0), QPoint(0, 120 * notches),
                         Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
        QApplication.sendEvent(strip, ev)
        return ev.isAccepted()

    g, _ = _chain(*[("wait", {"ms": 100}) for _ in range(60)])
    strip = flow_timeline.TimelineStrip(g)
    strip.resize(900, 90)
    strip._layout()
    assert wheel(strip, -1)                     # wheel down -> further along
    assert strip._scroll > 0
    first = strip._scroll
    wheel(strip, -1)
    assert strip._scroll > first
    for _ in range(80):                         # ram it into the end
        wheel(strip, -1)
    assert abs(strip._scroll - strip.max_scroll()) < 0.01
    for _ in range(200):
        wheel(strip, 1)
    assert strip._scroll == 0.0

    short, _ = _chain(("wait", {"ms": 100}))
    tiny = flow_timeline.TimelineStrip(short)
    tiny.resize(900, 90)
    tiny._layout()
    assert not wheel(tiny, -1), "a flow that fits must let the wheel through"
    assert tiny._scroll == 0.0


def test_the_chevron_folds_the_strip_and_the_thumb_pans_it(qapp):
    """The two hand-drawn controls answer the mouse, not just the painter."""
    import flow_timeline
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    def press(strip, pt):
        QApplication.sendEvent(strip, QMouseEvent(
            QEvent.MouseButtonPress, QPointF(pt), Qt.LeftButton,
            Qt.LeftButton, Qt.NoModifier))

    g, _ = _chain(*[("wait", {"ms": 100}) for _ in range(60)])
    strip = flow_timeline.TimelineStrip(g)
    strip.resize(900, 90)
    strip.grab()                                 # paint once: the rects come from it
    assert not strip._chev_rect.isNull() and not strip._thumb_rect.isNull()

    # Clicking bare track past the thumb jumps the lane that way.
    track = strip._track_rect
    press(strip, QPointF(track.right() - 4, track.center().y()))
    assert strip._scroll > 0
    QApplication.sendEvent(strip, QMouseEvent(
        QEvent.MouseButtonRelease, QPointF(track.center()), Qt.LeftButton,
        Qt.NoButton, Qt.NoModifier))
    assert strip._grab is None

    press(strip, strip._chev_rect.center())
    assert strip.is_collapsed()
    # Folded, the chevron is still there and still the only thing that answers.
    strip.grab()
    got = []
    strip.node_clicked.connect(got.append)
    press(strip, QPointF(strip.width() / 2, strip.height() / 2))
    assert got == []
    press(strip, strip._chev_rect.center())
    assert not strip.is_collapsed()


def test_the_strip_grows_a_lane_for_each_held_key(qapp):
    import flow_timeline
    g, made = _chain(("key", {"keys": ["w"], "mode": "down"}))
    strip = flow_timeline.TimelineStrip(g)
    strip.resize(900, 90)
    idle = strip.height()
    strip.run_started()
    strip.set_held([("w", made[0].id)])
    one = strip.height()
    strip.set_held([("w", made[0].id), ("a", made[0].id)])
    two = strip.height()
    assert idle < one < two
    strip.run_finished()
    assert strip.height() == idle


def test_the_strip_follows_a_graph_edited_under_it(qapp):
    """A crash report from the field: KeyError 'n39' out of `_estimates`.

    The strip worked out its node order when it was handed a graph and never
    again. Nothing tells it about an edit — the canvas mutates the same
    FlowGraph object in place — so with the strip unfolded, deleting a node
    left its id in the order and the next paint looked it up in a graph that
    no longer had it. The quieter half of the same bug: a node added on the
    canvas never appeared in the strip until the script was reloaded.
    """
    import flow
    import flow_timeline
    g, made = _chain(("wait", {"ms": 100}), ("wait", {"ms": 200}),
                     ("wait", {"ms": 300}))
    strip = flow_timeline.TimelineStrip(g)
    strip.resize(900, 90)
    for mode in (strip.TIME, strip.ORDER):
        strip.set_mode(mode)
        assert made[1].id in dict(strip._layout())

    g.remove_node(made[1].id)            # what Delete on the canvas does
    for mode in (strip.TIME, strip.ORDER):
        strip.set_mode(mode)
        boxes = dict(strip._layout())    # TIME used to raise KeyError here
        assert made[1].id not in boxes, f"a deleted node still has a box ({mode})"
        assert set(strip._estimates()) == set(boxes)
    strip.grab()                         # and a real paint gets through

    added = g.add_node(flow.N_ACTION,
                       {"step": {"kind": "wait", "data": {"ms": 400}}})
    g.add_edge(made[0].id, added.id)
    assert added.id in dict(strip._layout()), \
        "a node added on the canvas never reached the strip"

    # Rewiring changes the order without changing what exists.
    before = list(strip._order)
    g.add_edge(g.start_node().id, made[2].id)
    strip._layout()
    assert strip._order != before and strip._order == [
        nid for nid in flow.linearise(g)
        if g.nodes[nid].type not in flow.ANNOTATION_TYPES]


def test_a_running_node_gets_a_bar_and_an_unbounded_one_shimmers(qapp):
    import flow_canvas
    g, made = _chain(("wait", {"ms": 5000}),
                     ("wait_image", {"image_path": "x.png"}))
    canvas = flow_canvas.FlowCanvas(g)
    scene = canvas._scene

    canvas.begin_node(made[0].id)
    item = scene.node_item(made[0].id)
    assert item.progress is not None and item.progress != flow_canvas.PROGRESS_UNKNOWN

    # Nothing can say how long this one takes, so it must not pretend to.
    canvas.begin_node(made[1].id)
    assert scene.node_item(made[1].id).progress == flow_canvas.PROGRESS_UNKNOWN
    assert item.progress is None, "the previous node's bar must be cleared"

    canvas.end_run()
    assert all(scene.node_item(n.id).progress is None for n in made)


def test_a_predicted_bar_stops_short_rather_than_sitting_full(qapp):
    # The bar is a prediction (flow.estimate) and predictions run out. Parked
    # just short is usefully saying "this is running long"; parked at 100% for
    # a node that has not finished is a lie.
    import flow_canvas
    g, made = _chain(("wait", {"ms": 1}))
    canvas = flow_canvas.FlowCanvas(g)
    item = canvas._scene.node_item(made[0].id)
    canvas.begin_node(made[0].id)
    canvas._scene._run_t0 -= 10.0          # pretend ten seconds went by
    canvas._scene._tick()
    assert item.progress > 1.0             # the raw fraction is allowed to run on
    assert flow_canvas.PROGRESS_CAP < 1.0  # ...but paint clamps it


def test_a_node_whose_key_is_still_down_stays_marked(qapp):
    import flow_canvas
    g, made = _chain(("key", {"keys": ["w"], "mode": "down"}),
                     ("wait", {"ms": 100}))
    canvas = flow_canvas.FlowCanvas(g)
    item = canvas._scene.node_item(made[0].id)

    canvas.set_live({made[0].id: ["w"]})
    assert item.live_keys == ["w"]
    assert "W" in item.toolTip()

    # A rebuild throws the items away — but the key is still down whatever the
    # canvas did, so the mark has to survive it.
    canvas._scene.rebuild()
    assert canvas._scene.node_item(made[0].id).live_keys == ["w"]

    canvas.end_run()
    assert canvas._scene.node_item(made[0].id).live_keys == []


def test_no_per_step_progress_signal_exists(qapp):
    """The bar must stay locally animated.

    A progress signal per step is the 2.0.8 log flood rebuilt: the interpreter
    drives ~692k events/sec and the queue fills orders of magnitude faster than
    the GUI drains it. The engine says "node X started" and the canvas works out
    the rest, so there is nothing here that scales with how fast a flow runs.
    """
    import flow_exec
    from PySide6.QtCore import Signal
    sigs = {n for n in dir(flow_exec.FlowWorker)
            if isinstance(getattr(flow_exec.FlowWorker, n, None), Signal)}
    # node_started is the only per-node signal, and it fires once per node.
    # `progress` is the auto-click counter — bounded by the click rate, not by
    # how fast the interpreter can walk a graph — and predates this.
    assert {s for s in sigs if "node" in s} == {"node_started"}, sigs
    assert not {s for s in sigs if "node" in s and "progress" in s}


def test_no_pyqt5_only_indexed_signal_overloads():
    """`signal[int].connect(...)` is a PyQt5 spelling that Qt6 may not offer.

    It fails at connect time rather than at import, so it hides until a user
    opens the one dialog that uses it. Cheap to forbid outright — the Qt6
    replacements (idClicked, currentIndexChanged, …) are named signals.
    """
    import glob
    import os
    import re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    offenders = []
    for path in glob.glob(os.path.join(root, "*.py")):
        with open(path, "r", encoding="utf-8") as fh:
            src = code_only(fh.read())
        for m in re.finditer(r"(\w+)\[(?:int|str|object)\]\s*\.connect", src):
            offenders.append(f"{os.path.basename(path)}: {m.group(0)}")
    assert not offenders, f"PyQt5-only indexed signal overload: {offenders}"


# ── node defaults ─────────────────────────────────────────────────────────────
def test_new_nodes_get_no_automatic_pre_delay():
    """A node must not cost time the user never asked for.

    New nodes used to be seeded with `delay_before_ms = 500`, and the
    "Delay before…" dialog defaulted to 500 when the key was absent — so merely
    opening it to look turned into adding half a second. On a long flow that is
    seconds of invisible latency the user has to hunt down node by node.

    Searches code, not comments — the explanation above and in main.py both name
    the old value on purpose. See code_only().
    """
    import os
    import re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    offenders = []
    for name in ("main.py", "flow_canvas.py", "flow.py"):
        with open(os.path.join(root, name), "r", encoding="utf-8") as fh:
            src = code_only(fh.read())
        for m in re.finditer(r"delay_before_ms\"?\s*,\s*(\d+)", src):
            if m.group(1) != "0":
                offenders.append(f"{name}: default {m.group(1)}")
        for m in re.finditer(r"setdefault\(\s*[\"']delay_before_ms[\"']\s*,\s*(\d+)",
                             src):
            if m.group(1) != "0":
                offenders.append(f"{name}: setdefault {m.group(1)}")
    assert not offenders, f"nodes are being given a pre-delay nobody asked for: {offenders}"


# ── Qt6 port pins ─────────────────────────────────────────────────────────────
def test_qaction_is_imported_from_qtgui(main_mod):
    """QAction moved QtWidgets -> QtGui in Qt6.

    Importing it from the old place raises at import time, so this mostly guards
    against someone 'fixing' an import back to the Qt5 spelling.
    """
    import tray
    assert main_mod.QAction.__module__ == "PySide6.QtGui"
    assert tray.QAction is main_mod.QAction


def test_high_dpi_rounding_policy_is_passthrough():
    """Checked in the source, because the call must precede QApplication.

    Qt6 rounds fractional scale factors by default, so 125% becomes 100% and the
    compact face — which is sized to its content and then fixed — comes out
    wrong. By the time any test has a QApplication it is far too late to observe
    the setting being applied, so pin the call site instead.
    """
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "main.py"), "r", encoding="utf-8") as fh:
        src = fh.read()
    assert re.search(r"setHighDpiScaleFactorRoundingPolicy\(\s*\n?\s*"
                     r"Qt\.HighDpiScaleFactorRoundingPolicy\.PassThrough", src), \
        "the compact face mis-sizes at 125%/150% scaling without PassThrough"


def test_no_overlay_uses_primary_screen_geometry():
    """Belt and braces for the bug above, at the source level.

    The behavioural test needs the fake screen to catch it; this catches the
    substitution itself, including anywhere a future overlay copies the pattern.

    Both overlays carry a comment explaining why geometry() is wrong here, so
    this searches code only — see code_only().
    """
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "main.py"), "r", encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    offenders = [f"main.py:{i}" for i, ln in enumerate(lines, 1)
                 if "primaryScreen().geometry()" in ln
                 and not ln.strip().startswith("#")]
    assert not offenders, (
        f"{offenders}: a full-screen overlay must use virtualGeometry(); "
        "geometry() covers only the primary monitor")


# ── a new node looks like what you asked for, before you press OK ─────────────
def test_new_node_shows_its_own_family_while_its_editor_is_still_open(window,
                                                                     monkeypatch):
    """Adding from the Detect palette button used to leave a Click-coloured node
    on the canvas until the editor was confirmed, because the family only lived
    inside the step — and there is no step yet while the dialog is open.

    _edit_node is stubbed out because it opens a modal dialog; everything this
    test cares about happens before that call.
    """
    import flow_canvas
    tab = window._sequence_tab
    monkeypatch.setattr(tab, "_edit_node", lambda *a, **k: None)

    for emit, want_title in [("action:wait_image", "Detect"),
                             ("action:key", "Type"),
                             ("action:wait", "Wait"),
                             ("action:click", "Click")]:
        tab._add_node(emit, 0, 0)
        node = tab._graph.nodes[tab._canvas.scene_()._last_added]
        assert node.data.get("preset_kind") == emit.split(":", 1)[1]
        _icon, title = flow_canvas.node_header_label(node)
        assert title == want_title, f"{emit} drew as {title}"
        assert node.summary() == "not set yet"


def test_the_family_preset_never_survives_into_a_saved_flow(window, monkeypatch):
    """It is a drawing hint, not data — a confirmed step supersedes it."""
    tab = window._sequence_tab
    monkeypatch.setattr(tab, "_edit_node", lambda *a, **k: None)
    tab._add_node("action:wait_image", 0, 0)
    node = tab._graph.nodes[tab._canvas.scene_()._last_added]

    # What _edit_node does on OK, without the modal dialog.
    node.data["step"] = {"kind": "wait_text", "data": {"text": "hi"}}
    node.data.pop("preset_kind", None)
    import flow
    assert flow.action_kind(node) == "wait_text"
    assert "preset_kind" not in json.dumps(tab._graph.to_dict())


# ── copy / paste on the real canvas ──────────────────────────────────────────
@pytest.fixture
def canvas(qapp):
    import flow, flow_canvas
    g = flow.FlowGraph()
    g.add_node(flow.N_START, {"name": flow.START_NAME}, x=0, y=0)
    c = flow_canvas.FlowCanvas(g)
    yield c
    c.hide()


def test_canvas_copy_paste_round_trips_through_the_system_clipboard(canvas):
    import flow
    g = canvas.graph
    a = g.add_node(flow.N_ACTION, {"step": {"kind": "wait", "data": {"ms": 750}}})
    b = g.add_node(flow.N_ACTION, {"step": {"kind": "wait", "data": {"ms": 250}}})
    g.add_edge(a.id, b.id)
    canvas.scene_().rebuild()

    for nid in (a.id, b.id):
        canvas.scene_().node_item(nid).setSelected(True)
    assert canvas.copy_selection() == 2
    assert canvas.can_paste()
    assert canvas.paste() == 2

    assert len(g.nodes) == 5          # start + 2 originals + 2 copies
    waits = sorted(n.data["step"]["data"]["ms"] for n in g.nodes.values()
                   if n.data.get("step"))
    assert waits == [250, 250, 750, 750]
    # The copies are what stays selected — the next drag should move them.
    assert len(canvas.selected_node_ids()) == 2


@pytest.fixture
def no_slot_exceptions():
    """Fail if any Qt slot raises.

    An exception inside a slot invoked from C++ does NOT propagate to whatever
    called into Qt — PySide6 hands it to sys.excepthook and carries on. So a
    test that simply calls the method under test passes while the app is
    quietly throwing on every invocation. This is the only way to see it, and
    the reason the EdgeItem crash below reached a release: nothing failed, the
    canvas kept working, and only the 2.0.9 crash reporter noticed.
    """
    caught = []
    real = sys.excepthook
    sys.excepthook = lambda *a: caught.append(a)
    try:
        yield caught
    finally:
        sys.excepthook = real


def test_rebuilding_the_canvas_does_not_touch_deleted_edge_items(
        canvas, qapp, no_slot_exceptions):
    """Regression: shiboken RuntimeError on a selected edge during rebuild().

    QGraphicsScene.clear() destroys the C++ items and emits selectionChanged as
    it deselects them, synchronously. _on_selection_changed re-pens every edge
    in _edges, so clearing that dict *after* clear() left it walking wrappers
    whose C++ half was already gone:

        RuntimeError: libshiboken: Internal C++ object (EdgeItem) already
        deleted.

    Captured in the field on 2.0.9 before this was understood. A step edit that
    changes a node's ports calls rebuild() with the canvas still selected,
    which is the ordinary way in.
    """
    import flow
    g = canvas.graph
    a = g.add_node(flow.N_ACTION, {"step": {"kind": "wait", "data": {"ms": 100}}})
    b = g.add_node(flow.N_ACTION, {"step": {"kind": "wait", "data": {"ms": 200}}})
    g.add_edge(a.id, b.id)
    scene = canvas.scene_()
    scene.rebuild()

    # Something is selected, as it is after any click, marquee or highlight.
    for item in list(scene._edges.values()) + list(scene._nodes.values()):
        item.setSelected(True)
    assert scene._edges, "need a live edge for this to mean anything"

    scene.rebuild()
    qapp.processEvents()

    assert not no_slot_exceptions, (
        "a slot raised during rebuild: %r" % (no_slot_exceptions[0][1],))
    # And the rebuild still did its job.
    assert len(scene._edges) == 1
    assert len(scene._nodes) == len(g.nodes)


@pytest.mark.parametrize("text", [
    "just some text I copied from a browser",
    '{"nodes": [{"type": "action", "id": "n1"}], "edges": []}',   # JSON, not ours
])
def test_pasting_unrelated_clipboard_text_does_nothing(canvas, text):
    """Paste reads the system clipboard, which can hold literally anything —
    including JSON that happens to have a "nodes" key."""
    from PySide6.QtGui import QGuiApplication
    QGuiApplication.clipboard().setText(text)
    before = len(canvas.graph.nodes)
    assert canvas.can_paste() is False
    assert canvas.paste() == 0
    assert len(canvas.graph.nodes) == before


def test_duplicate_leaves_the_clipboard_alone(canvas):
    import flow
    from PySide6.QtGui import QGuiApplication
    QGuiApplication.clipboard().setText("precious")
    a = canvas.graph.add_node(flow.N_ACTION, {"step": {"kind": "wait",
                                                       "data": {"ms": 10}}})
    canvas.scene_().rebuild()
    canvas.scene_().node_item(a.id).setSelected(True)
    assert canvas.duplicate_selection() == 1
    assert QGuiApplication.clipboard().text() == "precious"


def test_the_start_node_can_never_be_copied_from_the_canvas(canvas):
    import flow
    start = canvas.graph.start_node()
    canvas.scene_().node_item(start.id).setSelected(True)
    assert canvas.copy_selection() == 0
    assert canvas.duplicate_selection() == 0
    assert len([n for n in canvas.graph.nodes.values()
                if n.type == flow.N_START]) == 1


# ── control-flow dialogs still construct under Qt6 ───────────────────────────
@pytest.mark.parametrize("cls_name,args", [
    ("IfDialog", ({"condition": {"type": "image"}},)),
    ("LoopDialog", ({"mode": "while"},)),
    ("GotoDialog", ({}, ["start"])),
    ("OnErrorDialog", ({}, ["start"])),
    ("BulkEditDialog", (7, 2)),
])
def test_every_flow_dialog_constructs(qapp, cls_name, args):
    """Same class of bug as the Detect palette one: a Qt5-only signal spelling
    blows up while *building* a dialog, which no import check can see."""
    import flow_dialogs
    dlg = getattr(flow_dialogs, cls_name)(*args)
    try:
        dlg.show()
        assert dlg.windowTitle()
    finally:
        dlg.hide()


def test_bulk_edit_only_reports_the_rows_that_were_ticked(qapp):
    """The safety property of the whole feature: an untouched row must not be
    written, or "apply to all" would reset settings the user never mentioned."""
    import flow_dialogs
    dlg = flow_dialogs.BulkEditDialog(5, 0)
    try:
        assert dlg.ops() == {}
        assert dlg.selection_only() is False   # nothing selected -> all nodes
        dlg._c_timeout.setChecked(True)
        dlg._timeout_s.setValue(42)
        assert dlg.ops() == {"timeout_s": 42}
        dlg._c_delay.setChecked(True)
        dlg._delay_mode.setCurrentIndex(1)     # "add"
        dlg._delay_ms.setValue(-100)
        assert dlg.ops() == {"timeout_s": 42, "delay_add_ms": -100}
    finally:
        dlg.hide()


# ── dialogs resize to fit what they reveal ───────────────────────────────────
def test_step_dialog_grows_to_fit_the_rows_it_reveals(main_mod, qapp):
    """Filling in a Detect step reveals rows — the window has to follow.

    adjustSize() does not: the layout still reports the size hint it cached
    before the preview and the click options appeared, so the dialog ended up
    ~30 px shorter than its own content and the bottom row was cut off. This
    test measures exactly that — height against the live size hint — so it fails
    against the old behaviour rather than merely describing it.
    """
    dlg = main_mod.StepDialog(None, default_text_cps=20, family="detect")
    try:
        dlg.show()
        dlg._imgwait_path.setText("some-image.png")   # reveals the preview
        dlg._imgwait_do_click.setChecked(True)        # reveals button/double
        qapp.processEvents()
        assert dlg.height() >= dlg.sizeHint().height(), \
            "the dialog is shorter than its own content — the last row is clipped"
    finally:
        dlg.hide()


def test_step_dialog_shrinks_again_when_a_smaller_panel_is_chosen(main_mod, qapp):
    dlg = main_mod.StepDialog(None, default_text_cps=20, family="detect")
    try:
        dlg.show(); qapp.processEvents()
        tall = dlg.height()
        dlg._fam_group.button(2).click()   # Wait for pixel — the smallest panel
        qapp.processEvents()
        assert dlg.height() < tall, "switching to a smaller panel left dead space"
    finally:
        dlg.hide()


# ── the click editor's segmented controls still round-trip ───────────────────
@pytest.mark.parametrize("data", [
    {"button": "left",   "x": 1,  "y": 2,  "clicks": 1, "hold": False, "hold_ms": 1000},
    {"button": "right",  "x": 10, "y": 20, "clicks": 2, "hold": False, "hold_ms": 1000},
    {"button": "middle", "x": -5, "y": 7,  "clicks": 1, "hold": True,  "hold_ms": 2500},
])
def test_click_step_round_trips_through_the_segmented_controls(main_mod, data):
    """Double-click and Hold used to be two checkboxes that unticked each other;
    they are one segmented control now. The stored step must be unchanged."""
    step = main_mod.SeqStep(main_mod.SeqStep.CLICK, dict(data), 300)
    dlg = main_mod.StepDialog(step, default_text_cps=20, family="click")
    try:
        dlg.show()
        assert dlg._click_hold_row.isVisible() is bool(data["hold"]), \
            "the Hold-for row must appear exactly for the Hold mode"
        dlg._on_ok()
        out = dlg.step.to_dict()
        assert out["data"] == data
        assert out["delay_ms"] == 300, "the click's own pre-delay was dropped"
    finally:
        dlg.hide()


@pytest.mark.parametrize("kind,data", [
    ("wait_image", {"image_path": "x.png", "confidence": 0.7, "timeout_s": 9,
                    "click": True, "button": "right", "clicks": 2,
                    "offset_x": 0, "offset_y": 0,
                    "region": (100, 200, 300, 400)}),
    ("wait_text", {"text": "hello", "timeout_s": 3, "click": True,
                   "button": "middle", "clicks": 1, "case_sensitive": True,
                   "fuzzy": False}),
    ("wait_pixel", {"x": 4, "y": 5, "color": "#102030", "tolerance": 22,
                    "timeout_s": 11, "click": True}),
])
def test_detect_steps_survive_the_relaid_out_panels(main_mod, kind, data):
    """The three Detect panels were rebuilt around a shared skeleton. Every
    setting has to come back out of them unchanged."""
    step = main_mod.SeqStep(kind, dict(data), 0)
    dlg = main_mod.StepDialog(step, default_text_cps=20, family="detect")
    try:
        dlg.show()
        dlg._on_ok()
        out = dlg.step.to_dict()["data"]
        assert {k: out.get(k) for k in data} == data
    finally:
        dlg.hide()


# ── no container widget paints over the card it sits in ──────────────────────
_TRANSPARENT_NAMES = {"fieldRow", "segWrap"}


def opaque_containers(dlg):
    """Layout-only container widgets that would paint their own background.

    `type(w) is QWidget` on purpose: a subclass draws itself and is not what this
    is about. `w.layout() is not None` narrows it to widgets that exist to hold
    a layout, and the "qt_" prefix drops Qt's own plumbing (combo-box popups,
    scroll-area containers) — neither is ours to name.
    """
    from PySide6.QtWidgets import QWidget
    return [w for w in dlg.findChildren(QWidget)
            if type(w) is QWidget and w.layout() is not None
            and not w.objectName().startswith("qt_")
            and w.objectName() not in _TRANSPARENT_NAMES]


@pytest.mark.parametrize("cls_name,args", [
    ("IfDialog", ({"condition": {"type": "image"}},)),
    ("LoopDialog", ({"mode": "while"},)),
    ("OnErrorDialog", ({}, ["start"])),
    ("BulkEditDialog", (7, 2)),
])
def test_dialog_containers_do_not_paint_over_their_card(qapp, cls_name, args):
    """The stylesheet sets `background: $bg` on every QWidget, so a plain
    container inside a QGroupBox paints a near-black rectangle over it — which
    is what put a big black square inside the If editor. Layout-only containers
    must carry a name the stylesheet makes transparent.
    """
    import flow_dialogs
    dlg = getattr(flow_dialogs, cls_name)(*args)
    try:
        assert not opaque_containers(dlg), f"opaque container(s) in {cls_name}"
        # ConditionWidget is a QWidget subclass, so the blanket rule matches it
        # too — it is the one that actually sat inside the If editor's box.
        conds = dlg.findChildren(flow_dialogs.ConditionWidget)
        assert all(c.objectName() in _TRANSPARENT_NAMES for c in conds)
    finally:
        dlg.hide()


@pytest.mark.parametrize("family", [None, "click", "type", "wait", "detect"])
def test_step_dialog_containers_do_not_paint_over_their_card(main_mod, family):
    dlg = main_mod.StepDialog(None, default_text_cps=20, family=family)
    try:
        assert not opaque_containers(dlg), "opaque container(s) in StepDialog"
    finally:
        dlg.hide()


# ── one place to name a node, not two ────────────────────────────────────────
@pytest.mark.parametrize("cls_name,args", [
    ("IfDialog", ({"name": "keep me", "condition": {"type": "image"}},)),
    ("LoopDialog", ({"name": "keep me", "mode": "while"},)),
])
def test_node_editors_no_longer_offer_a_second_place_to_name_a_node(qapp,
                                                                    cls_name, args):
    """Naming lives in right-click → Name… for every node type. Leaving the key
    out of data() is also what makes node.data.update(dlg.data()) keep the name
    the node already had."""
    import flow_dialogs
    dlg = getattr(flow_dialogs, cls_name)(*args)
    try:
        assert "name" not in dlg.data(), \
            "the editor would overwrite the node's name with its own field"
    finally:
        dlg.hide()


# ── Canvas: uniform nodes, marquee select, Detect → If promotion ─────────────
def test_every_step_node_is_drawn_at_the_same_height(canvas):
    """One-port and two-port nodes used to differ by 20 px, which read as an
    accident. Go to was the visibly short one — and showing a thumbnail must not
    make an image node the new one."""
    import flow
    g = canvas.graph
    for ntype, data in [(flow.N_GOTO, {"target_name": "start"}),
                        (flow.N_IF, {"condition": {"type": "always"}}),
                        (flow.N_LOOP, {"mode": "times", "times": 2}),
                        (flow.N_ACTION, {"step": {"kind": "click", "data": {}}}),
                        (flow.N_ACTION, {"step": {"kind": "wait_image",
                                                  "data": {"image_path": "a.png"}}}),
                        (flow.N_ACTION, {"step": {"kind": "wait_text",
                                                  "data": {"text": "hi"}}})]:
        g.add_node(ntype, data)
    canvas.scene_().rebuild()
    import flow_canvas
    heights = {it.boundingRect().height()
               for it in canvas.scene_()._nodes.values() if not it.terminal}
    assert heights == {float(flow_canvas.NODE_H)}


def test_the_thumbnail_fits_the_body_the_node_already_had(canvas):
    """It earns its place by fitting, not by growing. Three sides are measured,
    not guessed — the widest port label ("⚠ error", 77 px, right-aligned to
    x=172) starts at x≈95, and the retry / pre-delay badges own the last 18 px.
    If the well crosses either, it is painting over something."""
    import flow_canvas as fc
    from PySide6.QtGui import QFont, QFontMetrics
    f = QFont(); f.setPointSize(8); f.setBold(True)
    fm = QFontMetrics(f)
    label_left = fc.NODE_W - 12 - max(fm.horizontalAdvance(t)
                                      for t in fc.PORT_LABELS.values())
    assert fc.THUMB_LEFT + fc.THUMB_W <= label_left, "well runs into the port label"
    assert fc.THUMB_TOP + fc.THUMB_H <= fc.NODE_H - 18, "well runs into the badge row"
    assert fc.THUMB_TOP >= fc.HEADER_H, "well runs into the header"
    # ...and the caption beside it fits its column and stops above the label.
    fcap = QFont(); fcap.setPointSize(7)
    cap_w = QFontMetrics(fcap).horizontalAdvance("click ×2")
    assert fc.CAPTION_LEFT >= fc.THUMB_LEFT + fc.THUMB_W
    assert cap_w <= fc.NODE_W - fc.CAPTION_LEFT - 10, "the caption is clipped"
    assert fc.THUMB_TOP + fc.CAPTION_H <= 54, "the caption lands on the port label"


def test_the_picture_replaces_the_filename_rather_than_joining_it(canvas):
    """The filename was only ever a stand-in for the picture. Showing both is
    what left the node with nothing to say and no room to say it."""
    import flow
    g = canvas.graph
    n = g.add_node(flow.N_ACTION, {"step": {"kind": "wait_image",
                                            "data": {"image_path": "a.png"}}})
    canvas.scene_().rebuild()
    assert "a.png" in n.summary(), "the run log still names the file"
    item = canvas.scene_().node_item(n.id)
    assert item.thumb_path == "a.png"    # ...but the node draws it, not its name


def test_only_the_clicking_of_an_image_still_needs_a_word(canvas):
    """Which image it is, is visible now. Whether the step also clicks it is
    not, and that is the difference between watching and acting."""
    import flow, flow_canvas as fc
    mk = lambda d: flow.FlowNode("n", flow.N_ACTION,
                                 {"step": {"kind": "wait_image", "data": d}})
    assert fc.thumb_caption(mk({"image_path": "a.png"})) == ""
    assert fc.thumb_caption(mk({"image_path": "a.png", "click": True})) == "click"
    assert fc.thumb_caption(mk({"image_path": "a.png", "click": True,
                                "clicks": 2})) == "click ×2"
    assert fc.thumb_caption(flow.FlowNode("i", flow.N_IF, {
        "condition": {"type": "image", "image_path": "a.png"}})) == ""


def test_the_thumbnail_follows_the_image_a_node_actually_watches(canvas):
    """Including an If — wiring both outputs of an image Detect node promotes it
    into one, and the picture must not vanish at that moment."""
    import flow, flow_canvas as fc
    assert fc.node_image_path(flow.FlowNode("a", flow.N_ACTION, {
        "step": {"kind": "wait_image", "data": {"image_path": "a.png"}}})) == "a.png"
    assert fc.node_image_path(flow.FlowNode("b", flow.N_IF, {
        "condition": {"type": "image", "image_path": "b.png"}})) == "b.png"
    assert fc.node_image_path(flow.FlowNode("c", flow.N_LOOP, {
        "mode": "while",
        "condition": {"type": "image", "image_path": "c.png"}})) == "c.png"
    # A repeat-N loop doesn't test anything, even if a stale condition is left
    # in its data, and a text detect has no picture to show.
    assert fc.node_image_path(flow.FlowNode("d", flow.N_LOOP, {
        "mode": "repeat_n", "count": 3,
        "condition": {"type": "image", "image_path": "d.png"}})) == ""
    assert fc.node_image_path(flow.FlowNode("e", flow.N_ACTION, {
        "step": {"kind": "wait_text", "data": {"text": "hi"}}})) == ""


def test_repointing_a_node_at_another_image_follows_without_a_rebuild(canvas):
    """The path is cached on the item, so a repaint alone would keep drawing the
    old picture. Nothing about geometry changed, though, so refresh() re-reads
    it rather than throwing the item away."""
    import flow
    g = canvas.graph
    n = g.add_node(flow.N_ACTION, {"step": {"kind": "wait_image",
                                            "data": {"image_path": "before.png"}}})
    canvas.scene_().rebuild()
    item = canvas.scene_().node_item(n.id)
    n.data["step"]["data"]["image_path"] = "after.png"
    canvas.scene_().refresh_node(n.id)
    assert canvas.scene_().node_item(n.id) is item, "the item was rebuilt for nothing"
    assert item.thumb_path == "after.png"


def test_start_and_end_are_drawn_as_bars_that_keep_the_column(canvas):
    """Start and End hold no settings, so a full card was 52 px of blank. They
    are bars now — but the same width, the same left edge and the same port x,
    so the flow still lines up in one column."""
    import flow, flow_canvas
    g = canvas.graph
    end = g.add_node(flow.N_END, {}, x=0, y=300)
    canvas.scene_().rebuild()
    start = g.start_node()
    for nid in (start.id, end.id):
        it = canvas.scene_().node_item(nid)
        assert it.terminal
        assert it.boundingRect().height() == float(flow_canvas.TERMINAL_H)
        assert it.boundingRect().width() == float(flow_canvas.NODE_W)
    # Ports stay on the node's own edges, vertically centred on the bar.
    s_item = canvas.scene_().node_item(start.id)
    out = s_item.port_pos("out") - s_item.scenePos()
    assert (out.x(), out.y()) == (float(flow_canvas.NODE_W),
                                  flow_canvas.TERMINAL_H / 2)
    e_item = canvas.scene_().node_item(end.id)
    inp = e_item.port_pos("in") - e_item.scenePos()
    assert (inp.x(), inp.y()) == (0.0, flow_canvas.TERMINAL_H / 2)
    # And they still paint (the terminal branch is a separate paint path).
    canvas.grab()


def _send_mouse(canvas, kind, pos, button, buttons):
    """Post a mouse event the way the window system would.

    Straight to viewport().event() does not work: QGraphicsView receives its
    mouse events through QAbstractScrollArea's viewport filter, so bypassing
    the filter means mousePressEvent never runs and the test passes vacuously.
    """
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtCore import Qt
    QApplication.sendEvent(
        canvas.viewport(),
        QMouseEvent(kind, pos, canvas.viewport().mapToGlobal(pos),
                    button, buttons, Qt.NoModifier))


def _selected_canvas(canvas):
    import flow
    a = canvas.graph.add_node(flow.N_ACTION,
                              {"step": {"kind": "wait", "data": {"ms": 1}}},
                              x=0, y=0)
    canvas.scene_().rebuild()
    canvas.resize(600, 400)
    canvas.scene_().node_item(a.id).setSelected(True)
    assert canvas.selected_node_ids() == [a.id]
    return a


def test_clicking_empty_canvas_clears_the_selection(canvas):
    from PySide6.QtCore import QPoint, QEvent, Qt
    _selected_canvas(canvas)
    far = QPoint(560, 360)          # empty canvas, well clear of any node
    assert canvas._node_at(far) is None
    _send_mouse(canvas, QEvent.MouseButtonPress, far, Qt.LeftButton, Qt.LeftButton)
    _send_mouse(canvas, QEvent.MouseButtonRelease, far, Qt.LeftButton, Qt.NoButton)
    assert canvas.selected_node_ids() == []


def test_dragging_the_empty_canvas_pans_without_dropping_the_selection(canvas):
    from PySide6.QtCore import QPoint, QEvent, Qt
    a = _selected_canvas(canvas)
    start, end = QPoint(560, 360), QPoint(460, 300)
    _send_mouse(canvas, QEvent.MouseButtonPress, start, Qt.LeftButton, Qt.LeftButton)
    _send_mouse(canvas, QEvent.MouseMove, end, Qt.NoButton, Qt.LeftButton)
    _send_mouse(canvas, QEvent.MouseButtonRelease, end, Qt.LeftButton, Qt.NoButton)
    assert canvas.selected_node_ids() == [a.id]


def test_right_drag_onto_an_already_connected_node_removes_the_wire(canvas):
    """The wiring gesture is its own undo: repeating it on a connected pair
    used to replace the wire with an identical one, which was a no-op."""
    import flow
    g = canvas.graph
    a = g.add_node(flow.N_ACTION, {"step": {"kind": "wait", "data": {"ms": 1}}})
    b = g.add_node(flow.N_END, {})
    canvas.scene_().rebuild()

    assert canvas.scene_().connect_ports(a.id, "out", b.id, toggle=True) is True
    assert [(e.src, e.dst) for e in g.edges] == [(a.id, b.id)]
    assert canvas.scene_().connect_ports(a.id, "out", b.id, toggle=True) is False
    assert [(e.src, e.dst) for e in g.edges] == []
    assert canvas.scene_()._edges == {}      # and the drawn curve went with it


def test_toggling_only_removes_the_wire_you_gestured_at(canvas):
    """Re-aiming a port at a different node still moves the wire — only the
    exact pair you already had comes undone."""
    import flow
    g = canvas.graph
    a = g.add_node(flow.N_ACTION, {"step": {"kind": "wait", "data": {"ms": 1}}})
    b = g.add_node(flow.N_END, {})
    c = g.add_node(flow.N_END, {})
    canvas.scene_().rebuild()
    canvas.scene_().connect_ports(a.id, "out", b.id, toggle=True)
    assert canvas.scene_().connect_ports(a.id, "out", c.id, toggle=True) is True
    assert [(e.src, e.dst) for e in g.edges] == [(a.id, c.id)]


def test_ctrl_drag_selects_the_nodes_and_the_wires_between_them(canvas):
    from PySide6.QtCore import QRectF
    import flow
    g = canvas.graph
    # Clear of the fixture's Start node, which sits at 0,0 and is 184 px wide.
    a = g.add_node(flow.N_ACTION, {"step": {"kind": "wait", "data": {"ms": 1}}},
                   x=208, y=0)
    b = g.add_node(flow.N_ACTION, {"step": {"kind": "wait", "data": {"ms": 2}}},
                   x=468, y=0)
    far = g.add_node(flow.N_END, {}, x=3000, y=3000)
    g.add_edge(a.id, b.id)
    g.add_edge(b.id, far.id)
    canvas.scene_().rebuild()

    n = canvas.select_in_rect(QRectF(190, -40, 600, 300))
    assert set(canvas.selected_node_ids()) == {a.id, b.id}
    assert n == 3                       # two nodes + the wire joining them
    edges = canvas.scene_()._edges
    picked = {e.edge.src for e in edges.values() if e.isSelected()}
    assert picked == {a.id}             # a->b only; b->far leaves the box


def test_a_marquee_that_misses_everything_clears_the_selection(canvas):
    from PySide6.QtCore import QRectF
    import flow
    a = canvas.graph.add_node(flow.N_END, {}, x=0, y=0)
    canvas.scene_().rebuild()
    canvas.scene_().node_item(a.id).setSelected(True)
    assert canvas.select_in_rect(QRectF(5000, 5000, 100, 100)) == 0
    assert canvas.selected_node_ids() == []


def test_wiring_both_outputs_of_a_detect_node_makes_it_an_if_node(canvas):
    import flow
    g = canvas.graph
    det = g.add_node(flow.N_ACTION,
                     {"step": {"kind": "wait_text",
                               "data": {"text": "READY", "timeout_s": 4}}})
    yes = g.add_node(flow.N_END)
    no = g.add_node(flow.N_END)
    canvas.scene_().rebuild()

    canvas.scene_().connect_ports(det.id, "out", yes.id)
    assert canvas._maybe_promote_detect(det.id) is False   # one wire is not a branch
    assert det.type == flow.N_ACTION

    canvas.scene_().connect_ports(det.id, "error", no.id)
    assert canvas._maybe_promote_detect(det.id) is True
    assert det.type == flow.N_IF
    assert det.data["condition"] == {"type": "text", "text": "READY",
                                     "timeout_s": 4}
    ports = {(e.src, e.dst): e.src_port for e in g.edges}
    assert ports[(det.id, yes.id)] == "true"
    assert ports[(det.id, no.id)] == "false"
    # The canvas caught up: the node now draws If's two coloured branches.
    assert canvas.scene_().node_item(det.id).out_ports == ["true", "false"]


def test_editing_a_detect_node_into_a_click_takes_its_error_wire_with_it(canvas):
    import flow
    g = canvas.graph
    det = g.add_node(flow.N_ACTION,
                     {"step": {"kind": "wait_image", "data": {"image_path": "a.png"}}})
    end = g.add_node(flow.N_END)
    canvas.scene_().rebuild()
    canvas.scene_().connect_ports(det.id, "out", end.id)
    canvas.scene_().connect_ports(det.id, "error", end.id)
    assert len(g.edges) == 2

    det.data["step"] = {"kind": "click", "data": {"x": 1, "y": 2}}
    canvas.scene_().refresh_node(det.id)      # what _edit_node does on OK

    assert [e.src_port for e in g.edges] == ["out"]
    assert canvas.scene_().node_item(det.id).out_ports == ["out"]


# ── Footer polish ────────────────────────────────────────────────────────────
def test_the_speed_box_is_wide_enough_for_its_largest_value(window):
    """The '×' suffix used to fall off the end of a fixed-width box."""
    sb = window._sequence_tab._speed
    sb.setValue(sb.maximum())
    need = sb.fontMetrics().horizontalAdvance(sb.text())
    assert sb.minimumWidth() - need >= 42     # border + padding + arrow column
    # ...and no wider than that. Sizing for a second decimal nobody can type
    # (the step is 0.1) cost 18 px and made the footer look unbalanced.
    assert sb.decimals() == 1
    assert sb.minimumWidth() - need == 42


def test_a_flow_that_branches_instead_of_acting_still_counts_as_runnable(window):
    """Promoting a flow's only Detect node to an If/Else leaves zero action
    nodes. Play used to answer that with 'Nothing to run'."""
    import flow
    tab = window._sequence_tab
    g = tab.graph
    assert not tab.has_content()
    n = g.add_node(flow.N_IF, {"condition": {"type": "image",
                                             "image_path": "a.png"}})
    assert tab.has_content()
    g.remove_node(n.id)
    g.add_node(flow.N_END, {})
    assert not tab.has_content()      # scaffolding alone is still nothing to run


def test_an_auto_click_node_can_still_be_opened(window):
    """The Basic face was the only place this node could be edited, and when
    that went, double-clicking it answered with a message box pointing at a
    face that no longer existed. It is a real node in real saved flows -- and a
    node the user cannot open is a mistake this codebase has made before."""
    import flow_dialogs
    data = {"button": "right", "max_speed": False, "interval_ms": 250,
            "click_limit": 40, "human_mode": True, "jitter_px": 7}
    dlg = flow_dialogs.AutoClickDialog(data)
    try:
        out = dlg.result_data()
    finally:
        dlg.deleteLater()
    assert out["button"] == "right"
    assert out["interval_ms"] == 250
    assert out["click_limit"] == 40
    # Fields the editor does not show are carried through untouched, so opening
    # an old node and pressing OK cannot silently drop its settings.
    assert out["human_mode"] is True and out["jitter_px"] == 7


@pytest.fixture
def no_editor(monkeypatch, main_mod):
    """_add_node opens the step editor modally; the wiring it does first is
    what these tests are about."""
    monkeypatch.setattr(main_mod.SequenceTab, "_edit_node",
                        lambda *a, **k: None)


def test_the_first_node_added_to_an_empty_flow_wires_to_start(window, no_editor):
    import flow, flow_canvas
    tab = window._sequence_tab
    g = tab.graph
    start = g.start_node()
    assert g.out_edge(start.id, "out") is None
    tab._add_node("action:wait", 500.0, 500.0)
    added = [n for n in g.nodes.values() if n.type == flow.N_ACTION]
    assert len(added) == 1
    edge = g.out_edge(start.id, "out")
    assert edge is not None and edge.dst == added[0].id
    # It also lands beside Start rather than where the click happened, centred
    # on Start's bar so the wire runs straight.
    node = added[0]
    assert node.x > start.x
    s_item = tab._canvas.scene_().node_item(start.id)
    n_item = tab._canvas.scene_().node_item(node.id)
    centres = (start.y + s_item.boundingRect().height() / 2,
               node.y + n_item.boundingRect().height() / 2)
    assert abs(centres[0] - centres[1]) <= flow_canvas.GRID / 2


def test_a_second_node_chains_onto_the_first_not_back_onto_start(window,
                                                                 no_editor):
    import flow
    tab = window._sequence_tab
    g = tab.graph
    start = g.start_node()
    tab._add_node("action:wait", 0.0, 0.0)
    first = g.out_edge(start.id, "out").dst
    tab._add_node("action:wait", 0.0, 0.0)
    assert g.out_edge(start.id, "out").dst == first     # Start keeps its wire
    nxt = g.out_edge(first, "out")
    assert nxt is not None and nxt.dst != first


def test_the_overall_settings_button_says_what_it_is(window):
    from PySide6.QtWidgets import QPushButton
    labels = [b.text() for b in window._sequence_tab.findChildren(QPushButton)]
    assert "Overall settings" in labels
    assert not any(t.startswith("Overall") and t.endswith("…") for t in labels)


def test_macronaut_opens_on_the_canvas(window):
    """There is only one face now, and it is the node builder."""
    assert window.centralWidget() is not None
    assert window._sequence_tab._canvas is not None


def test_the_go_to_icon_is_presented_like_every_other_node_icon():
    """U+21AA has a text glyph in the UI font, so without the emoji variation
    selector Go to alone rendered small and thin next to its peers."""
    import flow
    assert "\ufe0f" in flow.node_icon(flow.N_GOTO)


def test_the_auto_chain_anchor_survives_a_port_changing_edit(canvas):
    """Saving a step can change a node's ports, which rebuilds the scene in the
    middle of adding that node. The rebuild must not forget which node the next
    palette add is supposed to chain onto."""
    import flow
    scene = canvas.scene_()
    n = canvas.graph.add_node(flow.N_ACTION, {"preset_kind": "wait_image"})
    scene.rebuild()
    scene._last_added = n.id
    n.data = {"step": {"kind": "click", "data": {"x": 1, "y": 2}}}
    scene.refresh_node(n.id)                  # ports shrink -> full rebuild
    assert scene._last_added == n.id


# ══════════════════════════════════════════════════════════════════════════════
#  2.0.8 — image capture everywhere, the segmented search area, and the crash
#  fixes around a running flow.
# ══════════════════════════════════════════════════════════════════════════════

# ── every image field can capture, not just browse ───────────────────────────
def _buttons(widget):
    from PySide6.QtWidgets import QAbstractButton
    return [b.text() for b in widget.findChildren(QAbstractButton)]


def test_the_if_else_image_condition_can_capture_from_the_screen(qapp):
    """Capturing a crop off the screen is how these images get made; browsing
    to a file you already have is the rare case. ConditionWidget offered only
    the rare one — and it is the whole of the If/Else, Loop-while and
    Loop-until editors, so one missing button meant three dialogs."""
    import flow_dialogs
    w = flow_dialogs.ConditionWidget({"type": "image"})
    try:
        texts = _buttons(w)
        assert any("Capture" in t for t in texts), texts
        assert any("Browse" in t for t in texts), texts
        assert any("Test match" in t for t in texts), texts
    finally:
        w.hide()


def test_the_detect_step_editor_can_still_capture(main_mod):
    dlg = main_mod.StepDialog(None, default_text_cps=20, family="detect")
    try:
        assert any("Capture" in t for t in _buttons(dlg))
    finally:
        dlg.hide()


# ── search area: the selected segment IS the state ───────────────────────────
def _region_selects(widget):
    import flow_dialogs
    return widget.findChildren(flow_dialogs.RegionSelect)


def test_every_search_area_control_is_the_shared_segmented_one(main_mod):
    """Three places asked "whole screen or a region?" and all three answered in
    a status label beside two identical-looking push buttons. One control now,
    and the answer is which half is filled."""
    import flow_dialogs
    dlg = flow_dialogs.IfDialog({"condition": {"type": "image"}})
    step = main_mod.StepDialog(None, default_text_cps=20, family="detect")
    try:
        assert len(_region_selects(dlg)) == 2      # image panel + text panel
        assert len(_region_selects(step)) == 2     # image panel + text panel
    finally:
        dlg.hide(); step.hide()


def test_no_dialog_still_narrates_the_search_area_in_prose(main_mod):
    """The old giveaway string. If it comes back, so has the label."""
    from PySide6.QtWidgets import QLabel
    import flow_dialogs
    dlg = flow_dialogs.IfDialog({"condition": {"type": "image"}})
    step = main_mod.StepDialog(None, default_text_cps=20, family="detect")
    try:
        for w in (dlg, step):
            for lbl in w.findChildren(QLabel):
                assert "Region set" not in lbl.text()
    finally:
        dlg.hide(); step.hide()


def test_the_region_half_carries_the_size_and_takes_the_selection(qapp):
    import flow_dialogs
    rs = flow_dialogs.RegionSelect()
    try:
        assert rs.region() is None
        assert rs._whole.isChecked() and not rs._pick.isChecked()
        rs.set_region((-1900, 20, 320, 140))
        assert rs.region() == (-1900, 20, 320, 140)
        assert rs._pick.isChecked() and not rs._whole.isChecked()
        assert "320×140" in rs._pick.text()
        rs.set_region(None)
        assert rs._whole.isChecked()
        assert "320" not in rs._pick.text()
    finally:
        rs.hide()


def test_a_cancelled_region_pick_leaves_the_choice_where_it_was(main_mod,
                                                                monkeypatch):
    """Clicking the region half moves the selection before the overlay has said
    anything, so a cancel has to put it back — otherwise cancelling reads as
    "region", with no region."""
    import flow_dialogs
    rs = flow_dialogs.RegionSelect()
    try:
        rs.set_region((0, 0, 100, 50))
        monkeypatch.setattr(
            main_mod, "_launch_region_picker",
            lambda on_region, parent_window=None, on_cancel=None: on_cancel())
        rs._choose_region()
        assert rs.region() == (0, 0, 100, 50)
        assert rs._pick.isChecked()
    finally:
        rs.hide()


def test_a_saved_region_survives_a_round_trip_through_the_condition_editor(qapp):
    import flow_dialogs
    cond = {"type": "image", "image_path": "a.png", "region": [-1900, 5, 64, 32]}
    w = flow_dialogs.ConditionWidget(cond)
    try:
        assert w.condition()["region"] == [-1900, 5, 64, 32]
    finally:
        w.hide()


# ── Stop must never be silently discarded ────────────────────────────────────
def _wait_flow(n=5):
    """Start -> n x (wait 1ms) -> End. Sends no input anywhere."""
    import flow
    g = flow.FlowGraph()
    prev = g.add_node(flow.N_START, {"name": "start"})
    for i in range(n):
        a = g.add_node(flow.N_ACTION,
                       {"step": {"kind": "wait", "data": {"ms": 1}}})
        g.add_edge(prev.id, a.id, "out")
        prev = a
    end = g.add_node(flow.N_END, {})
    g.add_edge(prev.id, end.id, "out")
    return g


def test_a_stop_before_run_is_dispatched_is_still_honoured(qapp):
    """run() is a queued slot, so Stop can land between thread.start() and the
    slot being invoked. run() used to set _running = True unconditionally and
    throw that stop away — and because stop_playback() had already cleared
    self._worker, pressing Stop again reached nobody and the flow ran on."""
    import flow_exec

    w = flow_exec.FlowWorker(_wait_flow())
    acts = []
    w.log_batch.connect(lambda evs: acts.extend(
        e for e in evs if e.get("kind") == "action"))
    done = []
    w.finished.connect(done.append)

    w.request_stop()
    w.run()

    assert done == ["stopped"], f"expected a stopped run, got {done}"
    assert not acts, f"{len(acts)} steps ran after Stop was requested"
    assert w.running() is False


def test_stop_reaches_a_retired_but_still_running_worker(main_mod, qapp):
    """stop_playback() waits 1.5 s, then _on_playback_done() drops the
    references whether or not the worker actually finished — detection steps are
    not interruptible. The worker is then only in _retired, where `if
    self._worker:` could not reach it, and is_playing() reported False so Play
    would start a second one alongside it."""
    import flow_exec

    tab = main_mod.SequenceTab.__new__(main_mod.SequenceTab)
    tab._worker = None
    tab._thread = None
    tab._retired = []
    # Shadows the bound method: the UI reset is not what this test is about,
    # and it touches widgets a bare __new__ instance does not have.
    tab._on_playback_done = lambda *a: None

    stranded = flow_exec.FlowWorker(_wait_flow())
    tab._retired.append((None, stranded))

    main_mod.SequenceTab.stop_playback(tab)
    assert stranded._stop_requested, "a retired worker never heard Stop"


# ── the run log cannot outgrow the app any more ──────────────────────────────
def test_the_worker_coalesces_log_events_instead_of_one_signal_each(qapp):
    """The interpreter emits hundreds of thousands of events a second and the
    GUI drains a few hundred. One queued signal per event is what filled memory
    until the app died."""
    import flow, flow_exec

    g = flow.FlowGraph()
    start = g.add_node(flow.N_START, {"name": "start"})
    loop = g.add_node(flow.N_LOOP, {"mode": "times", "times": 5000})
    act = g.add_node(flow.N_ACTION, {"step": {"kind": "wait", "data": {"ms": 0}}})
    g.add_edge(start.id, loop.id, "out")
    g.add_edge(loop.id, act.id, "body")
    g.add_edge(act.id, loop.id, "out")

    w = flow_exec.FlowWorker(g)
    batches, nodes = [], []
    w.log_batch.connect(batches.append)
    w.node_started.connect(nodes.append)
    w.run()

    assert batches, "nothing was delivered at all"
    # Same-thread connections, so this counts deliveries rather than drained
    # ones — which is the point: far fewer deliveries than there are events.
    assert len(batches) < 200, f"{len(batches)} deliveries is not coalescing"
    # Bounded, with a little headroom: the always-keep kinds and the "n more
    # events" marker are admitted past the cap on purpose.
    assert max(len(b) for b in batches) <= flow_exec.LOG_BATCH_MAX + 10
    assert len(nodes) <= len(batches), "one highlight per batch at most"
    kinds = {ev.get("kind") for b in batches for ev in b}
    assert "run_end" in kinds, "the final batch must be flushed"


def test_the_run_log_stops_growing_but_says_so(main_mod):
    tab = main_mod.SequenceTab(main_mod.SettingsManager())
    try:
        ev = {"t": 0, "kind": "node_enter", "name": "n", "desc": "d"}
        for _ in range(20):
            tab._on_log_batch([dict(ev) for _ in range(500)])
        assert tab._run_log.count() <= tab.LOG_MAX_ROWS
        assert len(tab._run_events) <= tab.LOG_MAX_EVENTS
        line = tab._fmt_log({"t": 0, "kind": "dropped", "n": 12345})
        assert "12,345" in line
    finally:
        tab.hide()


def test_reading_back_through_the_log_is_not_yanked_to_the_bottom(main_mod):
    tab = main_mod.SequenceTab(main_mod.SettingsManager())
    try:
        tab._run_log.setFixedHeight(60)
        tab._on_log_batch([{"t": 0, "kind": "node_enter", "name": str(i)}
                           for i in range(200)])
        bar = tab._run_log.verticalScrollBar()
        if bar.maximum() > 0:              # offscreen may not scroll at all
            bar.setValue(0)
            tab._on_log_batch([{"t": 0, "kind": "node_enter", "name": "x"}])
            assert bar.value() == 0
    finally:
        tab.hide()


# ── the playback thread is never destroyed while it runs ─────────────────────
def test_finishing_a_run_releases_its_thread_and_worker(main_mod):
    """Nothing used to clear these, so the next Play dropped a QObject whose
    thread affinity pointed at a dead QThread."""
    from PySide6.QtCore import QThread
    tab = main_mod.SequenceTab(main_mod.SettingsManager())
    try:
        tab._thread, tab._worker = QThread(), None
        tab._on_playback_done()
        assert tab._thread is None and tab._worker is None
        assert not tab.is_playing()
        assert tab._retired == [], "a finished thread should be let go at once"
    finally:
        tab.hide()


def test_stop_never_drops_the_last_reference_to_a_running_thread(main_mod, qapp):
    """wait() timing out is normal — OCR and image matching are not
    interruptible. Destroying a running QThread is a Qt abort, not an
    exception, which is why Stop could take the whole app with it."""
    from PySide6.QtCore import QObject, QThread
    import time as _time

    class _Stuck(QThread):
        def run(self):
            _time.sleep(2.0)          # outlives the courtesy wait in stop

    class _Worker(QObject):
        stopped = False

        def request_stop(self):
            self.stopped = True

    tab = main_mod.SequenceTab(main_mod.SettingsManager())
    t, w = _Stuck(), _Worker()
    try:
        tab._thread, tab._worker = t, w
        t.start()
        tab.stop_playback()
        assert w.stopped
        assert tab._thread is None
        # Still running -> still referenced. That reference is the fix.
        assert any(pair[0] is t for pair in tab._retired)
        t.wait(8000)
        qapp.processEvents()
        assert not any(pair[0] is t for pair in tab._retired), \
            "the pair should be released once the thread really finished"
    finally:
        t.wait(8000)
        tab.hide()


def test_stop_playback_is_safe_to_call_twice(main_mod):
    tab = main_mod.SequenceTab(main_mod.SettingsManager())
    try:
        tab.stop_playback()
        tab.stop_playback()
        assert tab._thread is None and tab._worker is None
    finally:
        tab.hide()


def test_re_highlighting_the_same_node_does_no_work(canvas):
    """highlight() runs once per node entered during playback, and setSelected
    fires selectionChanged, which re-pens every edge."""
    import flow
    a = canvas.graph.add_node(flow.N_ACTION,
                              {"step": {"kind": "wait", "data": {"ms": 1}}})
    scene = canvas.scene_()
    scene.rebuild()
    repaints = []
    scene.selectionChanged.connect(lambda: repaints.append(1))
    scene.highlight(a.id)
    first = len(repaints)
    for _ in range(50):
        scene.highlight(a.id)
    assert len(repaints) == first, "re-lighting the lit node did work"


def test_a_rebuild_forgets_what_was_highlighted(canvas):
    """The items are gone, so nothing is lit any more — remembering would make
    the next highlight of that node a no-op that lights nothing."""
    import flow
    a = canvas.graph.add_node(flow.N_ACTION,
                              {"step": {"kind": "wait", "data": {"ms": 1}}})
    scene = canvas.scene_()
    scene.rebuild()
    scene.highlight(a.id)
    scene.rebuild()
    assert scene._highlighted is None
    scene.highlight(a.id)
    assert scene.node_item(a.id).isSelected()


# ─────────────────────────────────────────────────────────────────────────────
#  Crash reporting — consent, and the promises the consent dialog makes.
# ─────────────────────────────────────────────────────────────────────────────

class _Settings:
    """Just enough of SettingsManager for the consent flow."""

    def __init__(self, choice="ask"):
        self.s = type("S", (), {"crash_reports": choice})()
        self.saved = {}

    def set(self, k, v):
        self.saved[k] = v
        setattr(self.s, k, v)


def _queue_one(tmp_path):
    import crashreport
    d = crashreport.crash_dir(tmp_path)
    (d / "crash-1000001.json").write_text(json.dumps({
        "schema": 1, "version": "2.0.8", "frozen": True, "os": "Windows",
        "started": 1.0, "fatal": [], "native": "", "breadcrumbs": [],
        "doing": {"node": "n1", "kind": "action"}, "silent": True,
    }), encoding="utf-8")


@pytest.fixture
def crash_home(tmp_path, monkeypatch):
    """Point the crash directory at a temp folder, not the real ~/.macronaut."""
    import crashreport
    monkeypatch.setattr(crashreport, "crash_dir",
                        lambda data_dir=None: crashreport.Path(
                            tmp_path).joinpath("crashes"))
    (tmp_path / "crashes").mkdir(exist_ok=True)
    return tmp_path


def test_the_consent_dialog_builds_and_offers_both_answers(qapp, crash_home):
    import crash_ui
    _queue_one(crash_home)
    from PySide6.QtWidgets import QPushButton
    dlg = crash_ui.ConsentDialog(pending=1)
    try:
        labels = [b.text() for b in dlg.findChildren(QPushButton)]
        assert any("Yes" in t for t in labels)
        assert any("No" in t for t in labels)
        assert not opaque_containers(dlg)
    finally:
        dlg.hide()


def test_the_consent_dialog_does_not_promise_what_it_cannot_keep(qapp,
                                                                 crash_home):
    """The dialog says scripts, keystrokes and screen contents are never sent.

    That is a claim about crashsend.to_event, so it is worth checking against
    the real thing rather than trusting the sentence.
    """
    import crashreport, crashsend
    _queue_one(crash_home)
    rep = crashreport.load(crashreport.pending(crash_home)[0])
    blob = json.dumps(crashsend.to_event(rep)).lower()
    for promised_absent in ("script", "keystroke", "clipboard", "screenshot"):
        assert promised_absent not in blob


def test_declining_deletes_the_reports_rather_than_merely_not_sending(
        qapp, crash_home, monkeypatch):
    """"No thanks" has to mean the data is gone. Keeping it on disk against the
    day the user changes their mind is not what they were asked."""
    import crash_ui, crashreport, crashsend
    _queue_one(crash_home)
    monkeypatch.setattr(crashsend, "enabled", lambda: True)
    monkeypatch.setattr(crash_ui.ConsentDialog, "exec", lambda self: 0)
    st = _Settings("ask")
    crash_ui._run(None, st)
    assert st.saved["crash_reports"] == "off"
    assert crashreport.pending(crash_home) == []


def test_agreeing_is_remembered_and_starts_an_upload(qapp, crash_home,
                                                     monkeypatch):
    import crash_ui, crashsend
    from PySide6.QtWidgets import QDialog
    _queue_one(crash_home)
    started = {"n": 0}
    monkeypatch.setattr(crashsend, "enabled", lambda: True)
    monkeypatch.setattr(crash_ui.ConsentDialog, "exec",
                        lambda self: QDialog.Accepted)
    monkeypatch.setattr(crash_ui, "_start_upload",
                        lambda w: started.__setitem__("n", started["n"] + 1))
    st = _Settings("ask")
    crash_ui._run(None, st)
    assert st.saved["crash_reports"] == "on"
    assert started["n"] == 1


def test_someone_who_has_never_crashed_is_never_asked(qapp, crash_home,
                                                      monkeypatch):
    """The question is only worth asking when there is something to answer it
    about — otherwise it is a pop-up about a problem the user does not have."""
    import crash_ui, crashsend
    monkeypatch.setattr(crashsend, "enabled", lambda: True)
    monkeypatch.setattr(crash_ui.ConsentDialog, "exec",
                        lambda self: pytest.fail("asked with nothing pending"))
    st = _Settings("ask")
    crash_ui._run(None, st)
    assert st.saved == {}          # still "ask": unanswered, not answered no


def test_a_declined_prompt_is_never_asked_again(qapp, crash_home, monkeypatch):
    import crash_ui, crashsend
    _queue_one(crash_home)
    monkeypatch.setattr(crashsend, "enabled", lambda: True)
    monkeypatch.setattr(crash_ui.ConsentDialog, "exec",
                        lambda self: pytest.fail("re-asked after a decline"))
    crash_ui._run(None, _Settings("off"))


def test_the_consent_text_is_not_clipped(qapp, crash_home):
    """A wrapping QLabel measured before it is polished answers in the default
    9pt Sans Serif, not the font the stylesheet is about to give it. Measured
    here that was 194 px against a real 285 — the pinned height silently cut
    most of a paragraph off the bottom, and the dialog still looked plausible.

    The paragraph that vanished was the one promising what is NOT collected,
    which makes this a consent problem rather than a cosmetic one.
    """
    import crash_ui
    _queue_one(crash_home)
    dlg = crash_ui.ConsentDialog(pending=1)
    try:
        dlg.show()
        qapp.processEvents()
        for lbl in dlg._wrapped:
            need = lbl.heightForWidth(crash_ui.ConsentDialog.TEXT_W)
            assert lbl.height() >= need, (
                "%r is %d px for %d px of text" % (lbl.text()[:40],
                                                   lbl.height(), need))
    finally:
        dlg.hide()


def test_the_viewer_shows_the_actual_payload(qapp, crash_home):
    """"See exactly what would be sent" has to show the real event, not a
    reassuring summary of it."""
    import crash_ui
    _queue_one(crash_home)
    dlg = crash_ui.ReportViewer()
    try:
        from PySide6.QtWidgets import QPlainTextEdit
        text = dlg.findChild(QPlainTextEdit).toPlainText()
        assert '"release": "2.0.8"' in text
        assert '"last_node": "n1"' in text
    finally:
        dlg.hide()


def test_a_second_consent_prompt_cannot_open_on_top_of_the_first(qapp, crash_home,
                                                                 monkeypatch):
    """`ConsentDialog.exec()` runs a nested event loop, so every other timer
    `schedule()` armed fires while it is open. Without a guard the second one
    builds a second modal dialog, whose loop lets a third through, and the
    process recurses until it dies.

    One window arms one timer, so this never reached a user. It reached the test
    suite, which builds many: the run stopped dead at whichever test happened to
    pump events once the shots had matured and a report was queued.
    """
    import crash_ui
    _queue_one(crash_home)
    made = []
    monkeypatch.setattr(crash_ui, "ConsentDialog",
                        lambda *a, **k: pytest.fail("a second dialog was built"))
    monkeypatch.setattr(crash_ui, "_asking", True)
    crash_ui._run(None, _Settings("ask"))
    assert not made


def test_the_guard_is_released_so_the_next_crash_can_still_ask(qapp, crash_home,
                                                               monkeypatch):
    """The guard must not latch. If a declined dialog left it set, the user
    would never be asked again for the rest of the session."""
    import crash_ui
    from PySide6.QtWidgets import QDialog
    _queue_one(crash_home)

    class _Dlg:
        def __init__(self, *a, **k):
            pass

        def exec(self):
            assert crash_ui._asking is True, "guard not set while asking"
            return QDialog.Rejected

    monkeypatch.setattr(crash_ui, "ConsentDialog", _Dlg)
    monkeypatch.setattr(crash_ui, "_discard_all", lambda: None)
    crash_ui._run(None, _Settings("ask"))
    assert crash_ui._asking is False, "guard stayed set after the dialog closed"


def test_nothing_is_scheduled_when_no_endpoint_is_configured(qapp, monkeypatch):
    import crash_ui, crashsend
    monkeypatch.setattr(crashsend, "enabled", lambda: False)
    monkeypatch.setattr(crash_ui.QTimer, "singleShot",
                        lambda *a: pytest.fail("scheduled with no DSN"))
    crash_ui.schedule(None, _Settings("on"))


# ── testing a match must not throw away the step being edited ────────────────
def test_test_match_dims_the_dialog_instead_of_hiding_it(main_mod, monkeypatch):
    """hide() on a modal QDialog makes exec() return Rejected, so pressing
    "Test match" while adding a Detect node discarded the node before OK was
    ever pressed — the dialog simply vanished mid-edit.

    Both halves matter, so both are asserted at the moment of the grab: the
    dialog is still shown (its exec loop is alive) AND it is fully transparent
    (it is not in the screenshot it just asked for)."""
    from PySide6.QtWidgets import QDialog, QLabel
    import matcher
    dlg = QDialog()
    dlg.show()
    seen = {}

    def fake_grab():
        seen["visible"] = dlg.isVisible()
        seen["opacity"] = dlg.windowOpacity()
        return None                       # -> "couldn't read", no preview popup

    monkeypatch.setattr(matcher, "ENABLED", True)
    monkeypatch.setattr(matcher, "grab_all_screens", fake_grab)
    monkeypatch.setattr(matcher, "best_match", lambda *a, **k: None)
    try:
        main_mod._run_match_test(__file__, 0.8, dlg, QLabel(), dlg)
        assert seen["visible"] is True, "the dialog was hidden — exec() is now Rejected"
        assert seen["opacity"] == 0.0, "the dialog would be in its own screenshot"
        assert dlg.windowOpacity() == 1.0, "the dialog was left invisible"
    finally:
        dlg.hide()


def test_neither_live_tester_reaches_for_hide(main_mod):
    """The image tester and the OCR tester had the same bug. One helper does it
    correctly now; a bare hide() in either is the bug coming back."""
    import inspect
    for fn in (main_mod._run_match_test,
               main_mod.StepDialog._textwait_test):
        src = inspect.getsource(fn)
        assert ".hide()" not in src, f"{fn.__name__} hides a window again"
        assert "_Dimmed" in src, f"{fn.__name__} no longer dims its windows"


def test_the_image_panel_can_narrow_its_search_area(main_mod):
    """The search area was on Wait-for-text only; the image side scanned the
    whole desktop at every scale, every poll."""
    step = main_mod.SeqStep("wait_image", {"image_path": "x.png",
                                           "region": [5, 6, 7, 8]}, 0)
    dlg = main_mod.StepDialog(step, default_text_cps=20, family="detect")
    try:
        dlg.show()
        assert dlg._imgwait_region_sel.region() == (5, 6, 7, 8)
        dlg._imgwait_region_sel.set_region(None)
        dlg._on_ok()
        assert dlg.step.to_dict()["data"]["region"] is None
    finally:
        dlg.hide()


def test_a_match_test_mid_edit_does_not_cancel_the_dialog(main_mod, monkeypatch):
    """The bug as the user meets it: add a Detect node, press Test match to
    check the image before committing, and the dialog is gone and no node was
    added. exec() had already returned Rejected — measured, not theorised:
    hide()+show() inside a live exec loop returns 0 even when OK is pressed
    afterwards."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QDialog
    import matcher
    monkeypatch.setattr(matcher, "ENABLED", True)
    monkeypatch.setattr(matcher, "grab_all_screens", lambda: None)
    monkeypatch.setattr(matcher, "best_match", lambda *a, **k: None)

    dlg = main_mod.StepDialog(None, default_text_cps=20, family="detect")
    dlg._imgwait_path.setText(__file__)      # any file that exists; matcher is stubbed
    QTimer.singleShot(0, lambda: _click_test_match(dlg))
    QTimer.singleShot(400, dlg._on_ok)       # ...and only then, OK
    QTimer.singleShot(4000, dlg.reject)      # never hang the suite
    try:
        assert dlg.exec() == QDialog.Accepted, "testing the match cancelled the dialog"
        assert dlg.step is not None, "no step came out of the dialog"
        assert dlg.step.kind == "wait_image"
    finally:
        dlg.hide()


def _click_test_match(dlg):
    from PySide6.QtWidgets import QPushButton
    for b in dlg.findChildren(QPushButton):
        if b.text() == "Test match" and b.isVisible():
            b.click()
            return
    raise AssertionError("no visible Test match button in the Detect ▸ Image panel")


# ── script launcher keys ─────────────────────────────────────────────────────
#
# One listener watches Start/Stop, the trigger key and every launcher key, so
# the thing under test is the dispatch: the right key has to reach the right
# script, and a launcher key must never disturb the canvas.

@pytest.fixture
def bound_window(window, main_mod, tmp_path, monkeypatch):
    """A window whose script library is a temp folder holding one real flow."""
    monkeypatch.setattr(main_mod, "scripts_dir", lambda: tmp_path)
    _wait_flow(3).save(str(tmp_path / "probe.json"))
    _wait_flow(3).save(str(tmp_path / "other.json"))
    return window


def _capture_start(win, monkeypatch):
    """Replace _start so dispatch can be tested without spawning a QThread."""
    seen = []
    monkeypatch.setattr(win, "_start", lambda graph=None: seen.append(graph))
    return seen


def test_a_launcher_key_runs_its_script_without_touching_the_canvas(
        bound_window, monkeypatch):
    import flow
    win = bound_window
    canvas_graph = win._sequence_tab._graph
    win._settings.s.script_hotkeys = {"f13": "probe"}
    started = _capture_start(win, monkeypatch)

    win._toggle("f13")

    assert len(started) == 1, "the launcher key did not start anything"
    assert started[0] is not None, "the bound script ran as the canvas graph"
    assert flow.has_work(started[0])
    assert win._sequence_tab._graph is canvas_graph, (
        "running a launcher key replaced the canvas graph — that silently "
        "destroys unsaved editing")


def test_the_same_launcher_key_twice_stops_instead_of_relaunching(
        bound_window, monkeypatch):
    win = bound_window
    win._settings.s.script_hotkeys = {"f13": "probe"}
    started = _capture_start(win, monkeypatch)
    stopped = []
    monkeypatch.setattr(win, "_stop", lambda: stopped.append(True))

    win._running = True
    win._active_hotkey = "f13"
    win._toggle("f13")

    assert stopped == [True], "the running script was not stopped"
    assert started == [], "the same key relaunched instead of toggling off"


def test_a_different_launcher_key_switches_scripts(bound_window, monkeypatch):
    win = bound_window
    win._settings.s.script_hotkeys = {"f13": "probe", "f14": "other"}
    started = _capture_start(win, monkeypatch)
    stopped = []
    monkeypatch.setattr(win, "_stop", lambda: stopped.append(True))

    win._running = True
    win._active_hotkey = "f13"
    win._toggle("f14")

    assert stopped == [True], "the previous script kept running"
    assert len(started) == 1, "the new script did not start"


def test_start_stop_wins_a_collision_with_a_launcher_key(
        bound_window, monkeypatch):
    """A hand-edited settings.json can bind a launcher key onto Start/Stop.
    Losing Start/Stop is far worse than a launcher key that does nothing."""
    win = bound_window
    win._settings.s.start_stop_hotkey = "f8"
    win._settings.s.script_hotkeys = {"f8": "probe"}
    started = _capture_start(win, monkeypatch)

    win._toggle("f8")

    assert started == [None], (
        "f8 ran the bound script instead of the normal Start/Stop")


def test_a_binding_whose_script_is_gone_reports_instead_of_raising(
        bound_window, monkeypatch):
    win = bound_window
    win._settings.s.script_hotkeys = {"f13": "deleted-script"}
    started = _capture_start(win, monkeypatch)
    said = []
    monkeypatch.setattr(win._tray, "notify", lambda t, m: said.append(m))

    win._toggle("f13")            # must not raise

    assert started == [], "a missing script still tried to run"
    assert said and "deleted-script" in said[0], (
        f"the user was not told which binding is broken: {said}")


def test_the_listener_watches_every_launcher_key(bound_window):
    win = bound_window
    win._settings.s.start_stop_hotkey = "f8"
    win._settings.s.script_hotkeys = {"f13": "probe", "f14": "other"}
    win._refresh_hotkeys()
    watched = win._hk_listener._hotkeys
    assert "f8" in watched
    assert "f13" in watched and "f14" in watched, (
        f"launcher keys are not being listened for: {watched}")


def test_the_bridge_reports_which_hotkey_fired(main_mod):
    """The listener always knew which hotkey matched; it used to discard it,
    which is why one signal could not drive several scripts."""
    class _FakeKey:
        def __init__(self, name):
            self.char = None
            self._name = name
        def __str__(self):
            return f"Key.{self._name}"

    bridge = main_mod.HotkeyBridge()
    fired = []
    bridge.triggered.connect(fired.append)
    lst = main_mod.HotkeyListener(bridge)
    lst._hotkeys = ["f13", "f14"]

    lst._on_press(_FakeKey("f14"))

    assert fired == ["f14"], f"expected the fired key to be named, got {fired}"


def test_launcher_rows_reject_bindings_that_would_misfire(window):
    """Reserved keys, duplicates and bare characters never reach settings."""
    tab = window._settings_tab
    window._settings.s.start_stop_hotkey = "f8"
    window._settings.s.panic_hotkey = "esc"
    for holder, _k, _c in list(tab._sh_rows):
        tab._remove_script_hotkey_row(holder)

    for hk in ("f8", "esc", "a"):
        tab._add_script_hotkey_row(hk, "probe")
    mapping, problems = tab._collect_script_hotkeys()

    assert mapping == {}, f"a bad binding was accepted: {mapping}"
    assert len(problems) == 3, f"expected three refusals, got {problems}"
    assert any("Start/Stop" in p for p in problems)
    assert any("panic" in p for p in problems)
    assert any("single character" in p for p in problems)


def test_launcher_bindings_survive_a_settings_round_trip(window, main_mod,
                                                         tmp_path, monkeypatch):
    monkeypatch.setattr(main_mod, "scripts_dir", lambda: tmp_path)
    _wait_flow(2).save(str(tmp_path / "probe.json"))
    tab = window._settings_tab
    window._settings.s.start_stop_hotkey = "f8"
    window._settings.s.script_hotkeys = {"f13": "probe"}

    tab._load()
    assert len(tab._sh_rows) == 1, "the saved binding did not come back as a row"
    mapping, problems = tab._collect_script_hotkeys()
    assert not problems and mapping == {"f13": "probe"}, (
        f"round trip changed the binding: {mapping} {problems}")


# ── A step editor taller than the screen ─────────────────────────────────────
# Qt clamps an over-tall dialog to the monitor, but the layout's minimum keeps
# demanding the full height — so the bottom row, which is OK/Cancel, ends up
# below the screen edge with no way to reach it. Seen for real: a StepDialog
# asking for 1031 px of minimum height on a 1080 px monitor.

def _step_dialog(main_mod, kind="click"):
    dlg = main_mod.StepDialog()
    idx = dlg._type_combo.findData(kind)
    if idx >= 0:
        dlg._type_combo.setCurrentIndex(idx)
    return dlg


def test_a_dialog_that_fits_keeps_its_plain_layout(main_mod):
    """The common case must not grow a scroll area."""
    dlg = _step_dialog(main_mod)
    dlg._refit()
    assert dlg._scrolled is False
    dlg.hide()


def test_an_over_tall_dialog_becomes_scrollable(main_mod, monkeypatch):
    dlg = _step_dialog(main_mod)
    monkeypatch.setattr(type(dlg), "_available_height", lambda self: 300)
    dlg._refit()
    assert dlg._scrolled is True
    assert dlg.height() <= 300, "dialog must not exceed the usable screen height"
    dlg.hide()


def test_the_ok_cancel_row_never_ends_up_inside_the_scroll_area(main_mod, monkeypatch):
    """Scrolling the buttons out of reach would be a worse bug than the one
    this fixes."""
    from PySide6.QtWidgets import QScrollArea

    dlg = _step_dialog(main_mod)
    monkeypatch.setattr(type(dlg), "_available_height", lambda self: 300)
    dlg._refit()

    areas = dlg.findChildren(QScrollArea)
    assert areas, "expected a scroll area"
    assert not any(dlg._btns.isAncestorOf(a) or a.isAncestorOf(dlg._btns)
                   for a in areas), "OK/Cancel must stay outside the scroll area"
    assert dlg.layout().indexOf(dlg._btns) >= 0, "OK/Cancel must be in the dialog layout"
    dlg.hide()


def test_no_field_is_lost_when_the_body_moves_into_the_scroll_area(main_mod, monkeypatch):
    """Re-parenting a whole layout is exactly where widgets go missing."""
    dlg = _step_dialog(main_mod)
    before = {id(w) for w in dlg.findChildren(main_mod.QWidget)}
    monkeypatch.setattr(type(dlg), "_available_height", lambda self: 300)
    dlg._refit()
    after = {id(w) for w in dlg.findChildren(main_mod.QWidget)}
    assert before <= after, "widgets disappeared when the body was re-parented"
    dlg.hide()


def test_fitting_to_screen_is_done_once_not_on_every_refit(main_mod, monkeypatch):
    """Re-running it would nest the body one scroll area deeper each time.

    Counted on the dialog's own layout, not with findChildren: a step editor
    already contains a scroll area of its own further down the tree.
    """
    from PySide6.QtWidgets import QScrollArea

    dlg = _step_dialog(main_mod)
    monkeypatch.setattr(type(dlg), "_available_height", lambda self: 300)
    dlg._refit()
    dlg._refit()
    dlg._refit()

    lay = dlg.layout()
    top = [lay.itemAt(i).widget() for i in range(lay.count())]
    assert sum(isinstance(w, QScrollArea) for w in top) == 1
    assert lay.count() == 2, "layout should hold exactly the scroll area and the buttons"
    dlg.hide()


# ── Clicking a node that a wire runs under ───────────────────────────────────
# Reported: "when clicking on a node with connections running under them it
# selects the connection". The press handler prefers nodes over edges by
# inspection, so this reproduces it rather than trusting the read.

def _wire_under_node(canvas):
    """A -> B with C parked halfway, so the wire passes beneath C."""
    import flow
    g = canvas.graph
    step = {"step": {"kind": "wait", "data": {"ms": 1}}}
    a = g.add_node(flow.N_ACTION, dict(step), x=0, y=0)
    b = g.add_node(flow.N_ACTION, dict(step), x=700, y=0)
    g.add_edge(a.id, b.id, "out")
    c = g.add_node(flow.N_ACTION, dict(step), x=330, y=0)
    canvas.scene_().rebuild()
    return a, b, c


def test_clicking_a_node_with_a_wire_under_it_selects_the_node(canvas):
    from PySide6.QtCore import QEvent, Qt
    _a, _b, c = _wire_under_node(canvas)

    item = canvas.scene_().node_item(c.id)
    centre = canvas.mapFromScene(item.sceneBoundingRect().center())

    _send_mouse(canvas, QEvent.MouseButtonPress, centre, Qt.LeftButton, Qt.LeftButton)
    _send_mouse(canvas, QEvent.MouseButtonRelease, centre, Qt.LeftButton, Qt.NoButton)

    assert canvas.selected_node_ids() == [c.id]
    import flow_canvas
    picked = [i for i in canvas.scene_().selectedItems()
              if isinstance(i, flow_canvas.EdgeItem)]
    assert picked == [], "a wire under a node must not take the click"


def test_a_wire_is_never_stacked_above_a_node(canvas):
    """Z order is the backstop: even if hit-testing changes, a wire must not
    be able to sit on top of the thing it runs behind."""
    import flow_canvas
    _wire_under_node(canvas)
    scene = canvas.scene_()
    nodes = [i for i in scene.items() if isinstance(i, flow_canvas.NodeItem)]
    edges = [i for i in scene.items() if isinstance(i, flow_canvas.EdgeItem)]
    assert nodes and edges
    assert max(e.zValue() for e in edges) < min(n.zValue() for n in nodes)


# ── Wire routing ─────────────────────────────────────────────────────────────
# The waypoints were checked against obstacles but the drawn curve was not: a
# Catmull-Rom spline overshoots its control polygon, so a route that had been
# verified clear could still be painted through a node. Backward wires -- the
# loop-back, the only backward wire our layouts use -- were worst, because the
# old turn-out columns collapsed (left >= right) for every one of them.

def _painted_intrusion(scene, samples=250):
    """Deepest penetration, in px, of any painted wire into a node that is not
    one of its own endpoints."""
    import flow_canvas as fc
    worst = 0.0
    for item in scene.items():
        if not isinstance(item, fc.EdgeItem):
            continue
        obstacles = [it.sceneBoundingRect() for nid, it in scene._nodes.items()
                     if nid not in (item.edge.src, item.edge.dst)]
        path = item.path()
        for i in range(samples + 1):
            p = path.pointAtPercent(i / samples)
            for r in obstacles:
                if r.contains(p):
                    worst = max(worst, min(p.x() - r.left(), r.right() - p.x(),
                                           p.y() - r.top(), r.bottom() - p.y()))
    return worst


def _grid_flow(seed, flow_mod, fc):
    import random
    rng = random.Random(seed)
    g = flow_mod.FlowGraph()
    g.add_node(flow_mod.N_START, {"name": flow_mod.START_NAME}, x=-400, y=0)
    cells = [(c, r) for c in range(5) for r in range(4)]
    rng.shuffle(cells)
    ns = [g.add_node(flow_mod.N_ACTION, {"step": {"kind": "wait", "data": {"ms": 1}}},
                     x=c * 300 + rng.randint(-20, 20),
                     y=r * 170 + rng.randint(-15, 15))
          for (c, r) in cells[:rng.randint(3, 7)]]
    for _ in range(rng.randint(1, 4)):
        a, b = rng.sample(ns, 2)
        g.add_edge(a.id, b.id, "out")
    scene = fc.FlowScene(g)
    scene.rebuild()
    return scene


def test_a_loop_back_wire_does_not_cross_the_node_it_loops_around(qapp):
    """Measured at 36.8 px of intrusion before the router was rewritten, and
    every loop in every flow has one of these."""
    import flow, flow_canvas as fc
    g = flow.FlowGraph()
    step = {"step": {"kind": "wait", "data": {"ms": 1}}}
    a = g.add_node(flow.N_ACTION, dict(step), x=0, y=0)
    b = g.add_node(flow.N_ACTION, dict(step), x=700, y=0)
    g.add_node(flow.N_ACTION, dict(step), x=330, y=0)      # in the way
    g.add_edge(b.id, a.id, "out")                          # backward
    scene = fc.FlowScene(g)
    scene.rebuild()
    assert _painted_intrusion(scene) <= 0.5


def test_wires_clear_nodes_across_many_realistic_layouts(qapp):
    """Regression floor. Measured 53/300 failing before the router rewrite and
    13/300 today, on this exact generator — so anything near the old rate is a
    regression, and the few that remain are dense corners with no clean way
    round rather than the old systematic overshoot.

    ⚠ The number here is over the *300*-seed generator; the assertion below
    runs 60 of them, where the same 13 show up as 3. And ⚠ measuring it against
    a second copy of flow_canvas (an old revision imported under another name,
    to compare) reports a flawless 0 — _painted_intrusion isinstance-checks the
    real module's NodeItem, so a scene built from the copy contains nothing it
    recognises and it counts nothing. Compare revisions by checking one out,
    not by importing both."""
    import flow, flow_canvas as fc
    bad = sum(1 for seed in range(60)
              if _painted_intrusion(_grid_flow(seed, flow, fc)) > 0.5)
    assert bad <= 4, f"{bad}/60 layouts route a wire through a node"


# ── Canvas cost on large flows ────────────────────────────────────────────────
# Measured at 500 nodes before this round: opening a flow 2250 ms, releasing a
# drag 2297 ms, selecting everything 657 ms, one zoomed-out repaint 125 ms. The
# tests below pin the four things that fixed it — none of them by timing, which
# would be flaky, but by the property that makes each one fast.

def _big_flow(flow, fc, n=200):
    """A flow shaped like the battery layout: 14 wide, wrapping, so every 14th
    wire runs backward — which is the case the router actually pays for."""
    g = flow.FlowGraph()
    prev = g.add_node(flow.N_START, {"name": flow.START_NAME}, 0, 0)
    for i in range(n):
        nd = g.add_node(flow.N_ACTION, {"step": {"kind": "wait", "data": {"ms": 1}}},
                        x=(i % 14) * 260.0, y=(i // 14) * 104.0 + 120)
        g.add_edge(prev.id, nd.id, "out")
        prev = nd
    return g


def test_selecting_nodes_does_not_repen_every_wire(qapp):
    """The scene used to re-pen every edge on every selectionChanged, and that
    signal fires once per setSelected — so selecting a 500-node flow was a
    quarter of a million setPen calls. An edge re-pens itself now, and
    selecting a *node* changes no edge's selection, so the count is zero."""
    import flow, flow_canvas as fc
    scene = fc.FlowScene(_big_flow(flow, fc, 40))
    calls = []
    real = fc.EdgeItem._apply_pen
    fc.EdgeItem._apply_pen = lambda self: calls.append(1) or real(self)
    try:
        for item in scene._nodes.values():
            item.setSelected(True)
    finally:
        fc.EdgeItem._apply_pen = real
    assert calls == [], (
        f"selecting {len(scene._nodes)} nodes re-penned wires {len(calls)} times")


def test_a_wire_repens_itself_the_moment_it_is_selected(qapp):
    """And it must not need the event loop to do it — the old scene-wide slot
    ran synchronously, so every caller is entitled to a correct pen on return."""
    import flow, flow_canvas as fc
    scene = fc.FlowScene(_big_flow(flow, fc, 3))
    edge = next(iter(scene._edges.values()))
    edge.setSelected(True)
    assert edge.pen().color() == fc.NODE_SEL
    edge.setSelected(False)
    assert edge.pen().color() != fc.NODE_SEL


def test_a_big_reroute_is_spread_over_frames_and_can_be_flushed(qapp):
    """Dropping a large selection used to freeze while every wire it touched
    was routed. The work still happens; it is handed out a slice at a time so
    the canvas keeps drawing, nearest the viewport first."""
    import flow, flow_canvas as fc
    scene = fc.FlowScene(_big_flow(flow, fc, fc.FlowScene.ASYNC_ROUTE_MIN + 40))
    scene.flush_routes()

    scene.reroute_nodes(list(scene.graph.nodes))
    assert scene._pending, "a reroute this size should not have run to completion"
    scene.flush_routes()
    assert scene._pending == [], "flush_routes must finish the queue"


def test_a_small_reroute_stays_synchronous(qapp):
    """Below the threshold nothing is deferred, so every caller and every test
    written before the queue existed still gets final paths on return."""
    import flow, flow_canvas as fc
    scene = fc.FlowScene(_big_flow(flow, fc, 10))
    scene.reroute_nodes(list(scene.graph.nodes))
    assert scene._pending == []


def test_the_background_grid_coarsens_instead_of_multiplying(qapp):
    """Dots are placed in scene coordinates, so their count grows as 1/zoom² —
    zoomed to fit, a 500-node flow drew 37,000 of them and cost more than every
    node and wire in it. Doubling the step keeps the count flat, and every dot
    that survives is still on a real grid line."""
    import flow_canvas as fc
    assert fc._grid_step(1.0)[0] == fc.GRID
    assert fc._grid_step(0.5)[0] == fc.GRID, "half zoom must look unchanged"
    for zoom in (0.49, 0.3, 0.24, 0.1, 0.05):
        step, width = fc._grid_step(zoom)
        assert step % fc.GRID == 0, "a coarser grid must still land on grid lines"
        assert step * zoom >= fc.MIN_DOT_PX, f"dots still crowded at {zoom}"
        # The dot has to grow with the step or zooming out fades to black.
        assert width > 2.2


def test_a_big_flow_can_always_be_zoomed_out_far_enough_to_see(qapp):
    """⚠ The wheel used to stop at a hard-coded 0.25 while `fit()` went through
    `fitInView`, which never consulted it. So Fit on a 400-node flow landed near
    0.06, and one notch of zoom-in was a one-way door: the wheel refused to go
    back under 0.25 and there was no way to see the whole thing again short of
    pressing Fit.
    """
    from PySide6.QtCore import QPoint, QPointF
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication
    import flow, flow_canvas as fc

    canvas = fc.FlowCanvas(_big_flow(flow, fc, 400))
    canvas.resize(900, 600)
    canvas.show()
    try:
        QApplication.processEvents()
        canvas.fit()
        fit_scale = canvas.transform().m11()
        assert fit_scale < 0.25, "test needs a flow too big for the old floor"

        for _ in range(6):      # zoom in, as a user reading one node would
            canvas.scale(1.15, 1.15)
        assert canvas.transform().m11() > fit_scale

        for _ in range(60):     # then roll all the way back out
            ev = QWheelEvent(QPointF(400, 300),
                             canvas.mapToGlobal(QPoint(400, 300)),
                             QPoint(0, -120), QPoint(0, -120), Qt.NoButton,
                             Qt.NoModifier, Qt.NoScrollPhase, False)
            QApplication.sendEvent(canvas.viewport(), ev)

        assert canvas.transform().m11() <= fit_scale, (
            "cannot get back to the fitting scale: stuck at %.3f, fit is %.3f"
            % (canvas.transform().m11(), fit_scale))
        assert canvas.transform().m11() >= fc.FlowCanvas.ZOOM_MIN_HARD
    finally:
        canvas.hide()


def test_a_node_zoomed_out_past_legibility_draws_no_text(qapp):
    """At the scale where 9 pt text is under 4 pt, laying it out costs a font
    setup, an elide and a glyph run per node to produce a grey smear."""
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtWidgets import QStyleOptionGraphicsItem
    import flow, flow_canvas as fc
    scene = fc.FlowScene(_big_flow(flow, fc, 3))
    item = next(i for i in scene._nodes.values()
                if i.node.type == flow.N_ACTION)

    drawn = []
    real = QPainter.drawText

    def counting(self, *a, **k):
        drawn.append(1)
        return real(self, *a, **k)

    img = QImage(400, 200, QImage.Format_ARGB32)
    QPainter.drawText = counting
    try:
        for scale, want_text in ((1.0, True), (fc.NodeItem.LOD_TEXT / 2, False)):
            drawn.clear()
            p = QPainter(img)
            p.scale(scale, scale)
            opt = QStyleOptionGraphicsItem()
            item.paint(p, opt)
            p.end()
            assert bool(drawn) is want_text, (
                f"at {scale:.2f}x zoom drawText ran {len(drawn)} times")
    finally:
        QPainter.drawText = real


def test_an_axis_aligned_segment_takes_the_shortcut_and_still_agrees(qapp):
    """Every segment a lane route is made of is horizontal or vertical, and for
    those "the bounding boxes overlap" is not an approximation of the answer,
    it *is* the answer — which is why the router can skip the slab clipping on
    all but the one diagonal it tests per route. If that were ever off by a
    boundary case the wires would quietly start crossing nodes again."""
    import random
    from PySide6.QtCore import QRectF
    import flow_canvas as fc
    rnd = random.Random(11)
    for _ in range(3000):
        l, t = rnd.randrange(-50, 50), rnd.randrange(-50, 50)
        rect = QRectF(l, t, rnd.randrange(1, 40), rnd.randrange(1, 40))
        a, b = rnd.randrange(-60, 60), rnd.randrange(-60, 60)
        fixed = rnd.randrange(-60, 60)
        vertical = rnd.random() < 0.5
        x0, y0, x1, y1 = ((fixed, a, fixed, b) if vertical else (a, fixed, b, fixed))
        seg = QRectF(min(x0, x1), min(y0, y1),
                     abs(x1 - x0), abs(y1 - y0))
        got = fc._seg_hits(x0, y0, x1, y1,
                           rect.left(), rect.top(), rect.right(), rect.bottom())
        # intersects() is false for a zero-area rect, which every axis-aligned
        # segment is, so compare the ranges directly rather than via Qt.
        want = (seg.right() >= rect.left() and seg.left() <= rect.right()
                and seg.bottom() >= rect.top() and seg.top() <= rect.bottom())
        assert got is want, f"seg {(x0, y0, x1, y1)} vs {rect}"


def test_segment_rect_intersection_catches_a_corner_clip(qapp):
    """Sampling every 8 px let a segment clip a corner unnoticed, which is how
    the router came to prefer grazing lanes once its search improved."""
    from PySide6.QtCore import QPointF, QRectF
    import flow_canvas as fc
    rect = QRectF(0, 0, 100, 100)
    assert fc._seg_hits_rect(QPointF(-5, 5), QPointF(5, -5), rect) is True
    assert fc._seg_hits_rect(QPointF(-5, -5), QPointF(-1, -1), rect) is False
    assert fc._seg_hits_rect(QPointF(50, -10), QPointF(50, 110), rect) is True


def test_pressing_an_already_selected_node_drops_a_selected_wire(canvas):
    """Otherwise the highlight stays on the wire and reads as 'clicking the
    node selected the connection'."""
    from PySide6.QtCore import QEvent, Qt
    import flow_canvas as fc
    _a, _b, c = _wire_under_node(canvas)

    item = canvas.scene_().node_item(c.id)
    item.setSelected(True)
    edge = next(i for i in canvas.scene_().items() if isinstance(i, fc.EdgeItem))
    edge.setSelected(True)

    centre = canvas.mapFromScene(item.sceneBoundingRect().center())
    _send_mouse(canvas, QEvent.MouseButtonPress, centre, Qt.LeftButton, Qt.LeftButton)
    _send_mouse(canvas, QEvent.MouseButtonRelease, centre, Qt.LeftButton, Qt.NoButton)

    assert edge.isSelected() is False
    assert canvas.selected_node_ids() == [c.id]


# ── Script library: delete ────────────────────────────────────────────────────
# Deleting is the one library action that can lose work, so it is the one that
# needs pinning: where the file goes, that a second deletion of the same name
# does not eat the first, and that a launcher key bound to it is cleared.

@pytest.fixture
def library(main_mod, monkeypatch, tmp_path):
    """A ScriptLibraryDialog over a throwaway scripts folder.

    Patches the names in main's globals, not settings.py's — main imported them
    directly, so rebinding the source module would not be seen.
    """
    import flow

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    monkeypatch.setattr(main_mod, "scripts_dir", lambda: scripts)
    monkeypatch.setattr(main_mod, "data_dir", lambda: tmp_path)

    for name in ("alpha", "beta", "gamma"):
        g = flow.FlowGraph()
        n = g.add_node("action", {"step": {"kind": "text",
                                           "data": {"text": name}}})
        g.add_edge(g.start_node().id, "out", n.id)
        g.save(str(scripts / f"{name}.json"))

    class _Settings:
        def __init__(self):
            self.s = type("S", (), {"script_hotkeys": {"f13": "beta"}})()
            self.saved = []

        def set(self, key, value):
            setattr(self.s, key, value)
            self.saved.append((key, value))

    dlg = main_mod.ScriptLibraryDialog(None, _Settings())
    try:
        yield dlg, scripts, tmp_path
    finally:
        dlg.hide()


def _select(dlg, *names):
    dlg._list.clearSelection()
    for i in range(dlg._list.count()):
        it = dlg._list.item(i)
        if it.text() in names:
            it.setSelected(True)


def test_deleting_a_script_moves_it_aside_rather_than_destroying_it(
        library, monkeypatch, main_mod):
    dlg, scripts, root = library
    monkeypatch.setattr(main_mod.QMessageBox, "exec", lambda self: 0)
    monkeypatch.setattr(main_mod.QMessageBox, "clickedButton",
                        lambda self: self.buttons()[-1])

    _select(dlg, "alpha")
    dlg._delete()

    assert not (scripts / "alpha.json").exists(), "removed from the library"
    assert (root / "_deleted" / "alpha.json").exists(), "still recoverable"
    assert [dlg._list.item(i).text() for i in range(dlg._list.count())] \
        == ["beta", "gamma"]


def test_deleting_the_same_name_twice_keeps_both(library, monkeypatch,
                                                 main_mod, tmp_path):
    """The second deletion must not silently overwrite the first — that is the
    one thing a recoverable-delete folder exists to prevent."""
    import flow
    dlg, scripts, root = library
    monkeypatch.setattr(main_mod.QMessageBox, "exec", lambda self: 0)
    monkeypatch.setattr(main_mod.QMessageBox, "clickedButton",
                        lambda self: self.buttons()[-1])

    _select(dlg, "alpha")
    dlg._delete()

    g = flow.FlowGraph()          # a different "alpha", saved and deleted again
    g.save(str(scripts / "alpha.json"))
    dlg._refresh()
    _select(dlg, "alpha")
    dlg._delete()

    kept = sorted(p.name for p in (root / "_deleted").glob("alpha*.json"))
    assert len(kept) == 2, f"one deletion ate the other: {kept}"


def test_deleting_a_bound_script_clears_its_launcher_key(library, monkeypatch,
                                                         main_mod):
    """A binding points at a script *name*. Left behind, the key stays armed
    and falls through to Start/Stop, which is worse than doing nothing."""
    dlg, _scripts, _root = library
    monkeypatch.setattr(main_mod.QMessageBox, "exec", lambda self: 0)
    monkeypatch.setattr(main_mod.QMessageBox, "clickedButton",
                        lambda self: self.buttons()[-1])

    _select(dlg, "beta")
    dlg._delete()

    assert dlg._settings.s.script_hotkeys == {}
    assert dlg.bindings_changed is True, "the listener has to be re-armed"


def test_cancelling_the_delete_confirmation_keeps_every_script(
        library, monkeypatch, main_mod):
    dlg, scripts, _root = library
    monkeypatch.setattr(main_mod.QMessageBox, "exec", lambda self: 0)
    monkeypatch.setattr(main_mod.QMessageBox, "clickedButton", lambda self: None)

    _select(dlg, "alpha", "beta")
    dlg._delete()

    assert sorted(p.stem for p in scripts.glob("*.json")) \
        == ["alpha", "beta", "gamma"]
    assert dlg._settings.s.script_hotkeys == {"f13": "beta"}


def test_search_filters_the_list_and_hidden_rows_cannot_be_acted_on(library):
    dlg, _scripts, _root = library
    _select(dlg, "alpha", "beta")
    dlg._search.setText("gam")

    visible = [dlg._list.item(i).text() for i in range(dlg._list.count())
               if not dlg._list.item(i).isHidden()]
    assert visible == ["gamma"]
    # A row filtered out of sight must not still be a delete target.
    assert dlg._selected_paths() == []


def test_library_buttons_follow_the_selection(library):
    dlg, _scripts, _root = library
    assert not dlg._btn_open.isEnabled()
    assert not dlg._btn_delete.isEnabled()

    _select(dlg, "alpha")
    assert dlg._btn_open.isEnabled()
    assert dlg._btn_delete.isEnabled()
    assert not dlg._btn_merge.isEnabled(), "merge needs two"

    _select(dlg, "alpha", "beta")
    assert dlg._btn_merge.isEnabled()
    assert not dlg._btn_open.isEnabled(), "open takes exactly one"


def test_library_containers_do_not_paint_over_the_dialog(library):
    dlg, _scripts, _root = library
    assert not opaque_containers(dlg)


def test_the_delete_shortcut_belongs_to_the_list_not_the_dialog(library):
    """A shortcut outranks a key press. On the dialog, Del would fire while the
    user was pressing Del to edit the search box."""
    from PySide6.QtGui import QKeySequence
    dels = [a for a in dlg_actions(library[0])
            if a.shortcut() == QKeySequence.Delete]
    assert dels, "no Delete shortcut at all"
    assert all(a.parent() is library[0]._list for a in dels)
    assert not [a for a in library[0].actions()
                if a.shortcut() == QKeySequence.Delete], \
        "the shortcut must not be armed dialog-wide"


def dlg_actions(dlg):
    from PySide6.QtGui import QAction
    return dlg.findChildren(QAction)


# ── per-node colour ───────────────────────────────────────────────────────────
def test_a_node_can_carry_its_own_colour(qapp):
    """HEADER_COLORS is keyed by type, so colour could only ever say what a node
    IS. What a node is FOR — the login one, the risky branch — is what people
    actually reach for colour to mark, and it is per node."""
    import flow, flow_canvas as fc
    g, made = _chain(("click", {}), ("click", {}))
    a, b = made
    assert fc.node_header_color(a) == fc.node_header_color(b), "same type, same colour"

    a.data["color"] = "#f43f5e"
    assert fc.node_header_color(a) == QColorOf("#f43f5e")
    assert fc.node_header_color(b) != QColorOf("#f43f5e"), "the other node is untouched"

    # Round-trips like any other node data, because it IS node data.
    g2 = flow.FlowGraph.from_dict(g.to_dict())
    assert g2.nodes[a.id].data["color"] == "#f43f5e"


def QColorOf(hexv):
    from PySide6.QtGui import QColor
    return QColor(hexv)


def test_a_junk_colour_paints_the_type_colour_not_a_black_hole(qapp):
    """QColor("nonsense") is *invalid*, and an invalid QColor paints black
    rather than raising — so a typo in a hand-edited flow would give a
    node-shaped hole and no clue why. Parse before painting."""
    import flow_canvas as fc
    _g, made = _chain(("click", {}))
    n = made[0]
    default = fc.node_header_color(n)
    for junk in ("nonsense", "#12", "", None, 7, [1, 2]):
        n.data["color"] = junk
        assert fc.node_tint(n) is None, f"{junk!r} should not parse"
        assert fc.node_header_color(n) == default, f"{junk!r} changed the colour"


def test_the_timeline_shows_a_nodes_own_colour(qapp):
    """One idea, two views. A tint that only appeared on the canvas would make
    the strip disagree with the graph it is a picture of."""
    import flow_timeline as ft
    _g, made = _chain(("click", {}), ("click", {}))
    made[0].data["color"] = "#84cc16"
    assert ft._seg_color(made[0]) == QColorOf("#84cc16")
    assert ft._seg_color(made[1]) != QColorOf("#84cc16")


def test_colouring_acts_on_the_whole_selection(qapp):
    """Colour is almost always applied to a group. Having to repeat it eight
    times is how people stop bothering — so it follows the selection, the way
    Duplicate already does."""
    import flow_canvas as fc
    g, made = _chain(("click", {}), ("click", {}), ("click", {}))
    canvas = fc.FlowCanvas(g)
    for n in made[:2]:
        canvas._scene.node_item(n.id).setSelected(True)
    canvas._set_color("#0ea5e9")
    assert [n.data.get("color") for n in made] == ["#0ea5e9", "#0ea5e9", None]
    canvas._set_color(None)
    assert [n.data.get("color") for n in made] == [None, None, None]


# ── reroute nodes on the canvas ───────────────────────────────────────────────
def _spread(*kinds):
    """_chain, but laid out down a column so wires have somewhere to be.

    _chain leaves every node at (0, 0), which is fine for a timeline and
    useless for hit-testing: the wires are all degenerate points.
    """
    import flow
    g = flow.FlowGraph()
    prev = g.add_node(flow.N_START, {}, 0, 0)
    made = []
    for i, (kind, data) in enumerate(kinds):
        n = g.add_node(flow.N_ACTION, {"step": {"kind": kind, "data": data}},
                       0, 160 * (i + 1))
        g.add_edge(prev.id, n.id)
        prev = n
        made.append(n)
    return g, made


def test_double_clicking_a_wire_puts_a_bend_in_it(qapp):
    """Unreal's stated reason for reroute nodes is "loops, and anywhere else
    where your code needs to double back on itself" — which is every loop-back
    in Macronaut. The gesture is the one every other editor uses."""
    import flow, flow_canvas as fc
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication
    g, made = _spread(("click", {}), ("click", {}))
    canvas = fc.FlowCanvas(g)
    canvas.resize(900, 600)
    edge = next(e for e in g.edges if e.src == made[0].id)
    item = canvas._scene._edges[edge.id]

    mid = item.path().pointAtPercent(0.5)
    canvas.centerOn(mid)
    view_pt = canvas.mapFromScene(mid)
    assert canvas.viewport().rect().contains(view_pt), "test aimed off-screen"
    QApplication.sendEvent(canvas.viewport(), QMouseEvent(
        QEvent.MouseButtonDblClick, QPointF(view_pt), Qt.LeftButton,
        Qt.LeftButton, Qt.NoModifier))

    routes = [n for n in g.nodes.values() if n.type == flow.N_REROUTE]
    assert len(routes) == 1, "a double-click on the wire should add exactly one"
    assert edge.dst == routes[0].id
    assert g.out_edge(routes[0].id, "out").dst == made[1].id
    assert canvas._scene.node_item(routes[0].id) is not None, "no item for it"


def test_deleting_a_bend_rejoins_the_wire_on_the_canvas_too(qapp):
    """The model rejoins; the scene has to keep up. Re-pointing the surviving
    EdgeItems rather than rebuilding, because the Delete key walks a list of
    selected items and a rebuild mid-loop leaves the caller holding dead ones."""
    import flow, flow_canvas as fc
    g, made = _spread(("click", {}), ("click", {}))
    canvas = fc.FlowCanvas(g)
    edge = next(e for e in g.edges if e.src == made[0].id)
    before = len(canvas._scene._edges)
    item = canvas._scene.insert_reroute(edge.id, 300, 300)
    assert item is not None
    assert len(canvas._scene._edges) == before + 1

    canvas._scene.delete_node(item.node.id)
    assert not [n for n in g.nodes.values() if n.type == flow.N_REROUTE]
    assert canvas._scene.node_item(item.node.id) is None
    assert len(canvas._scene._edges) == before, "the second half must go too"
    assert edge.dst == made[1].id
    for ei in canvas._scene._edges.values():
        assert not ei.path().isEmpty(), "a rejoined wire still has to be drawn"


def test_the_delete_key_dissolves_a_bend_rather_than_cutting_the_wire(qapp):
    import flow, flow_canvas as fc
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication
    g, made = _chain(("click", {}), ("click", {}))
    canvas = fc.FlowCanvas(g)
    edge = next(e for e in g.edges if e.src == made[0].id)
    item = canvas._scene.insert_reroute(edge.id, 300, 300)
    canvas._scene.clearSelection()
    item.setSelected(True)
    QApplication.sendEvent(canvas, QKeyEvent(QEvent.KeyPress, Qt.Key_Delete,
                                             Qt.NoModifier))
    assert not [n for n in g.nodes.values() if n.type == flow.N_REROUTE]
    assert edge.dst == made[1].id, "the wire must survive its own bend"


def test_a_bend_is_a_dot_not_a_card(qapp):
    """A card-shaped reroute would claim to be a step. It is not one: no
    settings, no runtime cost, and the flow reads the same without it."""
    import flow, flow_canvas as fc
    from PySide6.QtCore import QPointF
    g, made = _spread(("click", {}), ("click", {}))
    edge = next(e for e in g.edges if e.src == made[0].id)
    canvas = fc.FlowCanvas(g)
    item = canvas._scene.insert_reroute(edge.id, 300, 300)
    r = item.boundingRect()
    assert (r.width(), r.height()) == (fc.REROUTE_W, fc.REROUTE_W)
    assert r.width() < fc.NODE_W / 4
    # Both ports sit on the dot's own edges, or the wire would visibly miss it.
    assert item.port_pos("in") == item.scenePos() + QPointF(0, fc.REROUTE_W / 2)
    assert item.port_pos("out") == item.scenePos() + QPointF(fc.REROUTE_W,
                                                             fc.REROUTE_W / 2)


def test_the_timeline_leaves_bends_out(qapp):
    """A lane of boxes reading "reroute" four times describes the picture
    instead of the run."""
    import flow, flow_timeline as ft
    g, made = _chain(("wait", {"ms": 100}), ("wait", {"ms": 100}))
    strip = ft.TimelineStrip(g)
    before = list(strip._order)
    edge = next(e for e in g.edges if e.src == made[0].id)
    flow.insert_reroute(g, edge.id, 10, 10)
    strip.rebuild()
    assert strip._order == before


# ── scroll and drag in the Click editor ──────────────────────────────────────
# Combo indices, which are stored positions the editor speaks — not the order
# the segments appear in.
_I_CLICK, _I_SCROLL, _I_DRAG = 0, 7, 8


def _pick_family_segment(dlg, combo_index):
    """Click the family segment that selects `combo_index`.

    By what it selects, never by where it sits. The Click family gained a Drag
    segment *between* Click and Scroll, which silently retargeted every test
    that said `button(1)` at a different step kind — they went on passing while
    testing something else.
    """
    dlg._fam_group.button(dlg._fam_indices.index(combo_index)).click()


def test_the_click_family_toggles_between_click_drag_and_scroll(main_mod):
    """Press, drag and turn are the mouse's three verbs, so they live behind the
    one Click button rather than taking a palette button each. Same shape as the
    Type family's Key/Text toggle — which is also the toggle a PyQt5-only signal
    overload once broke at *construction* time, so building it is most of the
    value here."""
    dlg = main_mod.StepDialog(None, default_text_cps=20, family="click")
    try:
        dlg.show()
        assert dlg._type_combo.currentIndex() == _I_CLICK
        assert dlg._stack_click.isVisible()
        for idx, panel in ((_I_DRAG, dlg._stack_drag),
                           (_I_SCROLL, dlg._stack_scroll),
                           (_I_CLICK, dlg._stack_click)):
            _pick_family_segment(dlg, idx)
            assert dlg._type_combo.currentIndex() == idx
            assert panel.isVisible()
            # Exactly one panel at a time: a leftover visible panel is how a
            # dialog ends up saving fields nobody was looking at.
            assert sum(p.isVisible() for p in (dlg._stack_click, dlg._stack_drag,
                                               dlg._stack_scroll)) == 1
    finally:
        dlg.hide()


def test_a_drag_step_round_trips_through_its_editor(main_mod):
    """A step kind the editor cannot open is the trap this codebase already
    documents — the autoclick node answered a double-click with a message box.
    A drag has to open, show what it does, and save it back unchanged."""
    import flow
    from recorder import SeqStep
    step = SeqStep(SeqStep.DRAG, {"button": "right", "x": -900, "y": 300,
                                  "to_x": 120, "to_y": 640,
                                  "duration_ms": 1250}, 0)
    dlg = main_mod.StepDialog(step, default_text_cps=20, family="click")
    try:
        dlg.show()
        assert dlg._type_combo.currentIndex() == _I_DRAG
        assert dlg._stack_drag.isVisible()
        assert (dlg._drag_x.value(), dlg._drag_y.value()) == (-900, 300)
        assert (dlg._drag_to_x.value(), dlg._drag_to_y.value()) == (120, 640)
        assert dlg._drag_ms.value() == 1250
        assert dlg._drag_btn_grp.checkedId() == 1, "right button not reflected"
        dlg._on_ok()
        out = dlg._result_step
        assert out.kind == "drag"
        assert out.data == {"button": "right", "x": -900, "y": 300,
                            "to_x": 120, "to_y": 640, "duration_ms": 1250}
    finally:
        dlg.hide()


def test_a_new_drag_defaults_to_a_travel_time_that_is_actually_a_gesture(main_mod):
    """0 ms would be a teleport with a press on either side, which is the exact
    thing a click with `hold` already does and the reason this step exists."""
    import flow
    dlg = main_mod.StepDialog(None, default_text_cps=20, family="click")
    try:
        dlg.show()
        _pick_family_segment(dlg, _I_DRAG)
        dlg._on_ok()
        d = dlg._result_step.data
        assert d["duration_ms"] == flow.DEFAULT_DRAG_MS
        assert d["button"] == "left"
        assert flow.drag_moves(d) > 10
    finally:
        dlg.hide()


def test_the_click_panels_delay_cannot_ride_along_on_a_drag(main_mod):
    """Same trap Scroll documented: "Delay before" is a row inside the Click
    panel, so a value typed there and then toggled away from would be saved,
    invisibly, on a step whose editor never showed it."""
    dlg = main_mod.StepDialog(None, default_text_cps=20, family="click")
    try:
        dlg.show()
        dlg._delay.setValue(2500)
        _pick_family_segment(dlg, _I_DRAG)
        dlg._on_ok()
        assert dlg._result_step.delay_ms == 0
    finally:
        dlg.hide()


def test_both_drag_pickers_capture_into_their_own_boxes(main_mod):
    """Two pickers in one dialog. A shared countdown/timer would let the second
    one overwrite the first's target — which is what a third and fourth copy of
    the existing _start_pick would have risked."""
    from PySide6.QtGui import QCursor
    dlg = main_mod.StepDialog(None, default_text_cps=20, family="click")
    try:
        dlg.show()
        _pick_family_segment(dlg, _I_DRAG)
        dlg._begin_pick(dlg._btn_pick_drag_from, dlg._drag_x, dlg._drag_y)
        dlg._begin_pick(dlg._btn_pick_drag_to, dlg._drag_to_x, dlg._drag_to_y)
        assert not dlg._btn_pick_drag_from.isEnabled()
        assert not dlg._btn_pick_drag_to.isEnabled()
        # Both countdowns run to completion independently.
        for btn in (dlg._btn_pick_drag_from, dlg._btn_pick_drag_to):
            timer = btn.findChild(main_mod.QTimer)
            for _ in range(3):
                timer.timeout.emit()
        pos = QCursor.pos()
        assert (dlg._drag_x.value(), dlg._drag_y.value()) == (pos.x(), pos.y())
        assert (dlg._drag_to_x.value(), dlg._drag_to_y.value()) == (pos.x(), pos.y())
        assert dlg._btn_pick_drag_from.text() == dlg._btn_pick_drag_to.text()
        assert dlg._btn_pick_drag_from.isEnabled()
    finally:
        dlg.hide()


def test_a_scroll_step_round_trips_through_its_editor(main_mod):
    """Every control has to reopen showing what the step actually does. The
    values are read back through flow's own accessors, so a hand-written step
    cannot reopen as the first item in each control and be saved as that."""
    import flow
    from recorder import SeqStep
    step = SeqStep(SeqStep.SCROLL, {"direction": "right", "amount": 12,
                                    "speed_nps": 25, "at_cursor": False,
                                    "x": -900, "y": 300}, 0)
    dlg = main_mod.StepDialog(step, default_text_cps=20, family="click")
    try:
        dlg.show()
        assert dlg._type_combo.currentIndex() == 7
        assert dlg._scroll_amount.value() == 12
        assert dlg._scroll_speed.value() == 25
        assert dlg._scroll_pos_row.isVisible(), "a positioned scroll must show it"
        assert (dlg._scroll_x.value(), dlg._scroll_y.value()) == (-900, 300)
        dlg._on_ok()
        out = dlg._result_step
        assert out.kind == "scroll"
        assert out.data == {"direction": "right", "amount": 12, "speed_nps": 25,
                            "at_cursor": False, "x": -900, "y": 300}
    finally:
        dlg.hide()


def test_an_at_cursor_scroll_saves_no_position_at_all(main_mod):
    """Carrying a stale x/y on a step that ignores it is how a flow ends up
    scrolling somewhere nobody asked for the day someone flips the toggle."""
    dlg = main_mod.StepDialog(None, default_text_cps=20, family="click")
    try:
        dlg.show()
        _pick_family_segment(dlg, _I_SCROLL)
        dlg._scroll_x.setValue(500)
        dlg._scroll_y.setValue(500)
        assert not dlg._scroll_pos_row.isVisible(), "at-cursor hides the position"
        dlg._on_ok()
        d = dlg._result_step.data
        assert d["at_cursor"] is True
        assert "x" not in d and "y" not in d
        # Defaults worth pinning: down, three notches, unpaced.
        assert (d["direction"], d["amount"], d["speed_nps"]) == ("down", 3, 0)
    finally:
        dlg.hide()


def test_the_click_panels_delay_cannot_ride_along_on_a_scroll(main_mod):
    """"Delay before" is a row inside the Click panel. A value typed there and
    then toggled away from would otherwise be saved, invisibly, on a step whose
    editor never showed it."""
    dlg = main_mod.StepDialog(None, default_text_cps=20, family="click")
    try:
        dlg.show()
        dlg._delay.setValue(2500)
        _pick_family_segment(dlg, _I_SCROLL)
        dlg._on_ok()
        assert dlg._result_step.delay_ms == 0
    finally:
        dlg.hide()


def test_a_scroll_node_does_not_call_itself_a_click(qapp):
    """It keeps the mouse colour, because it is mouse work — but "Click" written
    across a node that never clicks costs more than one extra word does."""
    import flow, flow_canvas as fc
    g = flow.FlowGraph()
    n = g.add_node(flow.N_ACTION, {"step": {"kind": "scroll",
                                            "data": {"amount": 2}}})
    click = g.add_node(flow.N_ACTION, {"step": {"kind": "click", "data": {}}})
    assert fc.node_header_label(n)[1] == "Scroll"
    assert fc.node_header_color(n) == fc.node_header_color(click)


# ── Comment boxes (frames) ────────────────────────────────────────────────────
# A titled box drawn behind the graph that carries what stands on it. The tests
# below pin the four decisions that make it usable rather than merely present:
# it is not a routing obstacle, its body does not swallow clicks, dragging it
# moves its contents without selecting them, and deleting it keeps them.

def _framed(text="Login\nclicks through the banner"):
    """A flow with two nodes side by side and a comment box around them."""
    import flow, flow_canvas as fc
    g = flow.FlowGraph()
    prev = g.add_node(flow.N_START, {"name": flow.START_NAME}, 40, 40)
    made = []
    for i in range(4):
        n = g.add_node(flow.N_ACTION, {"step": {"kind": "click", "data": {}}},
                       40 + i * 260.0, 180)
        g.add_edge(prev.id, n.id, "out")
        prev = n
        made.append(n)
    canvas = fc.FlowCanvas(g)
    canvas.resize(1300, 600)
    scene = canvas.scene_()
    for n in made[1:3]:
        scene.node_item(n.id).setSelected(True)
    frame = canvas.wrap_selection_in_frame()
    frame.node.data["text"] = text
    frame.refresh()
    return canvas, frame, made


def test_a_comment_box_is_never_a_wire_obstacle(qapp):
    """⚠ Frames are kept out of FlowScene._nodes for exactly this. obstacles()
    is built from _nodes, so a frame in there would be something every wire had
    to route around — and since a frame is a *region* of the graph, every wire
    inside it would be pushed out of it. Wires cross frames freely."""
    canvas, frame, _made = _framed()
    obstacle_ids = {nid for nid, _r in canvas.scene_().obstacles()}
    assert frame.node.id not in obstacle_ids
    assert frame.node.id not in canvas.scene_()._nodes


def test_only_a_comment_box_header_and_grip_take_the_mouse(qapp):
    """The body has to be click-through or the region a comment labels becomes
    harder to work in than the rest of the canvas, which is backwards."""
    from PySide6.QtCore import QPointF
    canvas, frame, _made = _framed()
    w, h = frame.size()
    at = lambda p: canvas._frame_at(canvas.mapFromScene(frame.pos() + p))
    assert at(QPointF(40, 10)) is frame, "the title bar must be grabbable"
    assert at(QPointF(w - 6, h - 6)) is frame, "the resize grip must be grabbable"
    assert at(QPointF(w / 2, h - 40)) is None, "the body must be click-through"


def test_dragging_a_comment_box_carries_its_contents_but_not_the_selection(qapp):
    """Carrying them is the whole feature. *Not* selecting them matters just as
    much: the selection is what Delete and a colour change act on, and moving a
    box must not silently arm either of those against everything inside it."""
    from PySide6.QtCore import QEvent, QPointF, Qt
    import flow_canvas as fc
    canvas, frame, _made = _framed()
    scene = canvas.scene_()
    inside = scene.nodes_on_frame(frame.node.id)
    assert len(inside) == 2, "the wrap should have enclosed exactly two nodes"
    before = {it.node.id: QPointF(it.pos()) for it in inside}

    grab = canvas.mapFromScene(frame.pos() + QPointF(40, 10))
    _send_mouse(canvas, QEvent.MouseButtonPress, grab, Qt.LeftButton, Qt.LeftButton)
    _send_mouse(canvas, QEvent.MouseMove, grab + QPoint(120, 90),
                Qt.NoButton, Qt.LeftButton)
    _send_mouse(canvas, QEvent.MouseButtonRelease, grab + QPoint(120, 90),
                Qt.LeftButton, Qt.NoButton)

    for it in inside:
        assert it.pos() != before[it.node.id], "a node on the frame stayed put"
        assert not it.isSelected(), "carried nodes must not join the selection"
    assert frame.isSelected()


def test_deleting_a_comment_box_keeps_what_was_inside_it(qapp):
    """"I don't need this label" is the only thing deleting a box can mean, and
    the other reading would be an unrecoverable click on the same gesture."""
    canvas, frame, made = _framed()
    scene = canvas.scene_()
    scene.delete_node(frame.node.id)
    assert frame.node.id not in canvas.graph.nodes
    assert all(n.id in canvas.graph.nodes for n in made)
    assert len(canvas.graph.edges) == 4, "the flow's wiring must be untouched"


def test_a_comment_box_resizes_and_refuses_to_vanish(qapp):
    """A frame small enough to be invisible could never be grabbed again."""
    import flow, flow_canvas as fc
    canvas, frame, _made = _framed()
    frame.resize_to(700, 400)
    # Snapped like every other position on this canvas, so a row of boxes
    # lines up with the nodes and with each other.
    assert frame.size() == (fc._snap(700), fc._snap(400))
    frame.resize_to(1, 1)
    assert frame.size() == (flow.FRAME_MIN_W, flow.FRAME_MIN_H)


def test_the_c_key_wraps_the_selection(qapp):
    """The binding every other tool uses, because what you want to label is
    almost always already selected."""
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtCore import QEvent, Qt
    import flow, flow_canvas as fc
    g, made = _spread(("click", {}), ("click", {}))
    canvas = fc.FlowCanvas(g)
    for n in made:
        canvas.scene_().node_item(n.id).setSelected(True)
    before = len(canvas.scene_().frame_items())
    canvas.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_C, Qt.NoModifier))
    frames = canvas.scene_().frame_items()
    assert len(frames) == before + 1
    assert set(canvas.scene_().nodes_on_frame(frames[0].node.id)) == {
        canvas.scene_().node_item(n.id) for n in made}


def test_a_comment_box_survives_a_save_and_load(qapp):
    import flow, flow_canvas as fc
    canvas, frame, _made = _framed("Title line\nand a note")
    frame.resize_to(640, 300)
    g2 = flow.FlowGraph.from_dict(canvas.graph.to_dict())
    f2 = next(n for n in g2.nodes.values() if n.type == flow.N_FRAME)
    assert flow.frame_title(f2) == "Title line"
    assert flow.frame_body(f2) == "and a note"
    assert flow.frame_size(f2) == (fc._snap(640), fc._snap(300))


def test_a_loose_legacy_comment_becomes_a_box_and_a_wired_one_does_not(qapp):
    """⚠ The reason N_FRAME is a new type rather than a redefinition of the
    comment node that was already in flow.py. A comment has an "out" port and
    FlowInterpreter walks straight through it, so one wired into a chain is
    load-bearing — turning that into a port-less box would cut the flow in two
    on load. One with no wires at all cannot be doing anything, so it is safe
    to migrate and is what a comment was always trying to be."""
    import flow
    g = flow.FlowGraph()
    start = g.add_node(flow.N_START, {"name": flow.START_NAME}, 0, 0)
    loose = g.add_node(flow.N_COMMENT, {"text": "just a note"}, 300, 300)
    act = g.add_node(flow.N_ACTION, {"step": {"kind": "click", "data": {}}}, 0, 120)
    wired = g.add_node(flow.N_COMMENT, {"text": "in the chain"}, 0, 240)
    g.add_edge(start.id, act.id, "out")
    g.add_edge(act.id, wired.id, "out")

    g2 = flow.FlowGraph.from_dict(g.to_dict())
    assert g2.nodes[loose.id].type == flow.N_FRAME
    assert g2.nodes[wired.id].type == flow.N_COMMENT
    assert len(g2.edges) == 2, "migrating must not disturb any wiring"


def test_a_comment_box_is_not_work_and_not_on_the_timeline(qapp):
    """A flow of nothing but comment boxes has nothing to run, and a timeline
    lane that says "Comment" is describing the picture, not the run."""
    import flow, flow_timeline
    g = flow.FlowGraph()
    g.add_node(flow.N_START, {"name": flow.START_NAME}, 0, 0)
    g.add_node(flow.N_FRAME, {"text": "just a note"}, 40, 40)
    assert flow.has_work(g) is False
    assert flow.N_FRAME in flow.ANNOTATION_TYPES
    strip = flow_timeline.TimelineStrip(g)
    strip.rebuild()
    assert all(g.nodes[nid].type != flow.N_FRAME for nid in strip._order)


def test_the_comment_prompt_has_a_field_you_can_see(qapp):
    """⚠ The blanket `QWidget { background: $bg }` rule matches subclasses, so
    the editor QInputDialog builds for itself — which carries no object name —
    was painted the dialog's own background with no border. Asking for a comment
    opened a dialog holding a label, two buttons and no visible field at all.

    Rendered rather than asserted on the stylesheet: this is the one class of
    bug that only exists once something paints it (same lesson as the black
    square in the If editor)."""
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QPlainTextEdit
    import main
    canvas, _frame, _made = _framed()
    was = qapp.styleSheet()
    try:
        qapp.setStyleSheet(main.MISSION)
        dlg = canvas.frame_text_dialog("Login sequence\nWaits for the field.")
        eds = dlg.findChildren(QPlainTextEdit)
        assert len(eds) == 1, "the prompt must be the multi-line one"
        assert eds[0].objectName() == "textBody", \
            "the editor must opt into the app's input styling"
        dlg.show()
        qapp.processEvents()
        img = dlg.grab().toImage()
        ed = eds[0]
        inside = ed.mapTo(dlg, QPoint(ed.width() // 2, ed.height() - 8))
        outside = QPoint(dlg.width() - 3, dlg.height() // 2)
        assert img.pixelColor(inside) != img.pixelColor(outside), \
            "the text field is painted the same colour as the dialog behind it"
        dlg.hide()
    finally:
        qapp.setStyleSheet(was)


def test_cancelling_the_comment_prompt_leaves_no_box_behind(qapp):
    """The box used to be placed first and asked about afterwards, so cancelling
    — which is exactly how you say you did not want one — left one anyway."""
    import flow
    canvas, _frame, _made = _framed()
    before = len(flow.frames(canvas.graph))
    canvas.ask_frame_text = lambda initial="": None
    assert canvas.new_frame_at(500, 500) is None
    assert len(flow.frames(canvas.graph)) == before


def test_copy_cut_and_duplicate_take_the_comment_boxes_with_them(qapp):
    """⚠ Copy, Cut and Duplicate all read selected_node_ids(), which is steps
    only — while Ctrl+A and a marquee both select comment boxes. So copying a
    region and pasting it silently dropped every label, and cutting one left the
    boxes hanging over an empty stretch of canvas."""
    import flow
    canvas, frame, made = _framed()
    scene = canvas.scene_()

    scene.clearSelection()
    frame.setSelected(True)
    for n in made[1:3]:
        scene.node_item(n.id).setSelected(True)
    assert canvas.selected_frame_ids() == [frame.node.id]

    assert canvas.copy_selection() == 3, "the box has to travel with the steps"
    assert canvas.paste() == 3
    assert len(flow.frames(canvas.graph)) == 2

    # And a duplicate of a box on its own is a box, not nothing.
    scene.clearSelection()
    scene.frame_item(frame.node.id).setSelected(True)
    assert canvas.duplicate_selection() == 1
    assert len(flow.frames(canvas.graph)) == 3

    # Cut takes it away rather than leaving it standing over the hole.
    scene.clearSelection()
    for fi in scene.frame_items():
        fi.setSelected(True)
    canvas.cut_selection()
    assert flow.frames(canvas.graph) == []


def test_pasting_a_comment_box_does_not_break_the_auto_chain(qapp):
    """_last_added is what the next palette add chains onto, and a comment box
    has no ports — so it must never become the anchor."""
    import flow
    canvas, frame, made = _framed()
    scene = canvas.scene_()
    scene.clearSelection()
    scene.node_item(made[1].id).setSelected(True)
    frame.setSelected(True)
    canvas.duplicate_selection()
    assert scene._last_added in scene._nodes


def test_the_library_counts_branches_as_steps_not_only_actions(
        library, main_mod):
    """A branching flow's step count must ask flow.WORK_TYPES.

    `to_linear_steps()` returns None the moment a flow branches, and the
    fallback used to count N_ACTION alone — so the branches, which are the
    reason it branched at all, were invisible. A flow made only of If nodes
    reported "0 steps" while Play ran it perfectly, because has_work() asks
    WORK_TYPES. Same split 2.0.7 fixed in has_content().
    """
    import flow

    dlg, scripts, _root = library
    g = flow.FlowGraph()
    a = g.add_node("if", {"condition": {"type": "pixel", "x": 1, "y": 1,
                                        "color": "#ffffff", "tolerance": 10}})
    b = g.add_node("if", {"condition": {"type": "pixel", "x": 2, "y": 2,
                                        "color": "#ffffff", "tolerance": 10}})
    g.add_edge(g.start_node().id, "out", a.id)
    g.add_edge(a.id, "true", b.id)
    path = scripts / "branchy.json"
    g.save(str(path))

    assert flow.has_work(g), "the flow really is runnable"
    assert g.to_linear_steps() is None, "and really does reach the fallback"
    assert dlg._meta_for(path).startswith("2 steps")


@pytest.mark.parametrize("branching", [False, True])
def test_the_library_counts_a_flow_far_past_a_thousand_steps(
        library, main_mod, branching):
    """No cap, on either counting path, at any size.

    A real flow reached 999 `action` nodes and the library said "999 steps",
    which reads exactly like a ceiling — it was not one, the flow genuinely had
    999 actions, but "999" is the last number anyone would want to take on
    trust. Both paths are pinned here at a size no round number sits near:
    `to_linear_steps()` walks a chain and could be truncated, and the fallback
    is a comprehension that could grow a slice. 1207 is deliberately not 1000,
    1024 or 1200 — a wrong answer has to be wrong by a number nobody typed.
    """
    import flow

    dlg, scripts, _root = library
    n = 1207
    g = flow.FlowGraph()
    prev, prev_port = g.add_node(flow.N_START, {}).id, "out"
    for i in range(n):
        if branching:
            # Every node an If, chained down its `true` port: has_work() counts
            # these and to_linear_steps() refuses them, so this is the fallback.
            node = g.add_node("if", {"condition": {
                "type": "pixel", "x": i, "y": 1, "color": "#ffffff",
                "tolerance": 10}})
            g.add_edge(prev, node.id, prev_port)
            prev, prev_port = node.id, "true"
        else:
            node = g.add_node("action", {"step": {"kind": "wait",
                                                  "data": {"ms": 1}}})
            g.add_edge(prev, node.id, prev_port)
            prev, prev_port = node.id, "out"
    path = scripts / "huge.json"
    g.save(str(path))

    assert (g.to_linear_steps() is None) is branching, "the intended path ran"
    if not branching:
        assert len(g.to_linear_steps()) == n
    assert dlg._meta_for(path).startswith(f"{n} steps"), \
        f"library said {dlg._meta_for(path)!r}"


# ── the wait duration box, and the comment box's header band ──────────────────
def test_the_duration_box_steps_onto_round_numbers(qapp, main_mod):
    """It used to step BY a fixed amount, so 950 ms + one click was 1.05 s and
    every value above it stayed 50 ms off the grid for the life of the box —
    with 1 s unreachable from below without typing it."""
    sb = main_mod._durspin(0, 600000, 950)
    ups = []
    for _ in range(3):
        sb.stepBy(1)
        ups.append(sb.value())
    assert ups == [1000, 1100, 1200], ups

    # Down off the boundary drops into the finer grid rather than skipping it.
    sb.setValue(1000)
    sb.stepBy(-1)
    assert sb.value() == 950

    # A value already off the grid (typed, or loaded from an older flow) lands
    # on the grid first instead of carrying its offset forever.
    sb.setValue(137)
    sb.stepBy(1)
    assert sb.value() == 150
    sb.stepBy(-1)
    assert sb.value() == 100

    # Third tier: 100 ms steps would put ten minutes 5400 clicks from zero.
    sb.setValue(60000)
    sb.stepBy(1)
    assert sb.value() == 61000
    sb.stepBy(-1)
    assert sb.value() == 60000
    sb.stepBy(-1)
    assert sb.value() == 59900

    # The floor is the range, not a negative value.
    sb.setValue(0)
    sb.stepBy(-1)
    assert sb.value() == 0


def test_the_add_node_sidebar_leaves_the_widest_label_some_slack(window):
    """Sized to exactly the widest size hint, the sidebar had zero margin for
    error and "Comment" came back clipped by one letter. A hint is a glyph
    advance summed in one font; what is painted is ink, through whatever font
    carries the emoji, at whatever the monitor's scale rounds to."""
    from PySide6.QtWidgets import QPushButton

    btns = [b for b in window.findChildren(QPushButton)
            if b.objectName() == "palette_btn"]
    assert btns
    widest = max(b.sizeHint().width() for b in btns)
    side = btns[0].parentWidget()
    lm, _t, rm, _b = side.layout().getContentsMargins()
    assert side.width() - lm - rm - widest >= main_mod_slack(), \
        "the sidebar is back to fitting the widest label exactly"


def main_mod_slack() -> int:
    import main as main_mod
    return main_mod.PALETTE_SLACK


def test_the_palette_is_sized_under_the_theme_not_qt_defaults(qapp, main_mod):
    """⚠ THE bug behind the clipped "Comment", and it was never about slack.

    `main()` built the whole window and applied the application stylesheet
    *afterwards*. Every width measured during construction was therefore taken
    on a Qt-default 9 pt button with default padding, and then repainted in the
    theme's 14 pt font: the palette's hints came back tiny, the `max(132, ...)`
    floor won, and the buttons were frozen holding 76 px of content for a label
    that lays out at 92. Two letters short — exactly what was reported, and
    invisible to every test here because tests apply the stylesheet first.

    So drive the broken order deliberately and require the palette to recover.
    """
    from PySide6.QtWidgets import QPushButton, QStyleOptionButton, QStyle
    from PySide6.QtCore import QRect

    before = qapp.styleSheet()
    qapp.setStyleSheet("")
    w = main_mod.MainWindow()
    try:
        qapp.setStyleSheet(main_mod.THEMES["mission"])
        w.show()
        # Twice: the deferred re-measure runs on the next turn of the loop,
        # because at showEvent time Qt has not repolished against the new sheet.
        qapp.processEvents()
        qapp.processEvents()

        btns = [b for b in w.findChildren(QPushButton)
                if b.objectName() == "palette_btn"]
        assert len(btns) >= 9, "expected the whole palette, got %d" % len(btns)
        for b in btns:
            opt = QStyleOptionButton()
            opt.initFrom(b)
            opt.rect = QRect(0, 0, 1000, 34)
            opt.text = b.text()
            chrome = 1000 - b.style().subElementRect(
                QStyle.SE_PushButtonContents, opt, b).width()
            advance = b.fontMetrics().horizontalAdvance(b.text())
            assert b.width() - chrome >= advance, (
                "%r lays out at %d px in the theme's font and the button, sized "
                "before the theme existed, leaves room for %d"
                % (b.text(), advance, b.width() - chrome))
    finally:
        w.hide()
        qapp.setStyleSheet(before)


def test_the_window_title_carries_the_version_it_actually_is(window):
    """⚠ It said "Macronaut · 2.0" through every release up to 2.2.0.

    That string is the taskbar, alt-tab, and the title bar of every screenshot
    attached to a bug report — where a stale version number is worse than no
    version number, because it will be believed and then debugged against.
    """
    import version
    assert version.__version__ in window.windowTitle(), window.windowTitle()


def test_the_library_buttons_fit_at_the_dialogs_own_minimum_width(qapp, tmp_path,
                                                                  monkeypatch):
    """⚠ Same failure as the palette, in a different row, and it was already
    there before anything was added to it: at the old 620px minimum "Import…"
    clipped by 3px and "Open folder" by 13. Qt shaves a label rather than
    refuse to shrink a window, and it does it silently.

    Two things had to be true, and only the second is obvious. The dialog
    cannot be narrower than its own button row — and the buttons themselves
    need a floor, because `_btn` sets no minimum width and the stretch in the
    middle of the row swallows any extra width the dialog is given.
    """
    import main as main_mod
    monkeypatch.setattr(main_mod, "scripts_dir", lambda: tmp_path)
    dlg = main_mod.ScriptLibraryDialog(None, main_mod.SettingsManager())
    try:
        dlg.resize(dlg.minimumWidth(), dlg.minimumHeight())
        dlg.show()
        qapp.processEvents()
        for b in (dlg._btn_import, dlg._btn_examples, dlg._btn_folder,
                  dlg._btn_delete, dlg._btn_merge, dlg._btn_open):
            need = main_mod._label_width(b)
            assert b.width() >= need, (
                f"{b.text()!r} is {need - b.width()}px too narrow for its "
                "label at the dialog's own minimum width")
    finally:
        dlg.hide()


def test_a_palette_button_holds_its_own_label_not_just_the_sidebar(window):
    """⚠ Both earlier attempts at the clipped "Comment" widened the *sidebar*
    and left every button still asking for a hard-coded 124 px — which is 89 px
    of content once the style's borders and padding come off, for a label that
    lays out at 92. The text was three pixels from being cut the entire time and
    stayed whole only for as long as the sidebar happened to be the thing
    setting the width; anything that made it not so took the last glyph off,
    silently, because Qt clips instead of complaining.

    So the assertion belongs on the button: its own minimum width has to hold
    its own text. That is true wherever it is put and whatever contains it.
    """
    from PySide6.QtWidgets import QPushButton, QStyleOptionButton, QStyle
    from PySide6.QtCore import QRect

    btns = [b for b in window.findChildren(QPushButton)
            if b.objectName() == "palette_btn"]
    assert len(btns) >= 9, "expected the whole palette, got %d" % len(btns)
    for b in btns:
        opt = QStyleOptionButton()
        opt.initFrom(b)
        opt.rect = QRect(0, 0, 1000, 34)
        opt.text = b.text()
        # Chrome is constant in the width, so a 1000 px probe rect reads it off.
        chrome = 1000 - b.style().subElementRect(
            QStyle.SE_PushButtonContents, opt, b).width()
        advance = b.fontMetrics().horizontalAdvance(b.text())
        room = b.minimumWidth() - chrome
        assert room >= advance, (
            "%r lays out at %d px and its own minimum leaves room for %d"
            % (b.text(), advance, room))


def test_the_comment_box_header_is_filled_all_the_way_down(qapp):
    """The band is a rounded rect with a plain rect over its bottom edge to
    square the lower corners. Under QPainterPath's default odd-even fill the
    OVERLAP counts as outside, so the bottom 12 px of a 26 px header was punched
    out and showed the faint body tint — "the top of the node is half dark".

    Rendered rather than reasoned about: this is a paint-only bug, so only
    something that paints it can see it.
    """
    from PySide6.QtGui import QImage, QPainter
    import flow, flow_canvas

    n = flow.FlowNode("f", flow.N_FRAME, {"text": "Region"}, 0, 0)
    flow.set_frame_size(n, 260, 160)
    item = flow_canvas.FrameItem(n)
    hdr = int(item.header_h())

    img = QImage(260, 160, QImage.Format_ARGB32)
    img.fill(0)
    p = QPainter(img)
    item.paint(p, None, None)
    p.end()

    mid = 130
    top = img.pixelColor(mid, 4)              # solidly inside the band
    low = img.pixelColor(mid, hdr - 4)        # the strip that used to vanish
    body = img.pixelColor(mid, hdr + 40)      # the faint interior
    assert top.alpha() > 200 and low.alpha() > 200, \
        f"header not solid: top a={top.alpha()} low a={low.alpha()}"
    assert (low.red(), low.green(), low.blue()) == (top.red(), top.green(), top.blue()), \
        f"bottom of the header {low.getRgb()} differs from its top {top.getRgb()}"
    assert body.alpha() < low.alpha(), "the interior should be fainter than the band"


def test_the_region_overlay_outlives_the_method_that_opened_it(qapp, main_mod):
    """⚠ Reported as a flat "select region on screen doesn't work", and it did
    not — since the first commit.

    `SettingsTab._select_region` built the overlay as a bare local and let it
    fall out of scope, so Python collected it the instant the method returned
    and the window was gone before anyone could drag in it. The sibling
    `_launch_region_picker` has documented the fix in its own docstring the
    whole time ("Reuses the module-level keep-alive list so the overlay isn't
    garbage-collected") — one call site of two never got the memo.

    The assertion is on the keep-alive list, because that is the thing whose
    absence killed it. A test that only checked the widget existed would pass
    on the broken version too: it is alive until the caller's frame goes away.
    """
    main_mod._active_selector.clear()
    tab = main_mod.SettingsTab(main_mod.SettingsManager())
    try:
        tab._select_region()
        held = list(main_mod._active_selector)
        assert held, "the overlay was not retained — it will be collected"
        assert isinstance(held[0], main_mod.RegionSelector)
        # Closing is what every exit does (a picked region, Escape, and a drag
        # too small to count all call close()), and it must let go again.
        held[0].close()
        qapp.processEvents()
        assert not main_mod._active_selector, "closing left a dead wrapper behind"
    finally:
        main_mod._active_selector.clear()
        tab.hide()


def test_every_node_the_engine_runs_can_be_created_from_the_palette(window, no_editor):
    """Auto-Click was runnable, saveable and editable but had no palette button,
    so it was the one node in the app you could not make. Two Settings cards
    (click region, pause on focus loss) read from an autoclick node's own data
    and nothing else, which left them inert for every flow anyone could build —
    a tester reported them as "couldn't test those", and they were right.

    Pinned as the general rule rather than the one node: an action kind the
    interpreter honours and an editor exists for should be reachable.
    """
    import flow
    tab = window._sequence_tab
    before = set(tab.graph.nodes)
    tab._add_node("action:autoclick", 400.0, 400.0)
    made = set(tab.graph.nodes) - before
    assert len(made) == 1
    node = tab.graph.nodes[made.pop()]
    assert node.type == flow.N_ACTION
    # The family is recorded before the editor opens, so the node on the canvas
    # already looks like what was asked for while it is still being configured.
    assert node.data.get("preset_kind") == "autoclick"
    assert flow.action_kind(node) == "autoclick"


# ── the update dialog ─────────────────────────────────────────────────────────
#
# ⚠ This dialog had **no tests at all** until 3 September 2026, and was
# listed as one of the things that "needs a human at a window". Most of it
# does not. What genuinely needs eyes is whether it *looks* right; whether the
# notes render, whether Install is offered when there is nothing to install,
# and what each button returns are all reachable under the offscreen platform —
# which is the policy this file exists to apply.
#
# It matters more than a dialog usually would: it is the only screen in the app
# whose whole job is telling someone what a new version changes, and it is
# where the release notes are read. Eight releases shipped with none.

def _update_info(**kw):
    import updater
    fields = dict(version="9.9.9", url="https://example.invalid/Macronaut.exe",
                  sha256="0" * 64, size=1234, notes="")
    fields.update(kw)
    return updater.UpdateInfo(**fields)


def test_the_update_dialog_shows_the_release_notes(qapp):
    """The notes are the entire point of the dialog.

    A user is being asked to replace a working program with a different one;
    what changed is the only information that makes that a decision rather
    than a leap. `update.json` carries them, and this is where they surface.
    """
    import updater_ui
    notes = "Fixed the thing.\n\nAlso fixed the other thing."
    dlg = updater_ui.UpdateDialog(_update_info(notes=notes))
    try:
        from PySide6.QtWidgets import QLabel, QTextBrowser
        browsers = dlg.findChildren(QTextBrowser)
        assert browsers, (
            "the dialog has no text view at all, so the notes cannot be shown")
        assert any(notes in w.toPlainText() for w in browsers), (
            "the release notes are not displayed anywhere in the update dialog")

        import version
        labels = " ".join(w.text() for w in dlg.findChildren(QLabel))
        assert "9.9.9" in labels, "the dialog does not name the new version"
        assert version.__version__ in labels, (
            "the dialog does not say which version you are on, so 'is this "
            "newer' is left to the reader")
    finally:
        dlg.deleteLater()


def test_install_is_refused_until_a_verified_download_exists(qapp, monkeypatch):
    """⚠ The safety property, not a nicety.

    `Install and restart` hands a path to the swap. With nothing staged there
    is no path, and the swap is the one step in the update with no do-over —
    `tools/rehearse_swap.py` exists because of exactly that. So the button is
    disabled until a download has been fetched *and* checked against the
    manifest's SHA-256, and the status line says which state it is in.
    """
    import updater, updater_ui
    monkeypatch.setattr(updater, "is_frozen", lambda: True)

    dlg = updater_ui.UpdateDialog(_update_info(notes="x"))
    try:
        assert not dlg._install.isEnabled(), (
            "Install is offered with nothing staged — it would try to swap in "
            "a file that does not exist")
        assert "download" in dlg._status.text().lower()

        dlg.set_staged(r"C:\somewhere\Macronaut.exe")
        assert dlg._install.isEnabled(), "a verified download does not enable Install"
        assert "verified" in dlg._status.text().lower(), (
            "the status does not say the download was verified, which is the "
            "reason it is safe to press the button")
    finally:
        dlg.deleteLater()


def test_running_from_source_says_so_instead_of_offering_a_swap(qapp, monkeypatch):
    """Not frozen means there is no .exe to replace.

    Silently disabling the button would read as a broken dialog. It explains.
    """
    import updater, updater_ui
    monkeypatch.setattr(updater, "is_frozen", lambda: False)
    dlg = updater_ui.UpdateDialog(_update_info(notes="x"))
    try:
        assert not dlg._install.isEnabled()
        assert "source" in dlg._status.text().lower(), dlg._status.text()
    finally:
        dlg.deleteLater()


def test_each_button_reports_the_choice_it_names(qapp, monkeypatch):
    """Skip / Later / Install are three different answers and the caller acts
    on them differently — Skip suppresses this version for good."""
    import updater, updater_ui
    monkeypatch.setattr(updater, "is_frozen", lambda: True)
    for button, expected in (("_skip", updater_ui.UpdateDialog.SKIP),
                             ("_later", updater_ui.UpdateDialog.LATER),
                             ("_install", updater_ui.UpdateDialog.INSTALL)):
        dlg = updater_ui.UpdateDialog(_update_info(notes="x"),
                                      staged_path=r"C:\x\Macronaut.exe")
        try:
            getattr(dlg, button).click()
            assert dlg.choice == expected, (
                f"{button} reported {dlg.choice!r}, not {expected!r}")
        finally:
            dlg.deleteLater()


def test_progress_and_errors_reach_the_dialog(qapp, monkeypatch):
    """A download with no visible progress reads as a hang, and an error that
    leaves the bar running reads as one too."""
    import updater, updater_ui
    monkeypatch.setattr(updater, "is_frozen", lambda: True)
    dlg = updater_ui.UpdateDialog(_update_info(notes="x"))
    try:
        dlg.on_progress(30, 100)
        assert dlg._bar.isVisibleTo(dlg)
        assert dlg._bar.maximum() == 100 and dlg._bar.value() == 30

        # A server that sends no length must not freeze the bar at 0%.
        dlg.on_progress(5, 0)
        assert dlg._bar.maximum() == 0, "unknown total is not shown as indeterminate"

        dlg.on_error("the download did not match its checksum")
        assert not dlg._bar.isVisibleTo(dlg), (
            "the progress bar is still running after an error")
        assert "checksum" in dlg._status.text()
    finally:
        dlg.deleteLater()


# ── the trashcan ──────────────────────────────────────────────────────────────
#
# ⚠ Zero tests until 4 September 2026, on the only gesture in the canvas that
# destroys work. Wire routing has nineteen. The asymmetry is the wrong way
# round: a mis-routed wire is visible and annoying, and a node deleted by a
# gesture the user did not think they made is gone.

def _drag_node_to(canvas, node_id, view_pos, *, press_first=True):
    """Press on a node, travel, release at `view_pos` (viewport coordinates)."""
    from PySide6.QtCore import QPointF, QEvent, Qt
    item = canvas.scene_().node_item(node_id)
    start = canvas.mapFromScene(item.sceneBoundingRect().center())
    if press_first:
        _send_mouse(canvas, QEvent.MouseButtonPress, QPointF(start),
                    Qt.LeftButton, Qt.LeftButton)
    _send_mouse(canvas, QEvent.MouseMove, QPointF(view_pos),
                Qt.NoButton, Qt.LeftButton)
    _send_mouse(canvas, QEvent.MouseButtonRelease, QPointF(view_pos),
                Qt.LeftButton, Qt.NoButton)


def _trash_centre(canvas):
    from PySide6.QtCore import QPointF
    g = canvas._trash.geometry()
    return QPointF(g.center())


def test_dragging_a_node_onto_the_trash_deletes_it(canvas):
    """The gesture works at all — without this the three guards below could
    all pass on a trashcan that never fires."""
    import flow
    canvas.resize(700, 500)
    canvas.show()
    n = canvas.graph.add_node(flow.N_ACTION,
                              {"step": {"kind": "wait", "data": {"ms": 1}}},
                              x=40, y=40)
    canvas.scene_().rebuild()
    assert n.id in canvas.graph.nodes

    _drag_node_to(canvas, n.id, _trash_centre(canvas))
    assert n.id not in canvas.graph.nodes, "the trashcan did not delete the node"


def test_a_click_on_a_node_never_deletes_it(canvas):
    """⚠ The safety property. `drop_on_trash` is gated on `started`, so a press
    and release that never travelled is not a drag and must not destroy
    anything — even when the node happens to sit under the trashcan, which on a
    small window it easily can."""
    import flow
    from PySide6.QtCore import QPointF, QEvent, Qt
    canvas.resize(700, 500)
    canvas.show()
    n = canvas.graph.add_node(flow.N_ACTION,
                              {"step": {"kind": "wait", "data": {"ms": 1}}},
                              x=40, y=40)
    canvas.scene_().rebuild()

    centre = _trash_centre(canvas)
    _send_mouse(canvas, QEvent.MouseButtonPress, centre, Qt.LeftButton, Qt.LeftButton)
    _send_mouse(canvas, QEvent.MouseButtonRelease, centre, Qt.LeftButton, Qt.NoButton)
    assert n.id in canvas.graph.nodes, (
        "a press and release with no travel deleted a node")


def test_dropping_a_node_anywhere_else_keeps_it(canvas):
    """The other half: the trashcan must not be a general delete-on-release."""
    import flow
    from PySide6.QtCore import QPointF
    canvas.resize(700, 500)
    canvas.show()
    n = canvas.graph.add_node(flow.N_ACTION,
                              {"step": {"kind": "wait", "data": {"ms": 1}}},
                              x=40, y=40)
    canvas.scene_().rebuild()

    _drag_node_to(canvas, n.id, QPointF(120.0, 90.0))
    assert n.id in canvas.graph.nodes, "dropping away from the trash deleted it"


def test_the_trashcan_takes_the_whole_selection_with_it(canvas):
    """⚠ Documented here because it is a lot of destruction from one gesture,
    and because it could plausibly be 'fixed' in either direction by someone
    who did not know it was deliberate: dragging one node of a selection onto
    the trash deletes *all* of them, which is what makes it useful for clearing
    a region and what makes it worth being sure about."""
    import flow
    canvas.resize(700, 500)
    canvas.show()
    made = []
    for i in range(3):
        made.append(canvas.graph.add_node(
            flow.N_ACTION, {"step": {"kind": "wait", "data": {"ms": 1}}},
            x=40 + i * 30, y=40))
    canvas.scene_().rebuild()
    for n in made:
        canvas.scene_().node_item(n.id).setSelected(True)

    _drag_node_to(canvas, made[0].id, _trash_centre(canvas))
    left = [n.id for n in made if n.id in canvas.graph.nodes]
    assert not left, f"selection survived the trash: {left}"


def test_the_trashcan_is_hidden_until_a_drag_starts(canvas):
    """It cannot swallow a drop it is not showing — `_trash_contains` checks
    `isVisible()` first — and an always-present bin over the canvas would be
    both noise and a hazard."""
    import flow
    canvas.resize(700, 500)
    canvas.show()
    assert not canvas._trash.isVisible(), "the trashcan is showing at rest"

    n = canvas.graph.add_node(flow.N_ACTION,
                              {"step": {"kind": "wait", "data": {"ms": 1}}},
                              x=40, y=40)
    canvas.scene_().rebuild()
    _drag_node_to(canvas, n.id, _trash_centre(canvas))
    assert not canvas._trash.isVisible(), "the trashcan stayed up after the drop"


# ── Clear ─────────────────────────────────────────────────────────────────────
#
# ⚠ The other destructive control, and it had no tests either. Unlike the
# trashcan it takes *everything*, and unlike the trashcan its only safeguard is
# a modal question — which is exactly the kind of thing a later simplification
# removes on the grounds that it is annoying.

def _wired_flow(tab, n=3):
    """A flow with real work in it, so `_clear` does not take its early exit."""
    import flow
    for i in range(n):
        tab._graph.add_node(flow.N_ACTION,
                            {"step": {"kind": "wait", "data": {"ms": 1}}},
                            x=40 + i * 30, y=40)
    tab._canvas.set_graph(tab._graph)
    return len(tab._graph.nodes)


def test_clear_asks_before_deleting_everything(window, main_mod, monkeypatch):
    """⚠ The safeguard is a question and nothing else.

    `Clear` is styled `danger` and wipes every node on the canvas. There is no
    undo in this app, and a flow can be an afternoon's work. So the only thing
    between a misclick and losing it is this dialog: assert it is actually
    asked, not merely that clearing works.
    """
    tab = window._sequence_tab
    tab._graph = tab._new_graph()
    before = _wired_flow(tab)
    assert before > 1

    asked = []

    def fake_question(parent, title, text, *a, **k):
        asked.append((title, text))
        return main_mod.QMessageBox.No

    monkeypatch.setattr(main_mod.QMessageBox, "question", fake_question)
    tab._clear()

    assert asked, "Clear deleted the flow without asking"
    assert len(tab._graph.nodes) == before, (
        "answering No still cleared the canvas")


def test_clear_wipes_the_canvas_when_the_answer_is_yes(window, main_mod,
                                                       monkeypatch):
    """The positive case, so the guard above cannot pass on a Clear that never
    clears anything."""
    tab = window._sequence_tab
    tab._graph = tab._new_graph()
    _wired_flow(tab)

    monkeypatch.setattr(main_mod.QMessageBox, "question",
                        lambda *a, **k: main_mod.QMessageBox.Yes)
    tab._clear()

    import flow
    assert not flow.has_work(tab._graph), "Yes did not clear the flow"
    # A cleared canvas is a *new* flow, not an empty one: Start has to survive
    # or the next node added has nothing to chain onto.
    assert tab._graph.start_node() is not None, "Clear left no Start node"


def test_clear_does_not_nag_when_there_is_nothing_to_clear(window, main_mod,
                                                           monkeypatch):
    """A fresh canvas holds only Start. Asking "delete all nodes?" there is a
    dialog about nothing, and the early return exists to avoid it."""
    tab = window._sequence_tab
    tab._graph = tab._new_graph()
    tab._canvas.set_graph(tab._graph)

    asked = []
    monkeypatch.setattr(main_mod.QMessageBox, "question",
                        lambda *a, **k: asked.append(1) or main_mod.QMessageBox.Yes)
    tab._clear()
    assert not asked, "Clear asked about an empty canvas"


def test_clearing_the_session_history_asks_first(window, main_mod, monkeypatch):
    """The third destructive control, and the last one without a test.

    Lower stakes than the canvas two — this deletes recorded session stats,
    not somebody's flow — but the shape is identical: a `danger` button whose
    only safeguard is a question, deleting something that is not recoverable
    and not backed up anywhere. `StatsManager.clear_history` empties the list
    and writes the file, so there is nothing to undo from.
    """
    tab = window._stats_tab
    stats = tab._stats

    from stats import Session
    from datetime import datetime
    stats.sessions.append(Session(start_time=datetime.now()))
    before = len(stats.sessions)
    assert before, "no history to clear, so this test proves nothing"

    asked = []

    def refuse(parent, title, text, *a, **k):
        asked.append(title)
        return main_mod.QMessageBox.No

    monkeypatch.setattr(main_mod.QMessageBox, "question", refuse)
    tab._clear_history()
    assert asked, "history was deleted without asking"
    assert len(stats.sessions) == before, "answering No still deleted it"

    monkeypatch.setattr(main_mod.QMessageBox, "question",
                        lambda *a, **k: main_mod.QMessageBox.Yes)
    tab._clear_history()
    assert not stats.sessions, "Yes did not clear the history"
    assert tab._table.rowCount() == 0, (
        "the table still shows rows for sessions that no longer exist")


def test_a_failed_save_tells_the_user_nothing_was_lost(window, main_mod,
                                                       monkeypatch, tmp_path):
    """⚠ The reassurance is the point, and it is the part most likely to be
    trimmed later as wordy.

    Since `FlowGraph.save` became atomic, a failed save genuinely leaves any
    previous version of the file untouched — and leaves the flow on the canvas
    open either way. Somebody staring at a save error over an afternoon's work
    needs to be told that, not handed an OSError. The message also has to say
    something actionable, because "Permission denied" alone leaves them with
    nowhere to go.
    """
    import flow
    tab = window._sequence_tab
    tab._graph = tab._new_graph()
    tab._graph.add_node(flow.N_ACTION,
                        {"step": {"kind": "wait", "data": {"ms": 1}}})

    target = tmp_path / "afternoon.json"
    target.write_text('{"version": 2, "nodes": []}', encoding="utf-8")
    before = target.read_bytes()

    monkeypatch.setattr(main_mod.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(target), "")))
    monkeypatch.setattr(flow.FlowGraph, "save",
                        lambda self, p: (_ for _ in ()).throw(
                            OSError("Permission denied")))

    shown = []
    monkeypatch.setattr(main_mod.QMessageBox, "critical",
                        lambda parent, title, text, *a, **k: shown.append(text))
    tab._save()

    assert shown, "a failed save said nothing at all"
    msg = shown[0]
    assert "afternoon.json" in msg, "the message does not name the file"
    assert "Permission denied" in msg, "the actual cause was swallowed"
    assert "has not been changed" in msg, (
        "the message does not say the file already on disk survived, which is "
        "the one thing the user needs to know")
    assert target.read_bytes() == before, "the existing file was damaged"
    assert flow.has_work(tab._graph), "the flow was lost from the canvas"


def test_a_damaged_flow_is_still_listed_and_labelled(library, main_mod):
    """⚠ The worst outcome for a damaged file is that it disappears.

    `save` was not atomic until 4 September 2026, so a half-written flow is a
    file some users already have. Whatever the Library does with one, it must
    not quietly drop it from the list: the user would conclude their flow had
    been deleted, when it is sitting right there and is the only copy.

    `_meta_for` catches and returns "unreadable", so the row stays and says so.
    Pinned because `FlowGraph.load` got stricter the same day — it now raises
    on a payload that is not a flow, where it used to fail further in — and a
    stricter read path is exactly the kind of change that can turn "listed with
    a warning" into "not listed at all".
    """
    dlg, scripts, _root = library
    broken = scripts / "half written.json"
    broken.write_text('{\n  "version": 2,\n  "nodes": [\n', encoding="utf-8")
    dlg._refresh()
    names = [dlg._list.item(i).text() for i in range(dlg._list.count())]
    assert any("half written" in n for n in names), (
        f"the damaged flow vanished from the library: {names}")

    meta = dlg._meta_for(broken)
    assert meta == "unreadable", meta

    # The good ones are unaffected.
    assert any("alpha" in n for n in names)
    assert "step" in dlg._meta_for(scripts / "alpha.json")


def test_a_binding_whose_script_is_gone_stays_visible(window, monkeypatch,
                                                      tmp_path, main_mod):
    """⚠ A launcher binding can outlive its script, and losing it silently is
    the failure to avoid.

    Deleting through the Library clears the binding —
    `test_deleting_a_bound_script_clears_its_launcher_key` covers that. Deleting
    the *file* in Explorer cannot: Macronaut never sees it. This machine has
    three such bindings right now, left from a launcher test whose flows were
    removed by hand.

    So the row has to keep the name. If the combo fell back to "— none —" the
    binding would be gone on the next save, and somebody who moved a file or
    renamed a folder would lose a key they had set up deliberately, with nothing
    said. Marked `(missing)` instead, so it can be re-pointed.
    """
    tab = window._settings_tab
    before = len(tab._sh_rows)
    tab._add_script_hotkey_row("f19", "a script that is not there")
    assert len(tab._sh_rows) == before + 1

    _holder, key, combo = tab._sh_rows[-1]
    assert key.text() == "f19"
    assert "a script that is not there" in combo.currentText(), (
        "the missing script's name was dropped from the row")
    assert "missing" in combo.currentText().lower(), (
        "a binding to a deleted script is not marked as missing, so it reads "
        "as though it still works")
    assert combo.currentText() != "— none —"


def test_shutting_down_releases_a_held_key(window):
    """⚠ The mechanism is tested; the *wiring* was not, and only the wiring
    protects anybody.

    `flow_exec.release_all_held()` has its own tests. What nothing checked is
    that `_shutdown` still calls it — and that call sits inside a
    `try/except: pass`, so if it were removed or started raising, every test
    would stay green and the failure would be silent by construction.

    What it costs when it goes wrong is the worst thing this app can do. Both
    quit paths end in `os._exit(0)`, which runs no `atexit` and gives the
    worker thread no chance to reach its own `finally`. A `W` held down by a
    Hold-down node then outlives the process that pressed it: the user's
    keyboard is stuck until they work out what happened, and a stuck *mouse
    button* is worse still, because every click they try to fix it with becomes
    part of a drag they never started.
    """
    import flow_exec

    class _StuckWorker:
        """Matches what `release_all_held` actually calls. ⚠ It guards every
        call with `try/except: pass`, so a fake with the wrong signature is
        silently skipped and the test fails looking like an app bug — which is
        exactly what the first draft of this did."""

        def __init__(self):
            self._held = {"w": "node-1"}
            self.order = []

        def _release_mouse(self):
            self.order.append("mouse")

        def _release_keys(self, _node=None):
            self._held = {}
            self.order.append("keys")

    stuck = _StuckWorker()
    flow_exec._HOLDERS.add(stuck)
    try:
        window._shutdown()
        assert stuck.order, (
            "_shutdown no longer releases held input — a key held by a running "
            "flow would outlive the app that pressed it")
        assert stuck._held == {}
        assert stuck not in flow_exec._HOLDERS
        # ⚠ Mouse first, and deliberately so: a held button is what makes the
        # desktop unusable rather than merely wrong, because every click the
        # user tries to fix it with joins a drag they never started.
        assert stuck.order == ["mouse", "keys"], stuck.order
    finally:
        flow_exec._HOLDERS.discard(stuck)


def test_shutting_down_releases_both_global_keyboard_hooks(window):
    """⚠ Two low-level Windows hooks, each released inside a `try/except: pass`.

    `MainWindow` installs a global hotkey listener and a separate panic-key
    listener, both on threads of their own, and both hold a real system-wide
    keyboard hook. `_shutdown` stops them — silently, so if either call were
    removed or started raising, nothing would report it.

    The cost is documented in this project's own history: `hide()` alone leaves
    the hooks installed, each test leaked a few more, and at roughly seventy
    live hooks the suite died with a bare `Windows fatal exception: access
    violation` inside pynput's message loop, naming no test. This fixture calls
    `_shutdown()` for exactly that reason.

    ⚠ It matters beyond the suite too. Both quit paths end in `os._exit(0)`
    *because* an orphaned Macronaut holding a global hook is the failure worth
    killing the process over — but `_shutdown` runs first, and it is what makes
    the ordinary case clean rather than relying on the OS to tidy up.
    """
    class _FakeListener:
        def __init__(self):
            self.stopped = 0

        def stop(self):
            self.stopped += 1

    hk, panic = _FakeListener(), _FakeListener()
    window._hk_listener = hk
    window._panic_listener = panic

    window._shutdown()

    assert hk.stopped, "_shutdown no longer stops the global hotkey listener"
    assert panic.stopped, "_shutdown no longer stops the panic-key listener"

    # ⚠ Idempotent means it declines to run twice, not that it releases twice.
    # `_shutdown` returns early on `_shutting_down`, which matters because
    # conftest calls it at teardown and both quit paths call it as well — and
    # asking a stopped pynput listener to stop again is not obviously safe.
    # The first draft of this test asserted a second release and failed; the
    # guard is the correct design and the assertion was wrong.
    window._shutdown()
    assert hk.stopped == 1 and panic.stopped == 1, (
        "_shutdown ran its teardown twice; the _shutting_down guard is gone")


def test_the_panic_hotkey_is_actually_bound(window):
    """The failsafe binds the key it is configured with, and nothing else.

    ⚠ A mutation audit replaced the binding call in
    `_refresh_panic` with `set_hotkeys([])` — the always-on abort key binding
    nothing at all — and the whole suite stayed green (12 September 2026). It
    was the only mutation of seventeen to survive that run.

    The cost of that hole is not a wrong pixel. `settings.panic_hotkey`
    defaults to Esc and its own UI calls it "a panic key that always aborts
    automation"; it is the thing a person reaches for when a script is typing
    into the wrong window. A failsafe that silently binds nothing looks
    identical to one that works, right up to the moment it is needed, and
    there is no other moment at which anyone would find out.

    Three assertions rather than one, because "binds something" is not the
    claim: it has to bind *the configured key*, follow a change to it, and
    unbind when the feature is switched off — an abort key that stays live
    after being disabled is its own surprise.
    """
    class _FakeListener:
        """Records what it was asked to watch. `stop` exists for teardown."""

        def __init__(self):
            self.bound = None

        def set_hotkeys(self, keys):
            self.bound = list(keys)

        def stop(self):
            pass

    fake = _FakeListener()
    window._panic_listener = fake
    s = window._settings.s
    # ⚠ Restored below. The settings file is sandboxed by conftest, but
    # leaving the panic key switched off for whatever runs next is the
    # kind of cross-test coupling that gets blamed on the wrong test.
    was = (getattr(s, "panic_enabled", True), getattr(s, "panic_hotkey", "esc"))
    try:
        s.panic_enabled = True
        s.panic_hotkey = "f9"
        window._refresh_panic()
        assert fake.bound == ["f9"], (
            "the panic hotkey was not bound to the configured key; "
            f"the listener was asked to watch {fake.bound!r}")

        # A change to the key has to reach the listener, not just the settings.
        s.panic_hotkey = "esc"
        window._refresh_panic()
        assert fake.bound == ["esc"], (
            "changing panic_hotkey no longer rebinds the listener; "
            f"it is still watching {fake.bound!r}")

        # And off means off.
        s.panic_enabled = False
        window._refresh_panic()
        assert fake.bound == [], (
            "the panic hotkey stayed bound after being disabled; "
            f"the listener is watching {fake.bound!r}")
    finally:
        s.panic_enabled, s.panic_hotkey = was


def test_a_freshly_built_window_has_the_panic_key_bound(window):
    """The startup call, as opposed to what the function does when called.

    ⚠ Two separate ways for the failsafe to be dead, and the test above only
    covers one. `_refresh_panic` can bind correctly and still never run: it is
    called once during `MainWindow.__init__` and again from the settings tab's
    `failsafe_changed` signal, and deleting either line leaves a window whose
    panic key was never armed.

    So this asks the real listener what it is watching, on a window nobody has
    touched. `HotkeyListener.set_hotkeys` normalises and stores its argument in
    `_hotkeys`, which is the only place the answer exists without pressing a
    key in a GUI test.
    """
    s = window._settings.s
    assert getattr(s, "panic_enabled", True), (
        "the shipped default no longer enables the panic key — if that is "
        "deliberate, this test is the wrong shape, not the settings")
    expected = (getattr(s, "panic_hotkey", "") or "").lower().strip()
    assert expected, "the shipped default no longer defines a panic key"

    assert window._panic_listener._hotkeys == [expected], (
        "a freshly constructed window is not watching the panic key. "
        f"Expected [{expected!r}], the listener has "
        f"{window._panic_listener._hotkeys!r}. Either __init__ stopped "
        "calling _refresh_panic, or _refresh_panic stopped binding.")


# ── Pointer movement ─────────────────────────────────────────────────────────
# The glide is on by default and the switch is the escape hatch, so the control
# has to survive a round trip through settings — a preference that reads back
# wrong is worse than no preference, because the app then behaves in a way the
# window denies.


def test_the_pointer_movement_setting_survives_a_round_trip(window):
    import flow
    tab = window._settings_tab
    s = window._settings.s

    s.smooth_mouse = False
    s.mouse_travel_pps = 1200
    tab._load()
    assert tab._smooth_mouse.isChecked() is False
    assert tab._travel_speed.value() == 1200
    assert tab._travel_speed.isEnabled() is False, (
        "a speed box that cannot affect anything must not look live")

    tab._smooth_mouse.setChecked(True)
    tab._travel_speed.setValue(2400)
    tab.save_to_settings()
    assert s.smooth_mouse is True and s.mouse_travel_pps == 2400
    assert tab._travel_speed.isEnabled() is True

    # A speed the engine would refuse is not reachable from the control.
    assert tab._travel_speed.minimum() >= int(flow.MIN_TRAVEL_PPS)
    assert tab._travel_speed.maximum() <= int(flow.MAX_TRAVEL_PPS)


def test_the_click_editor_round_trips_held_modifiers(main_mod):
    """A pointer step can be performed with modifiers held — ctrl-click to add
    to a selection, shift-click to extend one. The recorder captures them, and
    this dialog rebuilds a step's data from its widgets rather than amending
    it, so without a control for them, opening a recorded Ctrl-click and
    pressing OK silently turned it back into a plain click.
    """
    import flow
    from recorder import SeqStep

    # A recorded ctrl-click reopens with Ctrl ticked, and survives OK untouched.
    dlg = main_mod.StepDialog(
        SeqStep(SeqStep.CLICK, {"x": 10, "y": 20, "button": "left",
                                "clicks": 1, "mods": ["ctrl"]}, 0),
        default_text_cps=20, family="click")
    try:
        assert dlg._click_mods["ctrl"].isChecked()
        assert not dlg._click_mods["shift"].isChecked()
        dlg._on_ok()
        assert flow.pointer_mods(dlg._result_step.data) == ["ctrl"]
    finally:
        dlg.hide()

    # And one can be built by hand, in flow's canonical order whatever order
    # the boxes were ticked in.
    dlg = main_mod.StepDialog(None, default_text_cps=20, family="click")
    try:
        dlg._click_mods["shift"].setChecked(True)
        dlg._click_mods["ctrl"].setChecked(True)
        dlg._on_ok()
        assert dlg._result_step.data["mods"] == ["ctrl", "shift"]
    finally:
        dlg.hide()


def test_a_click_with_no_modifiers_does_not_say_so_in_the_file(main_mod):
    """`flow.pointer_mods` reads the absence of the field as "none", which is
    what every flow saved before modifiers existed says. Writing "mods": []
    into every click from now on says the same thing in a way that reads like
    a setting somebody chose."""
    from recorder import SeqStep

    dlg = main_mod.StepDialog(None, default_text_cps=20, family="click")
    try:
        dlg._on_ok()
        assert "mods" not in dlg._result_step.data
    finally:
        dlg.hide()


def test_a_recorded_modifier_the_editor_can_show_is_not_normalised_away(main_mod):
    """A hand-written step can say "CTRL", or list them the other way round."""
    import flow
    from recorder import SeqStep

    dlg = main_mod.StepDialog(
        SeqStep(SeqStep.DRAG, {"x": 1, "y": 2, "to_x": 3, "to_y": 4,
                               "mods": ["SHIFT", "Ctrl"]}, 0),
        default_text_cps=20, family="click")
    try:
        assert dlg._drag_mods["ctrl"].isChecked()
        assert dlg._drag_mods["shift"].isChecked()
        assert not dlg._drag_mods["alt"].isChecked()
    finally:
        dlg.hide()


# ═════════════════════════════════════════════════════════════════════════════
#  The window guard — "abort if an unexpected window appears"
# ═════════════════════════════════════════════════════════════════════════════
#
# ⚠ This survived a mutation. A mutation audit replaced
# `_check_guard`'s early-return condition with `if True:` — the guard never
# trips, ever — and the whole suite stayed green (8 September 2026).
#
# It is the failsafe somebody switches on *because* a flow has already
# misbehaved once: an unexpected dialog steals focus and the automation carries
# on clicking into it. Deny-list mode aborts when the foreground window matches
# a forbidden title; allow-list mode aborts when it is anything but an approved
# one. Neither half was held by anything.

def _guarded(window, *, running=True, enabled=True, mode="deny",
             titles=("Notepad",), foreground="Notepad"):
    """Arm the guard, fake the foreground window, and record any abort."""
    import sys as _sys
    import types as _types

    s = window._settings.s
    s.guard_enabled = enabled
    s.guard_mode = mode
    s.guard_titles = list(titles)
    window._running = running

    fake = _types.ModuleType("win32gui")
    fake.GetForegroundWindow = lambda: 1
    fake.GetWindowText = lambda _h: foreground
    old = _sys.modules.get("win32gui")
    _sys.modules["win32gui"] = fake

    aborted = []
    real_panic = window._panic
    window._panic = lambda *a, **k: aborted.append(True)
    try:
        window._check_guard()
    finally:
        window._panic = real_panic
        if old is None:
            _sys.modules.pop("win32gui", None)
        else:
            _sys.modules["win32gui"] = old
    return bool(aborted)


def test_the_guard_aborts_on_a_forbidden_window(window):
    """⚠ The mutation's target. A deny-listed title in the foreground while a
    flow is running has to stop the run."""
    assert _guarded(window, mode="deny", titles=("Notepad",),
                    foreground="Untitled - Notepad"), \
        "a forbidden window came to the front and the flow carried on"


def test_the_guard_leaves_an_ordinary_window_alone(window):
    """The other half, and without it a guard that aborts unconditionally
    would pass the test above. Aborting a working flow is its own bug."""
    assert not _guarded(window, mode="deny", titles=("Notepad",),
                        foreground="The game I am automating")


def test_allow_list_mode_aborts_on_anything_unapproved(window):
    assert _guarded(window, mode="allow", titles=("MyGame",),
                    foreground="Some Installer") is True
    assert _guarded(window, mode="allow", titles=("MyGame",),
                    foreground="MyGame - level 3") is False


def test_the_guard_does_nothing_when_it_is_switched_off(window):
    assert not _guarded(window, enabled=False, foreground="Notepad")


def test_the_guard_does_nothing_when_no_flow_is_running(window):
    """It aborts a run; with no run there is nothing to abort, and firing the
    panic path at an idle app would stop whatever the user was doing by hand."""
    assert not _guarded(window, running=False, foreground="Notepad")


def test_an_empty_title_list_never_aborts(window):
    """⚠ Allow-list mode with no titles would otherwise mean "abort on every
    window", which is switching the guard on and having the app refuse to run
    anything at all."""
    assert not _guarded(window, mode="allow", titles=(), foreground="Anything")
    assert not _guarded(window, mode="deny", titles=(), foreground="Anything")


# ═════════════════════════════════════════════════════════════════════════════
#  A recorded hover survives being opened in the editor
# ═════════════════════════════════════════════════════════════════════════════
#
# ⚠ `StepDialog` rebuilds a step's data from its widgets, so a field it does not
# display is destroyed by Open-then-OK. `mods` was fixed that way earlier;
# `SeqStep.MOVE` was worse, because the dialog did not know the KIND.
#
# `_load`'s kind_map had no entry for MOVE, so `kind_map.get(s.kind, 0)`
# fell through to Click — and the Click branch that loads x/y never ran,
# because it is guarded on `s.kind == SeqStep.CLICK`. Opening a recorded
# hover and pressing OK therefore produced a **left click at (0, 0)**: a real
# action, in the corner of the screen, replacing the one that was there.
#
# It went unnoticed because MOVE used to be unreachable — "a step you could add
# by hand, that the engine could run, and that no recording could ever contain"
# — until hover capture landed on 8 September 2026 and every recorded hover
# became one.

def _move_step(main_mod, x=300, y=400, delay=150.0):
    from recorder import SeqStep
    return SeqStep(SeqStep.MOVE, {"x": x, "y": y}, delay)


def test_a_move_step_opens_as_a_move(main_mod, qapp):
    from recorder import SeqStep
    dlg = main_mod.StepDialog(_move_step(main_mod), None, default_text_cps=10.0)
    try:
        dlg._on_ok()
        out = dlg._result_step
    finally:
        dlg.hide()
    assert out is not None, "OK produced no step"
    assert out.kind == SeqStep.MOVE, (
        f"a Move step came back as {out.kind!r} — the editor converted it")


def test_a_move_step_keeps_where_it_was_going(main_mod, qapp):
    """⚠ The damage. (0, 0) is not a neutral default here: it is the top-left
    corner of the screen, and the step still runs."""
    dlg = main_mod.StepDialog(_move_step(main_mod, 300, 400), None,
                              default_text_cps=10.0)
    try:
        dlg._on_ok()
        out = dlg._result_step
    finally:
        dlg.hide()
    assert (out.data.get("x"), out.data.get("y")) == (300, 400), (
        f"moved to {out.data.get('x')},{out.data.get('y')} instead of 300,400")


def test_a_move_step_keeps_its_delay(main_mod, qapp):
    """`move` carries its delay in the step, like `click` and unlike `drag` —
    and that delay is the recorded dwell that made it a hover in the first
    place."""
    dlg = main_mod.StepDialog(_move_step(main_mod, delay=150.0), None,
                              default_text_cps=10.0)
    try:
        dlg._on_ok()
        out = dlg._result_step
    finally:
        dlg.hide()
    assert abs(out.delay_ms - 150.0) < 1.0, f"delay became {out.delay_ms}"


def test_the_click_panels_delay_does_not_ride_along_into_a_move(main_mod, qapp):
    """⚠ The trap Scroll and Drag already document: "Delay before" is a row
    inside the *Click* panel, so a Move that read `self._delay` would inherit
    whatever was typed there before the family toggle was switched."""
    dlg = main_mod.StepDialog(_move_step(main_mod, delay=150.0), None,
                              default_text_cps=10.0)
    try:
        dlg._delay.setValue(9999)          # as if Click had been visited first
        dlg._on_ok()
        out = dlg._result_step
    finally:
        dlg.hide()
    assert out.delay_ms != 9999, "the Click panel's delay leaked into the Move"


def test_move_is_reachable_from_the_click_family(main_mod, qapp):
    """Wiring: the step kind is worthless in the editor if no palette route
    reaches it. Click, Move, Drag and Scroll are the pointer's four verbs and
    they share one palette button."""
    from recorder import SeqStep
    dlg = main_mod.StepDialog(None, None, default_text_cps=10.0, family="click")
    try:
        assert dlg._fam_indices is not None
        idxs = list(dlg._fam_indices)
        assert len(idxs) == 4, f"the Click family offers {len(idxs)} kinds"
        kinds = []
        for i in idxs:
            dlg._type_combo.setCurrentIndex(i)
            dlg._on_ok()
            kinds.append(dlg._result_step.kind)
        assert SeqStep.MOVE in kinds, f"family produces {kinds}"
    finally:
        dlg.hide()


def test_a_recorded_spin_keeps_its_measured_rate_through_the_editor(main_mod, qapp):
    """⚠ The recorder measures a spin's real rate now — 33.06 notches/s for a
    flick, 6.67 for a slow scroll — and the Speed box was an integer spin, so
    the first Open-then-OK rounded it. Trivial for a flick, 10% for a slow one,
    and a loss of a number that was measured rather than typed."""
    from recorder import SeqStep
    step = SeqStep(SeqStep.SCROLL,
                   {"direction": "down", "amount": 7, "speed_nps": 6.7,
                    "at_cursor": True}, 0.0)
    dlg = main_mod.StepDialog(step, None, default_text_cps=10.0)
    try:
        dlg._on_ok()
        out = dlg._result_step
    finally:
        dlg.hide()
    assert abs(float(out.data["speed_nps"]) - 6.7) < 0.11, (
        f"a 6.7 notches/s spin came back as {out.data['speed_nps']}")


def test_as_fast_as_possible_survives_being_reopened(main_mod, qapp):
    """0 is a distinct meaning — "send them as fast as the backend will take
    them" — and it has its own special text in the box. Turning it into 0.0 is
    fine; turning it into anything else would pace a step that was not paced."""
    from recorder import SeqStep
    step = SeqStep(SeqStep.SCROLL,
                   {"direction": "up", "amount": 3, "speed_nps": 0,
                    "at_cursor": True}, 0.0)
    dlg = main_mod.StepDialog(step, None, default_text_cps=10.0)
    try:
        dlg._on_ok()
        out = dlg._result_step
    finally:
        dlg.hide()
    assert float(out.data["speed_nps"]) == 0.0


# ═════════════════════════════════════════════════════════════════════════════
#  The modal that stopped this file dead
# ═════════════════════════════════════════════════════════════════════════════
#
# ⚠ This is the mechanism behind the hang `_no_recovery_prompt` exists to
# prevent, pinned deterministically so it cannot come back unexplained.
#
# It is worth stating what is and is not proven. The faulthandler dump is hard
# evidence that a modal blocked a real run:
#
#     Timeout (0:00:45)!
#       File "main.py", line 5564 in _offer_recovery
#       File "tests/test_gui_offscreen.py", line 4384 in
#           test_the_library_buttons_fit_at_the_dialogs_own_minimum_width
#
# What is NOT proven by simply re-running is that the fixture is load-bearing:
# the hang needs a 20-second timer to have fired *and* written an offerable
# payload before a later MainWindow is built, so a green run proves nothing
# either way. That is exactly why this test creates the condition on purpose
# rather than waiting for it.

def test_a_pending_recovery_puts_a_modal_in_front_of_the_suite(main_mod, qapp,
                                                               monkeypatch):
    """⚠ With a payload on disk, building a MainWindow asks a question — and
    offscreen there is nobody to answer it.

    `QMessageBox.question` is stubbed here, so what would be an unrecoverable
    hang is recorded as a call instead. If this ever stops being asked, the
    autouse fixture above has become unnecessary and can go; while it IS asked,
    removing that fixture puts the flake back.
    """
    import flow
    import recovery
    monkeypatch.setattr(recovery, "read", _REAL_RECOVERY_READ)

    g = flow.FlowGraph()
    g.add_node(flow.N_START, {"name": flow.START_NAME}, x=-280, y=-20)
    g.add_node(flow.N_ACTION,
               {"step": {"kind": "text", "data": {"text": "unsaved"}}},
               x=0, y=0)
    assert recovery.write(g, ""), "could not stage a recovery payload"
    assert recovery.offerable(recovery.read()) is not None

    asked = []
    monkeypatch.setattr(
        main_mod.QMessageBox, "question",
        staticmethod(lambda *a, **kw: (asked.append(1),
                                       main_mod.QMessageBox.No)[1]))
    w = main_mod.MainWindow()
    try:
        qapp.processEvents()          # let QTimer.singleShot(0, ...) fire
        assert asked, (
            "no modal was raised for a pending recovery — either the offer is "
            "no longer wired into startup, or it stopped being modal")
    finally:
        try:
            w._shutdown()
        except Exception:
            pass
        w.hide()
        recovery.clear()


def test_the_fixture_really_silences_the_offer(main_mod, qapp, monkeypatch):
    """The other half: with the fixture in force, the same staged payload
    raises nothing. Without this, the test above could pass while the fixture
    quietly did nothing."""
    import flow
    import recovery
    g = flow.FlowGraph()
    g.add_node(flow.N_START, {"name": flow.START_NAME}, x=-280, y=-20)
    g.add_node(flow.N_ACTION,
               {"step": {"kind": "text", "data": {"text": "unsaved"}}},
               x=0, y=0)
    recovery.write(g, "")             # written, but `read` is stubbed to None

    asked = []
    monkeypatch.setattr(
        main_mod.QMessageBox, "question",
        staticmethod(lambda *a, **kw: (asked.append(1),
                                       main_mod.QMessageBox.No)[1]))
    w = main_mod.MainWindow()
    try:
        qapp.processEvents()
        assert not asked, "the recovery prompt got through the fixture"
    finally:
        try:
            w._shutdown()
        except Exception:
            pass
        w.hide()
        recovery.clear()
# ── The privacy page's promises, which nothing enforced ──────────────────────
#
# ⚠ Found 12 September 2026 by mutation, not by reading. Three edits to shipping
# code, each breaking a sentence the privacy page states in as many words, and
# the whole suite — 1145 tests — stayed green for all three:
#
#   * delete the `return` for a user who chose "off"  -> reports upload anyway
#   * make `_discard_all()` a no-op on a decline      -> declined reports persist
#   * delete the `auto_check_updates` guard           -> the off switch does nothing
#
# The near miss is worth recording, because it looks like coverage.
# `test_a_declined_prompt_is_never_asked_again` already drives `_run` with
# "off" — and asserts only that the dialog is not shown again. With the guard
# removed no dialog appears *and* the report is uploaded, so it passes. It was
# testing the asking, not the sending. A test named for half the promise is how
# the other half goes unguarded.
#
# These assert on the thing the page promises: what leaves the machine.


def test_someone_who_said_no_has_nothing_uploaded(qapp, crash_home, monkeypatch):
    """`crash_reports = "off"` must not send. The page: "only if you say yes"."""
    import crash_ui, crashsend
    _queue_one(crash_home)
    monkeypatch.setattr(crashsend, "enabled", lambda: True)
    monkeypatch.setattr(crash_ui, "_start_upload",
                        lambda w: pytest.fail(
                            "a crash report was uploaded from someone who "
                            "chose 'off' — the privacy page promises this "
                            "cannot happen"))
    crash_ui._run(None, _Settings("off"))


def test_an_unanswered_prompt_uploads_nothing_on_its_own(qapp, crash_home,
                                                         monkeypatch):
    """An unrecognised stored value must fail closed, not fall through to send.

    ⚠ The branch is `elif choice != "on": return`, so every value that is
    neither "ask" nor "on" lands here — including a settings file hand-edited
    to something meaningless, and including the empty string. Failing *open*
    there would upload from a machine whose owner never said yes.
    """
    import crash_ui, crashsend
    _queue_one(crash_home)
    monkeypatch.setattr(crashsend, "enabled", lambda: True)
    monkeypatch.setattr(crash_ui, "_start_upload",
                        lambda w: pytest.fail(
                            "an unrecognised crash_reports value was treated "
                            "as consent"))
    crash_ui._run(None, _Settings("something-nobody-wrote"))


def test_turning_the_update_check_off_stops_it_contacting_anything(monkeypatch):
    """The page: "You can turn the check off in Settings, and Macronaut then
    never contacts the network on its own at all."

    ⚠ Calls `_maybe_check_updates` unbound against a stand-in rather than
    building a MainWindow. The guard is two lines at the top of that method and
    a real window drags in the whole application to reach them; what matters is
    that the setting is consulted before anything is started, and that is
    visible from here. `_upd_ctl.start` is the single door to the network on
    this path — `updater` reaches urlopen only through it — so a stub that
    fails if called is the whole assertion.
    """
    import main

    class _Ctl:
        def start(self, download=False):
            pytest.fail("the update check ran with auto_check_updates off — "
                        "the privacy page promises it does not")

    class _Win:
        UPDATE_CHECK_INTERVAL = main.MainWindow.UPDATE_CHECK_INTERVAL
        _upd_ctl = _Ctl()

        def __init__(self):
            self._settings = _Settings()
            self._settings.s.auto_check_updates = False
            self._settings.s.last_update_check = 0.0

    main.MainWindow._maybe_check_updates(_Win())


def test_leaving_the_update_check_on_still_lets_it_run(monkeypatch):
    """The twin of the test above, so it cannot pass by never running at all.

    ⚠ Without this, a `_maybe_check_updates` that returned unconditionally
    would satisfy the previous test perfectly. Same shape as the guard on the
    guard in conftest.py.
    """
    import main

    started = []

    class _Ctl:
        def start(self, download=False):
            started.append(download)

    class _Win:
        UPDATE_CHECK_INTERVAL = main.MainWindow.UPDATE_CHECK_INTERVAL
        _upd_ctl = _Ctl()

        def __init__(self):
            self._settings = _Settings()
            self._settings.s.auto_check_updates = True
            self._settings.s.last_update_check = 0.0

    main.MainWindow._maybe_check_updates(_Win())
    assert started, ("the check did not run with the setting on, so the test "
                     "above proves nothing")
def test_the_consent_dialog_offers_to_show_what_would_be_sent(qapp, crash_home,
                                                              monkeypatch):
    """The privacy page promises a button that reads the exact report.

    ⚠ Verbatim: "there is a button to read the exact report before you decide".
    The button existed and nothing checked that it did anything. Unwiring its
    `clicked` connection left all 1156 tests passing — a consent dialog that
    cannot show what is being consented to, which is the one thing that makes
    the question answerable.

    Asserts the wiring, not the label. A button whose text is right and whose
    connection is gone looks perfect in a screenshot.
    """
    import crash_ui
    _queue_one(crash_home)

    shown = []
    monkeypatch.setattr(crash_ui, "show_reports", lambda parent=None: shown.append(parent))

    dlg = crash_ui.ConsentDialog(pending=1)
    try:
        assert dlg._show_btn.isEnabled(), (
            "the report button is disabled while a report is waiting")
        dlg._show_btn.click()
        assert shown, (
            "clicking 'See what would be sent' did nothing — the privacy page "
            "promises it reads the exact report")
    finally:
        dlg.hide()


def test_the_button_is_dead_when_there_is_nothing_to_show(qapp, crash_home):
    """The twin, so the test above cannot pass by the button always working.

    Offering to show a report that does not exist is its own small lie, and an
    always-enabled button would satisfy the previous test perfectly.
    """
    import crash_ui
    dlg = crash_ui.ConsentDialog(pending=0)
    try:
        assert not dlg._show_btn.isEnabled()
    finally:
        dlg.hide()


def test_the_viewer_shows_the_report_that_is_actually_queued(qapp, crash_home):
    """"The exact report" has to mean this one, not a specimen.

    ⚠ Reads the widget's text rather than trusting that a viewer was opened.
    The promise is about the contents: a dialog that appears and shows a
    template, or an empty box, would pass any check that only counts windows.
    """
    import crash_ui
    _queue_one(crash_home)

    from PySide6.QtWidgets import QPlainTextEdit, QTextEdit
    viewer = crash_ui.ReportViewer()
    try:
        boxes = viewer.findChildren(QPlainTextEdit) + viewer.findChildren(QTextEdit)
        assert boxes, "the report viewer has no text area"
        shown = "\n".join(b.toPlainText() for b in boxes)
        assert shown.strip() and shown.strip() != "(unreadable)", (
            "the viewer opened on an empty report")
        # Both come from the queued fixture, so this is that report and not a
        # sample: the version it crashed on and the step it was running.
        assert "2.0.8" in shown, "the viewer is not showing the queued report"
        assert "n1" in shown, (
            "the viewer does not show which step was running, which is most of "
            "what a person is being asked to consent to")
    finally:
        viewer.hide()


def test_the_about_card_offers_the_star_and_the_report(window):
    """The one-time question reaches people once; these are for afterwards.

    Somebody who dismissed it and changed their mind a month later should not
    have to go and find the repository.
    """
    import star_ui
    import version
    tab = window._settings_tab
    opened = star_ui.QDesktopServices.opened
    assert tab._about_star.text() == star_ui.STAR_LABEL
    assert tab._about_feedback.text() == star_ui.FEEDBACK_LABEL
    opened.clear()
    tab._about_star.click()
    tab._about_feedback.click()
    assert opened == [version.REPO_URL, version.ISSUES_URL]
    opened.clear()
