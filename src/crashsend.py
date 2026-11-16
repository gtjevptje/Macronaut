"""Send harvested crash reports to Sentry. Stdlib only, Qt-free.

**Why not `sentry-sdk`.** Almost all of that library is machinery for
instrumenting the process it lives in: install hooks, catch the exception,
serialise it, ship it. None of that applies here. Macronaut's reports come from
a process that is *already dead* — the interesting crashes abort down in C, so
there was never an opportunity to run a handler, which is the whole reason
`crashreport.py` works the way it does. What we actually need is to take a JSON
file off disk and POST it, which is one function.

Against that, adding the dependency costs bundle size, PyInstaller
hidden-import care, and an unusually bad failure mode for this project: this
codebase has been bitten three times by a bundled dependency that imports fine
and then silently does nothing when frozen (winsdk and the vanished Windows
OCR, numpy excluded while cv2 was kept). A crash reporter that quietly stops
reporting is the same bug in the one component whose job is to tell you about
bugs. `updater.py` already talks HTTPS with nothing but urllib; so does this.

The wire format is Sentry's envelope endpoint: three newline-delimited JSON
documents (envelope header, item header, payload) POSTed with an
`X-Sentry-Auth` header. The DSN is a write-only ingestion key — it can submit
events and read nothing — which is why it is safe to compile into a client that
anyone can unpack, unlike an API token.
"""
from __future__ import annotations

import json
import re
import ssl
import time
import urllib.error
import urllib.request
import uuid
from typing import List, Optional, Tuple

import version

# The Sentry ingest key (Project Settings -> Client Keys). Empty would compile
# sending out entirely: nothing posted, local capture in crashreport.py
# unaffected.
#
# Safe to have in a client that anyone can unpack — a DSN is write-only, so it
# can submit events and read nothing. That is the whole reason this is not a
# GitHub token quietly filing issues.
#
# `ingest.de.sentry.io` is Sentry's EU region: reports from European users stay
# in the EU, which is the easy answer to the question the privacy notice would
# otherwise have to work harder to explain.
SENTRY_DSN = ("https://fa6cd6f6a797e3d8caa66a2d20ae9510"
              "@o4511846076579840.ingest.de.sentry.io/4511846183338064")

NETWORK_TIMEOUT = 12          # seconds; a crash report is never worth a hang
USER_AGENT = f"macronaut/{version.__version__}"

# Per launch. A user who has accumulated a backlog offline should not spend a
# minute of their startup uploading it, and twenty reports of the same crash
# tell you nothing the first three did not.
MAX_PER_LAUNCH = 5

# "File "x.py", line 12, in func" — turning the formatted traceback back into
# structured frames is what lets Sentry group two reports of the same bug
# together instead of listing them as unrelated strings.
_FRAME_RE = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>.+)$')
_EXC_RE = re.compile(r"^(?P<type>[A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning))"
                     r"(?:: (?P<value>.*))?$")


def enabled() -> bool:
    return bool(SENTRY_DSN.strip())


# ── DSN ───────────────────────────────────────────────────────────────────────

def parse_dsn(dsn: str) -> Optional[Tuple[str, str]]:
    """'https://key@oNNN.ingest.sentry.io/PROJECT' -> (envelope_url, key).

    -> None if it is unparseable, which must disable sending rather than raise:
    a typo'd DSN should cost crash reports, not startups.
    """
    try:
        m = re.match(r"^(https?)://([^@:/]+)(?::[^@]*)?@([^/]+)/(.+)$", dsn.strip())
        if not m:
            return None
        scheme, key, host, project = m.groups()
        project = project.strip("/")
        if not project:
            return None
        return f"{scheme}://{host}/api/{project}/envelope/", key
    except Exception:
        return None


# ── report -> Sentry event ────────────────────────────────────────────────────

def _frames(text: str) -> List[dict]:
    """Parse a formatted Python traceback into Sentry frames (oldest first)."""
    out: List[dict] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = _FRAME_RE.match(line)
        if not m:
            continue
        ctx = ""
        if i + 1 < len(lines) and not _FRAME_RE.match(lines[i + 1]):
            ctx = lines[i + 1].strip()
        filename = m.group("file")
        out.append({
            "filename": filename,
            "abs_path": filename,
            "lineno": int(m.group("line")),
            "function": m.group("func"),
            "context_line": ctx or None,
            # Everything shipped inside the .exe is ours; this is what makes
            # Sentry's "suspect commit"/in-app frame highlighting useful.
            "in_app": "site-packages" not in filename.replace("\\", "/"),
        })
    return out


