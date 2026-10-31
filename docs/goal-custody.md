# Goal custody — hand delivered materials over to a holder (#49)

**Status:** 📐 design 2026-10-21, not built. Settled calls are marked
**(user)**; everything else is a recommendation open to change.

## The problem

A goal contribution is an allocation drawn from the contributor's own holding
(`inventory_allocations`, see [org-inventory-goals.md](org-inventory-goals.md)).
"Mark delivered" (#48 v2, [inventory-pickup-runs.md](inventory-pickup-runs.md))
only stamps `delivered_qty` on that allocation, so the stock stays in the
giver's inventory even after they've handed it over in-game. What a member
then does next breaks the goal:

- **Deletes the holding** (it isn't theirs any more): `db.delete_inventory`
  removes the allocations with it, so the goal **loses the contribution**,
  delivered or not. 500 Iron goal, 100 delivered, holding deleted → 0/500.
- **Edits the qty to 0**: the allocation survives, but it now reads as
  un-gathered (`short` = 100), so the goal shows 100 *promised* and 0
  delivered.

The ledger has no way to say "this stock is now someone else's, held for the
goal". This design adds that.

## The model

### Custody is a holding, not a new store

Handed-over stock is an ordinary `inventory` row. The custodian **owns** it, and
it is flagged as held for the goal. Reusing holdings brings along everything
that already works on them: lot quality (the `min_q` gate reads it),
location + `location_poi_id`, split, `move_holding`, and pickup runs. The
custodian can haul the goal's stock to the crafting spot with the cargo
planner unchanged.

New columns on `inventory`:

| column | meaning |
|---|---|
| `custody` | `NULL` = the owner's own stock (today's rows) · `'pending'` = handed over, waiting for the receiver · `'goal'` = held for a goal · `'org'` = org holdings |
| `custodian_id` | who holds it. While `pending`, the named receiver. Once received, it is the same as `owner_id` |
| `contributor_id` | who gave it. Goal progress and per-contributor credit count this person, never the custodian |
| `custody_goal_id` | the goal it's held for. For `'org'` stock, the goal it came from (provenance tag) |

The legacy `inventory.goal_id` column (the earmark from before allocations
existed) is left alone. Reusing it would mix old rows with the new rule.

Separate lots from different contributors are **never merged**, even when the
item, quality and location match, so each one keeps its credit. A custodian
holding 3 lots of Iron for one goal sees 3 rows, grouped in the UI.

### A transfer log

`custody_transfers` records every step, and doubles as the chain-of-custody
history shown on the goal:

```
id, holding_id, goal_id, qty, from_id, to_id,
status  -- pending | received | rejected | cancelled
location, location_poi_id,          -- set by the receiver
created_at, resolved_at, note
```

## The flow: two-step handover (user)

Custody is being tracked, so **both sides confirm**.

1. **Giver: "Donated → <member>".** This appears on a holding's commitment to
   an org goal. The giver picks the receiver (any member; there is no fixed
   custodian, see below) and an amount (default: the stocked part of the
   pledge, `qty − short`. The un-gathered part of a pledge can't be handed
   over, the same rule as delivery today).
   - The amount is split off the giver's holding into a new holding with
     `custody='pending'`, still `owner_id = giver`, `custodian_id = receiver`.
     The giver's allocation shrinks by the same amount. The pending lot counts
     toward the goal directly (see *Progress*).
   - The giver's remaining holding is now ordinary stock again. **Deleting it
     no longer touches what they donated.** That is the fix.
   - The giver can **cancel** while it's still pending. The lot merges back and
     the allocation is restored.
2. **Receiver: "Received".** It shows in the receiver's inventory as an inbound
   item. On accept, `owner_id` becomes the receiver and `custody='goal'`. The
   receiver **sets the location** at this point (user). The donor's location is
   kept as the default, but a ship-to-ship handover in space has no meaningful
   location, and the receiver may only know where it's going later. The
   location can be left blank and edited on the holding afterwards, like any
   holding.
   - **Reject** returns it to the giver (merged back, allocation restored).
3. **The goal creator or an admin can record a handover for any member**, the
   same receiver-side trust the delivered button has today. They still name the
   receiver, and the receiver still confirms.

### Custodian is chosen per transfer (user)

There is no single collector per goal. A long-running goal is gathered by
whoever is online, so each donation names its own receiver. A custodian can
pass goal stock on to another member with the same two-step transfer
(`from` = custodian, contributor unchanged), so the transfer log shows the
whole chain.

### What a custodian can't do with goal stock

A `custody='goal'` holding doesn't count as the custodian's free stock. It is
hidden from their available quantities, the marketplace sell/list actions, new
pledges and personal craft previews. In their inventory it shows as "holding
for <goal> · from <contributor>". The custodian can **move** it (location,
pickup run), **transfer** it (two-step), or **report it lost** (ships blow up,
and cargo is lost with them; the loss is logged in `custody_transfers` with
`to_id = NULL` and removes it from progress). Plain delete is replaced by those
three actions.

### Personal goals: none of this (user)

Personal goals are visible only to their creator, so nobody else can
contribute and there's nobody to hand over to. "Donated" isn't offered on a
commitment to a personal goal, and personal goal stock never enters the org
holdings category.

## Progress

`derive_goal_progress` keeps its inputs-as-flat-rows shape.
`list_goal_contributions` returns two kinds of rows:

