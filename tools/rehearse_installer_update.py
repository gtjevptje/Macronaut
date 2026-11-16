"""Rehearse an INSTALLED copy updating itself through the installer, offline.

    python tools/rehearse_installer_update.py \
        --old dist/Macronaut-Setup-2.3.5.exe --new dist/Macronaut-Setup-2.3.6.exe

Why this exists
---------------
`rehearse_swap.py` proves the one-file .exe can replace itself. A copy that was
installed with `Macronaut-Setup-<ver>.exe` does not take that path at all: it
downloads the *next installer* and runs it over itself
(`updater._apply_installer`). That path shipped in 2.3.5 and could not run
until there was a 2.3.6 to update to — so the first time it ever executed would
have been on other people's machines.

Three things only a real install can show, and none of them is in the suite:

  * Setup run with the updater's exact switches really does replace a folder
    build in place, under the AppId the old installer registered.
  * What lands still *runs* — `--selftest` on the installed copy, not on dist/.
  * The app comes back afterwards. The updater passes `/RESTARTAPPLICATIONS`
    and its docstring promises a relaunch; whether one happens depends on
    Restart Manager, on timing, and on the *new* installer's script.

It drives the real installers and the real `updater._apply_installer`. The
network half (check → download → verify) is not covered, for the same reason as
in `rehearse_swap.py`: it fails safely.

Two scenarios, because the real one is a race:

  quits   the app starts Setup and exits at once — what `apply_and_quit` does.
  stays   the app is still running when Setup looks for it, so Setup has to
          close it (`CloseApplications`).

⚠ It installs Macronaut for the current user and uninstalls it again, so it
refuses to run on a machine that already has an installed copy. The installed
app is started with `USERPROFILE` pointing at a scratch directory so that it
never reads or writes the real `~/.macronaut`; the real directory is
snapshotted before and after anyway, and the run fails if it moved.

A third check runs the new installer silently *without* the updater's switch
and requires that nothing starts: an unattended install must stay quiet.

Exit 0 = both scenarios upgraded, self-tested and relaunched.
Exit 2 = upgraded and self-tested, but the app did not come back.
Exit 1 = anything else.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
import winreg
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import updater  # noqa: E402
import probe_launch  # noqa: E402
from rehearse_swap import _file_version, _sha256  # noqa: E402

# Inno Setup registers an install under "<AppId>_is1". The AppId is permanent
# (see packaging/inno/macronaut.iss.in), so this string is too.
UNINSTALL_KEY = (r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
                 r"\{2C3F7A48-9E31-4B6D-9C57-5A0E1D4F8B22}_is1")

SETUP_TIMEOUT_S = 240
WINDOW_TIMEOUT_S = 40
RELAUNCH_WAIT_S = 25


def _installed_dir():
    """Where Setup says Macronaut is installed, or None if it is not."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
            return Path(winreg.QueryValueEx(k, "InstallLocation")[0])
    except OSError:
        return None


def _processes(name: str) -> list:
    """[(pid, path)] for every running process with this image name."""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"Get-CimInstance Win32_Process -Filter \"Name='{name}'\" | "
         "ForEach-Object { \"$($_.ProcessId)|$($_.ExecutablePath)\" }"],
        capture_output=True, text=True).stdout
    found = []
    for line in out.splitlines():
        pid, _, path = line.strip().partition("|")
        if pid.isdigit():
            found.append((int(pid), path))
    return found


def _setup_running(new: Path) -> bool:
    # Inno re-executes itself from %TEMP% as "<name>.tmp"; both must be gone.
    stem = new.stem
    return bool(_processes(new.name) or _processes(stem + ".tmp"))


def _kill(pid: int) -> None:
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                   capture_output=True)


def _kill_all_apps() -> None:
    for pid, _path in _processes("Macronaut.exe"):
        _kill(pid)


