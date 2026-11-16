"""Self-update: check GitHub Releases, download, verify, swap the .exe.

The shape of it
---------------
1. `check()`   — fetch a small JSON manifest published alongside each release
                 and decide whether it describes something newer than us.
2. `download()`— stream the new .exe into ~/.macronaut/updates/ and verify it
                 against the size + SHA-256 from the manifest.
3. `apply()`   — hand off to the freshly downloaded .exe, which waits for this
                 process to exit, replaces the old file and relaunches it.

Why a manifest instead of the GitHub API: `releases/latest/download/<asset>` is
a permanent redirect to the newest release's asset, so we need no version number
to find it and no API token. It also means moving off GitHub later is a URL
change, not a code change.

api.github.com is the *fallback*, not the route. github.com's web host was
measured refusing this client 12/12 on that download path in August 2026 while
the API and the asset CDN answered 12/12, so both `fetch_manifest()` and
`download()` retry a dead connection and then try the API's asset URL, which
lands on a different host. Staying off the API by default keeps us clear of its
60-requests-an-hour unauthenticated limit; reaching for it when the front door
is shut costs one extra request in the case that would otherwise be a failure.

Security notes — an updater is a remote-code-execution channel into the user's
machine, so the rules here are not optional:

- HTTPS only. A plain-http manifest or asset URL is rejected outright.
- The download is verified against the manifest's SHA-256 **and** size before
  anything is executed; a mismatch deletes the file and aborts.
- The trust anchor is TLS plus the GitHub account. Hash verification protects
  against corruption and a tampered CDN path, but NOT against someone who can
  publish a release — they would control the manifest and the binary alike.
  The real fix is code signing. `signature_status()` performs a live
  Authenticode check today; `REQUIRE_SIGNATURE` decides whether a failure is
  fatal, and stays False only while Macronaut itself ships unsigned.
- Nothing is ever downloaded or applied without the user's `auto_check_updates`
  setting, and applying always requires an explicit confirmation in the UI.

Windows specifics: a running .exe cannot be deleted, but it CAN be renamed. So
the swap is: rename the old one aside, move the new one into its place, relaunch,
and clean up the leftover on the next start. If the move fails half-way the
rename is rolled back, because the one unacceptable outcome is leaving the user
with no working Macronaut.
"""
from __future__ import annotations

import ctypes
import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Optional

import version

USER_AGENT = f"Macronaut/{version.__version__} (+auto-updater)"
NETWORK_TIMEOUT = 15  # seconds, per request

# The flag the newly downloaded .exe is relaunched with to perform the swap.
APPLY_FLAG = "--apply-update"

# The published name of the installer asset, and the one thing that tells `apply`
# which of the two update shapes it is holding. This module produces the name
# (`UpdateInfo.filename`) and consumes it, so the two cannot drift; `release.py`
# is pinned to the same spelling by a test.
SETUP_PREFIX = "Macronaut-Setup-"

# How long the swapping process waits for the old one to exit before giving up.
_EXIT_WAIT_SECONDS = 30

_MAX_MANIFEST_BYTES = 64 * 1024      # a manifest is ~500 bytes; anything near
                                     # this is wrong and we refuse to buffer it
_MAX_ASSET_BYTES = 400 * 1024 * 1024  # sanity ceiling for the .exe download
_MAX_API_BYTES = 1024 * 1024          # a release listing, with notes and assets

# How long to wait before each retry of a connection that died mid-handshake.
# Three tries in ~2 s total: long enough to ride out a reset, short enough that
# a genuinely unreachable server still reports quickly.
_RETRY_DELAYS = (0.4, 1.2)

GITHUB_API = "https://api.github.com"


