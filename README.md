<h1 align="center">Macronaut</h1>

<p align="center"><b>An auto clicker that watches the screen and decides what to do.</b><br>
Wait for something to appear, click it where it actually is, and keep going when
it doesn't. In any Windows app, without writing code.</p>

<p align="center">
  <a href="https://github.com/gtjevptje/Macronaut/releases/latest"><img alt="latest release" src="https://img.shields.io/github/v/release/gtjevptje/Macronaut?label=latest&color=7c5cff"></a>
  <a href="https://github.com/gtjevptje/Macronaut/releases/latest"><img alt="release date" src="https://img.shields.io/github/release-date/gtjevptje/Macronaut?label=released&color=7c5cff"></a>
  <a href="https://github.com/gtjevptje/Macronaut/releases"><img alt="downloads" src="https://img.shields.io/github/downloads/gtjevptje/Macronaut/Macronaut.exe?label=downloads&color=7c5cff"></a>
  <img alt="platform" src="https://img.shields.io/badge/Windows-10%20%7C%2011-7c5cff">
</p>

<h3 align="center">
  <a href="https://github.com/gtjevptje/Macronaut/releases/latest/download/Macronaut.exe">⬇&nbsp;&nbsp;Download Macronaut for Windows</a>
  <br><sub>Windows shows <b>“Windows protected your PC”</b> on first run —
  click <b>More info → Run anyway</b>. It does that for any unsigned app.</sub>
</h3>

<p align="center"><i>One 78 MB file. No installer, no account, no Python.
It updates itself.</i></p>

<p align="center"><a href="https://gtjevptje.github.io/Macronaut/"><b>macronaut website →</b></a></p>

<p align="center"><img alt="A Macronaut flow on the canvas" src="assets/hero.png" width="100%"></p>

## The first two minutes

1. **It opens as a plain auto-clicker.** Interval, button, how many times, where.
   Press Start. If that is all you came for, you are finished on this screen.
2. **5 scripts are already in the library** — a clicker that stops
   after a set number of clicks, one that clicks every 30 seconds to keep a
   session awake, one that types a block of text. Open one and press Play.
   Nothing to set up first.
3. **Advanced is one click away**, and it is a canvas: boxes for the things it
   does, wires for the order. Record yourself and it lands there as editable
   steps, or draw it yourself.

## What it does that a plain auto clicker cannot

**It waits for something, then clicks where that thing actually is.**
Point it at a button, a dialog or an icon. It searches the screen until that
appears — at a different DPI or window size too — and clicks it where it found
it, not at a coordinate that was right yesterday. Text works the same way, read
with Windows' own OCR, and so does a single pixel changing colour.

**It decides.** Every detection has a *found* branch and a *not found* branch,
so a script can handle the dialog that never appeared instead of hammering the
spot where it should have been. With loops and jumps to go with it.

**Keyboard and mouse in one script.** Clicks, keystrokes, chords, drags, scroll
flicks and held keys, in whatever order you need them — hold **W** to keep
moving while the mouse clicks, and everything is released when the run ends,
stops or crashes.

**It reaches programs that ignore ordinary input.** Three selectable input
backends: standard, SendInput scancodes, and the **Interception** kernel driver.
Many games discard injected input; the lower two look like a real keyboard to
them.

---

## The two faces

Macronaut automates clicking, typing and waiting in **any** Windows program —
including ones with no API, no scripting support and no plugins. It opens on
whichever face you closed it on, and each remembers its own size and position:
Basic parked in a corner beside the window it is clicking, Advanced as big as
you like.

<p align="center"><img alt="Macronaut's Basic face — an auto-clicker with click
interval, mouse button, repeat count, cursor position and a Start button"
src="assets/basic.png" width="440"></p>

If you have ever thought *"I do this exact sequence twenty times a day"*, that
is the thing Macronaut is for.

