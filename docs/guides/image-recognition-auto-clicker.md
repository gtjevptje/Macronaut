# An auto clicker with image recognition

An ordinary auto clicker clicks a position. The moment the button is somewhere
else, it clicks empty space. Macronaut can find the button by what it looks like,
wait for it to appear, and click it where it actually is.

**In one sentence:** you show it a picture of the button; it watches the screen
until that picture appears, then clicks it.

## How to set it up

1. Open **Advanced** and add a **Detect** step, set to look for an image.
2. Press **Capture…** and drag a box around the button. A small, distinctive crop
   works better than a large one.
3. Press **Test match** to check it finds the button now.
4. Tick **Click the image when it appears**. Click the preview to choose where on
   the image the click lands.
5. Set **Give up after**, or leave it waiting as long as it takes.

Press Play. The step waits, finds the button, moves the pointer to it and clicks.

## Making it reliable

- **Set a search area.** Faster, and it will not find the same icon somewhere
  you did not mean.
- **Adjust the match confidence.** It starts at 0.8. Lower it a little if it
  stops matching on another screen; raise it if it matches the wrong thing.
- **Display scaling** is handled for the common Windows scaling steps.
- **Plain-coloured and transparent PNGs** are matched by the parts you can see.

## What it cannot do

- **The button looks different** (dark theme, hover, disabled): capture the state
  you want clicked.
- **The button is not visible**: a covered or minimised window shows nothing.
- **Full-screen searches take a moment** when the image is not there. A search
  area makes it much quicker.

## More than images

The same Detect step can wait for **text** (read with the OCR built into Windows)
or for a **pixel** to change colour. Put it inside a Loop, or branch with If/Else
on whether it was found.

## Download

[Download Macronaut for Windows](https://github.com/gtjevptje/Macronaut/releases/latest)
— free, open source (GPL v3), no account, no advertising.

Also: [OP Auto Clicker alternative](op-auto-clicker-alternative.md) ·
[auto key presser](auto-key-presser.md)
