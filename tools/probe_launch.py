r"""Does the frozen build actually put a window on screen?

    python tools/probe_launch.py                     # dist/Macronaut.exe
    python tools/probe_launch.py --exe path\to\Macronaut.exe --seconds 30

`--selftest` proves the bundled *features* work, but it never opens a window, so
it cannot see the one failure that matters most to a first-time user: they
double-click the .exe and nothing appears. This launches the real binary,
watches for a visible top-level window, reports its size and position, and kills
it again. It is not a substitute for looking at the thing — it cannot tell you
the window is legible, only that it exists.

⚠ The trap this exists to avoid
-------------------------------
A PyInstaller **onefile** build is two processes: the bootloader you launched
unpacks the bundle and spawns a *child* that runs the actual app. The window
belongs to the child. Enumerating windows by the PID you started returns
nothing, for ~20 seconds, very convincingly — it looks exactly like "the app
launches but never shows a window", which is a frightening and completely wrong
conclusion. So this walks the whole process tree.

Second trap: verify the probe itself before trusting a negative. A GUI probe
that silently finds nothing is indistinguishable from a broken GUI, so --control
first opens a plain Tk window and refuses to report on anything until it has
detected that.

Third trap, and the one this got wrong for real: the same two-process fact
applies to *cleaning up*. `proc.kill()` ends the bootloader and leaves the child
running — the child being the one that owns the window, the tray icon and a
global keyboard hook. The probe then exits reporting success while a Macronaut
nobody is tracking keeps running indefinitely. So the whole family is killed,
and the family has to be captured **before** anything dies: the moment the
bootloader goes, the parent/child link `process_family()` walks goes with it and
the survivor becomes unfindable.

Killing it has a second consequence worth knowing about: an abnormal exit is
exactly what crashreport's dead man's switch is built to notice, so every probe
run would otherwise file a phantom crash on the next launch. The session files
belonging to the pids we killed are removed — but only when *we* did the
killing. If the app died by itself, that file is the record of a real startup
crash and is the most valuable thing the run produced.
"""
from __future__ import annotations

import argparse
import ctypes
import os
import signal
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_u32 = ctypes.windll.user32
_EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)


def visible_windows() -> list:
    """Every visible top-level window: (pid, title, x, y, w, h)."""
    out = []

    def cb(hwnd, _):
        if _u32.IsWindowVisible(hwnd):
            pid = wintypes.DWORD()
            _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            n = _u32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 2)
            _u32.GetWindowTextW(hwnd, buf, n + 2)
            r = wintypes.RECT()
            _u32.GetWindowRect(hwnd, ctypes.byref(r))
            out.append((pid.value, buf.value, r.left, r.top,
                        r.right - r.left, r.bottom - r.top))
        return True

    # Hold a reference: a garbage-collected callback yields an empty result
    # rather than an error, which is its own false negative.
    held = _EnumProc(cb)
    _u32.EnumWindows(held, 0)
    return out


def _process_table() -> tuple:
    """One CIM query -> (children_by_parent, every_live_pid)."""
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId"
         " | ConvertTo-Csv -NoTypeInformation"],
        capture_output=True, text=True)
    children: dict = {}
    alive: set = set()
    for line in proc.stdout.splitlines()[1:]:
        try:
            pid, parent = (int(v) for v in line.replace('"', "").split(","))
        except ValueError:
            continue
        children.setdefault(parent, []).append(pid)
        alive.add(pid)
    return children, alive


def process_family(root_pid: int) -> set:
    """`root_pid` plus every descendant — see the onefile note above."""
    children, _ = _process_table()
    seen, queue = {root_pid}, [root_pid]
    while queue:
        for child in children.get(queue.pop(), []):
            if child not in seen:
                seen.add(child)
                queue.append(child)
    return seen


def terminate_family(proc, family: set) -> set:
    """Stop the launched process and every descendant. -> pids still alive.

    Descendants first, the launcher last. `family` must have been captured
    while everything was still running — see the third trap in the module
    docstring.

    os.kill with anything other than a console-control signal is
    TerminateProcess on Windows, so this is a hard kill; there is no graceful
    shutdown to ask for from a process we only started to look at. Note that
    os.kill(pid, 0) is NOT a liveness probe here the way it is on POSIX — it
    would terminate the process with exit code 0 — which is why survivors are
    checked against the process table instead.
    """
    for pid in sorted(family - {proc.pid}, reverse=True):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass                      # already gone, or not ours to kill
    if proc.poll() is None:
        proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    # Windows tears processes down asynchronously, so a pid still listed one
    # moment after the kill is not yet a survivor. Give it a beat before saying
    # anything survived.
    left = set()
    for _ in range(10):
        _, alive = _process_table()
        left = family & alive
        if not left:
            break
        time.sleep(0.3)
    return left


