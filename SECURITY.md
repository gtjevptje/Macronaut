# Security policy

Macronaut installs a global keyboard hook, sends synthetic mouse and keyboard
input, and — if you install the optional Interception driver — talks to a
kernel-mode driver. It is exactly the kind of program where a security bug
matters, so please report one rather than filing it as an ordinary issue.

## Reporting a vulnerability

**Email gerbenvanpoucke0@gmail.com.** Put "Macronaut security" in the subject
so it does not get lost.

Please do not open a public issue for something exploitable. There is no bug
bounty — this is a one-person project — but you will
get credit in the release notes if you want it, and a straight answer if you
do not.

What helps, in rough order of usefulness: what an attacker gets, the steps to
reproduce it, the Macronaut version, and your Windows version. A proof of
concept is welcome and is not required.

### What to expect

It is one person in a European timezone, not a team with a rota. Realistically:
a first reply within a week, and an honest estimate rather than a promise
after that. If a week passes with silence, assume the mail went astray and
send it again — that is far more likely than it being ignored.

If a fix needs a release, it goes out as a normal update; Macronaut checks for
updates on start and applies them on restart, so users get it without doing
anything. The release notes will say what was fixed. If a report turns out not
to be a vulnerability, you will be told why rather than left waiting.

## Supported versions

The latest release, and only the latest release. Macronaut is a single
executable that updates itself, there are no long-term-support branches, and
pretending otherwise would be theatre.

The version you have is under **Settings → About & legal**; the newest is
always at
<https://github.com/gtjevptje/Macronaut/releases/latest>.

## Scope

**In scope** — anything that lets code or input reach a place it should not:

- Code execution from opening or importing a script file (`.json`) that someone
  else made
- The updater accepting a build it should not: the signature or SHA-256 check
  being bypassable, or the update being fetchable over a channel an attacker
  can control
- Licence verification being bypassable in a way that is a *flaw* rather than
  the obvious consequence of the code being public — see below
- A crash report carrying something it should not. The redaction lives in
  `crashreport.py`; the [privacy policy](https://gtjevptje.github.io/Macronaut/privacy.html)
  states what is meant to be in one, and a way to get more than that into one
  is a bug worth reporting
- Anything writing outside `%USERPROFILE%\.macronaut` that should not, or
  escalating privileges

**Not in scope**, and please do not spend your time on these:

- **That Macronaut can automate other programs.** That is the entire product.
  Sending clicks to another window is the feature, not a sandbox escape.
- **That antivirus flags it.** A global keyboard hook plus synthetic input plus
  an unsigned PyInstaller binary is a heuristic match for a keylogger. It is a
  false positive; a report about it is a support question, and welcome as an
  ordinary issue.
- **Changing what your own copy does.** The source is GPL and everything runs
  on your own machine, so of course you can. That is a property of open-source
  software, not a vulnerability.
- **The optional Interception kernel driver itself.** It is
  [a separate project](https://github.com/oblitum/Interception); report bugs in
  the driver to them. How Macronaut *uses* it is in scope.
- Anything needing an attacker who already runs code as you. At that point they
  do not need Macronaut.

## What Macronaut does not have

Worth stating, because it removes most of the usual attack surface: there is no
server, no account, no login, no session, no database and no telemetry. The app
makes exactly two outbound requests — an update check, and a crash report you
have explicitly agreed to. Licences are verified offline. There is no
infrastructure of mine to attack, only the program on your machine.
