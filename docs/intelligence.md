# Intelligence — the org's knowledge hub (inside Org Intel)

**Status:** 📐 design 2026-10-31, not built. Settled calls are marked
**(user)**. Everything else is a recommendation open to change.

**Settled 2026-10-31 (user):**

- **Images.** One click loads all of an entry's linked images (see *Images*).
  v1 has no uploads.
- **No player dossiers.** Dossiers cover places and orgs only.
- **No loadout builder.** Link to erkul.games and similar tools instead.
- **Channel choice.** The author picks any configured intel channel when
  publishing.
- **Review gate.** Admins can require review before publishing.
- **Discord on edits.** Major revisions always notify Discord, and the editor
  has a **Push notification** option for any other save.

## The problem

Everything the suite records today is either **live** (position, presence,
the danger board) or **numeric** (runs, trades, survey marks, ledgers). What
an org actually knows doesn't fit either. That's things like how we run a
Hathor escort, which outpost ambush works on a Pyro gate camp, what the
Vanduul-swarm loadout is this patch, what the last op at Orison taught us, and
who the gate campers at Stanton–Pyro are.

That knowledge lives in Discord today, and Discord is the wrong place for it:

- **It scrolls away.** A good write-up in `#tactics` is gone in a week, and
  Discord's search turns up the message without the thread around it.
- **No structure.** You can't ask Discord for "every SOP written before 4.3"
  or "everything we know about this outpost".
- **No upkeep.** Nothing marks a procedure as out of date when a patch
  changes the mechanic, or as superseded when someone writes a better one.
- **No link to the 'verse.** A note about a POI can't open the navigator at
  that POI. A debrief can't pull in the op's roster and ledger.

Intelligence is where the org writes down what it knows: procedures, tactics,
reports on places and actors, and the debriefs that turn an op into lessons
learned. It is **not chat**. Discord keeps the conversation. This app keeps
what's worth keeping after the conversation ends.

## Where it lives — a second half of Org Intel

There is already an **Org Intel** app at `#/intel`
([docs/apps/org-intel.md](apps/org-intel.md)): the read-only analytics deck
(mapping, hauling, trading, surveying, market, leaderboards, directory).
Instead of a twelfth app competing for the name and the route, **Org Intel
grows a second half** (proposed 2026-10-31 by the user, pending
confirmation):

- **INTELLIGENCE**: what the org knows (this doc).
- **ACTIVITY**: how the org is doing (today's eight sections, unchanged).

Why this beats a separate app (the other options were renaming Org Intel to
"Org Analytics", or two near-identical app names):

- **No rename and no clash.** "Org Intel" stays a fixed app name
  ([PRODUCT.md](../PRODUCT.md)) and becomes *more* accurate. Only the tile
  description changes.
- **No new launcher tile or artwork.**
- **Every existing link keeps working.** Activity sections already live at
  flat `#/intel/<slug>` routes (`mapping`, `hauling`, `trading`, `surveying`,
  `market`, `boards`, `directory`). Intelligence's slugs (`feed`, `library`,
  `dossiers`, `rfi`, `e/<id>`) don't collide with those, so there are no
  redirects. The older aliases (`#/stats` → `#/intel/mapping` and so on) work
  as they are.
- **The halves feed each other.** Activity's Surveying section can link a
  zone to its dossier, and its Overview can show "latest intel" (see below).

The cost is navigation depth. The fix is a two-level masthead, below.

## What it holds

One store of **entries**, in five kinds. Each kind maps to a familiar format
(wiki, blog, forum) because that's how people expect it to behave:

| kind | format it behaves like | life | example |
|---|---|---|---|
| **Procedure** (SOP) | wiki page | living, versioned, has an owner, falls stale on a patch | "Hull-C escort formation", "Org mining op checklist" |
| **Tactic** (TTP) | wiki page | same as a procedure, tagged by activity | "Breaking a Pyro gate interdiction" |
| **Report** | blog post / feed item | point-in-time, graded, then add-ons only | "Gate camp at Pyro Gateway, 3× Polaris, 21:40 UTC" |
| **Dossier** | wiki page about one thing | living, open-edit, gathers related items automatically | "Ruin Station", "Hostile org: XYZ", "Checkmate Station" |
| **Debrief** (AAR) | blog post tied to an op | written once after an op, then comments | "Op Iron Sky, 2026-10-24: what went right / wrong" |

Plus **Requests for Information (RFI)**. That's the forum part: a member asks a
question ("anyone mapped the caves at Daymar's Aberdeen?"), others answer, and
the asker marks an answer. An answered RFI can be promoted into a procedure or
dossier, so the answer doesn't stay buried in a thread.

**Comments** sit under every entry (flat, with one level of reply). They're for
discussion of *that* entry, not a general chat room.

### Why one store and not three products

A wiki, a blog and a forum installed side by side would mean three search
boxes, three permission models and three places to look. All five kinds plus
RFIs share one table, one search index, one permission check and one link
model. The kind only changes the default template, edit rights and lifecycle
(see below).

## How it feels — fitting the instrument

[DESIGN.md](../DESIGN.md) is a cockpit instrument: monospace, dark, made for
a glance. A knowledge base is the one app where people **read** at length.
That's a real tension, and the design takes both sides seriously:

- **Glance surfaces stay instruments.** The feed, library index, dossier
  headers and search results are dense lists of chips and readouts: kind,
  patch, freshness, grade, location. The eye finds what it wants without
  opening anything.
- **The reading view is the exception, on purpose.** Body text is set to a
  ~72-character measure at 1.6 line height, still in the one monospace family
  (the one-family rule holds). Headings follow the existing type scale.
  Monospace prose is fine at that measure. It's long lines that make it
  tiring, not the font.
- **The intel register, earned not costumed.** Report grading, the
  `RESTRICTED` marking and the "written for 4.3 · current 4.4" staleness
  readout are real fields doing real work. There are no stamped "TOP SECRET"
  graphics, typewriter fonts or redaction-bar gimmicks. The DESIGN.md
  anti-references are a toy UI and a cluttered fan-wiki, and a costume intel
  UI is the toy version of this app.
