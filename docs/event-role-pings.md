# Event role pings

Status: built 2026-10-09 — #266 (aliases + new-event ping) and the follow-up
(reminder pings the short roles + `{short}` reminder line). Org request.

## What it does
Admins map each event role (the fixed taxonomy in `event_taxonomy.ROLE_GROUPS`)
to the Discord roles it should call, e.g. Escort → @Escort, @Security;
Cargo / Hauling → @Haulers, @SpaceTrucker. An organizer turns on **Ping
Discord roles** on an event, and:

- the **new-event post** @-mentions the aliases of every role the event has
  seats for (`needed > 0`; a role listed with 0 seats isn't asking for anyone);
- the **reminder** @-mentions the aliases of the roles still **short** when it
  fires, and says how many each is short. Nothing short → no role ping (the
  signed-up members still get their personal ping).

Off by default per event. The toggle only appears once an admin has set up at
least one alias. Reschedule / cancel notices and live edits of the post never
role-ping (edits can't ping in Discord anyway).

## Settled calls
- **Role IDs, not names.** We're webhook-only (no bot, #18), so the app can't
  list the guild's roles, and Discord only pings `<@&id>` with that id in
  `allowed_mentions.roles` — a plain "@Escort" in text pings nobody. Admins
  paste the ID (Developer Mode › right-click role › Copy Role ID); a pasted
  `<@&id>` is accepted. The label is ours, for the settings list and the event
  form's preview.
- Only short roles at the reminder; `needed > 0` on the new-event post; one
  reminder (the existing org lead time), not several.
- Any organizer may use the toggle. The org's controls are the alias map
  (which roles can be pinged at all) and `events_admin_only` (who can create
  events).

## Guardrails
- `notify.send(roles=…)` puts exactly those ids in `allowed_mentions.roles`;
  `parse` stays `[]`, so a `<@&id>` a member types in a description never pings.
- `send_paged` puts role pings on page 1 only — a 120-signup reminder pages
  into three messages, and a role ping per page would ping the role 3×.
- A member's ping opt-out (`notify_opt_out`) can't block a role ping: Discord
  expands the role, not us. Members manage that with their Discord roles.
- Limits: 10 Discord roles per event role, 100 in all (Discord's
  `allowed_mentions.roles` cap); ids are 17–20 digit snowflakes.

Reminder message: the shipped `event_reminder` template gains a
`**Still short** {short}` line ("Escort ×2 · Salvage ×1"), shown with or
without role pings; it drops when every role is filled (the line-drop rule).
An org that already customized the reminder keeps its wording until it adds
`{short}`.

## Code
- Setting `discord_role_aliases` (JSON `{event role: [{id, label}]}`):
  `role_aliases()`, `_clean_role_aliases`, `RoleAliasIn` in `app.py`;
  Settings › Discord channels › EVENT ROLE PINGS (`renderRoleAliases`,
  `saveRoleAliases`).
- `events.ping_roles` (+ `EventIn`/`TemplateEventIn.ping_roles`; templates and
  clones carry it). `_event_ping_role_ids(ev, roles)` resolves ids; `_event_short_roles(ev)` feeds
  the reminder's ping list and `{short}`.
- `/api/events/taxonomy` → `role_pings` {event role: [labels]} (no ids) drives
  the form's `rolePingFieldHtml` / `renderRolePingPreview`.

## Open
- Can a webhook ping a role that is **not** set "Allow anyone to @mention this
  role"? Expected yes (webhooks bypass that check when `allowed_mentions`
  permits), unverified — test on dev against a locked role.
