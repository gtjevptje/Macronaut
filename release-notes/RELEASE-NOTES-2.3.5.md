A recording now plays back the way you made it, and there is an installer.

**Recordings replay in the time they took to make.** Record ten quick clicks
around the screen and play them back, and until this version they took about
half as long again as you did. Two things were adding time. The pointer
travelled between clicks at a fixed speed, even when your hand had clearly
covered the distance faster, since that is when the next click landed. And
every recorded pause was waited out in full *before* the pointer set off, so
the journey was added on top. The journey now happens inside the pause, at the
pace your hand actually went. Measured on the same ten clicks: 1.6 seconds
recorded, 2.4 seconds replayed before, 1.8 now, the same as with smooth
movement switched off.

That also brings back the **speed box**. Because the glides ignored it, "4x"
on a recording made with the mouse delivered barely more than 1x. It now does
what it says.

**The timing itself is recorded more precisely.** Windows' ordinary clock
ticks every 15.6 ms, and the recorder was using it, so every pause, key hold
and swipe was rounded to a tick before it was saved. At a 30 ms gap, which is
ordinary fast clicking, that is half the gap wrong, and a steady rhythm came
back as a stutter. The recorder uses the precise clock now. Flows you recorded
before keep the rounded numbers they were saved with.

**The recorder sees where the mouse rests.** It used to write down the places
the buttons went down and nothing in between. So hovering over a menu to open
it and then clicking an item recorded only the click, and the replay clicked
where the item would have been, with the menu still shut. When the pointer
stops somewhere for about 0.4 seconds, that is now a **Move** step, and the
replay waits there as long as you did. Plain clicking is unaffected: resting
on the spot you then click adds nothing.

Move steps open in the editor like any other step. (A recorded Move step
opened and saved in the editor used to turn into a left click in the top left
corner of the screen. Fixed before anyone could meet it outside a test, but
worth saying.)

**Ctrl-click, Shift-click and Alt-drag record as one gesture.** Holding Ctrl
and clicking two things used to record as two plain clicks and then a stray
"hold Ctrl" a second later, so the second click undid the first one's
selection. Click, Drag and Scroll steps now carry the keys held while they
happen, and the editor has a **Holding** row (Ctrl, Alt, Shift, Win), so you
can build one by hand too. The keys always come back up, even if the step is
stopped halfway.

**Three more recordings that came back wrong:**

- **The mouse's side buttons** (Back and Forward, or Mouse 4 and 5) were
  recorded as *left clicks* at the same spot. They record and replay as
  themselves now, and you can pick them in the Click, Drag and Detect editors.
- **A swipe** replayed 160 ms slower than it was made, which matters when the
  thing you are swiping measures speed.
- **A flick of the scroll wheel** replayed all its notches in the same
  instant, and many programs only read a few per frame. It keeps the speed you
  flicked at now.

**Watching the screen finds more of what is actually there.**

- **A plain-coloured button was always "found" in the top left corner.** Cut an
  image out of a flat colour (a swatch, a health bar, the inside of a button)
  and the match reported a perfect score at the corner of the screen, even for
  a colour that was nowhere on it. Detect clicked the corner and If/Else took
  the same branch forever. Flat images are searched a different way now and
  are found where they are, or not at all.
- **Icons with a transparent background** were matched against whatever colour
  happened to be stored under the transparency, which you cannot see and which
  depends on the program that saved the file. The same icon could be found or
  missed depending on that. The transparent part is now ignored.
- **Images captured at one display scaling and searched at another** were
  missed at 175%, and between 125% and 150%. Those sizes are searched now.
- **Text on screen** is read a second time, enlarged, when the first read finds
  nothing. Small interface text is often too small for Windows' text
  recognition, and in our tests the second read recovered most of those
  misses. This now also works on 4K screens, where it was switched off.
- **Very wide desktops** (three ultrawides side by side) read as empty, because
  Windows' text recognition silently returns nothing for an image wider than
  10,000 pixels. The image is scaled down to fit first now.

**A pointer glide is no longer a perfect ruler line.** It now bends very
slightly and has a tiny sideways tremor, the way a hand moves. It always
arrives exactly on the target pixel.

**A hand-edited script can no longer end a whole run.** If a step in the JSON
has a delay that is not a number, or `null`, that step now counts as having no
delay instead of stopping everything and leaving the mouse and keyboard
wherever step 40 of 200 left them. An infinite delay used to freeze the flow
without a word; it is treated as no delay too.

**The timeline under the canvas is more accurate** for scroll steps and for
key steps when you have changed the key hold time in Settings.

## An installer, beside the usual download

There are two downloads now, built from the same code:

- **`Macronaut.exe`**, the single file you already know. Nothing changes for
  you if you use it, and it keeps updating itself the way it always has.
- **`Macronaut-Setup-2.3.5.exe`**, a normal installer. It installs for your
  user only (no administrator prompt), adds a Start menu entry and an
  uninstaller, and does not unpack itself into a temporary folder every time
  it starts, which is the behaviour antivirus programs are most suspicious
  of. It is also the smaller download. A copy installed this way updates
  through the installer.

Your scripts and settings live in your own folder and are not touched by
either download, or by switching from one to the other.

## Verified before release

Every release goes through three gates before it is published: the built
program tests itself (image matching, text recognition and the rest, against
generated input with a known answer); a launch check confirms it really opens
a window; and the published 2.3.4 is downloaded and upgraded in place to this
build, with the upgraded copy passing its own self-test afterwards. The
installer's folder build passes the same self-test.

## Notes

- Still unsigned, so SmartScreen will still warn on first run. Click
  **More info → Run anyway**. The source is public if you would rather check:
  <https://github.com/gtjevptje/macronaut-source>
- Your scripts and settings are untouched by updating.
