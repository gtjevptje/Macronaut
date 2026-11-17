# A free auto key presser for Windows

Pressing the same key a few hundred times is a job for the computer: arrowing
down a long list, skipping through a form, keeping a game character walking.
Macronaut presses keys for you, on a count or on a timer, and can hold one down
while it does something else.

**Quickest start.** Macronaut comes with a script called **Press a key 50 times**
in its library. Open it, double-click the key step to pick your key and the
count, press Play.

## What it can do with a key

- **Tap** a key, as many times as you like in one step.
- **Hold** a key for a set time, then let go.
- **Hold down** a key and leave it down, so later steps can click or type while
  it is held, then **release** it. That is how you keep W held while clicking.
- **Combinations** like Ctrl+C or Shift+Tab, modifier first.
- **Type a block of text** at a speed you choose.

A key still held when a script ends, or when you press Stop, is released
automatically.

## Press a key every few seconds

1. Add a **Loop**, set to repeat forever or a number of times.
2. Inside it, add a **Key** step with your key.
3. After it, add a **Wait** step with the gap you want.

**F8** starts and stops it, **Esc** stops anything. You can also give each saved
script its own launcher key.

## Keys in games

Many games ignore keys sent the ordinary way, which is why a lot of key pressers
work in Notepad and do nothing in the game. Macronaut has three input modes in
Settings:

- **Standard** — ordinary windows.
- **SendInput scancodes** — real key positions instead of characters.
- **Interception** — a kernel driver that looks like a real keyboard. The driver
  is installed separately.

Read the game's rules first: many online games ban automation, and can ban the
account that uses it.

## Download

[Download Macronaut for Windows](https://github.com/gtjevptje/Macronaut/releases/latest)
— free, open source (GPL v3), no account, no advertising.

Also: [OP Auto Clicker alternative](op-auto-clicker-alternative.md) ·
[auto clicker with image recognition](image-recognition-auto-clicker.md)
