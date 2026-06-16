# Multi-user / org migration plan

How to evolve SC Nav from a single-user LAN tool into an org-scoped service on a
public IP (AWS EC2 + elastic IP), gated to one Discord guild, with live teammate
visibility on the map. This is a **design/plan only** — nothing here is built yet.

Guiding constraint: **`nav_core` never changes.** It operates on an in-memory
`NavData`, so every phase only touches `app.py`'s state/auth layer plus the
loader. Search / forecast / finder math is untouched throughout.

## Goal

Let an org's players use search, courses (per-user destination/breadcrumbs), and
add POIs/observations all at once, while locking out anyone not in the org's
Discord — even though the server is on a public IP.

## Decisions locked

1. **Auth gate = Discord guild membership** (everyone in the guild; no role gate,
   no bot). Scopes: `identify` + `guilds`.
2. **Identity = Discord user ID, permanently.** The RSI handle is **cosmetic** (it
   changes; people have alts). One Discord ID = one contributor. Filtering "by
   contributor" keys on Discord ID; the handle is a display label.
3. **Live teammate presence**: share **on by default, toggleable**, **one-way**
   opt-out (hide yourself but still see others).
4. **Handle stored as a per-record snapshot** at capture time (what they called
   themselves then). Owner key is always the Discord ID.
5. **Admins = a static list of Discord IDs** (`ADMIN_IDS`) for v1.
6. **Single uvicorn worker, always** — sessions/presence live in process memory.

## Identity model

- Canonical user = Discord user ID (snowflake string), immutable.
- Profile: display name + avatar (from Discord) + cosmetic handle + share toggle.
- Contributions (`custom_pois`, `observations`) attributed by `owner_id =
  discord_id`, plus a `handle` snapshot for display. Renames/alts never fragment
  history.

## Auth handshake

**Browser (interactive):**
```
GET /auth/login  -> 302 Discord authorize (scope=identify+guilds, state=<csrf>)
Discord -> GET /auth/callback?code&state
  -> exchange code -> GET /users/@me  (id, username, avatar)
                    -> GET /users/@me/guilds  -> assert ORG_GUILD_ID in list
  -> not a member: 403
  -> else: mint signed session cookie {discord_id, exp, ver}; 302 to app
```
Cookie httpOnly/Secure/SameSite. Re-check membership at login and on session
expiry (few-hour TTL); re-auth is a fast silent redirect (still logged into
Discord), so no refresh-token storage needed.