def _wait_window(pids_fn, timeout: float):
    """-> (pid, title) of the first visible window owned by one of the pids."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        pids = set(pids_fn())
        for pid, title, _x, _y, w, h in probe_launch.visible_windows():
            if pid in pids and w > 50 and h > 50:
                return pid, title
        time.sleep(0.5)
    return None


def _snapshot(directory: Path) -> dict:
    out = {}
    if directory.is_dir():
        for p in directory.rglob("*"):
            try:
                if p.is_file():
                    st = p.stat()
                    out[str(p.relative_to(directory))] = (st.st_size, st.st_mtime_ns)
            except OSError:
                continue
    return out


def _run_setup_silently(setup: Path, log) -> int:
    r = subprocess.run([str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES",
                        "/NORESTART"], timeout=SETUP_TIMEOUT_S)
    log(f"    {setup.name} (plain silent install) exit {r.returncode}")
    return r.returncode


def _scenario(name: str, old: Path, new: Path, new_ver: str, log) -> dict:
    result = {"name": name, "upgraded": False, "selftest": None,
              "relaunched": False, "closed_by_setup": None}
    log(f"\n── scenario: {name} " + "─" * 40)

    _kill_all_apps()
    if _run_setup_silently(old, log) != 0:
        return result
    app_dir = _installed_dir()
    if app_dir is None or not (app_dir / "Macronaut.exe").is_file():
        log("    ✗ the old installer left no installation behind")
        return result
    exe = app_dir / "Macronaut.exe"
    log(f"    installed {_file_version(exe)} at {app_dir}")

    app = subprocess.Popen([str(exe)], cwd=str(app_dir))
    family = lambda: probe_launch.process_family(app.pid)  # noqa: E731
    win = _wait_window(family, WINDOW_TIMEOUT_S)
    if win is None:
        log("    ✗ the installed old version never showed a window")
        _kill(app.pid)
        return result
    log(f"    old version is up (window {win[1]!r})")

    # The real thing: the same function the Install button ends in.
    updater._apply_installer(new, relaunch=True)
    started = time.time()
    if name == "quits":
        # `apply_and_quit` returns and the app ends in os._exit(0) — abruptly,
        # with no window messages. TerminateProcess is the faithful stand-in.
        _kill(app.pid)
        log("    app exited straight after starting Setup (as the real one does)")

    # Setup takes a moment to appear in the process table at all.
    time.sleep(3)
    while _setup_running(new):
        if time.time() - started > SETUP_TIMEOUT_S:
            log("    ✗ Setup did not finish; killing it")
            for pid, _p in _processes(new.name) + _processes(new.stem + ".tmp"):
                _kill(pid)
            _kill_all_apps()
            return result
        time.sleep(1)
    log(f"    Setup finished after {time.time() - started:.0f}s")

    if name == "stays":
        result["closed_by_setup"] = app.poll() is not None
        log(f"    the running app was closed by Setup: {result['closed_by_setup']}")

    got = _file_version(exe)
    result["upgraded"] = got.startswith(new_ver)
    log(f"    installed version is now {got} "
        f"({'✓' if result['upgraded'] else '✗ expected ' + new_ver})")

    # Did it come back? Look before the self-test starts a process of its own.
    back = _wait_window(lambda: [pid for pid, path in _processes("Macronaut.exe")
                                 if pid != app.pid],
                        RELAUNCH_WAIT_S)
    result["relaunched"] = back is not None
    log("    app relaunched after the update: "
        + (f"✓ (window {back[1]!r})" if back else "✗ nothing came back"))
    _kill_all_apps()
    time.sleep(1)

    if result["upgraded"]:
        r = subprocess.run([str(exe), "--selftest"], cwd=str(app_dir),
                           timeout=300)
        result["selftest"] = r.returncode
        log(f"    installed copy --selftest exit {r.returncode} "
            f"({'✓' if r.returncode == 0 else '✗'})")
    return result


def _plain_install_stays_quiet(new: Path, log) -> bool:
    """A silent install nobody is watching must not open a window.

    The relaunch above is a [Run] entry in the new installer, gated on the
    updater's own switch. winget, or anybody's deployment script, runs Setup
    silently WITHOUT that switch — and a program that appears on screen during
    an unattended install is how a package gets reported.
    """
    log("\n── plain silent install (no updater switch) " + "─" * 16)
    _kill_all_apps()
    if _run_setup_silently(new, log) != 0:
        return False
    time.sleep(8)
    running = _processes("Macronaut.exe")
    log("    app left alone after a plain silent install: "
        + ("✓" if not running else f"✗ {len(running)} process(es) started"))
    _kill_all_apps()
    return not running


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--old", required=True,
                    help="the PUBLISHED installer people have (download it; "
                         "do not rebuild it)")
    ap.add_argument("--new", required=True, help="the installer about to ship")
    ap.add_argument("--only", choices=("quits", "stays"),
                    help="run one scenario instead of both")
    args = ap.parse_args(argv)

    old, new = Path(args.old).resolve(), Path(args.new).resolve()
    for p in (old, new):
        if not p.is_file():
            print(f"error: {p} does not exist")
            return 1
    if not new.name.startswith(updater.SETUP_PREFIX):
        print(f"error: {new.name} is not named like an installer "
              f"({updater.SETUP_PREFIX}<version>.exe), and the name is what "
              "updater.apply() goes by")
        return 1
    new_ver = new.stem[len(updater.SETUP_PREFIX):]

    # ⚠ A piped stdout on Windows is cp1252, and one "✓" raises
    # UnicodeEncodeError — here that would be *between* install and uninstall.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    def log(msg: str) -> None:
        print(msg, flush=True)

    if _installed_dir() is not None:
        print(f"error: Macronaut is already installed at {_installed_dir()}.\n"
              "  This rehearsal installs and then UNINSTALLS; it will not do "
              "that to a real installation. Run it on a machine without one.")
        return 1
    if _processes("Macronaut.exe"):
        print("error: a Macronaut.exe is running. Close it first — the "
              "rehearsal has to know every such process is its own.")
        return 1

    log(f"old installer: {old.name}  sha256 {_sha256(old)}")
    log(f"new installer: {new.name}  sha256 {_sha256(new)}")

    real_home = Path.home()
    real_data = real_home / ".macronaut"
    before = _snapshot(real_data)

    sandbox = Path(tempfile.mkdtemp(prefix="macronaut-installer-rehearsal-"))
    # ⚠ Every child from here on — the app, Setup, and anything Setup starts —
    # inherits this, so the installed app's `Path.home()` is the scratch
    # directory. Setup itself resolves {localappdata} through the shell API,
    # not this variable, so the install still lands where a real one does.
    os.environ["USERPROFILE"] = str(sandbox)
    log(f"app data sandboxed to {sandbox}")

    results = []
    app_dir = None
    quiet = True
    try:
        for name in ([args.only] if args.only else ["quits", "stays"]):
            results.append(_scenario(name, old, new, new_ver, log))
            app_dir = _installed_dir() or app_dir
        if not args.only:
            quiet = _plain_install_stays_quiet(new, log)
            app_dir = _installed_dir() or app_dir
    finally:
        _kill_all_apps()
        time.sleep(1)
        app_dir = _installed_dir() or app_dir
        if app_dir is not None:
            unins = app_dir / "unins000.exe"
            if unins.is_file():
                subprocess.run([str(unins), "/VERYSILENT", "/SUPPRESSMSGBOXES",
                               "/NORESTART"], timeout=SETUP_TIMEOUT_S)
                # The uninstaller copies itself to %TEMP% and returns early.
                for _ in range(60):
                    if _installed_dir() is None and not app_dir.exists():
                        break
                    time.sleep(1)
        left = _installed_dir() is not None or (app_dir and app_dir.exists())
        log("\nuninstalled: " + ("✗ something is left at %s" % app_dir if left
                                 else "✓ the machine is as it was"))
        shutil.rmtree(sandbox, ignore_errors=True)

    after = _snapshot(real_data)
    touched = sorted(k for k in set(before) | set(after)
                     if before.get(k) != after.get(k))
    if touched:
        log(f"\n✗ the REAL {real_data} changed during the rehearsal: "
            + ", ".join(touched[:10]))

    log("\n" + "=" * 60)
    ok = bool(results) and not touched and not left and quiet
    relaunch_ok = True
    for r in results:
        good = r["upgraded"] and r["selftest"] == 0
        ok = ok and good
        relaunch_ok = relaunch_ok and r["relaunched"]
        log(f"{r['name']:>6}: upgraded={r['upgraded']} selftest={r['selftest']} "
            f"relaunched={r['relaunched']}"
            + (f" closed_by_setup={r['closed_by_setup']}"
               if r["closed_by_setup"] is not None else ""))
    if not ok:
        log("INSTALLER UPDATE: FAILED")
        return 1
    if not relaunch_ok:
        log("INSTALLER UPDATE: installs and self-tests, but the app does not "
            "come back afterwards")
        return 2
    log("INSTALLER UPDATE: sound")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
