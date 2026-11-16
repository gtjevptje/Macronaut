"""Guards on the licensing and packaging decisions.

These are one-line settings that are easy to revert by accident during an
unrelated edit, and getting them wrong is expensive rather than merely broken.

⚠ What "wrong" means here inverted on 30 August 2026. Macronaut was
proprietary, and these tests guarded against a permissive licence sneaking in
and a GPL component obliging the app to be GPL. Macronaut is now
GPL-3.0-or-later itself, and the thing to guard is that it stays that way: the
licence file is the real GPL rather than a summary of one, the app carries it,
and the source link the app shows is the repo that actually exists. A relicence
back to proprietary is a decision, not an accident — but it should not be
possible to do it by editing one line and having the suite stay green.
"""
import glob
import json
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(name):
    with open(os.path.join(ROOT, name), "r", encoding="utf-8") as fh:
        return fh.read()


# ── licence ───────────────────────────────────────────────────────────────────
def test_license_is_the_real_gpl3_not_a_summary_of_one():
    """A licence file has to be the licence, not a description of it.

    "Licensed under the GPL, see the FSF website" grants nothing on its own,
    and it is the natural thing to write when relicensing by hand. The GPL is
    35KB of specific text; assert enough of its structure that a paraphrase
    cannot pass.
    """
    text = _read("LICENSE")
    assert "GNU GENERAL PUBLIC LICENSE" in text
    assert "Version 3, 29 June 2007" in text
    # The operative grant, and the two disclaimers that stand in for the
    # warranty and liability sections the old EULA spelled out.
    assert "distribute copies" in text.lower()
    assert "15. Disclaimer of Warranty" in text
    assert "16. Limitation of Liability" in text
    assert len(text) > 30_000, "LICENSE is too short to be the GPL text"


def test_the_app_says_the_same_licence_the_licence_file_does():
    """Three places name the licence and they must not drift apart.

    A user who reads "all rights reserved" in Settings and finds the GPL in
    the LICENSE button next to it has been told two different things about
    what they are allowed to do with the program in front of them.
    """
    for name in ("README.md", "main.py"):
        text = _read(name)
        assert "GPL-3.0-or-later" in text, f"{name} does not name the licence"
        assert "All rights reserved" not in text, \
            f"{name} still claims all rights are reserved"


def test_the_source_link_in_the_app_points_at_a_real_repo():
    """GPL §6 is satisfied by publishing, but only if the link is right.

    `entitlements.SOURCE_URL` is rendered into Settings ▸ About and into the
    site. A typo in it is invisible in every test that does not look at it,
    and it is the one link whose whole job is to be checkable.
    """
    import entitlements
    assert entitlements.SOURCE_URL.startswith("https://github.com/")
    assert entitlements.SOURCE_URL.rstrip("/") == entitlements.SOURCE_URL
    # The site renders the same constant, so a broken link is broken twice.
    assert "github.com" in _read("README.md")


def test_releasing_with_a_placeholder_update_repo_is_refused(monkeypatch):
    """⚠⚠ The one mistake in this file with no recovery path at all.

    An installed build asks the URL baked into its own `.exe` forever, so
    `version.UPDATE_REPO` is permanent the moment anything ships. A release
    published with the placeholder still in it produces binaries that ask a
    repository which does not exist — and the only channel for correcting them
    is the one that is broken. There is no message you can send afterwards.

    It used to print a warning and write the manifest anyway. The evidence
    that a warning is not a control was already in this repository: eight
    release pages went out empty behind a warning in the same file, which is
    what `test_release_notes_are_found_by_name_and_every_release_has_them`
    exists for.

    `publish()` refuses a missing LICENSE or THIRD-PARTY-NOTICES.md the same
    way now — conveying a GPL work means conveying its licence — but that one
    needs the whole release path to exercise, and the suite already pins those
    files existing and being the real GPL.
    """
    import release
    import version

    # ⚠ Read the real value BEFORE patching. `release._v` *is* the `version`
    # module — release.py does `import version as _v` — so patching one
    # patches both, and asserting against `version.UPDATE_REPO` afterwards
    # reads back the fake. That mistake was in the first draft of this test
    # and it failed loudly, which is the only reason it is written down here.
    real = version.UPDATE_REPO
    assert "OWNER/" not in real, f"UPDATE_REPO is a placeholder: {real!r}"
    assert real.count("/") == 1, real

    monkeypatch.setattr(release._v, "UPDATE_REPO", "OWNER/REPO")
    with pytest.raises(SystemExit) as e:
        release.write_manifest("9.9.9")
    assert "placeholder" in str(e.value).lower(), (
        "write_manifest no longer refuses a placeholder UPDATE_REPO — a "
        "release published with one can never be updated")