@dataclass
class UpdateInfo:
    """One available update, as described by the manifest."""
    version: str
    url: str
    sha256: str
    size: int = 0
    notes: str = ""
    # ⚠ There was a `mandatory: bool` here until 12 September 2026, written by
    # `release.py --mandatory` and read by nothing. Dropped rather than wired
    # up, deliberately. A forced update needs an answer for the download that
    # fails — offline, GitHub down, antivirus blocking the write — and both
    # answers are bad: block the app, and a failed network call bricks
    # someone's tool; fall through to Later, and the promise was never real.
    # That path also cannot be tested honestly on one machine. Macronaut runs
    # long unattended jobs, so interrupting one to force an install is worse
    # here than in a browser.
    #
    # If a security fix ever needs urgency, add `urgent: bool` instead and give
    # UpdateDialog a banner saying what the fix is, keeping Later. Same
    # manifest shape, about an hour, none of the above.
    published: str = ""

    # ── The installer half of the same release (2.3.5 and later) ─────────────
    # ⚠ Optional, and every one of the manifests already published lacks it, so
    # nothing may depend on it being there. A release publishes two artefacts:
    # the portable one-file `Macronaut.exe` these fields do NOT describe, and a
    # folder build wrapped in an Inno Setup installer, which is what a copy
    # installed from that installer has to update with. A folder cannot be
    # swapped into a single .exe, so the shape of the running build decides —
    # see `best_for_this_install`.
    installer_url: str = ""
    installer_sha256: str = ""
    installer_size: int = 0
    kind: str = "exe"          # "exe" (portable, swapped) or "installer" (run)

    @property
    def filename(self) -> str:
        if self.kind == "installer":
            return f"{SETUP_PREFIX}{self.version}.exe"
        return f"Macronaut-{self.version}.exe"

    def as_installer(self) -> "UpdateInfo":
        """The same release, described by its installer asset. Raises if absent."""
        if not self.installer_url:
            raise UpdateError("This release publishes no installer.")
        return replace(self, url=self.installer_url, sha256=self.installer_sha256,
                       size=self.installer_size, kind="installer")

    def best_for_this_install(self) -> "UpdateInfo":
        """Whichever asset THIS copy of Macronaut can actually apply.

        ⚠ The portable build is the default and stays the default: it is what
        every copy published before 2.3.5 knows how to install, and those copies
        read this manifest forever.
        """
        if self.installer_url and is_folder_build():
            return self.as_installer()
        return self


class UpdateError(RuntimeError):
    """Anything that stops an update, with a message fit to show a user."""


# ── Paths ─────────────────────────────────────────────────────────────────────
def updates_dir() -> Path:
    """Where downloads are staged. Lives beside settings, not next to the .exe,
    so a Program Files install doesn't need write access to stage anything."""
    from settings import data_dir
    d = data_dir() / "updates"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def is_frozen() -> bool:
    """True when running as the PyInstaller .exe. Updating is only meaningful
    there — from source, `git pull` is the update mechanism."""
    return bool(getattr(sys, "frozen", False))


def is_folder_build() -> bool:
    """True when this is the FOLDER build — the one the installer ships.

    ⚠ How the two are told apart, and why it is this and not a registry lookup:
    PyInstaller sets `sys._MEIPASS` to wherever the bundle was unpacked. In a
    one-file build that is a fresh `_MEI…` directory under %TEMP%, far away from
    the .exe; in a folder build nothing is unpacked, so it is **inside the .exe's
    own directory**. That is a property of the running process rather than of
    what some installer wrote down, so it stays true for a folder somebody
    copied to a USB stick and cannot be made wrong by a half-finished uninstall.

    ⚠ "Inside", not "equal to", and the difference is a real build: PyInstaller
    6 puts the bundle in a `_internal` subdirectory rather than beside the .exe.
    The first version of this function compared the two for equality and
    answered False for every folder build there is — which fails *safe* (the
    portable asset still installs over a portable copy) and would therefore have
    gone unnoticed. The frozen build's own self-test prints the bundle path:
    `bundle=…/dist/Macronaut/_internal`.
    """
    if not is_frozen():
        return False
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return False
    try:
        bundle = Path(meipass).resolve()
        here = Path(sys.executable).resolve().parent
        return bundle == here or here in bundle.parents
    except OSError:
        return False


