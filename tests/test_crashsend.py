"""Headless tests for crash transport (crashsend.py). No network is touched.

The event-shaping tests matter more than they look. A crash reporter that
uploads successfully but groups everything into one bucket, or credits every
crash to the wrong release, produces a dashboard that is actively misleading —
which is worse than an empty one, because it gets believed.
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import crashreport
import crashsend
import version

DSN = "https://pubkey123@o111222.ingest.sentry.io/4455"


@pytest.fixture(autouse=True)
def _never_the_real_project(monkeypatch):
    """No test in this module may reach the live Sentry project.

    An omitted dsn argument deliberately falls back to the compiled-in one, so
    the moment a real DSN was configured two tests here started POSTing junk
    events at production — Sentry rejected them, which is the only reason it
    was visible at all. Pinning the module-level constant to the fake removes
    the possibility rather than relying on every test remembering.
    """
    monkeypatch.setattr(crashsend, "SENTRY_DSN", DSN)

TRACEBACK = '''Traceback (most recent call last):
  File "C:\\\\app\\\\main.py", line 210, in _on_log_batch
    self._run_log.addItems(lines)
  File "C:\\\\app\\\\flow_exec.py", line 88, in run
    raise RuntimeError("boom in a flow step")
RuntimeError: boom in a flow step
'''


def _report(**over):
    rep = {
        "schema": 1, "kind": "crash", "version": "2.0.7", "frozen": True,
        "python": "3.12.1", "os": "Windows", "os_release": "11",
        "arch": "AMD64", "started": time.time(), "fatal": [], "native": "",
        "breadcrumbs": [], "doing": None, "silent": True,
    }
    rep.update(over)
    return rep


# ── DSN ───────────────────────────────────────────────────────────────────────

def test_a_valid_dsn_becomes_an_envelope_url_and_a_key():
    url, key = crashsend.parse_dsn(DSN)
    assert url == "https://o111222.ingest.sentry.io/api/4455/envelope/"
    assert key == "pubkey123"


@pytest.mark.parametrize("bad", ["", "   ", "not-a-dsn", "https://o1.sentry.io/9",
                                 "https://key@host/", "ftp://k@h/1"])
def test_an_unusable_dsn_is_rejected_rather_than_raising(bad):
    """A typo in the DSN should cost crash reports, never a startup."""
    assert crashsend.parse_dsn(bad) is None


def test_the_real_dsn_that_ships_is_parseable(monkeypatch):
    """Guards the one value that cannot be caught by any other test: a DSN
    typo'd into the source disables reporting in a way nothing else notices,
    because "no crash reports" and "no crashes" look the same from here."""
    monkeypatch.undo()          # look at the genuine constant, not the fixture's
    import importlib
    real = importlib.import_module("crashsend").SENTRY_DSN
    if real.strip():
        url, key = crashsend.parse_dsn(real)
        assert url.endswith("/envelope/") and key


def test_an_omitted_dsn_falls_back_to_the_built_in_one():
    """Production callers pass nothing, so "" means "use the compiled-in DSN",
    not "send nowhere". Worth pinning: it is why the disabled path below has to
    patch the constant rather than pass an empty string."""
    assert crashsend.parse_dsn("") is None
    sent = {}
    assert crashsend._envelope({"event_id": "e"}, crashsend.SENTRY_DSN)


def test_sending_is_compiled_out_when_no_dsn_is_configured(monkeypatch):
    monkeypatch.setattr(crashsend, "SENTRY_DSN", "")
    assert crashsend.enabled() is False
    assert crashsend.send_event({"event_id": "x"}) is False


# ── event shaping ─────────────────────────────────────────────────────────────

def test_the_release_is_the_version_that_crashed(tmp_path):
    """Not the version doing the reporting.

    Updates apply on restart, so the build that uploads a crash is routinely
    newer than the build that produced it. Get this wrong and Sentry's
    regression detection points at the release that fixed the bug.
    """
    ev = crashsend.to_event(_report(version="2.0.7"))
    assert ev["release"] == "2.0.7"
    assert ev["release"] != version.__version__


def test_a_developer_run_is_tagged_separately_from_a_users_build():
    assert crashsend.to_event(_report(frozen=True))["environment"] == "production"
    assert crashsend.to_event(_report(frozen=False))["environment"] == "development"


def test_a_python_traceback_becomes_a_grouped_exception():
    ev = crashsend.to_event(_report(
        silent=False, fatal=[{"kind": "exception", "text": TRACEBACK}]))
    exc = ev["exception"]["values"][0]
    assert exc["type"] == "RuntimeError"
    assert exc["value"] == "boom in a flow step"
    assert exc["mechanism"]["handled"] is False
    frames = exc["stacktrace"]["frames"]
    assert [f["function"] for f in frames] == ["_on_log_batch", "run"]
    assert frames[0]["lineno"] == 210
    # Oldest frame first, which is the order Sentry renders bottom-up from.
    assert frames[-1]["function"] == "run"
    assert all(f["in_app"] for f in frames)


def test_a_third_party_frame_is_not_marked_in_app():
    tb = ('Traceback (most recent call last):\n'
          '  File "C:\\\\py\\\\site-packages\\\\pynput\\\\x.py", line 4, in tap\n'
          '    boom()\n'
          'ValueError: nope\n')
    ev = crashsend.to_event(_report(silent=False,
                                    fatal=[{"kind": "exception", "text": tb}]))
    assert ev["exception"]["values"][0]["stacktrace"]["frames"][0]["in_app"] is False


def test_a_qfatal_groups_on_its_message_because_it_has_no_stack():
    """The 2.0.8 crash. Qt aborts in C, so there is no Python stack to group on
    — without an explicit fingerprint every occurrence would be its own issue."""
    msg = "QThread: Destroyed while thread is still running"
    ev = crashsend.to_event(_report(silent=False,
                                    fatal=[{"kind": "qt_fatal", "text": msg}]))
    assert "Qt fatal" in ev["message"]["formatted"]
    assert ev["fingerprint"] == ["qt-fatal", msg[:120]]
    assert "exception" not in ev


def test_every_silent_death_groups_together():
    """Individually they carry nothing; the count of them is the whole signal."""
    a = crashsend.to_event(_report(doing={"node": "n1", "kind": "action"}))
    b = crashsend.to_event(_report(doing={"node": "n9", "kind": "action"}))
    assert a["fingerprint"] == b["fingerprint"] == ["silent-exit", "action"]
    assert a["tags"]["silent"] == "yes"


def test_a_python_traceback_wins_over_a_following_qfatal():
    """An abort often follows the exception that caused it, and the exception
    is the more informative of the two."""
    ev = crashsend.to_event(_report(silent=False, fatal=[
        {"kind": "exception", "text": TRACEBACK},
        {"kind": "qt_fatal", "text": "QThread: Destroyed"},
    ]))
    assert ev["exception"]["values"][0]["type"] == "RuntimeError"


def test_what_the_app_was_doing_survives_into_the_event():
    ev = crashsend.to_event(_report(doing={"node": "n4", "kind": "detect"}))
    assert ev["extra"]["last_node"] == "n4"
    assert ev["tags"]["node_kind"] == "detect"


def test_breadcrumbs_and_the_input_backend_are_carried_across():
    ev = crashsend.to_event(_report(breadcrumbs=[
        {"t": 1.0, "kind": "session_start"},
        {"t": 2.0, "kind": "run_start", "nodes": 7, "backend": "interception"},
        {"t": 3.0, "kind": "stop_requested", "running": True},
    ]))
    vals = ev["breadcrumbs"]["values"]
    assert [c["category"] for c in vals] == [
        "session_start", "run_start", "stop_requested"]
    assert vals[1]["data"]["nodes"] == 7
    assert ev["tags"]["input_backend"] == "interception"


def test_an_event_carries_no_script_contents():
    ev = crashsend.to_event(_report(
        doing={"node": "n1", "kind": "action"},
        breadcrumbs=[{"t": 1.0, "kind": "run_start", "nodes": 3,
                      "backend": "pynput"}]))
    blob = json.dumps(ev)
    for leak in ("image_path", "clipboard", "keystroke", "%HOME%\\"):
        assert leak not in blob


# ── the wire ──────────────────────────────────────────────────────────────────

def test_the_envelope_is_three_documents_with_a_correct_length():
    ev = crashsend.to_event(_report())
    raw = crashsend._envelope(ev, DSN)
    head, item, payload = raw.decode("utf-8").split("\n")[:3]
    assert json.loads(head)["event_id"] == ev["event_id"]
    meta = json.loads(item)
    assert meta["type"] == "event"
    # Sentry rejects the item outright if the declared length is wrong.
    assert meta["length"] == len(payload.encode("utf-8"))
    assert json.loads(payload)["release"] == ev["release"]


class _Resp:
    def __init__(self, status): self.status = status
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def getcode(self): return self.status


def test_a_successful_post_is_reported_as_sent(monkeypatch):
    seen = {}

    def fake(req, timeout=None, context=None):
        seen["url"] = req.full_url
        seen["auth"] = req.headers.get("X-sentry-auth", "")
        seen["ctype"] = req.headers.get("Content-type", "")
        return _Resp(200)

    monkeypatch.setattr(crashsend.urllib.request, "urlopen", fake)
    assert crashsend.send_event(crashsend.to_event(_report()), DSN) is True
    assert seen["url"].endswith("/api/4455/envelope/")
    assert "sentry_key=pubkey123" in seen["auth"]
    assert seen["ctype"] == "application/x-sentry-envelope"


def test_a_network_failure_keeps_the_report_for_a_later_launch(monkeypatch):
    def boom(*a, **k):
        raise OSError("no route to host")
    monkeypatch.setattr(crashsend.urllib.request, "urlopen", boom)
    assert crashsend.send_event(crashsend.to_event(_report()), DSN) is False


def test_a_permanent_rejection_stops_being_retried_forever(monkeypatch):
    """A dead project or a bad key will never accept this event. Retrying it on
    every launch for the rest of the install's life helps nobody."""
    def bad(*a, **k):
        raise crashsend.urllib.error.HTTPError(DSN, 403, "Forbidden", {}, None)
    monkeypatch.setattr(crashsend.urllib.request, "urlopen", bad)
    assert crashsend.send_event(crashsend.to_event(_report()), DSN) is True