def test_release_notes_are_found_by_name_and_every_release_has_them():
    """⚠ Eight of the first twenty-six releases went out with an empty page.

    Their body was `Macronaut 2.0.14` and nothing else — on the page a person
    lands on to download the thing. `release.py` printed a warning every time
    and published anyway, because that is what a warning does. Backfilled on
    3 September 2026 from the development record.

    The fix is that the notes file is now *found* by name rather than
    remembered: `RELEASE-NOTES-<version>.md`, resolved in both `publish()` and
    at manifest time. Both matter and for different audiences — the manifest
    is what the in-app update dialog shows, and it can be written by a
    separate invocation from the one that publishes, so a fix in only one
    place moves the empty page from GitHub to every user's update dialog.

    This pins the convention rather than the plumbing: every version that has
    ever shipped a notes file keeps one, and the resolver still finds it.
    """
    import release

    # The resolver agrees with the convention the files on disk use.
    for name in os.listdir(ROOT):
        m = re.fullmatch(r"RELEASE-NOTES-(\d+\.\d+\.\d+)\.md", name)
        if not m:
            continue
        found = release.notes_file_for(m.group(1))
        assert found.name == name, (
            f"release.py looks for {found.name} but the repo has {name}")
        assert found.is_file(), f"{found} is not found by its own resolver"

    # The current version must have one before it can be released, and the
    # published README quotes the same version.
    import version
    here = release.notes_file_for(version.__version__)
    assert here.is_file(), (
        f"{here.name} does not exist — publishing {version.__version__} now "
        "would produce a release page with nothing on it")
    assert len(here.read_text(encoding="utf-8").strip()) > 200, (
        f"{here.name} is too short to be release notes")


def test_the_scoop_and_winget_manifests_describe_the_same_download():
    """⚠ Two manifests, one binary, and nothing regenerates either of them.

    `packaging/scoop/macronaut.json` and the winget YAMLs each hardcode a
    version, a URL and a SHA-256. They are hand-maintained, they are not
    published yet, and the realistic mistake is updating one and forgetting the
    other — at which point the surviving stale one is submitted to a public
    package repository and rejected. A rejected package PR is a public record
    that is awkward to reopen, which is exactly why this is worth catching in
    the suite instead of in somebody else's CI.

    Deliberately checks the two against *each other* rather than against
    `version.__version__`. Coupling them to the app's version would fail every
    release the moment `release.py --bump` runs, before there is anything to
    regenerate them from — a test that is red for a correct reason nobody can
    act on gets disabled, and then catches nothing.

    Parsed with a regex rather than PyYAML, which is not in requirements.txt
    and so is not guaranteed on a clean clone or on the CI runner.
    """
    scoop_file = os.path.join(ROOT, "packaging", "scoop", "macronaut.json")
    if not os.path.isfile(scoop_file):
        pytest.skip("packaging/ is not in this checkout")

    with open(scoop_file, encoding="utf-8") as fh:
        scoop = json.load(fh)
    s_arch = scoop["architecture"]["64bit"]

    pattern = os.path.join(ROOT, "packaging", "winget", "**", "*.installer.yaml")
    wingets = sorted(glob.glob(pattern, recursive=True))
    assert wingets, "there is no winget installer manifest at all"

    # ⚠ The NEWEST one, not the only one. winget-pkgs keeps every version that
    # was ever submitted, so this directory grows by one per release and the
    # older manifests are a record of what was published rather than drafts to
    # keep current. Comparing Scoop against all of them would fail the day a
    # second version exists; comparing it against the newest keeps the thing
    # this test is actually for — the two files that describe the *next*
    # submission must not disagree.
    def _version_of(path):
        name = os.path.basename(os.path.dirname(path))
        return tuple(int(p) if p.isdigit() else -1 for p in name.split("."))

    newest = max(wingets, key=_version_of)
    with open(newest, encoding="utf-8") as fh:
        w = fh.read()

    def field(name):
        m = re.search(rf"^\s*{name}:\s*(\S+)\s*$", w, re.MULTILINE)
        assert m, f"winget installer manifest has no {name}"
        return m.group(1)

    assert scoop["version"] == field("PackageVersion"), (
        f"scoop says {scoop['version']}, winget says {field('PackageVersion')}")
    assert s_arch["url"] == field("InstallerUrl"), "the two point at different files"
    # winget wants the digest uppercase and scoop wants it lowercase; the point
    # is that they are the same bytes, not the same string.
    assert s_arch["hash"].lower() == field("InstallerSha256").lower(), (
        "the two manifests carry different SHA-256s for the same download")

    # ⚠ Both must name the canonical repo, not `macronaut-releases`. That name
    # only still resolves because of GitHub's rename redirect, and
    # the redirect dies the instant a repo takes that name again.
    # update.json is stuck with it forever in already-shipped builds; nothing
    # written today should add to that.
    for url in (s_arch["url"], field("InstallerUrl")):
        assert "macronaut-releases" not in url, (
            f"{url} depends on the rename redirect — use gtjevptje/Macronaut")
        assert scoop["version"] in url, "the URL does not match the version it claims"


def test_third_party_notices_record_the_lgpl_election():
    text = _read("THIRD-PARTY-NOTICES.md")
    assert "mouseinfo" in text
    # PySide6 is offered as "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only".
    # Any branch is clean now that Macronaut is GPL-3.0-or-later, but a
    # recorded election is still a recorded election and should not silently
    # become an assumed one.
    assert "PySide6" in text
    assert "elects LGPL-3.0-only" in text


