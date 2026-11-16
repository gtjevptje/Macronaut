# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Macronaut
# Build: pyinstaller macronaut.spec

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(SPECPATH)))
import version as _v

block_cipher = None

# Collect all hidden imports that PyInstaller misses for pynput / PyQt5
hidden_imports = [
    "pynput.keyboard._win32",
    "pynput.mouse._win32",
    "pynput._util.win32",
    "pynput._util",
    "win32api", "win32con", "win32gui", "win32process",
    "pyautogui",
    "PIL", "PIL.Image", "PIL.ImageGrab",
    "cv2",
    "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets",
    "shiboken6",
    "keystrokes", "recorder", "settings", "stats", "tray",
    "updater", "updater_ui", "version", "selftest",
    "crashreport", "crashsend", "crash_ui",
]

# ── OCR engines are chosen at RUNTIME, by dynamic import ──────────────────────
# ocr.py selects its engine with importlib.import_module(root + ".windows.media.ocr"),
# so PyInstaller's static analysis never sees these names and leaves them out.
# The result is the nastiest kind of broken build: Windows OCR is missing, ocr.py
# silently falls through to its fallback, `ocr.available()` still returns True,
# Settings still reports OCR as available -- and every Wait-for-Text step reads
# nothing at all. Caught by `Macronaut.exe --selftest`, never by launching it.
#
# Only the namespaces ocr.py actually imports.
#
# These are the SPLIT `winrt-*` packages, not the monolithic `winsdk` this used
# to bundle. winsdk projects the entire WinRT surface through one 38.5 MB .pyd
# to give us five namespaces; the split packages weigh 3.1 MB for the same five
# — 10 MB off every download, with identical OCR output (verified by reading
# rendered text back through both). `winsdk` is excluded below so a machine that
# still has it installed cannot quietly put it back in the bundle.
#
# ⚠ `windows.foundation` is what makes recognize_async awaitable, and the OCR
# package does NOT declare it as a dependency. Without it the engine imports,
# constructs, reports itself available — and reads nothing, forever, silently.
hidden_imports += [
    "winrt",
    "winrt.windows.media.ocr",
    "winrt.windows.graphics.imaging",
    "winrt.windows.storage.streams",
    "winrt.windows.globalization",
    "winrt.windows.foundation",
    "winrt.windows.foundation.collections",
]

