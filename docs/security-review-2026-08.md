# Security review — August 2026

**Scope:** the whole product — server (`server/app.py`, `db.py`, `notify.py`,
`auth.py`), the SPA (`server/static/index.html`), the Windows watcher
(`watcher/`), and the deployment (`Dockerfile`, `docker-compose.yml`, CI).

Four threat surfaces were reviewed independently, then the findings were
verified against the code before anything was changed. Several were reproduced
by executing them against a scratch database, not merely reasoned about.

1. **Watcher → app** — what a malicious or compromised watcher client can do.
2. **App → user** — what a member risks by running our software on their PC.
3. **The insider** — what a signed-in, disgruntled, non-admin member can do.
4. **The outside** — what an unauthenticated attacker can reach.

**Headline:** per-object authorization is in genuinely good shape — no IDOR and
no privilege-escalation path was found across ~180 routes, and the OAuth flow,
CSRF posture, SQL layer, upload handling, and container setup are all correct.
Every real finding fell into one of three gaps: **default-open surfaces**
(things nobody remembered to gate), **unbounded trust in member-supplied data**
(no caps, no plausibility checks, no way to undo), and **no off-boarding**.

---

## What was fixed

### Deny-by-default auth gate · `app.py` `auth_gate`
`auth_gate` read *"deny `/api/*`, allow everything else"*. Everything outside
`/api/` was therefore public unless the route defended itself — and
`/openapi.json`, `/docs`, `/redoc` did not. An anonymous visitor could read the
full endpoint map, all 66 request schemas, and this codebase's unusually
detailed docstrings, which explain the reasoning behind each security guard.
The static mount had the same shape: it served everything in `server/static/`,
including a stray `.DS_Store` or the `.impeccable/` cache (absolute local paths)
on an image built from a working copy.

Now an explicit allowlist — `_PUBLIC_EXACT` (method **and** path) plus
`_PUBLIC_PREFIXES` — and the interactive docs are switched off at construction.
This is the difference between the bug being *absent* and being *impossible*: a
route added tomorrow is private until someone lists it.

### Watcher tokens scoped to the watcher's own endpoints · `_WATCHER_PATHS`
A watcher token is an unattended credential living in plaintext next to a script
on a member's gaming PC. Its *write* privilege was already narrow (3 endpoints),
but its **read** privilege was the entire org dataset: ~20 dependency-less GETs
(`/api/handles`, `/api/observations`, `/api/pois`, `/api/custom_pois`, the trade
feeds, blueprints) were authorized by the gate alone. Any malware that read that
file got a silent, permanent export channel for exactly the asset the tool
exists to accumulate.

Tokens now reach only `/api/position`, `/api/handle`, and
`/api/trade/transactions`. Admin and session routes were already correctly
unreachable — `require_admin` chains off `require_session`, which is
session-only — and that layering was verified, not assumed.

### Member off-boarding · `POST /api/admin/members/{id}/access`
Discord guild membership was checked **once, at login**. Removing a disgruntled
member from the org's Discord server — the app's entire off-boarding story —
left their signed session fully valid for the rest of its 8-hour life, and their
watcher token valid *forever*. This was the single largest gap against the
insider threat: the org's response to a bad actor didn't actually lock them out.

Revoking stamps a time; every credential issued before it stops being honored on
the member's next request, their watcher tokens are deleted (they carry no issue
time and never expire, so deletion is the only honest revocation), and their live
session and WebSockets are dropped. Restoring clears the stamp — a later login
post-dates it and works normally. Admins can't be revoked; demote first, so the
recovery path is never SQL.

### Non-finite coordinates · `PositionIn`, `_COORD_MAX`
`json.loads` accepts the non-standard literals `NaN`/`Infinity` and Pydantic
passed them through floats, but `JSONResponse` serializes with
`allow_nan=False`. So a capture armed and posted at `NaN` persisted a custom POI
that **could never be read back**: `GET /api/custom_pois` raised for every
member, permanently, and survived restarts because the row reloads from SQLite.
Recovery required deleting a row the SPA could no longer enumerate. Coordinates
are now bounded and finite at the schema edge, and `db.list_custom_pois` skips
any already-poisoned row rather than letting it keep breaking the endpoint.