def current_exe() -> Optional[Path]:
    """The .exe to replace, or None when running from source."""
    return Path(sys.executable).resolve() if is_frozen() else None


def _self_path() -> Path:
    """This process's own executable — the file `run_apply_mode` installs.

    In the real flow that IS the freshly downloaded build, because the download
    is what gets launched to perform the swap. Split out so tests can point it
    at a fixture without reassigning the global `sys.executable`, which would
    break every subprocess in the test session.
    """
    return Path(sys.executable)


def manifest_url() -> str:
    """The manifest URL, allowing a settings override so the app can be pointed
    at a staging URL (or a self-hosted one later) without a new build."""
    try:
        from settings import SettingsManager
        override = (getattr(SettingsManager(), "update_manifest_url", "") or "").strip()
        if override:
            return override
    except Exception:
        pass
    return version.UPDATE_MANIFEST_URL


# ── Check ─────────────────────────────────────────────────────────────────────
def _require_https(url: str, what: str) -> None:
    if not str(url).lower().startswith("https://"):
        raise UpdateError(f"Refusing a non-HTTPS {what} URL: {url!r}")


def _is_transient(e: BaseException) -> bool:
    """True for a connection that died rather than one that answered "no".

    Measured 12 August 2026: github.com's *web* host refused this client on the
    release-download path 12 times out of 12 while api.github.com and
    raw.githubusercontent.com answered 12/12, and the root of github.com itself
    answered 6/12. The failure is "Remote end closed connection without
    response" — an `http.client.RemoteDisconnected`, i.e. a `ConnectionError` —
    arriving in ~50 ms, so it is not a timeout and every single-shot request
    reported it to the user as "Could not reach the update server".

    A retry costs nothing when the server is genuinely down (it refuses just as
    fast) and rescues the intermittent case, so the loop only covers failures of
    *transport*. An HTTPError is a real answer and is never retried.
    """
    seen: BaseException = e
    for _ in range(4):  # URLError wraps its cause in .reason, sometimes twice
        if isinstance(seen, (ConnectionError, TimeoutError, socket.timeout,
                             http.client.RemoteDisconnected,
                             http.client.IncompleteRead)):
            return True
        nxt = getattr(seen, "reason", None)
        if not isinstance(nxt, BaseException):
            return False
        seen = nxt
    return False


def _get(url: str, timeout: int = NETWORK_TIMEOUT,
         headers: Optional[dict] = None):
    _require_https(url, "download")
    head = {"User-Agent": USER_AGENT}
    head.update(headers or {})
    req = urllib.request.Request(url, headers=head)
    for delay in _RETRY_DELAYS:
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError:
            raise  # the server answered; asking again gets the same answer
        except (urllib.error.URLError, OSError) as e:
            if not _is_transient(e):
                raise
            time.sleep(delay)
    return urllib.request.urlopen(req, timeout=timeout)


def _asset_name(url: str) -> str:
    """The file name a release-asset URL ends in."""
    return str(url).split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]


def _repo_from_url(url: str) -> Optional[str]:
    """`owner/repo` out of a github.com release URL, or None if it isn't one."""
    m = re.match(r"https://github\.com/([^/]+)/([^/]+)/releases/", str(url), re.I)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def _api_asset_url(name: str, repo: str, timeout: int) -> str:
    """The api.github.com URL that serves the latest release's `name` asset.

    A different host from the one `releases/latest/download/` uses, which is the
    whole point: it is the route that still worked when the web host did not.
    Fetching it needs `Accept: application/octet-stream`, or the API hands back
    the asset's *metadata* instead of its bytes.
    """
    api = f"{GITHUB_API}/repos/{repo}/releases/latest"
    with _get(api, timeout, headers={"Accept": "application/vnd.github+json"}) as resp:
        data = json.loads(resp.read(_MAX_API_BYTES + 1).decode("utf-8"))
    for a in (data.get("assets") or []):
        if str(a.get("name", "")) == name:
            url = str(a.get("url") or "")
            _require_https(url, "asset")
            return url
    raise UpdateError(f"The latest release has no {name!r} asset.")


