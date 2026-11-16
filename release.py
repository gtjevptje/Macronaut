"""Cut a Macronaut release: bump the version, build the .exe, write the manifest.

The whole point is that the version in `version.py`, the version baked into the
built .exe, and the version the updater advertises are produced by one command
and cannot disagree.

    python release.py --bump patch --notes "Fixed the thing"
    python release.py --publish            # after checking dist/ looks right

Typical flow:

    1. Write release-notes/RELEASE-NOTES-<new version>.md, then
       python release.py --bump patch
         (the notes file is found by name — --notes / --notes-file override it)
         → bumps version.py, builds dist/Macronaut.exe, writes dist/update.json
         (add --sign once a code-signing certificate exists; it must run before
         the manifest, because signing changes the bytes the hash describes)
    2. Test dist/Macronaut.exe by hand, and rehearse the swap:
         python tools/rehearse_swap.py --new dist/Macronaut.exe --old <previous>
    3. python release.py --publish
         → git tag + `gh release create`, uploading the .exe and the manifest to
           the PUBLIC releases repo (version.UPDATE_REPO).

`--publish` needs the GitHub CLI (`gh auth login` once). If you'd rather upload
by hand, run steps 1-2 and drag both files onto the GitHub release page — the
manifest must be attached as `update.json` or the updater won't find it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys as _sys

# ⚠ This script prints ⚠ and — , and a Windows console (or anything that pipes
# it) hands Python a cp1252 stdout, where those raise UnicodeEncodeError. That
# killed a publish *between* the pre-flight checks and `gh release create`: the
# release was never created, and the traceback pointed at a print statement
# rather than at anything to do with releasing. Reconfigure rather than strip
# the characters — the notes are the user's text and may hold anything at all.
for _stream in (_sys.stdout, _sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# The app's modules live in src/.
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import version as _v  # noqa: E402  (after sys.path setup)

DIST = ROOT / "dist"
EXE = DIST / "Macronaut.exe"
MANIFEST = DIST / "update.json"


def setup_exe(ver: str) -> Path:
    """The installer asset for `ver`. ⚠ The name is `updater.SETUP_PREFIX` — the
    updater decides from the filename alone whether it is holding an installer
    or a portable build, so a release that spells it differently would be
    downloaded and then swapped over the .exe as if it were one."""
    import updater
    return DIST / f"{updater.SETUP_PREFIX}{ver}.exe"
VERSION_FILE = SRC / "version.py"


def _run(cmd: list, **kw) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    return subprocess.run(cmd, cwd=ROOT, **kw)


# ── Version bumping ───────────────────────────────────────────────────────────
def bump(kind: str) -> str:
    """Raise the version in version.py. `kind` is major|minor|patch or a literal
    version string. -> the new version."""
    current = _v.__version__
    if kind in ("major", "minor", "patch"):
        major, minor, patch = _v.as_tuple(current)
        if kind == "major":
            major, minor, patch = major + 1, 0, 0
        elif kind == "minor":
            minor, patch = minor + 1, 0
        else:
            patch += 1
        new = f"{major}.{minor}.{patch}"
    else:
        new = kind.lstrip("vV")
        if _v.parse(new) is None:
            raise SystemExit(f"error: {kind!r} is not a usable version")
        if not _v.is_newer(new, current):
            raise SystemExit(
                f"error: {new} is not newer than the current {current} — "
                "users on the current build would never be offered it")

    text = VERSION_FILE.read_text(encoding="utf-8")
    patched, n = re.subn(r'^__version__\s*=\s*".*?"',
                         f'__version__ = "{new}"', text,
                         count=1, flags=re.MULTILINE)
    if n != 1:
        raise SystemExit("error: could not find __version__ in version.py")
    VERSION_FILE.write_text(patched, encoding="utf-8")
    print(f"  version.py: {current} -> {new}")
    return new


# ── Build ─────────────────────────────────────────────────────────────────────
def build() -> Path:
    """Run PyInstaller. -> the built .exe."""
    print("Building (this takes a couple of minutes)…")
    for stale in (DIST / "Macronaut.exe",):
        if stale.exists():
            stale.unlink()
    r = _run([sys.executable, "-m", "PyInstaller", "macronaut.spec", "--noconfirm"])
    if r.returncode != 0:
        raise SystemExit("error: PyInstaller failed")
    if not EXE.exists():
        raise SystemExit(f"error: expected {EXE} but it wasn't produced")
    print(f"  built {EXE} ({EXE.stat().st_size:,} bytes)")
    return EXE


def build_installer(ver: str) -> Path:
    """Build the folder build and wrap it in the Inno Setup installer.

    ⚠ Second, never instead. The portable one-file .exe above is what every
    copy published before 2.3.5 updates itself with, and those copies read the
    manifest forever. This is the download the website leads with, because a
    one-file build unpacks itself into a temp folder on every launch and that is
    what antivirus heuristics react to — see tools/build_installer.py.
    """
    print("Building the installer…")
    r = _run([sys.executable, "tools/build_installer.py"])
    if r.returncode != 0:
        raise SystemExit("error: the installer build failed")
    out = setup_exe(ver)
    if not out.exists():
        raise SystemExit(f"error: expected {out} but it wasn't produced")
    print(f"  built {out} ({out.stat().st_size:,} bytes)")
    return out


# ── Code signing ──────────────────────────────────────────────────────────────
# The certificate is referenced by thumbprint, never by .pfx-plus-password: the
# key then stays in the Windows certificate store or on the hardware token (which
# publicly-trusted code-signing certificates have required since 2023), and no
# secret ever appears in a command line, a script, or this repository.
SIGN_THUMBPRINT_ENV = "MACRONAUT_SIGN_SHA1"
TIMESTAMP_URL = "http://timestamp.digicert.com"


def find_signtool() -> Path | None:
    """Locate signtool.exe. It ships with the Windows SDK and is not on PATH."""
    found = shutil.which("signtool")
    if found:
        return Path(found)
    roots = [Path(r"C:\Program Files (x86)\Windows Kits\10\bin"),
             Path(r"C:\Program Files\Windows Kits\10\bin")]
    best = None
    for root in roots:
        if not root.exists():
            continue
        for cand in root.glob("*/x64/signtool.exe"):
            # Sort by SDK version directory so the newest wins.
            if best is None or cand.parent.parent.name > best.parent.parent.name:
                best = cand
    return best


def sign(exe: Path) -> None:
    """Authenticode-sign the build in place. Raises SystemExit on failure.

    Must run BEFORE the manifest is written: signing rewrites the file, so a
    manifest made first would carry the hash of the unsigned bytes and every
    download would fail its integrity check. main() enforces that ordering, and
    publish() re-checks the hash as a backstop.
    """
    import os

    thumb = (os.environ.get(SIGN_THUMBPRINT_ENV) or "").replace(" ", "").strip()
    if not thumb:
        raise SystemExit(
            f"error: --sign needs the certificate thumbprint in ${SIGN_THUMBPRINT_ENV}.\n"
            "  Find it with: certutil -store My\n"
            "  Then: set MACRONAUT_SIGN_SHA1=<thumbprint>")
    tool = find_signtool()
    if tool is None:
        raise SystemExit(
            "error: signtool.exe not found — install the Windows SDK "
            "(Signing Tools for Desktop Apps).")

    print(f"Signing with {tool}…")
    # A timestamp is what keeps already-published builds valid after the
    # certificate expires; without it every release dies with the cert.
    r = _run([str(tool), "sign", "/sha1", thumb, "/fd", "SHA256",
              "/tr", TIMESTAMP_URL, "/td", "SHA256", "/v", str(exe)])
    if r.returncode != 0:
        raise SystemExit("error: signtool failed")

    sys.path.insert(0, str(SRC))
    import updater
    trusted, reason = updater.signature_status(exe)
    if not trusted:
        raise SystemExit(f"error: signed, but the result does not verify ({reason})")
    print(f"  signature verifies: {reason}")


# ── Manifest ──────────────────────────────────────────────────────────────────
def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def write_manifest(ver: str, notes: str = "") -> Path:
    # ⚠ Order matters, and it is checked before the build on purpose.
    #
    # A missing .exe is a two-minute fix. A placeholder UPDATE_REPO is
    # unrecoverable once anything ships — see the block below. Reporting the
    # cheap problem first meant the expensive one stayed hidden behind it.
    #
    # ⚠⚠ It also had a second cost, which is how this was found: the test that
    # guards the placeholder could only reach it on a machine that had already
    # built. On a clean checkout — every CI run, every contributor's first
    # clone — `write_manifest` died on the missing .exe instead, and the test
    # failed. The public mirror's CI had been red on exactly this since
    # 3 September 2026, with a failing badge at the top of the README, on a
    # project whose whole open-source argument is that the tests are
    # inspectable. Verified against the run log, not inferred.
    #
    # ⚠⚠ Fatal, not a warning, and this is the one place in the file where that
    # distinction is unarguable. An installed build asks the URL baked into its
    # own .exe, forever — UPDATE_REPO is effectively permanent the moment
    # anything ships. Publishing with the placeholder in it produces binaries
    # that point at a repository which does not exist and can never be told
    # otherwise, because the only channel for telling them is the one that is
    # broken. There is no recovery and no message you can send.
    #
    # It used to print a warning and write the manifest anyway. Eight release
    # pages went out empty behind a warning in this same file, so the evidence
    # that warnings are not a control is already in the repository.
    if "OWNER/" in _v.UPDATE_REPO:
        raise SystemExit(
            "error: version.UPDATE_REPO is still the placeholder "
            f"({_v.UPDATE_REPO!r}).\n"
            "Every build published with it would ask a repository that does "
            "not exist for its updates, permanently and unfixably. Set it to "
            "the real owner/repo before releasing.")
    if not EXE.exists():
        raise SystemExit(f"error: {EXE} not found — build first")
    data = {
        "version": ver,
        "url": (f"https://github.com/{_v.UPDATE_REPO}/releases/download/"
                f"v{ver}/Macronaut.exe"),
        "sha256": sha256(EXE),
        "size": EXE.stat().st_size,
        "notes": notes,
        "published": time.strftime("%Y-%m-%d"),
    }
    # ⚠ Added only when the installer was actually built, and read only by
    # clients that are themselves folder builds. An older client ignores the
    # key — `updater.parse_manifest` has ignored unknown keys since 2.0, which
    # is the property that makes adding one safe at all (see the `mandatory`
    # note in updater.py). Never make this block mandatory: the copies that
    # would break are the ones that can only be fixed through this manifest.
    setup = setup_exe(ver)
    if setup.exists():
        data["installer"] = {
            "url": (f"https://github.com/{_v.UPDATE_REPO}/releases/download/"
                    f"v{ver}/{setup.name}"),
            "sha256": sha256(setup),
            "size": setup.stat().st_size,
        }
    else:
        print("  ⚠ no installer in dist/ — the manifest will offer the "
              "portable build only. Build it with "
              "`python tools/build_installer.py`.")
    MANIFEST.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"  wrote {MANIFEST}")
    print(f"    sha256 {data['sha256']}")
    print(f"    url    {data['url']}")
    return MANIFEST


# ── Publish ───────────────────────────────────────────────────────────────────
# Every version's notes live in one folder, so the repository's front page is
# the README and not a list of twenty files that all start the same way.
NOTES_DIR = ROOT / "release-notes"


def notes_file_for(ver: str) -> Path:
    """The conventional notes file for a version:
    `release-notes/RELEASE-NOTES-<ver>.md`.

    ⚠ This exists because the warning below was not enough. Eight of the first
    twenty-six releases went out with a body of `Macronaut 2.0.14` and nothing
    else — the page a person lands on to download, saying nothing about what
    changed. `publish()` printed a warning every time and the release
    succeeded anyway, which is what a warning buys you.

    So the file is now *found* rather than passed. Every release since 2.1.1
    has written one under this name; looking for it costs nothing and removes
    the only step in the flow that had to be remembered.
    """
    return NOTES_DIR / f"RELEASE-NOTES-{ver}.md"


def publish(ver: str, notes: str = "") -> None:
    if shutil.which("gh") is None:
        raise SystemExit(
            "error: the GitHub CLI (gh) isn't installed.\n"
            "Install it, run `gh auth login`, or upload dist/Macronaut.exe and "
            "dist/update.json to the release page by hand.")
    if not (EXE.exists() and MANIFEST.exists()):
        raise SystemExit("error: build and write the manifest first")

    manifest_ver = json.loads(MANIFEST.read_text(encoding="utf-8"))["version"]
    if manifest_ver != ver:
        raise SystemExit(
            f"error: dist/update.json says {manifest_ver} but you're publishing "
            f"{ver} — rebuild so they match")
    # Guard against shipping a manifest whose hash doesn't describe this .exe.
    expected = json.loads(MANIFEST.read_text(encoding="utf-8"))["sha256"]
    if sha256(EXE) != expected:
        raise SystemExit(
            "error: dist/update.json's sha256 doesn't match dist/Macronaut.exe. "
            "The .exe changed after the manifest was written — rebuild.")
    # The same guard for the installer, because it fails the same way and worse:
    # a client that downloads it and finds the wrong hash refuses the update and
    # says the download is corrupt, which is indistinguishable from a bad
    # connection and will be reported as one.
    block = json.loads(MANIFEST.read_text(encoding="utf-8")).get("installer")
    setup = setup_exe(ver)
    if block:
        if not setup.exists():
            raise SystemExit(
                f"error: dist/update.json advertises an installer but {setup.name} "
                "is not in dist/ — build it, or rewrite the manifest.")
        if sha256(setup) != block.get("sha256"):
            raise SystemExit(
                f"error: dist/update.json's installer sha256 doesn't match "
                f"{setup.name}. Rebuild.")

    # Say it out loud at the one moment it matters. An unsigned release makes
    # SmartScreen warn every new user, and that reputation resets with each
    # unsigned build — so it compounds for an app that updates often.
    sys.path.insert(0, str(SRC))
    import updater
    trusted, reason = updater.signature_status(EXE)
    if not trusted:
        print(f"  ⚠ publishing an UNSIGNED build ({reason}) — SmartScreen will "
              "warn every new user.")

    # The terms travel with the download, so somebody who has only the release
    # page still has the licence in front of them.
    legal = [p for p in (ROOT / "LICENSE", ROOT / "THIRD-PARTY-NOTICES.md")
             if p.exists()]
    # Also fatal. Macronaut is GPL-3.0-or-later, and conveying the work means
    # conveying the licence with it — that is §4, not a preference. The .exe
    # bundles both files and `selftest._check_legal` proves it, so by the time
    # a build reaches here they exist; a missing one means something is wrong
    # upstream rather than that this release should go out lighter.
    if len(legal) < 2:
        raise SystemExit(
            "error: LICENSE and/or THIRD-PARTY-NOTICES.md are missing from the "
            "repository root, so the release would carry neither.\n"
            "A GPL release has to convey its licence. Restore them before "
            "publishing.")

    # ⚠ Fall back to the manifest's notes, not to the version number.
    #
    # `--publish` is a separate invocation from `--manifest`, so it is entirely
    # natural to pass --notes-file to one and forget it on the other — which is
    # exactly what happened publishing 2.3.0. The old fallback made that
    # succeed quietly: the in-app update dialog showed the full notes while the
    # GitHub release page, which is what a human actually reads, said
    # "Macronaut 2.3.0" and nothing else. Nothing failed and nothing warned.
    #
    # The manifest has already been checked to describe this exact .exe two
    # guards above, so its notes are the right notes by construction.
    if not notes:
        notes = json.loads(MANIFEST.read_text(encoding="utf-8")).get("notes", "")
        if notes:
            print("  · no --notes given; using the notes already in "
                  "dist/update.json")
    # Third source, and the one that needs no remembering: the conventional
    # file. See notes_file_for() for why a warning was not sufficient.
    if not notes:
        conventional = notes_file_for(ver)
        if conventional.is_file():
            notes = conventional.read_text(encoding="utf-8")
            print(f"  · no --notes given; using {conventional.name}")
    if not notes:
        print("  ⚠ publishing with NO release notes. The release page will be "
              f"empty — write release-notes/{notes_file_for(ver).name}, pass --notes-file, "
              "or fix it afterwards with\n"
              f"      gh release edit v{ver} --repo {_v.UPDATE_REPO} "
              f"--notes-file release-notes/{notes_file_for(ver).name}")

    tag = f"v{ver}"
    setup_assets = [str(setup)] if setup.exists() else []
    r = _run(["gh", "release", "create", tag,
              str(EXE), *setup_assets, str(MANIFEST), *[str(p) for p in legal],
              "--repo", _v.UPDATE_REPO,
              "--title", f"Macronaut {ver}",
              "--notes", notes or f"Macronaut {ver}"])
    if r.returncode != 0:
        raise SystemExit("error: gh release create failed")

    _prove_the_published_url_resolves(ver)

    print(f"\nPublished {tag}. Existing installs will find it within 6 hours, "
          "or immediately via Settings → Updates → Check now.")


def _prove_the_published_url_resolves(ver: str) -> None:
    """Fetch the URL the manifest hands to every client, right after publishing.

    ⚠ **`UPDATE_REPO` is `gtjevptje/macronaut-releases`, a name this repository
    no longer has.** It resolves only through GitHub's rename redirect, and it
    is baked into every manifest and every shipped build — permanent. The
    redirect dies the instant any repository takes that
    name, at which point every install on earth silently stops updating and
    nothing here would report it: `gh release create` would still have
    succeeded, and the manifest would still look perfect.

    This is the one moment the problem is cheap: the release exists, so the URL
    should now resolve, and if it does not the person who just published is
    still standing here. It also catches an upload that reported success but
    attached nothing, and a typo in UPDATE_REPO on a first release.

    ⚠ A HEAD request, and redirects followed on purpose — the redirect is the
    thing under test. Failure prints rather than raises: the release is already
    public by this point, so aborting would leave a published release and an
    angry traceback, which helps nobody. The job here is to say so loudly.

    ⚠ The status code is the whole test, and an earlier draft of this got that
    wrong. It also checked whether the final URL still said `macronaut-releases`,
    on the theory that a name-taken redirect would stop happening — but GitHub
    hands a release asset off to a signed `release-assets.githubusercontent.com`
    blob URL that names no repository at all, so that check could never fire.
    If a repository did take the name, this path would simply 404, which the
    status check already catches. The final URL is not printed for the same
    reason: it is a single-use signed link with a JWT in it, and dumping it into
    a release log is noise at best.
    """
    import urllib.error
    import urllib.request

    url = f"https://github.com/{_v.UPDATE_REPO}/releases/download/v{ver}/Macronaut.exe"
    req = urllib.request.Request(url, method="HEAD")
    try:
        urllib.request.urlopen(req, timeout=30).close()
    except urllib.error.HTTPError as exc:
        # ⚠ The server answered, and said no. This is the interesting failure —
        # and it has to be caught separately, because `urlopen` RAISES on 4xx
        # rather than returning a response. An earlier draft tested
        # `resp.status != 200` after the call, which can never be true: any
        # non-2xx has already become an exception by then. Two dead branches in
        # one function is enough to write the rule down — a code path that
        # cannot execute reads exactly like one that has never failed.
        print(f"\n⚠ THE PUBLISHED DOWNLOAD URL ANSWERED {exc.code}\n"
              f"    {url}\n"
              f"  Either the upload did not attach what it claimed, or "
              f"`{_v.UPDATE_REPO}` has stopped redirecting because something "
              f"took that name — in which case every install has silently "
              f"stopped updating.")
        return
    except (urllib.error.URLError, OSError) as exc:
        # No answer at all: offline, proxied, DNS. Says nothing about the
        # release, so it must not be reported as if it did.
        print(f"\n⚠ COULD NOT REACH GITHUB TO CHECK THE DOWNLOAD URL\n"
              f"    {url}\n"
              f"    {exc}\n"
              f"  This is a problem with this machine's connection, not "
              f"necessarily with the release. Check the address by hand.")
        return

    print(f"  ✓ the update URL every install uses still resolves:\n    {url}")


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bump", metavar="KIND",
                    help="major | minor | patch | an explicit version")
    ap.add_argument("--build", action="store_true", help="run PyInstaller")
    ap.add_argument("--sign", action="store_true",
                    help=f"Authenticode-sign the .exe (cert thumbprint in "
                         f"${SIGN_THUMBPRINT_ENV})")
    ap.add_argument("--installer", action="store_true",
                    help="also build the Inno Setup installer (dist/"
                         "Macronaut-Setup-<ver>.exe)")
    ap.add_argument("--manifest", action="store_true",
                    help="write dist/update.json for the current build")
    ap.add_argument("--publish", action="store_true",
                    help="create the GitHub release and upload the assets")
    ap.add_argument("--notes", default="", help="release notes text")
    ap.add_argument("--notes-file", help="read release notes from a file")
    args = ap.parse_args(argv)

    notes = args.notes
    if args.notes_file:
        notes = Path(args.notes_file).read_text(encoding="utf-8")

    # Bare invocation = the common path: bump nothing, build, write manifest.
    # ⚠ `--installer` is an ADDITION ("also build"), so it does not count as
    # having chosen steps. It used to: `--bump patch --installer` re-executes
    # as a bare `--installer`, which then built the installer and nothing else,
    # leaving the previous release's .exe and update.json in dist/ beside a
    # freshly bumped version.py (17 September 2026, cutting 2.3.5).
    if not any((args.bump, args.build, args.manifest, args.publish)):
        args.build = args.installer = args.manifest = True

    ver = _v.__version__
    # ⚠ Resolve the conventional notes file here too, not only in publish().
    # The manifest is what the *in-app* update dialog shows, and it is written
    # by --manifest, which can run in a separate invocation from --publish. If
    # only publish() found the file, the release page would carry the notes
    # while every user's update dialog showed nothing — which is the same bug
    # as the empty release page, shown to the other set of readers.
    if not notes and not args.bump:
        conventional = notes_file_for(ver)
        if conventional.is_file():
            notes = conventional.read_text(encoding="utf-8")
    if args.bump:
        ver = bump(args.bump)
        # Re-exec so the build below picks up the new version rather than the
        # one this process imported at startup.
        rest = [a for a in argv if a not in ("--bump", args.bump)]
        return subprocess.run([sys.executable, __file__] + rest, cwd=ROOT).returncode

    if args.build:
        build()
    if args.installer:
        build_installer(ver)
    # Signing rewrites the .exe, so it has to happen before the hash is taken.
    if args.sign:
        sign(EXE)
    if args.manifest:
        write_manifest(ver, notes)
    if args.publish:
        publish(ver, notes)

    if args.build or args.manifest:
        print(f"\nReady: Macronaut {ver}")
        print("  Test dist/Macronaut.exe, then: python release.py --publish")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
