"""Crash capture — what Macronaut leaves behind when it dies.

The thing this exists for is the 2.0.8 class of failure, and it is worth being
precise about why, because the obvious implementation would have caught none of
it. Two of that release's three crashes never raised a Python exception:
destroying a running QThread is `qFatal` -> `abort()` down in C, and the run-log
backlog was memory exhaustion with the UI frozen throughout. `sys.excepthook`
does not run in either case. Neither does anything registered with `atexit`, and
neither would a "write the report as we go down" handler, because by then there
is no interpreter left to run it.

So capture is two layers:

  Layer A — what Python can still see at the moment it happens. An excepthook, a
  threading excepthook, and a Qt message handler. That last one is the important
  one: a `qFatal` message passes through the message handler *before* the abort,
  which is the only chance anything gets to record "QThread: Destroyed while
  thread is still running" in the app's own words.

  Layer B — the dead man's switch, which needs no cooperation from the dying
  process at all. Arming writes a session file; a clean exit deletes it. Finding
  one on the next launch means the previous session did not get to exit. That
  covers `abort()`, a segfault inside cv2, the OOM death, and someone pulling the
  power cable — none of which layer A can touch.

Three rules that come from this codebase specifically:

  * **Do not arm in `--apply-update` or `--selftest`.** Both deliberately exit
    without a GUI, so both would look exactly like a crash. Apply-mode runs on
    *every single update*, which at the current release cadence would make the
    false positives the dominant signal. `install()` refuses on its own rather
    than trusting the caller to remember.

  * **The session file records the version that is running when it is armed**,
    not the version that later reads it. Updates apply on restart, so the launch
    that harvests a crash is frequently already a newer build — read the version
    at harvest time and every crash gets blamed on the release that fixed it.

  * **Breadcrumbs are state changes, never events.** A per-event trail is
    precisely the 2.0.8 bug rebuilt on the filesystem instead of in the signal
    queue. Run started, node entered, backend chosen, error — a few dozen per
    run. The cap is enforced here, and overflow is *recorded* rather than
    dropped silently: the count lands in the report as `breadcrumbs_dropped`,
    because a truncated trail that looks complete is worse than one that admits
    it. The count is approximate by design — it is written the same throttled,
    overwrite-in-place way as `state()`, so a flood of dropped crumbs cannot
    turn the accounting into the very problem it is accounting for.

Nothing in this module imports Qt at import time, so it can be installed before
the QApplication exists (and tested without one). `install_qt_handler()` is the
opt-in half that does.

Nothing here sends anything anywhere. This module only ever writes files under
`~/.macronaut/crashes/`; transport is a separate concern and a separate consent
question.
"""
from __future__ import annotations

import faulthandler
import json
import os
import platform
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import List, Optional

import version

# Bumped when the report dict changes shape, so a receiver can tell an old
# client's report from a new one. Reports outlive the build that wrote them:
# a crash on 2.0.8 can easily be harvested by 2.1.0.
SCHEMA = 1

DIR_NAME = "crashes"

# Per-session breadcrumb ceiling. Generous for the state-change traffic this is
# meant to carry, and a hard stop if something ever starts logging per-event.
MAX_BREADCRUMBS = 300

# How many un-sent reports to keep. A user who crashes repeatedly offline should
# not accumulate an unbounded pile; the newest are the ones worth having.
MAX_PENDING = 20

# After this long, a session file is harvested even though its recorded PID
# still appears to be alive. PIDs are reused — reboot and the number that
# belonged to the crashed run can easily belong to a system process — and
# without a fallback that session is deferred forever: the crash is never
# reported and its files are never cleaned up. From the outside that is
# indistinguishable from "no crashes", which is the one failure this whole
# module exists to rule out. Long enough that a genuine second instance running
# for half a day is still left alone.
STALE_SESSION_S = 12 * 3600

# argv flags that mean "this process is not a user session". Arming during
# either would manufacture a crash report on a completely normal exit.
_NON_SESSION_FLAGS = ("--apply-update", "--selftest")

_lock = threading.Lock()

_dir: Optional[Path] = None
_session_path: Optional[Path] = None
_crumbs_fh = None
_native_fh = None
_armed = False
_crumb_count = 0
_crumb_dropped = 0
_prev_excepthook = None
_prev_threadhook = None
_qt_handler_installed = False
_state_last = 0.0
_dropped_last = 0.0

# How often the "right now" marker may be rewritten. See `state()`. The dropped
# breadcrumb counter reuses it, for the same reason.
_STATE_MIN_INTERVAL = 1.0


# ── scrubbing ─────────────────────────────────────────────────────────────────

