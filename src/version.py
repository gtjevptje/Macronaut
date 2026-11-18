"""Single source of truth for Macronaut's application version.

Everything that needs to know "which build is this?" reads from here: the
About/Settings UI, the updater's comparison, the Windows file-properties
resource baked in by `macronaut.spec`, and `release.py` when it publishes.

Bump `__version__` for a release — `release.py --bump` does it for you, so the
version, the built .exe and the published manifest can never disagree.

Versioning is semver-ish: MAJOR.MINOR.PATCH, optionally with a pre-release
suffix ("2.1.0-beta.1"). A pre-release sorts BELOW the same numeric version
without one, so 2.1.0-beta.1 < 2.1.0 — a beta tester is offered the final
release as an upgrade, which is what you want.
"""
from __future__ import annotations

from typing import Optional, Tuple

__version__ = "2.3.7"

APP_NAME = "Macronaut"

# ── Where updates come from ───────────────────────────────────────────────────
# The PUBLIC repo that hosts released binaries. Macronaut's source lives in a
# private repo; this one holds only the .exe, the update manifest and the
# release notes, so the updater can fetch anonymously with no token.
#
# Changing this after a release strands every installed copy: an old build keeps
# asking the OLD url forever, because that url is baked into the .exe it shipped
# in. Treat it as permanent from the first published release onwards.
UPDATE_REPO = "gtjevptje/macronaut-releases"

# GitHub keeps this path pointing at the newest release's asset forever, so the
# app never has to know a version number to find the manifest, and we avoid the
# rate-limited api.github.com entirely.
UPDATE_MANIFEST_URL = (
    f"https://github.com/{UPDATE_REPO}/releases/latest/download/update.json"
)

# Human-facing page to send someone to when an automatic update can't proceed.
RELEASES_PAGE_URL = f"https://github.com/{UPDATE_REPO}/releases/latest"

# The project's page, under the name people see and search for. `UPDATE_REPO`
# above is the same repository under the name it had when the first build
# shipped; GitHub redirects it, and the updater has to keep using it. Anything
# a person is sent to — to star it, to report a problem — uses this one.
REPO_URL = "https://github.com/gtjevptje/Macronaut"
ISSUES_URL = f"{REPO_URL}/issues/new/choose"


def parse(v: str) -> Optional[Tuple[tuple, tuple]]:
    """'v2.1.0-beta.1' -> ((2, 1, 0), pre-release key). None if unparseable.

    The second element orders pre-releases below the plain release: a version
    with no suffix gets (1,) and one with a suffix gets (0, 'beta', 1), so a
    plain tuple comparison does the right thing.
    """
    if not isinstance(v, str):
        return None
    s = v.strip().lstrip("vV")
    if not s:
        return None
    core, _, pre = s.partition("-")
    parts = core.split(".")
    try:
        nums = tuple(int(p) for p in parts)
    except ValueError:
        return None
    if not nums:
        return None
    # Pad to 3 so "2.1" and "2.1.0" compare equal.
    nums = (nums + (0, 0, 0))[:3] if len(nums) < 3 else nums
    if not pre:
        return nums, (1,)
    key = tuple(int(p) if p.isdigit() else p for p in pre.split("."))
    return nums, (0,) + key


def is_newer(candidate: str, current: str = __version__) -> bool:
    """True if `candidate` is a strictly newer version than `current`.

    Unparseable input returns False — an updater must never offer a "newer"
    version it could not actually understand.
    """
    a, b = parse(candidate), parse(current)
    if a is None or b is None:
        return False
    try:
        return a > b
    except TypeError:
        # Mixed int/str pre-release parts (e.g. "beta" vs 1) aren't orderable.
        # Treat as "not newer" rather than guessing.
        return False


def as_tuple(v: str = __version__) -> tuple:
    """Numeric (major, minor, patch), for the Windows version resource."""
    p = parse(v)
    return p[0] if p else (0, 0, 0)
