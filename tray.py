"""System-tray icon with start/stop/quit menu."""
from PySide6.QtWidgets import QSystemTrayIcon, QMenu
from PySide6.QtGui     import (QIcon, QPixmap, QColor, QPainter, QPen, QBrush,
                               QAction)  # QAction moved QtWidgets -> QtGui in Qt6
from PySide6.QtCore    import Qt, Signal, QObject


def _make_icon(color: str = "#6366f1", size: int = 64) -> QIcon:
    """Draw a simple mouse-body icon programmatically."""
    px = QPixmap(size, size)
    px.fill(Qt.transparent)
    p  = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing)
    s  = size

    body_color  = QColor(color)
    outline     = QColor("#0e1016")
    wheel_color = QColor("#0e1016")

    # Mouse body
    p.setBrush(QBrush(body_color))
    p.setPen(QPen(outline, s * 0.04))
    p.drawRoundedRect(int(s*0.18), int(s*0.06), int(s*0.64), int(s*0.82), s*0.22, s*0.22)

    # Centre divider line
    p.setPen(QPen(outline, s * 0.04))
    p.drawLine(int(s*0.5), int(s*0.06), int(s*0.5), int(s*0.44))

    # Scroll wheel
    p.setBrush(QBrush(wheel_color))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(int(s*0.38), int(s*0.14), int(s*0.24), int(s*0.24), s*0.08, s*0.08)

    p.end()
    return QIcon(px)


class SystemTray(QObject):
    start_requested = Signal()
    stop_requested  = Signal()
    show_requested  = Signal()
    quit_requested  = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tray = QSystemTrayIcon(parent)
        self._tray.setIcon(_make_icon())
        self._tray.setToolTip("Macronaut — Idle")

        menu = QMenu()

        self._status_act = QAction("● Idle", menu)
        self._status_act.setEnabled(False)
        menu.addAction(self._status_act)
        menu.addSeparator()

        self._hotkey_label = ""

        self._start_act = QAction("▶  Start", menu)
        self._start_act.triggered.connect(self.start_requested)
        menu.addAction(self._start_act)

        self._stop_act = QAction("■  Stop", menu)
        self._stop_act.triggered.connect(self.stop_requested)
        self._stop_act.setEnabled(False)
        menu.addAction(self._stop_act)

        menu.addSeparator()

        show_act = QAction("Show Window", menu)
        show_act.triggered.connect(self.show_requested)
        menu.addAction(show_act)

        quit_act = QAction("Quit", menu)
        quit_act.triggered.connect(self.quit_requested)
        menu.addAction(quit_act)

        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_activated)
        self._tray.show()

    def _on_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self.show_requested.emit()

    def set_hotkey_label(self, disp: str):
        """Show the configured hotkey next to the Start/Stop menu actions."""
        self._hotkey_label = disp or ""
        s = f"   ({self._hotkey_label})" if self._hotkey_label else ""
        self._start_act.setText(f"▶  Start{s}")
        self._stop_act.setText(f"■  Stop{s}")

    def set_state(self, running: bool):
        if running:
            self._status_act.setText("● Running")
            self._start_act.setEnabled(False)
            self._stop_act.setEnabled(True)
            self._tray.setIcon(_make_icon("#22c55e"))   # green
            self._tray.setToolTip("Macronaut — Running")
        else:
            self._status_act.setText("● Idle")
            self._start_act.setEnabled(True)
            self._stop_act.setEnabled(False)
            self._tray.setIcon(_make_icon("#6366f1"))   # indigo
            self._tray.setToolTip("Macronaut — Idle")

    def is_visible(self) -> bool:
        return self._tray.isVisible()

    def hide(self):
        self._tray.hide()

    def notify(self, title: str, msg: str):
        self._tray.showMessage(title, msg, QSystemTrayIcon.Information, 2500)
