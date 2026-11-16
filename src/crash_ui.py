"""Consent, upload and inspection for crash reports. Kept out of main.py.

Three things live here, in the order the user meets them:

  1. A one-time question. Reporting is off until it is answered, and the answer
     is remembered as on/off — the "ask" tri-state exists so a declined prompt
     is never asked again.
  2. A background upload of whatever the last session left behind. On a thread
     because it is network I/O, and nothing about a crash report justifies
     making the window appear later.
  3. A viewer, so "what exactly are you sending?" has an answer the user can
     read rather than a promise they have to take on trust.

The consent default is Yes and both buttons are equally prominent. That is a
deliberate middle position between opt-out (more data, but a program that
phones home before asking is a bad look for an unsigned binary that already
trips SmartScreen) and a buried opt-in nobody finds.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                               QPushButton, QPlainTextEdit, QDialogButtonBox)

import crashreport
import crashsend

# How long after the window appears before anything is uploaded. Long enough
# that it never competes with the first paint; the update check uses 4 s and
# this deliberately follows it rather than racing it for the network.
SEND_DELAY_MS = 8000


class _SendThread(QThread):
    """Upload pending reports off the GUI thread."""

    done = Signal(int, int)          # (sent, remaining)

    def run(self):
        try:
            sent, remaining = crashsend.send_pending()
        except Exception:
            # Never let a reporting failure surface as a crash. The reports stay
            # on disk and the next launch tries again.
            sent, remaining = 0, 0
        self.done.emit(sent, remaining)


def _wrap(label: QLabel, width: int) -> None:
    """Make a wrapping QLabel occupy the height it actually needs.

    Word wrap plus a fixed width is not enough on its own: a QVBoxLayout asks
    for sizeHint(), which for a wrapping label is computed as though it were one
    long line, so the label is handed too little height and the tail of the
    paragraph is simply not drawn. Both of the obvious fixes were tried here and
    both were wrong — replacing the size policy drops hasHeightForWidth and
    clipped the last line, and setFixedWidth alone lost an entire paragraph.

    Asking the label how tall it needs to be at this exact width, and pinning
    that, is deterministic and needs no cooperation from the layout.

    `ensurePolished()` first, and this is the part that is easy to miss: an
    unpolished label answers in the default "Sans Serif" 9pt, not the font the
    stylesheet is about to give it. Measured here that was 194 px against a real
    285 px — the pinned height silently cut 91 px, most of a paragraph, off the
    bottom. `showEvent` re-pins for the same reason, in case a theme change has
    moved the answer again.
    """
    label.setWordWrap(True)
    label.setFixedWidth(width)
    label.ensurePolished()
    label.setMinimumHeight(label.heightForWidth(width))


class ConsentDialog(QDialog):
    """The one-time question.

    Plain language on purpose: "diagnostics" and "telemetry" are words that make
    people say no to things they would have said yes to, and the honest version
    is short enough to fit.
    """

    # A word-wrapped QLabel reports its UNWRAPPED width as its size hint, so a
    # dialog full of prose lays itself out one enormous line wide (this one came
    # out at 1225 px). Pinning the text width is what makes heightForWidth kick
    # in and the hint honest — the same trap StepDialog._refit() works around.
    TEXT_W = 460

    def __init__(self, parent=None, pending: int = 0):
        super().__init__(parent)
        self.setWindowTitle("Send crash reports?")
        self.setModal(True)
        v = QVBoxLayout(self)
        v.setContentsMargins(20, 18, 20, 16)
        v.setSpacing(12)

        head = QLabel("Help fix crashes?")
        head.setStyleSheet("font-size: 15pt; font-weight: 600;")
        v.addWidget(head)

        body = QLabel(
            "If Macronaut closes unexpectedly, it can send me a report about "
            "what went wrong so I can fix it.\n\n"
            "A report contains the error, the version you're running, your "
            "Windows version, and what the app was doing at the time.\n\n"
            "It never contains your scripts, what you type, what's on your "
            "screen, or your name — Windows account names are removed from "
            "file paths before anything is saved.")
        _wrap(body, self.TEXT_W)
        v.addWidget(body)

        self._note = None
        if pending:
            self._note = QLabel(
                "There %s %d report%s waiting from a previous crash."
                % ("is" if pending == 1 else "are", pending,
                   "" if pending == 1 else "s"))
            self._note.setObjectName("hint")
            _wrap(self._note, self.TEXT_W)
            v.addWidget(self._note)

        # Tertiary action, on the left of the button row. It was its own
        # full-width row first, where the theme's filled button style made it
        # read as the primary action — louder than the actual answer.
        self._show_btn = QPushButton("See what would be sent")
        self._show_btn.setCursor(Qt.PointingHandCursor)
        self._show_btn.clicked.connect(lambda: show_reports(self))
        self._show_btn.setEnabled(bool(pending))

        row = QHBoxLayout()
        row.addWidget(self._show_btn)
        row.addStretch(1)
        no = QPushButton("No thanks")
        yes = QPushButton("Yes, send reports")
        yes.setDefault(True)
        no.clicked.connect(self.reject)
        yes.clicked.connect(self.accept)
        # Equal footing: no dark pattern, but Yes is the default action.
        for b in (no, yes):
            b.setMinimumWidth(150)
            row.addWidget(b)
        v.addLayout(row)

        foot = QLabel("You can change this any time in Settings.")
        foot.setObjectName("hint")
        v.addWidget(foot)

        self._wrapped = [w for w in (body, self._note) if w is not None]

    def showEvent(self, e):
        # Re-measure once the real font is definitely in force. See _wrap().
        for lbl in getattr(self, "_wrapped", ()):
            lbl.setMinimumHeight(lbl.heightForWidth(self.TEXT_W))
        self.adjustSize()
        super().showEvent(e)


class ReportViewer(QDialog):
    """The exact JSON that would be uploaded. No summary, no paraphrase."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Crash reports")
        self.resize(720, 520)
        v = QVBoxLayout(self)
        paths = crashreport.pending()
        if not paths:
            v.addWidget(QLabel("No crash reports are waiting. Nothing to send."))
        else:
            head = QLabel("%d report%s waiting. This is exactly what is "
                          "uploaded — nothing is added on the way out."
                          % (len(paths), "" if len(paths) == 1 else "s"))
            # Wrapping, so the sentence cannot dictate the window's width.
            head.setWordWrap(True)
            v.addWidget(head)
            box = QPlainTextEdit()
            box.setReadOnly(True)
            chunks = []
            for p in paths:
                rep = crashreport.load(p)
                if not rep:
                    continue
                chunks.append("── %s ──\n%s" % (
                    crashreport.summarize(rep),
                    _pretty(crashsend.to_event(rep))))
            box.setPlainText("\n\n".join(chunks) or "(unreadable)")
            v.addWidget(box, 1)

        bb = QDialogButtonBox(QDialogButtonBox.Close)
        if paths:
            drop = bb.addButton("Delete these", QDialogButtonBox.DestructiveRole)
            drop.clicked.connect(lambda: (_discard_all(), self.accept()))
        # Close carries RejectRole and "Delete these" DestructiveRole, so this
        # box never emits `accepted` — connecting it would be wiring to nothing.
        bb.rejected.connect(self.reject)
        v.addWidget(bb)


