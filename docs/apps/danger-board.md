# Danger Board

> Warn your org about pirate snares and camped stations — post a danger zone,
> watch live threats on your trade lanes, or organize a hunt.
> **Route:** `#/pirates` · **Launcher group:** Rally the Org

<div align="center">
  <img src="../../images/readme_images/danger_board_screenshot.webp" alt="Danger Board: the REPORT A DANGER ZONE composer and the ACTIVE DANGERS board with severity readouts and warning cards" width="820">
</div>

## What it is

Star Citizen piracy isn't random — it's predictable. Pirates set quantum
snares along the same popular buy→sell lanes over and over, and campers sit
on the same handful of busy stations. That predictability is exactly what
makes a community warning layer worth having: the first org member snared at
Baijini Point or camped at CRU-L1 can tell everyone else in fifteen seconds,
and the tool does the rest.

The Danger Board is that layer. Any member can post a time-bound warning —
around one location or along a trade lane — tagged by who's doing it and how
bad it is. Warnings show up live for the whole org, age off on their own, and
can be refreshed by anyone who confirms the danger is still active. It's not
just a bulletin, either: the same warnings feed straight into the **Cargo
Planner** and **Trade Route Planner**, which can automatically detour your
route around a reported danger before you fly into it.

And because not every member wants to run *from* a pirate, every warning card
doubles as a recruiting post — one tap turns a danger report into a prefilled
event for hunting it down.

## How to use it

### Posting a warning

1. Open the app launcher and pick **Danger Board** under *Rally the Org*, or
   go straight to `#/pirates`.
2. In **REPORT A DANGER ZONE**, choose the type: `A location` (a camped
   station or POI) or `A trade lane` (a snare between two points).
3. Fill in **where it is**. A location uses one field, **Where's the
   danger?**; a lane uses two, **Lane start (buy end)** and **… and the other
   end**, using the same free-text-tolerant
   picker as the rest of the suite: pick a real POI for a routing-actionable
   warning, or just type what you remember ("Between Baijini and Orison") if
   you're mid-escape. An unresolved warning still posts and shows on the
   board — it just can't feed the planners' routing.
4. Set **Who** (`☠️ Players` / `🤖 NPCs`) and **How bad** (`Sighted` /
   `Active` / `Deadly`) — severity drives the card color and how wide a
   berth the planners give it.
5. Optionally add a note (up to 280 characters). If your org has a Discord
   channel set up for the Danger Board, a **📣 Announce to the org's
   Discord** checkbox appears — tick it to also post the warning there.
6. Click **Post warning**. It appears on the board and both planners' live
   danger set immediately. There's a cap on how many live warnings one member
   can have at once; if you hit it, the board tells you to clear one first.

### Reading and working the board

**ACTIVE DANGERS** leads with a glance strip — `ACTIVE`, `DEADLY`, `PLAYERS`
— then filter chips for Threat (All / Players / NPCs), Severity (Any /
Deadly / Active / Sighted), and a **Hide stale** toggle. Cards sort deadliest
and freshest first, and each shows severity, location (a lane names both
ends with `↔`), threat, whether it's a camped location or a lane snare,
system, reporter, note, and a countdown to expiry.
From a card:

- **`Still there?`** confirms the danger is still active — the
  community-refresh mechanism. It resets the warning's clock and adds you to
  its confirm count, so "✓ 3 confirmed" is a real credibility signal, not one
  person's word. Confirming flips the button to `Confirmed ✓`.
- **`⚔ Organize hunt`** — see below.
- **`All clear`**, visible to the poster and admins, removes the warning
  for everyone (after a confirm) instead of waiting for it to age off.

Warnings clean themselves up: a fresh one counts down normally, flips to
**stale** (`⏳ stale · <time left>`) near expiry, and drops off if nobody
confirms it before age-off (defaults 40/60 minutes, admin-tunable in the
**DANGER BOARD** panel under **Settings → Apps**). Whenever at least one
warning is active, a `☠️ N dangers` badge appears on the app launcher,
linking to `#/pirates`.

### Organizing a hunt

Click **⚔ Organize hunt** on any card and the app jumps to `#/events/new`
with a **Create Event** already filled in: a title built from the danger's
location, a description summarizing the reported severity/threat and your
note, category set to `PvP`/`PvE` and type set to `Combat Patrol` (players)
or `Bounty Hunt` (NPCs) to match, the event location, and a starting `Combat
(Ship)` role request for 3. You still review it, set the date/time, and
confirm before it posts.

