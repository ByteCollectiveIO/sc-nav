# Event operations — attendance, payouts, loot rolls, mission records — design plan

**Status: 📐 DESIGN, not built (2026-09-27).**

Events plans an op (roles, fleet groups, signups). Nothing records what
actually *happened* once the op starts. This doc adds that: who was there, who
gets paid what, who got which loot, and a record nobody can argue with
afterwards.

## 1. Source and intent

This came out of a conversation between the maintainer and the org leader.
The leader's framing is the design constraint for everything below:

> Make it transparent so nobody can argue about payouts, loot, attendance.
> Same rules for everyone — recruit, officer, organizer, leadership.
> Everything important is decided before the event starts. If there's ever a
> dispute, the event log is the official record.

The leader's specifics, all adopted:
- Attendance is **Present / Late / Left Early / Excused / Absent**.
- Non-signups and non-org players can be added by name. Guests get money and
  loot but no permanent stats.
- The default payout is an **equal split among participants**. Expenses
  (cargo, fuel, repairs) come off the top first.
- Partial participants can be set to **Full / Half / No share**, with a
  logged reason.
- Loot defaults to a **completely random, equal roll**, with no rank or
  leadership advantage.
- Deaths and disconnects can show on a live roster. They **never** affect
  payout: "Star Citizen is Star Citizen".
- Stats track participation. They don't judge members.
- Every organizer change is logged.

## 2. Settled calls (maintainer, 2026-09-27)

