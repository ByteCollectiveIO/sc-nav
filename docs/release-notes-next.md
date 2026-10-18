# Release notes — next release (draft)

Unreleased work on `main` since **v1.21.0**, written up for the release PR.
`/deploy` reads `git log v1.21.0..main`; this file is the ready-made text for
those commits so the notes don't have to be rebuilt from PR bodies. Add any
later feature to it, paste the highlights into the release PR, then delete it
in that PR (or clear it for the following release).

Suggested bump: **minor** (new user-facing features, watcher unchanged).

---

## Highlights since v1.21.0

**Inventory pickup runs (#48) — plan a route from what you're holding**
- **Plan a pickup run (#272):** tick holdings in Inventory → My holdings, pick a
  station or a goal's drop-off, and the cargo planner opens pre-filled with one
  row per holding. Planning, run mode and history work as for any cargo run.
- **Goal drop-off + "Plan my delivery" (#272):** a goal can name a drop-off
  station. Contributors get **📦 Plan my delivery**, which carries only what
  they've actually stocked; pledges not gathered yet are listed as "still to
  gather" and never become stops. Items counted per piece ride along as 0-SCU stops.
- **Split into trips (#272):** a new cargo planner checkbox, **split into trips
  if it doesn't fit** (off by default, remembered per player), plans several
  trips instead of "won't fit" when the cargo is bigger than the hold. Works for
  contract runs too (not multi-pickup deliveries).
- **Sell mode (#273):** **💰 Sell at best price** collects the ticked holdings
  and picks the buyer for each commodity with your ship, start and danger
  settings — best aUEC/hour or most aUEC, optionally one stop only. Shows each
  sale (price, price age, ⚡ org prices, demand left over) and two alternatives.
  Uses the trade planner's prices; contraband keeps its buyers (held cargo is
  never legality-filtered); camped stations are used only when they're the
  only buyer, and flagged.
- **Split sales (#274):** when a buyer won't take everything, the rest can go
  to the next-best buyers; a holding split between buyers shows as two rows.
- **Delivered + inventory follow-up (#274):** contributors **Mark delivered**;
  the goal's creator can **Mark received**; goal lines show "📦 N delivered".
  When a run planned from Inventory finishes, one click updates your inventory:
  deliveries move the stock to the drop-off (pledges follow it) and mark goal
  contributions delivered; sell runs take sold stock off.
- **Sell my leftover (#274):** a met goal offers to sell your free stock of its items.
- **Discord (#274):** goal posts and the goal-met ping name the drop-off.
- **Organizer view (#275):** a goal's creator sees **Where the materials are** —
  every contribution by the place it's staged, who holds what, what's left to
  deliver, and each place's travel time to the drop-off.

**Event calendar (Google, Apple, Outlook)**
- **Add to calendar:** every upcoming event page links to Google Calendar,
  Outlook.com, and an `.ics` download for Apple Calendar and anything else. It's a
  one-time copy, so a later reschedule doesn't follow it.
- **Calendar subscription (admin opt-in):** when an admin turns on Settings →
  Apps → CALENDAR SUBSCRIPTIONS, each member can make a private calendar link in
  Settings → Calendar. Their calendar app then keeps events up to date, including
  time changes and cancellations. Each member picks "events I'm in" or "all org
  events". Only the basics go out: title, time, rally point, event location, and
  a link back. Ad-hoc ops never appear.
- Google Calendar refreshes subscriptions on its own schedule (often 12–24 h);
  Apple Calendar and Outlook check about hourly. Discord pings stay the place for
  last-minute changes.

**Fixes**
- Holdings logged under a station's older name (e.g. "Wide Forest Station
  (ARC-L1)") now resolve to the renamed POI ("ARC-L1 Wide Forest Station")
  instead of reading as "not a known place" (#276).

**Tooling / research**
- Game.log live capture profiler, mining/QT and salvage session findings
  (#270, #271) — tools and docs only, no app change.

**Notes for self-hosting orgs:**
- Database changes apply automatically on start: new columns
  `inventory.location_poi_id`, `goals.dropoff_poi_id` / `dropoff_name`,
  `inventory_allocations.delivered_qty`. No settings to change.
- Older holdings have free-text locations; they're matched to a station by name
  where that's unambiguous, otherwise the planner asks the player to pick it.
- Sell mode is only as good as the loaded prices (UEX + org reports); the panel
  shows each price's age.
- Goals without a drop-off post to Discord exactly as before.
- **Calendar subscriptions are OFF by default.** A member's link works without
  signing in (calendar apps can't sign in), so turning it on is your call. Google
  fetches the feed from its own servers: the `/cal/` path has to be reachable from
  the internet (an access gate in front of the app needs an exception for it).
  Set `SC_NAV_PUBLIC_URL` so links use your public address. Turning the setting
  off deletes every member's link. New table `calendar_feeds` is created automatically.

Design + as-built detail: [inventory-pickup-runs.md](inventory-pickup-runs.md),
[event-calendar.md](event-calendar.md),
[implementation-notes.md](implementation-notes.md) › Resource manager / Cargo planner.
