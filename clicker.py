"""⚰ DEAD CODE. Nothing imports this module and nothing runs it.

Read this before spending any time here. It was Macronaut's original clicking
engine; the 2.0 rewrite replaced it with the `autoclick` node, and the live
implementation is `flow_exec.FlowWorker._do_autoclick`, which was ported from
`ClickWorker.run()` below and is where a fix actually belongs.

⚠ That pointer read `flow_exec.FlowInterpreter._run_autoclick` until 4 September
2026, which is wrong in the way that is hardest to notice: `FlowInterpreter` is
real, but it lives in `flow.py`, and `_run_autoclick` does not exist anywhere.
A name you half-recognise reads as verified. It cost a session — a real 15.625 ms
clock bug was found in the code *below*, fixed, benchmarked and reported as a
shipped-app improvement, because grep lands you in the middle of a file and this
banner was never read. If you are editing anything here, stop.

It is still in the tree for two reasons and neither is that it works. The mount
this project is developed on refuses deletes, and the published source is a
mirror of the working tree rather than a curated subset — so removing it is a
thing to do deliberately rather than a thing that happens. It is **not** bundled
into the .exe: `macronaut.spec` collects imports, and nothing imports this.

⚠ It is also out of date in ways that matter. It predates the selectable input
backends (`input_backends.make_mouse()`), so it talks to pynput directly and
cannot reach a game that ignores message-queue input — the single most common
reason someone needs this program at all.

Historical mouse click automation engine — ran in a QThread worker.
"""
import time
import random
from typing import Optional, Tuple
from PySide6.QtCore import QObject, Signal, Slot, QThread
from pynput.mouse import Button, Controller as MouseCtrl

_BTN = {"left": Button.left, "right": Button.right, "middle": Button.middle}

try:
    import pyautogui
    pyautogui.FAILSAFE = False
    _PYAUTOGUI = True
except Exception:
    _PYAUTOGUI = False

try:
    import win32gui as _w32
    _WIN32 = True
except Exception:
    _WIN32 = False

try:
    import matcher
    _HAS_MATCHER = True
except Exception:
    _HAS_MATCHER = False