A second bug surfaced while fixing this: FastAPI's default 422 handler echoes
the rejected value, so *successfully* rejecting a NaN produced a 500 when the
error itself failed to serialize. `validation_error` now reduces each error to
type/loc/msg, which also stops reflecting request payloads back to callers.

### Trade-price poisoning · `_ORG_PRICE_MAX_RATIO`, `POST /api/admin/prices/clear`
Org price observations overlay the UEX feed. The `_ORG_PRICE_MIN_CONFIRMS` gate
validates the learned guid→commodity and shop→POI **mappings** — it never looked
at the price. Once those mappings existed (they're learned legitimately for
every terminal the org actually trades at), anyone who could post a transaction
could set any terminal's price to any number. A fabricated sell price routes the
whole org to a terminal that pays nothing; a 1 aUEC buy manufactures a fake
infinite-margin leg. It **survived feed refreshes**, because the overlay is
re-applied from the ledger on every rebuild — and there was no delete function
for `price_reports` anywhere, so the only cleanup was hand-editing SQLite.

Observations are now judged against the pre-overlay UEX snapshot (never the
running value — otherwise reports ratchet the price 5× at a time), within a
deliberately loose 5× band that rejects fabrication rather than disagreement. A
report can't invent a price for a side UEX doesn't quote at all, which was the
cheapest way to conjure a lucrative fake market. And there is now an admin purge,
scopeable to one reporter so one member's bad data can be dropped without
discarding everyone else's evidence.

### Stored XSS in Prospector · `index.html:14369`
Survey-mark **ore names** are free member text, stored unfiltered, and were
joined raw into a `title="…"` attribute — while `pk.key` and `pk.name` on the
very same element were escaped. A member could arm a mark with a crafted ore
name; every member opening Prospector → DROP rendered it, with no interaction.

Severity is genuinely limited by the CSP — nonce-based `script-src` with no
`unsafe-inline` blocks `<script>` and inline handlers, `img-src 'self' data:`
and `connect-src 'self'` block exfil — but `style-src 'unsafe-inline'` still
permits a full-viewport phishing overlay rendered in the app's own trusted
origin, which targets admins as readily as members. Fixed with the codebase's
own `.map(esc)` idiom, plus a latent sibling at `index.html:9592`.

### Warning age-off · `Hub.confirm_warning`
`confirm_warning` reset a warning's age-off clock unconditionally, so the poster
— or one confirmer on a cron — could keep a warning alive indefinitely. Warnings
become hazard volumes that steer **every** member's routes (`avoid_mode` defaults
to `avoid`), so an un-ageable warning is a quiet org-wide route-poisoning
primitive. Only a genuinely new corroborator now resets the clock.

### Marketplace ping flooding · `_directed_ping_ok`, `_MAX_OPEN_LISTINGS_PER_MEMBER`
The two *directed* notification paths — a WTB matching members' inventory (up to
15 @-mentions per request) and a commission aimed at one crafter — deliberately
ignore the `announce` opt-in, because being told is the point. That left them
completely ungated, and nothing capped listing creation, so a loop targeted a
named person's phone. The victim's only defence was `notify_opt_out`, which
silences *every* notification the app sends. Both paths are now flood-gated per
member (the listing still posts; only the ping stops), and open listings are
capped per member.

### Watcher: clipboard text no longer transmitted · `build_payload`
The position payload carried a `raw` field with up to 512 characters of
clipboard text — **and the server never read it**. Anything sharing the
clipboard with a `/showlocation` fix went to the org server, and the 60-second
heartbeat re-sent it. Pure privacy surface for zero function; removed. The
server still accepts the field so older watchers keep working.

### Watcher: honest disclosure · `watcher/README.md`
Commodity-transaction capture is **on by default** and builds a permanent,
attributed, per-member ledger of every buy and sell — including personal trading
with no org involvement. The only disclosure was a console line at startup. The
README now carries a complete "what the watcher sends (and what it doesn't)"
inventory, the `--no-trade-capture` opt-out, the plaintext-token warning, and
the anti-cheat position below.

### Per-member rate limiting · `rate_limit`, `_RATE_LIMITS`
There was no rate limiting anywhere in the app — the only limiters were the
Discord-announce cooldowns and the new-handle guard. The solver routes run heavy
work that, under the GIL, pins the single worker even in a thread, so one member
looping `/api/trade/plan` degraded the app for the whole org. `/api/tokens` and
`POST /download/watcher` minted unlimited never-expiring credentials, and
`/api/trade/transactions` could grow the ledger without bound.

