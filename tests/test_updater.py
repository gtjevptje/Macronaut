"""Headless tests for version.py + updater.py.

Nothing here touches the network or replaces a real file: manifests are built as
dicts, downloads are fed by a fake response object, and the swap logic runs
against temp files with `_self_path` pointed at a fixture.
"""
import hashlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import updater
import version
from updater import UpdateError


# ── version comparison ────────────────────────────────────────────────────────
def test_parse_accepts_v_prefix_and_pads():
    assert version.parse("v2.1")[0] == (2, 1, 0)
    assert version.parse("2.1.0")[0] == (2, 1, 0)


def test_parse_rejects_junk():
    for bad in ("", "abc", "2.x.1", None, "v"):
        assert version.parse(bad) is None


def test_is_newer_basic_ordering():
    assert version.is_newer("2.0.1", "2.0.0")
    assert version.is_newer("2.1.0", "2.0.9")
    assert version.is_newer("3.0.0", "2.9.9")
    assert not version.is_newer("2.0.0", "2.0.0")
    assert not version.is_newer("1.9.9", "2.0.0")


def test_prerelease_sorts_below_final():
    # A beta tester must be offered the finished release as an upgrade.
    assert version.is_newer("2.1.0", "2.1.0-beta.1")
    assert not version.is_newer("2.1.0-beta.1", "2.1.0")


def test_is_newer_rejects_unparseable():
    # An updater must never offer a "newer" version it couldn't parse.
    assert not version.is_newer("garbage", "2.0.0")
    assert not version.is_newer("2.0.1", "garbage")


# ── manifest validation ───────────────────────────────────────────────────────
GOOD_SHA = "a" * 64


def _manifest(**over):
    m = {
        "version": "9.9.9",
        "url": "https://example.com/Macronaut.exe",
        "sha256": GOOD_SHA,
        "size": 123,
        "notes": "notes here",
    }
    m.update(over)
    return m


def test_parse_manifest_happy_path():
    info = updater.parse_manifest(_manifest())
    assert info.version == "9.9.9"
    assert info.size == 123
    assert info.filename == "Macronaut-9.9.9.exe"


@pytest.mark.parametrize("missing", ["version", "url", "sha256"])
def test_parse_manifest_requires_fields(missing):
    m = _manifest()
    del m[missing]
    with pytest.raises(UpdateError):
        updater.parse_manifest(m)


def test_parse_manifest_rejects_http_url():
    # Plain http would let anyone on the path swap the binary. With no code
    # signing, https plus the manifest hash is the whole defence — and the
    # manifest is fetched over the same channel, so an attacker who can serve
    # one can serve both.
    with pytest.raises(UpdateError, match="(?i)https"):
        updater.parse_manifest(_manifest(url="http://example.com/Macronaut.exe"))


def test_parse_manifest_rejects_bad_sha():
    for bad in ("", "abc", "z" * 64, GOOD_SHA[:-1]):
        with pytest.raises(UpdateError):
            updater.parse_manifest(_manifest(sha256=bad))


def test_parse_manifest_rejects_bad_version():
    with pytest.raises(UpdateError):
        updater.parse_manifest(_manifest(version="not-a-version"))


def test_parse_manifest_tolerates_bad_size():
    assert updater.parse_manifest(_manifest(size="huge")).size == 0


# ── check() ───────────────────────────────────────────────────────────────────
def test_check_returns_none_when_current_is_newer(monkeypatch):
    monkeypatch.setattr(updater, "fetch_manifest", lambda *a, **k: _manifest(version="1.0.0"))
    assert updater.check(current="2.0.0") is None


def test_check_returns_info_when_newer(monkeypatch):
    monkeypatch.setattr(updater, "fetch_manifest", lambda *a, **k: _manifest(version="2.5.0"))
    info = updater.check(current="2.0.0")
    assert info is not None and info.version == "2.5.0"


def test_check_propagates_manifest_errors(monkeypatch):
    def boom(*a, **k):
        raise UpdateError("no releases")
    monkeypatch.setattr(updater, "fetch_manifest", boom)
    with pytest.raises(UpdateError):
        updater.check()