class ClickWorker(QObject):
    """Executes the click loop in a background thread."""

    clicked          = Signal(int)   # cumulative click count
    status_changed   = Signal(str)   # "running" | "paused" | "idle"
    finished         = Signal()
    error_occurred   = Signal(str)

    # ── Config (set before start) ─────────────────────────────────────
    button: Button         = Button.left
    click_type: str        = "single"   # single | double | hold
    hold_duration_ms: int  = 100
    use_fixed: bool        = False
    fixed_x: int           = 0
    fixed_y: int           = 0
    interval_ms: int       = 1000
    randomize: bool        = False
    random_range_ms: int   = 100
    click_limit: int       = 0          # 0 = infinite
    stop_after_secs: float = 0.0        # 0 = no limit
    human_mode: bool       = False
    jitter_px: int         = 5
    use_region: bool       = False
    region: Tuple          = (0, 0, 1920, 1080)  # x, y, w, h
    pause_on_focus: bool   = False
    focus_window: str      = ""
    wait_for_image: bool   = False
    image_path: str        = ""
    image_confidence: float = 0.8

    def __init__(self):
        super().__init__()
        self._mouse = MouseCtrl()
        self._running = False
        self._paused = False

    # ── Control ───────────────────────────────────────────────────────
    def request_stop(self):
        self._running = False

    def request_pause(self):
        self._paused = True
        self.status_changed.emit("paused")

    def request_resume(self):
        self._paused = False
        self.status_changed.emit("running")

    # ── Helpers ───────────────────────────────────────────────────────
    def _interval(self) -> float:
        base = self.interval_ms / 1000.0
        if self.randomize or self.human_mode:
            rng = self.random_range_ms / 1000.0
            if self.human_mode:
                rng = max(rng, base * 0.15)
            base += random.uniform(-rng, rng)
        return max(0.005, base)

    def _jitter(self, x: int, y: int) -> Tuple[int, int]:
        if self.human_mode and self.jitter_px > 0:
            x += random.randint(-self.jitter_px, self.jitter_px)
            y += random.randint(-self.jitter_px, self.jitter_px)
        return x, y

    def _window_in_focus(self) -> bool:
        if not self.pause_on_focus or not self.focus_window:
            return True
        if not _WIN32:
            return True
        try:
            title = _w32.GetWindowText(_w32.GetForegroundWindow())
            return self.focus_window.lower() in title.lower()
        except Exception:
            return True

    def _image_present(self) -> bool:
        if not self.wait_for_image or not self.image_path:
            return True
        # If matching is unavailable we can't check — don't block the automation.
        if not _HAS_MATCHER or not matcher.ENABLED:
            return True
        try:
            # Multi-scale + grayscale-aware match across the full virtual desktop.
            return matcher.present(self.image_path, self.image_confidence)
        except Exception:
            return False

    def _do_click(self):
        if self.use_fixed:
            x, y = self._jitter(self.fixed_x, self.fixed_y)
        else:
            cx, cy = self._mouse.position
            x, y = self._jitter(int(cx), int(cy))

        if self.use_region:
            rx, ry, rw, rh = self.region
            x = max(rx, min(rx + rw - 1, x))
            y = max(ry, min(ry + rh - 1, y))

        if self.use_fixed:
            self._mouse.position = (x, y)

        btn = self.button
        if self.click_type == "single":
            self._mouse.click(btn)
        elif self.click_type == "double":
            self._mouse.click(btn, 2)
        else:  # hold
            dur = self.hold_duration_ms / 1000.0
            if self.human_mode:
                dur *= random.uniform(0.7, 1.3)
            self._mouse.press(btn)
            self._sleep(dur)
            self._mouse.release(btn)

    def _sleep(self, secs: float):
        """Interruptible sleep — Stop cuts it short within ~10 ms.

        ⚠ **perf_counter, not monotonic.** On Windows `time.monotonic()` is
        GetTickCount64 and its resolution is 15.625 ms, so every deadline here
        quantised up to the next tick. `time.sleep` was never the problem — it
        is accurate to well under a millisecond on this machine. Measured on
        4 September 2026, before and after, as the gap between consecutive
        clicks with the mouse stubbed out:

            interval   promised    was        now
              50 ms     20 /s     16.0 /s    20.0 /s
              10 ms    100 /s     91.7 /s    98.6 /s
            Max speed   ~200 /s   61.5 /s   181.7 /s

        Every "was" figure is an exact multiple of 15.625 ms, which is what
        gave it away: 5 ms and 10 ms waits both took 16.00 ms, a 50 ms wait
        took 62.50, a 200 ms wait took 203.00.

        ⚠ `flow_exec.FlowWorker.sleep` had been fixed for this long before,
        **and its comment even predicts the number above** — "it caps click
        rate at ~64 CPS however low the interval goes". The fix had never been
        carried across to the Basic clicker, which is the app's most-used path
        and the one the published interval table on the website describes.
        **That is what the change above is** — the gap is closed, and the
        website's "10 ms → 100 / second" row is honest again at a measured
        98.6 / s. Nothing is outstanding here. If you find another loop that
        paces something in milliseconds, check its clock.
        """
        deadline = time.perf_counter() + max(0.0, secs)
        while self._running:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            time.sleep(min(0.01, remaining))

    # ── Main loop ─────────────────────────────────────────────────────
    @Slot()
    def run(self):
        self._running = True
        count = 0
        t0 = time.monotonic()
        self.status_changed.emit("running")
        try:
            while self._running:
                # Pause
                while self._paused and self._running:
                    time.sleep(0.05)
                if not self._running:
                    break

                # Guard conditions
                if not self._window_in_focus():
                    time.sleep(0.1)
                    continue
                if not self._image_present():
                    time.sleep(0.3)
                    continue

                self._do_click()
                count += 1
                self.clicked.emit(count)

                if self.click_limit > 0 and count >= self.click_limit:
                    break
                if self.stop_after_secs > 0 and (time.monotonic() - t0) >= self.stop_after_secs:
                    break

                self._sleep(self._interval())

        except Exception as exc:
            self.error_occurred.emit(str(exc))
        finally:
            self.status_changed.emit("idle")
            self.finished.emit()


class ClickerEngine:
    """Owns the QThread that runs ClickWorker."""

    def __init__(self):
        self._thread: Optional[QThread] = None
        self._worker: Optional[ClickWorker] = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def start(self, cfg: dict) -> ClickWorker:
        self.stop()
        w = ClickWorker()
        w.button             = _BTN.get(cfg.get("button", "left"), Button.left)
        w.click_type         = cfg.get("click_type", "single")
        w.hold_duration_ms   = cfg.get("hold_duration_ms", 100)
        w.use_fixed          = cfg.get("use_fixed", False)
        w.fixed_x            = cfg.get("fixed_x", 0)
        w.fixed_y            = cfg.get("fixed_y", 0)
        w.interval_ms        = cfg.get("interval_ms", 1000)
        w.randomize          = cfg.get("randomize", False)
        w.random_range_ms    = cfg.get("random_range_ms", 100)
        w.click_limit        = cfg.get("click_limit", 0)
        w.stop_after_secs    = cfg.get("stop_after_secs", 0.0)
        w.human_mode         = cfg.get("human_mode", False)
        w.jitter_px          = cfg.get("jitter_px", 5)
        w.use_region         = cfg.get("use_region", False)
        w.region             = cfg.get("region", (0, 0, 1920, 1080))
        w.pause_on_focus     = cfg.get("pause_on_focus", False)
        w.focus_window       = cfg.get("focus_window", "")
        w.wait_for_image     = cfg.get("wait_for_image", False)
        w.image_path         = cfg.get("image_path", "")
        w.image_confidence   = cfg.get("image_confidence", 0.8)

        t = QThread()
        w.moveToThread(t)
        t.started.connect(w.run)
        w.finished.connect(t.quit)
        t.start()
        self._thread, self._worker = t, w
        return w

    def stop(self):
        if self._worker:
            self._worker.request_stop()
        if self._thread and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(3000)
        self._thread = self._worker = None

    def pause(self):
        if self._worker:
            self._worker.request_pause()

    def resume(self):
        if self._worker:
            self._worker.request_resume()
