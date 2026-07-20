# Resource Navigator

> Live turn-by-turn navigation from your in-game position to any POI, resource node, or piece of wildlife — the core the whole suite is built on. **Route:** `#/nav` · **Launcher group:** Out in the 'Verse

<div align="center">
  <img src="../../images/readme_images/sc-navigator-routes.png" alt="Resource Navigator: live position readouts, a north-up map, capture panels, and a nearby-POI table" width="820">
</div>

## What it is

Star Citizen doesn't give you a HUD marker for "the cave three org-mates found
last week" or "the Laranite node someone logged an hour ago." The in-game map
shows official points of interest, but everything your own org has actually
found lives nowhere — until someone remembers roughly where it was and tries
to eyeball it from orbit.

The Resource Navigator closes that gap. A tiny watcher script on your gaming
PC watches `Game.log`; when you run `/showlocation`, it forwards your raw
coordinates to the server, which resolves them into your container, your
latitude/longitude/altitude, and a live bearing, distance, and ETA to your
destination — then pushes all of it over WebSocket to a browser on a second
device, a laptop or phone next to your keyboard, so you get real navigation
without alt-tabbing out of the game.

It's also where the org's shared knowledge lives. Every resource node,
harvestable, or wildlife sighting anyone captures here becomes searchable,
mappable, and — for ores — priced, so the next miner who opens the app sees
not just "a node was here" but "a node was here, and it's worth chasing."

## How to use it

### Live position and destination guidance

1. Open the app (it's the default view — `#/nav`, also reachable as `#/main`)
   on a second device while you're in-game.
2. Run `/showlocation`. The watcher on your gaming PC picks up the clipboard
   copy and posts it to the server automatically — nothing to type in the
   browser.
3. The **readouts** row fills in: `CURRENT CONTAINER`, `BEARING`, `DISTANCE`,
   `ALTITUDE`, `SPEED`, and `LAT / LON`, updating live as new positions arrive.
4. Click a row in `NEARBY`, `SEARCH`, or the element finder to set it as your
   `DESTINATION`; the panel shows the target name and a `clear` button.
5. Run `/showlocation` again as you travel — `BEARING` and `DISTANCE`
   recompute against your new fix so you can steer toward the target live.

### The map

The `MAP` panel is a north-up canvas centered on your last known position —
drag to pan, and use the `view radius` slider to zoom from half a kilometer
out to 50 km. `Start Tracking Path` / `Clear Path` draw a breadcrumb trail as
you move; `Recenter` snaps back to your live position. The **Layers &
settings** panel toggles what draws (`path`, `POIs`, `resources`, `wildlife`,
`harvestables`, `teammates`, `survey marks`, plus the `fresh only`/`this
shard` filters below) and picks a **heatmap** mode that shades the map by
most-likely ore or harvestable across all-time data. Keyboard shortcuts while
the map has focus: `R` recenter, `T` toggle path tracking, `C` capture a POI
at your current position.

### Capturing observations

Three forms sit under the map — `ADD CUSTOM POI`, `ADD RESOURCE NODE`, and
`ADD FAUNA & HARVESTABLES` — and all three work the same way: fill in the
details (name/type for a POI; ore + scan `band` 1–8 + biome for a resource
node; species + biome for fauna/harvestables), click `capture
/showlocation` to arm it (`ARMED — run /showlocation`, or `cancel` to back
out), then run `/showlocation` in-game. The next position fix the watcher
forwards becomes that observation's location — no manual coordinate entry.
A custom POI can be flagged `QT marker` (a jumpable quantum-travel target,
e.g. an orbital marker) or `🔒 private` (visible only to you).

### NEARBY and SEARCH

`NEARBY` lists every POI, resource, fauna sighting, and harvestable close to
your last fix, filterable by kind (`All` / `POIs` / `Resources` / `Fauna` /
`Harvest`), container, contributor, or free text; click a row to set it as
your destination. `SEARCH (whole dataset)` runs the same lookup across
everything the org has ever recorded, not just what's nearby.

Two map-settings toggles govern what NEARBY and the map's point markers show
by default. **fresh only** hides ephemeral resource/fauna/harvestable
sightings older than the org's freshness window (Star Citizen respawns them,
so stale hits mislead you) — uncheck to see the full history as a reference
overlay. **this shard** hides nodes the watcher's shard id says are on a
different server than yours; untagged legacy sightings stay visible either
way.

### Resource forecast and element finder

`RESOURCE FORECAST` ranks the ores and harvestables most likely nearby, based
on everything the org has logged on your current body, with a likelihood bar,
sample count, and — for ores — a value chip per row. `ELEMENT FINDER` flips
the question: pick an ore or harvestable and get a ranked table of where to
find it — likelihood, nearest known spot (`GO TO`), nearest QT marker
(`JUMP TO (QT)`), travel distance, typical scan band, and sample count, sortable
by `most likely`, `nearest`, or `best value (from here)`. Belt ores with no
fixed POI (Aaron Halo bands) get an `IN THE BELTS` section of org-measured
survey clusters instead.

## Features

- **Live bearing/distance/ETA** to any POI, resource node, or wildlife
  sighting, recomputed on every `/showlocation` — no manual math, no alt-tab.
- **North-up map** with pan, zoom, breadcrumb path tracking, per-layer
  visibility, and keyboard shortcuts.
- **Observation capture** for custom POIs, resource nodes (scan band + biome),
  and fauna/harvestables — captured against your next live position fix, not
  a typed-in guess.
- **Fresh-only, shard-aware filtering** — ephemeral sightings age off
  automatically and are scoped to the shard they were actually seen on, so
  NEARBY never points you at a node that respawned elsewhere or despawned.
- **Resource forecast** — ranked "what's likely nearby," built from the org's
  accumulated sightings on your current body.
- **Element finder** — reverse lookup: pick an ore or species, get a ranked,
  travel-costed list of where to find it, including belt survey clusters for
  ores with no fixed location.
- **Ore value `$`-badges** — resource and harvestable names carry a `$`/`$$`/
  `$$$` chip (per-category value tercile, from live UEX sell prices) on
  forecast rows, NEARBY, the element finder, and capture results, so you can
  weigh "worth a detour" at a glance. A trailing `*` marks a refined-value
  basis; no badge just means unpriced, never worthless.
- **Live teammate presence** — a `TEAMMATES` roster and map markers for
  org-mates currently online, same-shard members sorted first, with a
  per-player `share my location` opt-out.

## Works with the rest of the suite

The Resource Navigator's live position feed is the backbone every other
"Out in the 'Verse" app reads from: the Cargo Planner and Trade Route Planner
use your current position to plan and replan legs, and Prospector's
post-drop refine loop and its belt-survey marks reuse the same
`/showlocation` capture flow used here — those marks then roll up into Org
Intel's Surveying stats. Everything here rides the app suite's single
WebSocket, so a teammate's capture or position update shows up on your screen
without a refresh.

## Tips

- Keep the watcher running in the background during a session — every
  `/showlocation` you already run for orbital navigation doubles as a live
  position update here, no extra steps.
- Toggle `fresh only` off when hunting for something rare that hasn't
  respawned recently, to see the full sighting history instead of just
  recent hits.
- If NEARBY looks sparse right after a shard hop, check `this shard` — nodes
  on your old shard are hidden by design, not missing.
- A `$$$*` badge (with the asterisk) is still a strong signal, but sell it
  refined, not raw — the raw ore has no direct market row.

---
<sub>Part of the <a href="./README.md">SC Org Navigator app suite</a>. Design/reference spec: <a href="../product-overview.md">docs/product-overview.md</a>.</sub>
