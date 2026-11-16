"""The one thing Macronaut asks of the person using it: a star, or a word.

Kept out of main.py for the same reason `updater_ui` and `crash_ui` are — it is
a self-contained piece of window behaviour with its own rules, and those rules
are the whole design:

- **Once.** `settings.star_asked` is written *before* the box is shown, so a
  crash, a kill or simply ignoring it can never bring the question back. A
  second ask is a nag, and a nag is the reason people uninstall free tools.
- **Only to somebody who uses it.** Not on the first launches, and not before
  at least one run has been recorded. Asking a person who has not yet found out
  whether the program works is asking for a favour on credit.
- **At startup, never after a run.** The moment a run stops, the person is
  looking at whatever they were automating — often a game in the foreground —
  and a window appearing then takes the keyboard away from it. At launch they
  are looking at Macronaut.
- **Not modal.** It blocks nothing, spins no nested event loop (which is how
  `crash_ui` once stacked dialogs until the stack ran out), and can be left
  sitting there while the app is used.
- **Never on top of another question.** Recovery, an update and crash consent
  are about the user's own work and machine; this is about ours. If one of
  those is up, this waits for a later launch — without spending its one ask.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QMessageBox

import version

# Launches before the question may be asked. Counted from the release that
# introduced the counter, so a long-time user is asked on their third launch
# after updating rather than on the first — an update that opens with a request
# reads as the reason for the update.
ASK_FROM_LAUNCH = 3

# After the window is up and the recovery offer (zero delay) has had its turn,
# before the update check four seconds in.
ASK_DELAY_MS = 1500

TITLE = "Enjoying Macronaut?"
TEXT = ("Macronaut is free and open source.\n\n"
        "If it is useful to you, a star on GitHub is what helps other people "
        "find it. And if something is broken or missing, say so — that is how "
        "it gets fixed.\n\n"
        "This is the only time Macronaut will ask.")
STAR_LABEL = "★  Star on GitHub"
FEEDBACK_LABEL = "Report a problem"
LATER_LABEL = "No thanks"


def note_launch(settings) -> None:
    """Count this launch. Best-effort: a counter must not stop the app opening."""
    try:
        settings.s.launch_count = int(settings.s.launch_count) + 1
        settings.save()
    except Exception:
        pass


def due(settings, stats) -> bool:
    """Has this person used Macronaut enough to be asked, and never been?"""
    try:
        if settings.s.star_asked:
            return False
        if int(settings.s.launch_count) < ASK_FROM_LAUNCH:
            return False
        return len(stats.sessions) >= 1
    except Exception:
        return False


def schedule(window, settings, stats) -> None:
    """Called once from MainWindow. Counts the launch; asks if it is time."""
    note_launch(settings)
    if due(settings, stats):
        QTimer.singleShot(ASK_DELAY_MS, lambda: ask(window, settings, stats))


def ask(window, settings, stats):
    """Show the box if nothing else is in the way. Returns it, or None.

    ⚠ `due` is asked again here: a suite or a second window can have answered
    in the meantime, and the promise is one ask per installation, not per timer.
    """
    try:
        if not due(settings, stats):
            return None
        # Hidden to the tray, or another question already on screen: not now,
        # and the ask is not spent.
        if not window.isVisible() or window.isMinimized():
            return None
        if QApplication.activeModalWidget() is not None:
            return None

        settings.s.star_asked = True
        settings.save()

        box = QMessageBox(window)
        box.setWindowTitle(TITLE)
        box.setText(TEXT)
        box.setIcon(QMessageBox.NoIcon)
        star = box.addButton(STAR_LABEL, QMessageBox.AcceptRole)
        feedback = box.addButton(FEEDBACK_LABEL, QMessageBox.ActionRole)
        box.addButton(LATER_LABEL, QMessageBox.RejectRole)
        box.setDefaultButton(star)
        box.setWindowModality(Qt.NonModal)
        box.setAttribute(Qt.WA_DeleteOnClose, True)

        def _clicked(button):
            if button is star:
                open_star_page()
            elif button is feedback:
                open_feedback_page()

        box.buttonClicked.connect(_clicked)
        box.show()
        return box
    except Exception:
        # Asking for a favour is never allowed to be what breaks the app.
        return None


def open_star_page() -> None:
    QDesktopServices.openUrl(QUrl(version.REPO_URL))


def open_feedback_page() -> None:
    QDesktopServices.openUrl(QUrl(version.ISSUES_URL))