def _open_asset(url: str, name: str, timeout: int = NETWORK_TIMEOUT):
    """Open a release asset, over the API if the web host won't have us.

    Both routes end at the same bytes and the caller verifies them against the
    manifest's size + SHA-256 either way, so the fallback widens *reachability*
    and not trust. If the API route also fails, the original error is what the
    user sees — it is the one that describes the route they were meant to take.
    """
    try:
        return _get(url, timeout)
    except urllib.error.HTTPError:
        raise
    except (urllib.error.URLError, OSError) as first:
        repo = _repo_from_url(url)
        if not repo:
            raise
        try:
            api_url = _api_asset_url(name, repo, timeout)
            return _get(api_url, timeout,
                        headers={"Accept": "application/octet-stream"})
        except Exception:
            raise first


def fetch_manifest(url: Optional[str] = None, timeout: int = NETWORK_TIMEOUT) -> dict:
    """Download and parse the update manifest. Raises UpdateError."""
    url = url or manifest_url()
    try:
        with _open_asset(url, _asset_name(url), timeout) as resp:
            raw = resp.read(_MAX_MANIFEST_BYTES + 1)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # No release published yet, or UPDATE_REPO is still the placeholder.
            raise UpdateError("No releases published yet.") from e
        raise UpdateError(f"Update check failed (HTTP {e.code}).") from e
    except (urllib.error.URLError, OSError) as e:
        raise UpdateError(f"Could not reach the update server ({e}).") from e
    if len(raw) > _MAX_MANIFEST_BYTES:
        raise UpdateError("Update manifest is implausibly large — ignoring it.")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise UpdateError("Update manifest is not valid JSON.") from e
    if not isinstance(data, dict):
        raise UpdateError("Update manifest is not an object.")
    return data


def parse_manifest(data: dict) -> UpdateInfo:
    """Validate a manifest dict into an UpdateInfo. Raises UpdateError."""
    try:
        ver = str(data["version"]).strip()
        url = str(data["url"]).strip()
        sha = str(data["sha256"]).strip().lower()
    except KeyError as e:
        raise UpdateError(f"Update manifest is missing {e.args[0]!r}.") from e
    if version.parse(ver) is None:
        raise UpdateError(f"Update manifest has an unreadable version: {ver!r}")
    _require_https(url, "asset")
    if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise UpdateError("Update manifest has a malformed sha256.")
    try:
        size = int(data.get("size", 0) or 0)
    except (TypeError, ValueError):
        size = 0
    inst_url, inst_sha, inst_size = _parse_installer(data.get("installer"))
    return UpdateInfo(
        version=ver, url=url, sha256=sha, size=size,
        notes=str(data.get("notes", "") or ""),
        published=str(data.get("published", "") or ""),
        installer_url=inst_url, installer_sha256=inst_sha,
        installer_size=inst_size,
    )


def _parse_installer(block) -> tuple:
    """-> (url, sha256, size), or ("", "", 0) when there is no usable installer.

    ⚠ A malformed block is IGNORED rather than fatal, and that is the whole
    design of this function. The portable asset beside it is always valid, so
    refusing the entire manifest over an optional extra would turn a typo in one
    release into "no updates at all" for every installed copy — including the
    copies that would have to be updated to fix it. A tampered block cannot buy
    anything either: whatever it names is still verified against the sha256 the
    same block carries, and against the signature check, before anything runs.
    """
    if not isinstance(block, dict):
        return "", "", 0
    url = str(block.get("url", "") or "").strip()
    sha = str(block.get("sha256", "") or "").strip().lower()
    if not url.lower().startswith("https://"):
        return "", "", 0
    if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        return "", "", 0
    try:
        size = int(block.get("size", 0) or 0)
    except (TypeError, ValueError):
        size = 0
    return url, sha, size


