---
name: SC Nav
description: A cockpit-instrument UI for a Star Citizen org companion suite — canonical dark theme with a tinted light (day) mode from one token set.
colors:
  bg: "#0b0e13"
  panel: "#141a23"
  panel2: "#1b2330"
  text: "#d8e1ee"
  dim: "#8693a6"
  accent: "#4fc3f7"
  warn: "#ffb74d"
  good: "#66bb6a"
  bad: "#ef5350"
  border: "#243044"
  node: "#b39ddb"
  fauna: "#81c784"
  mate: "#f06292"
  harvest: "#cddc39"
  sel: "#ffd54f"
  barter: "#c08af0"
  craft: "#e0a64f"
  ink-on-accent: "#07101a"
  track: "#0c1119"
typography:
  display:
    fontFamily: '"SF Mono", "Cascadia Code", Consolas, monospace'
    fontSize: "44px"
    fontWeight: 400
    lineHeight: 1.1
    letterSpacing: "normal"
  headline:
    fontFamily: '"SF Mono", "Cascadia Code", Consolas, monospace'
    fontSize: "18px"
    fontWeight: 400
    lineHeight: 1.3
    letterSpacing: "1px"
  title:
    fontFamily: '"SF Mono", "Cascadia Code", Consolas, monospace'
    fontSize: "16px"
    fontWeight: 400
    lineHeight: 1.3
    letterSpacing: "2px"
  body:
    fontFamily: '"SF Mono", "Cascadia Code", Consolas, monospace'
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "normal"
  label:
    fontFamily: '"SF Mono", "Cascadia Code", Consolas, monospace'
    fontSize: "11px"
    fontWeight: 400
    lineHeight: 1.4
    letterSpacing: "1px"
rounded:
  xs: "3px"
  sm: "4px"
  md: "6px"
  lg: "8px"
  xl: "12px"
  pill: "14px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "14px"
  xl: "18px"
  xxl: "24px"
components:
  button:
    backgroundColor: "{colors.panel2}"
    textColor: "{colors.text}"
    rounded: "{rounded.md}"
    padding: "5px 12px"
  button-active:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.ink-on-accent}"
    rounded: "{rounded.md}"
    padding: "5px 12px"
  input:
    backgroundColor: "{colors.panel2}"
    textColor: "{colors.text}"
    rounded: "{rounded.md}"
    padding: "8px 10px"
  panel:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.text}"
    rounded: "{rounded.lg}"
    padding: "12px 14px"
  readout:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.text}"
    rounded: "{rounded.lg}"
    padding: "10px 14px"
  chip:
    backgroundColor: "{colors.panel2}"
    textColor: "{colors.dim}"
    rounded: "{rounded.pill}"
    padding: "0 6px"
  app-card:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.text}"
    rounded: "{rounded.xl}"
    padding: "24px 20px"
---

# Design System: SC Nav

## 1. Overview

**Creative North Star: "The Cockpit Instrument"**

SC Nav looks like a ship's navigation computer read at night: a near-black hull
(`#0b0e13`), panels of faintly-lit glass, and cyan readouts that glow against
the dark. Everything is monospace, because this is an instrument, not a website —
numbers line up in columns, labels are small and quiet, and the one value that
matters is large and bright. The user is mid-flight on a second screen; the
interface earns its keep by yielding a reading in a single glance and getting
out of the way.

The system is **dense but breathable**. It carries a lot — bearings, ETAs,
route plans, leaderboards, marketplace listings — and it does not apologize for
density where density is the job. But density is rationed with deliberate
spacing: panels breathe at 12–14px, sections rule off from one another, and the
hierarchy is ruthless so the eye always finds the primary value first. Sci-fi
feel comes from typography, darkness, and restraint — never from skeuomorphic
chrome or gratuitous glow.

This system explicitly rejects the **toy / gamified UI** (no cartoon badges,
confetti, achievement spam — people are mid-mission) and the **cluttered
fan-wiki** (no dense, ad-heavy, unstyled wall of text). It is not a generic
rounded-card SaaS dashboard.