def _exception_value(text: str) -> Tuple[str, str]:
    """Last meaningful line of a traceback -> (type, message)."""
    for line in reversed([l for l in text.strip().splitlines() if l.strip()]):
        m = _EXC_RE.match(line.strip())
        if m:
            return m.group("type"), (m.group("value") or "").strip()
        if ":" in line and not line.startswith(" "):
            head, _, rest = line.partition(":")
            if head and " " not in head.strip():
                return head.strip(), rest.strip()
        break
    return "Error", text.strip().splitlines()[-1][:200] if text.strip() else "Unknown"


def to_event(report: dict) -> dict:
    """Turn one harvested crash report into a Sentry event.

    The two fields that make this worth doing at all:

    `release` is the version that CRASHED, carried through from the session
    file. Updates apply on restart, so the build that uploads a report is
    frequently newer than the build that produced it — this is what stops every
    crash being credited to the release that fixed it, and what makes Sentry's
    regression detection ("this came back in 2.1.1") mean anything.

    `fingerprint` is set by hand for the crashes that have no stack to group on.
    A qFatal and a silent death would otherwise land in one useless bucket per
    unique message.
    """
    fatal = report.get("fatal") or []
    event = {
        "event_id": uuid.uuid4().hex,
        "timestamp": report.get("started") or time.time(),
        "platform": "python",
        "level": "fatal",
        "logger": "macronaut.crash",
        "release": report.get("version") or "unknown",
        # Keeps the developer's own `python main.py` runs out of the same
        # stream as real users' frozen builds.
        "environment": "production" if report.get("frozen") else "development",
        "sdk": {"name": "macronaut.crashsend", "version": version.__version__},
        "tags": {
            "os": report.get("os") or "?",
            "os_release": report.get("os_release") or "?",
            "arch": report.get("arch") or "?",
            "frozen": "yes" if report.get("frozen") else "no",
            # The single most useful filter: "silent" is the OOM / hard-kill
            # shape, where nothing in the process noticed it was ending.
            "silent": "yes" if report.get("silent") else "no",
        },
        "contexts": {
            "os": {"name": report.get("os") or "Windows",
                   "version": report.get("os_release") or ""},
            "runtime": {"name": "python", "version": report.get("python") or ""},
        },
        "extra": {},
    }

    doing = report.get("doing") or {}
    if doing:
        # What the app was executing, as of at most a second before it died.
        event["extra"]["last_node"] = doing.get("node")
        event["extra"]["last_node_kind"] = doing.get("kind")
        if doing.get("kind"):
            event["tags"]["node_kind"] = str(doing["kind"])
    if report.get("native"):
        event["extra"]["native_traceback"] = report["native"][:8000]

    crumbs = []
    for c in report.get("breadcrumbs") or []:
        data = {k: v for k, v in c.items() if k not in ("t", "kind")}
        crumbs.append({
            "timestamp": c.get("t") or event["timestamp"],
            "category": str(c.get("kind") or "note"),
            "level": "error" if c.get("kind") in ("fatal", "error") else "info",
            "message": str(c.get("kind") or ""),
            "data": data,
        })
        if c.get("kind") == "run_start" and c.get("backend"):
            event["tags"]["input_backend"] = str(c["backend"])
    if crumbs:
        event["breadcrumbs"] = {"values": crumbs[-100:]}
    if report.get("breadcrumbs_dropped"):
        # Say that the trail is incomplete. Reading a truncated breadcrumb list
        # as though it were the whole story is how you conclude the app was
        # idle when it was in fact very busy.
        event["extra"]["breadcrumbs_dropped"] = report["breadcrumbs_dropped"]

    py = next((f for f in fatal
               if f.get("kind") in ("exception", "thread_exception")), None)
    if py:
        etype, evalue = _exception_value(py.get("text") or "")
        frames = _frames(py.get("text") or "")
        entry = {"type": etype, "value": evalue,
                 "mechanism": {"type": py.get("kind"), "handled": False}}
        if frames:
            entry["stacktrace"] = {"frames": frames}
        event["exception"] = {"values": [entry]}
    else:
        qt = next((f for f in fatal if f.get("kind") == "qt_fatal"), None)
        if qt:
            msg = (qt.get("text") or "").strip()
            event["message"] = {"formatted": "Qt fatal: %s" % msg[:300]}
            # No Python stack exists for a qFatal, so group on the message with
            # the varying parts already stripped by Qt itself.
            event["fingerprint"] = ["qt-fatal", msg[:120]]
        elif report.get("native"):
            event["message"] = {"formatted": "Native crash (no Python exception)"}
            event["fingerprint"] = ["native", _native_key(report["native"])]
        else:
            event["message"] = {
                "formatted": "Process ended without shutting down "
                             "(no exception, no signal)"}
            # Every silent death groups together on purpose: individually they
            # carry nothing, and the number of them is the signal.
            event["fingerprint"] = ["silent-exit",
                                    str(doing.get("kind") or "idle")]
    return event