## Features

- **Two warning shapes** — a `point` warning covers one location; a `lane`
  warning covers the corridor between two anchor POIs.
- **Threat and severity tagging** (`pvp`/`pve`,
  `sighted`/`active`/`deadly`) shown on the card and used by the planners'
  routing.
- **Community-refreshable, self-ageing** — anyone can confirm; stale reports
  vanish on their own with no admin curation needed.
- **Glance-read stats strip** — active/deadly/player counts, reflecting the
  whole board regardless of the current filter.
- **Filterable, deadliest-first board** — by threat, severity, and hide-stale.
- **Opt-in Discord announce** — posts an embed card to the org's Danger
  Board channel: the place in the title (`Pirate snare: A ↔ B` or `Danger
  near …`), your note, then severity, threat, extra location detail, and who
  reported it, with a link back to the board. A `Deadly` report posts red,
  anything else amber. No @-mentions. If your admins have turned on the org
  image it rides along as the thumbnail, and the wording can be changed in
  **Settings → Announcements**. One announced warning per member every ten
  minutes.
- **Free-text-tolerant location** — type a description instead of hunting
  for the exact POI; it still posts, it just isn't routing-actionable
  without a resolved anchor.
- **Poster/admin lifecycle control** — clear a warning early with
  `All clear`; admins tune age-off/stale windows and the routing berth in
  **Settings → Apps**.

## Works with the rest of the suite

This is the board's biggest payoff: it's live input to routing, not just a
bulletin. Every active, anchored warning becomes a **hazard volume** — a
sphere around a point warning, or a capsule along a lane's corridor, sized by
severity and a shared, admin-tunable base radius (**Settings → Apps →
DANGER BOARD** → "Route trade & cargo runs around a danger within `N` km").
Both the **Trade Route Planner** (`#/trade`, the **Danger** row) and the
**Cargo Planner** (`#/route`, the **Pirate danger** row) read this hazard set
through the same three-way control, each with a `board ↗` link back here:
`Ignore` plans without checking the board; `Warn` plans normally but flags
any leg or stop that touches a danger; `Avoid` — **on by default in both** —
actively routes around hazards — a leg that
merely flies past a danger gets a detour waypoint ("dodge via `<POI>`, +N
km"), while a leg whose endpoint sits inside a hazard (a camped destination)
can't be geometrically fixed — the trade planner drops it as a candidate
(and says `☠ destination camped — no reroute exists` on a leg you picked by
hand), while the cargo planner keeps the stop, since contract stops can't
change, and warns you it's reported camped so you fly in ready. A plan that
dodged anything says how many legs were rerouted.

On top of the org-wide board, every planner has an **AVOIDED LOCATIONS** list:
your personal "always route around this place" picks. It's one list, saved in
your browser, shared by the Trade Route Planner, the Cargo Planner, and
Prospector's drop planner — add a place in one and it shows in all three, and
the board still applies on top of it.

During a live trade run, if a fresh warning lands on a terminal still ahead
of you, a banner pops up — `☠ New danger reported on your route.` — with a
**Re-plan around it ▸** button.

The other direction runs through the Event Planner: **⚔ Organize hunt**
prefills a Create Event using the same seed mechanism the Group Finder uses
to promote an LFG post, so danger reports and player-organized hunts share
one path into the calendar.

## Tips

- Post a lane warning, not just a point, when you can — a snare only becomes
  detour-routable once both anchors are set.
- Mid-escape with no time to pick exact POIs? Just type the location — it
  still posts and still helps.
- `Still there?` matters: a well-confirmed warning is far more trustworthy
  than a lone aging report, and it keeps a real danger from quietly expiring.
- Leave the danger control on `Avoid` in both planners — it's the only mode
  that actually changes your route.
- A station you never want to visit, warning or not? Put it on **AVOIDED
  LOCATIONS** once instead of re-posting a warning for it.
- Use severity honestly: `Deadly` gives a wider berth than `Sighted`, so
  overusing it makes routes longer than they need to be.

---
<sub>Part of the <a href="./README.md">SC Org Navigator app suite</a>. Design/reference spec: <a href="../pirate-warnings.md">docs/pirate-warnings.md</a>.</sub>