def _scrub(text: str) -> str:
    """Take the user's name out of anything we might keep.

    Every traceback from a frozen build is full of `C:\\Users\\<name>\\...`, and
    the Windows account name is very often a real person's real name. There is
    no diagnostic value in it whatsoever — the part that matters is which module
    and which line — so it never gets written to disk in the first place.
    """
    if not text:
        return text
    out = str(text)
    try:
        home = str(Path.home())
        if home:
            out = out.replace(home, "%HOME%")
            # Windows paths turn up backslashed and forward-slashed, and case
            # varies with whatever produced them.
            out = out.replace(home.replace("\\", "/"), "%HOME%")
            out = out.replace(home.lower(), "%HOME%")
    except Exception:
        pass
    for var in ("USERNAME", "USER"):
        name = os.environ.get(var)
        # Guard the length: a one- or two-character username would otherwise
        # shred every unrelated word that happens to contain those letters.
        if name and len(name) >= 3:
            out = out.replace(name, "%USER%")
    return out


def _env() -> dict:
    """The context a report needs, and nothing that identifies a person."""
    try:
        win = platform.win32_ver()[0]
    except Exception:
        win = ""
    return {
        # Read here, at arming time, on purpose — see the module docstring.
        "version": version.__version__,
        "frozen": bool(getattr(sys, "frozen", False)),
        "python": platform.python_version(),
        "os": platform.system(),
        "os_release": win or platform.release(),
        "arch": platform.machine(),
    }


# ── arming ────────────────────────────────────────────────────────────────────

def crash_dir(data_dir: Optional[Path] = None) -> Path:
    if data_dir is None:
        import settings
        data_dir = settings.data_dir()
    d = Path(data_dir) / DIR_NAME
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def is_armed() -> bool:
    """Exposed for the tests. Nothing in the app asks — every production
    decision reads the private `_armed` from inside this module."""
    return _armed


def install(data_dir: Optional[Path] = None, *,
            argv: Optional[list] = None,
            native: bool = True) -> bool:
    """Arm crash capture for this session. -> True if it armed.

    Safe to call twice (the second call does nothing) and safe to call before
    the QApplication exists. Every failure mode is swallowed: a crash reporter
    that stops the app from starting is worse than no crash reporter.
    """
    global _dir, _session_path, _crumbs_fh, _native_fh, _armed
    global _crumb_count, _crumb_dropped, _prev_excepthook, _prev_threadhook
    global _state_last, _dropped_last

    with _lock:
        if _armed:
            return True
        argv = sys.argv if argv is None else argv
        if any(f in argv for f in _NON_SESSION_FLAGS):
            return False

        try:
            _dir = crash_dir(data_dir)
            stamp = time.time()
            base = "session-%d-%d" % (os.getpid(), int(stamp * 1000))
            _session_path = _dir / (base + ".json")
            info = dict(_env())
            info.update({"schema": SCHEMA, "pid": os.getpid(), "started": stamp})
            _session_path.write_text(json.dumps(info), encoding="utf-8")

            _crumbs_fh = open(_dir / (base + ".log"), "a", encoding="utf-8")
            if native:
                # Must be a real file object we hold open: a windowed
                # PyInstaller build has sys.stderr set to None, so the default
                # target would raise. faulthandler writes the Python stack from
                # inside the signal handler on SIGABRT/SIGSEGV — which is how a
                # qFatal or a segfault in cv2 leaves a trace at all.
                _native_fh = open(_dir / (base + ".native"), "a",
                                  encoding="utf-8", errors="replace")
                faulthandler.enable(file=_native_fh, all_threads=True)

            _prev_excepthook = sys.excepthook
            sys.excepthook = _on_exception
            if hasattr(threading, "excepthook"):
                _prev_threadhook = threading.excepthook
                threading.excepthook = _on_thread_exception

            _armed = True
            _crumb_count = _crumb_dropped = 0
            _state_last = _dropped_last = 0.0
        except Exception:
            # Undo a half-armed session before giving up. The session file may
            # already be on disk, and disarm() will not touch it because it only
            # acts when _armed — so leaving it there means the next launch
            # harvests a perfectly clean run as a crash with no exception and no
            # signal, i.e. `silent: True`, the bucket reserved for OOM and hard
            # kills. One build that cannot arm would otherwise file a phantom
            # crash in the highest-signal category on every single launch.
            _armed = False
            if _native_fh is not None:
                # Before closing the file: faulthandler may already be pointed
                # at it, and writing a fault into a closed handle is its own
                # crash. Disable first, close second.
                try:
                    faulthandler.disable()
                except Exception:
                    pass
            for fh in (_crumbs_fh, _native_fh):
                try:
                    if fh is not None:
                        fh.close()
                except Exception:
                    pass
            _crumbs_fh = _native_fh = None
            if _session_path is not None:
                stem = _session_path.with_suffix("")
                for p in (_session_path, stem.with_suffix(".log"),
                          stem.with_suffix(".native")):
                    try:
                        p.unlink()
                    except Exception:
                        pass
            _session_path = None
            return False

    breadcrumb("session_start", **_env())
    return True


