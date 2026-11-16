"""Shared pytest setup.

The offscreen platform plugin has to be chosen before the first QApplication is
built, and a QApplication has to exist before any QWidget. Both happen here so
individual test modules can just ask for the `qapp` fixture.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── The suite must not edit the developer's own installation ─────────────────
#
# ⚠ Same class of bug as the injection one below, and it had the same shape:
# a real side effect on the machine running the tests, believed to be covered
# and not. Tests build a real `SettingsManager` and a real `MainWindow`, both
# of which read *and write* `~/.macronaut/settings.json`. Running the suite
# therefore rewrote the developer's live settings — and it did it on every
# file save, because the PostToolUse hook runs pytest.
#
# It was found the hard way: `starters.seed_once` records a flag, the suite
# ran, and the developer's own copy silently became one that had "already been
# offered the starter flows" and never would be again.
#
# Three references have to be redirected and only the first is obvious:
#
#   1. `data_dir` / `scripts_dir`, the functions.
#   2. `SETTINGS_DIR` / `SETTINGS_FILE`, which are module *constants* resolved
#      at import time — so patching the functions above does nothing for them,
#      which is exactly the trap that made several sandboxed-looking scripts
#      read the real file anyway.
#   3. `main.py` does `from settings import data_dir, scripts_dir`, so it holds
#      its own names and a later patch of the `settings` module would never
#      reach them.
#
# Which is why this runs at conftest *import* time and not in a fixture, and
# why no separate patch of `main` is needed: pytest imports this file before
# any test module, so by the time anything imports `main` these two names are
# already the sandboxed ones and `main` copies those. Patching in a fixture
# would be both too late and, for the same reason, permanently wrong.
import tempfile
from pathlib import Path

import settings as _settings

_SANDBOX = Path(tempfile.mkdtemp(prefix="macronaut-tests-"))
_SCRIPTS = _SANDBOX / "scripts"
_SCRIPTS.mkdir(parents=True, exist_ok=True)

_settings.SETTINGS_DIR = _SANDBOX
_settings.SETTINGS_FILE = _SANDBOX / "settings.json"
_settings.data_dir = lambda: _SANDBOX
_settings.scripts_dir = lambda: _SCRIPTS


@pytest.fixture(autouse=True, scope="session")
def _prove_the_real_installation_was_not_touched():
    """A guard on the guard: fail the run if the real settings file moved.

    ⚠ The redirection above is three assignments, and every one of them is the
    kind that keeps looking correct after it has stopped working — a renamed
    constant, a new module taking its own copy, an import that happens earlier
    than expected. The failure is silent and lands on the developer's own
    installation, so it is worth one stat() at each end to know.
    """
    real_dir = Path.home() / ".macronaut"

    def _snapshot():
        """Every file in the real data directory, by size and mtime.

        ⚠ The whole directory, not just settings.json. This watched one file
        until 12 September 2026, and the evidence that one is not enough was
        sitting in the directory it was not watching: `update.log` on this
        machine holds 332 lines written by `updater._log` during test runs on
        31 July and 28 August, when `data_dir()` was not yet patched early
        enough to catch it. `settings.json` was untouched throughout, so the
        guard said nothing, correctly and uselessly.
        """
        if not real_dir.is_dir():
            return None
        out = {}
        for p in real_dir.rglob("*"):
            try:
                if p.is_file():
                    st = p.stat()
                    out[str(p.relative_to(real_dir))] = (st.st_size, st.st_mtime_ns)
            except OSError:
                # A file that vanished mid-walk is not evidence of anything.
                continue
        return out

    before = _snapshot()
    yield
    after = _snapshot()
    if before is None or after is None:
        return

    touched = sorted(k for k in set(before) | set(after)
                     if before.get(k) != after.get(k))
    # ⚠ Two explanations, and the message has to offer both or it will be
    # disbelieved the one time it is right. Widening this from settings.json to
    # the whole directory also widened what a *running Macronaut* can trip: the
    # app rewrites settings.json and appends to update.log on every run, so a
    # developer with it open during a suite run would see this fire with the
    # tests blamed for it. That is the failure mode the fixture below the
    # sandbox exists to avoid, applied to this fixture.
    assert not touched, (
        f"files under {real_dir} changed during the run:\n  "
        + "\n  ".join(touched[:20])
        + "\n\nEither the sandbox at the top of conftest.py has stopped "
          "catching a route to the real installation — which is what this "
          "guard is for — or Macronaut itself was running while the suite "
          "was. Check that before going looking for the leak.")


@pytest.fixture(autouse=True, scope="session")
def _no_crash_consent_timer_may_mature_mid_suite():
    """Push the crash-upload delay past the length of any run. Autouse.

    ⚠ This is a bug that hung the whole suite, not tidiness. Every MainWindow
    calls `crash_ui.schedule()`, which arms a real `QTimer.singleShot` 8 seconds
    out. The suite builds many windows and runs for ~40 seconds, so those shots
    mature and then fire inside whichever *unrelated* test next pumps events. If
    a crash report happens to be queued at that moment -- and the consent tests
    queue one -- `_run` opens a **modal** `ConsentDialog`, whose `exec()` blocks
    forever because no one is there to answer it.

    It presents as `test_the_consent_text_is_not_clipped` hanging, which is
    misleading: that test is a bystander that merely happened to call
    `processEvents()` at the wrong moment. Whether it hangs at all depends on
    how the 8 seconds line up against test order and machine speed, so it comes
    and goes and looks like flakiness.

    Delaying rather than stubbing `schedule` keeps the real function under test:
    it still arms, still checks the DSN, and the tests that call `_run` directly
    are untouched. Only the maturing is moved out of reach.
    """
    import crash_ui
    was, crash_ui.SEND_DELAY_MS = crash_ui.SEND_DELAY_MS, 24 * 60 * 60 * 1000
    yield
    crash_ui.SEND_DELAY_MS = was


class _NoBrowser:
    """Stands in for QDesktopServices inside `star_ui` for the whole session."""
    opened: list = []

    @classmethod
    def openUrl(cls, url):
        cls.opened.append(url.toString() if hasattr(url, "toString") else str(url))
        return True


@pytest.fixture(autouse=True, scope="session")
def _the_star_question_never_reaches_the_real_desktop():
    """Keep `star_ui` from opening a browser or maturing mid-suite. Autouse.

    Same class as the crash-consent timer above. Every MainWindow counts a
    launch in the sandboxed settings and arms the question once it is due — and
    the suite builds far more than three windows, so without this the shot
    matures inside an unrelated test. It is not modal, so nothing would hang,
    but its buttons call `QDesktopServices.openUrl`, and a test that clicks its
    way around a window would open GitHub in the developer's real browser.
    """
    import star_ui
    was_delay, star_ui.ASK_DELAY_MS = star_ui.ASK_DELAY_MS, 24 * 60 * 60 * 1000
    was_qds, star_ui.QDesktopServices = star_ui.QDesktopServices, _NoBrowser
    yield
    star_ui.ASK_DELAY_MS = was_delay
    star_ui.QDesktopServices = was_qds


@pytest.fixture(autouse=True, scope="session")
def _no_live_update_check():
    """Nothing in this suite may reach the network. Autouse.

    ⚠ Measured 9 September 2026: one run of `tests/test_gui_offscreen.py`
    attempted **20 live HTTPS requests to GitHub**. Not a hypothetical — the
    PostToolUse hook runs this suite on every file save, so it was twenty
    requests per edit, from a machine that never asked to check for updates.

    `MainWindow.__init__` arms `QTimer.singleShot(4000, self._maybe_check_updates)`.
    The suite builds many windows and runs far longer than four seconds, so
    those shots mature and fire inside whichever unrelated test next pumps
    events — the same shape as the crash-consent timer above, and as
    `_offer_recovery` in test_gui_offscreen.py. Here the payload is a network
    call rather than a modal, so it does not hang; it just quietly goes out.

    ⚠ Blocked at `urllib.request.urlopen` — the actual door — and not at any
    of the three tempting places above it. That took two wrong tries worth
    recording, because each looked like "the boundary" until the tests said
    otherwise:

      * `_maybe_check_updates` is one caller. A guard there stops covering the
        moment a second caller appears.
      * `fetch_manifest` has its own web-host-to-API fallback, and two tests
        exercise it; stubbing it made them assert against this guard's message
        instead of the one they were written for.
      * `updater._get` has the retry/backoff logic, and three more tests
        exercise *that* by stubbing urlopen underneath it.

    Every one of those layers is somebody's subject. `urlopen` is the only
    thing that is nobody's subject and everybody's dependency. Tests that want
    a specific network behaviour patch it themselves and their stub wins for
    the duration, which is exactly right.
    """
    import urllib.request
    real = urllib.request.urlopen

    def _blocked(*a, **kw):
        raise OSError("network disabled during tests (conftest)")

    urllib.request.urlopen = _blocked
    yield
    urllib.request.urlopen = real


@pytest.fixture(autouse=True, scope="session")
def _never_inject_into_the_real_desktop():
    """Nothing in this suite may reach a real keyboard or mouse. Autouse.

    ⚠ This is not belt-and-braces, it is a bug that shipped keystrokes into the
    developer's foreground window. The typing tests stubbed
    `sendinput_backend.user32.SendInput` and believed that covered them — but
    `FlowWorker` builds its backend from **settings**, and on a machine with
    Interception selected the strokes go through the kernel driver, which never
    touches SendInput. A whole test string was typed into a live window, in
    scancode order (`hallo` arriving as `hqllo` on AZERTY, which is what named
    the cause). The PostToolUse hook runs this suite on every edit, so it was
    once per file save.

    Two locks, because either alone has a hole: the backend setting is forced to
    pynput so nothing constructs a driver, and every injection primitive is
    stubbed so a test that asks for one by name still cannot send. A test that
    genuinely wants to inspect what a backend *would* send should monkeypatch
    these itself and capture, which is what the pinning tests do.
    """
    import input_backends as ib
    import sendinput_backend as sb

    _orig_make_kb, _orig_make_mouse = ib.make_keyboard, ib.make_mouse

    def _safe_make_keyboard(backend=None):
        # None means "read settings", which is the path that reached the driver.
        return _orig_make_kb(ib.BACKEND_PYNPUT if backend is None else backend)

    def _safe_make_mouse(backend=None):
        return _orig_make_mouse(ib.BACKEND_PYNPUT if backend is None else backend)

    ib.make_keyboard, ib.make_mouse = _safe_make_keyboard, _safe_make_mouse
    sb.user32.SendInput = lambda n, _arr, _size: n
    sb._send_scan = lambda _sc, keyup=False, extended=False: None
    try:
        import interception_backend as ic
        ic._icept = None       # the driver handle every send path goes through
    except Exception:
        pass
    yield
    ib.make_keyboard, ib.make_mouse = _orig_make_kb, _orig_make_mouse


@pytest.fixture(scope="session")
def qapp():
    """One QApplication for the whole session — Qt forbids a second."""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app
    # Deliberately not calling quit(): tearing the application down mid-session
    # takes any still-referenced widget with it and produces crashes that look
    # like test failures.


@pytest.fixture
def qapp_or_skip():
    """A QApplication for tests that need a real QThread event loop."""
    try:
        from PySide6.QtWidgets import QApplication
    except Exception:                                   # pragma: no cover
        pytest.skip("PySide6 unavailable")
    app = QApplication.instance() or QApplication([])
    return app
