"""The installer, and the two shapes a release now ships.

⚠ **Why this file exists at all.** Macronaut's download is an unsigned one-file
PyInstaller build that installs a global keyboard hook, and a one-file build
unpacks itself into a fresh `_MEI…` temp folder on every launch — the behaviour
Windows Defender's heuristics react to most strongly, and the one PyInstaller's
own maintainers answer with "use onedir plus an installer" (issue 6754). So a
release now carries **two** artefacts built from the same spec: the portable
`Macronaut.exe`, unchanged, and `Macronaut-Setup-<ver>.exe`, which installs a
folder build.

The risk that comes with that is entirely in the seams, and every test here is
aimed at one of them:

* An installed copy from before 2.3.5 swaps **one file** to update itself. It
  reads this manifest forever and cannot be fixed by a later release, so the
  installer block must stay optional and unknown-key-tolerant, and the portable
  asset must stay the default.
* A folder build cannot be updated by swapping one .exe. It has to run the
  installer instead — and it decides that from the **filename**, which two
  different modules produce.
* A malformed installer block must not take the whole manifest down with it.
"""
import json
import os
import re
import subprocess

import pytest

import updater

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISS = os.path.join(ROOT, "tools", "packaging", "inno", "macronaut.iss.in")


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _manifest(**extra):
    data = {
        "version": "9.9.9",
        "url": "https://example.invalid/Macronaut.exe",
        "sha256": "a" * 64,
        "size": 123,
    }
    data.update(extra)
    return data


# ── the manifest ──────────────────────────────────────────────────────────────
def test_a_manifest_without_an_installer_still_parses():
    """Every manifest published before 2.3.5 lacks the block. They must keep
    working, because the copies reading them are exactly the copies that cannot
    be updated to cope with a change here."""
    info = updater.parse_manifest(_manifest())
    assert info.installer_url == ""
    assert info.kind == "exe"
    assert info.best_for_this_install() is info


def test_an_installer_block_is_read():
    info = updater.parse_manifest(_manifest(installer={
        "url": "https://example.invalid/Macronaut-Setup-9.9.9.exe",
        "sha256": "b" * 64,
        "size": 456,
    }))
    assert info.installer_url.endswith("Macronaut-Setup-9.9.9.exe")
    assert info.installer_sha256 == "b" * 64
    assert info.installer_size == 456


@pytest.mark.parametrize("block", [
    "not a dict",
    {"url": "http://example.invalid/x.exe", "sha256": "b" * 64},   # not https
    {"url": "https://example.invalid/x.exe", "sha256": "nope"},    # short hash
    {"url": "https://example.invalid/x.exe", "sha256": "z" * 64},  # not hex
    {"sha256": "b" * 64},                                          # no url
    {},
])
def test_a_broken_installer_block_is_ignored_and_never_fatal(block):
    """⚠ The portable asset beside it is still valid, so refusing the whole
    manifest would turn one bad release into "no updates at all" for every
    installed copy — including the ones that could only be fixed this way."""
    info = updater.parse_manifest(_manifest(installer=block))
    assert info.installer_url == ""
    assert info.url == "https://example.invalid/Macronaut.exe"


def test_the_portable_asset_stays_the_default_for_a_one_file_build(monkeypatch):
    monkeypatch.setattr(updater, "is_folder_build", lambda: False)
    info = updater.parse_manifest(_manifest(installer={
        "url": "https://example.invalid/Macronaut-Setup-9.9.9.exe",
        "sha256": "b" * 64, "size": 456,
    }))
    chosen = info.best_for_this_install()
    assert chosen.kind == "exe"
    assert chosen.url.endswith("/Macronaut.exe")
    assert chosen.filename == "Macronaut-9.9.9.exe"


def test_a_folder_build_takes_the_installer(monkeypatch):
    monkeypatch.setattr(updater, "is_folder_build", lambda: True)
    info = updater.parse_manifest(_manifest(installer={
        "url": "https://example.invalid/Macronaut-Setup-9.9.9.exe",
        "sha256": "b" * 64, "size": 456,
    }))
    chosen = info.best_for_this_install()
    assert chosen.kind == "installer"
    assert chosen.sha256 == "b" * 64, "the installer's own hash must travel with it"
    assert chosen.size == 456
    assert chosen.filename == f"{updater.SETUP_PREFIX}9.9.9.exe"


