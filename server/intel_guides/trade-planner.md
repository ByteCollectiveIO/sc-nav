---
key: trade-planner
order: 3
title: Trade Route Planner: getting started
summary: Find the most profitable buy and sell loop your ship can carry on live prices, then run it leg by leg and report what the kiosks really had.
tags: app-guide, tutorial, trade-planner
activity: trading
---
## At a glance

| | |
|---|---|
| **Who** | Anyone trading commodities with their own money. |
| **What** | A planner that chains buy and sell terminals into the best loop for your hold. |
| **Where** | Launcher group Out in the 'Verse, at `#/trade`. Live guidance needs the watcher running. |
| **When** | Before you buy your first load, and again whenever a run goes wrong. |
| **Why** | Prices change all the time, and working out a loop by hand takes longer than flying it. |

## The basics

Everything lives in one panel, **PLAN A TRADE RUN**. The **SHIP** part holds facts about your ship: usable SCU (standard cargo units), where it can stop, and how it loads. The **ROUTE** part is how you want to trade. Hover a row label to see what it does. **Route rules** folds away the settings you rarely change, such as **Max spend**, **Cargo legality** and **Danger**.

Prices come from UEX, a community price site, mixed with newer prices your members actually paid. Each leg is one commodity bought at one terminal and sold at another. Kiosks sell whole containers, so a leg shows a box count as well as SCU.

The watcher, a small program on your gaming PC, reads your position when you type `/showlocation` in chat. It also spots kiosk buys and sells in your game log.

## Plan a trade run

Do this before you head to the first terminal.

1. Open the Trade Route Planner from the launcher.
2. Type your ship's name. Check usable SCU and **Can stop at**.
3. Choose **Auto-load** or **Hand-load**. Leave **Box size** on its default unless you know what you want.
4. Under **Start from**, click **my current location**, pick a place, or leave it blank.
5. Under **Plan**, pick **Auto** to let the planner choose everything, or **Pick commodities** to limit it to what you add.
6. Under **Optimize for**, keep **Best aUEC/hr** (aUEC is the game's currency) or pick another goal.
7. Open **Route rules** and set **Max spend** to the money you really have.
8. Click **Plan trade run** and check total profit and capital needed.

## Run it

Do this when the plan looks good.

1. Click **Start this run**. The buy terminal becomes your live destination.
2. At the buy kiosk, enter the container size, how many, and the total aUEC the screen showed.
3. Confirm the buy with the Bought button. The sell terminal becomes your destination.
4. At the sell kiosk, enter the SCU sold and the total aUEC you received, then click **✓ Sold**.
5. If the watcher fills in a "Terminal reported" line, check the numbers and confirm. It never confirms by itself.
6. Knocked off course? Click **re-plan from here**. Cargo already aboard stays in the plan as a leg to sell first.
7. When every leg is done, the run shows **TRADE RUN COMPLETE** and lands in **RECENT TRADES**.

## Report stock and prices

Do this when a kiosk doesn't match the plan.

1. If the buy kiosk ran out before you filled up, tick **all it had** when you enter your buy.
2. If the sell kiosk wouldn't take everything, tick **max?** on that sell row.
3. If a kiosk has nothing to sell, use the no-stock button on the buy step. It skips the leg and files a report.
4. If a terminal won't buy your cargo at all, use the "won't buy here" button on the sell step. It files a report and re-plans you to another buyer.
5. If the planned container size isn't offered, open **not on this kiosk?** on the buy step and pick the size you can buy.

## Save a route

Do this for a setup you fly often.

1. Set up the form the way you like it.
2. Click **★ Save route** and give it a name.
3. Next time, click it under **SAVED ROUTES**. It plans again on current prices.

## Tips

- Enter the kiosk's total, never a per-unit price. A buy kiosk prices per container and a sell kiosk per SCU, so a per-unit figure is easy to get wrong.
- **Best return** without a **Max spend** can pick a big multiple on very little money. Set your budget first.
- Leave **only prices newer than** on. Old prices can make a route look better than it is.
- **Mixed loads** helps a big hold when one commodity can't fill it.

## Related guides

- [[guide:danger-board]]: plans avoid reported pirates by default
- [[guide:cargo-planner]]: shares your ship profile and avoided locations
- [[guide:org-intel]]: finished runs feed the org trading stats
