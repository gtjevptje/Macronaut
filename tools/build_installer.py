"""Build the Windows installer: a folder build, packaged by Inno Setup.

    python tools/build_installer.py            # build the folder, then the setup
    python tools/build_installer.py --skip-pyinstaller   # package what is in dist/

Output: `dist/Macronaut-Setup-<version>.exe`.

⚠ **Why this exists at all.** A one-file PyInstaller build unpacks itself into a
fresh `_MEI…` temp folder on every launch and runs from there, and that is the
behaviour Windows Defender's heuristics react to most strongly — PyInstaller's
own maintainers name onedir-plus-an-installer as the fix (issue #6754). Macronaut
is an unsigned auto-clicker that installs a global keyboard hook, so it starts
from behind on reputation and cannot afford the extra suspicion.

⚠ **This does not replace `Macronaut.exe`.** Every copy installed before 2.3.5
updates itself by swapping exactly one file, and a folder cannot be swapped into
a single .exe. `release.py` publishes both: the portable one-file build for those
clients and for people who want no installer, and this setup as the download the
website leads with. Two shapes of the same build; see `macronaut.spec`.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import version as _v  # noqa: E402

TEMPLATE = ROOT / "packaging" / "inno" / "macronaut.iss.in"
RENDERED = ROOT / "build" / "inno" / "macronaut.iss"
ONEDIR = ROOT / "dist" / "Macronaut"
DIST = ROOT / "dist"

# ⚠ ISCC is not on PATH after a winget install, and the compiler's absence is
# the one failure worth a clear message: everything else here is a build error
# with a traceback, this one is "nothing happened".
ISCC_CANDIDATES = (
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
    Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
    Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
)


def find_iscc() -> Path:
    found = shutil.which("ISCC")
    if found:
        return Path(found)
    for cand in ISCC_CANDIDATES:
        if cand.is_file():
            return cand
    raise SystemExit(
        "error: ISCC.exe not found — install Inno Setup 6 with\n"
        "    winget install --id JRSoftware.InnoSetup -e\n"
        "and re-run. It is a free, open-source installer compiler; nothing here "
        "needs its IDE.")


def build_folder() -> Path:
    """Run PyInstaller in onedir mode. -> dist/Macronaut/"""
    if ONEDIR.exists():
        shutil.rmtree(ONEDIR, ignore_errors=True)
    env = dict(os.environ, MACRONAUT_ONEDIR="1")
    r = subprocess.run([sys.executable, "-m", "PyInstaller", "macronaut.spec",
                        "--noconfirm", "--distpath", str(DIST)],
                       cwd=ROOT, env=env)
    if r.returncode != 0:
        raise SystemExit("error: PyInstaller failed")
    exe = ONEDIR / "Macronaut.exe"
    if not exe.is_file():
        raise SystemExit(f"error: {exe} was not produced — did MACRONAUT_ONEDIR reach the spec?")
    return ONEDIR


def render(ver: str) -> Path:
    """Fill the version and the repo root into the .iss. -> the rendered path."""
    text = TEMPLATE.read_text(encoding="utf-8")
    for token, value in (("{{VERSION}}", ver), ("{{ROOT}}", str(ROOT))):
        if token not in text:
            raise SystemExit(f"error: {TEMPLATE.name} no longer contains {token}")
        text = text.replace(token, value)
    RENDERED.parent.mkdir(parents=True, exist_ok=True)
    # Inno's compiler reads the file as ANSI unless it is told otherwise; a BOM
    # is how it is told. Without it the © and the ⚠ in the comments abort the
    # compile on a line the error message points at only approximately.
    RENDERED.write_text(text, encoding="utf-8-sig")
    return RENDERED


def setup_path(ver: str) -> Path:
    return DIST / f"Macronaut-Setup-{ver}.exe"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-pyinstaller", action="store_true",
                    help="package the existing dist/Macronaut/ folder")
    args = ap.parse_args(argv)

    ver = _v.__version__
    iscc = find_iscc()

    if args.skip_pyinstaller:
        if not (ONEDIR / "Macronaut.exe").is_file():
            raise SystemExit(f"error: {ONEDIR} has no Macronaut.exe — build first")
        print(f"  reusing {ONEDIR}")
    else:
        print(f"  building the folder build for {ver} …")
        build_folder()

    iss = render(ver)
    print(f"  compiling {iss.name} with {iscc} …")
    r = subprocess.run([str(iscc), str(iss)], cwd=ROOT)
    if r.returncode != 0:
        raise SystemExit("error: Inno Setup failed")

    out = setup_path(ver)
    if not out.is_file():
        raise SystemExit(f"error: {out} was not produced")
    print(f"\n  {out}  ({out.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