def _control() -> bool:
    """Prove the probe can see a window it knows exists."""
    p = subprocess.Popen(
        [sys.executable, "-c",
         'import tkinter as t; r=t.Tk(); r.title("PROBE CONTROL");'
         ' r.geometry("300x120"); r.after(15000, r.destroy); r.mainloop()'])
    try:
        for _ in range(20):
            time.sleep(0.4)
            if any(w[0] == p.pid for w in visible_windows()):
                return True
        return False
    finally:
        p.kill()
        p.wait()


def clear_session_files(pids: set) -> int:
    """Remove the crash-session files left by processes THIS probe killed.

    Killing the app is an abnormal exit, so crashreport's dead man's switch
    correctly records one — and the next launch would file a phantom "silent"
    crash for a process that was perfectly healthy when we shot it. `silent` is
    the bucket reserved for OOM and hard kills, and this tool runs before every
    release, so left alone it would steadily pollute the one signal the crash
    reporter exists to provide. Exactly the false positive `install()` refuses
    --apply-update for, arriving by a different door.

    Scope is deliberately narrow. Only `session-<pid>-*`, and only for pids
    this probe started and killed. Never `crash-*.json`: those are harvested
    reports and may be real — including one this very launch may have just
    harvested from an earlier genuine crash.

    The path comes from crashreport itself rather than being rebuilt here, so
    the legacy-directory fallback cannot drift out of sync.
    """
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        import crashreport
        d = crashreport.crash_dir()
    except Exception as exc:                      # not fatal: this is tidying
        print(f"  note: could not locate the crash directory ({exc}); a session "
              "file may be left behind.")
        return 0
    removed = 0
    for pid in sorted(pids):
        for p in sorted(d.glob("session-%d-*" % pid)):
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def probe(exe: Path, seconds: float) -> int:
    print(f"probing {exe}")
    if not exe.exists():
        print("FAIL: no such file")
        return 1

    print("  validating the probe itself…", end=" ", flush=True)
    if not _control():
        print("FAILED")
        print("  The probe cannot detect a window it opened itself, so any "
              "result about Macronaut would be meaningless. Not reporting one.")
        return 2
    print("ok")

    proc = subprocess.Popen([str(exe)])
    print(f"  launched pid {proc.pid} (onefile bootloader; the window belongs "
          "to its child)")
    # Last known membership, kept outside the loop so the cleanup below still
    # has it after the bootloader has gone and the tree can no longer be walked.
    family = {proc.pid}
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            time.sleep(0.5)
            # Refresh BEFORE testing whether the launcher is still up: once it
            # exits, any surviving child is orphaned and unfindable.
            family = process_family(proc.pid)
            if proc.poll() is not None:
                print(f"FAIL: exited early with code {proc.returncode} — "
                      "no window was ever shown")
                return 1
            found = [w for w in visible_windows() if w[0] in family]
            if found:
                print(f"  process family: {sorted(family)}")
                for pid, title, x, y, w, h in found:
                    print(f"  WINDOW  pid={pid}  {w}x{h} at ({x},{y})  "
                          f"title={title!r}")
                if len(found) > 1:
                    print("  note: more than one window — check none of them is "
                          "a PyInstaller traceback dialog.")
                print("\nOK: the build starts and shows a window. Whether it "
                      "LOOKS right still needs a human.")
                return 0
        print(f"FAIL: no visible window within {seconds:g}s, though the process "
              "is still alive.")
        return 1
    finally:
        # Decided BEFORE the kill, because the kill destroys the distinction:
        # a process that was still running is one we are about to end
        # artificially, and its crash session is our litter. A process that had
        # already gone died on its own — that session file is the record of a
        # real startup crash, and is the single most useful thing this run
        # produced. Never delete that one.
        ours_to_clean = proc.poll() is None
        left = terminate_family(proc, family)
        if left:
            # Loud on purpose: a Macronaut nobody is tracking still owns a
            # global keyboard hook, and the next probe would report on a build
            # that is not the one it launched.
            print(f"\n  WARNING: {sorted(left)} survived the cleanup. A live "
                  "Macronaut holds a global keyboard hook — stop it by hand "
                  "before running this again.")
        elif ours_to_clean:
            n = clear_session_files(family)
            if n:
                print(f"  cleaned up {n} crash-session file(s) from the kill — "
                      "we ended it, so it was not a crash.")
        else:
            print("  kept the crash session on disk: the app ended on its own, "
                  "so that file is evidence. Next launch will report it.")


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--exe", default=str(ROOT / "dist" / "Macronaut.exe"))
    ap.add_argument("--seconds", type=float, default=30.0)
    args = ap.parse_args(argv)
    return probe(Path(args.exe).resolve(), args.seconds)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
