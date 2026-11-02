---
key: danger-board
order: 8
title: Danger Board: getting started
summary: Warn your org about pirate camps and snares, confirm the ones still active, and let the route planners steer around them.
tags: app-guide, tutorial, danger-board
---
## {orange}At a glance{/orange}

| | |
|---|---|
| **Who** | Every member. Anyone can post or confirm a warning. |
| **What** | A live board of short-lived pirate warnings, shared with the whole org. |
| **Where** | Launcher group Rally the Org, at `#/pirates`. No watcher needed. |
| **When** | The moment you get snared, camped or see pirates waiting. |
| **Why** | Pirates use the same lanes and stations again and again, so one warning saves the next member. |

## {orange}The basics{/orange}

The top of the page is **REPORT A DANGER ZONE**, where you post a warning. Below it, **ACTIVE DANGERS** lists every live warning, deadliest and freshest first, with filters for threat and severity and a **Hide stale** toggle.

A warning is either **A location** (a camped station or point of interest) or **A trade lane** (a quantum snare between two places). It carries who is doing it, players or NPCs, and how bad it is: **Sighted**, **Active** or **Deadly**.

Warnings expire on their own. Near the end a warning turns stale, and it drops off unless someone confirms it. Your org's admins set how long that takes.

The board also feeds the Cargo Planner and the Trade Route Planner. A warning tied to a real place on the map can make those planners route around it. One typed as free text still shows on the board, but the planners can't use it.

## {orange}Report a pirate{/orange}

Do this as soon as you're safe enough to type.

1. Open the Danger Board from the launcher.
2. Under **REPORT A DANGER ZONE**, choose **A location** or **A trade lane**.
3. Fill in **Where's the danger?**. Pick a place from the suggestions if you can, or just type where it was.
4. For a lane, also fill in the other end.
5. Pick **Players** or **NPCs** under **Who**, and a level under **How bad**.
6. Add a short note if it helps: ships, how many, what they did.
7. If the announce checkbox shows, tick it to post the warning to your org's Discord too.
8. Click **Post warning**.

## {orange}Confirm a warning is still active{/orange}

Do this when you fly past a reported spot and the danger is still there.

1. Find the warning under **ACTIVE DANGERS**.
2. Click **Still there?**. The button changes to **Confirmed ✓**.
3. The warning's clock resets and its confirm count goes up, so others know more than one person saw it.
4. If you posted it and the danger is gone, click **All clear** to remove it for everyone. Admins can do this too.

## {orange}Let the planners avoid danger{/orange}

Do this before a cargo or trade run. It is on by default.

1. In the Cargo Planner, find **Pirate danger**. In the Trade Route Planner, open **Route rules** and find **Danger**.
2. Leave it on **Avoid**. The plan adds a detour waypoint around danger on a lane.
3. Pick **Warn** if you only want legs that touch danger flagged, or **Ignore** to skip the board.
4. Plan the run and read any detour notes on the stops or legs.
5. Add a place you never want to visit to **AVOIDED LOCATIONS**. That list is yours and is shared between the planners.

The two planners treat a camped destination differently. The Trade Route Planner drops that trade. The Cargo Planner keeps the stop, because a contract stop can't change, and warns you it's camped.

## {orange}Write up a report in Intelligence{/orange}

Do this when what happened is worth keeping after the warning expires.

1. On the warning, click **Write up**.
2. A new Intelligence report opens, already filled in with the place, the threat, the time and a link back to the warning.
3. Add what you saw and what others should do about it.
4. Check how long the report stays valid.
5. Click **Publish report**.

Once a warning has a write-up, its card shows a link to it.

## {orange}Tips{/orange}

- Use severity honestly. **Deadly** makes the planners give a wider berth, so overusing it makes routes longer.
- A lane warning helps routing only when both ends are real places.
- Mid-escape, a typed location is fine. It still warns people.
- **Organize hunt** on a warning starts a prefilled event to go after the pirates, if your org lets you create events.

## {orange}Related guides{/orange}

- [[guide:cargo-planner]]: routes around warnings and flags camped stops
- [[guide:trade-planner]]: drops camped trades and detours around lanes
- [[guide:org-intel]]: write-ups live in Intelligence
- [[guide:event-planner]]: a hunt starts as a prefilled event