def test_a_folder_build_with_no_installer_published_falls_back(monkeypatch):
    """The shape of the running build is not permission to invent an asset."""
    monkeypatch.setattr(updater, "is_folder_build", lambda: True)
    info = updater.parse_manifest(_manifest())
    assert info.best_for_this_install().kind == "exe"


# ── which build am I ──────────────────────────────────────────────────────────
def test_running_from_source_is_not_a_folder_build():
    assert updater.is_folder_build() is False


def test_a_one_file_build_unpacks_somewhere_else_entirely(monkeypatch, tmp_path):
    exe = tmp_path / "app" / "Macronaut.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"x")
    meipass = tmp_path / "temp" / "_MEI12345"
    meipass.mkdir(parents=True)
    monkeypatch.setattr(updater.sys, "executable", str(exe))
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater.sys, "_MEIPASS", str(meipass), raising=False)
    assert updater.is_folder_build() is False


def test_a_folder_build_unpacks_nothing_and_says_so(monkeypatch, tmp_path):
    """⚠ The bundle lives in `_internal`, a SUBdirectory of the .exe's folder —
    PyInstaller 6 stopped putting it beside the .exe. The first version of
    `is_folder_build` compared the two for equality and answered False for every
    folder build there is, which fails safe and would therefore never have been
    noticed. The frozen build's own self-test prints the path it uses."""
    exe = tmp_path / "Macronaut" / "Macronaut.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"x")
    internal = exe.parent / "_internal"
    internal.mkdir()
    monkeypatch.setattr(updater.sys, "executable", str(exe))
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater.sys, "_MEIPASS", str(internal), raising=False)
    assert updater.is_folder_build() is True