Everything limited is authenticated, so the unit is the **member** rather than
the IP — that's the realistic actor and it's attributable. Three buckets
(`solve`, `watcher`, `token`) with limits set far above real use: this is a
flood guard, not a quota, and a member should never meet one by playing
normally. Edge-side rate limiting in Cloudflare remains a worthwhile complement
for the unauthenticated surface.

### `/api/health` and other small hardening
The public health endpoint handed the exact SemVer (which, against public
release notes, says precisely what is unpatched), org-size and activity metrics,
and `data.error` — a raw `str(exc)` that can carry absolute filesystem paths — to
anonymous callers. It's now a bare `{"ok": true}` pre-auth, keeping the compose
healthcheck working, with the diagnostics behind a session. `data.error` records
the exception class; the full text goes to the log. `.dockerignore` now excludes
`.DS_Store` and `.impeccable`.

---

## Verified sound — don't re-review these

Chased end to end, in several cases empirically:

- **Authorization.** No IDOR found. `ensure_owns` is correct (including
  admin-only ownerless legacy rows); events, resource manager, marketplace,
  survey zones, LFG, warnings, trade favorites and templates all carry correct
  owner-or-admin checks. `_my_contribution` correctly makes withdrawal
  *contributor*-or-admin rather than goal-owner.
- **Privilege escalation.** None. `current_user` recomputes `is_admin` against
  the live admin set rather than trusting the cookie; `ProfileIn` has no
  `is_admin` field (no mass assignment); all `/api/admin/*` are gated; the
  `extra_admin_ids` writer validates snowflakes and guards last-admin lockout.
- **Auth-gate path matching.** No bypass. `request.url.path` is the same value
  the router matches, so gate and router can't disagree. Case variants,
  `//api/…`, `/api/../…`, trailing slashes, `%2f`, and null bytes all fail
  closed; the exemptions are exact and method-scoped.
- **OAuth.** State is `secrets.token_urlsafe(24)` and compared with an early
  reject; there is no `next`/`redirect` parameter anywhere, so no open redirect.
  The `SameSite=None` state cookie is HttpOnly+Secure, `/auth`-scoped, 600s, and
  carries only a nonce. The Discord **access token is never stored in the
  session**. Guild/role checks can't be skipped on unexpected data.
- **CSRF.** No CORS middleware. JSON routes reject every content type a
  cross-site form can produce (verified: `text/plain`, form-encoded, and
  multipart all 422). The one multipart route is admin-gated and POST-only. The
  only state-changing GET increments an anonymous listing view counter.
- **SQL injection.** None. Every f-string-composed statement interpolates
  code-controlled identifiers (allowlists, fixed if-chains, generated `?`
  placeholders); all values are bound parameters.
- **Path traversal / uploads / deserialization / SSRF.** Logo extension comes
  from a closed map, not the filename, with a magic-byte sniff and size cap; no
  pickle/yaml/eval/XML anywhere; webhook URLs are regex-anchored to Discord
  hosts, the update repo is regex-validated at import, and the Strata sync is a
  list-argv subprocess.
- **Discord webhooks.** `allowed_mentions: {"parse": [], "users": [...]}` makes
  `@everyone`/`@here`/role injection impossible regardless of message content.
  `notify_opt_out` is honored on every directed path.
- **WebSocket.** Session-only (a watcher token cannot open one), origin-checked,
  with a per-member tab cap; presence broadcasts are coalesced at 1 Hz, so
  position spam is not an amplifier.
- **Handle binding.** Trust-on-first-use with a one-way anti-hijack guard,
  creation rate-limited, refusal surfaced to the member, admin unbind as the only
  way to move a binding.
- **Container/CI.** Runs as a non-root user; port bound to `127.0.0.1` with an
  outbound-only tunnel; `.env.example` ships no real values and no weak defaults;
  `COOKIE_SECURE` fails *closed* on unrecognized values; the release workflow
  already avoids the `workflow_run` "pwn request" hole.