def disarm() -> None:
    """Record this session as having ended on purpose.

    Called from `MainWindow.closeEvent`, which then ends the process with
    `os._exit(0)` — that skips `atexit` entirely, so there is no version of this
    that can be left to run itself later. If disarm does not happen here it does
    not happen at all, and the next launch reports a clean shutdown as a crash.
    """
    global _armed, _session_path, _crumbs_fh, _native_fh
    with _lock:
        if not _armed:
            return
        _armed = False
        for fh in (_crumbs_fh, _native_fh):
            try:
                if fh is not None:
                    fh.close()
            except Exception:
                pass
        paths = []
        if _session_path is not None:
            base = _session_path.with_suffix("")
            paths = [_session_path,
                     base.with_suffix(".log"),
                     base.with_suffix(".native"),
                     base.with_suffix(".fatal"),
                     base.with_suffix(".state"),
                     base.with_suffix(".dropped")]
        _crumbs_fh = _native_fh = None
        _session_path = None
    try:
        sys.excepthook = _prev_excepthook or sys.__excepthook__
        if _prev_threadhook is not None:
            threading.excepthook = _prev_threadhook
    except Exception:
        pass
    for p in paths:
        try:
            p.unlink()
        except Exception:
            pass


# ── breadcrumbs ───────────────────────────────────────────────────────────────

def breadcrumb(kind: str, **fields) -> None:
    """Record one state change. Never call this per event — see the docstring.

    Written and flushed immediately rather than buffered in memory, because the
    failures worth catching are the ones that never get to flush anything. A
    flush is enough: the data is in the OS's hands from that point, and the OS
    survives the process aborting. fsync would only add anything for a machine
    that loses power, which is not what we are chasing.
    """
    global _crumb_count, _crumb_dropped, _dropped_last
    drop_path = None
    drop_n = 0
    with _lock:
        if not _armed or _crumbs_fh is None:
            return
        if _crumb_count >= MAX_BREADCRUMBS:
            _crumb_dropped += 1
            # Say so, rather than truncating in silence. Written the same way
            # as state() — one small file, overwritten in place, throttled — so
            # that recording the overflow can never itself become the flood it
            # is recording. The consequence is that the count trails reality by
            # up to a second, which is fine: what a report needs from this is
            # "the trail is incomplete, by roughly this much", not a precise
            # tally.
            now = time.monotonic()
            if (_session_path is not None
                    and now - _dropped_last >= _STATE_MIN_INTERVAL):
                _dropped_last = now
                drop_path = _session_path.with_suffix(".dropped")
                drop_n = _crumb_dropped
        else:
            _crumb_count += 1
            rec = {"t": round(time.time(), 3), "kind": str(kind)}
            for k, v in fields.items():
                if isinstance(v, str):
                    v = _scrub(v)[:400]
                elif not isinstance(v, (int, float, bool, type(None))):
                    v = _scrub(repr(v))[:400]
                rec[k] = v
            try:
                _crumbs_fh.write(json.dumps(rec) + "\n")
                _crumbs_fh.flush()
            except Exception:
                pass
    if drop_path is not None:
        try:
            drop_path.write_text(json.dumps({"dropped": drop_n}),
                                 encoding="utf-8")
        except Exception:
            pass


# ── the "right now" marker ────────────────────────────────────────────────────

def state(**fields) -> None:
    """Record what the app is *currently* doing, overwriting the last answer.

    This exists because the single most useful fact about a crash — which node
    was executing — changes far too fast to be a breadcrumb. A tight loop enters
    hundreds of thousands of nodes a second, and appending that is the 2.0.8 bug
    with a filesystem instead of a signal queue.

    So it is not appended. One small file, rewritten in place, throttled to once
    a second. Cost is constant no matter how fast the flow runs, and the report
    gets "it was in node X, on iteration Y" instead of nothing. The trade is
    that the recorded state can be up to a second stale — which matters far less
    than it sounds, because the interesting cases sit in one place (an OCR call,
    an image match) for much longer than that.
    """
    global _state_last
    with _lock:
        if not _armed or _session_path is None:
            return
        now = time.monotonic()
        if now - _state_last < _STATE_MIN_INTERVAL:
            return
        _state_last = now
        path = _session_path.with_suffix(".state")
        rec = {"t": round(time.time(), 3)}
        for k, v in fields.items():
            rec[k] = _scrub(v)[:200] if isinstance(v, str) else v
    try:
        # Write-then-replace would be tidier, but a torn read here costs one
        # field in a diagnostic, and os.replace on Windows can fail if anything
        # has the target open. A short overwrite is the safer trade.
        path.write_text(json.dumps(rec), encoding="utf-8")
    except Exception:
        pass