- **One new category hue at most.** The Category-Color Rule says each hue is a
  category. Intelligence needs one hue for "an intel entry" on surfaces outside
  the app (the navigator POI chip, a danger-board warning's "written up" chip).
  Inside the app, kind is shown with a glyph + label, not a colour per kind.

### Masthead tabs

Org Intel today is one tab strip of 8 sections. Adding Intelligence's 4 tabs,
a system scope and search to that strip doesn't fit, so the masthead gets
**two levels**. On top is a half switch (the existing `.seg` vocabulary).
Under it is the active half's own row. Intelligence's row is modelled on
Prospector's masthead as it stands on dev (checked 2026-10-31): system scope
on the left, tabs on the right, search on the row below. Reports, dossiers
and most tactics belong to a system, so the scope earns its place.

```
ORG INTEL   [◆ INTELLIGENCE | ▥ ACTIVITY]
──────────────────────────────────────────────────────────────────────────────
[ALL|STANTON|NYX|PYRO]                [▤ FEED] [▦ LIBRARY] [◆ DOSSIERS] [? RFI]
⌕ search the org's knowledge…                    kind ▾   patch ▾    [+ New ▾]
```

```
ORG INTEL   [◆ INTELLIGENCE | ▥ ACTIVITY]
──────────────────────────────────────────────────────────────────────────────
Overview · Mapping · Hauling · Trading · Surveying · Market · Leaderboards · Directory
```

- **The switch remembers the half you last used** (a `localStorage`
  convenience, wrapped like `scTheme`). The first visit lands on
  **INTELLIGENCE → FEED**, because the feed changes daily while the stats are
  a periodic look. Open question 1 covers the alternative.
- **Routes.** Bare `#/intel` resolves to the remembered half's landing tab.
  Activity's Overview, which owns bare `#/intel` today, also gets an explicit
  `#/intel/overview`, so it can be linked directly. Every other existing
  `#/intel/<slug>` is untouched.
- **← / → keys** keep working, scoped to the active half's row. The existing
  handler walks every link in `#intel-nav`, so it needs to be narrowed to the
  visible row.
- **Activity Overview gets a "Latest intel" strip**: the three newest
  published entries the viewer can read. That's the doorway for people who
  open Org Intel for the numbers.
- **Launcher:** the Org Intel tile stays in *Run the Org* with the same art.
  Its description becomes something like *"What the org knows and how it's
  doing: procedures, reports, dossiers, and the activity deck."* The tile's
  name is unchanged.

- **FEED** (landing, `#/intel`): reverse-chronological reports, debriefs and
  newly published or updated procedures. This is the "blog". Filters: kind,
  system, activity, `since my last visit`.
- **LIBRARY** (`#/intel/library`): procedures + tactics grouped by activity
  (the #30 playstyle taxonomy, so mining, hauling, bounty, escort, … match the
  profile chips members already set). Each row: title, owner, patch stamp,
  freshness, "required reading" marker.
- **DOSSIERS** (`#/intel/dossiers`): places and actors. Places use the nav
  catalog (system → body → POI, plus survey zones). Actors are hostile or
  allied **orgs**, never individual players (user).
- **RFI** (`#/intel/rfi`): open questions first, then answered ones.

An entry lives at `#/intel/e/<id>-<slug>`. That's a stable, shareable link to
paste into Discord.

## Linking to the 'verse — the reason to build it in

The feature an off-the-shelf wiki can't have: entries **link to the suite's
own records**, and those records link back.

### Reference chips in the body

The editor accepts a small reference syntax, which the renderer turns into
live chips:

```
Approach [[poi:1842]] from the dark side; the [[zone:17]] lane is quieter.
Expect pirates near [[warning:311]]. See [[entry:52]] for the loadout.
Debriefed in [[op:88]], briefed at [[event:204]].
```

| ref | chip shows | click |
|---|---|---|
| `poi:` | name, system, type | **Set destination** in the navigator |
| `zone:` | survey zone name + ore summary | opens Prospector ATLAS on it |
| `warning:` | danger-board severity + age (greyed if expired) | opens the board |
| `event:` / `op:` | title, date, status | opens the event / op page |
| `entry:` | title + kind | opens the entry |
| `member:` | display name (never pings, it's not Discord) | profile |

An inserter (the existing `sightingPicker`-style combobox, "⌖ Insert
reference") searches POIs, zones, events and entries, so nobody types ids by
hand. A missing or deleted target renders as a struck-out chip with its last
known name, never as an error.

### Backlinks

Every reference is indexed (`intel_links`). That gives two things for free:

1. **Dossiers fill themselves.** A place dossier shows the hand-written
   summary on top, then automatically: reports that reference it (newest
   first), live danger-board warnings within its hazard radius, org survey
   records in the zone, and ops/events held there. That's IPB's "describe the
   battlespace" step, done by the data the org already captures.
2. **The rest of the suite points in.** The navigator's POI detail gets an
   "◆ 3 intel entries" chip. A danger-board warning gets **Write up →**, which
   starts a report pre-filled with location, threat and time. An op's record
   gets **Write debrief →**, which starts a debrief pre-filled with the op's
   roster, duration and ledger totals as a read-only header.

## Kinds in detail

### Procedures and tactics

- **Owner.** Each has an owner (default: the author). Owner, named maintainers
  and admins edit. Other members **comment** or **suggest an edit**. A
  suggestion is a proposed revision the owner accepts or rejects. Doctrine
  needs an accountable owner. That's different from dossiers (below).
- **Patch stamp.** Each revision records the game build it was written
  against. The build already comes from the watcher's Game.log stamps
  (`patch_of`, `PatchTracker`), so this costs nothing. When the org's current
  patch moves on, the library shows `written 4.3 · now 4.4` in warn amber
  until the owner re-confirms ("still valid on 4.4") or edits. This is the
  single most useful upkeep signal for a Star Citizen knowledge base:
  mechanics change every patch.
- **Review-by date** (optional, default off). Fades the entry to "due for
  review" after N days.
- **Supersede.** "Superseded by [[entry:…]]" archives the old one and redirects
  readers to the new one.
- **Required reading** (later slice). An event can list procedures as required
  reading. Signups show who has read the current revision (`intel_reads`
  stores the revision each member last read, so an edit resets it). This is
  "read and sign" from real-world SOP practice. No achievements, no badges,
  just a ✓ on the roster.

### Reports

Point-in-time observations. They're what makes the feed the intel feed.

- **Structured header**, prompted by a SALUTE-style template: **S**ize,
  **A**ctivity, **L**ocation (a `poi:` or zone ref, plus free text),
  **U**nit (the org or ships seen), **T**ime (defaults to now, editable),
  **E**quipment. All optional except location and time. The body is free text
  below.
- **Grading** (optional) on the two-axis Admiralty / NATO scale: source
  reliability A–F × information credibility 1–6, shown as a compact `B2` chip
  with a tooltip. The two are judged separately. A trusted member can still
  pass on an unconfirmed rumour ("A4"). Leaving it ungraded is normal and is
  shown as `—`, not as a low grade.
- **Corroborate.** Other members can **Corroborate** (same idea as the danger
  board's confirm) or **Dispute** with a note. The chip shows the counts.
- **No silent rewrites.** After publish, the body is locked to the author and
  can only be added to: **Add update** appends a timestamped addendum. A report
  is a record of what someone saw at a time. Typos can be fixed for 15
  minutes, after which edits become addenda.
- **Valid until** (optional). A gate camp report is stale in hours, a base
  layout in a patch. Expired reports drop off the live feed and stay in the
  dossier history.

How this differs from the danger board: the board is the **live warning**
(a 280-character note, location-anchored, expires, feeds routing detours). A report is the
**write-up** (longer, graded, permanent, searchable). **Write up →** on a
warning links the two. The board stays the fast path. Nothing here slows it
down.

### Dossiers

A dossier is **about one thing**: a place (POI, station, survey zone, body,
system) or an **org** (another RSI org, hostile, neutral or allied).

**No player dossiers (user).** A page about another player is a page about a
real person. Even with conduct rules and flagging, it's the feature most
likely to cause friction, inside the org and with other orgs. So the subject
kinds are `place` and `org` only, and there is no "player" option to switch
on. Reports can still describe what was seen ("3 Polaris flying XYZ tags").
The editor's guidance line for reports and org dossiers says *describe ships,
orgs and behaviour, not individual players*, and the flag reason
"names a player" goes straight to the admin queue.

- **Open edit**: any member can edit the summary, wiki-style, with full
  revision history and one-click revert. Dossiers are shared knowledge, not
  doctrine. Who changed what is always visible.
- **One dossier per subject.** Creating a second one for the same POI or
  org opens the existing one instead.
- **Org dossiers** carry a stance (hostile · neutral · allied · unknown,
  each with a glyph as well as a colour, per the colour-blind rule), aliases,
  and the RSI org SID. The SID is just text, never matched against anything
  of ours.
- Place dossiers can be **started from the navigator** ("◆ Start dossier" on a
  POI with none).

### Debriefs (AAR)

Written after an event or op. **Write debrief →** on the op page pre-fills a
read-only header from the op record (date, roster, duration, contracts,
ledger totals), so nobody retypes it, and starts the body from the standard
AAR prompts: *what was planned · what happened · what went well · what to
change*. The op's page then links to its debrief(s). The **pre-op briefing**
is the event's description plus its required reading. It doesn't need a
separate kind.

### RFIs

A question with answers. The asker (or an admin) marks one answer **accepted**.
An open RFI can carry a location ref, so "anything known about this outpost?"
shows up on that outpost's dossier as an open question. **Promote →** turns an
accepted answer into a new procedure or into an addition to a dossier, crediting
both people.

## Access — who sees what

Today the app has two roles (member, admin) plus per-object organizer/deputy
rights. Intelligence adds **one** concept: a **visibility marking** per entry.

| marking | who can read | Discord post |
|---|---|---|
| **ORG** (default) | every member | title + kind + link |
| **RESTRICTED** | admins + members holding a configured Discord role | "a restricted entry was published", no title |

- **Clearance comes from Discord roles**, the same way the member gate does.
  Login already requests `guilds.members.read` and reads the member's role ids
  (`auth.py`). Settings gets a "Restricted clearance roles" list (role ids,
  exactly like `member_role_id` and the event ping alias map). Roles are
  snapshotted at login, so a role change applies on the next login (≤ 8 h).
  That's the same lag as the member gate today, and **Revoke access** still
  ejects someone immediately.
- **Enforced on the server, everywhere**: entry GET, list, search results,
  backlink chips (a restricted entry referenced from an ORG one renders as
  `◆ restricted entry`), dossier rollups, the FTS query, and Discord posts.
  The search index filters by marking in SQL, never after the fact in the
  client.
- **No public marking in v1.** `auth_gate` stays deny-by-default and nothing
  here is anonymous. A public recruitment handbook is a reasonable later ask,
  but it's a separate decision with its own review (see Open questions).
- More than two levels (per-squad compartments) is deliberately left out of
  v1. Two levels cover "the org" vs "the leadership". Compartments can be
  added on the same column later if an org really needs them.

## Technical design

### Storage (SQLite, `db.py`)

```
intel_entries   id, kind, title, slug, summary, body_md, body_html,
                status        -- draft | published | archived
                visibility    -- org | restricted
                author_id, owner_id, maintainers (json),
                patch, patch_confirmed,     -- build written for / re-confirmed on
                activity,                   -- #30 playstyle taxonomy
                review_due, valid_until,
                grade_rel, grade_cred,      -- A–F, 1–6 (reports)
                header (json),              -- SALUTE fields / op snapshot / stance
                subject_kind, subject_ref,  -- dossier subject: place | org (unique per kind+ref)
                superseded_by, accepted_comment_id (rfi),
                created_at, updated_at, published_at
intel_revisions id, entry_id, rev, title, body_md, editor_id, note, created_at,
                status        -- live | suggested | rejected
intel_comments  id, entry_id, parent_id, author_id, body_md, body_html,
                kind          -- comment | addendum | corroborate | dispute | answer
                created_at, edited_at, deleted_at
intel_links     entry_id, ref_kind, ref_id          -- backlink index, rebuilt per save
intel_reads     member_id, entry_id, rev, read_at   -- required reading + unread markers
intel_tags      entry_id, tag                       -- free tags (activity is a column)
intel_collections       id, title, summary, owner_id, visibility
intel_collection_items  collection_id, entry_id, position
intel_watches   member_id, target_kind, target_id   -- entry | tag | collection
intel_flags     id, target_kind, target_id, member_id, reason, note, resolved_by, created_at
intel_discord_posts     entry_id, rev, channel_key, message_id, thread_id, posted_at
intel_fts       FTS5(title, summary, body, tags) content=intel_entries
-- later, only if uploads are ever enabled (see Images):
intel_files     id, entry_id, sha256, mime, bytes, width, height, uploader_id, created_at
```

- Authors are Discord member ids, like events and ops. A revoked member's
  entries **stay** (they're org knowledge) with the name kept as it was.
- `_merge_handle_rows` is unaffected: nothing here is keyed on PlayerID
  `owner_id`, and there are no player subjects.

### Markdown: rendered on the server, raw HTML off

The SPA is one file with a single `<script>` and no build step, so vendoring
a JS markdown library and sanitizer into it is awkward. Rendering on the
server fits better:

- Render with a Python CommonMark parser **with raw HTML disabled**, then pass
  the output through an allow-list HTML sanitizer anyway (belt and braces).
  Store `body_html` next to `body_md`, rendered on save.
- The `[[ref:id]]` syntax is a parser plugin that emits a placeholder element.
  The SPA hydrates placeholders into chips **at view time**, so names and
  states stay live and visibility is checked per viewer.
- Preview: `POST /api/intel/render` (session + the `solve`-style rate limit)
  returns the same HTML, so preview and publish can't differ.
- Links: `http(s)` only, `rel="noopener noreferrer nofollow"`, and external
  links show their host. Images: only from `IMAGE_PREVIEW_HOSTS`, rendered
  as click-to-load placeholders, never a bare `<img>`. Any other image URL
  renders as a link. See [Images](#images--linked-not-uploaded).

Library choices are in [Tool options](#tool-options--build-adopt-or-embed).

### Editor

A plain `textarea.ti` with a small toolbar (heading, bold, list, checklist,
code, **⌖ reference**, **▣ image link**, which asks for a URL and checks
the host as you type) and a **Write / Preview** toggle. No
WYSIWYG: every rich editor worth having needs a bundler, and the people
writing SOPs here are fine with markdown. Kind templates pre-fill the body
(SOP skeleton, SALUTE, AAR prompts). Drafts autosave to the server
(`status=draft`), not only to `localStorage`, so a phone-started draft can be
finished at the desk.

### Formatting: Markdown, not rich text (user, 2026-10-31)

Entries, comments, addenda and RFI answers are **Markdown**: CommonMark plus
the extensions below, rendered on the server (see *Markdown* above). The
renderer has an **allow-list**. Anything not on it renders as plain text or
a plain link, never as HTML.

| allowed | syntax | notes |
|---|---|---|
| Headings, up to level 3 | `## Loadout` | Each gets a stable `§` anchor (member feature 6). `#` (level 1) is reserved for the entry title and is demoted to `##` |
| Bold · italic · strikethrough | `**b**` · `*i*` / `_i_` · `~~s~~` | |
| Underline | `__u__` | **Discord semantics, not CommonMark**, where `__x__` is bold. Members paste from Discord, and `__text__` must look the same here as it did there |
| Bulleted and numbered lists | `- item` · `1. step` | Nested lists allowed |
| Task lists | `- [ ] item` · `- [x] item` | Each item gets a stable id (member feature 5). In the reading view the boxes are display-only, so ticking one doesn't edit the entry |
| Quotes | `> text` | |
| Inline code + code blocks | `` `x` `` · fenced blocks | No syntax highlighting. Fixed-pitch is already the house face |
| Tables | GFM pipe tables | Loadouts, crew roles, price lists. Wide tables scroll inside their panel like every other table |
| Horizontal rule | `---` | |
| Links | `[text](https://…)` · bare URLs | `http(s)` only. The site's name is shown, and links carry `rel="noopener noreferrer nofollow"`. Known loadout tools render as a loadout link card |
| Images | `![caption](https://host/…)` | `IMAGE_PREVIEW_HOSTS` only, as one-click-per-entry placeholders (see *Images*). Any other host renders as a link |
| Suite references | `[[poi:1842]]` · `[[op:88]]` … | Live chips (see *Linking to the 'verse*). Inserted from the **⌖** picker, not typed by hand |
| Discord timestamps | `<t:1730410800:f>` | Shown in the reader's own time zone. The SPA already does this for event text (`discordTime`), with the non-finite guard |

**Not allowed, deliberately:**

- **Raw HTML** (`html: false` in the parser, and the output still goes through
  the sanitizer).
- **Colour, font or size controls.** Entries take the app's look, so no SOP
  can make itself look like a different site.
- **Embeds and iframes** (video, maps, other sites). A video link renders as a
  plain link.
- **Footnotes and maths.** Not needed for SOPs and reports, and each is one
  more parser surface.
- **Mentions that ping.** `@name` is plain text in the app. Pings only happen
  through the Discord publish panel's role pings.

**Why not rich text (WYSIWYG):**

- **No build step.** Every rich editor worth having (TipTap, Milkdown) is
  ESM-only and needs a bundler. Toast UI is unmaintained, and EasyMDE pulls
  fonts from a CDN.
- **Storage.** Rich editors store HTML or their own JSON. Markdown is plain
  text, so revision diffs are readable, export/import is a folder of `.md`
  files, and the sanitizer has a small, known surface.
- **Members already know it.** Discord's formatting is a Markdown dialect.

**Closing the gap rich text would cover:**

- **Toolbar.** Heading · bold · italic · list · checklist · quote · code ·
  table (inserts a 3×3 skeleton) · link · **▣ image link** · **⌖ reference**.
  Keyboard shortcuts: Ctrl/⌘-B, -I, -K (link). Buttons wrap the selection,
  so nobody needs to remember the syntax.
- **Write / Preview** uses the same server render as publish, so what you
  preview is exactly what gets published.
- **Paste conversion.** Pasting from Google Docs, Notion, Word or a web page
  puts `text/html` on the clipboard. A plain textarea would keep only the
  words. The editor's paste handler converts that HTML to Markdown instead:
  - It handles headings, bold/italic/underline/strike, lists (nested), task
    lists, links, tables, quotes, code and rules.
  - It drops everything else (styles, colours, fonts, classes, scripts, and
    images from hosts not on the list) and keeps the text.
  - It's our own ~100-line DOM walk over a `DOMParser` document that's never
    attached to the page, so nothing in the pasted HTML runs or loads. No
    library is needed.
  - **Shift-paste** pastes plain text.
  - The server sanitizer is still the trust boundary. The paste converter is
    only for convenience.

  Together with **Import Markdown (.zip)** (member feature 7), this is how an
  org moves an existing Google Docs or Notion handbook in on day one.

### Search

SQLite **FTS5** over title + summary + body, with `bm25` ranking and
`snippet()` highlights, and filters for kind, system, activity, patch and
author. `intel_fts` is an external-content table kept in step by triggers.
FTS5 is present in the dev venv (SQLite 3.53.4). The production image
(`python:3.12-slim`) still needs checking: slice 0 adds a startup probe that
falls back to `LIKE` search and logs a warning, so a missing FTS5 build
degrades instead of failing to boot.

### Images — linked, not uploaded

In-game screenshots are the most valuable thing to embed (a base layout, a
mining scan, a gate camp). They're also the one thing that could make a
self-hosting org's disk grow without bound. This has been decided once
already: member uploads for event banners were **built and then removed** in
the 2026-10-06 security sweep. Each member had a cap, but nothing capped the
org as a whole ([discord-notification-customization.md](discord-notification-customization.md),
header). Intelligence follows that decision.

**v1: images are links to known image hosts.** The syntax is the normal
Markdown `![caption](https://i.imgur.com/…)`, accepted only from
`IMAGE_PREVIEW_HOSTS`: imgur, ibb, postimg, X/Twitter media, and RSI. That's
the same fixed list and the same reasoning as setting **S10**: known hosts
only, never "any https host", which closes the tracking-server case. Discord
CDN links are refused, as they are for banners, because Discord's attachment
URLs expire. Anything else renders as a plain link with its host shown. The
server stores nothing.

**What changes from the banner rule.** For a banner, one image is shown on
one explicit click. An article might hold ten screenshots, and clicking each
one is unusable. Proposal:

- The render pipeline turns each allowed image into a **placeholder** (host,
  caption, and a fixed-aspect frame so nothing jumps) instead of an `<img>`.
- The reading view shows one bar at the top: **"Load 6 images from imgur.com,
  i.ibb.co"**. One click loads them all for that entry.
- **"Always load images from these hosts"** is a per-viewer option stored in
  `localStorage`, for members who've decided they don't mind.
- Every `<img>` gets `referrerpolicy="no-referrer"` and `loading="lazy"`.
- All of it applies only while the admin's existing
  `notify_external_preview` toggle is on. That toggle is what puts these hosts
  in the CSP `img-src`. With it off, images render as links and Discord still
  shows them (see below).

**Approved (user).** This deliberately narrows a "Don't regress" rule in
CLAUDE.md (*"A linked org image is NOT auto-previewed… load only on an
explicit click"*). It's still an explicit click, just one per entry instead of
one per image. The build PR must reword that CLAUDE.md line to match, and add
a test that a rendered entry contains no bare `<img>`, so the rule earns its
way off that list.

**Later, if link-only proves too clunky: uploads with a hard org quota.**
This is the version the banner sweep lacked:

- a **total** org quota (admin-set, default off, e.g. 250 MB);
- re-encoded to WebP at ≤ 1600 px, which strips EXIF and turns a 4 MB PNG
  into roughly 200 KB;
- a per-member share of the quota;
- files deleted with their last referencing entry;
- served from `/api/intel/files/<id>` with a visibility check, **never** under
  the public `/images/` prefix.

Because uploads are bounded by the org's own quota and the server does the
re-encoding, it isn't an open-ended disk risk. It's still more code than v1
needs, so it waits until members ask.

### Discord — publishing to channels

The author decides whether an entry is announced, not the system. This is the
same pattern as the danger board's **📣 Announce to the org's Discord**
checkbox next to Post warning.

**Routing: a category with per-kind channels.** One new notify category,
`intel` (added to `CATEGORIES` in `notify.py`). Its webhook is the default
channel. Settings → Discord (channels) adds optional **per-kind overrides**:
reports → `#intel-reports`, procedures + tactics → `#sop`, debriefs →
`#after-action`, RFIs → `#questions`. A kind with no override uses the
default. This keeps the existing rule that a category is on exactly when it
has a valid webhook (`_webhook_key("intel")`, `_webhook_key("intel_report")`,
…), so validation, masking and the webhook health check apply unchanged.

**The publish panel** sits next to Publish, and under every save in the
editor (see *When Discord is notified*):

```
📣 Announce in Discord   → [#intel-reports ▾]  (defaults to the kind's channel; any configured intel channel)
   ping: [ none ▾ ]       ← the event role-ping alias map: @Security, @Miners…
   ✎ teaser (optional, 280)  "Gate camp moved to the Pyro side, 3 Polaris"
```

- **What's posted**: an embed built from the entry. It includes the title as
  a link to `#/intel/e/<id>`, the kind glyph + label, the system, the
  author's display name, a teaser (written, or the summary, or the first 280
  characters), and for reports the SALUTE header + grade as embed fields.
  **The full body is never posted.** The app is the record, so the post is a
  pointer to it.
- **Lead image.** The first linked image goes in the embed's `image` slot.
  Discord fetches it from imgur and so on itself, so **the image shows in
  Discord even when the in-app preview toggle is off**. It costs us no disk
  either way.
- **Role pings** reuse the event-role-pings alias map (admin maps app roles →
  Discord role ids). It's the only source of `allowed_mentions.roles`, as in
  CLAUDE.md, and the ping rides in `content`, never in the embed. A gate camp
  report pinging `@Security` is the obvious case. Nobody gets pinged by
  default.
- **Templates.** Each kind gets a slot in the notify template registry
  (`intel_report`, `intel_procedure`, …), so orgs can reword posts like the
  other templates, with the golden file regenerated deliberately.
- **Forum channels** (optional per webhook). If the target is a Discord forum
  channel, the post opens a thread named after the entry (`thread_name`),
  tagged by kind (`applied_tags`). The returned thread id is stored on the
  entry, so a major revision posts **into the same thread** ("Revised for
  4.4: what changed") instead of starting a new one. That thread can be where
  discussion of the entry happens (open question 7).
- **RESTRICTED entries** announce only "📕 A restricted entry was published"
  plus the link, with no title, teaser or image, to whichever channel the
  admin set for restricted posts. With no such channel set, nothing is posted.
- **Channel choice (user):** the author picks any configured `intel`
  channel. The kind's channel is only the default.

**When Discord is notified (user):**

| save | Discord |
|---|---|
| first **publish** | author's choice (the 📣 box) |
| **major** revision | **always** posts "Revised: what changed" (to the entry's forum thread if it has one, otherwise to its last channel, otherwise the kind's channel) |
| minor revision | nothing, unless the author ticks **📣 Push notification** in the editor |
| review approval (if the review gate is on) | the publish post goes out at approval, using the author's 📣 choices |
| RFI unanswered 24 h | one automatic nudge, never repeated |

The **Push notification** option sits in the editor's save bar on every
save. It's for an edit the author judges worth hearing about even though it
isn't a rewrite ("added the new Pyro gate coordinates"). It takes the same
channel, ping and teaser fields as the publish panel.
- **Recorded on the entry:** `discord_posts` (rev, channel key, message or
  thread id, time) shows "announced in #intel-reports · 2 h ago". It also
  stops the same revision being announced twice.

### Export

**Export → Markdown (.zip)**: every ORG entry as a `.md` file with front-matter
(the same fields Import reads, member feature 7). Linked images stay links.
Uploaded files would be added only if uploads are ever enabled. Restricted entries only when an admin exports. An org's
knowledge base is the thing it would least want locked into one tool. This is
also the migration path if an org later moves to a dedicated wiki.

## Member features — decide now, build when needed

Each of these is cheap to plan for now and expensive to add later, because
it shapes the data model, the permission check or the Markdown dialect.
**Now** means its column or rule is in slice 0–1, even if the UI comes later.

### Changes the data model (decide before slice 0)

1. **Major vs minor revisions.** Every save asks "Minor edit (typo,
   formatting)?" and defaults to minor for the owner's own small edits. Only
   a **major** revision:
   - resets required-reading acknowledgements;
   - always notifies Discord (user);
   - shows as "updated" in the feed;
   - clears the patch-staleness warning.

   Without the flag, fixing a typo would ping the whole org and make 40
   members re-acknowledge an SOP. A minor save can still notify Discord when
   the author ticks **Push notification** (user). **Now:**
   `intel_revisions.major`.
2. **Tags: a fixed activity list plus free tags.** Activity uses the #30
   playstyle taxonomy, so the Library groups match profile chips. Free tags
   (`#hathor`, `#pyro-gate`, `#polaris`) cover the rest, lower-cased and
   de-duplicated, with an admin merge for near-duplicates. **Now:** an
   `intel_tags(entry_id, tag)` table. A JSON column can't be indexed for "all
   entries tagged hathor".
3. **Collections: ordered reading lists.** "New member handbook", "Mining op
   crew brief", "Pyro survival". A collection is an ordered list of entries
   with its own title and summary. It's how a pile of entries becomes a
   handbook, and an event's required reading can be a whole collection.
   **Now:** `intel_collections` + `intel_collection_items(position)`.
4. **Watch.** A member can watch an entry, a dossier, a tag or a collection.
   There's no in-app notification centre today and Discord is webhook-only
   (no DMs), so watching means:
   - **unread markers** in the feed;
   - a **"Watching · 3 updated"** filter.

   That's enough for "tell me when the Ruin Station dossier changes". It
   shares `intel_reads` with required reading. **Now:** `intel_watches`.
5. **Checklists that can be run.** Procedures are mostly checklists
   ("pre-undock", "salvage op roles"). The Markdown dialect supports task
   lists (`- [ ]`), and each item gets a **stable id** in the rendered HTML.
   Later, a procedure can be **attached to a live op**, and the crew ticks it
   off together on the op page. The tick state belongs to the op, never the
   entry. **Now:** only the stable ids, so revisions don't scramble later
   check state. The op integration is a later slice.
6. **Heading anchors.** Every heading gets a permalink
   (`#/intel/e/52-hathor-escort/§loadout`), so "see §Loadout of the escort
   SOP" can be pasted into Discord. **Now:** the slug rule in the renderer,
   which must stay stable across edits.
7. **An import format that matches export.** Front-matter fields (kind, tags,
   system, patch, visibility, grade, refs) are fixed now, so
   **Import Markdown (.zip)** can round-trip an export. It's also how an org
   moves its existing Google Docs or Notion handbook in on day one. Notion
   and Docs both export Markdown. That onboarding matters more than any
   feature here.

### Changes the permission or moderation model

8. **Flag + hide.** Any member can flag an entry or comment ("out of date",
   "inaccurate", "inappropriate") with a note. Admins see a flag queue and can
   **hide** an entry, which keeps it but makes it admin-visible only,
   reversible and logged. Open edit on dossiers and a forum both need this
   from day one.
9. **No player dossiers (user).** Removed, because of the friction it
   could cause (see *Dossiers*). The data model's `subject_kind` is `place`
   or `org` only, and flag reasons include "names a player".
10. **Co-authors and credit.** An entry lists everyone with a major revision
    as a contributor, newest first. Activity's Leaderboards could count
    "intel contributions". That's useful for orgs that reward documentation,
    but it's off by default, given the "no achievement spam" anti-reference.
11. **Review before publish: optional, admin-enforceable (user).**
    - **Off (default):** any member can **Request review → member** on a
      draft. The reviewer comments and the author publishes when ready.
    - **Required:** an admin turns on **Require review before publishing** in
      Settings. Publish then becomes **Submit for review**. The draft goes into
      a **Review queue** visible to reviewers, and only a reviewer's
      **Approve** publishes it. **Return with comments** sends it back.
    - **Reviewers** are admins plus members holding a configured Discord role,
      using the same role-id list mechanism as RESTRICTED clearance.
    - **Bypass.** Admins and reviewers publish their own entries directly.
      A gate that blocks the people who'd approve it only adds a wait.
    - **Per kind (recommended).** The setting is a checkbox per kind. The
      suggested set is procedures, tactics and debriefs; **reports are
      exempt by default**, because a gate-camp report that waits two hours
      for approval is worthless.
    - **Major revisions of existing entries** go through review too when the
      kind requires it. Minor edits don't. While the revision is in review,
      the live entry keeps showing the last approved revision.

    **Now:** `status` gains `in_review`, revisions gain `approved_by` and
    `approved_at`, and comments gain `kind='review'`.

### Changes how it fits next to the game

12. **Phone reading mode.** The second-screen device is often a phone. The
    reading view needs a phone layout from v1: a single column, chips that
    wrap, and a sticky table of contents collapsed to a "§" button. This isn't
    a later polish pass.
13. **"Pin to watcher HUD".** Later: pin a procedure checklist to the in-game
    overlay (#40/#40.2). **Now:** nothing more than the stable checklist ids
    from 5.
14. **Location capture in reports.** **Use my position** fills the report's
    location from the member's last `/showlocation` fix (container + lat/lon,
    the same resolver as captures). It can optionally drop a custom POI so
    the report has something to link to. Exact coordinates of a find can be
    sensitive, so this is per-report and the default is
    **nearest POI only**, not raw coordinates.

### Deliberately not doing

- **Reactions or upvotes.** Corroborate/Dispute on reports and "✓ still
  accurate on 4.4" on procedures carry real information. A ❤️ count doesn't,
  and it tips toward the gamified anti-reference.
- **Video embeds.** YouTube means an `iframe` and widening `frame-src`. In
  v1 a video link renders as a plain link card (title + host, no thumbnail).
- **Real-time co-editing.** Two people editing one SOP get a "changed since
  you opened it, review the diff" conflict on save, not live cursors.
- **A loadout builder (user).** Ship loadouts matter to tactics and SOPs, but
  erkul.games (and SPViewer) already do them well. Matching them would take a
  component database and stat maths far beyond this app's scope. Instead,
  links to known loadout tools render as a **loadout link card**:
  - a ⚙ glyph, "erkul.games loadout", and the link text as its label;
  - nothing is fetched, scraped or embedded (cargo planner research already
    ruled out reusing erkul's data on licence grounds);
  - the procedure template has a `## Loadout` section that prompts for a
    link.

## Tool options — build, adopt, or embed

Researched 2026-10-31. Versions and licenses were checked against GitHub,
PyPI and npm that day. RAM figures are approximate. **[unverified]** marks
claims we couldn't confirm from a primary source.

### Off-the-shelf products (run alongside, or embed)

| product | license | needs | Discord login | verdict |
|---|---|---|---|---|
| **Discourse** | GPL-2.0 | Ruby + Postgres + Redis + Sidekiq; own Docker launcher; 2 GB RAM recommended | **in core**, with a trusted-guilds restriction; or Discourse Connect with *our* app as the identity provider | The best real forum, and the heaviest second stack |
| **Wiki.js** 2.5 / 3.0 beta | AGPL-3.0 | Node + Postgres (3.x is Postgres-only) | built-in module | A good wiki, but its 3.0 migration is still in flux |
| **Flarum** 2.0.2 | MIT | PHP 8 + MySQL | `fof/oauth` | Light and MIT-licensed, but a PHP stack |
| **NodeBB** 4.16 | GPL-3.0 | Node + Mongo/Redis/Postgres | community plugin [unverified] | Capable forum, another stack |
| **BookStack** 26.09 | MIT | PHP + MySQL 8 | OIDC only; Discord needs a shim | Clean book/chapter/page model |
| **Outline** 1.12 | BSL 1.1 (not open source) | Node + Postgres + Redis + S3 | needs an OIDC bridge | Ruled out by license + dependencies |
| **Docmost** 0.97 | AGPL + proprietary EE | Node + Postgres + Redis | **SSO is Enterprise-only** | Ruled out |
| **Ghost** 6.73 | MIT | Node + MySQL 8 | none native | A blog, not a knowledge base |
| WriteFreely / Memos / Trilium / DokuWiki | AGPL / MIT / AGPL / GPL | light | generic OAuth at best | Too thin, single-user, or dated |

**Why none of them is the recommendation:**

- **Every one breaks the one-container promise.** Each adds a second runtime,
  and usually a second database. Fenrir (~180 members) and the other
  self-hosting orgs would have to run, back up and patch it.
- **None of them shares our identity.** Each has its own session, its own
  idea of admin, and no `access_revoked()`. Off-boarding someone would then
  mean two systems, and off-boarding is security-relevant (see
  [security-review-2026-08.md](security-review-2026-08.md)).
- **None can see the org's data.** POIs, zones, ops, the danger board and
  patch stamps are invisible to them. Reference chips and backlinks are the
  main reason to have this app at all. Without them it's a generic wiki that
  happens to sit next to the suite.
- **Embedding needs the security headers widened.** An iframe plus a second
  origin would loosen CSP and `frame-ancestors` for one app.
- **Licenses.** Running GPL/AGPL software *alongside* ours is fine. Vendoring
  its code into this repo is not.

Little evidence turned up that Star Citizen orgs use any of these. The common
pattern seems to be RSI org page + Discord + Google Docs/Notion
**[anecdotal]**. The community wiki (starcitizen.tools, MediaWiki) hosts
`ORG:` charter pages. Guild Order sells hosted org sites with a wiki plugin.

**If an org wants a full forum anyway,** Discourse with Discourse Connect
pointed at *our* login is the one to recommend. It runs as their own second
service, out of scope for this repo, and the Markdown export above is how
they'd move content across.

### Building it in (recommended)

| piece | choice | license | why |
|---|---|---|---|
| Markdown → HTML | **markdown-it-py** 4.2 (+ `mdit-py-plugins` for task lists and footnotes) | MIT | CommonMark-compliant, raw HTML can be switched off, plugin API for `[[ref:id]]` |
| Sanitizer | **nh3** 0.3.7 (Rust ammonia binding) | MIT | Prebuilt wheels, so no compiler in the image. **Not bleach**: deprecated since 2023 |
| Search | SQLite **FTS5**, `porter` tokenizer (English stemming) | stdlib | Nothing new to install; `bm25()` + `snippet()` |
| Revisions | full text per revision, stdlib `difflib` word-level diff, rendered server-side | stdlib | No client diff library needed |
| Editor | `textarea.ti` + toolbar + Write/Preview | n/a | No build step, nothing new under CSP, already styled |
| Preview | `POST /api/intel/render` (same pipeline as publish) | n/a | Preview can't differ from what gets published, and nothing is vendored into the single `<script>` |

That's **two new pip dependencies** (`markdown-it-py`, `nh3`), both MIT,
both pure wheels. Each gets a security floor in `requirements.txt`, like the
existing ones.

Rejected for v1:

- **Vendored JS** (`markdown-it` 15 + DOMPurify 3.4 for a client-side live
  preview). It would mean a second `<script>` or pasting ~60 KB into the
  shell. A server round-trip on a Preview click is fast enough.
- **EasyMDE.** No build step, but it bundles CodeMirror 5 and by default
  pulls Font Awesome from a CDN, which the CSP blocks **[verify]**.
- **TipTap / Milkdown.** ESM-only, so they need a bundler. They also store
  ProseMirror JSON instead of Markdown.

### Ideas borrowed from real intel practice

- **IPB's four steps** (ATP 2-01.3) map onto what members write: *define the
  operational environment* → place dossiers; *describe its effects* → notes on
  armistice zones, QT lanes and hazards; *evaluate the threat* → org
  dossiers; *determine threat courses of action* → tactics ("most likely /
  most dangerous" is a good tactic template).
- **SALUTE** as the report template, with location and time pre-filled from
  the member's last `/showlocation` fix. The watcher already sends it.
- **Admiralty 6×6** grading, two independent axes. One concrete rule: a
  second member's **Corroborate** suggests raising credibility to `2`
  (probably true) and two suggest `1` (confirmed). Only the author or an
  admin can apply it, because a grade is a judgement, not a vote count.
- **Intellipedia's lesson.** Few rules up front, but an explicit status so
  readers know what's authoritative. Procedures get a three-step status:
  **Draft · Reviewed · Doctrine**. Only an admin (or a configured "doctrine"
  role) can mark something Doctrine, and the library sorts Doctrine first.

### Discord forum channels: the complement, not the competitor

Discord forum channels are what most orgs would reach for instead. They're
good for discussion and terrible as a record:

- threads auto-archive;
- there's no canonical current version, no history, no structured fields;
- search is weak;
- nothing links to the app;
- content is lost with the server.

The pattern to adopt: **the app is the record and Discord is the
conversation.** A webhook pointed at a *forum* channel can create a post per
entry (`thread_name`, `applied_tags` per kind) and post later revisions into
the same thread (`thread_id`, stored on the entry). This still needs no bot
and fits `notify.py` as is. An org then picks, per install, where comments
live:

- in-app (for RESTRICTED entries this is required);
- in the entry's Discord thread, with the app linking to it.

## Slices

Each slice is a shippable PR that's useful on its own.

0. **Org Intel two-level masthead** (half switch, scoped ← / →,
   `#/intel/overview`), data model, render pipeline, FTS5 probe, an empty
   Intelligence half, and the tile description. Activity is unchanged apart
   from moving under the switch.
1. **Library**: procedures + tactics, editor (formatting allow-list,
   toolbar, paste-to-Markdown), revisions with diff + revert,
   owner/maintainers, patch stamp + staleness, search.
2. **Access + review**: RESTRICTED marking + clearance roles, the
   admin-enforceable review gate + reviewer roles (one role-list mechanism
   for both). Comes before reports and dossiers, where sensitive material
   first appears.
3. **Reports + feed**: SALUTE header, grading, corroborate/dispute, addenda,
   valid-until; reference chips + backlinks; danger board **Write up →**.
4. **Dossiers**: place + org, open edit, auto rollups, navigator
   "◆ N intel entries" chip + "Start dossier".
5. **RFIs + comments**: answers, accept, promote.
6. **Discord publishing**: the `intel` category + per-kind channels, the
   publish panel, role pings, templates, forum-channel threads, RESTRICTED
   placeholder posts.
7. **Debriefs + required reading**: op **Write debrief →**, event required
   reading, read receipts on signups.
8. **Images + portability**: one-click image loading per entry,
   export/import `.zip`, collections, watch, flag queue. The data model for
   all of these ships in slice 0 (see *Member features*).
9. _Later, on demand:_ uploads under an org quota, procedure checklists run
   on a live op, pin to watcher HUD.

## Decided 2026-10-31 (user accepted the recommendations)

1. **Landing.** `#/intel` remembers each member's last half. A first visit
   lands on INTELLIGENCE → FEED.
2. **RESTRICTED is not in launch slice 1, but comes straight after it.**
   Access + review becomes **slice 2**, ahead of reports and dossiers, which
   are where sensitive material would first appear. The slices below are
   renumbered to match.
3. **Procedures** are edited by the owner, named maintainers and admins.
   Dossiers are open edit.
4. **No public entries in v1.** `auth_gate` stays deny-by-default.
5. **Debriefs are their own kind**, because of the pre-filled op header.
6. **Comments live in the app**, always. If an entry was posted to a Discord
   forum channel, the entry links to its thread ("discuss in Discord →") as
   well. Restricted entries have in-app comments only.
7. **Doctrine** is marked by admins and the reviewer role (one "trusted to
   vet" role, shared with the review gate).
8. **Review gate**: per kind. Reports are exempt by default. Admins and
   reviewers publish their own entries directly.

## Open questions

None outstanding. New ones go here.
