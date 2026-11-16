"""Qt glue for updater.py — background checks and the "update available" dialog.

Kept out of main.py deliberately: main.py is the 190 KB truncation-risk file, so
new UI that can live on its own does.

Nothing here blocks the GUI thread. `UpdateController` owns a QThread running the
network work and re-emits the results as signals; the caller connects and forgets.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QMessageBox,
                             QProgressBar, QPushButton, QTextBrowser, QVBoxLayout)

import updater
import version
from updater import UpdateError, UpdateInfo


class _Worker(QObject):
    """Runs check (and optionally download) off the GUI thread."""

    result = Signal(object)     # UpdateInfo, or None when up to date
    error = Signal(str)
    progress = Signal(int, int)  # bytes done, bytes total (0 = unknown)
    staged = Signal(str)        # verified file on disk, ready to apply
    done = Signal()

    def __init__(self, download: bool = False):
        super().__init__()
        self._download = download
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    @Slot()
    def run(self) -> None:
        try:
            info = updater.check()
        except UpdateError as e:
            self.error.emit(str(e))
            self.done.emit()
            return
        except Exception as e:  # pragma: no cover - defensive: never kill the thread
            self.error.emit(f"Unexpected error during update check ({e}).")
            self.done.emit()
            return

        self.result.emit(info)
        if info is not None and self._download:
            try:
                path = updater.download(
                    info,
                    progress=lambda d, t: self.progress.emit(d, t),
                    cancelled=lambda: self._cancel,
                )
                self.staged.emit(str(path))
            except UpdateError as e:
                self.error.emit(str(e))
            except Exception as e:  # pragma: no cover - defensive
                self.error.emit(f"Unexpected error during download ({e}).")
        self.done.emit()


class UpdateController(QObject):
    """Starts update checks and keeps the worker thread alive while it runs.

    Signals mirror the worker's. `busy` guards against overlapping runs (a user
    hammering "Check now" while the startup check is still in flight).
    """

    result = Signal(object)
    error = Signal(str)
    progress = Signal(int, int)
    staged = Signal(str)
    finished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread: Optional[QThread] = None
        self._worker: Optional[_Worker] = None

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def start(self, download: bool = False) -> bool:
        """Kick off a check. -> False if one is already running."""
        if self.busy:
            return False
        self._thread = QThread(self)
        self._worker = _Worker(download)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.result.connect(self.result)
        self._worker.error.connect(self.error)
        self._worker.progress.connect(self.progress)
        self._worker.staged.connect(self.staged)
        self._worker.done.connect(self._cleanup)
        self._thread.start()
        return True

    def cancel(self) -> None:
        if self._worker is not None:
            self._worker.cancel()

    @Slot()
    def _cleanup(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            # Skipped when this runs on the worker thread itself: waiting there
            # returns instantly and only warns, so it would read as a completed
            # wait. deleteLater() is safe either way — Qt defers the delete to
            # the object's own event loop rather than doing it here.
            if QThread.currentThread() is not self._thread:
                self._thread.wait(5000)
            self._thread.deleteLater()
        if self._worker is not None:
            self._worker.deleteLater()
        self._thread = None
        self._worker = None
        self.finished.emit()


class UpdateDialog(QDialog):
    """"Version X is available" — release notes plus the three real choices.

    Returns one of INSTALL / LATER / SKIP via `choice` after exec().
    """

    INSTALL, LATER, SKIP = "install", "later", "skip"

    def __init__(self, info: UpdateInfo, staged_path: Optional[str] = None, parent=None):
        super().__init__(parent)
        self.info = info
        self.choice = self.LATER
        self._staged = staged_path
        self.setWindowTitle("Macronaut update")
        self.setMinimumWidth(460)

        v = QVBoxLayout(self)
        v.setContentsMargins(20, 20, 20, 16)
        v.setSpacing(12)

        head = QLabel(f"Macronaut {info.version} is available")
        head.setObjectName("h1")
        v.addWidget(head)
        v.addWidget(QLabel(f"You're on {version.__version__}."))

        if info.notes:
            notes = QTextBrowser()
            notes.setPlainText(info.notes)
            notes.setMaximumHeight(200)
            v.addWidget(notes)

        self._status = QLabel("")
        self._status.setObjectName("hint")
        self._status.setWordWrap(True)
        v.addWidget(self._status)

        self._bar = QProgressBar()
        self._bar.setVisible(False)
        v.addWidget(self._bar)

        row = QHBoxLayout()
        row.addStretch(1)
        self._skip = QPushButton("Skip this version")
        self._later = QPushButton("Later")
        self._install = QPushButton("Install and restart")
        self._install.setDefault(True)
        for b in (self._skip, self._later, self._install):
            row.addWidget(b)
        v.addLayout(row)

        self._skip.clicked.connect(lambda: self._pick(self.SKIP))
        self._later.clicked.connect(lambda: self._pick(self.LATER))
        self._install.clicked.connect(lambda: self._pick(self.INSTALL))

        self.set_staged(staged_path)

    def set_staged(self, path: Optional[str]) -> None:
        """Enable installing only once a verified download exists on disk."""
        self._staged = path
        ready = bool(path)
        self._install.setEnabled(ready or not updater.is_frozen())
        if ready:
            self._status.setText("Downloaded and verified — ready to install.")
        elif not updater.is_frozen():
            self._status.setText(
                "Running from source, so there is nothing to replace — update "
                "with a git pull instead.")
            self._install.setEnabled(False)
        else:
            self._status.setText("Downloading…")
            self._bar.setVisible(True)

    def on_progress(self, done: int, total: int) -> None:
        self._bar.setVisible(True)
        if total:
            self._bar.setMaximum(total)
            self._bar.setValue(done)
        else:
            self._bar.setRange(0, 0)  # indeterminate

    def on_error(self, msg: str) -> None:
        self._bar.setVisible(False)
        self._status.setText(msg)

    def _pick(self, which: str) -> None:
        self.choice = which
        self.accept()


def apply_and_quit(staged_path: str, parent=None) -> bool:
    """Hand off to the staged .exe and close the app. -> False if it couldn't.

    The caller must actually exit when this returns True — the new process is
    already waiting for this PID to disappear.
    """
    from pathlib import Path
    try:
        updater.apply(Path(staged_path))
    except UpdateError as e:
        QMessageBox.warning(
            parent, "Update failed",
            f"{e}\n\nYou can download the new version manually from:\n"
            f"{version.RELEASES_PAGE_URL}")
        return False
    return True
