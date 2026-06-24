# Event Planner — next-pass adjustments

**STATUS: all 7 items SHIPPED 2026-06-24.** Item 7 was already resolved earlier
(asset swap). Items 1–6 implemented this pass — see notes inline. Kept for the
record; nothing outstanding.

Queued tweaks from user feedback (2026-06-23, after v0.2.1). All live in
`server/static/index.html` unless noted. See `docs/event-planner.md` for the
design and the existing implementation map.

## Implementation notes (2026-06-24)

- **#1** intro moved to its own block: `renderEventBoard()` now emits the
  EVENTS-head + intro in one panel, then a separate `.panel` for the calendar.
- **#2 / #5** added `h2.ev-screen-h` (22px/700/gold) and applied it to the
  EVENTS, CREATE EVENT, and EDIT EVENT headers.
- **#3** `type` is now multi-select like category. Backend: `EventIn.type` →
  `types: list[str]`; db stores `type` as a JSON list (`_EVENT_JSON` + the
  renamed `_event_json_list` parser w/ legacy single-string back-compat);
  `_validate_event` validates+dedupes each against `TYPES`, requires ≥1;
  `_event_view` exposes `types` (dropped `type` from `_EVENT_PUBLIC`). Front end:
  `eventChips`/`eventFormHtml`/`collectEventForm`/`bindEventForm` use a chip row
  (`#ev-f-types`, `.ev-f-type`). Added **Event** category + `.ev-chip.cat-event`.
- **#4** added uniform 38px height to `.ev-form-grid .ti / select / .poi-pick .ti`.
- **#6** added **Race** category + `.ev-chip.cat-race`.
- Test: `test_taxonomy_payload_shape` extended to assert Event + Race.

1. **Move the board intro out of the calendar panel.** The `.ev-intro` blurb
   currently sits inside the same `.panel` as the EVENTS header + calendar, so it
   reads as part of the calendar. Put it *above* the calendar panel (its own block
   or its own panel) so it's visually distinct. See `renderEventBoard()`.

2. **Make the "EVENTS" calendar header bigger / more pronounced.** It uses the
   default `.panel h2` style (12px, dim, letter-spaced) — too subdued for a screen
   header. Bump size/weight/color (the create-form header fix in #5 can share a
   style). It's the `<h2>EVENTS</h2>` in `renderEventBoard()`'s `.ev-head`.

3. **Make `type` multi-select too (like category), and add an "Event" category.**
   - Type should allow 1+ values, same chip UI as category. This is a backend
     change mirroring what was done for category in v0.2.1: `EventIn.type` →
     `types: list[str]`; store `type` as a JSON list in db (reuse the
     `_EVENT_JSON` + `_event_categories`-style parse + legacy single-string
     back-compat); `_validate_event` validates each against `event_taxonomy.TYPES`,
     require ≥1; `_event_view` exposes `types`. Update `eventChips()`,
     `eventFormHtml()` (type chip row), `collectEventForm()`.
   - Add **"Event"** to `event_taxonomy.CATEGORIES` (special/seasonal events).
     Needs a `.ev-chip.cat-event` color in CSS (catClass → `cat-event`).
   - Net: both type and category are "choose 1 or more."

4. **Align the form inputs — they're different heights/sizes.** In the
   `.ev-form-grid`, the `.ti` inputs, `<select>`, and the POI-pick-wrapped inputs
   render at different heights (the `.poi-pick` span wrapper + native number/date/
   time controls differ from plain `.ti`). Normalize: give every control in the
   grid the same `height`/`box-sizing`/`padding`/`line-height`, and make the
   `.poi-pick` fields fill the field the same way the bare inputs do. Goal: a clean
   aligned grid. Fields affected: type, date, time, duration, rally point, event
   location, min players, max players.

5. **Make the "CREATE EVENT" header more pronounced.** It's a plain `<h2>` that
   gets lost above the form. Give it a dedicated form-title style (size/weight,
   maybe gold like the event titles). In `renderEventForm()` / `renderEditForm()`.

6. **Add "Race" to `event_taxonomy.CATEGORIES`** (alongside the "Event" add in #3).
   Needs a `.ev-chip.cat-race` color too.

7. **Event Planner logo too small in the app picker — RESOLVED 2026-06-23.** The
   cause was the asset: the old `event_planner_logo.png` had heavy transparent
   padding, so at the shared `.app-logo { width: 160px }` size its artwork looked
   smaller than the Resource Navigator / Cargo Planner badges. User replaced the
   logo (in both `images/` and `server/static/images/`) with a version whose badge
   fills the canvas like the other two — now consistent. (Note: new asset is
   317×320 vs ~700² for the others; fine at 160px display, slightly lower res if a
   larger render is ever wanted. Both copies are modified-but-uncommitted in the
   working tree.)

Taxonomy edits (#3, #6) are in `server/event_taxonomy.py`; remember the
`test_taxonomy_payload_shape` test and add chip colors in the events CSS block.
