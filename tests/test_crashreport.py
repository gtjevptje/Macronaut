"""Headless tests for crash capture (crashreport.py).

The behaviours worth pinning here are the ones that would quietly poison the
data rather than raise: arming during a non-session run, blaming a crash on the
wrong version, or harvesting a session that is still alive. A crash reporter
that lies is worse than none, because it sends you hunting bugs that are not
there.

The end-to-end proof that this catches a real qFatal is a subprocess exercise,
not a unit test — see the module docstring in crashreport.py.
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import crashreport
import version


@pytest.fixture(autouse=True)
def _clean():
    """Every test arms and disarms in isolation — module state is global."""
    yield
    try:
        crashreport.disarm()
    except Exception:
        pass


def _sessions(d):
    return sorted(p.name for p in (d / crashreport.DIR_NAME).glob("session-*.json"))


# ── arming ────────────────────────────────────────────────────────────────────

def test_arming_writes_a_session_file_and_a_clean_exit_removes_it(tmp_path):
    assert crashreport.install(tmp_path, argv=["Macronaut.exe"]) is True
    assert len(_sessions(tmp_path)) == 1
    crashreport.disarm()
    assert _sessions(tmp_path) == []
    # And nothing to harvest: a clean exit is not a crash.
    assert crashreport.harvest(tmp_path) == []


@pytest.mark.parametrize("flag", ["--apply-update", "--selftest"])
def test_it_refuses_to_arm_for_a_non_session_run(tmp_path, flag):
    """The update path runs on EVERY update and exits without a GUI.

    Arming there would manufacture a crash report on every single successful
    update — at this project's release cadence, enough false positives to bury
    the real ones.
    """
    assert crashreport.install(tmp_path, argv=["Macronaut.exe", flag]) is False
    assert crashreport.is_armed() is False
    assert _sessions(tmp_path) == []


def test_arming_twice_is_harmless(tmp_path):
    assert crashreport.install(tmp_path, argv=["x"]) is True
    assert crashreport.install(tmp_path, argv=["x"]) is True
    assert len(_sessions(tmp_path)) == 1


def test_disarming_when_never_armed_does_nothing(tmp_path):
    crashreport.disarm()          # must not raise
    assert crashreport.is_armed() is False


def test_a_failed_arming_leaves_nothing_that_looks_like_a_crash(tmp_path,
                                                                monkeypatch):
    """Arming writes the session file early, then opens handles that can fail.

    If the failure path leaves that file behind, disarm() will not remove it —
    it only acts when armed — so the app runs fine, exits cleanly, and the NEXT
    launch harvests it as a crash with no exception and no signal: `silent`,
    the bucket reserved for OOM and hard kills. One machine that cannot arm
    would file a phantom crash in the highest-signal category every launch.
    """
    real_open = crashreport.open if hasattr(crashreport, "open") else open

    def boom(path, *a, **kw):
        if str(path).endswith((".log", ".native")):
            raise OSError("no handles for you")
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", boom)
    assert crashreport.install(tmp_path, argv=["x"]) is False
    monkeypatch.undo()

    assert crashreport.is_armed() is False
    assert _sessions(tmp_path) == []
    assert crashreport.harvest(tmp_path) == []


# ── harvesting ────────────────────────────────────────────────────────────────

def _abandon(tmp_path, *, ver="1.9.9", pid=None, crumbs=(), state=None,
             fatal=None, started=None, dropped=None):
    """Fake a session that died without disarming."""
    d = crashreport.crash_dir(tmp_path)
    pid = pid if pid is not None else 999_999_998
    started = time.time() if started is None else started
    base = d / ("session-%d-%d" % (pid, int(time.time() * 1000)))
    base.with_suffix(".json").write_text(json.dumps({
        "schema": crashreport.SCHEMA, "pid": pid, "started": started,
        "version": ver, "frozen": True, "os": "Windows",
    }), encoding="utf-8")
    if dropped is not None:
        base.with_suffix(".dropped").write_text(
            json.dumps({"dropped": dropped}), encoding="utf-8")
    if crumbs:
        base.with_suffix(".log").write_text(
            "".join(json.dumps({"kind": c}) + "\n" for c in crumbs),
            encoding="utf-8")
    if state:
        base.with_suffix(".state").write_text(json.dumps(state), encoding="utf-8")
    if fatal:
        base.with_suffix(".fatal").write_text(
            json.dumps({"kind": "qt_fatal", "text": fatal}) + "\n",
            encoding="utf-8")
    return base


def test_an_abandoned_session_becomes_a_report(tmp_path):
    _abandon(tmp_path, crumbs=["session_start", "run_start"],
             state={"node": "n7", "kind": "action"},
             fatal="QThread: Destroyed while thread is still running")
    out = crashreport.harvest(tmp_path)
    assert len(out) == 1
    rep = crashreport.load(out[0])
    assert [c["kind"] for c in rep["breadcrumbs"]] == ["session_start", "run_start"]
    assert rep["doing"]["node"] == "n7"
    assert "QThread" in rep["fatal"][0]["text"]
    assert rep["silent"] is False
    # The session's working files are consumed, so it is never reported twice.
    assert _sessions(tmp_path) == []
    assert crashreport.harvest(tmp_path) == []


def test_a_crash_is_blamed_on_the_version_that_crashed(tmp_path):
    """The trap that auto-update-on-restart sets.

    The launch that harvests a crash is frequently already a NEWER build,
    because the update applied on the restart that followed the crash. Reading
    the running version at harvest time would credit every crash to the release
    that fixed it — which is precisely backwards.
    """
    _abandon(tmp_path, ver="2.0.7")
    rep = crashreport.load(crashreport.harvest(tmp_path)[0])
    assert rep["version"] == "2.0.7"
    assert rep["version"] != version.__version__


def test_a_session_whose_process_is_still_alive_is_left_alone(tmp_path):
    """A second running instance must not have its session file stolen."""
    _abandon(tmp_path, pid=os.getpid())
    assert crashreport.harvest(tmp_path) == []
    assert len(_sessions(tmp_path)) == 1


def test_an_unreadable_pid_is_treated_as_alive(tmp_path, monkeypatch):
    """When we cannot tell, waiting a launch beats destroying the evidence."""
    monkeypatch.setattr(crashreport, "_pid_alive", lambda pid: True)
    _abandon(tmp_path)
    assert crashreport.harvest(tmp_path) == []


def test_a_recycled_pid_does_not_hide_a_crash_forever(tmp_path, monkeypatch):
    """PIDs get reused, and "its PID is alive" must not defer a report forever.

    Reboot after a crash and the number that belonged to the dead run can
    easily belong to a system process. Without an age fallback that session is
    skipped on every launch for the life of the install: the crash is never
    reported and its files are never cleaned up — which from the outside is
    indistinguishable from the app never crashing at all.
    """
    monkeypatch.setattr(crashreport, "_pid_alive", lambda pid: True)
    _abandon(tmp_path, started=time.time() - crashreport.STALE_SESSION_S - 60)
    out = crashreport.harvest(tmp_path)
    assert len(out) == 1
    assert _sessions(tmp_path) == []          # and the files are reclaimed


def test_a_recent_session_with_a_live_pid_is_still_left_alone(tmp_path,
                                                              monkeypatch):
    """The other half: the age fallback must not steal from a live instance."""
    monkeypatch.setattr(crashreport, "_pid_alive", lambda pid: True)
    _abandon(tmp_path, started=time.time() - 30)
    assert crashreport.harvest(tmp_path) == []
    assert len(_sessions(tmp_path)) == 1


def test_a_session_with_no_usable_timestamp_ages_out_immediately(tmp_path,
                                                                 monkeypatch):
    """An undatable session file is junk that would otherwise live forever."""
    monkeypatch.setattr(crashreport, "_pid_alive", lambda pid: True)
    base = _abandon(tmp_path)
    info = json.loads(base.with_suffix(".json").read_text(encoding="utf-8"))
    info.pop("started")
    base.with_suffix(".json").write_text(json.dumps(info), encoding="utf-8")
    assert len(crashreport.harvest(tmp_path)) == 1


def test_a_death_with_no_exception_at_all_is_flagged_silent(tmp_path):
    """The OOM / hard-kill shape: nothing in the process noticed it ended."""
    _abandon(tmp_path, state={"node": "n2"})
    rep = crashreport.load(crashreport.harvest(tmp_path)[0])
    assert rep["silent"] is True
    assert rep["doing"]["node"] == "n2"
    assert "closed without shutting down" in crashreport.summarize(rep)


def test_our_own_live_session_is_never_harvested(tmp_path):
    crashreport.install(tmp_path, argv=["x"])
    assert crashreport.harvest(tmp_path) == []
    assert len(_sessions(tmp_path)) == 1


# ── bounded by construction ───────────────────────────────────────────────────

def test_breadcrumbs_stop_at_the_cap(tmp_path):
    """The whole point of the 2.0.8 fix was 'bounded, and says so'."""
    crashreport.install(tmp_path, argv=["x"])
    for i in range(crashreport.MAX_BREADCRUMBS + 500):
        crashreport.breadcrumb("tick", i=i)
    path = crashreport._session_path.with_suffix(".log")
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) <= crashreport.MAX_BREADCRUMBS


def test_the_overflow_is_recorded_rather_than_swallowed(tmp_path):
    """"Bounded" is only half of it — a truncated trail must admit it is one.

    A breadcrumb list that stops at the cap and says nothing reads as a
    complete account of a quiet session, when it may be the first 300 entries
    of a very loud one.
    """
    crashreport.install(tmp_path, argv=["x"])
    for i in range(crashreport.MAX_BREADCRUMBS + 50):
        crashreport.breadcrumb("tick", i=i)
    assert crashreport._crumb_dropped >= 50
    marker = crashreport._session_path.with_suffix(".dropped")
    assert marker.exists(), "overflow was counted into nothing"
    assert json.loads(marker.read_text(encoding="utf-8"))["dropped"] >= 1


def test_a_truncated_trail_says_so_in_the_report(tmp_path):
    _abandon(tmp_path, crumbs=["session_start"], dropped=812)
    rep = crashreport.load(crashreport.harvest(tmp_path)[0])
    assert rep["breadcrumbs_dropped"] == 812

    import crashsend
    assert crashsend.to_event(rep)["extra"]["breadcrumbs_dropped"] == 812


def test_an_intact_trail_claims_no_drops(tmp_path):
    _abandon(tmp_path, crumbs=["session_start"])
    rep = crashreport.load(crashreport.harvest(tmp_path)[0])
    assert rep["breadcrumbs_dropped"] == 0
    assert "breadcrumbs_dropped" not in crashsend_extra(rep)


def crashsend_extra(rep):
    import crashsend
    return crashsend.to_event(rep)["extra"]


def test_the_right_now_marker_overwrites_instead_of_appending(tmp_path,
                                                              monkeypatch):
    """state() must cost the same whether it is called 10 times or 10 million.

    This is the guard against reintroducing the 2.0.8 flood on the filesystem:
    a node-entered signal fires hundreds of thousands of times a second.
    """
    crashreport.install(tmp_path, argv=["x"])
    monkeypatch.setattr(crashreport, "_STATE_MIN_INTERVAL", 0.0)
    for i in range(1000):
        crashreport.state(node="n%d" % i)
    path = crashreport._session_path.with_suffix(".state")
    rec = json.loads(path.read_text(encoding="utf-8"))
    assert rec["node"] == "n999"                 # the latest wins
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_the_marker_throttles_itself(tmp_path):
    crashreport.install(tmp_path, argv=["x"])
    for i in range(500):
        crashreport.state(node="n%d" % i)
    rec = json.loads(
        crashreport._session_path.with_suffix(".state").read_text(encoding="utf-8"))
    assert rec["node"] == "n0"                   # the rest were inside the window


def test_only_the_newest_reports_are_kept(tmp_path):
    d = crashreport.crash_dir(tmp_path)
    for i in range(crashreport.MAX_PENDING + 12):
        (d / ("crash-%d.json" % (1_000_000 + i))).write_text("{}", encoding="utf-8")
    crashreport._trim(d)
    kept = crashreport.pending(tmp_path)
    assert len(kept) == crashreport.MAX_PENDING
    assert kept[-1].name == "crash-%d.json" % (1_000_000 + crashreport.MAX_PENDING + 11)


# ── privacy ───────────────────────────────────────────────────────────────────

def test_the_users_name_never_reaches_a_report(tmp_path, monkeypatch):
    """A frozen traceback is full of C:\\Users\\<real name>\\... and that name
    carries no diagnostic value whatsoever."""
    monkeypatch.setenv("USERNAME", "Gerben")
    home = str(crashreport.Path.home())
    text = crashreport._scrub("File %s\\app\\main.py line 3, user Gerben" % home)
    assert "Gerben" not in text
    assert home not in text
    assert "%USER%" in text and "%HOME%" in text


def test_a_short_username_is_not_used_to_shred_unrelated_words(monkeypatch):
    monkeypatch.setenv("USERNAME", "ab")
    assert crashreport._scrub("abort in abstract label") == "abort in abstract label"


def test_breadcrumb_values_are_scrubbed_and_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("USERNAME", "Gerben")
    crashreport.install(tmp_path, argv=["x"])
    crashreport.breadcrumb("open", path="C:/x/Gerben/s.json", blob="y" * 5000)
    rec = json.loads(
        crashreport._session_path.with_suffix(".log")
        .read_text(encoding="utf-8").strip().splitlines()[-1])
    assert "Gerben" not in rec["path"]
    assert len(rec["blob"]) <= 400


def test_a_report_carries_no_script_contents(tmp_path):
    """Only shape and identity of the run, never what the user automates."""
    _abandon(tmp_path, crumbs=["run_start"], state={"node": "n1", "kind": "action"})
    rep = crashreport.load(crashreport.harvest(tmp_path)[0])
    blob = json.dumps(rep)
    for leak in ("image_path", "text", "keys", "clipboard"):
        assert leak not in blob


# ── nothing here may ever break startup ───────────────────────────────────────

def test_a_broken_data_directory_does_not_raise(tmp_path):
    """Best-effort by design: a crash reporter that stops the app from starting
    is strictly worse than no crash reporter."""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    assert crashreport.install(blocker, argv=["x"]) is False
    assert crashreport.harvest(blocker) == []
    crashreport.breadcrumb("tick")     # must not raise
    crashreport.state(node="n1")       # must not raise