def check(current: str = version.__version__,
          url: Optional[str] = None,
          timeout: int = NETWORK_TIMEOUT) -> Optional[UpdateInfo]:
    """-> UpdateInfo if a newer version is published, else None.

    Raises UpdateError on a network/manifest problem so a *manual* "Check now"
    can report why. The automatic startup check swallows it — a user offline, or
    behind a proxy, must never get a popup every launch.
    """
    info = parse_manifest(fetch_manifest(url, timeout))
    return info if version.is_newer(info.version, current) else None


# ── Download + verify ─────────────────────────────────────────────────────────
def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def verify(path: Path, info: UpdateInfo) -> None:
    """Raise UpdateError unless `path` matches the manifest exactly."""
    actual_size = path.stat().st_size
    if info.size and actual_size != info.size:
        raise UpdateError(
            f"Downloaded file is {actual_size} bytes, expected {info.size}.")
    actual = sha256_file(path)
    if actual != info.sha256:
        raise UpdateError(
            "Downloaded file failed its integrity check — it was corrupted or "
            "tampered with, and has been discarded.")


# Whether an unsigned or badly-signed update is refused outright.
#
# False while Macronaut ships unsigned: enforcing it now would reject every
# legitimate update and leave users stranded on the build they have. Flip to
# True in the SAME release that first ships a signed .exe -- not before (nothing
# validates) and not later (a release that tolerates unsigned updates is a
# release whose users can still be handed one).
#
# Note the ordering constraint: the check runs in the OLD build, against the NEW
# download. So the first signed release is verified by a build compiled with
# this still False. Flipping it takes effect for the release AFTER the first
# signed one -- which is correct, and is why it must not wait.
REQUIRE_SIGNATURE = False

# WinVerifyTrust results worth naming. Anything else is reported as a raw code.
_TRUST_STATUS = {
    0x00000000: (True, "signed and trusted"),
    0x800B0100: (False, "not signed"),
    0x800B0101: (False, "the signing certificate has expired"),
    0x800B0109: (False, "signed by an untrusted root certificate"),
    0x800B010C: (False, "the signing certificate was revoked"),
    0x800B0111: (False, "the signing certificate is explicitly distrusted"),
    0x80096010: (False, "the file was modified after it was signed"),
    0x80092026: (False, "local security settings blocked the check"),
}


def signature_status(path: Path) -> tuple:
    """Authenticode-verify `path`. -> (trusted: bool, reason: str).

    Never raises: a failure to *check* is reported as untrusted with the reason,
    because "we could not tell" and "it is fine" must not collapse into the same
    answer in an update path.

    Embedded signatures only (WTD_CHOICE_FILE). Catalog signatures are how
    Windows signs its own components; a third-party .exe downloaded from the
    internet carries its signature inside the file, so looking for a catalog
    entry would only ever produce false negatives here.
    """
    if os.name != "nt":  # pragma: no cover - Windows-only product
        return (False, "signature checking is only implemented on Windows")
    if not path.exists():
        return (False, "the file does not exist")

    import ctypes.wintypes as wt

    class _GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                    ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    class _FileInfo(ctypes.Structure):
        _fields_ = [("cbStruct", wt.DWORD),
                    ("pcwszFilePath", wt.LPCWSTR),
                    ("hFile", wt.HANDLE),
                    ("pgKnownSubject", ctypes.c_void_p)]

    class _WintrustData(ctypes.Structure):
        _fields_ = [("cbStruct", wt.DWORD),
                    ("pPolicyCallbackData", ctypes.c_void_p),
                    ("pSIPClientData", ctypes.c_void_p),
                    ("dwUIChoice", wt.DWORD),
                    ("fdwRevocationChecks", wt.DWORD),
                    ("dwUnionChoice", wt.DWORD),
                    ("pFile", ctypes.POINTER(_FileInfo)),
                    ("dwStateAction", wt.DWORD),
                    ("hWVTStateData", wt.HANDLE),
                    ("pwszURLReference", wt.LPCWSTR),
                    ("dwProvFlags", wt.DWORD),
                    ("dwUIContext", wt.DWORD),
                    ("pSignatureSettings", ctypes.c_void_p)]

    # WINTRUST_ACTION_GENERIC_VERIFY_V2
    action = _GUID(0x00AAC56B, 0xCD44, 0x11D0,
                   (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0,
                                        0x4F, 0xC2, 0x95, 0xEE))
    fi = _FileInfo(ctypes.sizeof(_FileInfo), str(path), None, None)
    wd = _WintrustData()
    wd.cbStruct = ctypes.sizeof(_WintrustData)
    wd.dwUIChoice = 2            # WTD_UI_NONE - never prompt inside an updater
    wd.fdwRevocationChecks = 0   # WTD_REVOKE_NONE - see below
    wd.dwUnionChoice = 1         # WTD_CHOICE_FILE
    wd.pFile = ctypes.pointer(fi)
    wd.dwStateAction = 1         # WTD_STATEACTION_VERIFY
    wd.dwProvFlags = 0x00000100  # WTD_SAFER_FLAG

    # Revocation checking is off deliberately: it makes a network call, and an
    # offline or proxied user would otherwise have every update fail with a
    # confusing "could not check" instead of installing. Expiry, tampering and
    # an untrusted root are all still caught, and they are the realistic cases.
    try:
        wintrust = ctypes.WinDLL("wintrust.dll")
        rc = wintrust.WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(wd))
        wd.dwStateAction = 2     # WTD_STATEACTION_CLOSE - frees the state data
        wintrust.WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(wd))
    except (OSError, AttributeError) as e:
        return (False, f"the signature could not be checked ({e})")

    return _TRUST_STATUS.get(rc & 0xFFFFFFFF,
                             (False, f"signature check failed (0x{rc & 0xFFFFFFFF:08X})"))