def test_no_gpl_qt_binding_anywhere_in_the_app():
    """One Qt binding, and it is PySide6.

    ⚠ This used to be a licence constraint — PyQt5's GPL v3 would have
    obliged a then-proprietary Macronaut to be GPL. That reason expired on
    30 August 2026 and PyQt5 would be licence-clean today. The test stays
    because the *other* reason did not expire: two Qt bindings in one process
    is a crash, not a style question, and `pip install PyQt5` for an unrelated
    experiment is how the second one arrives.

    Matches real usage, not the word: prose about PyQt5 is worth keeping, and
    a naive substring check would forbid its own rationale.
    """
    import glob
    imports = re.compile(r"^\s*(?:from|import)\s+PyQt5", re.MULTILINE)
    offenders = []
    for path in glob.glob(os.path.join(ROOT, "*.py")):
        with open(path, "r", encoding="utf-8") as fh:
            if imports.search(fh.read()):
                offenders.append(os.path.basename(path))
    assert not offenders, f"PyQt5 (GPL) reintroduced in: {offenders}"

    # A requirement line, as opposed to a comment mentioning the name.
    reqs = [ln.strip() for ln in _read("requirements.txt").splitlines()
            if ln.strip() and not ln.strip().startswith("#")]
    assert not [r for r in reqs if r.lower().startswith("pyqt5")]
    assert any(r.lower().startswith("pyside6") for r in reqs)

    spec = _read("macronaut.spec")
    assert '"PyQt5' not in spec and "'PyQt5" not in spec
    assert "PySide6" in spec


# ── packaging ─────────────────────────────────────────────────────────────────
def test_spec_excludes_the_gpl_component():
    spec = _read("macronaut.spec")
    assert "mouseinfo" in spec, "GPLv3 mouseinfo must stay out of the build"
    assert "gpl_excludes" in spec.split("excludes=")[1][:120], \
        "gpl_excludes is defined but no longer wired into Analysis(excludes=)"


def test_spec_does_not_exclude_numpy():
    """cv2 and rapidocr both import numpy.

    Excluding it while still bundling cv2 produces a build where image matching
    and OCR fail at import — and fail *silently*, because matcher.py catches
    ImportError and degrades to a fallback that needs cv2 too. Nothing crashes;
    the features just stop working in the frozen app and nowhere else.
    """
    spec = _read("macronaut.spec")
    excl = spec.split("excludes=", 1)[1][:400]
    assert '"numpy"' not in excl and "'numpy'" not in excl


def test_spec_keeps_the_ml_toolchain_out():
    # onnxruntime.transformers imports torch, and PyInstaller follows it into
    # 300+ MB of ML tooling that never runs. Excluding the root cause is what
    # keeps the download reasonable for a self-updating app.
    spec = _read("macronaut.spec")
    assert "onnxruntime.transformers" in spec
    assert "ml_excludes" in spec.split("excludes=")[1][:200]


def test_spec_keeps_the_dead_ocr_fallback_out():
    """rapidocr could never run frozen, and cost 13.2 MB to prove it.

    Its .onnx models and config.yaml live in its own package directory and were
    never collected, so `RapidOCR()` failed to construct in every published .exe
    while onnxruntime, shapely and pyclipper still shipped. Removed in 2.0.12.
    The hazard now is the reverse of the old one: a transitive import quietly
    puts 13 MB back and nothing looks any different.
    """
    spec = _read("macronaut.spec")
    assert "dead_ocr_excludes" in spec.split("excludes=")[1][:200], \
        "dead_ocr_excludes is defined but no longer wired into Analysis(excludes=)"
    for mod in ("rapidocr_onnxruntime", "onnxruntime", "shapely", "pyclipper"):
        assert f'"{mod}"' in spec, f"{mod} is no longer excluded"

    reqs = [ln.strip() for ln in _read("requirements.txt").splitlines()
            if ln.strip() and not ln.strip().startswith("#")]
    assert not [r for r in reqs if "rapidocr" in r.lower()]


def test_no_source_file_still_reaches_for_the_removed_engine():
    """A leftover ocr.is_fallback() call raises AttributeError, not ImportError.

    It sat in main.py's startup path and in selftest, so a half-done removal
    would fail at runtime in the .exe and nowhere else.
    """
    import glob
    offenders = []
    for path in (glob.glob(os.path.join(ROOT, "*.py"))
                 + glob.glob(os.path.join(ROOT, "tools", "*.py"))):
        with open(path, "r", encoding="utf-8") as fh:
            body = fh.read()
        if re.search(r"\bis_fallback\b|\bRapidOcrService\b|rapidocr_onnxruntime",
                     body):
            offenders.append(os.path.basename(path))
    assert not offenders, f"removed OCR fallback still referenced in: {offenders}"

    import ocr
    assert len(ocr._ENGINE_PRIORITY) == 1


