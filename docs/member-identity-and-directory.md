# Member identity, primary handle & directory — design

**Status:** designed 2026-06-29; **ALL FOUR STEPS BUILT 2026-06-29** (uncommitted,
needs /deploy). Verified via smoke tests over a temp DB (members CRUD + listing
seller_handle round-trip) and in-process app logic (owns_handle live verification,
_resolve_member_name precedence, _member_identity defaulting, directory rows);
nav_core suite still 144 green, SPA JS syntax-checked. This doc captures the
decisions agreed before any code. It closes an **identity inconsistency** across
the apps and adds one new admin-only Intel feature. It touches three existing apps
([Marketplace](marketplace.md), the cargo
[leaderboard](cargo-hauling-planner.md), and Org Intel / POIs) and introduces a
shared identity primitive the whole SPA can lean on.

## The problem

Identity in the app today is split three ways, and the split leaks into the UX:

- **Discord user id** is the canonical, OAuth-verified, guild-gated account key.
  It controls ownership/auth everywhere it matters.
- The **in-game (RSI) handle** is bound to a Discord id **trust-on-first-use**,
  the first time an authenticated watcher posts a position carrying that handle
  (`HandleRegistry.register`, `app.py`). One Discord account can own **several**
  handles — `handles.player_ids_for(discord_id)` already returns a **set**.
- **Discord display name / username / avatar** are captured at login
  (`auth.py` → `global_name`, `username`, `avatar`) but **only live in the
  session cookie** (`request.session["user"]`). They are *not* persisted; they're
  opportunistically copied onto cargo runs and watcher tokens. The guild
  `member` object is fetched at login but everything except `roles` — including
  the org-specific **`nick`** — is thrown away.

The visible symptoms:

1. **Marketplace shows no handle.** Listings key on `seller_id = discord_id` and
   surface nothing about *who to meet in-game* — the entire point of the
   in-game handoff. POIs and the leaderboard show handles; the marketplace
   doesn't. (Inconsistency the user flagged.)
2. **The leaderboard guesses a name.** `_resolve_member_name` improvises a
   display name through a 4-step fallback (stamped name → token name → *first*
   bound handle → id stub). "First bound handle" is arbitrary for multi-handle
   accounts — the same ambiguity, quietly.
3. **No notion of *the* handle for an account.** Nothing records which of a
   member's handles is their primary, so every surface picks differently.

## The fix, in three parts

The root cause is that there's no persistent **member record** and no concept of
a **primary handle**. Introduce both once; the marketplace gap and the
leaderboard guessing both dissolve, and the new directory falls out for free.

### Decisions

- **Ownership/auth stays on `discord_id` everywhere.** The handle is for
  *display and meetup*, never for permissions. The marketplace does **not**
  switch its key to the handle — it gains a denormalized display handle, exactly
  mirroring `custom_pois.owner_id` + `owner_handle`.
- **Marketplace handles may be unverified.** The marketplace is the one app that
  doesn't require a watcher; forcing a watcher-bound handle to list would add a
  hard dependency to the feature least able to absorb it (adoption ceiling). So:
  optional handle, picker of verified handles **plus free-text fallback**, the
  free-text one flagged `unverified` until a watcher capture binds it. The
  unverified chip doubles as a gentle nudge to install the watcher.
- **Verification is computed live, never stored.** A listing's handle is
  "verified" iff it's *currently* bound to the seller's `discord_id` in the
  registry. So a handle typed before the seller ran the watcher **auto-upgrades**
  to verified the moment a capture binds it — self-healing, no flag to flip.
- **Directory is admin-only; opt-out is cosmetic.** Admins can already derive the
  Discord↔handle link from raw POI/handle data, so an opt-out can't truly hide it
  from a determined admin. Members get an opt-out that hides them from any
  member-facing surface; **admins always see everyone**, and the opt-out UI says
  so plainly. The directory feature itself is admin-gated and off until enabled.

## Data model

