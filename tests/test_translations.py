"""The translated READMEs under `docs/` — French, German and Spanish.

They exist because the README answered in one language and the people who
use an auto clicker read many. They are deliberately **short and written by hand**, not generated: a
summary that says what the program is and where the download is, pointing at
the English README for everything else.

That choice has one cost, which is what this file is for. A hand-written page
is not rebuilt when the facts move, so it must not contain a fact that moves:
no version, no download size, no count of anything. And the three ways such a
page goes quietly wrong — a dead link, a claim the English page does not make,
a mention of a price — render perfectly either way.
"""
import os
import re

import pytest

import version

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# language code -> the sentence that tells a reader the app itself is English.
# Each is checked literally: a visitor who downloads on the strength of a
# translated page and meets an English window was misled by omission.
LANGUAGES = {
    "fr": "L'application elle-même est en anglais.",
    "de": "Die Anwendung selbst ist auf Englisch.",
    "es": "La aplicación en sí está en inglés.",
}

DOWNLOAD = version.REPO_URL + "/releases/latest/download/Macronaut.exe"


def _path(code):
    return os.path.join(REPO, "docs", "README.%s.md" % code)


def _read(path):
    with open(path, "rb") as fh:
        return fh.read().decode("utf-8")


def _links(text):
    """Every link target in the page, Markdown and HTML alike."""
    return (re.findall(r"\]\(([^)\s]+)\)", text)
            + re.findall(r'(?:href|src)="([^"]+)"', text))


@pytest.mark.parametrize("code", sorted(LANGUAGES))
def test_the_english_readme_offers_each_translation(code):
    readme = _read(os.path.join(REPO, "README.md"))
    assert "docs/README.%s.md" % code in readme, (
        "README.md does not link to the %s translation, so nothing can reach "
        "it — a page no link points at is a page nobody finds" % code)
    assert os.path.exists(_path(code)), "docs/README.%s.md is missing" % code


@pytest.mark.parametrize("code", sorted(LANGUAGES))
def test_a_translation_links_back_and_across(code):
    text = _read(_path(code))
    links = _links(text)
    assert "../README.md" in links, "no way back to the English README"
    for other in sorted(set(LANGUAGES) - {code}):
        assert "README.%s.md" % other in links, (
            "the %s page does not offer the %s one" % (code, other))
    assert "README.%s.md" % code not in links, "the page links to itself"


@pytest.mark.parametrize("code", sorted(LANGUAGES))
def test_every_link_in_a_translation_goes_somewhere(code):
    """Relative links are resolved against `docs/`, where the file lives.

    ⚠ The obvious mistake is copying a link from the English README, where
    `LICENSE` and `docs/hero.png` are right, into a file one directory down,
    where both are 404s that GitHub renders as perfectly ordinary links.
    """
    text = _read(_path(code))
    assert DOWNLOAD in text, "the translation does not offer the download"

    for target in _links(text):
        if target.startswith("https://"):
            assert target.startswith((version.REPO_URL,
                                      "https://gtjevptje.github.io/Macronaut/")), (
                "an external link that is not the project's own: %r" % target)
            continue
        assert not target.startswith(("http://", "mailto:")), target
        resolved = os.path.normpath(os.path.join(REPO, "docs", target))
        assert os.path.exists(resolved), (
            "docs/README.%s.md links to %r, which does not exist" % (code, target))


@pytest.mark.parametrize("code", sorted(LANGUAGES))
def test_a_translation_says_the_app_is_english(code):
    assert LANGUAGES[code] in _read(_path(code))


@pytest.mark.parametrize("code", sorted(LANGUAGES))
def test_a_translation_states_nothing_that_goes_stale(code):
    """No version, no size, no count: nothing rebuilds these pages."""
    # Tags (`<h1>`, `width="100%"`) and link targets are not prose.
    text = re.sub(r"<[^>]+>", "", _read(_path(code)))
    text = re.sub(r"\]\([^)]*\)", "]", text)

    # "GPL v3" is the licence's name, not a number that moves.
    prose = text.replace("GPL v3", "GPL")
    found = re.findall(r"\d[\d.,]*", prose)
    assert not found, (
        "docs/README.%s.md states numbers (%s). It is written by hand and "
        "nothing regenerates it, so a version, a size or a count in it will "
        "be wrong after the next release — put those in the English README, "
        "which is generated." % (code, ", ".join(found)))


@pytest.mark.parametrize("code", sorted(LANGUAGES))
def test_a_translation_mentions_no_paid_version(code):
    """Macronaut is presented as completely free — in every language."""
    text = _read(_path(code))
    for pattern in (r"\bPro\b", r"€", r"\$", r"\bPremium\b", r"\bEUR\b",
                    r"\bUSD\b"):
        assert not re.search(pattern, text), (
            "docs/README.%s.md matches %r" % (code, pattern))