def test_the_ocr_projection_is_the_split_one_and_carries_foundation(tmp_path):
    """winsdk is 38.5 MB to provide five namespaces the winrt-* packages do in 3.1.

    The `foundation` entry is the load-bearing one and the easiest to drop as
    "nothing imports it": it is what makes recognize_async awaitable. Leave it
    out and Windows OCR imports, constructs, reports itself available, and reads
    nothing forever — read_regions swallows the ModuleNotFoundError because an
    OCR failure must not kill a running flow. That is the exact set the old
    requirements.txt recommended.
    """
    # Read the values the spec hands to Analysis, not the text around them —
    # both lists are assembled from several pieces and a substring window over
    # the source moves every time a comment does.
    ana = _exec_spec(tmp_path)["a"]
    for ns in ("media.ocr", "graphics.imaging", "storage.streams",
               "globalization", "foundation"):
        assert f"winrt.windows.{ns}" in ana.hiddenimports, \
            f"winrt.windows.{ns} is not bundled"
    assert not [h for h in ana.hiddenimports if h.startswith("winsdk")], \
        "the monolithic winsdk projection is back in hiddenimports (+10 MB)"
    assert "winsdk" in ana.excludes, \
        "winsdk must be excluded, or the ocr.py fallback drags it into the build"

    reqs = _read("requirements.txt")
    assert "winrt-Windows.Foundation;" in reqs

    import ocr
    assert ocr.WindowsOcrService._ROOTS[0] == "winrt"
    import inspect
    assert "windows.foundation" in inspect.getsource(
        ocr.WindowsOcrService._load_modules), \
        "_load_modules no longer proves the projection can await"


def test_the_trim_filters_data_files_and_not_only_binaries(tmp_path):
    """Qt's translations are DATA entries — 1.92 MB a binaries-only filter keeps.

    The original trim only walked a.binaries, which is correct for a .dll and
    silently wrong for a .qm. Assert both lists actually get filtered, using the
    spec's own predicate rather than trusting the loop.
    """
    ns = _exec_spec(tmp_path)
    keep = ns["_keep"]
    assert keep(("PySide6\\Qt6Widgets.dll", "/x", "BINARY"))
    assert keep(("assets\\macronaut.ico", "/x", "DATA"))
    assert not keep(("PySide6\\translations\\qtbase_de.qm", "/x", "DATA"))

    spec = _read("macronaut.spec")
    assert 'for _name in ("binaries", "datas")' in spec, \
        "the trim no longer walks a.datas — Qt translations would come back"


def test_the_trim_leaves_the_things_that_only_look_unused(tmp_path):
    """Near-misses, each of which breaks something real if trimmed.

    A substring filter is the right tool here and also a loaded gun: it matches
    whatever it matches. `libcrypto-3-x64` and `libcrypto-3` differ by a suffix,
    and dropping the wrong one takes HTTPS — the updater and crash reporting —
    with it, in the frozen build only.
    """
    ns_keep = _exec_spec(tmp_path)["_keep"]

    for dest in ("libcrypto-3.dll", "libssl-3.dll",          # Python's OpenSSL
                 "PySide6\\opengl32sw.dll",                  # software GL
                 "PySide6\\plugins\\platforms\\qwindows.dll",  # the platform
                 "PySide6\\plugins\\imageformats\\qwebp.dll",  # user templates
                 "cv2\\cv2.pyd", "numpy.libs\\libscipy_openblas64_-abc.dll"):
        assert ns_keep((dest, "/x", "BINARY")), f"the trim would drop {dest}"

    for dest in ("cv2\\opencv_videoio_ffmpeg4130_64.dll",
                 "PIL\\_avif.cp312-win_amd64.pyd",
                 "libcrypto-3-x64-b3fdc532034c.dll",
                 "PySide6\\Qt6Network.dll",
                 "PySide6\\plugins\\tls\\qopensslbackend.dll",
                 "PySide6\\plugins\\platforms\\qdirect2d.dll"):
        assert not ns_keep((dest, "/x", "BINARY")), f"the trim missed {dest}"


def test_spec_does_not_upx_pack():
    # UPX + unsigned + one-file PyInstaller is a reliable antivirus false
    # positive, and a quarantined .exe also breaks the self-updater.
    spec = _read("macronaut.spec")
    assert re.search(r"^\s*upx=False,", spec, re.MULTILINE)


def test_spec_bundles_the_legal_files():
    spec = _read("macronaut.spec")
    assert "LICENSE" in spec and "THIRD-PARTY-NOTICES.md" in spec, \
        "a downloaded .exe must carry its own terms"


def test_readme_tells_a_visitor_the_licence_and_how_to_build_it():
    """The README is the first thing a visitor to the repository reads.

    Two things a stranger looks for before anything else — what am I allowed
    to do with this, and can I build it myself. The second is the whole reason
    the source is published: "read the code" only answers the trust objection
    if the code demonstrably produces the binary being offered.
    """
    readme = _read("README.md")
    assert "GNU General Public License" in readme
    assert "pyinstaller macronaut.spec" in readme
    assert "Proprietary" not in readme


