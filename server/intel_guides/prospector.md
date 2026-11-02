---
key: prospector
order: 4
title: "Prospector: getting started"
summary: The org's shared mining map: which areas on moons and in the belts are worth going to, plus help marking rocks and dropping into belts.
tags: app-guide, tutorial, prospector
activity: mining
---
## {orange}At a glance{/orange}

| | |
|---|---|
| **Who** | Miners and surveyors, and anyone choosing where the org should mine next. |
| **What** | A shared map of named mining areas, a live screen for marking belt rocks, and a planner for dropping out of quantum into a belt. |
| **Where** | Launcher group Out in the 'Verse, route `#/halo`. FIELD and the drop result need the watcher running. |
| **When** | Before a mining run to pick a spot, and during it to log what you find. |
| **Why** | Every member's finds add up to one map, so nobody has to rely on rumours. |

## {orange}The basics{/orange}

Prospector has three tabs at the top, and a STANTON, NYX or PYRO switch that sets the system for all of them.

- ATLAS (`#/halo`) lists the org's named areas. It works without the game running.
- FIELD (`#/halo/field`) is for when you're at the rocks. It reads your live position from the watcher, the small program on your PC that sends your `/showlocation` coordinates.
- DROP (`#/halo/drop`) plans a jump into a belt. Belts have no quantum markers, so you jump past one between two markers and exit quantum (hold B) at the right distance.

There are two kinds of area. A surface area is a circle on a planet or moon, named from the Resource Navigator. A belt zone is a named asteroid field, created here. Every sighting inside an area counts toward it.

Each area has a health score and a value. Health says how far you can trust its numbers. Value says what it's worth. A well-surveyed empty area is healthy and worth nothing.

## {orange}Pick a place to mine{/orange}

Use this before a session, when you want the best spot the org knows.

1. Open ATLAS and choose your system at the top.
2. Read the SURVEY ZONES table. VALUE shows $ to $$$, and WHAT'S HERE lists the top finds.
3. Search by ore, body or zone name to narrow the list.
4. Click **Details** on a row to open its card.
5. Check the health bars and how fresh the evidence is.
6. Click **Set destination** to point the Resource Navigator at the nearest quantum marker.

A pre-patch chip in HEALTH means all the evidence is from before the current game patch. Ore spawns may have moved since.

## {orange}Mark rocks in a belt{/orange}

Use this when you're in a belt zone and want to log each rock you scan.

1. On ATLAS, click **New zone** if the field has no zone yet. It becomes your active zone.
2. Open FIELD and check that ZONE in SURVEY MARK shows the right zone.
3. To identify a rock from range, type its RS signature (the number your scanner shows) into RS SIGNATURE LOOKUP.
4. Fly to the rock, scan it, and enter its ore and Q (quality, 0 to 1000) under ROCK.
5. Set ROCKS AROUND to how dense the field is here.
6. Click **Arm /showlocation capture**, then type `/showlocation` in game.
7. If you made a mistake, click **Undo this mark** and mark again.

Found nothing? Mark it with ROCKS AROUND set to none. Empty space shows where a field ends, and it counts toward the zone's health.

## {orange}Drop into a belt{/orange}

Use this when you want to reach rock space that has no quantum marker.

1. Open DROP and pick a TARGET: Density band, My POI or Ore.
2. Pick the band, pocket or field (it depends on the system).
3. Set START FROM, or leave it blank to plan from your live position.
4. Add your SHIP if you want the drop window in seconds and fuel figures.
5. Click **Plan my drop**.
6. Set the marker it names as your quantum destination, jump, and exit when "distance to destination" in your HUD reaches the drop number.
7. Click **FLY IT** to pin the plan in FIELD, then run `/showlocation` after the drop to see where you landed.

## {orange}Turn an area into an org goal{/orange}

Use this when an area needs more evidence and you want the org to help.

1. Open the area's card from ATLAS.
2. Click **Make it a goal**.
3. Set the target, either a number of sightings or of different surveyors.
4. Post the goal in the Resource Manager form that opens.

Only evidence logged after you post the goal counts toward it.

## {orange}Tips{/orange}

- Sort SURVEY ZONES by Needs surveying to find the areas the org knows least about.
- Name a belt zone before a survey run. Without one, marks group by distance and two nearby fields can merge.
- Deleting a zone never deletes the sightings or marks in it.

## {orange}Related guides{/orange}

- [[guide:navigator]]: surface areas and node captures come from there
- [[guide:resource-manager]]: survey goals live there
- [[guide:org-intel]]: the Surveying page totals the org's survey work
