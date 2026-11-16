# Contributing to Macronaut

Thanks for looking. This is a one-person project, so replies are not instant,
but issues and pull requests are genuinely welcome.

## Three things to know before you start

**Macronaut is largely AI-authored, under my direction and with my name on it.**
The commit history makes that obvious, so you would find out anyway, and you
should know before you spend an evening on it. What it means in practice: the
code is heavily commented, often with the *reasoning* rather than a restatement
of the line below, and those comments are load-bearing. Several of them exist
because something broke in a way nobody would guess from the code. If a comment
warns you off a change, it is usually right, and if it turns out to be wrong,
say so in the PR — a comment that lies is a bug.

**It is Windows-only, and so is the test suite.** It uses global Windows hooks,
`pywin32`, and the Windows OCR engine. There is no Linux or macOS port planned
and a PR adding one is a bigger conversation than a PR.

**Every line that goes into the `.exe` is here**, and
`pyinstaller src/macronaut.spec` builds it. A clean clone runs the test suite
green.

## Getting set up

Windows 10 or 11, 64-bit, and Python 3.12.

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pip install pytest
python src/main.py
```

`pytest` is deliberately not in `requirements.txt` — that file is what a *user*
installs to run Macronaut, not what a contributor installs to test it.

## Running the tests

```powershell
python -m pytest -q
```

Upwards of eight hundred tests, about 35 seconds. They must all pass before a PR
is merged, and CI runs them on a clean Windows runner on every push, along with
building the `.exe` and self-testing the binary it produced.

(The exact figure is deliberately not written here. It said 748 for a while
after the suite had grown well past that, and a number nobody updates is worse
than no number — you would reasonably wonder what you had broken.)

`conftest.py` sets `QT_QPA_PLATFORM=offscreen` and redirects the settings
directory into a tempdir before any test module loads, so the suite never
touches your real `~/.macronaut` and never opens a window. Do not undo either.

Three traps, all of which have actually bitten:

- **Never call `close()` on `MainWindow` in a test.** Its close handler ends the
  process via `os._exit(0)`, so the test run stops dead, mid-suite, with no
  failure reported. Use `hide()`.
- **Never run the suite in the background or two copies at once.** It installs
  real global keyboard hooks; an abandoned run leaves them on your machine.
- **Run `pytest` from the repo root, not `pytest tests/`-by-hand habits** — and
  do not remove `pytest.ini`. The root contains stale full copies of the project
  in `backups/`, and without the `norecursedirs` line pytest imports *those*
  modules first and the entire suite fails at collection with an error that
  points at innocent files.

## Things that must not change

These are not style preferences. Changing one breaks software already on other
people's computers:

| Constant | Why |
| --- | --- |
| `licensing.PUBLIC_KEY_HEX` | Every licence key is verified against it. Change it and they all stop working. |
| `version.UPDATE_REPO` | Builds already out in the world fetch their updates from it. |
| The `v$version` release-tag format | The updater and the Scoop manifest's auto-update both parse it. |

Also: the GUI is PySide6, not PyQt5, and swapping the binding is not a
contribution anyone is looking for — `requirements.txt` explains why at length.

## Pull requests

- **One thing per PR.** A bug fix and a refactor in one branch is two reviews
  wearing a trenchcoat.
- **Say what broke and how you know it is fixed.** A test that fails before your
  change and passes after is the best possible version of that sentence.
- **Match the surrounding code.** Including the comment density — this codebase
  explains itself more than most, and a patch that does not stands out.
- **Do not bump the version or edit `docs/release-notes/`.** Releases are cut by
  `tools/release.py` and version bumps in a PR just cause conflicts.
- **New dependencies are a hard sell.** Every one is +MB on a download that
  already makes people hesitate, and one was removed for exactly that reason
  (see the OCR note in `requirements.txt`). If you need one, say why in the PR
  rather than in the diff.

If you are planning something large, open an issue first. I would rather talk
about it than have to turn down work you have already finished.

## Reporting bugs

Say what you did, what you expected, and what happened instead, plus your
Macronaut version (**Settings → About & legal**) and your Windows version. If
it crashed, the app will offer to send a report — agreeing to that helps, and
[the privacy policy](https://gtjevptje.github.io/Macronaut/privacy.html) says
exactly what is in one.

**Security bugs go to email, not to the issue tracker** — see
[SECURITY.md](SECURITY.md).

"My antivirus flags Macronaut" is a fine thing to open an issue about, but the
short answer is in the README: a global keyboard hook plus synthetic input plus
an unsigned PyInstaller binary matches every keylogger heuristic there is.

## Licence

Macronaut is GPL-3.0-or-later. By contributing you agree your contribution is
licensed the same way. There is no CLA and you are not asked to assign
copyright — you keep yours.

One consequence worth stating plainly: I do **not** ask for the right to
relicense your code proprietarily, which means I cannot. Contributions stay
GPL, in a GPL program.