# ── the spec actually runs ────────────────────────────────────────────────────
def _exec_spec(tmp_path):
    """Execute macronaut.spec with PyInstaller's injected namespace stubbed out.

    The spec is only ever run by PyInstaller at build time, so a plain NameError
    in it stays invisible until someone cuts a release -- exactly how a
    `WORKPATH` typo (the real name is lowercase `workpath`) shipped unnoticed and
    broke every build. Executing it here turns that into a failing test.

    Returns the resulting namespace so tests can inspect what the spec passed to
    Analysis/EXE.
    """
    class _Stub:
        # Analysis/PYZ/EXE are only used for their constructor side effects here.
        def __init__(self, *a, **k):
            self.__dict__.update(k)
            for name in ("pure", "zipped_data", "scripts", "binaries",
                         "zipfiles", "datas"):
                setattr(self, name, [])

    ns = {
        "__file__": os.path.join(ROOT, "macronaut.spec"),
        "DISTPATH": str(tmp_path / "dist"),
        "HOMEPATH": str(tmp_path),
        "SPEC": os.path.join(ROOT, "macronaut.spec"),
        "specnm": "macronaut",
        "SPECPATH": ROOT,
        "WARNFILE": str(tmp_path / "warn.txt"),
        "workpath": str(tmp_path / "build"),
        "Analysis": _Stub, "PYZ": _Stub, "EXE": _Stub, "COLLECT": _Stub,
        "BUNDLE": _Stub, "MERGE": _Stub, "Tree": _Stub, "Splash": _Stub,
        "TOC": _Stub,
    }
    exec(compile(_read("macronaut.spec"), "macronaut.spec", "exec"), ns)
    return ns


def test_spec_executes_against_pyinstallers_real_globals(tmp_path):
    _exec_spec(tmp_path)

    # The version resource must have been generated from version.py.
    import version
    res = tmp_path / "build" / "version_info.txt"
    assert res.exists(), "the Windows version resource was not written"
    assert version.__version__ in res.read_text(encoding="utf-8")


def test_exe_receives_the_version_resource_under_the_key_pyinstaller_reads():
    """`EXE(version=...)`, not `version_file=...`.

    `--version-file` is the command-line flag; the spec API keyword is `version`.
    EXE() takes **kwargs and ignores anything it does not recognise, so the wrong
    key is accepted in silence and produces an .exe with no version resource --
    file properties read 0.0.0.0 and nothing anywhere reports an error. That is
    only ever caught by inspecting a built binary, so pin it here instead.
    """
    spec = _read("macronaut.spec")
    # An actual keyword argument, not the comment that explains why it is wrong.
    assert not re.search(r"^\s*version_file=", spec, re.MULTILINE), (
        "EXE(version_file=...) is silently ignored by PyInstaller; use version=")
    assert re.search(r"^\s*version=str\(_version_res\),", spec, re.MULTILINE)


def test_exe_kwargs_carry_a_real_version_resource_path(tmp_path):
    """Belt and braces: check the value the spec actually hands to EXE."""
    ns = _exec_spec(tmp_path)
    exe = ns["exe"]
    assert hasattr(exe, "version"), "EXE got no `version` kwarg at all"
    assert os.path.exists(exe.version), \
        f"EXE(version=) points at a file that was never written: {exe.version}"


def test_selftest_is_bundled(tmp_path):
    """--selftest is the only automated check that can see a frozen build.

    It is imported lazily inside main(), so if it ever stops being collected the
    flag fails at runtime in the .exe and nowhere else.
    """
    spec = _read("macronaut.spec")
    assert '"selftest"' in spec or "'selftest'" in spec
    main = _read("main.py")
    assert "--selftest" in main and "import selftest" in main


