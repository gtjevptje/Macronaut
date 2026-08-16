<h1 align="center">Macronaut</h1>

<p align="center"><b>The AutoHotkey alternative without the code.</b><br>
Automate repetitive tasks in any Windows app — draw the flow, press play.</p>

<p align="center">
  <a href="https://github.com/gtjevptje/Macronaut/releases/latest"><img alt="latest release" src="https://img.shields.io/github/v/release/gtjevptje/Macronaut?label=latest&color=7c5cff"></a>
  <a href="https://github.com/gtjevptje/Macronaut/releases/latest"><img alt="release date" src="https://img.shields.io/github/release-date/gtjevptje/Macronaut?label=released&color=7c5cff"></a>
  <a href="https://github.com/gtjevptje/Macronaut/releases"><img alt="downloads" src="https://img.shields.io/github/downloads/gtjevptje/Macronaut/total?label=downloads&color=7c5cff"></a>
  <img alt="platform" src="https://img.shields.io/badge/Windows-10%20%7C%2011-7c5cff">
</p>

<h3 align="center">
  <a href="https://github.com/gtjevptje/Macronaut/releases/latest/download/Macronaut.exe">⬇&nbsp;&nbsp;Download Macronaut for Windows</a>
</h3>

<p align="center"><i>One file. No installer. It updates itself.</i></p>

---

## What it is

Macronaut automates clicking, typing and waiting in **any** Windows program —
including ones with no API, no scripting support and no plugins. You build the
automation as a **flow**: boxes for the things it does, wires for the order they
happen in. No scripting language to learn.

If you have ever thought *"I do this exact sequence twenty times a day"*, that
is the thing Macronaut is for.

## What it can do

**Build a flow visually**
- Drop in **Click**, **Move**, **Drag**, **Scroll**, **Type text**, **Key press**,
  **Wait**, **Detect**, **If / Else**, **Loop**, **Go to** and **Comment** nodes
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
- Single 77 MB `.exe`, no installer, no Python, no admin needed to run
- Checks for updates and applies them on restart, verified by SHA-256
- Always-on-top mode, system tray icon, dark and light themes

## Requirements

Windows 10 or 11, 64-bit. Nothing else — everything is inside the `.exe`.

> **On first run, Windows may warn you.** Macronaut is not code-signed yet, so
> SmartScreen shows *"Windows protected your PC"* for new downloads. Click
> **More info → Run anyway**. Signing is on the roadmap.

## A word of warning

Macronaut sends real keyboard and mouse input. **Many online games and services
forbid automation in their terms of service, and using it against them can cost
you your account.** That is your call to make — see section 5 of the
[LICENSE](https://github.com/gtjevptje/Macronaut/releases/latest/download/LICENSE).

## Licence

**Proprietary — © 2026 Gerben van Poucke. All rights reserved.**

Macronaut is licensed, not sold: install and run it on machines you own or
control. Redistribution, resale and reverse engineering are not permitted. The
source is not published. Full terms ship inside the app under
**Settings → About & legal**, alongside the
[third-party notices](https://github.com/gtjevptje/Macronaut/releases/latest/download/THIRD-PARTY-NOTICES.md)
for the open-source components it is built on.

---

<p align="center">
  <a href="https://github.com/gtjevptje/Macronaut/releases/latest/download/Macronaut.exe"><b>⬇&nbsp;&nbsp;Download Macronaut for Windows</b></a>
</p>

<p align="center"><img alt="A Macronaut flow on the canvas" src="assets/hero.png" width="100%"></p>