Dark is the **canonical** theme — the cockpit at night — but the instrument
also ships a **light (day) mode** built from the same CSS-variable token set
(see §2, *Theming Contract*). What is non-negotiable in either theme is the
**tinted-neutral discipline**: no pure black, no pure white, no cream, no flat
SaaS white — every neutral is tinted along the navy/cyan brand axis so the
light mode reads as the same instrument in daylight, never as a generic white
dashboard.

**Key Characteristics:**
- Near-black navy hull (day mode: navy-tinted off-white) with cyan as the single
  voice of "live / actionable" — never pure black or pure white in either theme.
- Monospace throughout; tabular numerals on every figure.
- Flat, bordered panels; shadow is reserved for lift and focus, never decoration.
- A fixed, meaning-bearing color vocabulary (each hue *is* a category).
- Dense data laid out for the glance, not the read.

## 2. Colors

A cold, instrument-panel palette: a near-black navy base, one electric cyan
voice, and a small set of saturated hues that each carry a fixed meaning.

### Primary
- **Instrument Cyan** (`#4fc3f7`): the single accent. Used for interactive
  affordances (button hover/active, focused field borders), the current-app
  title, primary readout values, links, and the "live/positive-action" signal.
  It is the brightest thing on any screen and its rarity is what makes a reading
  pop.

### Secondary
- **Beacon Gold** (`#ffd54f`, `--sel`): reserved almost exclusively for the
  *currently-selected destination* — the map ring and the highlighted table row.
  Gold means "this is the one you chose."
- **Caution Amber** (`#ffb74d`, `--warn`): destination names, armed/pending
  states, and aUEC (currency) metrics. The "attention, not error" register.

### Tertiary — the category vocabulary
These are not decorative. Each hue is bound to a data category and reused
nowhere else.
- **Node Violet** (`#b39ddb`): recorded resource nodes.
- **Fauna Green** (`#81c784`): wildlife.
- **Harvest Lime** (`#cddc39`): harvestables.
- **Teammate Pink** (`#f06292`): teammate presence / roster dots.
- **Barter Purple** (`#c08af0`, `--barter`): the marketplace barter listing mode
  (distinct from node violet so trade-mode reads apart from resource data).
- **Crafted Gold** (`#e0a64f`, `--craft`): marketplace crafted-item quality
  accents (the ⚒ badge, quality table header, crafted-filter disclosure).
- **Status Good** (`#66bb6a`) / **Status Bad** (`#ef5350`): success / error and
  same-shard / connection state.

### Neutral
- **Hull Black** (`#0b0e13`, `--bg`): the page; the unlit cockpit.
- **Panel Steel** (`#141a23`, `--panel`) / **Panel Steel Raised**
  (`#1b2330`, `--panel2`): the two surface layers — content panels, and the
  cooler layer for controls, inputs, and inset cards.
- **Readout Ink** (`#d8e1ee`, `--text`): primary text, just off-white and
  faintly cool.
- **Dim Label** (`#8693a6`, `--dim`): labels, captions, secondary metadata. Tuned to
  clear WCAG AA (≥4.5:1) even on the raised `--panel2` layer.
- **Hairline** (`#243044`, `--border`): the 1px borders and dividers that do
  the structural work shadows would do elsewhere.
- **Track** (`#0c1119`) / **Console Black** (`#080b10`): the recessed wells
  behind progress bars, charts, and the map canvas.
- **Ink-on-Accent** (`#07101a`): the near-black text laid over cyan/gold fills
  so active controls stay legible.

### Named Rules
**The Category-Color Rule.** Every saturated hue *is* a category — node violet,
fauna green, harvest lime, teammate pink, gold = selection. A category color is
never borrowed for decoration, emphasis, or a different data type. If you reach
for a color to "make it pop", you are wrong; pop comes from cyan, weight, or
size. This is also an accessibility contract: because hue carries meaning, hue
is never the *only* carrier — pair it with an icon, glyph, label, or position.

**The One Voice Rule.** Cyan is the only accent. It marks what is live or
actionable and nothing else. Its scarcity is the point.