**Watcher (headless, can't do OAuth):**
```
(logged-in browser) POST /api/tokens {label}  -> returns opaque token ONCE
                    GET  /api/tokens           -> list (id,label,last_used)
                    DELETE /api/tokens/{id}     -> revoke
Watcher -> every request: Authorization: Bearer <token>
Server: hash -> look up -> discord_id (401 if unknown/revoked)
```
Tokens stored hashed, bound to discord_id, revocable. Watcher lifecycle is
independent of the browser session. The watcher may still send a cosmetic handle
label; it is no longer identity.

## Per-user server state

```
sessions[discord_id] = {
  pos, t, prev_pos, prev_t,        # was global AppState
  destination_id, nav_state,       # was global; computed per user
  capture_pending, last_capture,   # per-user arming (no more shared 409)
  tracking, path,                  # per-user breadcrumb trail
  share_presence: bool = true,
  ws_clients: set[WebSocket],      # this user's browser tabs
}
presence[discord_id] = {handle, system, body, lat, lon, heading, last_update}
   # present only while share_presence and on a body surface
```

## WebSocket message contract

Connect over WSS with the session cookie; handshake resolves the Discord ID or
rejects. The watcher is NOT a WS client — it only POSTs.

**Server -> client:**
```jsonc
// your own nav — only to YOUR tabs, on your position/destination/capture change
{ "type": "self", "data": <nav_state>, "capture": {…} }

// initial teammate snapshot on connect
{ "type": "roster", "users": [ {discord_id, handle, system, body, lat, lon, heading, age_s}, … ] }

// teammate deltas (throttled ~1–2 Hz)
{ "type": "presence", "op": "upsert", "users": [ {discord_id, handle, system, body, lat, lon, heading, age_s} ] }
{ "type": "presence", "op": "remove", "discord_id": "…" }   // offline / stale / opted-out
```
**Client -> server:** keepalive pings only. Share toggle + profile via
`PUT /api/me {handle, share_presence}`.

**Fan-out for one watcher POST (user U):**
```
POST /api/position (Bearer token -> U)
  1. update sessions[U].pos; recompute U's nav_state; record U's breadcrumb
  2. send {type:"self"} to sessions[U].ws_clients          # only U's tabs
  3. if U.share_presence and on a body:
       update presence[U]; broadcast {type:"presence",op:"upsert"} to ALL tabs  # throttled
```
Toggling share off emits `presence/remove` and stops broadcasting U (one-way: U
still receives). Server drops a presence entry after no update for N minutes
(emits `remove`); clients also fade markers locally. Map draws same-body
teammates as a toggle layer; a roster lists everyone across systems.

## Endpoint auth matrix

| Group | Auth | Notes |
|---|---|---|
| `/auth/*`, `/api/health` | none | login + liveness |
| reads: `/api/pois`, `/api/observations`, `/api/resource_*` | session OR token | org-only |
| `/api/position` | token | watcher; attributes to U |
| `/api/destination`, `/api/capture/*`, `/api/path/*` | session | per-user |
| delete `/api/custom_pois/{id}`, `/api/observations/{id}` | session | owner OR admin |
| `/api/me`, `/api/tokens*` | session | profile + watcher tokens |
| `/api/refresh` | admin | static `ADMIN_IDS` |

## Phased migration (each phase ships and keeps the app working)

### Phase 0 — Front door (gate only; state still global)
Delivers the lockout without the risky refactor.
- Caddy reverse proxy + TLS (domain -> elastic IP, auto Let's Encrypt). Security
  group: 443 + locked-down 22. Never expose raw uvicorn.
- Discord OAuth: `/auth/login`, `/auth/callback`, signed session cookie.
- Auth deps: `require_session`, `require_token`, `require_user` (session OR
  token), `require_admin`.
- Watcher tokens: `POST/GET/DELETE /api/tokens`.
- Apply the auth matrix to every endpoint; SPA redirects to `/auth/login`; WS
  handshake reads the cookie.
- State is STILL the global singleton — behaves like today (one shared cursor)
  but org-gated. Shippable.
- Watcher gains a token in its config; its handle becomes a cosmetic label.

### Phase 1 — Per-user sessions (core refactor)
Replace `AppState` singletons with `sessions[discord_id]`:

| Today (global AppState) | Becomes |
|---|---|
| `pos,t,prev_pos,prev_t` | `sessions[uid]` |
| `destination_id`, `nav_state` | `sessions[uid]` (per user) |
| `capture_pending`, `last_capture` | `sessions[uid]` (no shared 409) |
| `tracking`, `path` | `sessions[uid]` |
| `owner` | gone — the session IS the user |
| `clients` set | `sessions[uid].ws_clients` |
| `obs_id_seq` | stays global atomic allocator until Phase 2 |

- `/api/position` (token->uid): mutate `sessions[uid]`, recompute that user's
  `nav_state`, push `{type:"self"}` to `sessions[uid].ws_clients` only.
- `/api/destination`, `/api/capture/*`, `/api/path/*` (cookie->uid): operate on
  the caller's session. Arming now binds correctly — browser arms
  `sessions[uid].capture_pending`, the watcher's next position for the same uid
  fulfils it (cookie + token both resolve to one discord_id).
- Captures store `owner_id = uid` + `handle` snapshot.
- WS connect: resolve uid, add to `ws_clients`, send current self-state.
- Rename client `"state"` message handling to `"self"`.
- Now simultaneous courses + concurrent captures work. Still JSON-backed.

### Phase 2 — SQLite (durable, concurrent-safe)
- Tables: `custom_pois`, `observations`, `profiles` (discord_id, display_name,
  handle, share_default), `watcher_tokens`. Autoincrement ids kill the in-memory
  counter races; transactions serialize writes, reads stay concurrent.
- Loader changes from "read JSON files" to "query DB," still building a
  `NavData` for `nav_core` (search/forecast/finder untouched). `/api/refresh`
  rebuilds `NavData` from DB + upstream cache.
- One-time import script for existing `custom_pois.json`, `resource_nodes.json`,
  `wildlife.json`, `handles.json`.

### Phase 3 — Presence (teammate map)
- `presence: dict[uid, record]` + throttled (~1–2 Hz) broadcaster.
- `/api/position`: if `share_presence` and on a body, update presence and
  broadcast upsert to all sessions' ws_clients. WS connect sends `roster`.
- `PUT /api/me {handle, share_presence}`: off -> emit `presence/remove`, stop
  broadcasting, but keep receiving (one-way).
- Background sweeper drops stale presence (no update N min -> `remove`).
- SPA: teammate map layer (same-body, distinct color + handle labels), roster
  panel, share toggle.

### Phase 4 — Admin & ops
- `require_admin` (static `ADMIN_IDS`) for delete-anyone, token admin,
  `/api/refresh`.
- Ownership-scoped deletes: non-admin deletes only `owner_id == uid`.
- EBS snapshots of the SQLite/data volume — contributions are now irreplaceable.

## Watch-items
- **Single worker stays mandatory** — sessions/presence are in process memory.
  Document loudly; don't add workers. (Redis only if you ever scale out — you
  won't at org size.)
- Secrets (session-signing key, Discord client secret) via env/SSM, never in the
  image.
- Same-origin cookie-authed WebSocket works behind Caddy on one domain.
- Membership drift handled by re-check on session expiry.

## Recommended order
Phase 0 (deployable lockout) -> 1 (simultaneous courses + captures) -> 2
(durability) -> 3 (teammate map) -> 4 (admin/backups).

## Current single-user architecture (baseline being migrated)
- FastAPI + single global `AppState` (one live cursor): `pos`, `destination_id`,
  `capture_pending` (single slot, 409 if armed), `tracking`/`path` (one global
  breadcrumb), `clients` set; WS broadcasts one `nav_state` to all clients.
- Watcher posts `/api/position` with an optional free-text handle;
  `HandleRegistry` maps handle -> player_id.
- Data in a Docker named volume: upstream cache (`poi.json`/`containers.json`),
  `custom_pois.json`, `resource_nodes.json`, `wildlife.json`, `handles.json`,
  `commodities.json`. IDs via in-memory `max()+1` / monotonic counters.
- Deployed today at 192.168.1.68:8765 (LAN), systemd/Docker.
