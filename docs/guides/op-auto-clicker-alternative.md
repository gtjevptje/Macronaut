# An OP Auto Clicker alternative that can also see the screen

OP Auto Clicker got one thing exactly right: one small window with an interval,
a button, a repeat count and a Start key. Macronaut opens on that same layout on
purpose. The difference is what happens when clicking on a timer is no longer
enough.

**The short version.** If you click the same spot at a fixed speed and that
works, OP Auto Clicker is smaller and does the job. If you want it to wait until
something appears, or click a button that is not always in the same place, that
is what Macronaut adds.

## The window you already know

Macronaut opens on **Basic**, a plain auto clicker:

- **Click interval** in hours, minutes, seconds and milliseconds, or **Max speed**.
- **Button:** left or right.
- **Where:** at the cursor, or a spot you pick on screen.
- **Repeat:** until stopped, or a set number of times.
- **F8** starts and stops, **Esc** stops anything at any time.

No nodes, no wires, nothing to set up.

## Where a timer runs out

- **The button moves.** A window opens somewhere else and the clicker keeps
  hitting the spot where the button used to be.
- **You need it to wait.** Click, wait until the next screen has loaded, click
  again. A timer has to guess how long that takes.
- **You need it to stop on its own.** Stop when a word appears or a pixel changes
  colour, not after a number picked in advance.

Macronaut's **Advanced** view turns those into steps: wait for an image and click
it where it is, wait for text (Windows' own OCR), wait for a pixel, loop, and
branch on what it found.

## Side by side

| | OP Auto Clicker | Macronaut |
|---|---|---|
| Interval, button, repeat, Start | Yes | Yes, the Basic window |
| Click at the cursor or a fixed spot | Yes | Yes |
| Middle button, double click | In the main window | In the Auto-Click step |
| Record clicks and keys | No | Yes |
| Press keys and type text | No | Yes |
| Wait for an image or text | No | Yes |
| Click a target where it actually is | No | Yes |
| Input that games accept | Standard injection | Three input modes, including a kernel driver |
| Open source | No | Yes, GPL v3 |
| Cost | Free | Free |

## What OP Auto Clicker does better

- **Size.** It is tiny and opens instantly. Macronaut is much larger, because it
  carries a full interface toolkit and image matching.
- **Everything in one window.** Middle button and double click are in its main
  window; in Macronaut they are one step further away.

## Download

[Download Macronaut for Windows](https://github.com/gtjevptje/Macronaut/releases/latest)
— free, no account, no advertising.

Also: [auto key presser](auto-key-presser.md) ·
[auto clicker with image recognition](image-recognition-auto-clicker.md)

*OP Auto Clicker is not my software and I have no connection to it. If something
here is out of date or unfair, open an issue and I will change it.*
