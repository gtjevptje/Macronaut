"""Measured node durations — the sidecar that makes the Time axis honest."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import runstats             # noqa: E402
import settings as settings_mod   # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Never touch the real ~/.macronaut.

    Without this the suite would fold junk timings into whatever the user has
    actually been running, and a test would "pass" against their data.
    """
    monkeypatch.setattr(settings_mod, "data_dir", lambda: tmp_path)
    return tmp_path


def test_a_recorded_run_comes_back_as_a_median():
    runstats.record("demo", {"n1": 100})
    runstats.record("demo", {"n1": 300})
    runstats.record("demo", {"n1": 200})
    assert runstats.medians("demo")["n1"] == 200


def test_one_freak_sample_does_not_move_the_median():
    # The reason this is a median and not a mean: one Detect that happened to
    # wait out a slow game launch must not redraw the whole timeline.
    for v in (100, 110, 105, 95, 30000):
        runstats.record("demo", {"n1": v})
    assert runstats.medians("demo")["n1"] == 105


def test_only_the_last_few_runs_are_kept_newest_first():
    for i in range(runstats.KEEP + 6):
        runstats.record("demo", {"n1": i})
    samples = runstats.load("demo")["n1"]
    assert len(samples) == runstats.KEEP
    # Newest first, so an edited flow stops being described by what it used to
    # do after KEEP runs rather than never.
    assert samples[0] == runstats.KEEP + 5


def test_nothing_recorded_yet_is_empty_not_an_error():
    assert runstats.load("never-run") == {}
    assert runstats.medians("never-run") == {}


def test_a_corrupt_file_reads_as_no_measurements(isolated):
    (isolated / "runstats").mkdir(exist_ok=True)
    (isolated / "runstats" / "demo.json").write_text("{not json", encoding="utf-8")
    assert runstats.medians("demo") == {}
    # ...and recording over it repairs rather than raises
    runstats.record("demo", {"n1": 42})
    assert runstats.medians("demo") == {"n1": 42}


def test_a_flow_name_becomes_a_safe_filename():
    assert runstats.key_for("My Flow / v2") == "My-Flow-v2"
    assert runstats.key_for("") == "_unsaved"
    assert runstats.key_for("   ") == "_unsaved"
    assert runstats.key_for("...") == "_unsaved"
    assert len(runstats.key_for("x" * 300)) <= 80


def test_two_flows_do_not_share_measurements():
    # A Detect in one script says nothing about a Detect in another, and node
    # ids repeat across flows (n1, n2, ...) so a shared bucket would silently
    # describe the wrong nodes.
    runstats.record("alpha", {"n1": 100})
    runstats.record("beta", {"n1": 9000})
    assert runstats.medians("alpha")["n1"] == 100
    assert runstats.medians("beta")["n1"] == 9000


def test_recording_nothing_leaves_what_is_there(isolated):
    runstats.record("demo", {"n1": 100})
    assert runstats.record("demo", {}) == {"n1": 100}


def test_junk_values_are_skipped_rather_than_stored():
    runstats.record("demo", {"n1": "banana", "n2": -5, "n3": 12.7})
    assert runstats.medians("demo") == {"n3": 13}


def test_the_file_is_plain_readable_json(isolated):
    runstats.record("demo", {"n1": 100})
    raw = json.loads((isolated / "runstats" / "demo.json").read_text(encoding="utf-8"))
    assert raw == {"nodes": {"n1": [100]}}