- **pledges**: allocations on contributors' own holdings, unchanged
  (`short` = still gathering);
- **custody lots**: `custody IN ('pending','goal')` holdings for the goal, with
  `owner_id` reported as `contributor_id`, so credit stays with the giver.

The per-line breakdown becomes **promised → on hand → in transit (pending) →
held for the goal (received)**. The received total is what "delivered" meant
before, and it replaces the `delivered_qty` bookkeeping (see *Migration*).

## Completion: the owner chooses what happens (user)

"Met" (enough is pledged) stays the automatic flip on read. A new explicit
**Complete goal** action (creator or admin) moves the goal to a new terminal
status `completed`, and asks what to do with the stock held for the goal:

| choice | effect on custody lots | for |
|---|---|---|
| **Expended** | deleted; one `expended` row per lot in the transfer log so history survives | spent goals: a ship, a craft, a contract hand-in |
| **Add to org holdings** | `custody='org'`, still owned by the custodian, `custody_goal_id` kept as provenance | stockpile goals: "pad the org's coffers with X, Y, Z" |
| **Release to holders** | `custody=NULL`: it becomes the custodian's own free stock | a goal abandoned after gathering, nobody wants it tracked |

Recommendations on top of the settled call:

- **Choose per line item, with one goal-wide default.** A ship goal can spend
  the Titanium and bank the surplus Quantanium. The dialog shows each line with
  its held quantity and a disposition select prefilled from the default.
- **Surplus**: when held stock is greater than the need, expend just the need
  and offer a separate choice for the excess (default: org holdings).
- **A disposition intent can be set at creation** ("on completion: expend" vs
  "add to org holdings"). It only prefills the dialog, so stockpile goals don't
  ask every time.
- **Pledges still on contributors' own holdings** at completion are released
  (allocations removed, stock untouched). Those items were never handed over.
- **Pending transfers** at completion: the dialog says "N transfers still
  waiting to be received". They finish into whatever disposition was picked, or
  the owner cancels them.
- **Archive** (already exists) stays as the "stop, not done" path. Archiving a
  goal with held stock asks the same question, minus "expended".

## Org rollup: a goal / org holdings category (user)

The org rollup (`derive_inventory_rollup`) already sums every member's
holdings. It gains a split, so stock the org actually controls is visible as
such:

- **Org holdings**: `custody IN ('goal','org')`. Each lot is tagged with its
  goal (active goal, or the provenance goal for `'org'`), its **current holder**
  and its **location**, e.g. *Iron · 100 SCU · Q640 · held by <member> at Area18
  · for "Hull-C fund"*.
- **Member stock**: everything else, as today.

The item total includes both; the expandable row shows the split. Nothing is
double-counted, because a transfer *moves* qty out of the giver's holding
rather than copying it.

Org holdings (`'org'`) are then managed by admins:

- transfer to another holder (two-step, as above);
- **commit to a new goal**, the natural follow-up to a stockpile goal (bank
  Quantanium, later spend it on a ship goal). This becomes a custody lot of the
  new goal directly, with the original contributor credit kept, and no
  re-donation needed;
- mark expended or lost.

## Interactions and edge cases

- **Pickup runs (#48).** A goal run reaching the drop-off currently adds
  `delivered`. With this design it **opens a pending donation** for the hauled
  amount instead, to the goal creator by default, editable in the run summary.
  The creator receives it. It never auto-receives, for the same reason a
  detected trade never auto-confirms.
- **Off-boarding.** `POST /api/admin/members/{id}/access` revoking a custodian
  doesn't delete their goal or org lots (they're the org's). An admin
  reassigns them: a one-step admin transfer, since the holder is gone. Pending
  transfers to or from a revoked member are cancelled.
- **Deleting a goal** currently drops its allocations. With custody lots held it
  asks the same disposition question first, and refuses if the owner backs out.
- **Quality.** Lots keep `quality` through every transfer, so `min_q` gating is
  unaffected.
- **Withdrawing a pledge** only affects what's still on the contributor's own
  holding. Donated stock comes back only through a cancelled, rejected or
  reversed transfer.

## Migration

Existing allocations with `delivered_qty > 0` become **pending donations to the
goal creator**, at the drop-off (if set) or the holding's location. The
creator confirms each one in their inbound list. Guessing "received" would
invent a custody fact nobody stated. `delivered_qty` and
`POST /api/goals/{id}/delivered` are retired once the migration runs (the
endpoint answers 410 with a pointer to the transfer flow for one release).

## Slices

1. **Schema + two-step transfer + progress.** Columns, `custody_transfers`,
   donate/cancel/receive/reject, the location at receipt, custody lots in
   `list_goal_contributions`, the delivered migration. This alone fixes the
   delete bug.
2. **Custodian surfaces.** The inbound list, the "holding for" rows, restricted
   actions (move / transfer / report lost), custodian → custodian transfer,
   chain of custody on the goal.
3. **Completion.** The `completed` status, the per-line disposition dialog,
   surplus, intent at creation, archive/delete asking the same question.
4. **Org holdings.** The rollup split, admin management, commit-to-new-goal.
5. **Pickup-run handoff + off-boarding reassignment.**

## Open questions

- Discord: post on "received" (goal progress milestone) or only on completion?
  The existing goal announce path could carry both.
- Should a receiver be able to **accept part** of a pending donation (they got
  80 of the 100 in-game)? Recommendation: yes, with the remainder returned to
  the giver, and both amounts logged.