- **The watcher does not touch the game.** Every Win32 call was enumerated:
  foreground-window and monitor geometry, the exe *path*, one passive
  `GetAsyncKeyState`, and topmost/click-through on *its own* window. There is no
  `ReadProcessMemory`, no `SetWindowsHookEx`, no `SendInput`, no DLL injection,
  no graphics hooking. It is a lower risk class than the Discord overlay. It also
  refuses HTTP redirects outright so the bearer token can never be replayed to
  another host, and verifies TLS everywhere.

---

## Open — recommended next, in order

1. **No audit log.** `mined_by`, `price_reports.reporter` and the depletion
   poster are the only "who did this" breadcrumbs, and the first is
   last-writer-wins. After a mass action an admin can detect damage but not
   attribute or bulk-revert it. One append-only `audit(ts, actor, action,
   target)` table written from the mutating handlers would make most of the
   remaining insider findings recoverable rather than merely survivable.
2. **Shared-write surfaces with no per-member cap and no undo.** `POST
   /api/catalog` has no cap and no delete anywhere in `db.py` — inserted items
   are permanent in every picker. `POST /api/observations/{id}/mine` and `POST
   /api/survey/depleted` are intentionally org-shared, but a loop over the
   observation list can drop every resource node off the map in minutes, and
   the only reversal for depletion is an admin wipe of *everyone's* reports.
   Custom POIs and public QT markers are likewise uncapped.
3. **Token lifecycle.** Watcher tokens have no expiry and no rotation;
   `last_used` is bumped in memory only and never persisted, so after a restart
   an admin can't tell a live token from a forgotten one; there's no admin view
   of who holds tokens; and every `POST /download/watcher` mints another one.
   Suggested: an `expires_at` default, throttled `last_used` persistence, and
   `GET /api/admin/tokens` (revocation already supports the admin case).
4. **Marketplace publishes the Discord↔handle link** (partly addressed). Every
   listing carries `seller_id`, `seller_handle` and `handle_verified`, ignoring
   `directory_opt_out` — the very link `/api/handles` is projected to hide. On
   review this is largely inherent: a buyer has to know who to meet in-game, so
   a listing can't be both useful and anonymous, and members share a Discord
   guild where snowflakes aren't secret anyway. The opt-out UI now says so
   plainly. What's still worth doing is dropping the raw `seller_id` from the
   public projections (the SPA only uses it for filter-by-seller, and the server
   already computes `is_seller`), so the board isn't a bulk-exportable table.
5. **Manifest posting is an unmetered Discord firehose.** An organizer can post
   their own event's manifest in a loop; 60 groups with 500-char notes chunk to
   ~25 messages per call. Cap chunks and add a cooldown.
6. **Dependencies float.** `requirements.txt` has no upper bounds and no
   lockfile, so two builds of the same commit can differ. The declared floor
   `python-multipart>=0.0.7` permits versions vulnerable to CVE-2024-53981,
   reachable via the logo upload — not exploitable today (the resolver picks a
   fixed version) but worth pinning.

### Deliberately not done

- **`TrustedHostMiddleware` ("host-header pin").** `CLAUDE.md` claimed this
  guardrail existed; it never did, and the claim has been corrected rather than
  the middleware added. The app binds to `127.0.0.1` behind a tunnel with a
  fixed hostname, and the one place a spoofed host mattered — baking a server URL
  into a member's watcher bundle — already prefers `SC_NAV_PUBLIC_URL`. A
  misconfigured allowed-hosts list that 400s the whole org is a larger
  operational risk than the residual threat.
- **A `.join()`-escaping lint.** Suggested by the audit, but it flags 29 sites
  of which 27 join pre-escaped HTML fragments. The convention is documented in
  `CLAUDE.md` instead.

---

## Residual risk worth stating plainly

**The org server operator is fully trusted, by construction.** They control the
watcher bundle members download, and there is no signing or integrity check —
so a compromised server can ship malicious code to every member at download
time. There is no runtime channel to exploit afterward (the watcher never
self-updates, downloads code, or evals), so a member's exposure is bounded to
the moment they download. Publishing per-release bundle hashes would let a
cautious member verify against the public repo.

**Members trust each other more than the software enforces.** The shared-write
surfaces in item 3 above are deliberate product decisions — an org tool where
everyone can mark a rock mined-out is more useful than one where they can't. The
mitigation for those is social and administrative, which is exactly why items 1
and 2 (rate limiting and an audit trail) matter more than adding permissions.
