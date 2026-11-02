---
key: navigator
order: 1
title: "Resource Navigator: getting started"
summary: Live bearing and distance from your in-game position to any place, ore node or creature your org has logged, and the forms to log new ones.
tags: app-guide, tutorial, navigator
---
## {orange}At a glance{/orange}

| | |
|---|---|
| **Who** | Any member who flies, mines or explores and wants the org's finds on a second screen. |
| **What** | Live navigation to org-logged places, ore nodes, plants and creatures, plus the forms to log your own. |
| **Where** | Launcher group Out in the 'Verse, route `#/nav`. You need the watcher running on your gaming PC first. |
| **When** | During a play session, open on a laptop or phone next to your keyboard. |
| **Why** | The game has no marker for "the node someone found last week". This app does. |

## {orange}The basics{/orange}

The watcher is a small program on your gaming PC. When you type `/showlocation` in the in-game chat, the game copies your coordinates and the watcher sends them to the app. Every fix updates the readouts at the top: CURRENT CONTAINER (the planet, moon or station you're near), BEARING, DISTANCE, ALTITUDE, SPEED and LAT / LON.

The rest of the screen:

- DESTINATION shows what you're steering toward, with a **clear** button.
- MAP is a north-up map around your last fix. **Layers & settings** holds the layer toggles and filters.
- RESOURCE FORECAST ranks the ores and harvestables most likely near you.
- The SURVEY band holds the capture forms for ore nodes, fauna and harvestables.
- NEARBY lists what's close to you. SEARCH looks through everything the org has logged.
- ELEMENT FINDER answers "where do I find this ore?"

Nothing updates until you run `/showlocation` again, so re-run it as you travel (the up arrow in chat repeats it).

## {orange}Set up the watcher{/orange}

Do this once, on the PC where you play.

1. Click **Setup** in the header to open the setup guide (`#/setup`).
2. Click **Download my watcher** and unzip the bundle somewhere easy to find.
3. Install Python from python.org if the PC doesn't have it, and tick "Add python.exe to PATH" in the installer.
4. Double-click `run_watcher.bat`, type your in-game handle, and choose an overlay or none.
5. Leave the window open (minimized is fine) and type `/showlocation` in game.
6. Wait for the setup page to say the watcher is connected, then open `#/nav`.

The download holds a personal access token. If you change PCs, revoke the old one in Settings > Watcher (`#/settings/watcher`).

## {orange}Fly to something the org has logged{/orange}

Use this to reach a node or place someone already recorded.

1. Run `/showlocation` so the app knows where you are.
2. Look through NEARBY, or type in SEARCH to look across everything.
3. Click a row to set it as your destination.
4. Fly using the BEARING and DISTANCE readouts.
5. Run `/showlocation` again every so often to update them.

## {orange}Log an ore node{/orange}

Use this when you scan a rock worth sharing.

1. In ADD RESOURCE NODE, type or pick the ore. Ores likely in this spot are listed first.
2. Enter the Q (quality, 0 to 1000) from the ore's line on your scan. The band fills in by itself.
3. If the scan lists that ore more than once at different qualities, click **+ quality lines** and enter each line.
4. Pick the biome and add a note if you like.
5. Click **Arm /showlocation capture**.
6. Type `/showlocation` in game. Your next position becomes the node's location.

Fauna and plants work the same way in ADD FAUNA & HARVESTABLES. For a place rather than a resource, use ADD CUSTOM POI.

## {orange}Find where an ore spawns{/orange}

Use this when you know the ore but not where to find it.

1. Pick the ore in ELEMENT FINDER.
2. Choose the order: most likely, nearest, or best value (from here).
3. Read the table. GO TO is the spot, JUMP TO (QT) is the quantum marker to aim for.
4. Click a row to set it as your destination.

## {orange}Name a mining area{/orange}

Use this when you work the same patch of ground often.

1. Stand in the area on a planet or moon and run `/showlocation`.
2. At the end of the SURVEY band, pick a radius.
3. Click **Name this area**, type a name and click **Create**.

Every sighting ever logged inside the circle joins it, and new ones keep joining. The area shows in Prospector's ATLAS.

## {orange}Tips{/orange}

- If NEARBY looks empty after a server change, check the shard filter in **Layers & settings**. Nodes from other shards are hidden on purpose.
- Untick fresh only to see old sightings, which may have respawned elsewhere.
- Use the pin on a node row to keep it on your own list. Use mark mined when a node is used up, so the org stops flying to it.

## {orange}Related guides{/orange}

- [[guide:prospector]]: named areas and belt surveys show up there
- [[guide:cargo-planner]]: plans from the live position this app receives
- [[guide:trade-planner]]: plans and replans from your live position
- [[guide:resource-manager]]: a named area can become a survey goal
- [[guide:org-intel]]: your destination links to its dossier