# ── applying it ───────────────────────────────────────────────────────────────
def test_an_installer_is_run_and_never_swapped_over_the_exe(monkeypatch, tmp_path):
    """⚠ The whole hazard of shipping two shapes. Swapping the installer over
    `Macronaut.exe` would leave the user's Macronaut replaced by a setup wizard
    — a copy that no longer runs and can no longer update itself."""
    staged = tmp_path / f"{updater.SETUP_PREFIX}9.9.9.exe"
    staged.write_bytes(b"setup")
    target = tmp_path / "installed" / "Macronaut.exe"
    target.parent.mkdir()
    target.write_bytes(b"old build")

    seen = {}

    def fake_popen(args, **kw):
        seen["args"] = list(args)
        return object()

    monkeypatch.setattr(updater.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(updater, "verify_signature", lambda p: True)
    updater.apply(staged, target=target)

    assert seen["args"][0] == str(staged)
    assert "/SILENT" in seen["args"]
    assert "/RESTARTAPPLICATIONS" in seen["args"]
    assert updater.APPLY_FLAG not in seen["args"], \
        "the installer was handed the .exe-swap flags"
    assert target.read_bytes() == b"old build", "the target was touched"


def test_the_installer_brings_the_app_back_after_an_update():
    """⚠ An in-app update through the installer used to end on an empty desktop.

    The updater runs Setup with `/RESTARTAPPLICATIONS` and quits. That switch
    restarts only what registered with Windows' Restart Manager — Macronaut
    does not — so nothing relaunched. Rehearsed 16 November 2026 against the
    published 2.3.5 installer, with the app already gone and with it still
    running: installed, self-tested, and no window either time
    (`tools/rehearse_installer_update.py`).

    The fix has to live in the *installer*, because the copy doing the update
    is the old one and its switches are already shipped. So this pins the three
    things that make it work, and the one that keeps it from being a nuisance:
    a [Run] entry with no `postinstall` (a silent run has no last page), gated
    on a check that looks for the very switch the updater passes, and
    `skipifnotsilent` so an ordinary install is unaffected. A plain silent
    install — winget, a script — passes no such switch and opens nothing.
    """
    import inspect

    with open(ISS, encoding="utf-8") as fh:
        iss = fh.read()
    run = iss.split("[Run]", 1)[1].split("\n[", 1)[0]
    entries = [ln for ln in run.splitlines()
               if ln.strip().startswith("Filename:")]

    relaunch = [ln for ln in entries if "UpdaterAskedForRelaunch" in ln]
    assert len(relaunch) == 1, "the relaunch entry is missing from [Run]"
    line = relaunch[0]
    assert "skipifnotsilent" in line
    assert "nowait" in line, "Setup would wait for the app to be closed"
    assert "postinstall" not in line, (
        "a postinstall entry is a tick box on the last page, and a silent "
        "run has no last page — it would never start")

    # The check and the updater must agree on one switch, spelled one way.
    switch = "/RESTARTAPPLICATIONS"
    assert switch in inspect.getsource(updater._apply_installer)
    code = iss.split("[Code]", 1)[1]
    assert "function UpdaterAskedForRelaunch" in code
    assert f"'{switch}'" in code, (
        "the installer no longer looks for the switch the updater passes")

    # The ordinary "Start Macronaut" tick must stay out of silent runs, or an
    # update starts two copies.
    tick = [ln for ln in entries if "postinstall" in ln]
    assert len(tick) == 1 and "skipifsilent" in tick[0]


def test_a_portable_build_still_takes_the_swap_path(monkeypatch, tmp_path):
    staged = tmp_path / "Macronaut-9.9.9.exe"
    staged.write_bytes(b"new build")
    target = tmp_path / "installed" / "Macronaut.exe"
    target.parent.mkdir()
    target.write_bytes(b"old build")

    seen = {}
    monkeypatch.setattr(updater.subprocess, "Popen",
                        lambda args, **kw: seen.update(args=list(args)) or object())
    monkeypatch.setattr(updater, "verify_signature", lambda p: True)
    updater.apply(staged, target=target)

    assert updater.APPLY_FLAG in seen["args"]
    assert "/SILENT" not in seen["args"]


def test_an_unsigned_or_missing_download_is_refused_whichever_shape_it_is(tmp_path):
    missing = tmp_path / f"{updater.SETUP_PREFIX}9.9.9.exe"
    with pytest.raises(updater.UpdateError):
        updater.apply(missing, target=tmp_path / "Macronaut.exe")


def test_the_download_picks_the_asset_this_install_can_apply(monkeypatch, tmp_path):
    """`download()` chooses, not the caller. ⚠ Every caller today passes whatever
    `check()` returned, so a choice made anywhere else would have to be made in
    each UI path separately — and the one that forgot would stage an installer
    under the portable name, which `apply` then swaps over the .exe."""
    monkeypatch.setattr(updater, "is_folder_build", lambda: True)
    info = updater.parse_manifest(_manifest(installer={
        "url": "https://example.invalid/Macronaut-Setup-9.9.9.exe",
        "sha256": "b" * 64, "size": 5,
    }))

    asked = {}

    class _Resp:
        headers = {"Content-Length": "5"}

        def read(self, n):
            if asked.get("done"):
                return b""
            asked["done"] = True
            return b"setup"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_open(url, name, timeout=None):
        asked["url"] = url
        return _Resp()

    monkeypatch.setattr(updater, "_open_asset", fake_open)
    monkeypatch.setattr(updater, "verify", lambda p, i: None)
    out = updater.download(info, dest_dir=tmp_path)

    assert asked["url"].endswith("Macronaut-Setup-9.9.9.exe")
    assert out.name == f"{updater.SETUP_PREFIX}9.9.9.exe"


# ── the two modules that have to agree on the name ────────────────────────────
def test_release_and_updater_spell_the_installer_the_same_way():
    """⚠ `apply()` decides what it is holding from the filename alone, and
    `release.py` is what writes that name onto the published asset. Spelled
    differently, the installer downloads fine and is then swapped over the .exe
    as though it were a portable build."""
    import release
    assert release.setup_exe("9.9.9").name == f"{updater.SETUP_PREFIX}9.9.9.exe"


def test_the_manifest_advertises_the_installer_it_actually_built(monkeypatch, tmp_path):
    import release

    monkeypatch.setattr(release, "DIST", tmp_path)
    monkeypatch.setattr(release, "EXE", tmp_path / "Macronaut.exe")
    monkeypatch.setattr(release, "MANIFEST", tmp_path / "update.json")
    (tmp_path / "Macronaut.exe").write_bytes(b"portable")
    setup = tmp_path / f"{updater.SETUP_PREFIX}{release._v.__version__}.exe"
    setup.write_bytes(b"installer bytes")

    release.write_manifest(release._v.__version__, "notes")
    data = json.loads((tmp_path / "update.json").read_text(encoding="utf-8"))

    assert data["installer"]["sha256"] == release.sha256(setup)
    assert data["installer"]["size"] == setup.stat().st_size
    assert data["installer"]["url"].endswith(setup.name)
    assert data["url"].endswith("/Macronaut.exe"), \
        "the portable asset must stay the one every old client reads"


def test_a_release_without_an_installer_writes_no_installer_block(monkeypatch, tmp_path):
    import release

    monkeypatch.setattr(release, "DIST", tmp_path)
    monkeypatch.setattr(release, "EXE", tmp_path / "Macronaut.exe")
    monkeypatch.setattr(release, "MANIFEST", tmp_path / "update.json")
    (tmp_path / "Macronaut.exe").write_bytes(b"portable")

    release.write_manifest(release._v.__version__, "notes")
    data = json.loads((tmp_path / "update.json").read_text(encoding="utf-8"))
    assert "installer" not in data


# ── the Inno Setup script ─────────────────────────────────────────────────────
def test_the_installer_script_asks_for_no_administrator_rights():
    """⚠ An unsigned installer asking for administrator rights is the worst
    dialog this project could show a first-time user, on top of the SmartScreen
    warning they have already clicked through."""
    text = _read(ISS)
    assert re.search(r"^PrivilegesRequired=lowest\s*$", text, re.M)
    assert "{localappdata}" in text


def test_the_installer_can_replace_a_running_macronaut():
    """The updater runs it with /SILENT while the app is open. Without these two
    the update aborts on a locked file with nothing on screen to say why."""
    text = _read(ISS)
    assert re.search(r"^CloseApplications=yes\s*$", text, re.M)
    assert re.search(r"^RestartApplications=yes\s*$", text, re.M)


def test_the_installer_keeps_the_users_data():
    """⚠ `~/.macronaut` holds the script library, the settings and the licence.
    An uninstall that deletes somebody's scripts is a bug report you never get
    to read."""
    # Comments stripped: the script explains this decision in prose, and the
    # prose names the directory it is promising not to touch.
    body = "\n".join(line for line in _read(ISS).splitlines()
                     if not line.lstrip().startswith(";"))
    assert ".macronaut" not in body, \
        "the uninstaller reaches into the user's data directory"
    assert "{userappdata}" not in body and "{userdocs}" not in body


def test_the_installer_ships_the_whole_folder_build():
    """⚠ Qt's plugins and the winrt OCR packages live several directories down.
    A build missing them starts, reports OCR as available and reads nothing —
    the silent failure macronaut.spec documents at length."""
    text = _read(ISS)
    assert "recursesubdirs" in text and "createallsubdirs" in text


def test_the_app_id_is_pinned():
    """⚠ Windows recognises an existing installation by AppId. Change it and
    every upgrade becomes a second entry in Add/Remove Programs, with two Start
    Menu shortcuts and two uninstallers."""
    text = _read(ISS)
    assert "{{2C3F7A48-9E31-4B6D-9C57-5A0E1D4F8B22}" in text


def test_the_installer_is_published_under_the_product_name():
    """Since 17 September 2026 the installer shows
    "Macronaut" as its publisher, in the wizard and in Add/Remove Programs, and
    not a person's name. The winget package made the same move the day before."""
    text = _read(ISS)
    assert re.search(r'^#define AppPublisher "Macronaut"\s*$', text, re.M)
    assert "poucke" not in text.lower()


def test_the_template_still_has_the_holes_the_builder_fills():
    text = _read(ISS)
    for token in ("{{VERSION}}", "{{ROOT}}"):
        assert token in text, f"{token} is gone, so the builder cannot fill it"


def test_the_builder_renders_the_real_version(tmp_path, monkeypatch):
    import version

    import tools.build_installer as bi
    monkeypatch.setattr(bi, "RENDERED", tmp_path / "macronaut.iss")
    out = bi.render(version.__version__)
    text = out.read_text(encoding="utf-8-sig")
    assert f'#define AppVersion "{version.__version__}"' in text
    assert "{{VERSION}}" not in text and "{{ROOT}}" not in text
    assert out.read_bytes().startswith(b"\xef\xbb\xbf"), \
        "Inno reads the script as ANSI without a BOM, and the comments are not ASCII"


# ── the spec builds both shapes ───────────────────────────────────────────────
def _exec_spec(tmp_path, onedir):
    """Run macronaut.spec the way PyInstaller does, in one mode or the other."""
    class _Stub:
        def __init__(self, *a, **k):
            self.args = a
            self.__dict__.update(k)
            for name in ("pure", "zipped_data", "scripts", "binaries",
                         "zipfiles", "datas"):
                setattr(self, name, [])

    made = []

    class _Collect(_Stub):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            made.append(self)

    env = dict(os.environ)
    if onedir:
        env["MACRONAUT_ONEDIR"] = "1"
    else:
        env.pop("MACRONAUT_ONEDIR", None)
    old = os.environ.copy()
    os.environ.clear()
    os.environ.update(env)
    try:
        ns = {
            "__file__": os.path.join(ROOT, "src", "macronaut.spec"),
            "DISTPATH": str(tmp_path / "dist"),
            "HOMEPATH": str(tmp_path),
            "SPEC": os.path.join(ROOT, "src", "macronaut.spec"),
            "specnm": "macronaut",
            "SPECPATH": os.path.join(ROOT, "src"),
            "WARNFILE": str(tmp_path / "warn.txt"),
            "workpath": str(tmp_path / "build"),
            "Analysis": _Stub, "PYZ": _Stub, "EXE": _Stub, "COLLECT": _Collect,
            "BUNDLE": _Stub, "MERGE": _Stub, "Tree": _Stub, "Splash": _Stub,
            "TOC": _Stub,
        }
        exec(compile(_read(os.path.join(ROOT, "src", "macronaut.spec")),
                     "macronaut.spec", "exec"), ns)
    finally:
        os.environ.clear()
        os.environ.update(old)
    return ns, made


def test_the_spec_builds_one_file_unless_told_otherwise(tmp_path):
    """⚠ The default must not move. `release.py --build`, every CI job and every
    habit in this repository expect `dist/Macronaut.exe`."""
    ns, collected = _exec_spec(tmp_path, onedir=False)
    assert ns["ONEDIR"] is False
    assert collected == [], "a one-file build must not COLLECT a folder"
    assert ns["exe"].__dict__.get("exclude_binaries") is False


def test_the_spec_builds_a_folder_when_asked(tmp_path):
    ns, collected = _exec_spec(tmp_path, onedir=True)
    assert ns["ONEDIR"] is True
    assert len(collected) == 1, "MACRONAUT_ONEDIR did not produce a COLLECT"
    assert collected[0].__dict__.get("name") == "Macronaut"
    assert ns["exe"].__dict__.get("exclude_binaries") is True, \
        "the binaries would be stuffed into the .exe AND copied beside it"


def test_both_shapes_are_built_from_the_same_spec():
    """One spec, so a trim, an exclude or a hidden import cannot apply to one
    download and not the other — which would be invisible until somebody ran the
    shape nobody tests."""
    text = _read(os.path.join(ROOT, "src", "macronaut.spec"))
    assert text.count("a = Analysis(") == 1
    assert "MACRONAUT_ONEDIR" in text