# ── release tooling ───────────────────────────────────────────────────────────
def _probe_launch():
    import importlib.util
    path = os.path.join(ROOT, "tools", "probe_launch.py")
    spec = importlib.util.spec_from_file_location("probe_launch", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_probe_cleanup_takes_only_the_sessions_it_killed(tmp_path, monkeypatch):
    """probe_launch deletes files, so pin exactly which ones.

    It kills the app, which crashreport correctly records as an abnormal exit —
    so without cleanup every probe run files a phantom `silent` crash, and this
    tool runs before every release. The cleanup is therefore necessary, but it
    is also a delete in the directory holding real crash evidence. Two things
    must never be touched: a harvested crash-*.json (possibly a real user crash,
    possibly harvested by this very launch) and another instance's session.
    """
    import sys
    sys.path.insert(0, ROOT)
    import crashreport

    probe = _probe_launch()
    monkeypatch.setattr(crashreport, "crash_dir", lambda *a, **k: tmp_path)

    ours = ["session-4242-1785800000000.json", "session-4242-1785800000000.log",
            "session-4242-1785800000000.native", "session-99-1785800000001.json"]
    sacred = ["crash-1785757646712.json",        # a harvested report
              "session-777-1785800000002.json"]  # another live instance
    for name in ours + sacred:
        (tmp_path / name).write_text("x", encoding="utf-8")

    assert probe.clear_session_files({4242, 99}) == len(ours)
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(sacred)


def test_probe_kills_the_child_not_just_the_bootloader():
    """A onefile build is two processes and the app is the child.

    proc.kill() alone left it running — window, tray icon and a global keyboard
    hook — while the probe printed OK and exited. Observed for real: a probed
    build still alive 171 s after the tool had finished.
    """
    src = _read(os.path.join("tools", "probe_launch.py"))
    assert "def terminate_family" in src, "the family kill was removed"
    assert "terminate_family(proc, family)" in src, \
        "terminate_family is defined but never called"

    # The ordering hazard: membership must be captured while everything is
    # still running. Once the bootloader exits, the parent/child link
    # process_family() walks is gone and the survivor cannot be found — so the
    # refresh has to come BEFORE the "did it exit?" check, not after.
    body = src[src.index("def probe("):]
    refresh = body.index("family = process_family(proc.pid)")
    exited = body.index("if proc.poll() is not None:")
    assert refresh < exited, \
        "family is refreshed after the exit check — an orphan would be unfindable"


# ── licensing ─────────────────────────────────────────────────────────────────


def test_the_public_key_is_baked_in_and_is_not_a_placeholder():
    """A build shipped with the all-zero placeholder would reject every real
    key, and would do it silently — "invalid key" looks identical whether the
    key is wrong or the constant is."""
    import licensing
    assert len(licensing.PUBLIC_KEY_HEX) == 64
    bytes.fromhex(licensing.PUBLIC_KEY_HEX)          # raises if not hex
    assert set(licensing.PUBLIC_KEY_HEX) != {"0"}


def test_no_module_uses_a_name_it_never_defines():
    """⚠ A missing import in a branch that rarely runs is invisible until it runs.

    Found on 3 September 2026 in this suite's own
    `tests/test_input_backends.py`: it called `pytest.skip(...)` without
    importing pytest. The line sat behind a guard that only fires on a 32-bit
    interpreter, so every run on x64 was green and the `NameError` waited.

    That shape is worse in the app than in a test. The branches that rarely run
    are the error paths — a fallback, an `except`, a "your display is being
    reconfigured" case — which is precisely where a crash is least welcome and
    least likely to have been exercised. `python -B -c "import x"` catches a
    truncated file, and this catches the other thing an import cannot see:
    a name that is only looked up when something goes wrong.

    Deliberately a name check and not a linter. It parses rather than imports,
    so it costs nothing and needs no dependency, and it makes no judgement about
    style — only that every name a module loads is one it could actually have.
    """
    import ast
    import builtins

    targets = (sorted(glob.glob(os.path.join(ROOT, "*.py")))
               + sorted(glob.glob(os.path.join(ROOT, "tests", "*.py")))
               + sorted(glob.glob(os.path.join(ROOT, "tools", "*.py"))))
    assert targets, "found no modules to check"

    ALWAYS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__spec__"}
    offenders = {}

    for path in targets:
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        bound = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    bound.add((a.asname or a.name).split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                # ⚠ `from x import *` would make this check meaningless, so it
                # is refused rather than silently tolerated.
                assert not any(a.name == "*" for a in node.names), (
                    f"{os.path.basename(path)} uses a star import; this "
                    "check cannot see what it brings in")
                for a in node.names:
                    bound.add(a.asname or a.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                   ast.ClassDef)):
                bound.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx,
                                                           (ast.Store, ast.Del)):
                bound.add(node.id)
            elif isinstance(node, ast.arg):
                bound.add(node.arg)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                bound.add(node.name)
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                bound.update(node.names)

        used = {n.id for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        missing = sorted(used - bound - ALWAYS)
        if missing:
            offenders[os.path.basename(path)] = missing

    assert not offenders, "names used but never defined:\n" + "\n".join(
        f"  {name}: {', '.join(miss)}" for name, miss in offenders.items())


def test_no_setting_exists_that_nothing_reads():
    """⚠ A knob that does nothing is the worst kind of dead code.

    Eight settings were removed on 4 September 2026 — residue from the pre-2.0
    Basic tab that nothing had read since. They were harmless in themselves,
    but they were written into every user's `settings.json`, so somebody
    reading their own file found `use_image_trigger` and went looking for a
    feature that does not exist.

    The allowlist here is deliberately **empty**. Every one of the remaining
    fields is read somewhere, so any new one that is not is a setting somebody
    added and never wired up — which is the same defect as `release.py
    --mandatory` writing a flag no client acted on (retired 12 September 2026),
    and as `ocr.warmup` returning True having done nothing. All three were
    found by hand; this is so the next one is not.

    Counts a string reference too, because `settings.set("name", …)` and
    `getattr` are both real ways to reach one.
    """
    import ast
    import collections
    import dataclasses
    import settings as _settings

    names = [f.name for f in dataclasses.fields(_settings.AppSettings)]
    reads = collections.Counter()
    for path in (sorted(glob.glob(os.path.join(ROOT, "*.py")))
                 + sorted(glob.glob(os.path.join(ROOT, "tools", "*.py")))):
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                reads[node.attr] += 1
        for name in names:
            reads[name] += len(re.findall(r'["\']%s["\']' % re.escape(name), src))

    unread = sorted(n for n in names if reads[n] == 0)
    assert not unread, (
        "settings nothing in the app ever reads:\n"
        + "\n".join(f"  {n}" for n in unread)
        + "\nEither wire it up or remove it. Removing is safe — `_load` ignores "
          "an unknown key — but renaming is not.")


def test_both_quit_paths_disarm_the_crash_reporter_and_save_state():
    """⚠ Read with `ast`, because these two functions cannot be *called*.

    `closeEvent` and `_quit_app` both end in `os._exit(0)` — deliberately, so
    no orphaned Macronaut keeps a global hotkey hook. Invoking either from a
    test would end the pytest run silently with status 0, which is why this
    suite's own rule is never to `close()` a MainWindow. So the only way to
    check what they do is to read them.

    Three calls have to be in both, and each is a wiring that nothing else
    protects:

    * `crashreport.disarm()` — the dead-man's switch is armed for the whole
      session and a clean exit is recorded here **or never**, because
      `os._exit` runs no `atexit`. Miss it and every ordinary shutdown is
      reported as a crash on the next launch: the user is asked to send a
      report for something that did not happen, and the real crash rate
      becomes unreadable.
    * `_shutdown()` — which is what reaches `flow_exec.release_all_held()`,
      so a key or a mouse button held by a running flow does not outlive the
      app.
    * `_save_state()` — the window geometry, the settings and the face.

    ⚠ Parsed rather than grepped, for the reason `test_flow`'s clock guard is:
    the comments around these lines name the very calls being looked for, so a
    textual search passes on the explanation after the code is gone.
    """
    import ast

    with open(os.path.join(ROOT, "main.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())

    wanted = {"crashreport.disarm", "self._shutdown", "self._save_state"}
    seen = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in ("closeEvent",
                                                               "_quit_app"):
            seen[node.name] = {ast.unparse(n.func) for n in ast.walk(node)
                               if isinstance(n, ast.Call)}

    assert set(seen) == {"closeEvent", "_quit_app"}, (
        f"a quit path is missing from main.py: found {sorted(seen)}")
    for name, calls in seen.items():
        missing = sorted(wanted - calls)
        assert not missing, f"{name} no longer calls: {', '.join(missing)}"
        # If a quit path ever stops ending the process, this test is measuring
        # something else and should be revisited rather than trusted.
        assert "_os._exit" in calls, (
            f"{name} no longer ends the process; the reasoning above assumed "
            "it does, and the whole point of disarming here was that "
            "os._exit runs no atexit")


def test_startup_harvests_before_it_arms_the_crash_reporter():
    """⚠ Three calls in a `try/except: pass`, and their order is load-bearing.

    `main()` does `harvest()`, then `install()`, then `install_qt_handler()`.
    Nothing fails if any of them goes: crash capture simply stops existing, and
    the only symptom is reports that never arrive — which is indistinguishable
    from a version that did not crash. This project has already spent a release
    reconstructing three bugs from the sentence "a ton of crashes" because there
    was no capture; that is what it costs.

    ⚠ The order is the subtle half. Harvesting is what turns *last* session's
    abandoned session file into a report. Arming immediately writes a *new* one.
    Arm first and the fresh file is there to be harvested, so a clean start
    reports itself as a crash — or last session's real crash is overwritten
    before anyone reads it. Both directions are wrong and neither raises.

    `install_qt_handler` is separate on purpose: a `qFatal` calls `abort()` in
    C, so no Python exception is ever raised, and that handler seeing the text
    on the way past is the only record that exists. It is how 2.0.8's Stop crash
    was identified at all.
    """
    import ast

    with open(os.path.join(ROOT, "main.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())

    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
    assert fn is not None, "main() has moved"

    order = [(n.lineno, ast.unparse(n.func)) for n in ast.walk(fn)
             if isinstance(n, ast.Call)
             and ast.unparse(n.func).startswith("crashreport.")]
    names = [c for _, c in sorted(order)]

    for wanted in ("crashreport.harvest", "crashreport.install",
                   "crashreport.install_qt_handler"):
        assert wanted in names, f"main() no longer calls {wanted}()"

    assert names.index("crashreport.harvest") < names.index("crashreport.install"), (
        "main() arms the crash reporter before harvesting. The session file "
        "written by arming is then there to be harvested, so a clean start "
        "reports itself as a crash — or last session's real one is lost.")


def test_the_notices_name_every_dependency_that_is_actually_bundled():
    """Every installed dependency has to appear in THIRD-PARTY-NOTICES.md.

    ⚠ The document went out incomplete and nothing noticed. The release
    attached to v2.3.4 — the one people are downloading — omits numpy,
    PyMsgBox, PyRect and typing-extensions entirely, and all four are inside
    the .exe. The existing test above checks three strings, which is why: a
    document can satisfy "mentions PySide6" while missing a quarter of what it
    is supposed to list.

    ⚠ **The four that were missing are all TRANSITIVE**, pulled in by
    opencv-python and pyautogui rather than named in requirements.txt. Checking
    the direct dependencies would not have caught any of them, which is why
    this walks the closure instead.

    ⚠ Only packages that are actually **installed** count. The closure of
    pynput and pyautogui names pyobjc, evdev and python-xlib — the macOS and
    Linux backends — which are never installed here and never bundled. Asking
    the environment what it has avoids listing platforms this app does not run
    on.

    ⚠ A `foo.*` entry in the document covers that prefix. The six winrt split
    projection packages are one project at one version under one licence, and
    the document says so in one row rather than six. That is a real entry, not
    an omission.
    """
    import importlib.metadata as md

    # The direct dependencies, as requirements.txt names them. pyinstaller is
    # deliberately absent: it builds the .exe, it does not go inside it.
    direct = ["PySide6", "pynput", "interception-python", "pyautogui",
              "opencv-python", "Pillow", "pywin32",
              "winrt-Windows.Media.Ocr", "winrt-Windows.Graphics.Imaging",
              "winrt-Windows.Storage.Streams", "winrt-Windows.Globalization",
              "winrt-Windows.Foundation", "winrt-Windows.Foundation.Collections"]

    def norm(name):
        return re.sub(r"[-_.]+", "-", name.strip()).lower()

    def is_installed(name):
        try:
            md.version(name)
            return True
        except Exception:
            return False

    if not any(is_installed(d) for d in direct):
        pytest.skip("none of the dependencies are installed in this environment")

    seen, queue = set(), list(direct)
    while queue:
        name = queue.pop()
        key = norm(name)
        if key in seen or not is_installed(name):
            continue
        seen.add(key)
        for req in (md.requires(name) or []):
            # `extra == "..."` markers are optional extras nobody installs.
            if "extra ==" in req:
                continue
            dep = re.split(r"[<>=!;\[ ~]", req.strip())[0]
            if dep and norm(dep) not in seen:
                queue.append(dep)

    text = _read("THIRD-PARTY-NOTICES.md")
    flat = norm(text).replace("-", "")
    # Prefixes the document covers with a wildcard, e.g. "winrt-Windows.*".
    wildcards = [norm(p).replace("-", "")
                 for p in re.findall(r"([A-Za-z0-9_.-]+)\.\*", text)]

    missing = sorted(
        p for p in seen
        if p.replace("-", "") not in flat
        and not any(p.replace("-", "").startswith(w) for w in wildcards))

    assert not missing, (
        "THIRD-PARTY-NOTICES.md does not name these bundled dependencies:\n  "
        + "\n  ".join(missing)
        + "\n\nThey ship inside the .exe, so they belong in the notices. "
          "Regenerate with the command at the bottom of that file — and add "
          "them to the command's package list too, which is how the last four "
          "went missing from both at once.")
# ── The one check that runs after a release is already public ────────────────

def _url_check(monkeypatch, capsys, behaviour):
    """Run `_prove_the_published_url_resolves` with urlopen replaced.

    ⚠ Patches `urllib.request.urlopen`, which conftest.py has already replaced
    with a blocker for the whole session. Patching it again here is the
    documented way to ask for a specific network behaviour — the stub wins for
    the duration of the test and the blocker comes back after.
    """
    import urllib.request
    import release

    monkeypatch.setattr(urllib.request, "urlopen", behaviour)
    release._prove_the_published_url_resolves("2.3.4")
    return capsys.readouterr().out


def test_a_reachable_download_url_is_reported_as_fine(monkeypatch, capsys):
    class _Resp:
        status = 200

        def close(self):
            pass

    out = _url_check(monkeypatch, capsys, lambda *a, **k: _Resp())
    assert "still resolves" in out
    assert "⚠" not in out


def test_a_download_url_that_404s_is_shouted_about(monkeypatch, capsys):
    """⚠ The case this exists for. `UPDATE_REPO` is a name this repository no
    longer has; it works only through GitHub's rename redirect, it is baked
    into every shipped build, and it dies the moment anything takes that name.
    Then every install stops updating and nothing else would say so — the
    release itself published perfectly.
    """
    import urllib.error

    def _gone(*a, **k):
        raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)

    out = _url_check(monkeypatch, capsys, _gone)
    assert "404" in out
    assert "silently stopped updating" in out


def test_being_offline_is_not_reported_as_a_broken_release(monkeypatch, capsys):
    """⚠ A different failure with a different meaning, and conflating them is
    how a warning gets ignored. No answer from the network says nothing at all
    about the release, so it must not be worded as though it did.
    """
    def _offline(*a, **k):
        raise OSError("getaddrinfo failed")

    out = _url_check(monkeypatch, capsys, _offline)
    assert "connection" in out.lower()
    assert "silently stopped updating" not in out


def test_the_publish_step_actually_calls_the_url_check():
    """A check nothing calls is not a check.

    ⚠ Reads the source rather than running `publish()`, which would need a
    built .exe, a manifest and a live `gh`. What matters is that the call sits
    in the publish path at all: the function was written first and wired in
    second, and that is exactly the order in which a wiring step gets skipped.
    """
    src = _read("release.py")
    assert "_prove_the_published_url_resolves(ver)" in src, (
        "release.py defines the post-publish URL check but never calls it")
    # It has to run after the release exists, or it is checking for something
    # that is not there yet and will always warn.
    assert src.index("gh\", \"release\", \"create") < src.index(
        "_prove_the_published_url_resolves(ver)"), (
        "the URL check runs before the release is created, so it can only ever "
        "report a 404")

