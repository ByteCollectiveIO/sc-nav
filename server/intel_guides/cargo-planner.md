---
key: cargo-planner
order: 2
title: Cargo Planner: getting started
summary: Turn a stack of hauling contracts into one run in the best stop order, check it fits your ship, then fly it stop by stop.
tags: app-guide, tutorial, cargo-planner
activity: hauling
---
## {orange}At a glance{/orange}

| | |
|---|---|
| **Who** | Anyone flying hauling contracts, solo or for an org goal. |
| **What** | A route solver that orders your pickups and drop-offs to fit your hold. |
| **Where** | Launcher group Out in the 'Verse, at `#/route`. Run mode needs the watcher running for live guidance. |
| **When** | After you accept contracts in game and before you leave the station. |
| **Why** | The game never tells you the best order, or whether all the cargo fits at once. |

## {orange}The basics{/orange}

The page is one form, top to bottom. **SHIP** is your ship and its usable SCU (standard cargo units, the game's cargo measure). **START FROM** is where you begin. **PACKAGES** holds one row per contract line: commodity, SCU, from and to. **Pirate danger** decides how the plan treats warnings on the Danger Board.

Two ideas matter most. Every package is picked up before it is dropped off, and your hold never goes over its usable SCU. And the app never completes a stop for you: you move the cargo in game, then tick each package off.

Run mode uses the watcher, a small program on your gaming PC that reads your position when you type `/showlocation` in chat. You can plan without it.

## {orange}Plan a hauling run{/orange}

Do this right after accepting a batch of contracts.

1. Open the Cargo Planner from the launcher.
2. Type your ship's name under **SHIP**. Check the usable SCU it fills in, correct it if your real fit differs, and tick **remember**.
3. Under **START FROM**, pick the station you're at, click **my current location**, or leave it blank to let the planner choose the first stop.
4. Click **+ add package** for each contract line and fill in commodity, SCU, from and to. Give lines from the same contract the same contract label.
5. For a contract that gives only a total across several pickup points, use **+ add multi-pickup delivery** instead and list every pickup.
6. Optionally enter each contract's aUEC (game currency) reward in the payouts section to see aUEC per hour.
7. Leave **Pirate danger** on **Avoid** unless you have a reason not to.
8. Click **Plan route** and read the stop list and the peak load.

## {orange}Fly the run{/orange}

Do this once the plan says everything fits.

1. Click **Start this run**. Your first stop becomes your live destination.
2. Type `/showlocation` in game as you travel to update bearing, distance and the nearest QT (quantum travel) marker.
3. At each stop, move the cargo in game, then tick each package on the stop's checklist.
4. The run moves to the next stop once every package at a stop is ticked. Use **skip to next stop** to move on early, or **abandon** to drop the run.
5. On **ROUTE COMPLETE**, click **Plan another route** to start the next batch with your ship kept.

## {orange}Handle a bundle that doesn't fit{/orange}

Do this when the plan says the cargo won't fit your hold.

1. Tick **split into trips if it doesn't fit** below **Plan route**.
2. Click **Plan route** again. The stop list marks where each trip starts.
3. If it still won't fit, remove a contract or fly a bigger ship.

## {orange}Haul your own inventory{/orange}

Do this to deliver or sell items you've logged in Resource Manager.

1. Open Resource Manager's inventory and stay on **My holdings**.
2. Tick the holdings you want to move.
3. To deliver them, type a destination or choose a goal's drop-off, then click **Plan pickup run**.
4. To sell them, click **Sell at best price** instead. The planner then picks a buyer for each row.
5. On the Cargo Planner, pick a location for any row with an empty from box.
6. Click **Plan route**, then fly it as above.
7. On **ROUTE COMPLETE**, use the button offered there to update where those holdings are, or take sold items off your inventory.

## {orange}Tips{/orange}

- Two contracts running both ways between the same two places are fine. The same stop can appear twice in the plan, once to drop and once to pick up.
- **Avoid** never drops a contract stop. A stop reported as camped stays in the plan with a warning, so fly in ready.
- **RECENT HAULS** keeps your frequent lanes as one-click rows, and **clone** on a past run refills the whole form.

## {orange}Related guides{/orange}

- [[guide:danger-board]]: plans route around reported pirates
- [[guide:resource-manager]]: pickup and sell runs start from your inventory
- [[guide:trade-planner]]: shares your ship profile and avoided locations
- [[guide:org-intel]]: finished runs feed the org hauling stats