Coming from another tool? There are honest comparisons — each one saying what
the other still does better, because there is always something:
[**vs AutoHotkey**](https://gtjevptje.github.io/Macronaut/autohotkey-alternative.html) if you have been
meaning to learn it for a year, and
[**vs TinyTask**](https://gtjevptje.github.io/Macronaut/tinytask-alternative.html) if you have re-recorded
the same macro three times because a window moved.

Just want the clicker? [**The auto clicker page**](https://gtjevptje.github.io/Macronaut/auto-clicker.html)
covers setting one up, choosing an interval, and the two things nobody explains:
why antivirus complains about every tool in this category, and how you stop the
thing once it is running. There is also a
[**click speed test**](https://gtjevptje.github.io/Macronaut/click-speed-test.html) — no ads, no sign-up,
runs in the page.

## Free, all of it

No trial, no watermark, no account, no advertising, and no limit on what a
script can do. Every feature on this page is in the download.

## What it can do

**Just click something, over and over** *(the Basic face)*
- Interval in hours / minutes / seconds / milliseconds, or **Max** for as fast
  as the machine will go
- Left or right button, at the cursor or at a fixed X·Y you can pick off screen
- Repeat a set number of times, until you stop it, or until a stop-after timer
  runs out
- Human mode jitters the cursor so the movement is not machine-perfect
- Start and Stop, plus the same global hotkey the rest of the app uses

**Build a flow visually** *(the Advanced face)*
- Drop in **Click**, **Move**, **Drag**, **Scroll**, **Type text**, **Key press**,
  **Wait**, **Comment**, **Detect**, **If / Else**, **Loop** and **Go to**
- Wire them together and press ▶ — the running step lights up as it goes
- Group a region with a **comment box**; drag the box and everything on it moves
- Colour-code nodes, bend wires around each other, jump between sections
- Copy, paste, duplicate and bulk-edit across the whole flow or just a selection

**React to what's on screen**
- **Find an image** — wait for a button, a dialog or an icon to appear, then click it
- **Read text** — wait until specific words show up, using Windows' built-in OCR
- **Check a pixel** — the cheapest possible "is the panel open yet?"
- Every detection has a **✓ true** and a **✗ false** branch, so a flow can handle
  the case where the thing never appears

**Record instead of build**
- Hit ⏺ and work normally — clicks, keystrokes, chords, scroll flicks and
  click-and-drag gestures all land on the canvas as editable nodes
- Held keys stay held; a swipe is recorded as a swipe, not as a click

**Hold keys down**
- Press and release are separate actions, so a flow can hold **W** to keep moving
  while it clicks with the mouse
- Anything still held is always released when the run ends, stops or crashes

**Run it your way**
- Global start/stop hotkey that works while another window is focused
- Bind **different scripts to different keys** — one keypress launches one flow
- Playback speed multiplier, loop counts, per-step delays
- Timing estimates and a run timeline, measured on your machine rather than guessed

**Reach programs that ignore normal input**
- Three selectable input backends: standard, **SendInput scancodes**, and the
  **Interception** kernel driver
- Some programs — games especially — discard ordinary injected input. The lower
  backends look like a real keyboard to them.

**Stays out of the way**
- Single 78 MB `.exe`, no installer, no Python, no admin needed to run
- Checks for updates and applies them on restart, verified by SHA-256
- Always-on-top mode, system tray icon, dark and light themes
- Light when idle: about 0.1% of one CPU core while it sits waiting (measured on 2.3.5)

## Privacy

Macronaut has no accounts and no telemetry. Automations run entirely on your
machine. The one thing that ever leaves
your computer is a crash report, and only if you opt in; those carry the error,
the version, your Windows version and which step was running, never your
scripts, your keystrokes, anything on screen, or your name.

The [full privacy policy](https://gtjevptje.github.io/Macronaut/privacy.html) says exactly what is in a
crash report, what is stripped out of it before it is written to disk, and how
to have one deleted.

## Requirements

Windows 10 or 11, 64-bit. Nothing else — everything is inside the `.exe`.

> **On first run, Windows may warn you.** Macronaut is not code-signed yet, so
> SmartScreen shows *"Windows protected your PC"* for new downloads. Click
> **More info → Run anyway**. Signing is on the roadmap — and in the meantime
> you do not have to take my word for what is in the file:
> [the source is public](https://github.com/gtjevptje/macronaut-source), and `pyinstaller macronaut.spec`
> builds the program from it. Your build will not have the same SHA-256 as the
> download — PyInstaller stamps timestamps into its output, so no two builds
> match byte for byte, not even two of mine. What you can check is that nothing
> goes into the `.exe` that is not in that repository.

## A word of warning

Macronaut sends real keyboard and mouse input. **Many online games and services
forbid automation in their terms of service, and using it against them can cost
you your account.** That is your call to make — read their rules first. There is
no warranty of any kind; see sections 15 and 16 of the
[licence](https://github.com/gtjevptje/macronaut-source/blob/main/LICENSE).

## Licence

**Free software, under the GNU General Public License v3.0 or later.**
Copyright © 2026 Gerben van Poucke. `SPDX-License-Identifier: GPL-3.0-or-later`

Run it, read it, change it, pass it on. The one obligation is that a modified
version you distribute — as source or as a built `.exe` — comes with its changes
published under the same licence.

**[The complete source is here](https://github.com/gtjevptje/macronaut-source.)** Macronaut was proprietary
until 30 August 2026, and the reason it is not any more is the paragraph above
this one: an unsigned executable that installs a global keyboard hook is asking
for a lot of trust, and publishing the code is the only answer to that which
does not ask for more. `pyinstaller macronaut.spec` builds it from that source.

⚠ **Which builds this covers.** Macronaut was proprietary until 30 August 2026.
Releases from **2.3.3 onward are GPL**; builds published before that date remain
under the EULA they shipped with, and relicensing does not reach backwards into
copies already downloaded. The `LICENSE` file in this repository is the licence
of what is here now.

The licence and the
[third-party notices](https://github.com/gtjevptje/macronaut-source/blob/main/THIRD-PARTY-NOTICES.md) also ship
inside the app, under **Settings → About & legal**, next to a link back to the
source.

## Getting in touch

**[gerbenvanpoucke0@gmail.com](mailto:gerbenvanpoucke0@gmail.com)** — bug reports. It is one person, not a ticket queue.

---

<p align="center">
  <a href="https://github.com/gtjevptje/Macronaut/releases/latest/download/Macronaut.exe"><b>⬇&nbsp;&nbsp;Download Macronaut for Windows</b></a>
</p>