def _pretty(event: dict) -> str:
    import json
    return json.dumps(event, indent=2, default=str)


def _discard_all() -> None:
    for p in crashreport.pending():
        crashreport.discard(p)


def show_reports(parent=None) -> None:
    ReportViewer(parent).exec()


# ── orchestration ─────────────────────────────────────────────────────────────

def schedule(window, settings) -> None:
    """Ask if we have never asked, then upload if allowed.

    Called once from MainWindow. Everything is delayed and best-effort: a user
    who never crashes should never see or wait for any of this.
    """
    if not crashsend.enabled():
        return                       # no DSN compiled in — nothing to send to
    QTimer.singleShot(SEND_DELAY_MS, lambda: _run(window, settings))


# True only while the consent dialog is up. `exec()` spins a nested event loop,
# so any other timer armed by `schedule()` fires *inside* it -- and without this
# guard that second `_run` opens a second modal dialog on top of the first, whose
# own nested loop lets a third through, and so on until the stack runs out.
#
# One window arms one timer, so a real user never sees this. A test suite that
# builds many windows does: each arms its own 8-second shot, they all mature, and
# the first one to find a queued report while another test is pumping events
# takes the whole run down. That is how it was found.
_asking = False


def _run(window, settings) -> None:
    global _asking
    try:
        choice = str(getattr(settings.s, "crash_reports", "ask") or "ask")
        pending = len(crashreport.pending())
        if choice == "ask":
            # Don't interrupt someone who has never had a problem: the question
            # is only worth asking when there is something to answer it about.
            if not pending:
                return
            if _asking:
                return               # already asking; do not stack dialogs
            dlg = ConsentDialog(window, pending=pending)
            _asking = True
            try:
                allow = dlg.exec() == QDialog.Accepted
            finally:
                _asking = False
            settings.set("crash_reports", "on" if allow else "off")
            if not allow:
                _discard_all()       # declined means deleted, not merely unsent
                return
        elif choice != "on":
            return
        if not crashreport.pending():
            return
        _start_upload(window)
    except Exception:
        pass


def _start_upload(window) -> None:
    th = _SendThread(window)
    # Held on the window so the thread is not garbage-collected mid-run — the
    # 2.0.8 lesson about dropping the last reference to a live QThread.
    window._crash_send_thread = th
    # `done` fires on the upload thread. It is connected to a BOUND METHOD of
    # the window on purpose: Qt reads the receiver's thread affinity from the
    # bound instance and queues the call onto the GUI thread. A bare lambda has
    # no receiver, so it would run the handler — and its widget work — on the
    # thread that is about to finish. That is the same trap `_retire()` in
    # main.py works around.
    handler = getattr(window, "_on_crash_reports_sent", None)
    if handler is not None:
        th.done.connect(handler)
    th.finished.connect(th.deleteLater)
    th.start()