**New `members` table** — the shared primitive. Upserted on every login (all
fields are already available at OAuth time):

```sql
CREATE TABLE IF NOT EXISTS members (
  discord_id     TEXT PRIMARY KEY,
  username       TEXT,              -- discord username
  display_name   TEXT,              -- global_name
  guild_nick     TEXT,              -- member.nick (currently discarded in auth.py)
  primary_handle TEXT,              -- chosen in-game handle for display
  directory_opt_out INTEGER NOT NULL DEFAULT 0,
  first_login TEXT, last_login TEXT
);
```

**`listings` migration** — add one column; **no** `verified` column (computed
live):

```sql
ALTER TABLE listings ADD COLUMN seller_handle TEXT;   -- via _ensure_column
```

Both land through the existing idempotent migration block in `db.py`
(`CREATE TABLE IF NOT EXISTS` + `_ensure_column`).

## Server changes

1. **`auth.py`** — stop discarding `member.nick`; include it in the returned
   profile dict.
2. **Login handler** (around `request.session["user"] = profile`) —
   `db.upsert_member(profile)`, stamping `first_login` once and `last_login`
   each time.
3. **`_resolve_member_name`** — collapse the 4-step guess to a lookup:
   `members.guild_nick or display_name or primary_handle or "Member ####"`.
   Pure refactor; the leaderboard/stats labels keep working.
4. **Primary handle** — `PUT /api/me/primary-handle` (value must be one of the
   caller's verified handles). Default to the most-recently-seen bound handle
   when unset. Expose current value in `/api/me`.
5. **Marketplace create + serialize:**
   - Create body accepts `seller_handle`; default to `primary_handle`; free-text
     allowed.
   - `_listing_card` adds `seller_handle` and a **live** `handle_verified` =
     `seller_handle in {handle_for(pid) for pid in player_ids_for(seller_id)}`.
6. **Directory** — `GET /api/intel/directory`, `require_admin`, returns each
   member joined to their handle set + `opt_out` flag. Admin view excludes
   nothing; `opt_out` only filters the (not-yet-built) member-facing surfaces.
7. **Opt-out toggle** — `PUT /api/me/directory-opt-out`.

## UI

- **Marketplace** — handle picker on the create form (verified handles +
  free-text). Listing card and the dual-confirm handoff show `seller_handle`
  with an `unverified` chip when `handle_verified` is false. The buyer's handle
  surfaces on the card once a deal goes pending, so **both** sides know who to
  look for in-game.
- **Settings / account** — primary-handle picker + "Hide me from the member
  directory" toggle, captioned honestly ("org admins can still see this").
- **Intel** — admin-only **Directory** tab: Discord nick / display name →
  handle(s); opted-out rows visibly flagged.

## Privacy

- Directory is **admin-only** and **off** until an admin enables it.
- Per-member **`directory_opt_out`** hides the member from any member-facing
  view. It is **cosmetic with respect to admins** — admins always see everyone,
  by design and by disclosure. The Discord↔handle link already exists in the
  data (the `handles` registry); this feature *surfaces* it, it doesn't *create*
  it.
- This is a closed, single-guild, Discord-gated app — the same trust boundary as
  every other app. No data leaves that boundary.

## Build order

Each step is independently shippable and test-gated.

1. **`members` table + login upsert + `_resolve_member_name` rewrite.** Pure
   refactor, no UX change — ship and verify leaderboard/stats labels are intact.
2. **Primary handle** — endpoint + settings picker.
3. **Marketplace `seller_handle`** — the original consistency fix. Depends on 1 & 2.
4. **Directory + opt-out** — additive admin Intel feature. Depends on 1.

Steps 1–3 deliver the consistency fix; step 4 is the new feature and can land
separately.

## Out of scope

- Handle ownership *verification* beyond trust-on-first-use (no RSI-account
  proof flow).
- Exposing the Discord↔handle cross-walk to non-admins (explicitly rejected).
- Avatars in the directory (we capture the avatar hash but rendering Discord
  CDN images is a separate call).