1. **Rules are amendable, never hard-locked.** They're chosen before start.
   After that, any change needs a reason, is logged with before and after,
   and is flagged to every participant. With a game this buggy, a hard lock
   means a tool that only works some of the time (a crash eats half the
   loot, a contract won't share, and so on). Section 4.2 has the mechanics.
2. **All three loot modes ship: Random, Weighted and Round robin.** Every roll
   collects a **Need / Want / Pass** from each eligible player. In Weighted
   mode, Need weighs *slightly* heavier than Want. Saying Need on everything
   is free, so the only check on it is visibility: **every player's intent is
   shown to everyone at roll time and stays in the record.** Section 7 has
   the details.
3. **Linking a guest to a member is retroactive.** A guest who later joins the
   Discord server gets credit for the ops they ran as a guest. It's an
   admin-only action, so nobody can claim someone else's history. This is the
   incentive to join.
4. **A closed op posts a summary to Discord *and* gets a permanent Mission
   record card in the app.** The card can be reached from the events calendar
   and the Past board, and ad-hoc ops get one too. Section 9.
5. **Event templates bundle setup *and* rules**, with maintainer defaults:
   any member saves, org-visible, creator or admin edits, admins mark
   Official. An event takes a *copy* of its template, never a live link, and
   any difference from the template shows as a chip. Six built-in starters
   ship read-only with "copy to customize". Section 14.

## 3. The model: one Operation, two ways in

An **Operation** is the running instance of a mission.

- **From an event:** a new **Start mission** button on the event detail page
  sits next to Mark complete. The roster is seeded from `going` + `waitlist`
  signups and the fleet group plan. One event has at most one operation.
- **Ad hoc:** a new launcher tile, **Ops**, has *Quick op* → name it → build
  the roster → go. It uses the same screens and tools, with `event_id` NULL.
  The Ops tile also lists live ops and recent records.

Existing surfaces stay as they are. Events owns *planning*, Operations owns
*running and settling*. `POST /api/events/{id}/complete` stays for events that
never ran an op. Closing an operation completes its event.

### 3.1 Phases (forward-only, plus logged amendments)

| Phase | What happens | Roster/attendance | Rules | Money | Loot |
|---|---|---|---|---|---|
| **Setup** | pick rules, build roster | free edit | free edit | – | – |
| **Live** | the op is running | edits logged with reason | amend (§4.2) | expenses can be logged as they happen | items logged, rolls run |
| **Settle** | the op is over, money gets divided | amend | amend | income/expenses entered, split computed, transfers confirmed | outstanding rolls finished |
| **Closed** | the record | – | – | transfers can still be confirmed | – |

Setup → Live stamps `started_at`, snapshots the rules as **rules v1**, and
publishes the loot commitment (§7.4). Closed can be **reopened** to Settle,
with a reason. That's logged and triggers a follow-up Discord post. Closed is
not a vault. The *log* is what's immutable.

### 3.2 Who can act

- **Organizer** is the event's organizer, or whoever created the quick op.
- **Deputies** are a per-op list the organizer picks, for co-leads running a
  second squad. They get the same powers.
- **Admins** get the same powers.
- **Participants** see everything, log their own loot intent, and confirm
  transfers addressed to them.

Every write names its actor in the log. A deputy's change is attributed to the
deputy, not to the organizer.

## 4. Rules

### 4.1 The rule set (chosen in Setup)

```jsonc
{
  "shares": {                     // attendance status → default share
    "present": 1.0, "late": 1.0, "left_early": 0.5,
    "excused": 0.0, "absent": 0.0
  },
  "expenses_first": true,         // reimburse expenses before splitting
  "bonuses": "pool",              // pool | keep — achievement bonus cash (§6.1)
  "loot": {
    "mode": "random",             // random | weighted | round_robin
    "need_weight": 1.5,           // weighted only: Need = ×1.5 vs Want ×1.0
    "attendance_weight": {        // weighted only (§7.3)
      "window_days": 90, "per_op": 0.1, "cap": 2.0
    },
    "eligible": ["present", "late", "left_early"],
    "rotation": null              // round_robin: null = this op only, or a
                                  // named rotation carried across ops (§7.5)
  }
}
```

Org settings hold a **default rule set** so a quick op is one tap. The rules
panel shows each deviation from the org default as a chip, like the trade
planner's route-rule chips, so "the organizer changed the defaults for this
one" is visible at a glance.

### 4.2 Amendments

Any rule change after Setup:
- requires a reason (at least 3 characters, the same bar as share overrides);
- creates a new rules version (v2, v3…) and a log entry with a field-level
  diff;
- puts a **"Rules amended mid-op"** banner on the op and on the record, and
  it can't be dismissed;
- is **never retroactive** for results that already happened. A roll done
  under v1 stays a v1 roll. Re-running it is a separate, logged action that
  keeps the original result visible (§7.4).

A change to a share mapping *does* re-derive the split (that's usually the
point of amending it). The before and after amounts are both in the log.

## 5. Roster and attendance

### 5.1 Roster rows

One row per person in the op:
- A **member** (Discord id). Their signup status is carried over as
  `signed_up: going|waitlist|maybe|none` so "walked in" stays distinguishable
  from "signed up".
- A **guest** (free-text name up to 40 characters, optional RSI handle
  text, no Discord id).

Each row has:
- `attendance` (the five statuses; empty until marked)
- `share_override` (1.0 / 0.5 / 0, optional) and `share_reason` (required
  whenever the override is set)
- `joined_at` / `left_at` (optional, so Late and Left Early carry times)
- `group_id` (carried over from the fleet plan)

Attendance is **not** written back into `event_signups.status`. A signup is
intent and attendance is fact, and stats need both.

### 5.2 Start-of-op flow

Pressing **Start mission** gives a check-in grid: one row per signup with the
five status buttons, a **Mark remaining Absent** bulk action, and **+ Add
member** / **+ Add guest**. People can be marked while the op is Live, since
they drift in. Changing a status after it's first set is a logged edit, and a
reason is needed only once the op is past Live.

### 5.3 Guest → member linking (retroactive, admin-only)

`POST /api/admin/ops/guests/link` `{guest_row_ids[], discord_id}` rewrites
the chosen guest rows to that member. The admin picks the rows, because guest
names are free text: "Bolvangar" in March and "bolvangar" in May are
probably the same person, but a human decides. It's logged on each affected
op's record as "guest *X* linked to member *Y* by *admin*". Stats pick it up
at once, since they're derived and not stored (§10).

Matching aid: when a new member's in-game handle is bound
(`handles.discord_id`), the admin panel lists guest rows whose name or handle
matches it, case-folded like `app.handle_key`. It suggests, never
auto-links.

## 6. Money

### 6.1 The real problem is who's holding the money

Star Citizen pays aUEC into whichever wallet sold the cargo or turned in the
contract, and expenses come out of whoever paid them. So a split has to know
**who holds what**, and its output is a list of **transfers**, not just
"everyone gets 212k".

Ledger entries (`op_ledger`):
- **Income:** amount, *held by* (a roster row), a note ("Cargo sale at
  Baijini", "Bounty payout"). Three kinds, because the game pays them
  differently (confirmed in-game 2026-09-27, §12):
  - **Sale / other:** lands in one wallet (whoever sold the cargo, turned in
    the salvage, and so on). One holder.
  - **Shared contract:** the game pays the reward **in equal cuts to every
    party member who had the contract shared**, and to nobody else. It's
    entered once: what each person received, and who received it (defaulting
    to everyone who took part). That becomes one income line per recipient.
  - **Achievement bonus:** bonus cash the game pays **only to the player who
    earned it**. It isn't split by the game. One holder, flagged as a bonus so
    the `bonuses` rule (§4.1) applies.
- **Expense:** amount, *paid by* (a roster row), category (fuel, repair,
  cargo purchase, ammo, rental, other), a note.

Any participant can add an entry that names *themselves* as holder or payer
(they know what they sold). The organizer and deputies can add any entry.
Every entry shows who entered it, and edits are logged.

**Why shared-contract cuts still go in the ledger.** The game's split and
the org's split are different rules. The game pays everyone who had the
contract shared, equally. The org pays by attendance and shares. They differ
whenever:
- a guest wasn't in the party, or wasn't shared the contract;
- someone joined late and never accepted it;
- someone is on a half share;
- or expenses come off first.

Recording each cut as income that person already holds lets §6.2 compute
only the difference. When everyone had the contract, everyone is on a full
share and there are no expenses, every balance is zero and the screen says
**"The game's split already matches — no transfers needed."** Nobody has to
decide whether a contract "counts". It always goes in, and the arithmetic
shows whether anything moves.

**Bonuses** follow the rule chosen before the op:
- `pool` (the default) puts the bonus in the pot like any income. That is the
  leader's "same rules for everyone": the player who landed the kill shot
  isn't paid more for it.
- `keep` records the bonus on the ledger, so it's visible, but leaves it out
  of the pot, so the earner keeps it.

Either way it's on the record, and changing the rule mid-op is an amendment
(§4.2).

### 6.2 The split

```
pot        = Σ poolable income − (expenses_first ? Σ expenses : 0)
             // poolable = everything except bonuses under bonuses: "keep"
weight_i   = share_override_i ?? rules.shares[attendance_i]
share_i    = pot × weight_i / Σ weight
owed_i     = share_i + (expenses_first ? expenses_paid_i : 0)
balance_i  = poolable_held_i − owed_i     // + sends, − receives
```

- aUEC is an integer. Shares are rounded with the **largest-remainder**
  method so they sum exactly to `pot`. Ties go to the earlier roster row,
  which is deterministic, so a re-derive gives the same answer.
- A **negative pot** (the op lost money) is split the same way, so the
  loss is shared. The screen says so plainly rather than showing negative
  shares with no explanation.
- The split lives in `nav_core.derive_op_split` as a pure function with unit
  tests, like the rest of nav_core.

### 6.3 Transfers

`nav_core.plan_op_transfers(balances)` matches the largest sender to the
largest receiver greedily, which gives at most n−1 transfers. It's rendered
as "**Alice → Bob 142,000 aUEC**", with a copy button for the amount.

Each transfer is marked **sent** by the sender and **received** by the
recipient. Received is what counts, because the recipient is the only party
who can't overclaim. A transfer the recipient disputes gets a flag and a
note, and that's visible on the record. The op can close with transfers
outstanding. The record shows them as open, and the Ops tile nags the people
involved.

Transfers are re-derived whenever the ledger or shares change *until the
first one is marked sent*. After that, a change adds **correction transfers**
on top instead of reshuffling payments already made, and says so.

### 6.4 Transfer fee

None. A player-to-player aUEC transfer arrives in full (confirmed in-game
2026-09-27), so a transfer's amount is exactly what the recipient nets and
there's no fee setting.

## 7. Loot

### 7.1 Items and rolls

- **Loot item:** a name (catalog picker, or free text for FPS drops the
  catalog lacks), qty, an optional note, and *found by*.
- One roll per item. Stacks: roll per unit, or "roll the lot" at the
  organizer's choice when logging.

The flow for each item:
1. **Open.** The item is posted and a **Need / Want / Pass** row appears for
   every eligible player (`rules.loot.eligible` × attendance). Players pick
   their own on their device, live over WebSocket.
2. The organizer or a deputy can set an intent **on someone's behalf**
   (guests, someone mid-fight). It shows as *"Need (set by Carol)"* forever.
3. **Call it.** The organizer locks intents. Anyone who didn't respond counts
   as **Want**, which is the neutral choice: silence shouldn't be a pass or a
   claim. That default is shown.
4. **Roll.** Before the roll the screen shows **every player's intent and
   their computed odds**. After it: the winner, the full weight table, and
   the roll number.

The intent table is **part of the result**. It's shown at roll time, on the
record card, and in the Discord summary. That's the whole answer to "just say
Need on everything": everyone sees who did.

### 7.2 Random (the default)

Uniform over every eligible player whose intent isn't Pass. **Need and Want
are equal here.** The leader's default is "completely random, equal", and
Need only means something in Weighted mode.

### 7.3 Weighted

```
w = attendance_factor × intent_factor
attendance_factor = min(cap, 1 + per_op × ops_attended_in_window)   // 1.0 → 2.0
intent_factor     = need_weight if Need else 1.0                     // 1.5 / 1.0
```

- The attendance factor counts ops where the person was Present, Late or Left
  Early inside the window. **Excused and Absent count as nothing** (neither
  adds nor subtracts). Guests and brand-new members get 1.0.
- Linked guests (§5.3) count their guest ops.
- The **cap** is what keeps this from favouring veterans too much. Even at
  the cap, the best case is a veteran with Need versus a recruit with Want:
  2.0 × 1.5 = 3 against 1, so the recruit still has a real chance, and the
  odds are shown before the roll.
- `need_weight` is org-configurable, but the rule panel shows it as a chip
  and changing it mid-op is an amendment.

### 7.4 Proof the roll wasn't redone

"The server rolled it" still leaves "the organizer rerolled until their friend
won". So:

- **At Live:** the server makes a 32-byte secret seed and publishes
  `sha256(seed)` on the op. The commitment is shown on the op and on the
  record.
- **Each roll:** the result comes from
  `HMAC-SHA256(seed, "<op_id>:<roll_seq>")` mapped onto the weight table.
  `roll_seq` goes up by one per roll and is logged.
- **At Close:** the seed is revealed. The record card has a **Verify** button
  that recomputes every roll in the browser from the seed, the logged weight
  tables and the sequence numbers, then checks the hash.

A **re-roll** (for example, the item was lost to a crash) is allowed, since
§2.1 says amendable, never locked. It takes a reason, uses the next
`roll_seq`, and the voided roll stays on the record, struck through with the
reason. Anyone can count re-rolls, and no roll ever disappears.

Guests and the transparency claim: the Verify page is for members. It doesn't
make the record public (it stays behind `auth_gate`).

### 7.5 Round robin

A rotation is an ordered list of people.
- Each item goes to the **next person in the rotation who didn't Pass**.
  Passing keeps your place. It doesn't cost you your turn.
- The winner moves to the back.
- Need and Want don't change the order (they're still recorded and shown).
  Round robin's fairness comes from the rotation, and letting Need jump the
  queue would defeat it.
- **Scope:** by default the rotation is per-op, starting from a seeded
  shuffle of the roster (so the start isn't organizer-picked either).
  Optionally, a **named rotation** ("Friday bunkers") carries across ops, so
  whoever was next when last week ended is next this week. People new to a
  named rotation join at the back.

## 8. Live status (watcher)

It's shown on the live roster and **never read by payout, loot or stats**.

| State | Source | Notes |
|---|---|---|
| Online / no signal | the existing presence heartbeat | a stale heartbeat reads "no signal", which covers disconnects and crashes |
| Downed | own-player `Incapacitated` notification (seen in our reference logs) | cleared on revive/respawn (the line shape needs a capture) |
| Dead | `Actor Death` (confirmed in log by third-party parsers, per watcher-modular-hud.md §9) / corpse item-recovery lines | a player's log reports **their own** state. Other players' deaths weren't found in our corpus |

So live status works **only for members running the watcher**. Guests and
members without it read as "–". The watcher already posts position, so this
is one more field on `PositionIn` (or a small event batch on the
existing `/api/trade/transactions` pattern), with no new token scope. It
needs a Game.log capture that includes a death, an incap → revive and a
respawn before any regex is written.

**Future slice, noted here and not designed:** the same log names mission
completion and **aUEC reward notifications**. Those could pre-fill income
ledger entries as "⚡ reported by <member>'s game", as a confirm nudge and
never an auto-entry, mirroring #41's trade capture.

## 9. The Mission record

### 9.1 The card

Every Closed op (event-linked or ad hoc) has a **Mission record** at
`#/ops/<id>`, and the event detail links to it. It's a read-only page with:

- a header: name, date and duration, organizer and deputies, event link, and
  the rules (with a "Rules amended" banner if any were);
- **Attendance:** each roster row with status, times, share, and the override
  reason where one was set. Guests are marked as guests;
- **Money:** ledger, pot, per-person share, transfers with sent/received
  state;
- **Loot:** each item and each roll, with the intent table, weights, winner,
  and voided re-rolls struck through. The Verify button and the seed
  commitment;
- **Log:** the full append-only log, newest last, filterable by kind.

### 9.2 Where it's reached from

- **Events calendar:** today the calendar only renders on the Upcoming board.
  It moves to both tabs so past days are clickable. A past event with a record
  gets a ✓ marker on its calendar cell and a **Record** chip on its Past-board
  card.
- **Ad-hoc ops** have no event, so they show on the calendar as their own
  entries (distinct styling, "Quick op") and on the Past board in the same
  list, so the calendar is the complete history.
- The **Ops tile** lists "Recent records".
- Members see a **My ops** list (their own history) under Settings → Profile,
  next to their stats (§10).

### 9.3 Discord

On Close, a summary embed goes to the events webhook category (a new `ops`
notify category, so it can be split off):
- title, date, organizer
- attendance counts, plus names grouped by status
- pot, per-share amount, and transfers outstanding
- each loot item → winner (+ mode, plus "N re-rolls" if any)
- a deep link to the record

Amendments or a reopen after Close send a short follow-up ("Record amended: …
reason") linked to the same record. No @-pings on the summary. It's a record,
not a call to action. Transfer reminders are left to the Ops tile.

## 10. Stats (participation only)

Derived from `op_roster`, never stored:
- ops attended (Present / Late / Left Early)
- ops organized
- **showed when signed up** (signed up `going` → attended), per member

Visibility:
- **You** see your own.
- **Admins** see everyone's in Org Intel.
- The **public leaderboard shows attendance counts only**, never a no-show
  rate. "Stats track participation, not judging members" is where a public
  reliability score would go wrong.

Excused never counts against anyone.

## 11. Data model (sketch)

```
operations      id, event_id NULL, name, organizer_id, deputies JSON, phase,
                rules JSON, rules_version, seed_hash, seed (revealed at close),
                started_at, settled_at, closed_at, created_at
op_roster       id, op_id, discord_id NULL, guest_name NULL, guest_handle NULL,
                signed_up, attendance, joined_at, left_at, group_id,
                share_override, share_reason, linked_from_guest (bool)
op_ledger       id, op_id, kind income|expense, amount, roster_id, category,
                note, entered_by, created_at, voided_at, void_reason
op_transfers    id, op_id, from_roster, to_roster, amount, sent_at, received_at,
                disputed, note, correction (bool)
op_loot         id, op_id, item_id NULL, name, qty, found_by, note, created_at
op_loot_intents loot_id, roster_id, intent need|want|pass, set_by, set_at
op_rolls        id, loot_id, roll_seq, mode, weights JSON, winner_roster,
                voided_at, void_reason, rules_version, created_at
op_rotations    id, name, order JSON (discord_id | guest key), updated_at
op_log          id, op_id, actor_id, kind, payload JSON (before/after),
                reason, created_at          -- append-only, no UPDATE/DELETE
```

`op_log` is append-only by convention *and* by API: no route edits or deletes
it. The admin clear actions that exist for other analytics do **not** extend
to it.

Endpoints follow the existing events shape (`/api/ops`, `/api/ops/{id}`,
`/roster`, `/attendance`, `/ledger`, `/transfers/{tid}`, `/loot`,
`/loot/{lid}/intent`, `/loot/{lid}/roll`, `/phase`, `/rules`) plus
`POST /api/events/{id}/start`. The live op pushes a WS `op` frame (roster,
intents, rolls) so everyone's screen updates at once. Intent visibility
*requires* that.

**Guardrails:** guest names and every note are free text and go through
`esc()` in every template (the `.map(esc).join()` trap). Amounts are integers
bounded like the marketplace `price_auec`. `rate_limit` applies to intent and
roll writes. All routes are private under `auth_gate` (none go in
`_PUBLIC_EXACT`).

## 12. Needs in-game verification before copy or code

1. ~~**Transfer fee:**~~ **Answered 2026-09-27: no fee.** §6.4.
2. ~~**Contract sharing:**~~ **Answered 2026-09-27:** a shared contract pays
   equal cuts to every party member who had it shared; achievement bonus cash
   goes only to the player who earned it and isn't split by the game. §6.1.
   Still unknown: how the game rounds an odd total. It doesn't matter,
   because members enter the cut they actually received.
3. **Game.log lines** for own death, incap → revive, respawn, and whether any
   *other* player's death appears. It needs one capture from an FPS op.
4. **Mission reward lines** (for the future income nudge in §8).

## 13. Event form additions (FPS / PvP gaps the leader asked about)

These are optional fields on `EventIn`, shown under a "Mission details"
disclosure:
- **Mission type:** bunker, bounty (tier), Xenothreat, contested zone,
  Executive hangar, cargo, mining, salvage, other
- **Required loadout / armour class** and a medical expectation (for example,
  "bring 2 medpens")
- **Comms:** channel name
- **Rules of engagement:** PvE only / PvP if engaged / PvP hunt
- **Prerequisites:** contract or reputation needed to be shared in (some
  contracts can't be shared with players who haven't unlocked them)
- **Default op rules:** the §4.1 rule set can be pre-picked on the event,
  so signups see the payout and loot rules *before* they commit. That's the
  leader's "decided before the event starts" at its strongest.

## 14. Event templates

Organizers shouldn't rebuild the same raid, mining run or bunker op every
week. An **event template** is a saved preset that bundles the event setup
*and* its op rules, so "Load template → Raid" gives the organizer a complete
event with its rules decided in one step.

This builds on three pieces that already exist: **Clone**
(`cloneEvent` → `eventSeed`), **fleet group templates** (`group_templates`,
`POST /api/events/{id}/groups/apply-template`), and the §4.1 **rule set**.

### 14.1 What a template holds

| Part | Contents |
|---|---|
| Event fields | types, categories, title pattern, description, duration, min/max players, role targets `[{role, needed}]`, rally/event location (optional) |
| Mission details | the §13 fields: mission type, loadout/armour, medical, comms, rules of engagement, prerequisites |
| Op rules | the §4.1 rule set (shares, expenses-first, loot mode, Need weight, eligibility, rotation) |
| Fleet layout | groups embedded as the `group_templates` shape `[{name, kind, ship, capacity}]` (copied in when saved, not referenced, so deleting a group template doesn't break an event template) |

It deliberately leaves out date, time, signup deadline and people. A template
describes *what kind* of event it is, not a particular one.

### 14.2 Using templates

- **Start from template** on `#/events/new` is a picker (built-ins and Official
  first, then org templates, most-used first). Choosing one fills the form
  through the existing `eventSeed` path, including the fleet layout (applied
  on create) and the rule set. Everything stays editable before publishing.
- **Save as template** on any event's detail page (organizer/admin) is the
  main way templates get made. The org's real Friday raid becomes the
  template, and it's easier than a blank builder. The name is prompted
  through `promptDialog`.
- **Quick op from template** on the Ops tile starts an ad-hoc operation with
  the template's rules and fleet layout already set.
- Templates are managed from an **Event templates** panel on the events board
  (list, rename, edit, duplicate, delete, mark Official).

### 14.3 Copy, don't link (the fairness rule)

An event takes a **copy** of the template when it's created. Editing the Raid
template later **never** changes an event already created from it. A live
reference would let someone quietly change the loot rules on an event people
had already signed up for, which is exactly what §1 forbids.

Each event records where it came from: `template_id` and
`template_version`. Templates get a version number that goes up on every
edit. The event detail page and the signup card show:

- *From **Raid** template* when nothing was changed
- *From **Raid** template · loot: Weighted → Random · Need weight 1.5 → 1.25*
  when the organizer changed things. The diff is computed against the
  template version the event was created from and shown as chips, the same
  pattern as the trade planner's route-rule chips.

So signups can see "this one's run differently" *before* committing, and
that difference is also shown on the Mission record.

### 14.4 Permissions (maintainer defaults, 2026-09-27)

- **Any member** can save a template. Templates are **org-visible**.
- The **creator or an admin** can edit or delete one. Deleting doesn't touch
  events created from it (they hold copies). Their "From template" line then
  reads *(template deleted)*.
- **Admins** can mark a template **Official**. Official templates sort first,
  carry a badge, and are what org leadership points to as the standard rules.
  Marking a template Official, and editing one that already is, are both
  logged to a small template history (who, when, what changed), since an
  Official template's rules effectively set org policy.

### 14.5 Built-in starters

Six read-only presets ship **in code**, not as DB rows, so a release can
improve them without clobbering what an org made:

| Starter | Types | Roles (needed) | Loot | Notes |
|---|---|---|---|---|
| Mining | Mining | Miner (3), Hauler (1), Escort (1) | Random | expenses-first (refinery, fuel) |
| Salvage | Salvage | Salvager (2), Hauler (1), Escort (1) | Random | expenses-first |
| Cargo haul | Cargo | Pilot (1), Loader (2), Escort (2) | Random | expenses-first (cargo purchase) |
| Bounty / combat patrol | Combat, PvE | Fighter (4), Medic (1) | Random | ROE: PvE only |
| Bunker (FPS) | FPS, PvE | Assault (4), Medic (1), Pilot (1) | Random | loadout: medium armour, 2 medpens |
| Raid | FPS, PvP | Assault (6), Medic (2), Pilot (2), Overwatch (1) | Weighted | ROE: PvP if engaged |

Built-ins use the org default rule set (§4.1) except where the table says
otherwise. **The role lists are placeholders pending review by someone who
runs these ops**, since they have to match the org's real role vocabulary
(`roles` on `EventIn`). Built-ins offer **Copy to customize**, which creates
an ordinary org template. Once an org has a customized copy, the built-in is
hidden from the picker.

An org can turn built-ins off entirely (org setting
`event_builtin_templates`, default on) for orgs that want only their own.

### 14.6 Data model

```
event_templates  id, name, version, official (bool), created_by, created_at,
                 updated_at, uses (int, for "most used" sort),
                 event JSON        -- §14.1 event fields + mission details
                 rules JSON NULL   -- §4.1 rule set (NULL until slice 2 lands)
                 groups JSON       -- fleet layout
                 builtin_key NULL  -- set on a "copy to customize"
event_template_history  template_id, version, actor_id, action, snapshot JSON,
                 created_at      -- append-only
events           + template_id NULL, template_version NULL, template_name NULL,
                 template_snapshot JSON NULL, details JSON, rules JSON NULL
```

**As built (slice 1):** the event stores a full **snapshot** of the template
contents it was created from (`template_snapshot`), and the deviation chips
diff against that snapshot. The template history is not consulted. This keeps
the diff correct for built-ins (whose old versions live only in past
releases) and for deleted templates, and it needs no lookup. `event_templates`
uses `AUTOINCREMENT`: a reused rowid would graft a deleted template's history
onto the next template and re-point its events. `rules` is not on `events`
yet; it arrives with slice 3.

Endpoints: `GET/POST /api/event-templates`, `PATCH/DELETE
/api/event-templates/{id}`, `POST /api/event-templates/{id}/official`
(admin), `POST /api/events/{id}/save-template`. Built-ins are merged into
`GET` with `builtin: true` and a string id (`builtin:raid`). Input caps follow
`EventIn`.

## 15. Build slices

1. **Event form additions + event templates** (§13, §14). This is useful on
   its own, before any op tooling exists, because it saves organizers setup
   time on regular events right away. Templates store `rules` from day one
   (NULL until slice 3), so no migration is needed later.
2. **Operations + attendance + guests + log.** The `operations`/`op_roster`/
   `op_log` tables, Start mission, quick op (including quick op from a
   template), the Ops tile, the check-in grid, and phase transitions. The
   Mission record card is attendance-only at this point, plus the calendar
   and Past-board reachability.
3. **Money.** Ledger, `derive_op_split`, `plan_op_transfers`, sent/received,
   correction transfers, amendments, the rule-set panel with org defaults,
   and the rules section in templates plus the §14.3 deviation chips.
4. **Loot.** Items, intents over WS, the three modes, seed commitment and
   Verify, re-rolls, named rotations.
5. **Close-out.** The Discord summary, the `ops` notify category, the
   follow-up posts, stats (profile + Intel + attendance leaderboard), and
   guest linking with handle-match suggestions.
6. **Watcher live status** (§8). Blocked on the §12.3 capture.