# ── layer A: what Python can still see ────────────────────────────────────────

def _write_fatal(kind: str, text: str, extra: Optional[dict] = None) -> None:
    """Persist a cause of death while there is still an interpreter to do it."""
    with _lock:
        path = None if _session_path is None else _session_path.with_suffix(".fatal")
    if path is None:
        return
    rec = {"kind": kind, "t": time.time(), "text": _scrub(text)[:20000]}
    if extra:
        rec.update(extra)
    try:
        # Append: a qFatal often follows an exception that was already recorded,
        # and the earlier one is usually the more informative of the two.
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def _on_exception(exc_type, exc, tb) -> None:
    try:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        _write_fatal("exception", text, {"type": getattr(exc_type, "__name__", "?")})
        breadcrumb("fatal", type=getattr(exc_type, "__name__", "?"))
    except Exception:
        pass
    hook = _prev_excepthook or sys.__excepthook__
    try:
        hook(exc_type, exc, tb)
    except Exception:
        pass


def _on_thread_exception(args) -> None:
    """Unhandled exception in a threading.Thread — a flow worker, for instance.

    Worth capturing separately: this never reaches sys.excepthook, so before
    this the app's background threads could die completely silently.
    """
    try:
        text = "".join(traceback.format_exception(
            args.exc_type, args.exc_value, args.exc_traceback))
        _write_fatal("thread_exception", text, {
            "type": getattr(args.exc_type, "__name__", "?"),
            "thread": getattr(getattr(args, "thread", None), "name", "?"),
        })
    except Exception:
        pass
    if _prev_threadhook is not None:
        try:
            _prev_threadhook(args)
        except Exception:
            pass


def install_qt_handler() -> bool:
    """Route Qt's own messages through here. -> True if installed.

    Separate from `install()` so the rest of the module stays Qt-free, and
    because this one needs to run after PySide6 is importable.

    The reason this layer exists: `qFatal` prints through the message handler
    and *then* calls abort(). This handler is therefore the last code that runs
    with the message in hand, and the only place "QThread: Destroyed while
    thread is still running" can be written down as text rather than inferred
    from a stack.
    """
    global _qt_handler_installed
    if _qt_handler_installed:
        return True
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except Exception:
        return False

    def handler(mode, context, message):
        try:
            text = str(message)
            if mode == QtMsgType.QtFatalMsg:
                # Abort follows the moment this returns.
                _write_fatal("qt_fatal", text)
            elif mode in (QtMsgType.QtCriticalMsg, QtMsgType.QtWarningMsg):
                # Qt warns before it kills in a few cases worth keeping — the
                # QThread destruction warning among them.
                breadcrumb("qt", level=("critical"
                                        if mode == QtMsgType.QtCriticalMsg
                                        else "warning"),
                           msg=text)
        except Exception:
            pass
        try:
            # Keep the console behaviour developers rely on when not frozen.
            if not getattr(sys, "frozen", False) and sys.stderr is not None:
                sys.stderr.write(str(message) + "\n")
        except Exception:
            pass

    try:
        qInstallMessageHandler(handler)
        _qt_handler_installed = True
        return True
    except Exception:
        return False


# ── layer B: harvesting what the last session left ────────────────────────────

def _pid_alive(pid: int) -> bool:
    """Conservative: if we cannot tell, assume alive.

    Getting this backwards would harvest the session file of a *running* second
    instance, which then loses its own crash record. Waiting a launch longer to
    report a real crash costs nothing by comparison.
    """
    try:
        import updater
        return updater._pid_alive(int(pid))
    except Exception:
        return True


def _read_jsonl(path: Path, limit: int) -> List[dict]:
    out: List[dict] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return out
    return out[-limit:] if len(out) > limit else out