### Theming Contract — Dark & Light

SC Nav supports **two themes from one set of CSS variables**. Dark is the
canonical instrument ("the cockpit at night"); **Light is the same instrument
on a lit hangar deck** — identical layout, hierarchy, and category vocabulary,
re-toned for a bright environment. Light mode is *not* a generic white SaaS
skin and it is *not* an inversion of dark; it is a deliberately re-designed
day mode that obeys every rule below.

**The Tinted-Neutral Law (both themes, non-negotiable).** No surface, text, or
border is ever pure black (`#000`) or pure white (`#fff`). Every neutral carries
a faint cold tint toward the brand hue (the navy/cyan axis, ~hue 215–230) so the
hull and the readouts feel cut from the same material. Dark neutrals are
navy-tinted near-blacks; light neutrals are navy-tinted off-whites and slate
inks. A flat `#fff` panel or `#000` text instantly breaks the instrument feel
and is treated as a bug.

#### Token architecture (the contract)

Two layers, and the theme switch only ever touches the second:

1. **Semantic tokens** — the named roles the whole UI reads: `--bg`, `--panel`,
   `--panel2`, `--text`, `--dim`, `--border`, `--accent`, `--ink-on-accent`,
   `--track`, and the category hues (`--sel`, `--warn`, `--good`, `--bad`,
   `--node`, `--fauna`, `--harvest`, `--mate`, `--barter`, `--craft`).
   **Components reference only these — never a raw hex.** This is what makes one
   stylesheet serve two themes.
