"""The one-time "would you star it?" question — `star_ui.py`.

Every rule in that module's docstring is a promise to the person using the app,
and each has a test here that fails when the promise is broken.
"""
import types

import pytest

import star_ui
import version


class _Settings:
    """The two fields and the save the module touches; counts its saves."""

    def __init__(self, launch_count=0, star_asked=False):
        self.s = types.SimpleNamespace(launch_count=launch_count,
                                       star_asked=star_asked)
        self.saves = 0
        self.asked_at_save = []

    def save(self):
        self.saves += 1
        self.asked_at_save.append(self.s.star_asked)


def _stats(sessions=1):
    return types.SimpleNamespace(sessions=[object()] * sessions)


@pytest.fixture
def shown_window(qapp):
    from PySide6.QtWidgets import QWidget
    w = QWidget()
    w.show()
    qapp.processEvents()
    yield w
    w.hide()


@pytest.fixture
def browser():
    star_ui.QDesktopServices.opened.clear()
    return star_ui.QDesktopServices.opened


def test_nobody_is_asked_on_their_first_launches():
    s = _Settings()
    for launch in range(1, star_ui.ASK_FROM_LAUNCH):
        star_ui.note_launch(s)
        assert s.s.launch_count == launch
        assert not star_ui.due(s, _stats()), f"asked on launch {launch}"
    star_ui.note_launch(s)
    assert star_ui.due(s, _stats())


def test_somebody_who_never_ran_anything_is_not_asked():
    s = _Settings(launch_count=50)
    assert not star_ui.due(s, _stats(sessions=0))
    assert star_ui.due(s, _stats(sessions=1))


def test_it_is_asked_once_and_the_answer_is_recorded_before_the_box_shows(
        shown_window, browser):
    s = _Settings(launch_count=star_ui.ASK_FROM_LAUNCH)
    box = star_ui.ask(shown_window, s, _stats())
    assert box is not None and box.isVisible()
    # Written before show(): a crash or a kill with the box up must not
    # bring the question back.
    assert s.s.star_asked is True and s.asked_at_save == [True]
    assert not box.isModal(), "the question must never block the app"

    assert star_ui.ask(shown_window, s, _stats()) is None, "asked twice"
    assert not star_ui.due(s, _stats())
    assert browser == [], "showing the box opened a page nobody clicked"
    box.close()


def test_a_hidden_window_does_not_spend_the_one_ask(qapp, browser):
    from PySide6.QtWidgets import QWidget
    hidden = QWidget()                       # started to the tray
    s = _Settings(launch_count=star_ui.ASK_FROM_LAUNCH)
    assert star_ui.ask(hidden, s, _stats()) is None
    assert s.s.star_asked is False and s.saves == 0


def test_it_stands_down_while_another_question_is_on_screen(
        qapp, shown_window, monkeypatch):
    """Recovery, an update and crash consent are about the user's own work."""
    monkeypatch.setattr(star_ui.QApplication, "activeModalWidget",
                        staticmethod(lambda: object()))
    s = _Settings(launch_count=star_ui.ASK_FROM_LAUNCH)
    assert star_ui.ask(shown_window, s, _stats()) is None
    assert s.s.star_asked is False, "stood down, but spent the ask anyway"


@pytest.mark.parametrize("label, expected", [
    (star_ui.STAR_LABEL, [version.REPO_URL]),
    (star_ui.FEEDBACK_LABEL, [version.ISSUES_URL]),
    (star_ui.LATER_LABEL, []),
])
def test_each_button_does_what_it_says(shown_window, browser, label, expected):
    s = _Settings(launch_count=star_ui.ASK_FROM_LAUNCH)
    box = star_ui.ask(shown_window, s, _stats())
    button = next(b for b in box.buttons() if b.text() == label)
    button.click()
    assert browser == expected


def test_a_broken_settings_file_cannot_break_startup(shown_window):
    class Broken:
        @property
        def s(self):
            raise OSError("settings unreadable")

        def save(self):
            raise OSError("disk full")

    star_ui.schedule(shown_window, Broken(), _stats())     # must not raise
    assert star_ui.ask(shown_window, Broken(), _stats()) is None


def test_the_question_says_it_is_the_only_one_and_names_no_price():
    text = (star_ui.TITLE + " " + star_ui.TEXT).lower()
    assert "only time" in text
    for word in ("pro", "buy", "price", "paid", "licence key", "upgrade",
                 "donat"):
        assert word not in text.replace("problem", ""), \
            f"the star question mentions {word!r}"


def test_people_are_sent_to_the_name_they_know_not_the_updaters():
    """`UPDATE_REPO` is the same repository under its first name.

    The updater must keep that name forever; a person must never be shown it.
    """
    assert version.REPO_URL == "https://github.com/gtjevptje/Macronaut"
    assert version.REPO_URL.split("/")[3] == version.UPDATE_REPO.split("/")[0]
    assert "macronaut-releases" not in version.REPO_URL
    assert version.ISSUES_URL.startswith(version.REPO_URL + "/")


def test_the_settings_know_both_fields_and_start_unasked():
    import settings
    fresh = settings.AppSettings()
    assert fresh.launch_count == 0 and fresh.star_asked is False