def verify_signature(path: Path) -> bool:
    """Whether `path` is acceptable to install, per the REQUIRE_SIGNATURE policy.

    While Macronaut ships unsigned this records the verdict and allows the
    install; the check itself is live either way, so the logs show what the
    answer *would* have been before enforcement is switched on.
    """
    trusted, reason = signature_status(path)
    if trusted:
        return True
    if REQUIRE_SIGNATURE:
        _log(f"REFUSED unsigned update {path.name}: {reason}")
        return False
    _log(f"signature not verified for {path.name}: {reason} "
         "(allowed - REQUIRE_SIGNATURE is off)")
    return True


def download(info: UpdateInfo,
             dest_dir: Optional[Path] = None,
             progress: Optional[Callable[[int, int], None]] = None,
             cancelled: Optional[Callable[[], bool]] = None) -> Path:
    """Stream the update to disk and verify it. -> path to the verified file.

    `progress(done_bytes, total_bytes)` is called as it goes; `cancelled()` is
    polled so a UI can abort a slow download. A failed or cancelled download
    leaves nothing behind.
    """
    # ⚠ Chosen here rather than by the caller. Every caller today hands over
    # whatever `check()` returned, and the question "which asset can this copy
    # actually install" is answered by the running build, not by the UI. Doing
    # it here also means the progress total, the verified hash and the staged
    # filename all describe the same file.
    info = info.best_for_this_install()

    dest_dir = dest_dir or updates_dir()
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise UpdateError(f"Cannot create the download folder ({e}).") from e

    final = dest_dir / info.filename
    part = final.with_suffix(".exe.part")  # never leave a half file looking whole
    if final.exists():
        try:  # already staged from a previous run — re-verify, don't re-download
            verify(final, info)
            return final
        except (UpdateError, OSError):
            _quiet_unlink(final)

    try:
        with _open_asset(info.url, _asset_name(info.url)) as resp, open(part, "wb") as f:
            total = info.size or int(resp.headers.get("Content-Length") or 0)
            done = 0
            while True:
                if cancelled is not None and cancelled():
                    raise UpdateError("Update download cancelled.")
                block = resp.read(1 << 18)
                if not block:
                    break
                done += len(block)
                if done > _MAX_ASSET_BYTES:
                    raise UpdateError("Update download exceeded the size limit.")
                f.write(block)
                if progress is not None:
                    progress(done, total)
    except UpdateError:
        _quiet_unlink(part)
        raise
    except (urllib.error.URLError, OSError) as e:
        _quiet_unlink(part)
        raise UpdateError(f"Download failed ({e}).") from e

    try:
        verify(part, info)
    except UpdateError:
        _quiet_unlink(part)  # never keep a file that failed verification
        raise

    try:
        os.replace(part, final)
    except OSError as e:
        _quiet_unlink(part)
        raise UpdateError(f"Could not finalise the download ({e}).") from e
    return final


