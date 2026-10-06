# Discord notification customization (images + message templates)

**Status:** ✅ all slices built 2026-10-05/06 (1 attachments + org image · 2 per-event banner · 2b known-host previews · 3 LFG/danger/goal embeds · 4 template registry · 5 template editor); unreleased. Written against v1.19.1.
**Revised 2026-10-06 (security sweep):** event banners are **link-only** — the member upload path in §3.2 (S1's "upload" half, S9, the `notify_images` table, the sweep) was removed: 30 uploads/h × 4 MB per member with a startup-only sweep had no storage ceiling, and an event-referenced file was never swept. The ADMIN org-image upload (§3.3, one file replaced in place) stays. Discord-CDN links are still refused; members host banners on an image site. Same sweep: every template var carrying member text is now `md` (a nickname `[text](url)` rendered as a masked link), and the in-app `discordTime` guards non-finite dates (a crafted `<t:…:R>` threw and blanked the event page). The §3.2 upload text below is kept as the historical design.
**Origin:** a self-hosting org asked to put its own graphics on event
announcements. That raised the general question of what an admin can change
about the messages the app posts to Discord.
**Builds on:** [discord-notifications.md](discord-notifications.md) (the
webhook-only dispatcher, #18) and the 2026-08 embed upgrade (`_embed()`,
`notify.send(embed=)`, pacing, delivery health).

---

## 1. Goals and non-goals

**Goals**
- An event announcement can carry an image the org made, either **linked from
  an external host** or **uploaded to this server**. Both are supported; neither
  is the "real" way.
- An org can set an **org image** that appears on every announcement. It's
  **off by default**: not every org has art or wants images in its channel.
  When an admin turns it on, the Org Navigator patch logo is offered as a
  one-click choice beside Upload and URL.
- An admin can **reword the announcement messages** (title, description,
  footer, color) using fill-in variables, without touching code. A fresh install
  posts exactly what it posts today.

**Non-goals (deliberate)**
- **Pings are not templatable.** Who gets @-mentioned stays decided by code
  (`allowed_mentions` = `parse: []` + an explicit id list). A template can't add
  or remove a ping, and `@everyone`/role mentions can never fire.
- **No logic in templates.** No conditionals, loops, or expressions. See §4.1 for
  how optional lines are handled without them.
- **Transactional messages stay fixed** (outbid, offer declined, waitlist
  promoted, payment and settlement notices). They answer "what happened to *my*
  thing" and gain nothing from branding.
- **Organizers don't reword messages.** Organizers pick an event's image.
  Admins own the wording.
- **No bot.** Everything stays webhook-native (§2 of the original doc).

## 2. Settled calls

| # | Call | Why |
|---|---|---|
| S1 | **Hybrid image source**: external URL *or* upload (sent as a webhook attachment) | User's call. A URL costs nothing for an org that already hosts its art. An upload works for everyone else and never expires |
| S2 | **Templates = slot overrides with `{variable}` substitution** (option A) | User's call. Same shape as the existing org copy overrides. Low risk, and a typo is caught when the admin saves, not when the message posts |
| S3 | **No image by default.** The org image is opt-in; the Org Navigator patch logo (`server/static/images/sc_org_navigator_logo.png`, 359×360) is a ready-made choice, not a default | User's call (2026-10-05, revising the first draft). A fresh install posts exactly what it posts today |
| S6 | **The org image goes in the `thumbnail` slot**, the event graphic in the `image` slot | User's call (was D1). A square mark in the banner slot renders ~400 px tall |
| S7 | **Template test posts are admin-only** | User's call (was O4). An organizer test would spam the org channel |
| S8 | **In-app preview of external image URLs is an admin toggle, off by default**, with the privacy warning in §4.7 | User's call (was O3). The CSP stays `img-src 'self' data:` unless an admin opts in |
| S9 | **GIF uploads accepted** under the same 4 MB cap; in-app preview respects reduced motion | User's call (was O1). See §3.2 for the drawbacks |
| S10 | **External previews: known hosts only**, no "any https host" option | User's call (was O5). Closes the organizer-run tracking server case |
| S4 | Uploads are **attached to the webhook post**, never served publicly | Discord hosts an attachment permanently in the channel. A public image route would widen `auth_gate`'s anonymous surface, which the security review says to keep closed |
| S5 | Shipped wording lives **only in code**, and an empty override means "use the shipped text" | Mirrors `org_copy()`. A fresh install is byte-identical to today, and there's no second copy of the defaults to drift |

## 3. Images

### 3.1 Two slots, not one

A Discord embed has two image slots. This design gives each a job:

| Slot | Renders as | Filled by |
|---|---|---|
| `thumbnail` | small square, top-right | the **org image**, when an admin has set one (none by default) |
| `image` | full-width banner under the text | the **event's own graphic**, when it has one |

An org mark is usually square (the patch logo is 359×360), and a square in the
full-width `image` slot renders about 400 px tall, which would make every post
in the channel tall. As a thumbnail it brands each post at a glance. An org's
custom event graphic is usually a wide banner, which is what the `image` slot is
for. A post can carry both, either, or neither, and the slots are independent:
an event can have a banner even when the org has no org image.

### 3.2 Image sources

An image reference is stored as a small JSON object wherever it's used:

```json
{"kind": "url",    "url": "https://i.imgur.com/abc123.png"}
{"kind": "upload", "hash": "3f9a…16 hex", "ext": "png"}
```

**External URL** (`kind: "url"`)
- `https://` only, ≤ 1,000 chars, no userinfo (`user:pass@`).
- **Rejected: Discord attachment links** (`cdn.discordapp.com/attachments/…`,
  `media.discordapp.net/attachments/…`). These have been signed and expiring
  since 2024: the image works the day it's pasted and is gone about a day later,
  so a reminder posted the next evening would show nothing. The 400 says so and
  points at Upload. This is the single most likely way an org would break the
  feature without noticing.
- **We never fetch the URL.** Discord's media proxy does, so there's no SSRF
  surface on our side. A dead URL just renders the embed without the image;
  Discord doesn't reject the message.
- No extension check. Plenty of image hosts serve images from URLs without one.

**Upload** (`kind: "upload"`)
- Same checks as the org logo and app art: PNG/JPG/WebP/GIF by Content-Type **and**
  magic bytes (`_sniff_image`), no SVG.
- Size cap **4 MB** (the logo cap is 2 MB, but event graphics are larger and we
  have no image library to re-encode with). Discord's webhook attachment limit
  is well above this.
- Content-addressed storage: `DATA_DIR/notify_images/<sha256[:16]>.<ext>` plus
  a `notify_images` table (`hash`, `ext`, `bytes`, `uploaded_by`, `created`).
  Identical uploads dedupe. A reference can be **copied** (event → template →
  cloned event) without copying bytes, because files are immutable. This follows
  the event templates' "copy, never link" rule: nothing is shared mutably.
- Members view it through `GET /api/notify-images/{hash}` (member-only, hash
  regex-validated, path built from the validated hash; never from user input)
  so the event form and event page can preview it.
- **Cleanup:** a startup sweep deletes files no event, template, or setting
  references, once they're older than 7 days (the grace period covers a form
  that uploaded but hasn't saved yet).
- Upload goes through the `token` rate-limit bucket (or a new `upload` bucket)
  so one member can't fill the volume.

**Animated images (settled, S9).** GIF is accepted alongside PNG/JPG/WebP:
`_sniff_image` gains the `GIF87a`/`GIF89a` magic. Animation was already
possible (APNG passes the PNG check, animated WebP the WebP check, and a URL can
point at anything), so this only adds the format orgs actually have. The
drawbacks, and how each is handled:
- **Size.** GIF compresses badly; a few seconds of banner is easily 5–15 MB.
  The 4 MB cap is the guardrail. Without an image library we can't re-encode,
  so the rejection message says the limit plainly and suggests a shorter or
  smaller GIF, or hosting it and pasting the URL.
- **Repeated upload.** An attachment is re-sent with every post (created,
  reminder, rescheduled); the cap bounds the cost.
- **Photosensitivity and distraction.** We can't inspect frames, so we can't
  catch fast flashing. Discord honors each viewer's "play GIFs automatically"
  setting. The in-app preview respects `prefers-reduced-motion`: it shows a
  placeholder with "Play" instead of autoplaying.
- **Parser risk** is negligible on our side: we only check magic bytes and
  never decode the image.

### 3.3 Where images can be set

| Where | Who | Slot | Stored in |
|---|---|---|---|
| Settings › Discord → **Org image** | admin | thumbnail | meta `notify_org_image` (an image ref; absent = no thumbnail). Choices: Org Navigator patch (`{"kind": "shipped"}`) · Upload · URL · Remove |
| Event form → **Announcement image** (URL tab / Upload tab, preview, remove) | organizer | image | `events.notify_image` (JSON, `_ensure_column`) |
| Event template form | template owner | image | the template's `event` JSON (`event.notify_image`, no new column); copied onto events created from it, and kept by Clone |

> **Optional later (O2):** a per-category thumbnail override (e.g. a different
> mark on the marketplace channel). It's the same machinery keyed by category.
> Leave it out of v1 unless an org asks.

### 3.4 Resolution at send time

`_notify_images(kind, event=None) -> list[Attachment | UrlRef]`:

1. `thumbnail`: the org image if set, else **none**. The `shipped` kind is
   attached from `server/static/images/sc_org_navigator_logo.png` (279 KB).
2. `image`: the event's `notify_image`, if any, on **created, reminder, and
   rescheduled** posts. Not on **cancelled**: a celebratory banner on a
   cancellation reads wrong.
3. An image that's missing on disk (volume restored without it) is skipped,
   never an error. The post goes out without it.

Which notifications get the thumbnail: the **announcement** set in §4.3. An
org mark on "you were outbid" is noise. With no org image and no event image, a
post has no attachments and goes out as plain JSON, exactly as today.

### 3.5 Dispatcher change (`server/notify.py`)

`send()` gains `files: list[tuple[str, bytes, str]]` (filename, bytes, mime).
When `files` is non-empty, `_post` sends `multipart/form-data`:

- `payload_json` = the existing JSON payload, plus `attachments: [{id, filename}]`
- `files[0]`, `files[1]`, … = the bytes

The embed points at them with `"thumbnail": {"url": "attachment://thumb.png"}`
and `"image": {"url": "attachment://banner.png"}`. Filenames are fixed ASCII
(`thumb.<ext>`, `banner.<ext>`), never user-supplied. The multipart body is
hand-encoded with `urllib`, the same as today's JSON path, so there's no new
dependency.

Unchanged: pacing (`_pace`), 429 retry (it re-sends the same body), and delivery
health (`_note_result`).

**Degrade, don't drop:** if a post with attachments fails with 413 or 400,
retry once **without** the files (the URL-based image, if any, stays). The
announcement still lands; the health row records the error so the admin sees
why the art went missing. A notification must never be lost to its decoration.

`send_paged` attaches files to page 0 only, the same as the embed.

## 4. Message templates (option A)

### 4.1 The template language

- Substitution only: `{name}`, matched by `\{([a-z_]+)\}`. `{{` and `}}`
  produce literal braces. **Not `str.format`**, because format strings allow
  attribute and index access (`{ev.__class__}`).
- Each template key declares its **closed** variable set. Saving a template that
  names an unknown variable fails with a 400 that lists the valid ones. Typos are
  caught when the admin saves, not at 2 a.m. when the reminder fires.
- **Line-drop rule** (instead of conditionals): after substitution, a line whose
  variables all came out empty, *and* that had at least one, is removed. So
  `📍 {location}` disappears for an event with no location, and the shipped
  defaults keep doing what their `if` statements do today.
- **Escaping:** values that came from members (event titles, notes, names) go
  through `_md_plain` in description and footer slots, which render markdown.
  Titles render no markdown, so they're inserted raw. Variables that are already
  formatted (`{start}` → `<t:…:F>`, `{link}`) are marked "pre-formatted" in the
  registry and never escaped.
- Length caps after rendering are unchanged (`notify.send` already truncates
  title 256, description 4096, field value 1024). Template text itself is capped
  at save time: title 200, description 1,500, footer 200.

**As built (slice 4, 2026-10-06), where it refines the above:**
- The engine and the shipped wording live in `server/notify_templates.py` (`TEMPLATES`, `render`, `render_text`, `template_vars`). Builders in `app.py` compute RAW values and call `_announcement(key, values, url=, color=, fields=)`.
- **The drop rule works at three levels, not just lines.** Any part that names variables, all of which came out empty, is left out. A part is a ` · `-separated piece of a line, a line, or a paragraph. This was needed to reproduce today's posts exactly ("Starts … · 1 h 30 min" drops just the length; the "Mission briefing" paragraph vanishes when no detail is set), and it's one rule to explain to admins.
- **Escaping is per slot, not per variable.** A variable marked `md` (member text) is escaped in the DESCRIPTION only, the one slot Discord renders markdown in. A name moved into a title by an admin won't show stray backslashes.
- **Byte-identical, proven:** `server/testdata/notify_golden.json` was generated from the pre-registry builders over 26 inputs (committed before the refactor) and `NotifyGoldenTests` replays it. A test also pins that each builder supplies exactly its template's declared variables.
- ~~Kept identical on purpose and worth a later look: the reminder's place, the listing poster and the goal poster/description go out unescaped~~ — closed by the 2026-10-06 security sweep (PR #245): every member-text variable is `md`; the goal description is `fmt` (formatting kept, masked links defused) like the event description.

### 4.2 Slots

For each template key, an admin can override:

| Slot | Notes |
|---|---|
| `title` | embed title |
| `description` | embed body |
| `footer` | new. Shipped default is empty, so no footer appears until an admin adds one (e.g. `{org_name} · fly safe`) |
| `color` | `#RRGGBB`; shipped default = the current `_EMBED_*` color for that key |

**Not overridable:** `content` (the ping line), the embed `url` (the deep link
into the app), structured `fields` (the op record's attendance and payout
tables). Templates change the voice of a message, not its data.

### 4.3 What's templatable in v1 (the announcement set)

| Key | Today | Channel | Example variables |
|---|---|---|---|
| `event_created` | embed | events | `title`, `start`, `start_relative`, `location`, `rally`, `organizer`, `types`, `slots`, `link`, `org_name` |
| `event_reminder` | embed | events | same + `lead` ("in 30 minutes") |
| `event_rescheduled` | embed | events | same + `old_start` |
| `event_cancelled` | embed | events | `title`, `start`, `organizer`, `reason`, `org_name` |
| `listing_posted` | embed | market | `headline` (mode copy: FOR SALE / AUCTION / …), `item`, `qty`, `price`, `seller`, `terms`, `link` |
| `lfg_posted` | **plain text** | lfg | `poster`, `direction`, `slots`, `filled`, `tags`, `note`, `rally`, `link` |
| `warning_posted` | **plain text** | pirates | `severity`, `kind`, `where`, `note`, `poster`, `link` |
| `goal_posted` | **plain text** | goals | `title`, `percent`, `due`, `poster`, `link` |
| `op_closed` | embed | ops | **title + footer + color only.** Its body is structured fields |

The three plain-text builders (LFG, danger board, goals) have to become embeds
first. That was already the parked "goals/records/lfg/pirates builders still
plain" follow-up from the 2026-08 embed batch. It's a prerequisite slice (§6),
and it gives those posts the thumbnail too.

Each variable in the registry carries a one-line description and a **sample
value** (used by the preview and test send).

### 4.4 Code shape

```python
# One registry, in code, holding the shipped wording:
NOTIFY_TEMPLATES = {
  "event_created": NotifyTemplate(
      category="events", color=_EMBED_INFO,
      title="📅 New event: {title}",
      description="Starts {start} ({start_relative})\n📍 {location}",
      footer="",
      vars={"title": Var("Event title", "Mining op: Aaron Halo", user=True), …}),
  …
}
```

Builders stop writing f-strings. They compute a `vars` dict and call
`_render_notify(key, vars) -> embed`, which merges admin overrides (meta
`notify_tpl:<key>` → JSON `{title?, description?, footer?, color?}`; an absent
or empty slot means shipped) over the registry default.

**The refactor must be byte-identical.** Slice 3 lands with zero behavior change,
pinned by tests that render every key with no overrides and compare against
today's builder output.

### 4.5 API (admin)

| Route | Purpose |
|---|---|
| `GET /api/admin/notify-templates` | every key: category, slots (shipped + override), variables w/ descriptions + samples |
| `PUT /api/admin/notify-templates/{key}` | save overrides. Validates variables, caps, color |
| `DELETE /api/admin/notify-templates/{key}` | reset all slots to shipped |
| `POST /api/admin/notify-templates/{key}/preview` | body = draft slots → rendered embed JSON with sample values. **The server renders the preview** so there's no JS copy of the renderer to drift |
| `POST /api/admin/notify-templates/{key}/test` | send the rendered sample (with images) to that category's webhook. Bypasses dedup; rate-limited; title prefixed `[TEST]` |
| `GET/PUT/DELETE /api/admin/notify-default-image` (+ upload) | the thumbnail slot (§3.3) |

`{key}` is checked against the registry (404 otherwise), the same closed-set
pattern as `APP_IMAGE_KEYS`.

**Slice 5 as built (2026-10-06):** overrides are meta rows `notify_tpl:<key>` (`{title?, description?, footer?, color?}`; blank = shipped), validated by `notify_templates.validate_override`: any `{…}` that isn't one of the template's fields fails with the list of valid ones (so `{Title}`, `{title.x}` and typos are caught, not silently left literal), caps 200/1500/200, `#RRGGBB`, and the op record's body is not editable. A colour override replaces the automatic variations (deadly red, goal-met green), and the editor says so. Routes match §4.5; preview and test take the DRAFT, so an admin can test before saving.

### 4.6 UI: Settings › Discord

The Discord section of Settings (`#/settings/discord`, the admin category rail added before slice 1) gains panels under the existing webhook rows:

- **Org image**: "None" until set. Choices: Use the Org Navigator patch · Upload ·
  Use URL; once set, a thumbnail preview and Remove.
- **Preview external images in the app**: the §4.7 toggle, off, with its warning.
- **Message list**, grouped by channel (Events, Marketplace, Group Finder,
  Danger Board, Goals, Ops). Each row: key name, "customized" chip when any slot
  is overridden, Edit.
- **Editor**: one field per slot, **variable chips** under each (click to insert
  at the cursor, hover for description), color picker, and a **Discord-style
  preview card** (thumbnail + banner placeholder + title/description/footer
  rendered from `/preview`, debounced). Buttons: Save · Reset to shipped ·
  Send test (disabled with a reason when the channel has no webhook).
- Validation errors from Save point to the slot and name the bad variable.

Event form: an **Announcement image** block with URL | Upload tabs, a preview,
and Remove. Uploads always preview. A URL image previews only when the admin
has turned on external previews (§4.7); otherwise the block shows the link and
a note that Discord will display it.

"Send test" is **admin-only** (S7), both for templates and for an event's
announcement. An organizer who wants to see their banner in Discord asks an
admin, or sees it when the event posts.

### 4.7 External image previews (admin toggle, off by default)

The SPA's CSP is `img-src 'self' data:`, which is why an Imgur link can't render
in the app today. The toggle lets an admin relax that for previews.

**What leaks when a member's browser loads an external image.** The image host
(Imgur, or whatever server the organizer chose) receives:

- the member's **IP address**, which gives a rough location and ISP;
- the **time** they opened the event page or form;
- their **User-Agent** (browser, version, OS) and **Accept-Language**;
- any cookies the browser holds for that host, though most browsers now block
  third-party cookies.

It does **not** receive the page address: our `Referrer-Policy: same-origin`
strips the Referer on cross-origin requests. It doesn't receive the member's
Discord identity or anything else from the app.

**The sharper risk is internal, not Imgur.** Any organizer chooses the URL. An
organizer who points it at a server they run sees every request, and a unique
URL per event tells them roughly who looked and when. That's the insider case
from the security review: a member tracking other members. Discord itself never
exposes viewers this way, because it fetches images through its own proxy.

**The toggle.**
- Settings › Discord → "Preview external images in the app",
  **off by default**, stored in meta `notify_external_preview`.
- Turning it on opens a confirm dialog that states the list above in plain
  words, including the organizer-run-server case, and requires an explicit
  "Turn on".
- **Known hosts only** (settled, S10). When on, `_csp()` adds a fixed list of
  established image hosts (`https://i.imgur.com`, …; the list lives in code,
  not in settings) to `img-src`. Their tracking risk is the generic one above,
  and an organizer **can't** point a preview at a server they run, which removes
  the insider case. There is deliberately no "any https host" option.
  `_csp()` reads the setting per request, so turning it off takes effect on the
  next page load.
- A URL on an unlisted host never previews in the app, toggle or not; the block
  shows the link and "Discord will display this". The image still posts to
  Discord normally. The host list only governs in-app previews.
- In the event form, a known-host preview loads behind a
  "Load preview from i.imgur.com" click, so a member's browser contacts the host
  only when they choose to. On the event page it loads automatically.

**Not considered: proxying the image through our server.** That would hide
members' IPs, but our server would then fetch arbitrary organizer-supplied URLs.
That's an SSRF surface (requests into the host's private network) plus
bandwidth, and it's the kind of widening the security review says not to do
casually.