# ── Windows version resource ──────────────────────────────────────────────────
# Generated from version.py so the file properties, the in-app version and the
# published manifest can never drift apart. Windows wants a 4-part numeric
# version, so the semver triple is padded with a trailing 0.
_n = _v.as_tuple() + (0,)
# NOTE: PyInstaller injects `workpath` lowercase (`WORKPATH` does not exist and
# raises NameError before Analysis even starts).
_version_res = Path(workpath) / "version_info.txt"
_version_res.parent.mkdir(parents=True, exist_ok=True)
_version_res.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={_n}, prodvers={_n}, mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Macronaut'),
      StringStruct('FileDescription', 'Macronaut — input automation'),
      StringStruct('FileVersion', '{_v.__version__}'),
      StringStruct('InternalName', 'Macronaut'),
      StringStruct('OriginalFilename', 'Macronaut.exe'),
      StringStruct('ProductName', 'Macronaut'),
      StringStruct('ProductVersion', '{_v.__version__}'),
    ])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])]),
  ],
)
""", encoding="utf-8")

# Data files (assets folder)
datas = []
assets = Path("assets")
if assets.exists():
    datas.append((str(assets), "assets"))

# Legal text ships inside the .exe so a downloaded build always carries its own
# licence — a user who only ever sees Macronaut.exe still has the terms.
for _legal in ("LICENSE", "THIRD-PARTY-NOTICES.md"):
    if (Path(SPECPATH) / _legal).exists():
        datas.append((str(Path(SPECPATH) / _legal), "."))

# `mouseinfo` is GPLv3 and is pulled in transitively by `import pyautogui`.
# Macronaut never calls it (it backs pyautogui's mouseInfo() developer window),
# and pyautogui + matcher.py were verified to import and locate fine without it,
# so it is kept out of the build rather than tainting a closed-source binary.
# See THIRD-PARTY-NOTICES.md.
gpl_excludes = ["mouseinfo"]

# ── Keep the ML toolchain out of the build ────────────────────────────────────
# `onnxruntime` ships an `onnxruntime.transformers` subpackage of model-
# optimisation developer tooling. Macronaut never touches it -- rapidocr only
# wants InferenceSession -- but its machine_info module imports torch, and
# PyInstaller follows that statically into torch -> transformers -> accelerate
# -> huggingface_hub -> pyarrow / boto3 / av / sklearn. That cascade was 300+ MB
# of an ~350 MB build, for code that never runs.
#
# Excluding the root cause is what actually shrinks it; the rest are belt and
# braces in case another chain reaches them. Verified: rapidocr_onnxruntime,
# onnxruntime.InferenceSession and ocr.py all import fine without it.
ml_excludes = [
    "onnxruntime.transformers",
    "torch", "torchvision", "transformers", "accelerate", "huggingface_hub",
    "datasets", "tokenizers", "safetensors", "hf_xet",
    "pyarrow", "boto3", "botocore", "s3transfer",
    "av", "sklearn", "nltk", "grpc", "sympy",
]

# ── The RapidOCR fallback, removed in 2.0.12 ──────────────────────────────────
# ocr.py had a second engine behind Windows OCR. It never worked in a frozen
# build: rapidocr reads its .onnx models and config.yaml from its own package
# directory, nothing here ever collected them, and there is not one model file
# in any published .exe. So the engine reported itself unavailable and every
# download carried 13.2 MB to run nothing:
#
#     onnxruntime  11.63 MB · shapely 1.46 MB · pyclipper 0.10 MB  (compressed)
#
# It is gone from ocr.py and requirements.txt too, so these are belt and braces
# for the day something transitively imports one of them again. `onnxruntime`
# here is the ROOT; the `.transformers` entry above stays because it documents a
# different and much larger trap (it drags in torch).
dead_ocr_excludes = ["rapidocr_onnxruntime", "onnxruntime", "shapely", "pyclipper"]

# The old monolithic WinRT projection, replaced by the split winrt-* packages
# above (11.04 MB -> ~1 MB compressed). ocr.py still falls back to it when only
# winsdk is installed, which is right for a source checkout and wrong for the
# build: this machine has both, and without the exclude PyInstaller follows that
# fallback import and ships 38.5 MB to be the second choice.
dead_ocr_excludes += ["winsdk"]

# NOTE: numpy must NOT be excluded. cv2 and rapidocr both import it, so
# excluding it while bundling cv2 produced a build whose image matching and OCR
# both failed at import time -- silently, because matcher.py catches ImportError
# and degrades. That combination shipped in this spec for a while; the broken
# version resource was masking it by preventing any build at all.

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=(["tkinter", "matplotlib", "scipy", "pandas"]
              + gpl_excludes + ml_excludes + dead_ocr_excludes),
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# ── Trim files Macronaut never loads ──────────────────────────────────────────
# Every entry below was measured against a real build (the compressed figure is
# what actually comes off the download, not the size on disk), and every one is
# code no code path in this app can reach. Re-measure before adding to the list:
#
#     python -c "from PyInstaller.archive.readers import CArchiveReader; \
#                a=CArchiveReader('dist/Macronaut.exe'); \
#                print(sorted(((v[1],k) for k,v in a.toc.items()), reverse=True)[:40])"
#
# This filter must run over a.datas as well as a.binaries — the Qt translations
# are DATA entries, so a binaries-only filter silently keeps 1.9 MB of them.
#
# NOT trimmed on purpose:
#   opengl32sw.dll (7.7 MB) — Qt's software OpenGL fallback. Dropping it saves
#     the single biggest Qt file, but breaks rendering on machines with broken or
#     missing GPU drivers. The MB beats a black window on someone's PC.
#   Qt6OpenGL.dll (0.4 MB) — small, and QtGui can reach for it at runtime.
#   imageformats\qwebp, qtiff, qgif, qico, qjpeg — a user browses to their own
#     template image, and a format we cannot decode shows an empty node
#     thumbnail. Cheap insurance; leave them.
#   libcrypto-3.dll / libssl-3.dll — Python's own OpenSSL. HTTPS in updater.py
#     and crashsend.py runs on these. Only the `-x64` DUPLICATES go (see below).
#   numpy.libs\libscipy_openblas64_*.dll (20.4 MB raw, 6.5 MB of the download)
#     — ⚠ TRIED AND IT BREAKS EVERYTHING. 4 September 2026. The app's entire
#     use of numpy is two `np.asarray` calls in matcher.py and no linear
#     algebra whatsoever, so this looks like 6.5 MB of free download. It is
#     not: `_multiarray_umath` links against it at *load* time, so excluding it
#     means numpy will not import — and cv2 imports numpy, so image matching
#     dies with it. Measured: the .exe drops 77.8 -> 71.4 MB and `--selftest`
#     reports "BROKEN: numpy, cv2, image match - do not ship this build".
#     Do not try this again; the size is not optional.
_unused = (
    # QML/Quick/PDF: PySide6's hooks collect them regardless; this app is pure
    # QtWidgets.
    "qt6quick", "qt6qml", "qt6qmlmodels", "qt6pdf", "imageformats/qpdf",

    # 11.82 MB. OpenCV's video-I/O backend. matcher.py calls exactly six cv2
    # functions — cvtColor, resize, matchTemplate, minMaxLoc and two constants —
    # and never opens a video. cv2 loads this DLL lazily, only for VideoCapture.
    "opencv_videoio_ffmpeg",

    # 4.33 MB. Pillow's AVIF codec. Screen captures are written as PNG.
    "pil/_avif",

    # 3.76 MB. Nothing in the app imports QtNetwork — updater.py and crashsend.py
    # both use stdlib urllib on purpose. Qt6Core/Gui/Widgets have no static
    # import of Qt6Network (checked with pefile), so it goes, and with it the TLS
    # plugin stack. The two `-x64` OpenSSL DLLs are the payoff: PyInstaller's
    # QtNetwork hook hunts OpenSSL on PATH, found Git for Windows' copy in
    # C:\Program Files\Git\mingw64\bin, and shipped 2.63 MB of it in every
    # release. Which is also to say: this build was picking up whatever happened
    # to be on the developer's PATH.
    "qt6network", "qtnetwork", "plugins/tls/", "plugins/networkinformation/",
    "libcrypto-3-x64", "libssl-3-x64",

    # 1.92 MB, 96 files. Qt's own translations. Macronaut's UI is English-only,
    # so these only ever localised Qt's stock file-dialog and message-box
    # buttons — on a non-English Windows those now read English like the rest of
    # the app. The one item here a user could notice.
    "pyside6/translations/",

    # 0.48 MB. Alternative Windows platform plugin; Qt uses qwindows unless
    # someone sets QT_QPA_PLATFORM=windows:direct2d.
    "qdirect2d",

    # 0.33 MB. QIcon/QImage SVG support. assets/ ships two .svg files but no code
    # loads them — main.py asks for macronaut.ico, macronaut_icon.png, icon.ico.
    "qt6svg", "qsvg",

    # 0.23 MB. On-screen keyboard and TUIO multi-touch; both load only when an
    # environment variable or command-line flag asks for them.
    "virtualkeyboard", "qtuiotouchplugin",
)


def _keep(entry):
    dest = entry[0].replace("\\", "/").lower()
    return not any(u in dest for u in _unused)


for _name in ("binaries", "datas"):
    _items = getattr(a, _name)
    _kept = [e for e in _items if _keep(e)]
    print(f"[macronaut.spec] trimmed {len(_items) - len(_kept)} unused {_name}")
    setattr(a, _name, _kept)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# ── One file, or one folder ───────────────────────────────────────────────────
# `MACRONAUT_ONEDIR=1` builds dist/Macronaut/ instead of dist/Macronaut.exe.
#
# ⚠ Why both exist. A one-file build unpacks itself into a fresh `_MEI…` temp
# folder on every launch and runs from there, which is the single behaviour
# Defender's heuristics react to most strongly — PyInstaller's own maintainers
# name onedir-plus-an-installer as the fix in issue 6754. The one-file build is
# still published, because every installed copy before 2.3.5 updates itself by
# swapping exactly one file and would be left stranded by a folder.
#
# So: the *installer* ships the folder, and `Macronaut.exe` stays a portable
# one-file download. Same code, two shapes, one spec.
ONEDIR = bool(os.environ.get("MACRONAUT_ONEDIR"))

exe = EXE(
    pyz,
    a.scripts,
    *([] if ONEDIR else [a.binaries, a.zipfiles, a.datas]),
    [],
    exclude_binaries=ONEDIR,
    name="Macronaut",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX off deliberately. A UPX-packed, unsigned, one-file PyInstaller build is
    # one of the most reliable ways to be flagged by antivirus heuristics — and
    # Macronaut is an input-automation tool that already looks suspicious to a
    # scanner. A quarantined .exe costs far more than the few MB UPX saves, and
    # a false positive would also break the self-updater's downloaded build.
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,                        # no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/macronaut.ico",
    # `version=`, NOT `version_file=`. `--version-file` is the COMMAND LINE flag;
    # the EXE() spec API reads `version`. EXE takes **kwargs, so a wrong key is
    # swallowed with no warning and the build silently produces an .exe with no
    # version resource at all (properties show 0.0.0.0). That shipped once.
    version=str(_version_res),
    uac_admin=False,
)


if ONEDIR:
    # The folder build. `dist/Macronaut/Macronaut.exe` beside its DLLs, which is
    # what the Inno Setup installer packages — nothing unpacks itself at launch,
    # so the `_MEI` temp-folder behaviour Defender reacts to never happens.
    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="Macronaut",
    )
