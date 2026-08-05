# Org-launch review — Marketplace · Event Planner · Discord webhooks (2026-08-04)

**Context:** first external deployment — a second org (~180 members) whose primary
interest is the Marketplace, the Event Planner, and Discord notifications.
Four parallel deep-dives (marketplace end-to-end, events end-to-end, notify
pipeline, competitor research on sc-market.space + uexcorp.space marketplace)
produced this consolidated, prioritized plan. Status boxes updated as work lands.

## Verdict in one paragraph

Core mechanics are solid (atomic two-sided confirm, race-safe reminder claims,
self-healing roster derivation, hardened webhook dispatcher). The launch gap is
almost entirely **silent lifecycle moments** — auctions settle with no "you won",
events reschedule with no ping and no re-armed reminder, dead webhooks fail
invisibly, reminder pings silently cap at 50 of 180 members — plus **missing
at-scale surfaces** (my bids, board filters, capacity/waitlist, staleness).
One true P0: the event detail roster never matches signups to target roles.

## Package A — bugs + notification coverage (P0/P1)

- [x] A1 **P0 events**: `attendeesByRole` indexes `byRole[t]` with a `{role,needed}`
      object → `"[object Object]"` never matches; target roles always render
      "— open —" while fill bars say full. One-line fix + regression test.
- [x] A2 **Reschedule handling**: `edit_event` on `start_at` change → reset
      `reminded_at` (else a moved event never gets its reminder) + "event
      rescheduled" notify pinging active signups. Location change rides along.
- [x] A3 **Auction settlement notify**: `_resolve_listing_expiry` settles lazily
      with zero pings — no "you won" exists anywhere. Fire winner + seller pings
      on settle; "expired with N quotes" for commissions (quoters + requester).
- [x] A4 **Outbid ping**: `place_offer` auction branch — ping displaced high bidder.
- [x] A5 **Deal-exit notifications**: cancel-while-pending → notify bound buyer;
      accepted-crafter withdraw → notify requester (listing reopened).
- [x] A6 **Decline offers**: seller `action:"decline"` → status `rejected` + quiet
      notify (low-balls no longer linger "active" forever).
- [x] A7 **Chunked sends at scale**: reminder pings batch ≤50 mentions/message
      (body never truncates the pings); manifest post splits on group boundaries.
      1900-char/50-mention caps get tests.
- [x] A8 **Webhook health**: per-category `{last_ok_at, last_error, fail_count}`
      in `notify.send` → `webhook_status()` → red "last delivery failed" note in
      ORG SETTINGS row. Dead webhooks stop being invisible.
- [x] A9 **`_notify_bg` task refs**: keep strong refs (GC can eat fire-and-forget
      tasks mid-flight).

## Package B — Marketplace UX + lifecycle (P1/P2)

- [x] B1 **My activity**: bids/quotes I've made + pending deals awaiting my
      confirm (server-side `bidder=me` filter + board tab). Retention surface.
- [x] B2 **Staleness + renew**: org-set `listing_stale_days` → amber stale badge +
      one-click renew (mirrors pirate-warning age-off, already-built pattern).
- [x] B3 **Server-side "can craft" filter** (current one filters only the loaded
      page and suppresses Load-more).
- [x] B4 **Admin moderation**: `DELETE /api/market/{id}` (admin) for scam/abuse
      listings — capability before it's needed.
- [x] B5 **Relist** from a closed listing (seed create form) + **bidder reputation**
      (completed-deals count) in the seller's offers list.
- [x] B6 **Liveness**: auction countdown ticks client-side + refetch on
      `visibilitychange`; `ends_at` lock (extend-only) once bids exist.
- [x] B7 **Org price memory**: per-unit last/median/count derived from completed
      deals' `final_auec` (already the ACCEPTED amount — what was really paid —
      so no confirm-time input was needed); surfaced on the listing detail
      ("⚖ Sold in-org before") + the create form's "Sold in-org" hint with a
      one-click use (`/api/market/item_history`).
- [x] B8 **Availability enum** on listings (instock · pickup · ondemand ·
      scheduled) + **pickup/handoff location** w/ POI autocomplete; board chip +
      filter (`avail=`), detail line, editable.
- [x] B9 **Quote-with-message** on commissions — already existed (`offer_note`
      on quotes); no work needed.

## Package C — Event Planner UX (P1/P2)

- [x] C1 **Capacity + waitlist**: `max_players` enforced — a full event
      waitlists new `going` joiners (no hard 409; kinder), first-come
      auto-promote on withdrawal + 🎟️ Discord ping.
- [x] C2 **Board filters + mine**: category/type chips, "my events" toggle,
      "✓ going" badge on cards (data already client-side).
- [x] C3 **Clone event** via existing `eventSeed` path (covers weekly ops).
- [x] C4 **Past events + completed**: board `range=past` tab; organizer "mark
      completed"; turns dead `completed` status into attendance history.
- [x] C5 **Organizer attendee management**: add/remove signups (organizer/admin)
      for day-of walk-up seating.
- [x] C6 **Manifest + links**: manifest header gains start time `<t:unix:F>`;
      created/reminder notifications deep-link `#/events/{id}` not the board.
- [x] C7 **Cancelled-but-future events** stay visible on the board w/ badge.
- [x] C8 **Group edit**: stop wiping unsent `notes` on edit (send full state).

## Package D — notification platform upgrades

- [x] D1 **Marketplace sweep loop** (clone of `event_reminder_loop`): auction
      ending-soon (1h), proactive settlement (supersedes lazy-only), commission
      needed-by approaching. (Stale-pending-deal nudge not included — the
      confirm-nudge ping already covers the common case.)
- [x] D2 **Discord embeds** for events + marketplace (18 builders; webhook-native,
      no bot; app-palette colors — cyan facts / green wins / amber time-pressure /
      red endings; deep-link as embed URL; `content` carries only the `<@id>`
      pings, since mentions inside embeds never ping; user names live in embed
      titles, which don't render markdown — retiring the `**`-injection
      cosmetics; `<t:>` timestamps kept out of titles, where they don't render;
      manifest post stays plain text on purpose — it's a chunked document).
      Goals/records/LFG/pirates/survey builders stay plain for now — same
      `_embed()` helper when wanted.
- [x] D3 **Send pacing**: per-category slot reservation (2.0s spacing = ≤30
      msg/min, Discord's webhook cap; atomic in the single-threaded loop, no
      queue thread needed) + 429 `retry_after` honored up to 30s (was capped 5s).
- [x] D4 **Member opt-out**: `notify_opt_out` profile flag honored in
      `_mentions()` (name stays in text, ping dropped).

## Shipped state

The A–D batch shipped as **v0.96.0**; D2 embeds as **v0.97.0** (both
confirmed live 2026-08-05). B7 + B8 + D3 followed the same day (919 tests) —
every box in this review is now closed. What remains are the parked competitor
ideas below.

## Deliberately NOT doing (validated by competitor research)

Real-money anything · escrow (neither competitor has it; confirm-handshake is the
right model) · Discord bot/threads (webhook-only is a project decision) ·
cross-org public market · image hosting · rent mode · star-ratings (org-internal
social cost; light trade-stats instead) · full reviews system.

## Competitor ideas parked for later

WTB/buy-order mode (M) · crafter storefronts / directed commissions (M-L) ·
availability weekly grid (M) · org trends panel (M) · WTB-matches-your-inventory
alert (M) · view counters (S).

## Source review details

Full agent reports (state machines, file:line anchors, competitor feature
inventories) live in the session transcript, 2026-08-04. Key anchors verified at
implementation time, not trusted blindly from this doc.
