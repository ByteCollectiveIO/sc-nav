# Product

## Register

product

## Users

Members of a Star Citizen org (a player guild), using the tool alongside the
game on a second device — a laptop or phone open next to the gaming PC. They are
mid-to-experienced players in an active session: navigating between points of
interest, planning and running cargo hauls, coordinating org events, tracking
shared resources/goals, and trading on an internal aUEC marketplace. Context is
split-attention and time-pressured — they glance at the tool, take a reading,
and get back to flying. A smaller set of org admins manage members, shards, and
org-wide settings.

The job to be done: turn an in-game `/showlocation` reading (forwarded by a
watcher on the gaming PC) into precise, glanceable navigation and logistics —
bearing, distance, ETA, route plans, and shared org data — without breaking
immersion or pulling focus from the game.

## Product Purpose

SC Nav is a self-hosted org companion app for Star Citizen. A watcher on the
gaming PC forwards coordinates to a small LAN/Docker server that computes
container, lat/lon, altitude, and bearing/distance/ETA to any POI or recorded
resource node, then pushes it live (WebSocket) to a browser on a second device.
It has grown into a nine-app suite under one hash-routed SPA, grouped in the
launcher as **Out in the 'Verse** (resource navigator, cargo planner, trade
route planner), **Rally the Org** (event planner with fleet rosters, group
finder/LFG, pirate danger board), and **Run the Org** (resource manager
inventory/goals, aUEC marketplace, org intel/analytics) — with Discord-OAuth
multi-user/org support, live presence, and Discord webhook notifications.

Success is the tool disappearing into play: a member gets an accurate reading or
a usable plan in one glance and returns to the game. It is an unofficial fan
project, not affiliated with Cloud Imperium Games.

## Brand Personality

Precise · utilitarian · sci-fi. The voice is a flight computer / ship HUD:
terse, confident, data-first. It speaks in readings and numbers, not marketing.
It feels in-universe — a cockpit instrument at night, monospace and glowing —
without becoming a costume. Polish serves legibility and speed, never spectacle.
The identity is product-led but free to evolve: the dark HUD/terminal system is
the starting point to extend, not a museum piece to freeze.

## Anti-references

- **Toy / gamified UI.** No cartoon badges, confetti, oversized playful
  elements, or achievement-spam. This is a serious instrument for people mid-mission.
- **Cluttered fan-wiki.** No dense, ad-heavy, unstyled wall-of-text fansite
  look. Information is curated and laid out, not dumped.
- (Carry-over from shared bans:) no generic rounded-card SaaS-dashboard
  template, no light/cream/flat treatment that breaks the cockpit-at-night feel.

## Design Principles

- **Glance, don't read.** Every screen must yield its key reading in one look —
  big tabular numbers, clear primary value, ruthless hierarchy. The user is
  flying; they do not have time to study the page.
- **Instrument, not interface.** Borrow the discipline of cockpit gauges:
  precise, consistent, calm under load. Decoration that doesn't convey state or
  improve legibility is removed.
- **In-universe, earned not costumed.** Sci-fi feel comes from typography,
  darkness, and restraint — not from skeuomorphic chrome or gratuitous glow.
  Familiar product affordances win when they serve the task faster.
- **One vocabulary across apps.** Navigator, cargo, events, marketplace, and
  resources share one component language (panels, readouts, chips, semantic
  colors). The same action looks the same everywhere.
- **Self-hosted and honest.** A small tool run by and for an org. No dark
  patterns, no fluff; clear about being an unofficial fan project.

## Accessibility & Inclusion

- Target **WCAG 2.1 AA** contrast: body text ≥4.5:1, large/bold text ≥3:1,
  including against the dark `--bg`/`--panel` surfaces and for placeholder text.
- **Color-blind safe:** the semantic color roles (node, fauna, mate, harvest,
  good/warn/bad, gold selection) must never be the *only* signal. Pair them with
  icons, labels, badge text, or position so meaning survives without hue.
- Honor `prefers-reduced-motion` on every animation (crossfade or instant
  fallback). Keep the UI keyboard-navigable with visible focus states (the
  existing accent-border focus is a good base).