def _quiet_unlink(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


# ── Apply ─────────────────────────────────────────────────────────────────────
def apply(staged: Path, target: Optional[Path] = None, relaunch: bool = True) -> None:
    """Hand the swap over to the downloaded .exe and return.

    The caller must quit promptly afterwards: the new process is already waiting
    for this PID to disappear before it can replace the file.
    """
    if not staged.exists():
        raise UpdateError("The downloaded update is missing.")
    if not verify_signature(staged):
        raise UpdateError("The downloaded update is not correctly signed.")
    if staged.name.startswith(SETUP_PREFIX):
        return _apply_installer(staged, relaunch=relaunch)

    target = target or current_exe()
    if target is None:
        raise UpdateError(
            "Running from source — update by pulling the repository instead.")
    if not os.access(target.parent, os.W_OK):
        raise UpdateError(
            f"No write access to {target.parent} — run Macronaut as "
            "administrator, or install it somewhere writable.")

    args = [str(staged), APPLY_FLAG,
            "--target", str(target),
            "--pid", str(os.getpid())]
    if relaunch:
        args.append("--relaunch")
    flags = 0
    if os.name == "nt":  # survive the parent's exit
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | \
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen(args, close_fds=True, creationflags=flags)
    except OSError as e:
        raise UpdateError(f"Could not start the updater ({e}).") from e


def _apply_installer(staged: Path, relaunch: bool = True) -> None:
    """Run the downloaded Inno Setup installer over this installation.

    The caller quits immediately afterwards, exactly as for the .exe swap — but
    for a different reason. The swap needs this process gone before it can
    rename the file; Setup can close the app itself (`CloseApplications=yes`),
    and quitting first simply means the user is not looking at a window that is
    about to be replaced underneath them.

    ⚠ `/SILENT`, not `/VERYSILENT`. The user pressed Install and is watching; a
    progress window is the only thing on screen that says the update is
    happening. `/VERYSILENT` is for an unattended machine, which this is not.

    ⚠ No `--target`: Setup knows where it installed from its own AppId, so a
    copy the user moved or renamed still upgrades in place instead of landing a
    second installation beside it.
    """
    args = [str(staged), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
            "/CLOSEAPPLICATIONS"]
    if relaunch:
        # Setup restarts what it closed. Without this the update finishes and
        # the user is left staring at an empty desktop wondering whether it
        # worked — which is also how the .exe swap behaves without --relaunch.
        args.append("/RESTARTAPPLICATIONS")
    flags = 0
    if os.name == "nt":  # survive the parent's exit
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) |                 getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen(args, close_fds=True, creationflags=flags)
    except OSError as e:
        raise UpdateError(f"Could not start the installer ({e}).") from e