2. **Theme blocks** — `:root` defines the dark (default) values. A
   `[data-theme="light"]` block (and a matching `@media (prefers-color-scheme:
   light)` fallback for users who haven't chosen) redefines *only the semantic
   tokens*. No component CSS is duplicated per theme; if a rule needs a literal
   color, that color is wrong — promote it to a token.

`color-scheme` is set per theme (`dark` under `:root`, `light` under the light
block) so native controls (date/time pickers, selects, scrollbars) follow the
hull instead of fighting it. Theme selection persists in `localStorage` and
defaults to the OS preference on first load.

#### Light-mode semantic values (starter ramp, tuned for AA)

Same roles, re-toned. These are the contract's reference values — tune for
contrast, never reach for `#fff`/`#000`:

| Token | Dark (canonical) | Light (day mode) | Role |
|-------|------------------|------------------|------|
| `--bg` | `#0b0e13` | `#e8ecf2` | the hull / page |
| `--panel` | `#141a23` | `#f2f5f9` | content panel surface |
| `--panel2` | `#1b2330` | `#f8fafc` | raised controls / inset cards |
| `--track` | `#0c1119` | `#dbe2ec` | recessed wells (bars, map, charts) |
| `--text` | `#d8e1ee` | `#11151c` | primary ink |
| `--dim` | `#8693a6` | `#586277` | labels / secondary metadata |
| `--border` | `#243044` | `#cfd7e3` | 1px hairlines |
| `--accent` | `#4fc3f7` | `#0c84c4` | the single cyan voice |
| `--ink-on-accent` | `#07101a` | `#f2f5f9` | text over an accent fill |

In dark mode, raised surfaces get *lighter* (hull → panel → panel2). In light
mode the same ladder still climbs toward white but the steps are gentler, so
**control definition leans on the hairline border (and a faint shadow) rather
than on tonal contrast alone** — see Elevation.

#### Category hues across themes (hue identity is preserved)

A category's *identity* (node = violet, fauna = green, gold = selection, …) is
the same in both themes — the Category-Color Rule outranks the theme. What
changes is **luminance for contrast**: the bright dark-mode hues (`--sel`
`#ffd54f`, `--warn` `#ffb74d`, etc.) fail AA as text on a light surface, so in
light mode each category token resolves to a **darker on-light variant** (gold ≈
`#9a6a00`, amber ≈ `#b3631a`, good ≈ `#2e7d32`, bad ≈ `#c62828`, node ≈
`#6a4fb0`, …) used wherever the hue is *opaque* — text, icons, 1px borders, and
solid fills alike. A solid fill then pairs with the light `--ink-on-accent`
(`#f2f5f9`), exactly as the dark theme pairs its bright fills with near-black
ink. Only the **low-alpha washes and rings keep the bright hue** (`--sel-wash`,
`--accent-wash`, `--focus-ring`), because there the surface beneath carries the
contrast. Hue still never travels alone — it is always paired with an icon,
glyph, label, or position (the accessibility half of the Category-Color Rule),
which is what makes a luminance shift between themes safe.

**The dark-scope exception.** Two element classes stay dark in *both* themes
and are deliberately omitted from the light override: the **map / radar canvas**
(`--map-bg`, a recessed scope that reads as a screen inside a light panel, the
way game MFDs and chart embeds do) and the **shadow / scrim vocabulary**
(drop shadows, the modal backdrop) — shadow is an absence of light, not a
neutral surface, so it is dark regardless of theme. CSS data-track wells
(`--track`, behind bars and charts) *do* flip with the theme; only the canvas
scope does not.

#### Theming laws

- **One token set, two themes.** Components read semantic tokens only; the theme
  block redefines those tokens and nothing else. A literal hex in a component is
  a contract violation.
- **No pure black or pure white, ever, in either theme.** All neutrals are
  navy/cyan-tinted. (The Tinted-Neutral Law.)
- **Re-tone, don't invert.** Light mode is re-designed for daylight legibility —
  surface ladder, accent luminance, and category-hue darkness are tuned per
  theme, not algorithmically flipped.
- **Contrast holds in both.** Body text, dim labels, placeholders, and accent-
  as-text clear WCAG AA (≥4.5:1) on whichever surface they sit on, in both
  themes. The dim label is verified against its *raised* layer in each theme.
- **Hue identity is theme-invariant.** A category means the same thing in both
  modes; only its luminance shifts to stay legible.

## 3. Typography

**Display / Body / Label Font:** `"SF Mono", "Cascadia Code", Consolas,
monospace` — one monospace family does all the work.

**Character:** Single-family, fixed-pitch, technical. There is no display/body
pairing because an instrument speaks in one typeface. Hierarchy comes from
size, weight, letter-spacing, and color — not from a second font. Tracking
opens up as type gets larger and more title-like (h1 rides at +2px), and
everything numeric is set in tabular figures so columns lock.

### Hierarchy
- **Display** (400, `44px`, tight): the one primary readout value per screen
  (`.readout.primary .value`) in cyan. The number you flew here to see.
- **Headline** (400, `30px`, tabular): standard readout values, leaderboard /
  stat-card metrics. Big but secondary to the primary.
- **Title** (400, `16px`, `+2px` tracking): the app/site title (`header h1`),
  launcher headings. The most "spaced-out" text in the system.
- **Body** (400, `13px`, 1.5): table cells, descriptions, form values, general
  copy. Prose blocks (legal pages) cap around 65–75ch.
- **Label** (400, `11px`, `+1px` tracking): panel headers (`.panel h2`),
  readout labels, column headers, chip text — small, dim, and quiet.

### Named Rules
**The Tabular-Numerals Rule.** Every figure — distances, ETAs, aUEC, counts,
ranks — uses `font-variant-numeric: tabular-nums`. Numbers in this system are
data; they must align and never jitter as they update live.

**The One-Family Rule.** No second typeface, ever. No display serif, no UI sans
"for warmth". Monospace is the instrument's voice; a contrasting font would
break the cockpit illusion instantly.

## 4. Elevation

Flat by default. Depth is built from **tonal layering plus 1px hairline
borders**, not from ambient shadows: the hull (`#0b0e13`) sits under panels
(`#141a23`), which sit under the raised control/inset layer (`#1b2330`), each
separated by a `#243044` border. This is what gives the UI its console look —
crisp edges, not soft cards.

Shadow is therefore *meaningful*: it appears only as a response to state
(lift, overlay, focus) or as a powered-on glow on a live indicator.

### Shadow Vocabulary
- **Hover lift** (`box-shadow: 0 10px 28px rgba(0,0,0,0.5)` + `translateY(-2px)`):
  launcher app-cards on hover only.
- **Overlay** (`box-shadow: 0 14px 40px rgba(0,0,0,0.55)`): the login card and
  modal dialogs floating over the dimmed hull.
- **Dropdown** (`box-shadow: 0 6px 18px rgba(0,0,0,0.5)`): the POI autocomplete
  menu.
- **Focus ring** (`box-shadow: 0 0 0 3px rgba(79,195,247,0.2)`): a soft cyan
  halo on focused composite controls.
- **Live glow** (`box-shadow: 0 0 5–6px var(--<category>)`): a colored bloom on
  an *active* indicator only — the "you are here" teammate dot, the selected
  calendar day. Off-state indicators do not glow.

### Named Rules
**The Flat-Console Rule.** Surfaces are flat at rest and separated by tone and a
1px hairline. A drop shadow on a resting element is forbidden — if it isn't
hovered, floating, focused, or live, it casts no shadow.

**Theme note.** Dark mode reads depth from the tonal ladder; the hairline does
the structural work and resting shadows stay absent. Light mode's surface steps
are gentler, so the **hairline carries even more of the load** and a *very*
faint resting shadow on panels/cards is permissible to keep surfaces from
flattening into the page — but it stays subtle (depth still comes from tone +
border, not from soft drop shadows), and the hover-lift / overlay / focus /
live-glow vocabulary above is identical in both themes.

## 5. Components

### Buttons
- **Shape:** gently rounded (6px radius), compact.
- **Default:** raised panel layer (`#1b2330`) on a 1px hairline border, `#d8e1ee`
  text, `5px 12px` padding, 12px label-size monospace.
- **Hover:** border shifts to cyan (`border-color: var(--accent)`); fast
  (`0.12s`).
- **Active / on:** filled cyan (`#4fc3f7`) with near-black ink (`#07101a`) — the
  "engaged" state for segmented toggles and primary actions.
- **Danger:** `#ef5350` border + text; on hover fills red with dark ink.
- **Segmented control (`.seg` / `.range-toggle`):** a bordered group of buttons
  sharing 1px dividers; the on segment fills cyan.

### Chips
- **Style:** small bordered pills (`14px` radius on category chips, square-ish on
  inline data chips), 10–12px uppercase, tinted to their meaning. Border + text
  share the category color; background stays panel-dark.
- **State:** filter chips read dim by default and switch to cyan border+text when
  `on`; event-category and contract chips are color-coded by the category
  vocabulary (PvP=red, PvE=green, social=pink, logistics=amber, race=lime…).

### Cards / Containers
- **Panel** (the workhorse): `#141a23` background, 1px `#243044` border, **8px**
  radius, `12–14px` padding. Header is an 11px dim `+1px`-tracked label. Stacked
  sections inside one long panel rule off with a top border. Cards are used
  sparingly and never nested.
- **Readout:** panel-style tile holding a small dim label + a large tabular
  value; `.primary` variant renders the value at 44px in cyan.
- **Metric card** (`.lb-card` / `.stat-card`): centered, raised layer, big
  tabular number over a tiny tracked caption — for leaderboards and stats only.
- **App-card (launcher):** the one lifting element — 12px radius, centered
  logo+name+desc, and on hover only it raises 2px with a cyan border and deep
  shadow. Disabled apps desaturate (`grayscale(0.6)`, 55% opacity) and carry a
  small "soon" badge.

### Inputs / Fields
- **Style:** raised layer (`var(--panel2)`) background, 1px hairline border, 6px
  radius, `8px 10px` padding, body-size monospace. Native controls inherit the
  theme's `color-scheme` (`dark` / `light`) so date/time/select menus and
  scrollbars match the hull in either mode.
- **Focus:** outline removed, border shifts to cyan; composite controls add the
  soft cyan focus-ring glow. No layout shift on focus.
- **Placeholder:** must meet the same 4.5:1 contrast as body text — not a faint
  gray.

### Tables
- **Style:** dense (13px), full-width, hairline row borders, no vertical rules.
  Column headers are 11px dim tracked labels; numeric columns are right-aligned
  and tabular. Long tables get a sticky header and a scroll window; wide tables
  scroll horizontally inside their panel.
- **Selected row:** tinted gold wash (`rgba(255,213,79,.12)`) with gold text and
  an inset gold marker — "the one you chose".

### Navigation
- The header is a single flat bar: logo + title (doubles as home → launcher),
  a connection dot (green ok / red down), the current-app title in amber, and
  account controls pushed right. It wraps gracefully on narrow screens.
- **Theme toggle (`#theme-toggle`):** a compact bordered icon button in the
  header-right cluster that flips dark ↔ light. Its glyph shows the mode you'd
  switch *to* (☀ while dark is active, ☾ while light is active) and carries an
  `aria-pressed` + `aria-label`. The choice persists in `localStorage`
  (`scTheme`); with no stored choice the UI follows the OS via CSS
  `prefers-color-scheme` and the button just mirrors the resolved theme.
- App-level navigation is the launcher grid (`auto-fit, minmax(220px, 1fr)`)
  and hash routes (`#/nav`, `#/route`, `#/market`, …).

### Signature Component — the readout
The `.readout` tile is the heartbeat of the system: a quiet 11px label over a
large tabular value. The `.primary` readout (44px cyan) is the single most
important number on the screen. If you remember one component, it is this — it
is the cockpit gauge the whole North Star is named for.

### Hand-rolled bars & charts
Progress bars, stacked leaderboard bars, horizontal stat bars, and weekly
column charts are all pure CSS over a recessed `#0c1119` track, filled with the
category color. No charting library; the instrument draws its own gauges.

## 6. Do's and Don'ts

### Do:
- **Do** keep cyan (`#4fc3f7`) as the single accent voice — interactive,
  live, and primary values only. Its scarcity is the point.
- **Do** treat every saturated hue as a fixed category (node violet, fauna
  green, harvest lime, teammate pink, beacon gold = selection) and pair it with
  an icon/glyph/label so meaning never depends on color alone.
- **Do** set every number in `tabular-nums` so live-updating figures align and
  don't jitter.
- **Do** build depth from tonal layers (dark: `#0b0e13` → `#141a23` → `#1b2330`)
  and 1px hairlines; keep surfaces flat at rest.
- **Do** drive both themes from one semantic token set (`--bg`, `--panel`,
  `--text`, `--accent`, …); redefine those tokens under `[data-theme="light"]`
  and never duplicate component CSS or hard-code a hex.
- **Do** lead each screen with one large primary value the eye finds instantly —
  glance, don't read.
- **Do** keep state transitions fast and functional (≈`0.12s`); animate to
  convey state, never to choreograph.

### Don't:
- **Don't** ship a **toy / gamified UI** — no cartoon badges, confetti,
  oversized playful elements, or achievement spam. This is an instrument for
  people mid-mission.
- **Don't** drift toward a **cluttered fan-wiki** — no dense, ad-heavy,
  unstyled wall of text. Information is curated and laid out.
- **Don't** use pure black (`#000`) or pure white (`#fff`) anywhere, in either
  theme — every neutral is tinted along the navy/cyan brand axis. Cream, generic
  warm-tint off-white, and flat SaaS white are all forbidden; light mode is a
  tinted *instrument* in daylight, not a white dashboard.
- **Don't** invert dark mode to make light mode, hard-code a hex inside a
  component, or fork component CSS per theme — re-tone the semantic tokens only.
- **Don't** turn this into a generic rounded-card SaaS dashboard, and never nest
  cards.
- **Don't** introduce a second font family or a non-monospace UI face "for
  warmth" — the one-family rule is the instrument's voice.
- **Don't** borrow a category color for decoration or emphasis, and don't make
  hue the only signal for a state.
- **Don't** put a resting drop shadow on anything; shadow is for hover-lift,
  overlays, focus, and live glow only.
- **Don't** use gold for anything but the current selection.