# ── verification ──────────────────────────────────────────────────────────────
def _write(path, data=b"payload"):
    path.write_bytes(data)
    return updater.UpdateInfo(
        version="9.9.9", url="https://example.com/x.exe",
        sha256=hashlib.sha256(data).hexdigest(), size=len(data))


def test_verify_accepts_matching_file(tmp_path):
    p = tmp_path / "x.exe"
    info = _write(p)
    updater.verify(p, info)  # must not raise


def test_verify_rejects_wrong_hash(tmp_path):
    p = tmp_path / "x.exe"
    info = _write(p)
    p.write_bytes(b"tampered")
    with pytest.raises(UpdateError):
        updater.verify(p, info)


def test_verify_rejects_wrong_size(tmp_path):
    p = tmp_path / "x.exe"
    info = _write(p)
    info.size = 999
    with pytest.raises(UpdateError):
        updater.verify(p, info)


def test_download_deletes_a_file_that_fails_verification(tmp_path, monkeypatch):
    # The critical safety property: a corrupted or tampered download must not be
    # left on disk where a later run could pick it up and execute it.
    payload = b"the real build"
    info = updater.UpdateInfo(
        version="9.9.9", url="https://example.com/x.exe",
        sha256=hashlib.sha256(b"something else").hexdigest(), size=len(payload))

    class _Resp:
        headers = {"Content-Length": str(len(payload))}
        def __init__(self): self._d = [payload, b""]
        def read(self, n=-1): return self._d.pop(0) if self._d else b""
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(updater, "_get", lambda *a, **k: _Resp())
    with pytest.raises(UpdateError):
        updater.download(info, dest_dir=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_download_succeeds_and_names_by_version(tmp_path, monkeypatch):
    payload = b"the real build"
    info = updater.UpdateInfo(
        version="9.9.9", url="https://example.com/x.exe",
        sha256=hashlib.sha256(payload).hexdigest(), size=len(payload))

    class _Resp:
        headers = {"Content-Length": str(len(payload))}
        def __init__(self): self._d = [payload, b""]
        def read(self, n=-1): return self._d.pop(0) if self._d else b""
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(updater, "_get", lambda *a, **k: _Resp())
    out = updater.download(info, dest_dir=tmp_path)
    assert out.name == "Macronaut-9.9.9.exe"
    assert out.read_bytes() == payload


def test_https_is_enforced_on_fetch():
    """⚠ Matches the message, not just the type.

    Without `match`, this passes for any UpdateError at all. `_get` re-raises
    network failures unwrapped today, so it happened to be sound — but it is
    one reasonable refactor away from being a test that passes because the
    request failed rather than because it was refused. It also means that if
    the guard ever goes, this reaches `urlopen` with a plain-http URL: the only
    thing standing between that and a real request to example.com is the
    session-wide blocker in conftest, and this suite has a history of
    discovering it was making live requests.
    """
    with pytest.raises(UpdateError, match="(?i)https"):
        updater._get("http://example.com/update.json")


# ── reaching the server at all ────────────────────────────────────────────────
import http.client            # noqa: E402  (used only by the tests below)
import urllib.error           # noqa: E402


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    """Retries are real seconds. Keep the suite honest about the count, fast
    about the wait."""
    monkeypatch.setattr(updater.time, "sleep", lambda *_a: None)


def _reset():
    return urllib.error.URLError(
        http.client.RemoteDisconnected("Remote end closed connection without response"))


def test_a_reset_connection_is_retried_not_reported(monkeypatch):
    # github.com refuses this client intermittently; a single-shot request turns
    # one bad connection into "Could not reach the update server".
    tries = []

    def flaky(req, timeout=None):
        tries.append(req.full_url)
        if len(tries) < 3:
            raise _reset()
        return object()

    monkeypatch.setattr(updater.urllib.request, "urlopen", flaky)
    assert updater._get("https://example.com/update.json") is not None
    assert len(tries) == 3


def test_a_refusal_is_not_retried(monkeypatch):
    # An HTTPError is the server answering. Asking again gets the same answer,
    # so retrying only makes the user wait for a verdict we already have.
    tries = []

    def refused(req, timeout=None):
        tries.append(1)
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(updater.urllib.request, "urlopen", refused)
    with pytest.raises(urllib.error.HTTPError):
        updater._get("https://example.com/update.json")
    assert len(tries) == 1


def test_an_unreachable_host_still_gives_up(monkeypatch):
    def dead(req, timeout=None):
        raise _reset()

    monkeypatch.setattr(updater.urllib.request, "urlopen", dead)
    with pytest.raises(urllib.error.URLError):
        updater._get("https://example.com/update.json")


class _FakeResp:
    def __init__(self, payload):
        self._d = [payload, b""]
        self.headers = {"Content-Length": str(len(payload))}
    def read(self, n=-1): return self._d.pop(0) if self._d else b""
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_the_manifest_falls_back_to_the_api_when_the_web_host_refuses(monkeypatch):
    # Measured: github.com/…/releases/latest/download/ answered 0/12 while
    # api.github.com answered 12/12. Same bytes, different host.
    manifest = json.dumps({"version": "9.9.9", "url": "https://example.com/x.exe",
                           "sha256": "a" * 64}).encode()
    listing = json.dumps({"assets": [
        {"name": "update.json",
         "url": "https://api.github.com/repos/o/r/releases/assets/1"}]}).encode()
    seen = []

    def routed(url, timeout=updater.NETWORK_TIMEOUT, headers=None):
        seen.append(url)
        if url.startswith("https://github.com/"):
            raise _reset()
        if url.endswith("/releases/latest"):
            return _FakeResp(listing)
        return _FakeResp(manifest)

    monkeypatch.setattr(updater, "_get", routed)
    data = updater.fetch_manifest(
        "https://github.com/o/r/releases/latest/download/update.json")
    assert data["version"] == "9.9.9"
    assert seen[-1] == "https://api.github.com/repos/o/r/releases/assets/1"


def test_the_original_error_survives_a_failed_fallback(monkeypatch):
    # If both routes are down, the message must name the route the user meant to
    # take — not whatever the API happened to say on the way past.
    def dead(url, timeout=updater.NETWORK_TIMEOUT, headers=None):
        raise _reset()

    monkeypatch.setattr(updater, "_get", dead)
    with pytest.raises(UpdateError) as e:
        updater.fetch_manifest(
            "https://github.com/o/r/releases/latest/download/update.json")
    assert "Could not reach the update server" in str(e.value)


# ── apply() guards ────────────────────────────────────────────────────────────
def test_apply_refuses_when_running_from_source(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "current_exe", lambda: None)
    staged = tmp_path / "new.exe"
    staged.write_bytes(b"x")
    with pytest.raises(UpdateError):
        updater.apply(staged)


def test_apply_refuses_a_missing_staged_file(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "current_exe", lambda: tmp_path / "Macronaut.exe")
    with pytest.raises(UpdateError):
        updater.apply(tmp_path / "not-there.exe")


# ── Authenticode ──────────────────────────────────────────────────────────────
def test_signature_status_reports_an_unsigned_file_as_unsigned(tmp_path):
    p = tmp_path / "unsigned.exe"
    p.write_bytes(b"MZ not really an executable")
    trusted, reason = updater.signature_status(p)
    assert trusted is False
    assert reason  # a bare False with no explanation is useless in a log


def test_signature_status_never_raises_on_a_missing_file(tmp_path):
    # An updater that throws while deciding whether to trust something has
    # already failed; "we could not tell" must come back as untrusted.
    trusted, reason = updater.signature_status(tmp_path / "nope.exe")
    assert trusted is False and reason


def test_signature_status_recognises_a_genuinely_signed_binary():
    """Proves the WinVerifyTrust call actually works, not just that it says no.

    A broken ctypes struct would return "not signed" for everything and look
    exactly like correct behaviour against unsigned input -- which is all
    Macronaut has until it gets a certificate.
    """
    candidates = [__import__("pathlib").Path(sys.executable)]
    for c in candidates:
        if updater.signature_status(c)[0]:
            return
    pytest.skip(f"no embedded-signed binary available to test with: {candidates}")


def test_unsigned_updates_are_allowed_while_the_policy_is_off(tmp_path):
    p = tmp_path / "unsigned.exe"
    p.write_bytes(b"payload")
    assert updater.REQUIRE_SIGNATURE is False, \
        "flip this test's expectation in the same commit that starts signing"
    assert updater.verify_signature(p) is True


def test_unsigned_updates_are_refused_once_the_policy_is_on(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "REQUIRE_SIGNATURE", True)
    p = tmp_path / "unsigned.exe"
    p.write_bytes(b"payload")
    assert updater.verify_signature(p) is False


def test_apply_refuses_an_unsigned_staged_build_when_required(tmp_path, monkeypatch):
    # The property that matters: the policy is wired into the install path, not
    # merely available to call.
    monkeypatch.setattr(updater, "REQUIRE_SIGNATURE", True)
    monkeypatch.setattr(updater, "current_exe", lambda: tmp_path / "Macronaut.exe")
    staged = tmp_path / "new.exe"
    staged.write_bytes(b"payload")
    with pytest.raises(UpdateError):
        updater.apply(staged)


# ── the swap itself ───────────────────────────────────────────────────────────
def _dead_pid() -> int:
    """A PID that is definitely not running: spawn a trivial process, reap it,
    reuse its number. Beats guessing a magic number that might be live."""
    import subprocess
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_run_apply_mode_replaces_the_target(tmp_path, monkeypatch):
    target = tmp_path / "Macronaut.exe"
    target.write_bytes(b"OLD BUILD")
    staged = tmp_path / "Macronaut-9.9.9.exe"
    staged.write_bytes(b"NEW BUILD")
    # run_apply_mode installs its own executable over the target; in the real
    # flow that IS the staged .exe, because the staged one is what runs.
    monkeypatch.setattr(updater, "_self_path", lambda: staged)

    rc = updater.run_apply_mode(
        ["x", updater.APPLY_FLAG, "--target", str(target), "--pid", str(_dead_pid())])

    assert rc == 0
    assert target.read_bytes() == b"NEW BUILD"
    # The old build is kept aside, not deleted, until the next clean start.
    assert (tmp_path / "Macronaut.exe.old").read_bytes() == b"OLD BUILD"


def test_run_apply_mode_rolls_back_a_failed_copy(tmp_path, monkeypatch):
    # The one unacceptable outcome is a user left with no working Macronaut, so
    # a failure after the rename must put the old build back.
    target = tmp_path / "Macronaut.exe"
    target.write_bytes(b"OLD BUILD")
    staged = tmp_path / "Macronaut-9.9.9.exe"
    staged.write_bytes(b"NEW BUILD")
    monkeypatch.setattr(updater, "_self_path", lambda: staged)

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(updater.shutil, "copy2", boom)
    monkeypatch.setattr(updater, "_fail", lambda msg: None)  # no message box

    rc = updater.run_apply_mode(
        ["x", updater.APPLY_FLAG, "--target", str(target), "--pid", str(_dead_pid())])

    assert rc == 1
    assert target.read_bytes() == b"OLD BUILD"  # recovered
    assert not (tmp_path / "Macronaut.exe.old").exists()


def test_run_apply_mode_refuses_while_the_old_process_lives(tmp_path, monkeypatch):
    # Overwriting a file the old process still holds open is how you get a
    # corrupt install, so a live PID must abort the swap.
    target = tmp_path / "Macronaut.exe"
    target.write_bytes(b"OLD BUILD")
    monkeypatch.setattr(updater, "_wait_for_exit", lambda pid, timeout=0: False)
    monkeypatch.setattr(updater, "_fail", lambda msg: None)

    rc = updater.run_apply_mode(
        ["x", updater.APPLY_FLAG, "--target", str(target), "--pid", "1234"])

    assert rc == 1
    assert target.read_bytes() == b"OLD BUILD"


def test_run_apply_mode_needs_both_arguments(tmp_path):
    assert updater.run_apply_mode(["x", updater.APPLY_FLAG]) == 2
    assert updater.run_apply_mode(["x", updater.APPLY_FLAG, "--pid", "1"]) == 2


def test_argv_value_parsing():
    argv = ["x.exe", "--apply-update", "--target", "C:\\a b\\M.exe", "--pid", "42"]
    assert updater._argv_value(argv, "--target") == "C:\\a b\\M.exe"
    assert updater._argv_value(argv, "--pid") == "42"
    assert updater._argv_value(argv, "--nope") is None
    assert updater._argv_value(["--target"], "--target") is None  # no value after


# ── release manifest round-trip ───────────────────────────────────────────────
def test_manifest_written_by_release_is_readable_by_updater(tmp_path):
    # Guards the seam between release.py and updater.py: the file one writes is
    # exactly the shape the other validates.
    exe = tmp_path / "Macronaut.exe"
    exe.write_bytes(b"pretend build")
    manifest = {
        "version": "2.0.1",
        "url": "https://github.com/OWNER/repo/releases/download/v2.0.1/Macronaut.exe",
        "sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
        "size": exe.stat().st_size,
        "notes": "Fixed things",
        # ⚠ Kept on purpose. `mandatory` was retired on 12 September 2026, but
        # every update.json already published carries it, and an installed 2.3.x
        # keeps reading manifests forever. parse_manifest must go on ignoring
        # unknown keys rather than raising, or retiring a field would break the
        # updater in exactly the copies that cannot be updated to fix it.
        "mandatory": False,
        "published": "2026-07-30",
    }
    info = updater.parse_manifest(json.loads(json.dumps(manifest)))
    assert not hasattr(info, "mandatory")
    assert info.version == "2.0.1"
    assert version.is_newer(info.version, "2.0.0")
    updater.verify(exe, info)


def test_release_py_actually_produces_a_manifest_the_updater_accepts(tmp_path,
                                                                     monkeypatch):
    """The same seam, but driven by the real producer.

    The test above hand-writes the dict, so it agrees with updater.py by
    construction and would keep passing if release.py started emitting
    `"hash"` instead of `"sha256"`. Publishing is the one operation with no
    undo — a manifest nobody can parse is only discovered by users whose
    updates silently stop working — so run release.write_manifest() itself and
    validate whatever it emits.
    """
    import release

    exe = tmp_path / "Macronaut.exe"
    exe.write_bytes(b"pretend build" * 1000)
    monkeypatch.setattr(release, "EXE", exe)
    monkeypatch.setattr(release, "MANIFEST", tmp_path / "update.json")

    written = release.write_manifest("2.0.1", notes="Fixed things")
    info = updater.parse_manifest(json.loads(written.read_text(encoding="utf-8")))

    # Deliberately no comparison against version.__version__: this test is about
    # the release.py -> updater.py seam, not about which release is current, and
    # tying a literal here to the live version made it fail on the next bump.
    assert info.version == "2.0.1", "the version asked for must round-trip"
    assert info.size == exe.stat().st_size
    updater.verify(exe, info)          # hash + size agree with the real file
    # The client stages downloads under this name; a mismatch orphans the file.
    assert info.filename == "Macronaut-2.0.1.exe"


def test_release_manifest_url_points_at_the_configured_repo(tmp_path, monkeypatch):
    """The published URL must follow from version.UPDATE_REPO.

    UPDATE_REPO is baked into every shipped .exe and cannot be changed
    afterwards without stranding existing installs, so a manifest pointing
    somewhere else is unrecoverable rather than merely wrong.
    """
    import release

    exe = tmp_path / "Macronaut.exe"
    exe.write_bytes(b"x")
    monkeypatch.setattr(release, "EXE", exe)
    monkeypatch.setattr(release, "MANIFEST", tmp_path / "update.json")
    written = release.write_manifest("2.0.1")

    url = json.loads(written.read_text(encoding="utf-8"))["url"]
    assert url.startswith("https://"), "an http asset URL is rejected by the client"
    assert version.UPDATE_REPO in url
    assert url.endswith("/v2.0.1/Macronaut.exe")
    # Whatever it is, the client has to accept it.
    updater._require_https(url, "asset")


def test_signing_happens_before_the_manifest_is_written(tmp_path, monkeypatch):
    """Order matters and the failure is invisible until users try to update.

    signtool rewrites the .exe. A manifest written first records the hash of the
    *unsigned* bytes, so every download then fails its integrity check --
    correctly, and for a reason nothing in the release output would explain.
    """
    import release

    calls = []
    monkeypatch.setattr(release, "build", lambda: calls.append("build"))
    monkeypatch.setattr(release, "sign", lambda exe: calls.append("sign"))
    monkeypatch.setattr(release, "write_manifest",
                        lambda *a, **k: calls.append("manifest"))
    release.main(["--build", "--sign", "--manifest"])
    assert calls == ["build", "sign", "manifest"]


@pytest.mark.parametrize("argv", [[], ["--installer"]])
def test_asking_for_the_installer_still_builds_the_whole_release(argv, monkeypatch):
    """`--installer` means "also". `--bump patch --installer` re-executes as a
    bare `--installer`, and that once built the installer alone, leaving the
    previous .exe and manifest in dist/ under a bumped version."""
    import release

    calls = []
    monkeypatch.setattr(release, "build", lambda: calls.append("build"))
    monkeypatch.setattr(release, "build_installer",
                        lambda ver: calls.append("installer"))
    monkeypatch.setattr(release, "write_manifest",
                        lambda *a, **k: calls.append("manifest"))
    release.main(argv)
    assert calls == ["build", "installer", "manifest"]


def test_sign_refuses_without_a_certificate_thumbprint(monkeypatch, tmp_path):
    # No thumbprint must stop the release, not silently publish unsigned.
    import release
    monkeypatch.delenv(release.SIGN_THUMBPRINT_ENV, raising=False)
    with pytest.raises(SystemExit):
        release.sign(tmp_path / "Macronaut.exe")


def test_find_signtool_does_not_explode_when_the_sdk_is_absent():
    # Returns a path or None; never raises, so --sign can report a clean error.
    import release
    got = release.find_signtool()
    assert got is None or got.name.lower() == "signtool.exe"


def test_publish_refuses_a_manifest_that_does_not_describe_the_exe(tmp_path,
                                                                  monkeypatch):
    """A stale manifest is the realistic publishing mistake: rebuild the .exe,
    forget to rewrite update.json, and every download fails its hash check with
    no way to fix it except a new release."""
    import release

    exe = tmp_path / "Macronaut.exe"
    exe.write_bytes(b"build A")
    monkeypatch.setattr(release, "EXE", exe)
    monkeypatch.setattr(release, "MANIFEST", tmp_path / "update.json")
    release.write_manifest("2.0.1")
    exe.write_bytes(b"build B - rebuilt after the manifest was written")

    monkeypatch.setattr(release.shutil, "which", lambda _n: "gh")
    with pytest.raises(SystemExit):
        release.publish("2.0.1")


# ── the suite must not phone home ────────────────────────────────────────────

def test_the_suite_cannot_reach_the_network():
    """⚠ Wiring, for the `_no_live_update_check` guard in conftest.

    Measured 9 September 2026 before it existed: one run of
    `tests/test_gui_offscreen.py` made **20 live HTTPS requests to GitHub**,
    counted at `urllib.request.urlopen`. `MainWindow` arms a four-second
    single-shot update check, the suite builds many windows and runs far longer
    than four seconds, and the PostToolUse hook runs the suite on every file
    save — so it was twenty requests per edit from a machine that never asked.
    With the guard: zero.

    ⚠ Asserted by identity, not by behaviour. The obvious version — call it and
    require `UpdateError` — passes just as well on a machine that is simply
    offline, which is the one place this guard is least needed and most likely
    to be believed.
    """
    import urllib.request
    assert getattr(urllib.request.urlopen, "__name__", "") == "_blocked", (
        "conftest's network guard is not installed — the suite can make real "
        "requests to GitHub. See _no_live_update_check.")
def _fake_api(assets):
    """A stand-in for the api.github.com releases/latest response."""
    body = json.dumps({"assets": assets}).encode("utf-8")

    class _Resp:
        def read(self, n=-1):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return lambda *a, **k: _Resp()


def test_an_asset_url_from_the_api_must_also_be_https(monkeypatch):
    """The third `_require_https` call site, and the one that was untested.

    ⚠ The other two check a URL this project wrote: the manifest's `url`, and
    whatever `_get` was handed. This one checks a URL that arrives inside a
    **remote JSON response**, which is the only one of the three that is not
    ours. It is the fallback route — the API host, used when the web host
    refuses — so it runs exactly when something has already gone unusually
    wrong.

    Macronaut ships unsigned. With no signature to fall back on, https and the
    manifest hash are the whole defence, and a downgrade here would hand the
    bytes to whoever is on the path.
    """
    monkeypatch.setattr(updater, "_get", _fake_api(
        [{"name": "Macronaut.exe", "url": "http://example.com/asset"}]))
    with pytest.raises(UpdateError, match="(?i)https"):
        updater._api_asset_url("Macronaut.exe", "owner/repo", timeout=1)


def test_an_https_asset_url_from_the_api_is_accepted(monkeypatch):
    """The twin, so the test above cannot pass by refusing everything.

    ⚠ Without this, `_api_asset_url` could raise on every URL it is given and
    the test above would be perfectly happy — while the fallback route, the one
    that only runs when the normal one has failed, was broken for everyone.
    """
    monkeypatch.setattr(updater, "_get", _fake_api(
        [{"name": "Macronaut.exe", "url": "https://api.example.com/asset/1"}]))
    assert updater._api_asset_url(
        "Macronaut.exe", "owner/repo", timeout=1) == "https://api.example.com/asset/1"


def test_a_release_without_the_asset_says_so(monkeypatch):
    """Names the missing asset rather than failing somewhere later.

    The realistic shape: a release published with the manifest attached and the
    .exe forgotten. Every install then asks for a file that is not there.
    """
    monkeypatch.setattr(updater, "_get", _fake_api(
        [{"name": "update.json", "url": "https://api.example.com/asset/2"}]))
    with pytest.raises(UpdateError, match="Macronaut.exe"):
        updater._api_asset_url("Macronaut.exe", "owner/repo", timeout=1)
def test_when_the_rollback_also_fails_the_message_can_be_acted_on(tmp_path,
                                                                  monkeypatch):
    """The last line of defence's last line of defence.

    ⚠ The rollback above is what stops a failed update leaving someone with no
    Macronaut. This is what happens when the rollback *itself* fails: the app
    is sitting in a file called `Macronaut.exe.old` and the only thing standing
    between the user and a working install is the sentence they are shown.

    So the sentence is the subject. It has to name a path that exists and that
    really holds the old build — a message naming the wrong file, or a file
    that is not there, is worse than no message, because it is the only
    instruction a person has at the point where everything else has failed.

    Rare, and total when it happens: no GUI is running at this point, nothing
    is logged anywhere the user will look, and there is no second chance.
    """
    target = tmp_path / "Macronaut.exe"
    target.write_bytes(b"OLD BUILD")
    staged = tmp_path / "Macronaut-9.9.9.exe"
    staged.write_bytes(b"NEW BUILD")
    monkeypatch.setattr(updater, "_self_path", lambda: staged)

    def no_copy(*a, **k):
        raise OSError("disk full")

    real_replace = os.replace
    calls = {"n": 0}

    def replace_once_then_fail(src, dst):
        """Let the rename to .old through; refuse to put it back."""
        calls["n"] += 1
        if calls["n"] == 1:
            return real_replace(src, dst)
        raise OSError("the file is locked")

    monkeypatch.setattr(updater.shutil, "copy2", no_copy)
    monkeypatch.setattr(updater.os, "replace", replace_once_then_fail)

    said = []
    monkeypatch.setattr(updater, "_fail", lambda msg: said.append(msg))

    rc = updater.run_apply_mode(
        ["x", updater.APPLY_FLAG, "--target", str(target),
         "--pid", str(_dead_pid())])

    assert rc == 1
    assert said, "the worst outcome was not reported at all"
    message = said[-1]

    backup = tmp_path / "Macronaut.exe.old"
    assert backup.exists(), (
        "the old build is not where the rollback left it, so whatever the "
        "message says cannot be followed")
    assert backup.read_bytes() == b"OLD BUILD", (
        "the file the user is pointed at is not their old build")
    assert str(backup) in message, (
        "the message does not name the file the old build is actually in:\n"
        + message)
    assert "Macronaut.exe" in message, (
        "the message does not say what to rename it back to")