def harvest(data_dir: Optional[Path] = None) -> List[Path]:
    """Turn any session that never disarmed into a crash report.

    Runs at startup, before arming this session. -> the report paths written.
    """
    d = crash_dir(data_dir)
    written: List[Path] = []
    try:
        sessions = sorted(d.glob("session-*.json"))
    except Exception:
        return written

    for sp in sessions:
        try:
            info = json.loads(sp.read_text(encoding="utf-8"))
        except Exception:
            info = {}
        pid = info.get("pid")
        if pid == os.getpid():
            continue          # our own, armed moments ago
        # "Its PID is alive" normally means a second instance is running, and
        # taking its session file would cost that instance its own crash
        # record. But PIDs are recycled, so believing it forever strands the
        # report permanently — see STALE_SESSION_S. An undatable session file
        # (no usable `started`) is junk that would otherwise live forever, so
        # it ages out immediately.
        try:
            age = time.time() - float(info.get("started"))
        except (TypeError, ValueError):
            age = float("inf")
        if (isinstance(pid, int) and _pid_alive(pid)
                and age < STALE_SESSION_S):
            continue          # a live second instance — leave it alone

        base = sp.with_suffix("")
        crumbs = _read_jsonl(base.with_suffix(".log"), MAX_BREADCRUMBS)
        fatal = _read_jsonl(base.with_suffix(".fatal"), 10)
        native = ""
        try:
            native = _scrub(base.with_suffix(".native")
                            .read_text(encoding="utf-8", errors="replace"))[:20000]
        except Exception:
            pass
        doing = None
        try:
            doing = json.loads(base.with_suffix(".state").read_text(encoding="utf-8"))
        except Exception:
            pass
        dropped = 0
        try:
            dropped = int(json.loads(base.with_suffix(".dropped")
                                     .read_text(encoding="utf-8"))
                          .get("dropped") or 0)
        except Exception:
            pass

        report = {
            "schema": SCHEMA,
            "kind": "crash",
            # Everything below describes the session that DIED, not this one.
            "version": info.get("version", "unknown"),
            "frozen": info.get("frozen"),
            "python": info.get("python"),
            "os": info.get("os"),
            "os_release": info.get("os_release"),
            "arch": info.get("arch"),
            "started": info.get("started"),
            "last_seen": _mtime(base.with_suffix(".log")) or info.get("started"),
            "fatal": fatal,
            "native": native,
            "breadcrumbs": crumbs,
            # Non-zero means the trail above is truncated — see breadcrumb().
            "breadcrumbs_dropped": dropped,
            "doing": doing,
            # No exception and no signal: nothing in the process noticed it was
            # ending. That is the OOM/hard-kill shape, and worth flagging,
            # because it is the one where `doing` is the only evidence there is.
            "silent": not fatal and not native,
        }
        stamp = int((report.get("started") or time.time()) * 1000)
        out = d / ("crash-%d.json" % stamp)
        try:
            out.write_text(json.dumps(report, indent=1), encoding="utf-8")
            written.append(out)
        except Exception:
            pass
        for p in (sp, base.with_suffix(".log"), base.with_suffix(".native"),
                  base.with_suffix(".fatal"), base.with_suffix(".state"),
                  base.with_suffix(".dropped")):
            try:
                p.unlink()
            except Exception:
                pass

    _trim(d)
    return written


def _mtime(p: Path) -> Optional[float]:
    try:
        return p.stat().st_mtime
    except Exception:
        return None


def _trim(d: Path) -> None:
    try:
        reports = sorted(d.glob("crash-*.json"), key=lambda p: p.name)
    except Exception:
        return
    for p in reports[:-MAX_PENDING] if len(reports) > MAX_PENDING else []:
        try:
            p.unlink()
        except Exception:
            pass


def pending(data_dir: Optional[Path] = None) -> List[Path]:
    """Reports written but not yet sent, oldest first."""
    try:
        return sorted(crash_dir(data_dir).glob("crash-*.json"),
                      key=lambda p: p.name)
    except Exception:
        return []


def load(path: Path) -> Optional[dict]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def discard(path: Path) -> None:
    """Drop a report — sent, or declined."""
    try:
        Path(path).unlink()
    except Exception:
        pass


def summarize(report: dict) -> str:
    """One human-readable line, for the Settings list and the consent prompt."""
    when = report.get("started") or 0
    try:
        stamp = time.strftime("%d %b %Y %H:%M", time.localtime(when))
    except Exception:
        stamp = "unknown time"
    ver = report.get("version", "?")
    fatal = report.get("fatal") or []
    if fatal:
        last = fatal[-1]
        text = (last.get("text") or "").strip().splitlines()
        detail = text[-1][:120] if text else last.get("kind", "crash")
    elif report.get("native"):
        detail = "stopped in native code"
    else:
        # No exception, no signal — the dead man's switch on its own. Usually
        # means it was killed rather than that it failed.
        detail = "closed without shutting down"
    return "%s — %s (%s)" % (stamp, detail, ver)
