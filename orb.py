"""⚰ DEAD CODE, and deliberately kept that way. Do not wire this up.

Nothing imports this module, no version of Macronaut has ever shipped it
enabled, and `tests/test_gui_offscreen.py::test_the_orb_is_still_an_orphan`
fails if `main.py`, `compact.py`, `flow_canvas.py`, `flow_dialogs.py` or
`settings.py` starts importing it. That test is the point: importing it would
put a half-built feature back into the frozen build.

It was never finished. Its enable flag was switched off during the 2.0 fixes
round and every call site was removed when the compact face was, on 13 August
2026. The compact face came back on 28 August; this did not, and that was a
decision rather than an oversight.

It stays in the tree because the mount this project is developed on refuses
deletes, and because the published source mirrors the working tree. It is not
in the .exe — `macronaut.spec` collects imports, and nothing imports this.

⚠ The design document this used to cite is not in the public repository. It is
one of the early planning files kept private; there is no missing link to chase.

What it was going to be: a small, draggable, always-on-top circle showing the
app's live state at a glance, expanding back to the window on click, with three
states mirroring the tray convention — idle (indigo, rocket + CPS), running
(green ring, live CPS), recording (coral, REC).
"""
from PySide6.QtCore import Qt, Signal, QPoint, QRectF
from PySide6.QtGui import QPainter, QColor, QPen, QFont
from PySide6.QtWidgets import QWidget


# Palette lifted from the 2.0 interface mock (the cosmic theme).
_PALETTE = {
    "idle":      dict(bg="#1d1b3f", ring="#3C3489", accent="#7F77DD", fg="#EEEDFE"),
    "running":   dict(bg="#0f2a24", ring="#1D9E75", accent="#9FE1CB", fg="#EAF7F1"),
    "recording": dict(bg="#2c130b", ring="#D85A30", accent="#F0997B", fg="#F5C4B3"),
}

_DIAM = 66


class FloatingOrb(QWidget):
    """Frameless always-on-top orb. Click to expand; drag to reposition."""

    expand_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                            | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(_DIAM, _DIAM)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Macronaut — click to open, drag to move")

        self._state = "idle"
        self._cps = 0.0
        self._idle_count = 10        # the "10" badge from the mock (saved-script count)
        self._press_pos = None
        self._moved = False

    # ── public API ────────────────────────────────────────────────────
    def set_state(self, state: str, cps: float = 0.0):
        if state not in _PALETTE:
            state = "idle"
        self._state = state
        self._cps = cps
        self.update()

    def set_idle_badge(self, n: int):
        self._idle_count = max(0, int(n))
        if self._state == "idle":
            self.update()

    def show_at(self, x: int, y: int):
        self.move(int(x), int(y))
        self.show()
        self.raise_()

    # ── drag vs click ─────────────────────────────────────────────────
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press_pos = e.globalPos() - self.frameGeometry().topLeft()
            self._moved = False
            e.accept()

    def mouseMoveEvent(self, e):
        if self._press_pos is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPos() - self._press_pos)
            self._moved = True
            e.accept()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            if not self._moved:
                self.expand_requested.emit()
            self._press_pos = None
            e.accept()

    # ── paint ─────────────────────────────────────────────────────────
    def paintEvent(self, _e):
        c = _PALETTE[self._state]
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        rect = QRectF(3, 3, _DIAM - 6, _DIAM - 6)
        ring_w = 2 if self._state == "idle" else 3
        p.setBrush(QColor(c["bg"]))
        p.setPen(QPen(QColor(c["ring"]), ring_w))
        p.drawEllipse(rect)

        if self._state == "running":
            self._text(p, "RUNNING", 8, QColor(c["accent"]), -13, bold=False)
            self._text(p, f"{self._cps:.1f}", 16, QColor(c["fg"]), 5, bold=True)
            self._badge(p, "■", QColor(c["ring"]))
        elif self._state == "recording":
            self._text(p, "●", 14, QColor(c["accent"]), -8, bold=True)
            self._text(p, "REC", 10, QColor(c["fg"]), 9, bold=True)
            self._badge(p, "■", QColor(c["ring"]))
        else:  # idle
            self._text(p, "\U0001F680", 17, QColor(c["accent"]), -8)   # rocket
            self._text(p, str(self._idle_count), 12, QColor(c["fg"]), 11, bold=True)
            self._badge(p, "▶", QColor(c["accent"]))               # play ▶

        p.end()

    def _text(self, p, s, pt, color, dy, bold=True):
        f = QFont("Segoe UI", pt)
        f.setBold(bold)
        p.setFont(f)
        p.setPen(color)
        r = self.rect().adjusted(0, dy, 0, dy)
        p.drawText(r, Qt.AlignCenter, s)

    def _badge(self, p, glyph, color):
        d = 20
        x = _DIAM - d - 1
        y = _DIAM - d - 1
        p.setBrush(color)
        p.setPen(Qt.NoPen)
        p.drawEllipse(x, y, d, d)
        f = QFont("Segoe UI", 8)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor("#0d0c20"))
        p.drawText(QRectF(x, y, d, d), Qt.AlignCenter, glyph)