def _wait_for_exit(pid: int, timeout: float = _EXIT_WAIT_SECONDS) -> bool:
    """Poll until `pid` is gone. -> False on timeout (we then refuse to swap:
    replacing a file the old process still has open is how you get a corrupt
    install)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.2)
    return not _pid_alive(pid)


def _pid_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    SYNCHRONIZE = 0x00100000
    STILL_ACTIVE = 259
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(SYNCHRONIZE | 0x0400, False, pid)  # +QUERY_LIMITED
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return code.value == STILL_ACTIVE
        return True
    finally:
        k32.CloseHandle(handle)


def run_apply_mode(argv: list) -> int:
    """The `--apply-update` entry point, executed BY the newly downloaded .exe.

    Runs before any GUI exists, so problems are reported with a native message
    box and written to the update log rather than raised.
    """
    target = _argv_value(argv, "--target")
    pid = _argv_value(argv, "--pid")
    relaunch = "--relaunch" in argv
    if not target or not pid:
        return 2
    target_path = Path(target)
    backup = target_path.with_suffix(target_path.suffix + ".old")

    if not _wait_for_exit(int(pid)):
        _fail("Macronaut is still running, so the update could not be applied. "
              "Close it completely and try again.")
        return 1

    _quiet_unlink(backup)  # a leftover from an earlier update
    renamed = False
    try:
        if target_path.exists():
            os.replace(target_path, backup)
            renamed = True
        shutil.copy2(_self_path(), target_path)
    except OSError as e:
        if renamed:
            # Roll back: better the old version than no version at all.
            try:
                os.replace(backup, target_path)
            except OSError:
                _fail(f"The update failed ({e}) AND the previous version could "
                      f"not be restored. Your app is at:\n{backup}\n"
                      "Rename it back to Macronaut.exe to recover.")
                return 1
        _fail(f"The update could not be applied ({e}). Macronaut was left "
              "unchanged.")
        return 1

    _log(f"updated to {version.__version__} at {target_path}")
    if relaunch:
        try:
            subprocess.Popen([str(target_path)], close_fds=True,
                             creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        except OSError:
            pass
    return 0


def cleanup(target: Optional[Path] = None) -> None:
    """Delete the previous version left behind by a swap, plus any stale
    downloads. Safe to call on every start; failures are ignored because a
    leftover file is a cosmetic problem, not a functional one."""
    target = target or current_exe()
    if target is not None:
        _quiet_unlink(target.with_suffix(target.suffix + ".old"))
    try:
        keep = f"Macronaut-{version.__version__}.exe"
        for p in updates_dir().glob("Macronaut-*.exe"):
            if p.name != keep:
                _quiet_unlink(p)
        for p in updates_dir().glob("*.part"):
            _quiet_unlink(p)
    except OSError:
        pass


# ── Small helpers ─────────────────────────────────────────────────────────────
def _argv_value(argv: list, flag: str) -> Optional[str]:
    try:
        return argv[argv.index(flag) + 1]
    except (ValueError, IndexError):
        return None


def _log(msg: str) -> None:
    try:
        from settings import data_dir
        with open(data_dir() / "update.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def _fail(msg: str) -> None:
    _log(f"ERROR {msg}")
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(0, msg, "Macronaut update", 0x10)
        except Exception:
            pass
    else:  # pragma: no cover - non-Windows
        print(msg, file=sys.stderr)


# ── CLI (manual testing without launching the GUI) ────────────────────────────
def _cli(argv: list) -> int:
    if argv and argv[0] == "--check":
        print(f"current: {version.__version__}")
        print(f"manifest: {manifest_url()}")
        try:
            info = check()
        except UpdateError as e:
            print(f"NOT AVAILABLE: {e}")
            return 1
        if info is None:
            print("Up to date.")
            return 0
        print(f"Update available: {info.version} ({info.size} bytes)")
        print(f"  {info.url}")
        print(f"  sha256 {info.sha256}")
        if info.notes:
            print(f"  notes: {info.notes[:200]}")
        return 0

    if argv and argv[0] == "--download":
        try:
            info = check()
            if info is None:
                print("Up to date — nothing to download.")
                return 0
            last = [-1]

            def show(done, total):
                pct = int(done * 100 / total) if total else 0
                if pct != last[0]:
                    last[0] = pct
                    print(f"\r  {pct}%", end="", flush=True)

            path = download(info, progress=show)
            print(f"\nVerified: {path}")
            return 0
        except UpdateError as e:
            print(f"FAILED: {e}")
            return 1

    print(__doc__.strip().splitlines()[0])
    print("usage: python updater.py [--check | --download]")
    return 0


if __name__ == "__main__":
    if APPLY_FLAG in sys.argv:
        raise SystemExit(run_apply_mode(sys.argv))
    raise SystemExit(_cli(sys.argv[1:]))
