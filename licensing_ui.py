"""The two licence dialogs. Kept out of main.py, like `updater_ui` and `crash_ui`.

Someone meets these at exactly two moments: they pressed Play on a flow the free
tier will not run, or they have bought a key and want to enter it. Both are
short, and both are written on the assumption that the person in front of them
is *trying to give us money* and should not have to work at it.

Three things the upgrade dialog deliberately does not do:

- **It does not appear on a timer, on launch, or on a schedule.** It appears
  when a specific flow cannot run, says which of its steps are the reason, and
  otherwise never exists. A nag screen is the thing that makes people uninstall
  free software.
- **It does not hide the price behind a click.** The number is on the button.
- **It does not lose the user's work.** The flow they built is still there,
  still editable, still saveable. Cancelling this dialog returns them to it.
"""
from __future__ import annotations

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPlainTextEdit,
                               QPushButton, QVBoxLayout, QWidget)

import entitlements
import licensing

# Re-exported from `entitlements`, which imports no Qt. Kept as names here
# because the dialogs and the Settings card read them.
PRICE = entitlements.PRICE
TERMS = entitlements.TERMS


def _hint(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("hint")
    lbl.setWordWrap(True)
    return lbl


def _pane(layout) -> QWidget:
    """A layout-only container that does not paint over the dialog.

    ⚠ The application stylesheet sets `background: $bg` on **every** QWidget,
    subclasses included, so a plain QWidget holding a row of buttons paints a
    near-black rectangle across the card behind it. `fieldRow` is the opt-out
    the rest of the app uses; `tests/test_gui_offscreen.py::opaque_containers`
    walks every dialog looking for one that forgot.
    """
    w = QWidget()
    w.setObjectName("fieldRow")
    w.setLayout(layout)
    return w


def _wrap(label: QLabel, width: int) -> None:
    """Give a wrapping label the height it actually needs at `width`.

    The same fix, for the same reason, as `crash_ui._wrap` — deliberately
    duplicated rather than imported, because importing it would drag
    `crashreport` and `crashsend` in behind it for six lines of layout.

    A QVBoxLayout asks a label for `sizeHint()`, which a wrapping label answers
    as though it were one long line — so it is handed too little height and the
    tail of the paragraph is silently not drawn. And `ensurePolished()` first
    is the part that is easy to miss: an unpolished label measures in Qt's
    default 9 pt, not the stylesheet's font, which is the same class of bug as
    the palette buttons that were measured before the theme was applied.
    """
    label.setWordWrap(True)
    label.setFixedWidth(width)
    label.ensurePolished()
    label.setMinimumHeight(label.heightForWidth(width))


class ActivateDialog(QDialog):
    """Paste a key, press Activate.

    A `QPlainTextEdit` rather than a line edit, because the key is 170-odd
    characters and arrives wrapped across several lines out of an e-mail. A
    single-line field would show the last thirty of them and hide whether the
    paste worked — the same trap the Type-text step already paid for.
    """

    TEXT_W = 460

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Activate Macronaut Pro")
        self.setModal(True)
        v = QVBoxLayout(self)
        v.setContentsMargins(20, 18, 20, 16)
        v.setSpacing(12)

        head = QLabel("Enter your licence key")
        head.setStyleSheet("font-size: 15pt; font-weight: 600;")
        v.addWidget(head)

        body = _hint("Paste the key from your purchase e-mail. It starts with "
                     "MN1- and is one long line; spacing and line breaks don't "
                     "matter.")
        _wrap(body, self.TEXT_W)
        v.addWidget(body)

        # ⚠ objectName "textBody" is REQUIRED, not decorative: without it the
        # blanket background rule paints this field the dialog's own colour and
        # it becomes an invisible box the user cannot tell they have typed into.
        self._field = QPlainTextEdit()
        self._field.setObjectName("textBody")
        self._field.setFixedHeight(96)
        self._field.setPlaceholderText("MN1-…")
        v.addWidget(self._field)

        self._status = _hint("")
        _wrap(self._status, self.TEXT_W)
        v.addWidget(self._status)

        # ⚠ A second label, not more text in the red one. What went wrong and
        # what will be done about it are different kinds of sentence, and
        # painting the offer of help in error-red makes a dialog that reads
        # entirely as "you did something wrong" — to somebody who has already
        # paid and is deciding, right now, whether they have been had.
        # Hidden until it is needed so it reserves no space.
        self._help = _hint("")
        _wrap(self._help, self.TEXT_W)
        self._help.setVisible(False)
        v.addWidget(self._help)

        paste = QPushButton("Paste from clipboard")
        paste.setObjectName("btnGhost")
        paste.clicked.connect(self._paste)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("btnGhost")
        cancel.clicked.connect(self.reject)
        self._ok = QPushButton("Activate")
        self._ok.setObjectName("btnPrimary")
        self._ok.clicked.connect(self._activate)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(paste)
        row.addStretch(1)
        row.addWidget(cancel)
        row.addWidget(self._ok)
        v.addWidget(_pane(row))

        # The overwhelmingly likely next action is a paste, so save them the
        # click when the clipboard already holds something key-shaped.
        self._prefill()

    def _prefill(self) -> None:
        try:
            text = QGuiApplication.clipboard().text() or ""
        except Exception:
            return
        if licensing.KEY_PREFIX in text.upper() and len(text) < 4000:
            self._field.setPlainText(text.strip())

    def _paste(self) -> None:
        try:
            self._field.setPlainText((QGuiApplication.clipboard().text() or "").strip())
        except Exception:
            pass

    def _activate(self) -> None:
        ok, message = licensing.activate(self._field.toPlainText())
        if ok:
            self.accept()
        else:
            # Stay open. The commonest cause is a half-selected paste, and
            # closing the dialog would make them find it again.
            #
            # ⚠ And give them a way out on the second reading. Until this line
            # the dialog's only advice was to check their own copy-and-paste.
            self._status.setText(message)
            self._status.setStyleSheet("color: #ff6b6b;")
            self._help.setText(
                "If it still will not take, e-mail "
                f"{entitlements.CONTACT_EMAIL} and I will sort it out.")
            self._help.setVisible(True)
            # ⚠ Both labels were measured while empty, so each carries a floor
            # of one line — and the refusal wraps to two. Re-measure or the
            # tail is silently not drawn, which is `crash_ui._wrap`'s bug in
            # the one place it would cost a customer.
            for lbl in (self._status, self._help):
                lbl.setMinimumHeight(lbl.heightForWidth(self.TEXT_W))
            self.adjustSize()


class UpgradeDialog(QDialog):
    """Shown when a flow needs Pro and this copy does not have it."""

    TEXT_W = 470

    def __init__(self, parent=None, reason: str = "", features=None):
        super().__init__(parent)
        self.setWindowTitle("Macronaut Pro")
        self.setModal(True)
        self.activated = False

        v = QVBoxLayout(self)
        v.setContentsMargins(20, 18, 20, 16)
        v.setSpacing(12)

        head = QLabel("This script needs Macronaut Pro")
        head.setStyleSheet("font-size: 15pt; font-weight: 600;")
        v.addWidget(head)

        if reason:
            why = QLabel(reason)
            _wrap(why, self.TEXT_W)
            v.addWidget(why)

        what = _hint(
            "Pro adds the steps that let a script watch the screen and decide "
            "what to do — Wait for image, Wait for text, Wait for pixel, "
            # ⚠ "variables" was in this list and is deliberately not any more.
            # `flow.py` implements `set_var` and the `var` condition, but no
            # part of the UI can create either: the palette is nine buttons and
            # Set Var is not one, and ConditionWidget.TYPES is image / text /
            # pixel / always. So this dialog — the screen where somebody is
            # asked for money — was listing a feature they could not use.
            # If Set Var ever comes back to the palette, put it back here.
            "If / Else, Loop and Go to — and removes the "
            f"{entitlements.FREE_MAX_STEPS}-step limit.\n\n"
            "Everything you have built stays exactly as it is. Clicking, "
            "typing, dragging and scrolling are free and always will be.")
        _wrap(what, self.TEXT_W)
        v.addWidget(what)

        terms = _hint(TERMS)
        _wrap(terms, self.TEXT_W)
        v.addWidget(terms)

        later = QPushButton("Not now")
        later.setObjectName("btnGhost")
        later.clicked.connect(self.reject)
        have = QPushButton("I have a key")
        have.setObjectName("btnGhost")
        have.clicked.connect(self._enter_key)
        buy = QPushButton(f"Get Pro — {PRICE}")
        buy.setObjectName("btnPrimary")
        buy.clicked.connect(self._buy)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(later)
        row.addStretch(1)
        row.addWidget(have)
        row.addWidget(buy)
        v.addWidget(_pane(row))

    def _buy(self) -> None:
        # Deliberately does NOT close: they come back to this window with a key
        # in the clipboard, and "I have a key" is right there.
        try:
            QDesktopServices.openUrl(QUrl(entitlements.BUY_URL))
        except Exception:
            pass

    def _enter_key(self) -> None:
        dlg = ActivateDialog(self)
        if dlg.exec() == QDialog.Accepted:
            self.activated = True
            self.accept()


def prompt_for_upgrade(parent, reason: str, features=None) -> bool:
    """Show the upgrade dialog. True if they activated a key just now — in
    which case the caller should carry on and do whatever it was blocked on,
    because making someone press Play twice after paying is a poor thank-you."""
    dlg = UpgradeDialog(parent, reason=reason, features=features)
    dlg.exec()
    return dlg.activated


def prompt_for_key(parent) -> bool:
    """The Settings route in. True if a key was activated."""
    return ActivateDialog(parent).exec() == QDialog.Accepted