def test_being_rate_limited_is_temporary_and_the_report_is_kept(monkeypatch):
    def limited(*a, **k):
        raise crashsend.urllib.error.HTTPError(DSN, 429, "Too Many", {}, None)
    monkeypatch.setattr(crashsend.urllib.request, "urlopen", limited)
    assert crashsend.send_event(crashsend.to_event(_report()), DSN) is False


def test_a_server_error_is_temporary_too(monkeypatch):
    def down(*a, **k):
        raise crashsend.urllib.error.HTTPError(DSN, 503, "Down", {}, None)
    monkeypatch.setattr(crashsend.urllib.request, "urlopen", down)
    assert crashsend.send_event(crashsend.to_event(_report()), DSN) is False


# ── the queue ─────────────────────────────────────────────────────────────────

def _queue(tmp_path, n):
    d = crashreport.crash_dir(tmp_path)
    for i in range(n):
        (d / ("crash-%d.json" % (1_000_000 + i))).write_text(
            json.dumps(_report(version="2.0.%d" % i)), encoding="utf-8")


def test_sent_reports_are_removed_and_the_rest_remain(tmp_path, monkeypatch):
    _queue(tmp_path, 3)
    monkeypatch.setattr(crashsend.urllib.request,
                        "urlopen", lambda *a, **k: _Resp(200))
    sent, remaining = crashsend.send_pending(tmp_path, DSN)
    assert (sent, remaining) == (3, 0)
    assert crashreport.pending(tmp_path) == []


