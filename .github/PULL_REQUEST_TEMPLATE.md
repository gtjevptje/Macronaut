<!-- Thanks for this. The suite and a clean-runner build run automatically on
     every push, so you do not need to prove they pass — just fill in the bits
     CI cannot know. Delete any section that does not apply. -->

## What this changes

<!-- One or two sentences. If it fixes an issue, "Fixes #12" here closes it
     automatically on merge. -->

## Why

<!-- What was wrong, or what could not be done before. The reasoning is the part
     that is hard to recover later — this codebase comments the *why* for the
     same reason. -->

## How it was checked

<!-- CI runs the suite and builds the .exe. What it cannot do is use the
     program: it has no screen, no mouse, and no other application to automate.
     So: did you run it? Against what? -->

- [ ] `python -m pytest -q` passes locally
- [ ] I ran the app and used the thing I changed
- [ ] Added or updated a test — or it genuinely cannot be tested headlessly,
      and I have said why below

## Anything a reviewer should know

<!-- Trade-offs you made, a comment you deliberately contradicted (please say
     so — those comments are load-bearing and one being wrong is worth
     knowing), or a bit you were unsure about. "I couldn't work out how to test
     X" is a useful thing to write here, not an admission. -->

---

<!-- By opening this PR you agree your contribution is licensed GPL-3.0-or-later,
     the same as the rest of Macronaut. There is no CLA and you keep your
     copyright. Please don't bump the version or edit RELEASE-NOTES-*.md —
     releases are cut by release.py and version bumps only cause conflicts. -->
