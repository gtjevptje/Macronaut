Interface fixes from a round of real use.

**The Add-node buttons no longer clip their labels.** "Comment" was drawn as
"Comme". The cause was app-wide rather than cosmetic: the window was built
before the theme was applied, so every widget measured itself in Qt's default
font and was then repainted in the theme's larger one. The theme is now in force
before the first widget exists, and the palette re-checks its own width once
it is on screen.

**Wait steps land on round numbers.** Stepping up from 950 ms gave 1.05 s, then
1.15, 1.25 — every value afterwards off the grid, and 1 s unreachable without
typing it. It now steps onto the next round value, and above a minute it steps
in whole seconds instead of tenths.

**Wait nodes read like durations.** A ten-minute wait said "Wait 600000 ms". It
now says "10 min", and shorter ones "1.5 s" or "1 min 30 s". Pre-delay badges
read the same way.

**The comment box header is no longer half dark.** The bottom of the title band
was being painted the body tint — on a 26 px bar, close to half of it.

**The timeline strip starts folded, always.** Opening it once no longer leaves it
open in every later session; the canvas already highlights the running node, so
looking at the timing is something you do for one run rather than a preference.