## 5. Security notes

- No new anonymous route. Uploaded images are member-only to view and reach
  Discord as attachments (S4).
- No SSRF: the server never fetches an external image URL.
- Pings are unaffected by templates (non-goals). `allowed_mentions` is still
  built only in code.
- Template injection: substitution-only, closed variable set, member-sourced
  values escaped per slot (§4.1).
- Uploads: magic-byte sniff, size cap, content-addressed names (no user
  filenames reach the filesystem), rate-limited.
- `PRODUCT.md` → "Where org customization stops" gets a line: **notification
  wording and art are org presentation (customizable); what a notification means
  and who it pings are not.**

## 6. Build slices

Each slice ships on its own and is useful without the next.

1. **Attachments + org image.** `notify` multipart + degrade-retry; opt-in
   org image (patch logo · upload · URL) as the thumbnail on the existing embed
   announcements. Off by default, so no change until an admin turns it on.
2. **Per-event announcement image.** `notify_images` store + GC, member-only
   GET, `events.notify_image` + templates + Clone, event form URL|Upload block,
   admin-only event test send. *Answers the org's request.*
2b. **External preview toggle** (§4.7). Small and independent; can ride slice 2
   or follow it.
3. **Plain-text → embed** for LFG, danger board, goals (they pick up the
   thumbnail). Wording unchanged apart from the embed layout.