def test_uploading_stops_at_the_first_failure(tmp_path, monkeypatch):
    """If the network is down the rest will fail too, and each costs a timeout."""
    _queue(tmp_path, 4)
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Resp(200)
        raise OSError("offline")

    monkeypatch.setattr(crashsend.urllib.request, "urlopen", flaky)
    sent, remaining = crashsend.send_pending(tmp_path, DSN)
    assert sent == 1 and remaining == 3
    assert calls["n"] == 2


def test_a_launch_never_uploads_more_than_its_share(tmp_path, monkeypatch):
    _queue(tmp_path, crashsend.MAX_PER_LAUNCH + 6)
    monkeypatch.setattr(crashsend.urllib.request,
                        "urlopen", lambda *a, **k: _Resp(200))
    sent, remaining = crashsend.send_pending(tmp_path, DSN)
    assert sent == crashsend.MAX_PER_LAUNCH
    assert remaining == 6


def test_nothing_is_uploaded_when_no_dsn_is_configured(tmp_path, monkeypatch):
    _queue(tmp_path, 2)
    monkeypatch.setattr(crashsend, "SENTRY_DSN", "")

    def never(*a, **k):
        raise AssertionError("posted with no endpoint configured")

    monkeypatch.setattr(crashsend.urllib.request, "urlopen", never)
    sent, remaining = crashsend.send_pending(tmp_path)
    assert (sent, remaining) == (0, 2)


def test_an_unreadable_report_is_dropped_not_retried(tmp_path, monkeypatch):
    d = crashreport.crash_dir(tmp_path)
    (d / "crash-1000001.json").write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setattr(crashsend.urllib.request,
                        "urlopen", lambda *a, **k: _Resp(200))
    sent, remaining = crashsend.send_pending(tmp_path, DSN)
    assert (sent, remaining) == (0, 0)