def _native_key(native: str) -> str:
    """A stable grouping key from a faulthandler dump's topmost frame."""
    for line in native.splitlines():
        m = _FRAME_RE.match(line)
        if m:
            return "%s:%s" % (m.group("file").split("\\")[-1].split("/")[-1],
                              m.group("func"))
    return "unknown"


# ── the wire ──────────────────────────────────────────────────────────────────

def _envelope(event: dict, dsn: str) -> bytes:
    payload = json.dumps(event, default=str).encode("utf-8")
    header = json.dumps({"event_id": event["event_id"], "dsn": dsn,
                         "sent_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                  time.gmtime())}).encode("utf-8")
    item = json.dumps({"type": "event", "length": len(payload)}).encode("utf-8")
    return header + b"\n" + item + b"\n" + payload + b"\n"


def send_event(event: dict, dsn: str = "", timeout: float = NETWORK_TIMEOUT) -> bool:
    """POST one event. -> True if Sentry accepted it.

    Never raises. A failure leaves the report file on disk to be retried on a
    later launch, which is the right behaviour for a user who crashed while
    offline.
    """
    dsn = (dsn or SENTRY_DSN).strip()
    parsed = parse_dsn(dsn)
    if not parsed:
        return False
    url, key = parsed
    req = urllib.request.Request(
        url, data=_envelope(event, dsn), method="POST",
        headers={
            "Content-Type": "application/x-sentry-envelope",
            "User-Agent": USER_AGENT,
            "X-Sentry-Auth": (
                "Sentry sentry_version=7, sentry_client=%s, sentry_key=%s"
                % (USER_AGENT, key)),
        })
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return 200 <= getattr(r, "status", r.getcode()) < 300
    except urllib.error.HTTPError as e:
        # 4xx means this event will never be accepted (bad DSN, project gone,
        # or over quota) — report it as sent so it stops being retried forever.
        # 429 is the exception: that one is temporary and worth keeping.
        return 400 <= e.code < 500 and e.code != 429
    except Exception:
        return False


def send_pending(data_dir=None, dsn: str = "",
                 limit: int = MAX_PER_LAUNCH) -> Tuple[int, int]:
    """Upload harvested reports. -> (sent, remaining).

    Meant to be called from a background thread: it does network I/O, and
    nothing about a crash report justifies delaying the window appearing.
    """
    import crashreport
    dsn = (dsn or SENTRY_DSN).strip()
    if not parse_dsn(dsn):
        return 0, len(crashreport.pending(data_dir))
    sent = 0
    for path in crashreport.pending(data_dir)[:limit]:
        rep = crashreport.load(path)
        if not rep:
            crashreport.discard(path)     # unreadable: no value in keeping it
            continue
        try:
            ok = send_event(to_event(rep), dsn)
        except Exception:
            ok = False
        if ok:
            crashreport.discard(path)
            sent += 1
        else:
            # Stop at the first failure: if the network is down, the rest will
            # fail too, and each one costs another timeout.
            break
    return sent, len(crashreport.pending(data_dir))
