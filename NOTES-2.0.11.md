Detect steps — testing a match no longer throws the step away, images can be
narrowed to part of the screen, and a node shows the picture it looks for.

**Fixed**

- **Pressing "Test match" while adding a Detect step discarded the step.** The
  dialog got out of the way so it wouldn't appear in its own screenshot, and
  doing it that way told the dialog it had been cancelled — so the node you were
  halfway through building disappeared and was never added. The same thing
  happened with "Test text", and with both buttons inside the If / Else editor.
  Testing before you commit is the whole point of those buttons, so this made
  them a trap.

**New**

- **A search area on Detect ▸ Image**, the same one Detect ▸ Text has had.
  Point a step at part of the screen and it only looks there: faster, and an
  icon that appears in two places matches the one you actually meant. The If /
  Else image test now uses the region too, so testing and running ask the same
  question.
- **Detect nodes show the image they are looking for.** A node used to read
  "Image «capture 31544.png»" — a filename nobody chose — so telling two Detect
  nodes apart meant opening them. The picture is on the node now, in the space
  the filename used to take. Nodes stay exactly the size they were.

**Changed**

- "Wait for image", "Wait for text" and "Wait for pixel" are now just **Image**,
  **Text** and **Pixel**. Waiting is what the whole Detect family does, so the
  words were on all three and told you nothing about which one you had.

No change to how anything runs, and none to what crash reporting collects:
never your scripts, your keystrokes, what is on your screen, or your name.