4. **Template registry refactor.** Move the §4.3 builders onto
   `NOTIFY_TEMPLATES` + `_render_notify`. Byte-identical, pinned by tests.
5. **Template overrides.** API (§4.5) + Settings › Discord template editor with preview
   and test send.

## 7. Tests (sketch)

- `notify`: multipart body shape (`payload_json` + `files[n]`, `attachments`
  ids), `attachment://` references, 413 → text-only retry, paged send attaches
  to page 0 only, 429 resend with files.
- URL validation: https-only, Discord-CDN rejection, length cap.
- Upload: sniff rejects a mislabeled file, size cap, dedupe by hash, GC keeps
  referenced and recent files.
- Resolver: no org image → no thumbnail and no attachments (JSON path, byte-
  identical to today); `shipped` kind attaches the patch logo; banner on created/reminder/rescheduled and
  not on cancelled; a missing file degrades silently.
- Templates: unknown variable → 400 listing valid ones; `{{`/`}}` literals;
  line-drop rule; `_md_plain` applied to user vars only; attribute-access
  attempts (`{title.__class__}`) are rejected as unknown, not evaluated;
  every key with no overrides == today's output.
- Admin-only gates on every `/api/admin/notify-*` route and on test sends;
  non-registry key → 404.
- CSP: `img-src` is `'self' data:` with the toggle off; gains exactly the
  known-host list (never `https:`) when on; flips back on the next response
  after turning off. An unlisted-host URL renders no `<img>`.
- GIF: `GIF87a`/`GIF89a` accepted, a `.gif` label on non-GIF bytes rejected,
  over-cap GIF rejected with the size message.

## 8. Open questions (summary)

| # | Question | Default if unanswered |
|---|---|---|
| O2 | Per-category thumbnail overrides | later, on request |

Settled 2026-10-05: no image by default (S3), thumbnail slot for the org image
(S6), admin-only test sends (S7), external preview as an off-by-default admin
toggle (S8), GIF accepted under the 4 MB cap (S9), known hosts only (S10). Settled at build (was O6): the host list is `i.imgur.com`, `i.ibb.co`, `i.postimg.cc`, `pbs.twimg.com` (added 2026-10-06: an org posts its art to X and links the image from there), `robertsspaceindustries.com`, `media.robertsspaceindustries.com` (RSI is where org banners already live); `app.IMAGE_PREVIEW_HOSTS`.
