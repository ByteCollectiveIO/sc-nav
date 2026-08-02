# UEX data contribution — post prices back to the community feed (backlog #39) — design plan

**Status: 🔨 SLICE 0 BUILT 2026-08-02 (v0.87.0); outbound half still 🅿 PARKED
2026-07-25.** The parked half depends on facts we can only learn with a real
UEX key (§3), and the external-posting story carries reputational risk we
don't have to take to get most of the value. **Slice 0 (§6.1) — the local
price overlay — is now built**, and better than designed: #41 (trade
transaction capture, [trade-transaction-capture.md](trade-transaction-capture.md))
made its observations *log-derived* rather than typed, so the ledger fills
with zero manual entry. Org prices overlay the feed per (terminal, commodity,
side), newest-wins vs the scrape stamp, with per-side freshness, `⚡ org`
badges on plan legs + the best-trades board, side-aware `max_price_age`
filtering, and `is_missing=1` observations from stockout/demand-out skips.
Ingest-time observations are gated on both learned mappings having ≥2 human
validations (`_ORG_PRICE_MIN_CONFIRMS`); confirm-path observations always
count. `id_commodity` now rides the price points, as §6.1 asked.

Companion to [trade-route-planner.md](trade-route-planner.md) (#21/#34, the app
this lives in) and the shipped stock/demand reports (#21 step 6), whose data
model this extends rather than forks.

---

## 1. The idea

Run mode already asks the player, at the terminal, for **the real price and the
real SCU** they just transacted (`TradeRunPatchIn.price/scu`,
`app.py` ~3315; consumed ~3790–3810). We use those numbers once — to compute
realized profit — and then discard them.

That is exactly the observation UEX's community price feed is built from. Two
things follow:

1. **Locally**, our own org is flying on price data that can be up to
   `feed_refresh_h` old (6 h default, 2 h hard floor — see #33) while a member is
   standing at the terminal reading the live number off the screen. We should
   trust our own member over a stale scrape.
2. **Upstream**, we could hand those observations back to UEX and stop being a
   pure consumer of a community-run dataset we depend on.

These are separable, and (1) is worth more to us than (2). §6 sequences them so
that the risky half is optional.

## 2. What UEX's submit API actually wants

`POST https://api.uexcorp.uk/2.0/data_submit`
([docs](https://uexcorp.space/api/documentation/id/post_data_submit/), read
2026-07-25)

Header: `secret-key: <the user's secret key from their UEX profile>`.

```jsonc
{
  "id_terminal": 12,          // UEX terminal id
  "type": "commodity",        // commodity | item | vehicle_buy | vehicle_rent
  "is_production": 1,         // 1 = LIVE servers, 0 = test
  "game_version": "4.9",      // optional, defaults to LIVE
  "screenshot": "<base64>",   // optional PNG/JPG ≤10 MB — but see §3/§4.1
  "prices": [                 // ≤ 500 rows
    { "id_commodity": 5,
      "price_buy": 3800,      // or price_sell — per SCU, player's side
      "scu_buy": 120,         // or scu_sell — inventory seen
      "status_buy": 4,        // or status_sell — 1–7 band, see below
      "is_missing": 0,        // 1 = not offered here at all
      "quality": 0 }          // optional 0–1000
  ]
}
```

**Status scale** (`/2.0/commodities_status`, both sides): 1 Out of stock ·
2 Very low · 3 Low · 4 Medium · 5 High · 6 Very high · 7 Maximum (sell-side 7
reads "no demand"). It is a *percentage band of that terminal's capacity*, not
an absolute SCU figure — which matters, see §4.2.

**Response**: `{ ids_reports, date_added, username }` with a string status —
`ok`, `invalid_input`, `missing_secret_key`, `invalid_secret_key`,
`max_rows_exceeded`, `duplicated_report`, `screenshot_required`,
`invalid_game_version`, `user_not_found`, `user_not_allowed`, `user_disabled`.

**Limits**: 500 rows/submission · 1 000 reports per 30 min · no duplicate report
for the same item+location within 5 min · **new datarunners must attach a
screenshot for their first 90 days**.

## 3. Open questions — verify before any of §6.2 is estimated

These are cheap (≈30 min with a real key against `is_production: 0`) and every
one of them can move the design:

1. **Header contract.** The doc page describes the auth as "Bearer Token (via
   `secret-key` header)", which is ambiguous. UEX 2.0 generally issues an *app*
   `api_key` (used as `Authorization: Bearer …`) **plus** a per-user
   `secret-key` for attribution. If that's true here, the two options framed at
   the outset ("player's token" vs "org app token") are **not either/or — it's
   both at once**, and §5's hybrid becomes the obvious model rather than a
   compromise.
2. **Is `status_*` required**, or is `price_* + scu_*` accepted alone? Drives
   §4.2 entirely.
3. **Which host accepts POST** — we read feeds from *both* `api.uexcorp.uk`
   (commodities, vehicles) and `api.uexcorp.space` (items, terminals, prices);
   the submit doc says `.uk`.
4. **Does the screenshot rule apply to our org account** (is it already an
   established datarunner?) — the single biggest gate on §6.2, see §4.1.
5. **App registration** — UEX has a "My Apps" registration flow. Assume we must
   register before posting, and that posting unregistered is a good way to lose
   read access we depend on.

## 4. The three real problems

### 4.1 The screenshot rule may make per-user tokens unusable

New datarunners must attach a screenshot for 90 days or the call returns
`screenshot_required`. **We are a server-side web app — we cannot produce an
in-game screenshot.** The player's screen is on their Windows box, not on ours.

Consequences:

- A member who links a fresh UEX key will have every submission rejected until
  they've established themselves on uexcorp.space *by hand*, outside our app.
  Any per-user design must treat `screenshot_required` as an expected,
  first-class outcome with honest UI copy — not an error to retry.
- An org account that is already an established datarunner has no such problem,
  which is the strongest argument for the org token being the default path.
- The watcher (`watcher/`, already running on the player's Windows box) *could*
  technically capture a screenshot. That is a large scope jump into
  privacy-sensitive territory for a feature the org may not need at all.
  **Explicitly out of scope**; noted only so it isn't rediscovered as a bright
  idea later.

### 4.2 Deriving the 1–7 status band

Status is a fraction of terminal capacity, and we don't know a terminal's
capacity. Options, in preference order:

- **(a) Omit it** if §3.2 says it's optional. Preferred.
- **(b) Derive a proxy.** The price feed rows carry `scu_buy_avg` /
  `scu_sell_stock_avg` alongside the live figures; a capacity proxy of
  `max(observed, avg × 2)` clamped to 1–7 is crude but honest-ish, and can be
  labelled as derived in `details`.
- **(c) Ask the player.** A 7-way segmented control mid-cockpit-flow, at the
  moment they're trying to get back in the ship. **Rejected** — this violates the
  one-tap principle the survey stack already established; we don't tax the
  player for a field we can approximate.

### 4.3 We'd be writing into a community dataset under one identity

Garbage in gets a key banned, and under the org-token model **one careless
member burns everyone's** — including, plausibly, the read access the entire
trade planner depends on. Non-negotiable guardrails:

- Submit **only** on an explicitly *typed* price. Plan-derived figures are UEX's
  own numbers echoed back — resubmitting them is noise at best, a feedback loop
  at worst.
- **Never** for manual legs without a resolved `terminal_id`, and never for the
  `carried cargo` pseudo-leg (`nav_core.py` ~3782, `buy_terminal_id: None`).
- **Sanity gate**: deviation beyond ±50 % from the last known feed price for that
  (terminal, commodity) does not post — it holds for admin review. A fat-fingered
  38 000 instead of 3 800 must not reach UEX.
- Honor the 5-minute duplicate window and the rate limits **centrally**, in the
  worker (§6.2), not at each call site.
- Every submission is logged with its response, admin-visible. If UEX ever
  complains, we can answer precisely.

## 5. Credentials & attribution model

**Recommended: hybrid**, pending §3.1.

- Org-level **app key** identifies the app (registered with UEX), stored via the
  existing `db.get_setting`/`set_setting` meta helpers.
- Optional per-member **secret-key** for attribution — UEX gives datarunners
  standing/rank, and members who already run data should get credit for what they
  report through us. Members who haven't linked one fall back to the org
  datarunner account (which, per §4.1, is the one that actually works).
- Master org switch, default **off**. Per-member opt-in, default **off**.

Storage notes:

- Watcher tokens are *hashed* (`watcher_tokens.hash`) because we only ever need
  to verify them. We must **replay** a UEX secret, so hashing is not available —
  it's plaintext next to your backups unless we encrypt. Minimum bar: encrypt
  with a key from the environment (so a DB leak alone isn't enough), and make the
  API **write-only** — return `present: true` + `•••• last4`, never the value.
- New table rather than a `members` column: `uex_accounts(discord_id PK,
  secret_enc, uex_username, enabled, last_ok, last_error)` — it carries state
  (last result, disabled-after-failure) that doesn't belong on the identity row.

**Privacy/legal deliverable, easy to miss:** today the Privacy page lists
uexcorp as a service provider we *read from* (`index.html` ~4142). Posting
inverts that — a member's trading activity (terminal, commodity, price,
timestamp) leaves the org, tied to an identity, to a third party. Terms +
Privacy both need a line before this ships, not after.

## 6. Slices

### 6.1 Slice 0 — local price overlay (no UEX involvement) ✅ build this first

Uses data we already collect; zero external dependency; fixes the staleness
complaint that motivated the whole idea.

- New table `price_reports(id, terminal_id, poi_id, commodity, side, price, scu,
  is_missing, reporter, reporter_name, created, uex_state)` — one ledger, with
  `uex_state` (`none|pending|sent|failed|held`) reserved so slice 1 does **not**
  introduce a second parallel concept.
- **Relationship to the shipped `stock_reports` board (`db.py` ~425):** keep it.
  That table is the *routing-avoid* board with its own age-off and solver
  semantics (`nav_core.stock_avoid_buys/_sells`); `price_reports` is the
  *observation ledger*. The confirm handler writes both; `stockout`/`demandout`
  additionally emit a `price_reports` row with `is_missing=1`. Two roles, two
  tables, one write path — documented here so nobody forks a third.
- `_serialize_trade_price` (`app.py` ~1325) grows an overlay: for a (terminal,
  commodity, side) with a fresh org report, prefer it over the feed row until a
  feed row with a newer `date_modified` arrives.
- Keep `id_commodity` in that same serializer (currently dropped) — free now,
  required by slice 1.
- UI: freshness badge distinguishing **"org-reported, 12m ago"** from
  **"UEX, 3h ago"** on plan legs and the best-trades board.

*≈250–350 LOC, one session.*

### 6.2 Slice 1 — submission core, org token

- Thread `id_commodity` onto legs (`nav_core.py` ~3129–3131 already carries
  `terminal_id`; this is the last missing identifier).
- Payload mapper + the §4.3 sanity gates.
- **Queued, never inline.** A UEX outage must not stall a player mid-run: the
  confirm handler enqueues, a background worker drains — modelled on
  `feed_refresh_loop()` (`app.py` ~2673). Rate limits, the 5-min dedupe, backoff
  and the audit log all live in the worker.
- ORG SETTINGS panel: master switch, app key, last-result readout.
- On a successful submit, patch the in-memory `trade_price_points` row too —
  though slice 0 has already made that mostly redundant, which is the point.

*≈400–500 LOC, one session. Estimate is only valid after §3.*

### 6.3 Slice 2 — per-user keys, consent, attribution

Encrypted key store, Settings panel (write-only field), per-member opt-in, and a
per-run "📡 share with UEX" affordance. Honest `screenshot_required` copy per
§4.1. *≈250 LOC.*

### 6.4 Slice 3 — guardrails & visibility

Held-report admin review queue, retry/backoff policy, submission log view, and a
"N reports contributed" stat (Org Intel Trading is the natural home). *≈200–300
LOC.*

**Total ≈1 000–1 400 LOC** across `app.py`, `db.py`, `index.html` + tests —
about three focused sessions with 2+3 sharing one, *plus* the §3 verification
pass, which gates everything from 6.2 down.

## 7. Why parked (and what would unpark it)

Parked because the value is lopsided: slice 0 delivers the staleness fix on its
own, while slices 1–3 add an external dependency, a credential-handling burden, a
privacy-policy change, and a way for one member's typo to cost the org its read
access to the feed the trade planner is built on.

Unpark when any of these becomes true:

- The org wants to be a visible contributor to UEX (community standing is a real,
  stated goal — not a side effect).
- §3.4 comes back favourable: the org account is an established datarunner, so
  submissions actually land without screenshots.
- Members ask for it — specifically, members who already run data for UEX by hand
  and would rather it happen automatically while they haul.

Slice 0 needs none of that and shouldn't wait for it.
