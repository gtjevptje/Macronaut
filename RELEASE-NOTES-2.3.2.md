# Macronaut 2.3.2 — Basic, remastered, and a purple that reaches the edges

2.3.0 brought the Basic auto-clicker back and 2.3.1 made a new install actually
open on it. This release is about how it *looks* — because what came back was
the 2.0 window, and it wore the wrong colours in a wrong-looking way.

## Basic has been rebuilt

Three labelled cards — **Click interval**, **Click**, **Options** — instead of a
column of grey boxes. Aligned labels, real spacing, buttons that look like the
buttons everywhere else in the app, and a Start bar you can find without
reading. Everything it did before, it still does; nothing moved anywhere
surprising.

## Cosmic is a real theme now, and it is the default

Macronaut has shipped a purple rocket, a purple website and a navy-blue
application since 2.0. The purple was never a theme — it was four hardcoded
copies of a colour, in four places, while the actual default theme was
**Mission Control**, a navy console.

**Cosmic** is now a proper theme sitting alongside Mission Control, Graphite and
Daylight in **Settings → Appearance**, and it is what Macronaut opens on.

**If you never picked a theme, you will see this one on your next launch.** That
is deliberate: the app now looks like the thing it is named after. If you *did*
choose a theme on purpose, you keep it — picking Mission Control is a decision,
and an update should not overrule it. You can switch back in two clicks either
way.

## The title bar follows the theme too

The window's title bar, the canvas header and the settings drawer each painted
themselves and ignored the theme entirely. On **Daylight** that meant a dark
purple bar sitting on top of a light window; between Basic and Advanced it meant
two slightly different shades of the same bar. All three now take their colour
from the theme, so every theme is the whole window.

## Auto-Click has left the palette

The **⚡ Auto-Click** button is gone from the node palette on the canvas. Basic
*is* the auto-clicker now, and having a second one hiding in a palette was one
place too many.

Nothing was removed from the app. Every saved flow that contains an Auto-Click
step still runs exactly as before, still opens its own editor when you
double-click it, and is still free — including repeat counts and intervals,
which a Click inside a Loop is not.

---

Everything in 2.3.0 and 2.3.1 is unchanged, and all of it is free.

Feedback and bug reports: **gerbenvanpoucke0@gmail.com**.

Existing installs update themselves on restart. New downloads: Windows will say
*"Windows protected your PC"* because the file is not code-signed yet — click
**More info**, then **Run anyway**.
