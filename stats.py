"""Statistics tracking: rolling CPS/KPS and per-session history."""
import csv
import json
import time
from pathlib import Path
from datetime import datetime
from typing import List, Optional
from dataclasses import dataclass
from collections import deque

from settings import data_dir

HISTORY_DIR  = data_dir()
HISTORY_FILE = HISTORY_DIR / "sessions.json"
_MAX_HISTORY = 500   # keep only the most recent N sessions on disk


@dataclass
class Session:
    start_time: datetime
    end_time: Optional[datetime] = None
    total_clicks: int = 0
    total_keystrokes: int = 0

    @property
    def duration_seconds(self) -> float:
        end = self.end_time or datetime.now()
        return max(0.001, (end - self.start_time).total_seconds())

    def to_dict(self) -> dict:
        return {
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "total_clicks": self.total_clicks,
            "total_keystrokes": self.total_keystrokes,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Session":
        return cls(
            start_time=datetime.fromisoformat(d["start_time"]),
            end_time=datetime.fromisoformat(d["end_time"]) if d.get("end_time") else None,
            total_clicks=int(d.get("total_clicks", 0)),
            total_keystrokes=int(d.get("total_keystrokes", 0)),
        )

    @property
    def cps(self) -> float:
        return self.total_clicks / self.duration_seconds

    @property
    def kps(self) -> float:
        return self.total_keystrokes / self.duration_seconds

    def to_csv_row(self) -> list:
        return [
            self.start_time.strftime("%Y-%m-%d %H:%M:%S"),
            self.end_time.strftime("%Y-%m-%d %H:%M:%S") if self.end_time else "—",
            f"{self.duration_seconds:.1f}",
            str(self.total_clicks),
            str(self.total_keystrokes),
            f"{self.cps:.2f}",
            f"{self.kps:.2f}",
        ]


class StatsManager:
    _WINDOW = 5.0  # seconds for rolling averages

    def __init__(self):
        self.sessions: List[Session] = []
        self._current: Optional[Session] = None
        self._t0: Optional[float] = None
        self._clicks: deque = deque()
        self._keys: deque = deque()
        self._load_history()

    # ── Persistence ───────────────────────────────────────────────────
    def _load_history(self):
        try:
            if HISTORY_FILE.exists():
                data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
                self.sessions = [Session.from_dict(d) for d in data]
        except Exception:
            self.sessions = []   # corrupt file → start fresh

    def save_history(self):
        try:
            HISTORY_DIR.mkdir(parents=True, exist_ok=True)
            recent = self.sessions[-_MAX_HISTORY:]
            HISTORY_FILE.write_text(
                json.dumps([s.to_dict() for s in recent], indent=2),
                encoding="utf-8")
        except Exception:
            pass

    def clear_history(self):
        self.sessions.clear()
        self.save_history()

    # ── Session lifecycle ─────────────────────────────────────────────
    def start_session(self):
        self._current = Session(start_time=datetime.now())
        self._t0 = time.monotonic()
        self._clicks.clear()
        self._keys.clear()

    def end_session(self):
        if self._current:
            self._current.end_time = datetime.now()
            self.sessions.append(self._current)
            self._current = None
            self._t0 = None
            self.save_history()

    # ── Event recording ───────────────────────────────────────────────
    def record_click(self):
        now = time.monotonic()
        self._clicks.append(now)
        self._prune(self._clicks, now)
        if self._current:
            self._current.total_clicks += 1

    def record_keystroke(self):
        now = time.monotonic()
        self._keys.append(now)
        self._prune(self._keys, now)
        if self._current:
            self._current.total_keystrokes += 1

    def _prune(self, dq: deque, now: float):
        cutoff = now - self._WINDOW
        while dq and dq[0] < cutoff:
            dq.popleft()

    # ── Live metrics ──────────────────────────────────────────────────
    def current_cps(self) -> float:
        now = time.monotonic()
        self._prune(self._clicks, now)
        n = len(self._clicks)
        return n / self._WINDOW if n >= 2 else 0.0

    def current_kps(self) -> float:
        now = time.monotonic()
        self._prune(self._keys, now)
        n = len(self._keys)
        return n / self._WINDOW if n >= 2 else 0.0

    def elapsed(self) -> float:
        return 0.0 if self._t0 is None else time.monotonic() - self._t0

    @property
    def total_clicks(self) -> int:
        return self._current.total_clicks if self._current else 0

    @property
    def total_keystrokes(self) -> int:
        return self._current.total_keystrokes if self._current else 0

    # ── Export ────────────────────────────────────────────────────────
    def export_csv(self, path: str):
        headers = ["Start", "End", "Duration(s)", "Clicks", "Keystrokes", "Avg CPS", "Avg KPS"]
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            for s in self.sessions:
                writer.writerow(s.to_csv_row())