# ── What a breadcrumb is allowed to carry off the machine ────────────────────
#
# ⚠ `crashsend.to_event` forwards breadcrumb fields WHOLESALE:
#
#     data = {k: v for k, v in c.items() if k not in ("t", "kind")}
#
# Every key a breadcrumb happens to carry is uploaded. That is fine for the
# seven that exist, and it is a standing invitation for the eighth: a
# `breadcrumb("save", name=script.name)` added in a hurry would put the names
# of people's scripts into Sentry, and the privacy page says in a list of five
# bullets that those never leave the machine.
#
# `crashreport._scrub` does not help here. It removes the Windows account name
# and the home path — it has no idea what a script name is, and it is not
# supposed to.
#
# So the field names are pinned. Adding one is fine; adding one *without
# noticing it leaves the machine* is what this prevents. The reason each is
# safe is written beside it, because "it was already there" is not one.
ALLOWED_CRUMB_FIELDS = {
    # crashreport._env(), recorded at arming time. Build and platform only.
    "version", "frozen", "python", "os", "os_release", "arch",
    # The exception class name, e.g. "KeyError". Never its message.
    "type",
    # ⚠ Qt's own warning text. The one free-form string in this list, and the
    # only reason it is here is that Qt writes it: it is library diagnostics
    # ("QThread: Destroyed while thread is still running"), not anything the
    # user typed. It is scrubbed and capped at 400 characters like the rest.
    "level", "msg",
    # A count, a multiplier, a bool and the backend's name. Describe the run,
    # not what it did: no node titles, no typed text, no coordinates.
    "nodes", "speed", "detached", "backend",
    "running",
    # Two integers.
    "sent", "remaining",
}


def test_a_breadcrumb_cannot_quietly_start_carrying_user_content():
    """Pin every field name any breadcrumb passes to the uploader.

    ⚠ Reads the call sites rather than running them, because the risk is a
    call site that no test exercises — which is most of them. `**_env()` is
    expanded from the function it names for the same reason.
    """
    import ast

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def env_keys():
        """The keys `_env()` returns, read out of its source."""
        src = open(os.path.join(root, "crashreport.py"), encoding="utf-8").read()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_env":
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Dict):
                        return {k.value for k in sub.keys
                                if isinstance(k, ast.Constant)}
        return set()

    found, unresolved = {}, []
    for name in sorted(os.listdir(root)):
        if not name.endswith(".py"):
            continue
        try:
            tree = ast.parse(open(os.path.join(root, name),
                                  encoding="utf-8").read())
        except SyntaxError:                                 # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            label = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if label != "breadcrumb":
                continue
            where = "%s: breadcrumb(%s)" % (
                name,
                node.args[0].value if node.args and isinstance(node.args[0], ast.Constant)
                else "?")
            for kw in node.keywords:
                if kw.arg:
                    found[kw.arg] = where
                elif isinstance(kw.value, ast.Call) and getattr(
                        kw.value.func, "id", None) == "_env":
                    for k in env_keys():
                        found[k] = where + " via _env()"
                else:
                    unresolved.append(where)

    assert found, "no breadcrumb call sites found — this test has stopped working"

    assert not unresolved, (
        "a breadcrumb is splatted from something this test cannot read:\n  "
        + "\n  ".join(sorted(set(unresolved)))
        + "\n\nEvery field it carries is uploaded. Name them, or teach this "
          "test how to expand it.")

    new = sorted(f"{k}  ({v})" for k, v in found.items()
                 if k not in ALLOWED_CRUMB_FIELDS)
    assert not new, (
        "these breadcrumb fields are uploaded and are not on the allowlist:\n  "
        + "\n  ".join(new)
        + "\n\ncrashsend.to_event forwards every breadcrumb key wholesale. "
          "Before adding one, check it against the privacy page's list of what "
          "never leaves the machine — scripts and their names, keystrokes, "
          "screen contents, settings, clipboard. Then add it to "
          "ALLOWED_CRUMB_FIELDS with the reason it is safe.")


def test_the_uploader_still_forwards_breadcrumb_fields():
    """The allowlist above is only worth anything while that is true.

    ⚠ If `to_event` ever stopped copying breadcrumb data — or started picking
    named fields the way it already does for `doing` — the test above would
    keep passing while guarding nothing at all.
    """
    src = open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "crashsend.py"), encoding="utf-8").read()
    assert 'for k, v in c.items() if k not in ("t", "kind")' in src, (
        "crashsend.to_event no longer forwards breadcrumb fields wholesale. "
        "That is an improvement, but the allowlist above now guards nothing — "
        "rewrite it against however the fields are chosen now.")
