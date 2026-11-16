"""Rehearse the update swap against real binaries, offline.

    python tools/rehearse_swap.py                       # swap dist/Macronaut.exe onto itself
    python tools/rehearse_swap.py --new dist/Macronaut.exe --old old/Macronaut.exe

Why this exists
---------------
`tests/test_updater.py` proves the swap logic with `b"OLD BUILD"` / `b"NEW BUILD"`
and a monkeypatched `_self_path`. That is the right level for the branching, but
it cannot see the three things that only exist in a real install:

  * `_self_path()` is `sys.executable`, which under PyInstaller **onefile** is the
    .exe itself and not the unpacked bundle — if that were ever wrong, every unit
    test would still pass and every real update would install garbage.
  * copying 120 MB is not copying 9 bytes: partial copies, locks and AV
    interference all live at that size.
  * the installed result has to still *run*. A byte-for-byte copy that Windows
    then refuses to execute is the failure mode a hash check cannot catch.

So this drives the real `--apply-update` entry point in the real .exe, then runs
`--selftest` on what landed. The one thing it deliberately does not cover is the
network half (check → download), because that fails *safely*: verification
rejects a bad download and nothing is applied. The swap is the step with no
do-over, which is why it is the one rehearsed offline before anything ships.

Exit 0 = the swap path is sound.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import updater  # noqa: E402  (after sys.path setup)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _file_version(path: Path) -> str:
    """Read the Windows version resource, or '?' if we cannot."""
    try:
        import win32api
        info = win32api.GetFileVersionInfo(str(path), "\\")
        ms, ls = info["FileVersionMS"], info["FileVersionLS"]
        return ".".join(str(n) for n in (ms >> 16, ms & 0xFFFF,
                                         ls >> 16, ls & 0xFFFF))
    except Exception:
        return "?"


def _dead_pid() -> int:
    """A PID that is certainly not running.

    Spawn something trivial and reap it rather than inventing a number: a made-up
    PID might belong to a live process, and `run_apply_mode` would then correctly
    refuse to swap and the rehearsal would 'fail' for the wrong reason.
    """
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def _step(n: int, msg: str) -> None:
    print(f"[{n}] {msg}")


def rehearse(new_exe: Path, old_exe: Path, keep: bool = False) -> int:
    for p in (new_exe, old_exe):
        if not p.exists():
            print(f"FAIL: {p} does not exist — build first.")
            return 1

    work = Path(tempfile.mkdtemp(prefix="macronaut-rehearsal-"))
    install = work / "install"
    install.mkdir()
    target = install / "Macronaut.exe"

    new_sha, old_sha = _sha256(new_exe), _sha256(old_exe)
    print(f"    new  {new_exe}  v{_file_version(new_exe)}  {new_sha[:12]}…")
    print(f"    old  {old_exe}  v{_file_version(old_exe)}  {old_sha[:12]}…")
    if new_sha == old_sha:
        print("    note: both sides are the same build, so this proves the swap "
              "mechanics but not a version change.")
    print(f"    work {work}\n")

    _step(1, "installing the 'old' build")
    shutil.copy2(old_exe, target)

    _step(2, "running the new build's --apply-update against it")
    pid = _dead_pid()
    started = time.monotonic()
    # APPLY_FLAG rather than the literal, so renaming it cannot leave this tool
    # silently rehearsing a flag the app no longer answers to.
    proc = subprocess.run([str(new_exe), updater.APPLY_FLAG,
                           "--target", str(target), "--pid", str(pid)],
                          capture_output=True, timeout=180)
    took = time.monotonic() - started
    # No --relaunch on purpose: this is a headless rehearsal and relaunching
    # would leave a GUI process running with nobody to close it.
    if proc.returncode != 0:
        print(f"FAIL: --apply-update exited {proc.returncode} after {took:.1f}s")
        if proc.stderr:
            print(proc.stderr.decode("utf-8", "replace")[:2000])
        print(f"    (work dir kept for inspection: {work})")
        return 1
    print(f"    exited 0 in {took:.1f}s")

    _step(3, "checking what landed")
    ok = True
    if not target.exists():
        print("FAIL: the target is gone — the swap destroyed the install")
        return 1
    got = _sha256(target)
    if got != new_sha:
        print(f"FAIL: target is {got[:12]}…, expected the new build {new_sha[:12]}…")
        ok = False
    else:
        print(f"    target is the new build  v{_file_version(target)}")

    backup = target.with_suffix(target.suffix + ".old")
    if not backup.exists():
        print("FAIL: no .old backup — a failed update would have no way back")
        ok = False
    elif _sha256(backup) != old_sha:
        print("FAIL: the .old backup is not the previous build")
        ok = False
    else:
        print(f"    previous build kept aside as {backup.name}")

    if not ok:
        print(f"    (work dir kept for inspection: {work})")
        return 1

    _step(4, "proving the installed copy still runs (--selftest)")
    # The point of the whole exercise: a hash match says the bytes arrived, not
    # that Windows will execute them.
    st = subprocess.run([str(target), "--selftest"], capture_output=True,
                        timeout=300)
    if st.returncode != 0:
        print(f"FAIL: the installed build's self-test exited {st.returncode}")
        print(st.stdout.decode("utf-8", "replace")[-2000:])
        print(f"    (work dir kept for inspection: {work})")
        return 1
    print("    installed build passes its own self-test")

    _step(5, "cleanup helper: cleanup() removes the .old leftover")
    updater.cleanup(target=target)
    if backup.exists():
        print("FAIL: cleanup() left the .old file behind")
        return 1
    print("    .old removed")

    if not keep:
        shutil.rmtree(work, ignore_errors=True)
    else:
        print(f"\n    work dir kept: {work}")

    print("\nOK: the update swap works against real binaries.")
    return 0


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--new", default=str(ROOT / "dist" / "Macronaut.exe"),
                    help="the .exe being 'downloaded' (default: dist/Macronaut.exe)")
    ap.add_argument("--old", default=None,
                    help="the .exe already installed (default: same as --new)")
    ap.add_argument("--keep", action="store_true",
                    help="keep the temporary install folder")
    args = ap.parse_args(argv)
    new = Path(args.new).resolve()
    old = Path(args.old).resolve() if args.old else new
    print("Macronaut update-swap rehearsal")
    print("-" * 72)
    return rehearse(new, old, keep=args.keep)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
