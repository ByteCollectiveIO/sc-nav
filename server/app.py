"""SC Nav server.

Receives positions from the Windows clipboard watcher, computes navigation
state against the containers/poi dataset, and pushes live updates to browser
clients over WebSocket.

Run:  uvicorn app:app --host 0.0.0.0 --port 8765
Data: ../poi by default, override with SC_NAV_DATA=/path/to/poi
"""

import asyncio
import hashlib
import io
import json
import os
import re
import secrets
import time
import traceback
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

import auth
import db
import event_taxonomy
import nav_core
from version import __version__ as APP_VERSION

DATA_DIR = Path(os.environ.get("SC_NAV_DATA", Path(__file__).parent.parent / "poi"))
STATIC_DIR = Path(__file__).parent / "static"
# Admin-uploaded guild logo lives on the writable /data volume (not the static
# dir, which is baked into the image and lost on rebuild). Served by a route,
# not the StaticFiles mount. PNG/JPG/WebP only — no SVG (script-injection risk).
BRANDING_DIR = DATA_DIR / "branding"
_LOGO_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
_LOGO_MAX_BYTES = 2 * 1024 * 1024


def _sniff_image(data: bytes, ext: str) -> bool:
    """True when the leading bytes match the magic number for `ext`. Guards the
    upload against a mislabeled (or polyglot) file slipping onto the volume."""
    if ext == "png":
        return data[:8] == b"\x89PNG\r\n\x1a\n"
    if ext == "jpg":
        return data[:3] == b"\xff\xd8\xff"
    if ext == "webp":
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return False
# Watcher source for the Setup-page download. In the Docker image the files are
# copied to server/watcher_src (see Dockerfile); in a dev checkout they live in
# the repo's ../watcher. First existing wins.
WATCHER_DIR = next(
    (p for p in (Path(__file__).parent / "watcher_src",
                 Path(__file__).parent.parent / "watcher") if p.is_dir()),
    None,
)
# Files bundled into the download (everything else — tests, __pycache__, any
# stale watcher_config.json — is left out).
WATCHER_BUNDLE_FILES = ("sc_nav_watcher.py", "run_watcher.bat", "README.md")

# Live dataset endpoints (the files in DATA_DIR act as the offline cache).
OC_URL = os.environ.get("SC_NAV_OC_URL", "https://starmap.space/api/v3/oc/index.php")
POI_URL = os.environ.get("SC_NAV_POI_URL", "https://starmap.space/api/v3/pois/index.php")
COMMODITIES_URL = os.environ.get("SC_NAV_COMMODITIES_URL", "https://api.uexcorp.uk/2.0/commodities")
SHIPS_URL = os.environ.get("SC_NAV_SHIPS_URL", "https://api.uexcorp.uk/2.0/vehicles")
OFFLINE = os.environ.get("SC_NAV_OFFLINE") == "1"

# Canonical public URL (e.g. https://nav.bytecollective.io). When set it is the
# only address baked into the watcher download bundle, so a spoofed Host /
# X-Forwarded-Host header can't redirect a member's watcher (and its token).
PUBLIC_BASE_URL = os.environ.get("SC_NAV_PUBLIC_URL", "").rstrip("/")

data_info = {"source": None, "fetched_at": None, "error": None}


def _fetch_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "sc-nav/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def starmap_pois_enabled() -> bool:
    """Whether to load starmap.space's POI catalog. Defaults OFF: a new org
    starts from a blank POI database (their own custom POIs only) and an admin
    opts in. Once opted in, the flag persists, so every restart and /api/refresh
    re-fetches the latest catalog automatically. Celestial bodies (the container
    catalog) are always loaded — the nav math needs them."""
    return db.get_setting("starmap_pois_enabled", "0") == "1"


def member_role_id() -> str:
    """Discord role a user must hold (besides guild membership) to sign in.
    Empty = any guild member is allowed. DB-backed + admin-editable, seeded by
    the ORG_MEMBER_ROLE_ID env default. Admins (ADMIN_IDS) bypass this check."""
    return db.get_setting("member_role_id", auth.MEMBER_ROLE_ID) or ""


def extra_admin_ids() -> list[str]:
    """Discord ids granted admin from the UI (DB-backed, admin-editable).
    Additive to the env `ADMIN_IDS` root admins, which the UI can't touch."""
    raw = db.get_setting("extra_admin_ids", "") or ""
    return [x.strip() for x in raw.split(",") if x.strip()]


def admin_ids() -> set[str]:
    """Effective admin set: the immutable env root admins (auth.ADMIN_IDS)
    unioned with the DB-backed list. Keeping the env admins as a floor means a
    wrecked DB or a bad UI edit can never lock everyone out, and resolving this
    live (rather than trusting the login-time flag) makes a grant/revoke take
    effect on the member's very next request."""
    return auth.ADMIN_IDS | set(extra_admin_ids())


def obs_fresh_window_h() -> int:
    """How many hours an observation stays "fresh" for the map markers + NEARBY
    list. Resource nodes and fauna are ephemeral (SC respawns them), so stale
    sightings are hidden from those actionable views by default. DB-backed +
    admin-editable so it can be tuned to SC's respawn cadence without a redeploy;
    the heatmap still aggregates all sightings regardless of this."""
    try:
        return max(1, int(db.get_setting("obs_fresh_window_h", "48")))
    except (TypeError, ValueError):
        return 48


def load_nav_data() -> nav_core.NavData:
    """Fetch live data from starmap.space; fall back to the on-disk cache.

    A successful fetch refreshes the cache files, so the newest good dataset
    survives restarts and network outages. The POI catalog is skipped when the
    org has opted out (containers are always loaded).
    """
    want_pois = starmap_pois_enabled()
    if not OFFLINE:
        try:
            oc_raw = _fetch_json(OC_URL)
            poi_raw = _fetch_json(POI_URL) if want_pois else []
            if len(oc_raw) < 50 or (want_pois and len(poi_raw) < 100):
                raise ValueError(
                    f"suspiciously small dataset ({len(oc_raw)} containers, "
                    f"{len(poi_raw)} pois) — keeping cache"
                )
            fresh = nav_core.parse_data(oc_raw, poi_raw)
            try:
                (DATA_DIR / "containers.json").write_text(json.dumps(oc_raw))
                if want_pois:
                    (DATA_DIR / "poi.json").write_text(json.dumps(poi_raw))
            except OSError as exc:
                print(f"[sc-nav] cache write failed (continuing): {exc}")
            data_info.update(
                source="live",
                fetched_at=datetime.now(timezone.utc).isoformat(),
                error=None,
            )
            return fresh
        except Exception as exc:
            data_info["error"] = str(exc)
            print(f"[sc-nav] live fetch failed, using cached data: {exc}")
    data_info["source"] = "offline" if OFFLINE else "cache"
    oc_raw = json.loads((DATA_DIR / "containers.json").read_text())
    poi_raw = json.loads((DATA_DIR / "poi.json").read_text()) if want_pois else []
    return nav_core.parse_data(oc_raw, poi_raw)


COMMODITIES_FILE = DATA_DIR / "commodities.json"  # cached uexcorp commodities
SHIPS_FILE = DATA_DIR / "ships.json"               # cached uexcorp vehicles
DB_FILE = DATA_DIR / "sc_nav.db"                   # user-contributed data (Phase 2)


def _load_json_list(path: Path) -> list[dict]:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []


def _save_json_list(path: Path, items: list[dict]) -> None:
    path.write_text(json.dumps(items, indent=1))


def load_biomes() -> dict:
    """Normalize server/biomes.json into lookups for the biome datalist:
      by_body[system][body] -> [biome names]   (system/body lowercased)
      by_system[system]      -> [union of the system's biome names]
      all                    -> [every biome name]
    Source shape: star_systems -> system -> planets -> planet ->
      { "biomes": [{biome_name,...}], "moons": { moon: [{biome_name,...}] } }.
    Both planets and moons land in by_body; the UI narrows to the player's body
    and falls back body -> system -> all."""
    out = {"by_body": {}, "by_system": {}, "all": []}
    try:
        raw = json.loads((Path(__file__).parent / "biomes.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[sc-nav] biomes load failed: {exc}")
        return out

    def names_of(entries):
        return sorted({e["biome_name"] for e in (entries or []) if e.get("biome_name")})

    all_names = set()
    for system, sysv in (raw.get("star_systems") or {}).items():
        s = system.lower()
        sys_names = set()
        bodies = out["by_body"].setdefault(s, {})
        for planet, pv in ((sysv or {}).get("planets") or {}).items():
            pv = pv or {}
            pnames = names_of(pv.get("biomes"))
            if pnames:
                bodies[planet.lower()] = pnames
                sys_names.update(pnames)
            for moon, entries in (pv.get("moons") or {}).items():
                mnames = names_of(entries)
                if mnames:
                    bodies[moon.lower()] = mnames
                    sys_names.update(mnames)
        out["by_system"][s] = sorted(sys_names)
        all_names.update(sys_names)
    out["all"] = sorted(all_names)
    return out


def load_fauna_names() -> list[str]:
    """Curated fauna/species names for the Add Fauna datalist. A committed
    reference list shipped with the server (server/fauna.json)."""
    try:
        names = json.loads((Path(__file__).parent / "fauna.json").read_text())
        return sorted({n for n in names if n}, key=str.lower)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[sc-nav] fauna list load failed: {exc}")
        return []


def load_raw_commodity_names() -> list[str]:
    """Sorted names of raw (is_raw==1) commodities from uexcorp, used to
    populate the ore datalist. Fetched live with an on-disk cache fallback,
    mirroring the dataset loader."""
    rows = None
    if not OFFLINE:
        try:
            resp = _fetch_json(COMMODITIES_URL, timeout=15)
            rows = resp.get("data") if isinstance(resp, dict) else resp
            if rows:
                _save_json_list(COMMODITIES_FILE, rows)
        except Exception as exc:
            print(f"[sc-nav] commodities fetch failed, using cache: {exc}")
    if not rows:
        rows = _load_json_list(COMMODITIES_FILE)
    names = {r["name"] for r in rows if r.get("is_raw") in (1, "1", True) and r.get("name")}
    return sorted(names)


def load_ships() -> list[dict]:
    """Cargo-capable spaceships from the uexcorp vehicles feed (name + stated
    SCU + company), for the cargo-planner ship picker. Fetched live with an
    on-disk cache fallback, mirroring the commodities loader. The full rows are
    cached (not just the trimmed view) so the deferred quantum-drive/range work
    can reuse fuel + capability fields without a second feed."""
    rows = None
    if not OFFLINE:
        try:
            resp = _fetch_json(SHIPS_URL, timeout=15)
            rows = resp.get("data") if isinstance(resp, dict) else resp
            if rows:
                _save_json_list(SHIPS_FILE, rows)
        except Exception as exc:
            print(f"[sc-nav] ships fetch failed, using cache: {exc}")
    if not rows:
        rows = _load_json_list(SHIPS_FILE)

    def to_scu(v):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return 0

    ships = [
        # name_full includes the manufacturer ("Argo MOLE"), which is how players
        # search; fall back to the bare model name.
        {"name": r.get("name_full") or r["name"], "company": r.get("company_name"),
         "scu": to_scu(r.get("scu"))}
        for r in rows
        if (r.get("name_full") or r.get("name")) and r.get("is_spaceship") in (1, "1", True)
        and to_scu(r.get("scu")) > 0
    ]
    ships.sort(key=lambda s: s["name"].lower())
    return ships


def load_commodity_names() -> list[str]:
    """All commodity names from uexcorp (every kind, not just is_raw ores) for
    the cargo-planner commodity picker — hauling contracts carry Medical
    Supplies, Processed Food, etc., not only raw ores. Same fetch + on-disk
    cache as the ore loader."""
    rows = None
    if not OFFLINE:
        try:
            resp = _fetch_json(COMMODITIES_URL, timeout=15)
            rows = resp.get("data") if isinstance(resp, dict) else resp
            if rows:
                _save_json_list(COMMODITIES_FILE, rows)
        except Exception as exc:
            print(f"[sc-nav] commodities fetch failed, using cache: {exc}")
    if not rows:
        rows = _load_json_list(COMMODITIES_FILE)
    return sorted({r["name"] for r in rows if r.get("name")})


def load_harvestable_names() -> list[str]:
    """Sorted names of harvestable flora/natural commodities (uexcorp
    kind=="Natural" and is_harvestable==1) for the Add Fauna & Harvestables
    datalist. Reuses the same commodities cache as the ore loader."""
    rows = None
    if not OFFLINE:
        try:
            resp = _fetch_json(COMMODITIES_URL, timeout=15)
            rows = resp.get("data") if isinstance(resp, dict) else resp
            if rows:
                _save_json_list(COMMODITIES_FILE, rows)
        except Exception as exc:
            print(f"[sc-nav] commodities fetch failed, using cache: {exc}")
    if not rows:
        rows = _load_json_list(COMMODITIES_FILE)
    names = {
        r["name"] for r in rows
        if r.get("kind") == "Natural"
        and r.get("is_harvestable") in (1, "1", True)
        and r.get("name")
    }
    return sorted(names)


class HandleRegistry:
    """Maps in-game handles to stable assigned PlayerIDs (DB-backed, cached
    in memory).

    The PlayerID (not the raw handle) is the key attached to contributions, so
    a character rename keeps a player's history intact."""

    def __init__(self):
        self.by_handle = {h["handle"]: h for h in db.all_handles()}

    def register(self, handle: str, discord_id: str | None = None) -> dict:
        handle = handle.strip()
        now = datetime.now(timezone.utc).isoformat()
        entry = self.by_handle.get(handle)
        if entry is None:
            next_id = max((e["player_id"] for e in self.by_handle.values()), default=0) + 1
            entry = {"player_id": next_id, "handle": handle, "first_seen": now,
                     "last_seen": now, "discord_id": discord_id}
            self.by_handle[handle] = entry
            # Persist only when a genuinely new handle appears — this runs on the
            # position hot path (every /showlocation), so we don't write per
            # sample just to bump last_seen (kept in memory).
            try:
                db.upsert_handle(entry)
            except Exception as exc:
                print(f"[sc-nav] handle registry save failed: {exc}")
        else:
            entry["last_seen"] = now  # in-memory only; not worth a write per position
            # Bind ownership the first time we learn who is posting this handle
            # (the watcher's token resolves to a Discord id). Persist that once.
            if discord_id and entry.get("discord_id") != discord_id:
                entry["discord_id"] = discord_id
                try:
                    db.upsert_handle(entry)
                except Exception as exc:
                    print(f"[sc-nav] handle owner bind failed: {exc}")
        return entry

    def player_ids_for(self, discord_id: str) -> set[int]:
        """Every PlayerID owned by a Discord member (alts/renames included).
        Used to scope deletes to a member's own contributions."""
        return {e["player_id"] for e in self.by_handle.values()
                if e.get("discord_id") == discord_id}

    def handle_for(self, player_id: int) -> str | None:
        """Current handle for a PlayerID (latest known after any rename)."""
        for e in self.by_handle.values():
            if e["player_id"] == player_id:
                return e["handle"]
        return None

    def list(self) -> list[dict]:
        return sorted(self.by_handle.values(), key=lambda e: e["handle"].lower())


class TokenStore:
    """Per-user watcher tokens (DB-backed, cached in memory). The headless
    watcher can't do OAuth, so an org member mints a token in the web UI and the
    watcher sends it as a bearer token. Only the hash is persisted; admin status
    is resolved live."""

    def __init__(self):
        self.items = db.all_tokens()

    @staticmethod
    def _hash(raw: str) -> str:
        return hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def _public(t: dict) -> dict:
        return {"id": t["id"], "label": t["label"],
                "created": t["created"], "last_used": t["last_used"]}

    def resolve(self, raw: str) -> dict | None:
        """A raw bearer token -> the owning member (id/display_name/is_admin),
        or None. last_used is bumped in memory only (avoids a write per position
        on the watcher heartbeat)."""
        h = self._hash(raw)
        for t in self.items:
            if secrets.compare_digest(t["hash"], h):
                t["last_used"] = datetime.now(timezone.utc).isoformat()
                return {
                    "id": t["discord_id"],
                    "display_name": t.get("display_name"),
                    "is_admin": t["discord_id"] in admin_ids(),
                }
        return None

    def mint(self, discord_id: str, display_name: str, label: str) -> tuple[str, dict]:
        raw = secrets.token_urlsafe(32)
        entry = {
            "id": secrets.token_hex(8),
            "hash": self._hash(raw),
            "discord_id": discord_id,
            "display_name": display_name,
            "label": (label or "watcher").strip()[:60],
            "created": datetime.now(timezone.utc).isoformat(),
            "last_used": None,
        }
        db.add_token(entry)
        self.items.append(entry)
        return raw, self._public(entry)

    def list_for(self, discord_id: str) -> list[dict]:
        return [self._public(t) for t in self.items if t["discord_id"] == discord_id]

    def revoke(self, token_id: str, discord_id: str, is_admin: bool) -> bool:
        for i, t in enumerate(self.items):
            if t["id"] == token_id and (is_admin or t["discord_id"] == discord_id):
                db.delete_token(token_id)
                self.items.pop(i)
                return True
        return False


def merge_all_observations(target_nav) -> None:
    nav_core.merge_observations(target_nav, db.list_observations())


app = FastAPI(title="SC Nav")


# Auth gate: every /api/* call needs a logged-in org member (session) or a valid
# watcher token; the SPA shell, /auth/* and /api/health stay open. Registered
# BEFORE SessionMiddleware below so that (being the inner layer) it runs after
# the session has been loaded from the cookie.
@app.middleware("http")
async def auth_gate(request: Request, call_next):
    path = request.url.path
    # The org logo is shown on the pre-auth login splash, so reading it is public
    # (GET only — POST/DELETE still fall through to the admin-gated route).
    if (not path.startswith("/api/") or path == "/api/health"
            or (path == "/api/org-logo" and request.method == "GET")):
        return await call_next(request)
    if request.session.get("user") or token_user(request):
        return await call_next(request)
    return JSONResponse({"detail": "not authenticated"}, status_code=401)


# Signed session cookie (Discord login state). The secret must be stable across
# restarts so sessions survive a redeploy; a random fallback keeps dev working.
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() == "true"
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET") or secrets.token_hex(32),
    https_only=COOKIE_SECURE,
    same_site="lax",
    max_age=8 * 3600,
)

# Defense-in-depth response headers on every response (static + API). The SPA
# ships one inline <script>, authorized by a per-request nonce so script-src can
# drop 'unsafe-inline' entirely — an injected <script> (or img onerror, etc.)
# carries no valid nonce and won't execute, so a future escaping slip can't
# become script execution. Inline STYLE attributes are pervasive (style="width:
# ..%") and nonces don't cover them, so style-src keeps 'unsafe-inline' (style
# injection is far lower risk). No 'unsafe-eval', no external script/object
# sources, framing denied (clickjacking). Output-escaping is still the primary
# XSS defense; the nonce makes the CSP a real backstop rather than a formality.
def _csp(nonce: str) -> str:
    return (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "font-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    )


@app.middleware("http")
async def security_headers(request: Request, call_next):
    # Fresh, unguessable nonce per request; the index route reads it back off
    # request.state to stamp the inline <script> with a matching nonce.
    nonce = secrets.token_urlsafe(16)
    request.state.csp_nonce = nonce
    resp = await call_next(request)
    resp.headers.setdefault("Content-Security-Policy", _csp(nonce))
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


db.init(DB_FILE)
db.import_legacy_json(DATA_DIR, nav_core.OBSERVATION_CATEGORIES)  # one-time JSON -> SQLite

nav = load_nav_data()
handles = HandleRegistry()
tokens = TokenStore()
raw_commodity_names = load_raw_commodity_names()
commodity_names = load_commodity_names()
harvestable_names = load_harvestable_names()
ships = load_ships()
fauna_names = load_fauna_names()
biomes = load_biomes()
nav_core.merge_custom_pois(nav, db.list_custom_pois())
merge_all_observations(nav)
nav_core.assign_qt_markers(nav)


# --- auth dependencies (defined before the endpoints that use them) ---------
def current_user(request: Request) -> dict | None:
    """The signed-in member. `is_admin` is recomputed against the live admin set
    (not read from the login-time session value) so a UI grant/revoke applies on
    the member's next request rather than only at their next sign-in."""
    user = request.session.get("user")
    if user is None:
        return None
    return {**user, "is_admin": user["id"] in admin_ids()}


def token_user(request: Request) -> dict | None:
    """The org member behind a `Authorization: Bearer <watcher token>`, or None."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    return tokens.resolve(header[7:].strip())


def require_session(request: Request) -> dict:
    """Dependency: a logged-in org member, else 401."""
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="not authenticated")
    return user


def require_admin(user: dict = Depends(require_session)) -> dict:
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="admin only")
    return user


def ensure_owns(user: dict, owner_id: int | None) -> None:
    """A member may delete only their own contributions — any PlayerID bound to
    their Discord id (alts/renames included). Admins delete anything.
    Ownerless legacy records (owner_id is None) are admin-only."""
    if user.get("is_admin"):
        return
    if owner_id is not None and owner_id in handles.player_ids_for(user["id"]):
        return
    raise HTTPException(status_code=403, detail="you can only delete your own contributions")


def require_user(request: Request) -> dict:
    """A logged-in member (browser session) OR a watcher token — used where
    either client is valid (e.g. posting a position)."""
    user = current_user(request) or token_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="not authenticated")
    return user


def viewer_owner_ids(user: dict | None) -> frozenset[int]:
    """The PlayerIDs a viewer owns (alts/renames included) — the key that lets
    them see their own private POIs. Empty for an anonymous viewer, so they see
    only shared POIs."""
    if not user:
        return frozenset()
    return frozenset(handles.player_ids_for(user["id"]))


# Field length caps. User-supplied free text is bounded at the schema edge so a
# member (or a watcher token) can't persist multi-MB strings that then get
# fanned out to every connected tab over the WebSocket. Generous vs. real use,
# tight vs. abuse.
_NAME_MAX = 120
_TYPE_MAX = 60
_NOTE_MAX = 500
_TERM_MAX = 80     # ore / species / harvestable name
_BIOME_MAX = 60
_HANDLE_MAX = 64
_BAND_MAX = 16
_RAW_MAX = 512
_META_MAX = 64     # client_time / source / discord-id-ish small fields
_SHARD_MAX = 64    # SC shard id, e.g. "pub_use1b_12030094_130"
_LABEL_MAX = 60
_COMMODITY_MAX = 80
_PKG_ID_MAX = 64
_CONTRACT_MAX = 60   # player's free-text contract/group label
_MAX_PACKAGES = 60   # one hauling run rarely exceeds a handful of contracts
_DESC_MAX = 2000     # event description free-text
_MAX_ROSTER_ROLES = 20   # target roles on one event
_MAX_SIGNUP_ROLES = 10   # roles one member claims on a signup
_MAX_PLAYERS = 10_000    # sanity cap on min/max player counts
# How far back the upcoming board reaches to keep live/ongoing events visible
# (their start has passed but they aren't finished); finished ones are then filtered
# out by derived phase. Generous enough to cover most op lengths + the live grace.
_EVENT_BOARD_LOOKBACK_MIN = 24 * 60


class PositionIn(BaseModel):
    x: float
    y: float
    z: float
    raw: str | None = Field(default=None, max_length=_RAW_MAX)
    client_time: str | None = Field(default=None, max_length=_META_MAX)
    source: str | None = Field(default=None, max_length=_META_MAX)
    handle: str | None = Field(default=None, max_length=_HANDLE_MAX)
    # SC shard id from Game.log (watcher). Stamped onto captures and broadcast so
    # clients can tell which ephemeral nodes / teammates are on their own server.
    shard: str | None = Field(default=None, max_length=_SHARD_MAX)


class DestinationIn(BaseModel):
    poi_id: int


class CaptureIn(BaseModel):
    name: str = Field(max_length=_NAME_MAX)
    type: str = Field(default="Custom", max_length=_TYPE_MAX)
    qt_marker: bool = False   # record as a jumpable QT marker (e.g. an OM)
    private: bool = False     # owner-only POI; hidden from the rest of the org
    note: str = Field(default="", max_length=_NOTE_MAX)   # optional free-text context


class NodeCaptureIn(BaseModel):
    ore: str = Field(max_length=_TERM_MAX)
    band: int | str | None = None   # 1-8, or "Unk"/None; str length checked in the handler

    biome: str | None = Field(default=None, max_length=_BIOME_MAX)
    note: str | None = Field(default=None, max_length=_NOTE_MAX)


class WildlifeCaptureIn(BaseModel):
    species: str = Field(max_length=_TERM_MAX)
    biome: str | None = Field(default=None, max_length=_BIOME_MAX)
    note: str | None = Field(default=None, max_length=_NOTE_MAX)


class HarvestableCaptureIn(BaseModel):
    name: str = Field(max_length=_TERM_MAX)
    biome: str | None = Field(default=None, max_length=_BIOME_MAX)
    note: str | None = Field(default=None, max_length=_NOTE_MAX)


class PackageIn(BaseModel):
    """One cargo line: pick up `scu` of `commodity` at `from_id`, deliver to
    `to_id`. from->to encodes pickup-before-dropoff precedence.

    Multi-pickup delivery (rare): when a contract gives one commodity total but
    spreads the cargo over several pickup locations without saying how much is at
    each, the rows share a `group` id and carry the delivery total in `group_scu`
    (per-row `scu` is then unused/0). The solver counts that total once, holds it
    conservatively from the group's first pickup to its drop, and requires every
    listed pickup to precede the dropoff. Normal rows leave `group` None."""
    id: str | None = Field(default=None, max_length=_PKG_ID_MAX)
    commodity: str | None = Field(default=None, max_length=_COMMODITY_MAX)
    scu: float = Field(ge=0, le=100_000)
    from_id: int
    to_id: int
    contract: str | None = Field(default=None, max_length=_CONTRACT_MAX)  # display-only group label
    group: str | None = Field(default=None, max_length=_PKG_ID_MAX)       # multi-pickup binding id
    group_scu: float | None = Field(default=None, ge=0, le=100_000)       # delivery total for the group


_MAX_REWARD = 1e12   # generous aUEC ceiling — a sanity cap, not a real limit


class RoutePlanIn(BaseModel):
    packages: list[PackageIn] = Field(max_length=_MAX_PACKAGES)
    usable_scu: float = Field(gt=0, le=100_000)
    start_id: int | None = None    # POI to start from
    start_here: bool = False       # start from the caller's live show_location fix
    # Precedence: start_here (live position) > start_id (chosen POI) > free start.
    # Per-contract payout keyed by the package `contract` label ("" = the
    # ungrouped bucket). Display-only/advisory — never affects routing; the run's
    # total payout is the sum, the denominator for aUEC/hour.
    rewards: dict[str, float] = Field(default_factory=dict)


# Breadcrumb trail tuning. In-memory and session-scoped (lost on restart).
PATH_MIN_MOVE_M = 250.0   # don't record a crumb until you've moved this far
PATH_MAX = 5000           # cap so a long session can't grow unbounded

# Cargo-run arrival thresholds. Generous on purpose: arrival only surfaces the
# stop's package checklist for the player to confirm — it never auto-completes —
# so erring toward "you're here" is safe and helpful.
ARRIVAL_SURFACE_M = 5_000.0    # on the destination's own body (surface guidance)
ARRIVAL_SPACE_M = 50_000.0     # everything else (station / space approach)

# Live presence tuning.
PRESENCE_TICK_S = 1.0     # broadcaster cadence (coalesced upserts, ~1 Hz)
PRESENCE_STALE_S = 120.0  # drop a teammate after this long with no new position
PRESENCE_MOVE_M = 5.0     # only recompute heading once actually moving


class Session:
    """One org member's live state: position cursor, destination, capture
    arming, breadcrumb trail, and their open browser tabs. Keyed by Discord id
    so each member gets an independent course while sharing the dataset."""

    def __init__(self, user: dict):
        self.user = user           # {"id","display_name","is_admin"}
        self.pos = None
        self.t = None
        self.prev_pos = None
        self.prev_t = None
        self.destination_id = None
        self.run = None            # active cargo-planner run blob (or None)
        self.nav_state = None
        # capture_pending: {"kind": "poi"} or
        # {"kind": "observation", "category", "data", "biome", "note"} while armed
        self.capture_pending = None
        self.last_capture = None      # summary of this member's most recent capture
        self.owner = None             # {"player_id","handle"} from latest position
        self.shard = None             # current SC shard id (from the watcher's Game.log)
        self.tracking = False
        self.path = []                # crumbs: {lat, lon, container}
        # Live teammate presence: on by default, one-way opt-out (hide yourself
        # but keep seeing others). In-memory + per-session (resets to share-on
        # on restart, matching the "share by default" decision).
        self.share_presence = True
        self.ws_clients: set[WebSocket] = set()

    def capture_status(self):
        return {
            "armed": self.capture_pending is not None,
            "pending": self.capture_pending,
            "last": self.last_capture,
            "owner": self.owner,
        }

    def recompute(self):
        if self.pos is None:
            self.nav_state = None
            return
        self.nav_state = nav_core.compute_state(
            nav, self.pos, self.t,
            destination_id=self.destination_id,
            prev_pos=self.prev_pos, prev_t=self.prev_t,
            viewer_owner_ids=viewer_owner_ids(self.user),
        )
        # The client's own shard rides on the state so it can flag which
        # observations / teammates share its server.
        self.nav_state["shard"] = self.shard
        self._attach_breadcrumbs()
        self.nav_state["run"] = self.run_view()

    def _arrived_at_active(self) -> bool:
        """Whether the guidance distance to the active stop is within the
        arrival threshold (surface vs. space picked from the live readout)."""
        dest = self.nav_state.get("destination") if self.nav_state else None
        if not dest:
            return False
        surf = dest.get("surface_distance_m")
        if surf is not None:
            return surf < ARRIVAL_SURFACE_M
        d = dest.get("distance_m")
        return d is not None and d < ARRIVAL_SPACE_M

    def onboard_scu(self) -> float:
        """Live cargo aboard = sum of SCU for packages currently 'onboard'. A
        multi-pickup group's full total counts once while any of its pickups is
        aboard (conservative, matching the planner's capacity model)."""
        if not self.run:
            return 0.0
        total = 0.0
        group_aboard = {}     # gid -> group_scu, counted if any pickup is onboard
        for p in self.run["packages"].values():
            g = p.get("group")
            if g is None:
                if p["state"] == "onboard":
                    total += p["scu"]
            elif p["state"] == "onboard":
                group_aboard[g] = float(p.get("group_scu") or 0)
        return total + sum(group_aboard.values())

    def run_view(self) -> dict | None:
        """The active run as the client renders it: ordered stops with per-package
        live state, the active-stop cursor, live onboard SCU, and the arrival
        flag for the active stop."""
        if not self.run:
            return None
        run, pkgs = self.run, self.run["packages"]
        active = run["active"]
        stops = []
        for i, s in enumerate(run["stops"]):
            stops.append({
                **s,
                "pickups": [{**pkgs[str(p["id"])]} for p in s["pickups"]],
                "dropoffs": [{**pkgs[str(p["id"])]} for p in s["dropoffs"]],
            })
        return {
            "id": run["id"], "ship": run.get("ship"), "usable_scu": run["usable_scu"],
            "active": active, "done": active >= len(run["stops"]),
            "arrived": active < len(run["stops"]) and self._arrived_at_active(),
            "onboard_scu": round(self.onboard_scu(), 2),
            "stops": stops,
        }

    def record_crumb(self):
        """Append a breadcrumb if tracking is on, we're on a body surface, and
        we've moved far enough since the last crumb. Call after recompute()."""
        s = self.nav_state
        cont = s.get("container") if s else None
        if not (self.tracking and cont and cont.get("is_body") and s.get("latitude") is not None):
            return
        name, lat, lon = cont["name"], s["latitude"], s["longitude"]
        if self.path and self.path[-1]["container"] == name:
            radius = cont.get("body_radius_m") or 1.0
            last = self.path[-1]
            if nav_core.surface_distance_m(last["lat"], last["lon"], lat, lon, radius) < PATH_MIN_MOVE_M:
                return
        self.path.append({"lat": lat, "lon": lon, "container": name})
        if len(self.path) > PATH_MAX:
            del self.path[: len(self.path) - PATH_MAX]
        self._attach_breadcrumbs()

    def _attach_breadcrumbs(self):
        """Expose the tracking flag + the trail for the *current* container
        (crumbs on other bodies aren't drawable on the local map)."""
        if self.nav_state is None:
            return
        cont = self.nav_state.get("container")
        cur = cont["name"] if cont else None
        self.nav_state["tracking"] = self.tracking
        self.nav_state["path"] = (
            [{"lat": c["lat"], "lon": c["lon"]} for c in self.path if c["container"] == cur]
            if cur else []
        )

    async def broadcast(self):
        message = json.dumps(
            {"type": "state", "data": self.nav_state, "capture": self.capture_status()}
        )
        for ws in list(self.ws_clients):   # copy: a tab may connect/drop mid-send
            try:
                await ws.send_text(message)
            except Exception:
                self.ws_clients.discard(ws)


class SessionHub:
    """All live member sessions + the single lock that serializes state +
    shared-dataset mutations (org scale is low; one lock is simplest + safe).

    Also owns live teammate presence: `presence[uid]` holds each sharing
    member's latest on-a-body fix; changes are queued (`_dirty`/`_removed`) and
    flushed by a ~1 Hz background broadcaster so a fast watcher can't spam tabs.
    All mutations happen while holding `lock`."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.sessions: dict[str, Session] = {}
        self.presence: dict[str, dict] = {}   # uid -> internal record (w/ last_update)
        self._dirty: set[str] = set()          # uids with a pending upsert
        self._removed: set[str] = set()        # uids with a pending remove

    def get(self, user: dict) -> Session:
        sess = self.sessions.get(user["id"])
        if sess is None:
            sess = Session(user)
            self.sessions[user["id"]] = sess
            # Resume an in-progress cargo run across restart / reconnect: reload
            # it and re-point guidance at its active stop.
            run = db.get_active_run(user["id"])
            if run:
                sess.run = run
                _point_at_active_stop(sess)
        else:
            sess.user = user           # refresh display name / admin flag
        return sess

    # --- presence -----------------------------------------------------------
    def _presence_record(self, sess: "Session") -> dict | None:
        """Build a presence fix from a session's current nav_state, or None when
        the member isn't on a body surface (presence is surface-only — there's no
        teammate map in deep space). Heading is derived from the last fix."""
        s = sess.nav_state
        cont = s.get("container") if s else None
        if not (cont and cont.get("is_body") and s.get("latitude") is not None):
            return None
        uid = sess.user["id"]
        lat, lon = s["latitude"], s["longitude"]
        system, body = s.get("system"), cont["name"]
        heading = None
        prev = self.presence.get(uid)
        if prev and prev["system"] == system and prev["body"] == body:
            radius = cont.get("body_radius_m") or 1.0
            dist, bearing = nav_core.great_circle(prev["lat"], prev["lon"], lat, lon, radius)
            heading = bearing if dist > PRESENCE_MOVE_M else prev.get("heading")
        return {
            "discord_id": uid,
            "display_name": sess.user.get("display_name"),
            "handle": sess.owner["handle"] if sess.owner else None,
            "shard": sess.shard,
            "system": system, "body": body, "lat": lat, "lon": lon,
            "heading": heading, "last_update": time.time(),
        }

    @staticmethod
    def _public_presence(rec: dict) -> dict:
        """Wire form: drop last_update, expose age_s at send time."""
        return {
            "discord_id": rec["discord_id"], "display_name": rec["display_name"],
            "handle": rec["handle"], "shard": rec["shard"],
            "system": rec["system"], "body": rec["body"],
            "lat": rec["lat"], "lon": rec["lon"], "heading": rec["heading"],
            "age_s": max(0.0, time.time() - rec["last_update"]),
        }

    def touch_presence(self, sess: "Session") -> None:
        """Recompute + queue this member's presence (or a remove if they left a
        body / stopped sharing). Call under the lock after recompute()."""
        uid = sess.user["id"]
        rec = self._presence_record(sess) if sess.share_presence else None
        if rec is None:
            self.drop_presence(uid)
            return
        self.presence[uid] = rec
        self._dirty.add(uid)
        self._removed.discard(uid)

    def drop_presence(self, uid: str) -> None:
        if uid in self.presence:
            del self.presence[uid]
            self._removed.add(uid)
            self._dirty.discard(uid)

    def roster(self) -> list[dict]:
        return [self._public_presence(r) for r in self.presence.values()]

    async def send_to_all_clients(self, message: dict) -> None:
        text = json.dumps(message)
        for s in list(self.sessions.values()):   # copy: get() may add a session mid-send
            for ws in list(s.ws_clients):   # copy: a tab may drop mid-send
                try:
                    await ws.send_text(text)
                except Exception:
                    s.ws_clients.discard(ws)

    def online_count(self) -> int:
        """Members with at least one open tab right now. Counts people, not tabs
        (one member with three tabs is one online player). Approximate: a tab
        that dies without a clean close lingers until a failed send prunes it."""
        return sum(1 for s in self.sessions.values() if s.ws_clients)

    async def broadcast_online(self) -> None:
        """Push the current online-player count to every tab. Cheap; called on
        connect/disconnect so the top-bar count tracks comings and goings."""
        await self.send_to_all_clients({"type": "online", "count": self.online_count()})

    def forget_entity(self, entity_id: int) -> None:
        """A deleted/refreshed-away POI/observation must stop being any member's
        destination or last-capture reference."""
        for s in self.sessions.values():
            if s.destination_id == entity_id:
                s.destination_id = None
            if s.last_capture and s.last_capture.get("id") == entity_id:
                s.last_capture = None

    async def broadcast_all(self) -> None:
        """The shared dataset changed (capture/delete/refresh) — recompute and
        push every session so all members' nearby/destination reflect it."""
        for s in self.sessions.values():
            s.recompute()
            await s.broadcast()


hub = SessionHub()


async def presence_broadcaster():
    """~1 Hz loop: drop teammates whose last fix is stale (emit `remove`), then
    flush coalesced upserts/removes to every open tab. Coalescing means a fast
    watcher posting many positions still costs at most one upsert per tick."""
    while True:
        await asyncio.sleep(PRESENCE_TICK_S)
        try:
            async with hub.lock:
                now = time.time()
                for uid, rec in list(hub.presence.items()):
                    if now - rec["last_update"] > PRESENCE_STALE_S:
                        hub.drop_presence(uid)
                upserts = [hub._public_presence(hub.presence[u])
                           for u in hub._dirty if u in hub.presence]
                removes = list(hub._removed)
                hub._dirty.clear()
                hub._removed.clear()
                if upserts:
                    await hub.send_to_all_clients(
                        {"type": "presence", "op": "upsert", "users": upserts})
                for uid in removes:
                    await hub.send_to_all_clients(
                        {"type": "presence", "op": "remove", "discord_id": uid})
        except Exception as exc:   # never let the loop die on a transient error
            print(f"[sc-nav] presence broadcaster error: {exc}")


@app.on_event("startup")
async def _start_presence_broadcaster():
    asyncio.create_task(presence_broadcaster())


@app.post("/api/position")
async def post_position(body: PositionIn, user: dict = Depends(require_user)):
    async with hub.lock:
        sess = hub.get(user)
        now = time.time()
        new_pos = (body.x, body.y, body.z)
        if sess.pos is not None and new_pos != sess.pos:
            sess.prev_pos, sess.prev_t = sess.pos, sess.t
        sess.pos, sess.t = new_pos, now
        if body.shard:
            sess.shard = body.shard.strip() or None

        if body.handle:
            entry = handles.register(body.handle, sess.user["id"])
            sess.owner = {"player_id": entry["player_id"], "handle": entry["handle"]}

        captured = False
        if sess.capture_pending is not None:
            pending = sess.capture_pending
            sess.capture_pending = None
            owner = sess.owner or {}
            if pending["kind"] == "observation":
                _capture_observation(sess, new_pos, now, pending, owner)
            else:
                _capture_poi(sess, new_pos, now, pending, owner)
            captured = True

        sess.recompute()
        sess.record_crumb()
        hub.touch_presence(sess)        # queue a teammate-map upsert (or remove)
        if captured:
            await hub.broadcast_all()   # a new POI is visible to everyone
        else:
            await sess.broadcast()
    return {"ok": True}


def _capture_poi(sess, pos_m, now, pending, owner):
    next_id = db.next_custom_poi_id()
    poi = nav_core.custom_poi_from_position(
        nav, pos_m, now, pending["name"], pending["type"], next_id,
        owner_id=owner.get("player_id"), owner_handle=owner.get("handle"),
        qt_marker=pending.get("qt_marker", False),
        private=pending.get("private", False), note=pending.get("note"),
    )
    try:
        db.add_custom_poi(nav_core.custom_poi_to_dict(poi))
    except Exception as exc:
        print(f"[sc-nav] custom poi save failed: {exc}")
    nav.pois[poi.id] = poi
    # A new QT marker changes the nearest-jump answer for every other entity,
    # so rebuild the index + reassign nearest_qt across the dataset.
    if poi.qt_marker:
        nav_core.assign_qt_markers(nav)
    sess.last_capture = {
        "kind": "poi", "id": poi.id, "name": poi.name, "type": poi.type,
        "container": poi.container_name or "Space", "system": poi.system,
        "latitude": poi.latitude, "longitude": poi.longitude,
        "qt_marker": poi.qt_marker, "private": poi.private,
        "owner_handle": poi.owner_handle,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }


def _capture_observation(sess, pos_m, now, pending, owner):
    category = pending["category"]
    # Shared id space across categories (>= OBSERVATION_ID_START); MAX(id)+1 from
    # the DB, so a deleted top id is never reused even across restarts.
    next_id = db.next_observation_id()
    obs = nav_core.observation_from_position(
        nav, pos_m, now, category, pending["data"], next_id,
        biome=pending.get("biome"), note=pending.get("note"),
        owner_id=owner.get("player_id"), owner_handle=owner.get("handle"),
        shard_id=sess.shard,
    )
    try:
        db.add_observation(nav_core.observation_to_dict(obs))
    except Exception as exc:
        print(f"[sc-nav] observation save failed: {exc}")
    nav.observations[obs.id] = obs
    sess.last_capture = {
        **nav_core._observation_base(obs),
        "latitude": obs.latitude, "longitude": obs.longitude,
        "captured_at": obs.observed_at,
    }


@app.get("/api/state")
async def get_state(user: dict = Depends(require_session)):
    sess = hub.get(user)
    return {
        "state": sess.nav_state,
        "destination_id": sess.destination_id,
        "capture": sess.capture_status(),
        "systems": nav.systems,
    }


@app.get("/api/pois")
async def get_pois(
    q: str = "", system: str | None = None, container: str | None = None,
    type: str | None = None, owner_id: int | None = None, limit: int = 25,
    user: dict | None = Depends(current_user),
):
    return nav_core.search_pois(
        nav, query=q, system=system, container=container, poi_type=type,
        owner_id=owner_id, limit=min(limit, 5000),
        viewer_owner_ids=viewer_owner_ids(user),
    )


@app.post("/api/destination")
async def set_destination(body: DestinationIn, user: dict = Depends(require_session)):
    target = nav.pois.get(body.poi_id) or nav.observations.get(body.poi_id)
    # A private POI you don't own is invisible — including as a routing target.
    if isinstance(target, nav_core.Poi) and not nav_core.poi_visible_to(
        target, viewer_owner_ids(user)
    ):
        target = None
    if target is None:
        raise HTTPException(status_code=404, detail="unknown poi_id")
    async with hub.lock:
        sess = hub.get(user)
        sess.destination_id = body.poi_id
        sess.recompute()
        await sess.broadcast()
    if isinstance(target, nav_core.Observation):
        name = nav_core.OBSERVATION_CATEGORIES[target.category]["display_name"](target.data)
    else:
        name = target.name
    return {"ok": True, "destination": {"id": body.poi_id, "name": name}}


@app.delete("/api/destination")
async def clear_destination(user: dict = Depends(require_session)):
    async with hub.lock:
        sess = hub.get(user)
        sess.destination_id = None
        sess.recompute()
        await sess.broadcast()
    return {"ok": True}


@app.post("/api/capture/start")
async def capture_start(body: CaptureIn, user: dict = Depends(require_session)):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    async with hub.lock:
        sess = hub.get(user)
        if sess.capture_pending is not None:
            raise HTTPException(status_code=409, detail="another capture is already armed; cancel it first")
        sess.capture_pending = {
            "kind": "poi", "name": name, "type": body.type.strip() or "Custom",
            # A QT marker is shared navigation infrastructure, so private wins:
            # a private POI is never also a QT marker.
            "qt_marker": body.qt_marker and not body.private,
            "private": body.private,
            "note": body.note.strip() or None,
        }
        await sess.broadcast()
        return {"ok": True, "capture": sess.capture_status()}


async def _arm_observation(user, category, data, biome, note):
    async with hub.lock:
        sess = hub.get(user)
        if sess.capture_pending is not None:
            raise HTTPException(status_code=409, detail="another capture is already armed; cancel it first")
        sess.capture_pending = {
            "kind": "observation",
            "category": category,
            "data": data,
            "biome": (biome or "").strip() or None,
            "note": (note or "").strip() or None,
        }
        await sess.broadcast()
        return {"ok": True, "capture": sess.capture_status()}


@app.post("/api/capture/node")
async def capture_node_start(body: NodeCaptureIn, user: dict = Depends(require_session)):
    ore = body.ore.strip()
    if not ore:
        raise HTTPException(status_code=400, detail="ore is required")
    if isinstance(body.band, str) and len(body.band) > _BAND_MAX:
        raise HTTPException(status_code=400, detail="band value too long")
    # band passed through raw; _normalize_resource handles "Unk"/None.
    return await _arm_observation(
        user, "resource", {"ore": ore, "band": body.band}, body.biome, body.note
    )


@app.post("/api/capture/wildlife")
async def capture_wildlife_start(body: WildlifeCaptureIn, user: dict = Depends(require_session)):
    species = body.species.strip()
    if not species:
        raise HTTPException(status_code=400, detail="species is required")
    return await _arm_observation(user, "wildlife", {"species": species}, body.biome, body.note)


@app.post("/api/capture/harvestable")
async def capture_harvestable_start(body: HarvestableCaptureIn, user: dict = Depends(require_session)):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    return await _arm_observation(user, "harvestable", {"name": name}, body.biome, body.note)


@app.post("/api/capture/cancel")
async def capture_cancel(user: dict = Depends(require_session)):
    async with hub.lock:
        sess = hub.get(user)
        sess.capture_pending = None
        await sess.broadcast()
    return {"ok": True}


@app.get("/api/handles")
async def list_handles():
    return handles.list()


@app.get("/api/raw_commodities")
async def list_raw_commodities():
    """Raw-ore names (uexcorp is_raw==1) for the ore datalist."""
    return raw_commodity_names


@app.get("/api/commodities")
async def list_commodities():
    """All commodity names (every kind) for the cargo-planner commodity picker."""
    return commodity_names


@app.get("/api/ships")
async def list_ships():
    """Cargo-capable ships (name + stated SCU) for the cargo-planner ship
    picker, from the uexcorp vehicles feed."""
    return ships


@app.get("/api/harvestables")
async def list_harvestables():
    """Harvestable flora/natural names (uexcorp kind=Natural, is_harvestable=1)
    for the Add Fauna & Harvestables datalist."""
    return harvestable_names


@app.get("/api/fauna")
async def list_fauna():
    """Curated fauna/species names for the Add Fauna datalist."""
    return fauna_names


# The forecast/finder/heatmap endpoints work for any mappable observation
# category (resources by ore, harvestables by name); `category` selects which.
_MAPPABLE_CATEGORIES = ("resource", "harvestable")


def _require_mappable_category(category: str) -> str:
    if category not in _MAPPABLE_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"unknown category: {category}")
    return category


@app.get("/api/resource_cells")
async def get_resource_cells(system: str, body: str, category: str = "resource"):
    """Per-cell type composition for the map heatmap (cells with ≥1 sighting)."""
    _require_mappable_category(category)
    cont = nav.containers.get((system, body))
    if cont is None or not cont.is_body:
        raise HTTPException(status_code=404, detail="unknown body")
    cells = nav_core.resource_cells(nav, system, body, cont.body_radius, category=category)
    return {"cell_m": nav_core.RESOURCE_CELL_M, "cells": cells}


@app.get("/api/resource_ores")
async def get_resource_ores(category: str = "resource"):
    """Type names present in sightings of `category` (element-finder picker)."""
    _require_mappable_category(category)
    return nav_core.resource_ore_names(nav, category=category)


@app.get("/api/resource_hotspots")
async def get_resource_hotspots(
    request: Request, ore: str, system: str | None = None, body: str | None = None,
    limit: int = 20, sort: str = "likely", category: str = "resource",
):
    """Known areas richest in `ore` (or harvestable name), ranked. sort: likely |
    near | value. The 'near'/'value' modes use the caller's own live position."""
    _require_mappable_category(category)
    sess = hub.sessions.get(require_user(request)["id"])
    pos = sess.pos if sess else None
    t = sess.t if sess else None
    return {
        "ore": ore,
        "sort": sort,
        "category": category,
        "has_position": pos is not None,
        "cell_m": nav_core.RESOURCE_CELL_M,
        "hotspots": nav_core.resource_hotspots(
            nav, ore, system=system, body=body, limit=min(limit, 100),
            from_pos=pos, t_ref=t, sort=sort, category=category,
        ),
    }


def _clean_rewards(rewards: dict) -> dict:
    """Validate + drop empty per-contract payouts. Keys are contract labels
    (capped like the entry field); values are non-negative aUEC under a sanity
    ceiling. 400 on anything malformed."""
    out = {}
    if len(rewards) > _MAX_PACKAGES:
        raise HTTPException(status_code=400, detail="too many contract rewards")
    for label, amount in rewards.items():
        if len(label) > _CONTRACT_MAX:
            raise HTTPException(status_code=400, detail="contract label too long")
        if not (0 <= amount <= _MAX_REWARD):
            raise HTTPException(status_code=400, detail="reward out of range")
        if amount:                       # 0 / blank means "no payout entered"
            out[label] = float(amount)
    return out


def _apply_reward_summary(summary: dict, rewards: dict) -> dict:
    """Layer the run's payout onto a plan summary: total reward and the derived
    aUEC/hour (needs a finite run time). Always present so the client can render
    uniformly; aUEC/hour is null when there's no reward or no time estimate."""
    total = round(float(sum(rewards.values())), 2)
    t = summary.get("total_time_s")
    summary["total_reward"] = total
    summary["auec_per_hour"] = round(total / (t / 3600.0), 2) if (total and t) else None
    return summary


@app.post("/api/route/plan")
async def post_route_plan(body: RoutePlanIn, user: dict = Depends(require_session)):
    """Stateless cargo-route optimizer: order the accepted packages into an
    efficient run under the ship's usable SCU. Returns ordered stops (each with
    pickups/dropoffs, arrival leg detail, running onboard SCU) plus a feasibility
    + totals summary (payout + aUEC/hour when rewards are supplied). Leg distances
    reflect the caller's live rotation time."""
    sess = hub.sessions.get(user["id"])
    if body.start_id is not None and body.start_id not in nav.pois:
        raise HTTPException(status_code=404, detail="unknown start_id")
    rewards = _clean_rewards(body.rewards)
    start_pos = None
    if body.start_here:
        if sess is None or sess.pos is None:
            raise HTTPException(status_code=400,
                                detail="no live position yet — run /showlocation, or pick a start POI")
        start_pos = sess.pos
    try:
        plan = nav_core.plan_route(
            nav, [p.model_dump() for p in body.packages],
            usable_scu=body.usable_scu, start_id=body.start_id, start_pos=start_pos,
            t_ref=sess.t if sess else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _apply_reward_summary(plan["summary"], rewards)
    return plan


# --- cargo run execution (stateful, persisted per member) -------------------


class RunStartIn(RoutePlanIn):
    ship: str | None = Field(default=None, max_length=_NAME_MAX)


class RunPatchIn(BaseModel):
    package_id: str | None = Field(default=None, max_length=_PKG_ID_MAX)
    group: str | None = Field(default=None, max_length=_PKG_ID_MAX)  # set every package of a group
    state: str | None = None       # pending | onboard | delivered
    advance: bool = False          # force past the current stop (partial load)


def _point_at_active_stop(sess: "Session") -> None:
    """Point the session's destination at the run's active stop (or clear it
    when the run is finished), so the existing guidance loop drives the player."""
    run = sess.run
    if run and run["active"] < len(run["stops"]):
        sess.destination_id = run["stops"][run["active"]]["stop_id"]
    else:
        sess.destination_id = None


def _stop_resolved(run: dict, i: int) -> bool:
    """A stop is done when every package it loads is onboard/delivered and every
    package it drops is delivered."""
    pkgs = run["packages"]
    if any(pkgs[str(p["id"])]["state"] == "pending" for p in run["stops"][i]["pickups"]):
        return False
    return all(pkgs[str(p["id"])]["state"] == "delivered" for p in run["stops"][i]["dropoffs"])


def _advance_run(sess: "Session") -> bool:
    """Skip the cursor past any fully-resolved stops and re-point guidance.
    Returns True when the run is now complete."""
    run = sess.run
    while run["active"] < len(run["stops"]) and _stop_resolved(run, run["active"]):
        run["active"] += 1
    _point_at_active_stop(sess)
    return run["active"] >= len(run["stops"])


@app.post("/api/route/run")
async def start_run(body: RunStartIn, user: dict = Depends(require_session)):
    """Start (and persist) an active run from the same input as /plan. Re-solves
    server-side, sets the first stop as the guidance destination, and replaces
    any prior active run. 409 if the bundle is infeasible."""
    if body.start_id is not None and body.start_id not in nav.pois:
        raise HTTPException(status_code=404, detail="unknown start_id")
    rewards = _clean_rewards(body.rewards)
    async with hub.lock:
        sess = hub.get(user)
        start_pos = None
        if body.start_here:
            if sess.pos is None:
                raise HTTPException(status_code=400,
                                    detail="no live position yet — run /showlocation, or pick a start POI")
            start_pos = sess.pos
        try:
            plan = nav_core.plan_route(
                nav, [p.model_dump() for p in body.packages],
                usable_scu=body.usable_scu, start_id=body.start_id, start_pos=start_pos,
                t_ref=sess.t,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not plan["summary"]["feasible"]:
            raise HTTPException(
                status_code=409,
                detail=f"route infeasible: needs {plan['summary']['min_capacity_scu']} usable SCU",
            )
        _apply_reward_summary(plan["summary"], rewards)
        packages = {}
        for s in plan["stops"]:
            for p in s["pickups"]:
                packages[str(p["id"])] = {**p, "state": "pending"}
        sm = plan["summary"]
        run = {
            "ship": body.ship, "usable_scu": body.usable_scu,
            # denormalized onto the blob so the guild leaderboard can label the
            # member without a join (the runs row keys only on discord_id).
            "display_name": user.get("display_name"),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "stops": plan["stops"], "packages": packages, "active": 0,
            # frozen totals for history/stats (no need to re-solve completed runs)
            "rewards": rewards, "total_reward": sm["total_reward"],
            "total_time_s": sm["total_time_s"], "total_distance_m": sm["total_distance_m"],
        }
        run["id"] = db.start_run(user["id"], body.ship, run["started_at"], run)
        sess.run = run
        _point_at_active_stop(sess)
        sess.recompute()
        await sess.broadcast()
        return {"ok": True, "run": sess.run_view()}


@app.get("/api/route/run")
async def get_run(user: dict = Depends(require_session)):
    """The caller's active run (or null) — for the UI to load / resume run mode."""
    async with hub.lock:
        return {"run": hub.get(user).run_view()}


@app.patch("/api/route/run")
async def patch_run(body: RunPatchIn, user: dict = Depends(require_session)):
    """Check a package off at the active stop (state = onboard / delivered), or
    force-advance. Auto-advances past fully-resolved stops; completing the last
    stop finishes the run."""
    async with hub.lock:
        sess = hub.get(user)
        run = sess.run
        if not run:
            raise HTTPException(status_code=404, detail="no active run")
        if body.state is not None and (body.package_id is not None or body.group is not None):
            if body.state not in ("pending", "onboard", "delivered"):
                raise HTTPException(status_code=400, detail="bad package state")
            if body.group is not None:
                # set every package of a multi-pickup group at once (its single drop)
                ids = [pid for pid, p in run["packages"].items() if p.get("group") == body.group]
                if not ids:
                    raise HTTPException(status_code=404, detail="unknown group")
                for pid in ids:
                    run["packages"][pid]["state"] = body.state
            else:
                if body.package_id not in run["packages"]:
                    raise HTTPException(status_code=404, detail="unknown package")
                run["packages"][body.package_id]["state"] = body.state
        if body.advance and run["active"] < len(run["stops"]):
            run["active"] += 1
            _point_at_active_stop(sess)
        completed = _advance_run(sess)
        if completed:
            db.complete_run(user["id"], run["id"],
                            datetime.now(timezone.utc).isoformat(), run)
            sess.run = None
            sess.destination_id = None
        else:
            db.update_run(user["id"], run["id"], run)
        sess.recompute()
        await sess.broadcast()
        return {"ok": True, "completed": completed, "run": sess.run_view()}


@app.delete("/api/route/run")
async def abandon_run(user: dict = Depends(require_session)):
    """Abandon the caller's active run and release the guidance destination."""
    async with hub.lock:
        sess = hub.get(user)
        had = db.abandon_run(user["id"])
        sess.run = None
        sess.destination_id = None
        sess.recompute()
        await sess.broadcast()
        return {"ok": True, "abandoned": had}


def _run_summary(run: dict) -> dict:
    """Compact completed-run record for the history list + the 'clone' shortcut:
    headline totals plus the full package list (POI ids resolved to names) so the
    UI can repopulate the entry form without another round-trip."""
    pkgs = []
    records = nav_core.run_packages(run)
    total_scu = nav_core.packages_scu(records)
    for p in records:
        fid, tid = p.get("from_id"), p.get("to_id")
        pkgs.append({
            "commodity": p.get("commodity"), "scu": float(p.get("scu") or 0),
            "from_id": fid, "from_name": nav_core._poi_name(nav, fid),
            "to_id": tid, "to_name": nav_core._poi_name(nav, tid),
            "contract": p.get("contract"),
            "group": p.get("group"), "group_scu": p.get("group_scu"),
        })
    reward = nav_core.run_total_reward(run)
    t = run.get("total_time_s")
    return {
        "id": run.get("id"), "ship": run.get("ship"),
        "started_at": run.get("started_at"), "completed_at": run.get("completed_at"),
        "usable_scu": run.get("usable_scu"),
        "num_stops": len(run.get("stops", [])), "num_packages": len(pkgs),
        "total_scu": round(total_scu, 2), "packages": pkgs,
        "reward": round(reward, 2), "rewards": run.get("rewards") or {},
        "auec_per_hour": round(reward / (t / 3600.0), 2) if (reward and t) else None,
    }


@app.get("/api/route/history")
async def get_route_history(user: dict = Depends(require_session)):
    """The caller's completed hauling runs (freshest first, for the recent-runs
    list + clone), headline hauling stats (totals + aUEC/hour) in two scopes —
    `stats` over the recent window and `session_stats` since the player's session
    marker — and frequency-ranked quick-picks (lanes / commodities / ships) that
    float a player's repeat hauls to the top of the entry pickers."""
    runs = db.list_run_history(user["id"])
    session_start = db.get_cargo_session_start(user["id"])
    # ISO-8601 UTC timestamps compare lexicographically, so a string >= works.
    session_runs = ([r for r in runs if (r.get("completed_at") or "") >= session_start]
                    if session_start else runs)
    return {"runs": [_run_summary(r) for r in runs],
            "stats": nav_core.derive_run_stats(runs),
            "session_stats": nav_core.derive_run_stats(session_runs),
            "session_start": session_start,
            "picks": nav_core.derive_quick_picks(nav, runs)}


@app.post("/api/route/session/reset")
async def reset_route_session(user: dict = Depends(require_session)):
    """Start a fresh hauling session: stamp 'now' as the session marker so the
    session-scoped stats reset to zero and count only runs completed from here on.
    Non-destructive — run history and quick-picks are untouched."""
    ts = datetime.now(timezone.utc).isoformat()
    db.set_cargo_session_start(user["id"], ts)
    return {"ok": True, "session_start": ts}


def _resolve_member_name(discord_id: str, stored: str | None) -> str:
    """A display name for a member on the cargo leaderboard. Prefers the name the
    run was stamped with; otherwise falls back to a watcher token's name, then a
    linked SC handle, then a short id stub. Keeps the board labelled even for
    runs that predate name capture or members who never minted a token."""
    if stored:
        return stored
    for t in tokens.items:
        if t.get("discord_id") == discord_id and t.get("display_name"):
            return t["display_name"]
    for pid in handles.player_ids_for(discord_id):
        h = handles.handle_for(pid)
        if h:
            return h
    return f"Member {str(discord_id)[-4:]}"


def _cargo_window_start(rng: str) -> str | None:
    """ISO start for a cargo leaderboard/stats time window: 'week' = the trailing
    7 days; anything else = all-time (None)."""
    if rng == "week":
        return (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    return None


@app.get("/api/cargo/leaderboard")
async def cargo_leaderboard(range: str = "all", user: dict = Depends(require_session)):
    """Guild hauling leaderboard. Every member's completed runs are tallied per
    member (no opt-in — contribution is the point), then surfaced as two boards:
    top earners (total aUEC) and most efficient (aUEC/hour, members with timed
    runs only). `range=week` limits to the trailing 7 days; default is all-time."""
    runs = db.list_all_completed_runs(_cargo_window_start(range))
    rows = nav_core.derive_guild_leaderboard(runs)
    for r in rows:
        r["display_name"] = _resolve_member_name(r["discord_id"], r.get("display_name"))
        r["mine"] = r["discord_id"] == user["id"]
    earners = sorted(rows, key=lambda r: (-r["total_reward"], r["display_name"].lower()))
    efficient = sorted((r for r in rows if r.get("auec_per_hour")),
                       key=lambda r: (-r["auec_per_hour"], r["display_name"].lower()))
    return {"range": range, "num_haulers": len(rows),
            "earners": earners, "efficient": efficient}


@app.get("/api/cargo/stats")
async def cargo_stats(range: str = "all", user: dict = Depends(require_session)):
    """Guild-wide hauling statistics for the cargo Statistics page: headline
    totals, top commodities / lanes / ships, and a weekly aUEC sparkline.
    `range=week` scopes the totals/breakdowns to the trailing 7 days; the
    sparkline always spans the trailing weeks so the trend stays readable."""
    runs = db.list_all_completed_runs(_cargo_window_start(range))
    stats = nav_core.derive_guild_cargo_stats(nav, runs)
    # Weekly aUEC earned (mirrors /api/stats' activity series). Always all-time so
    # the trend doesn't collapse to a single bar under the 'week' range.
    spark_runs = runs if range != "week" else db.list_all_completed_runs(None)
    weeks: Counter = Counter()
    for run in spark_runs:
        ts = run.get("completed_at")
        rw = nav_core.run_total_reward(run)
        if not ts or not rw:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        weeks[_iso_week_start(dt)] += rw
    activity = []
    if weeks:
        end = _iso_week_start(datetime.now(timezone.utc))
        start = end - timedelta(weeks=_STATS_WEEKS - 1)
        wk = start
        while wk <= end:
            activity.append({"label": wk.strftime("%b %d"),
                             "count": round(weeks.get(wk, 0), 2)})
            wk += timedelta(weeks=1)
    return {"range": range, **stats, "activity": activity}


# --- event planner (guild events) ------------------------------------------


class RoleTargetIn(BaseModel):
    role: str = Field(max_length=_TYPE_MAX)
    needed: int = Field(default=1, ge=0, le=500)


class EventIn(BaseModel):
    title: str = Field(min_length=1, max_length=_NAME_MAX)
    description: str = Field(default="", max_length=_DESC_MAX)
    # An event can span several activities (e.g. a Cargo Haul + Combat Patrol)
    # and several flavors (e.g. both PvP and PvE) at once.
    types: list[str] = Field(default_factory=list, max_length=12)
    categories: list[str] = Field(default_factory=list, max_length=12)
    start_at: str = Field(max_length=_META_MAX)   # ISO8601 UTC; validated below
    # Optional: after this, signups lock. Blank ⇒ signups close at start_at.
    signup_deadline: str | None = Field(default=None, max_length=_META_MAX)
    duration_min: int | None = Field(default=None, ge=0, le=100_000)
    location: str = Field(default="", max_length=_NAME_MAX)         # rally point
    event_location: str = Field(default="", max_length=_NAME_MAX)   # where it happens
    min_players: int = Field(default=0, ge=0, le=_MAX_PLAYERS)
    max_players: int | None = Field(default=None, ge=1, le=_MAX_PLAYERS)
    roles: list[RoleTargetIn] = Field(default_factory=list, max_length=_MAX_ROSTER_ROLES)


class SignupIn(BaseModel):
    roles: list[str] = Field(default_factory=list, max_length=_MAX_SIGNUP_ROLES)
    status: str = Field(default="going", max_length=16)   # going | maybe
    note: str | None = Field(default=None, max_length=_NOTE_MAX)


_EVENT_PUBLIC = ("id", "organizer_id", "title", "description",
                 "start_at", "signup_deadline", "duration_min", "location", "event_location",
                 "min_players", "max_players", "roles", "status",
                 "created_at", "updated_at")


def _normalize_event_start(s: str) -> str:
    """Parse the client's start time and canonicalize to a UTC ISO8601 string.
    Naive (tz-less) inputs are assumed UTC. Rejects unparseable values."""
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="start_at must be ISO8601")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _validate_event(body: EventIn) -> dict:
    """Validate an event against the curated taxonomy and normalize its fields
    into the column dict db.create_event / db.update_event expect."""
    types, type_seen = [], set()
    for t in body.types:
        if t not in event_taxonomy.TYPES:
            raise HTTPException(status_code=400, detail=f"unknown event type: {t}")
        if t not in type_seen:
            type_seen.add(t)
            types.append(t)
    if not types:
        raise HTTPException(status_code=400, detail="pick at least one type")
    categories, cat_seen = [], set()
    for c in body.categories:
        if c not in event_taxonomy.CATEGORIES:
            raise HTTPException(status_code=400, detail=f"unknown event category: {c}")
        if c not in cat_seen:
            cat_seen.add(c)
            categories.append(c)
    if not categories:
        raise HTTPException(status_code=400, detail="pick at least one category")
    roster, seen = [], set()
    for r in body.roles:
        if r.role not in event_taxonomy.ROLES:
            raise HTTPException(status_code=400, detail=f"unknown role: {r.role}")
        if r.role in seen:
            raise HTTPException(status_code=400, detail=f"duplicate role: {r.role}")
        seen.add(r.role)
        roster.append({"role": r.role, "needed": r.needed})
    if body.max_players is not None and body.max_players < max(1, body.min_players):
        raise HTTPException(status_code=400, detail="max_players must be >= min_players")
    start_at = _normalize_event_start(body.start_at)
    signup_deadline = None
    if body.signup_deadline:
        signup_deadline = _normalize_event_start(body.signup_deadline)
        if signup_deadline > start_at:
            raise HTTPException(status_code=400,
                                detail="signup deadline must be at or before the event start")
    return {
        "title": body.title.strip(),
        "description": (body.description or "").strip(),
        "type": types, "category": categories,
        "start_at": start_at,
        "signup_deadline": signup_deadline,
        "duration_min": body.duration_min,
        "location": (body.location or "").strip(),
        "event_location": (body.event_location or "").strip(),
        "min_players": body.min_players, "max_players": body.max_players,
        "roles": roster,
    }


def _validate_signup_roles(roles: list[str]) -> list[str]:
    """De-dupe (order-preserving) and reject any role outside the taxonomy."""
    out, seen = [], set()
    for r in roles:
        if r not in event_taxonomy.ROLES:
            raise HTTPException(status_code=400, detail=f"unknown role: {r}")
        if r in seen:
            continue
        seen.add(r)
        out.append(r)
    return out


def _require_event_owner(ev: dict, user: dict) -> None:
    if ev["organizer_id"] != user["id"] and not user.get("is_admin"):
        raise HTTPException(status_code=403,
                            detail="only the organizer or an admin can change this event")


def _event_view(ev: dict, user: dict, detail: bool = False) -> dict:
    """Serialize an event for the client: its fields plus the derived fill
    summary, the organizer's name, the caller's own signup, and the caller's
    permissions. `detail=True` adds the attendee roster (used by the detail view;
    list cards skip it to stay light)."""
    signups = db.list_signups(ev["id"])
    mine = next((s for s in signups
                 if s["discord_id"] == user["id"] and s["status"] != "withdrawn"), None)
    view = {k: ev.get(k) for k in _EVENT_PUBLIC}
    view["types"] = ev.get("type") or []
    view["categories"] = ev.get("category") or []
    view["organizer_name"] = _resolve_member_name(ev["organizer_id"], None)
    view["is_organizer"] = ev["organizer_id"] == user["id"]
    view["can_edit"] = view["is_organizer"] or bool(user.get("is_admin"))
    view["fill"] = nav_core.derive_event_fill(ev, signups)
    view.update(nav_core.derive_event_phase(ev, datetime.now(timezone.utc)))
    view["my_signup"] = ({"roles": mine["roles"], "status": mine["status"]}
                         if mine else None)
    if detail:
        view["attendees"] = [
            {"discord_id": s["discord_id"],
             "display_name": _resolve_member_name(s["discord_id"], None),
             "roles": s["roles"], "status": s["status"]}
            for s in signups if s["status"] in ("going", "maybe")
        ]
    return view


@app.get("/api/events/taxonomy")
async def events_taxonomy(user: dict = Depends(require_session)):
    """Curated types / categories / grouped roles for the create form."""
    return event_taxonomy.taxonomy()


@app.get("/api/events")
async def list_events(range: str = "upcoming", user: dict = Depends(require_session)):
    """The event board. `range=past` lists finished/cancelled events (freshest
    first); default lists everything not yet finished — open, signups-closed, and
    live/ongoing — soonest first. Each carries its derived fill + phase so cards
    render (and badge) without a per-event round-trip."""
    now_dt = datetime.now(timezone.utc)
    if range == "past":
        rows = db.list_events("past", now_dt.isoformat())
    else:
        # Reach back so live/ongoing events (start passed, not yet ended) stay on the
        # board; the phase filter below drops the ones that have actually finished.
        lookback = (now_dt - timedelta(minutes=_EVENT_BOARD_LOOKBACK_MIN)).isoformat()
        rows = db.list_events("upcoming", lookback)
    views = [_event_view(e, user) for e in rows]
    if range == "past":
        views = [v for v in views if v["phase"] in ("ended", "cancelled")]
    else:
        views = [v for v in views if v["phase"] != "ended"]
    return {"range": range, "events": views}


@app.post("/api/events")
async def create_event(body: EventIn, user: dict = Depends(require_session)):
    """Create an event. Any org member may organize."""
    fields = _validate_event(body)
    now = datetime.now(timezone.utc).isoformat()
    eid = db.create_event({**fields, "organizer_id": user["id"],
                           "status": "scheduled", "created_at": now, "updated_at": now})
    return _event_view(db.get_event(eid), user, detail=True)


@app.get("/api/events/{event_id}")
async def get_event(event_id: int, user: dict = Depends(require_session)):
    """One event with its attendee roster + fill."""
    ev = db.get_event(event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="unknown event")
    return _event_view(ev, user, detail=True)


@app.patch("/api/events/{event_id}")
async def edit_event(event_id: int, body: EventIn, user: dict = Depends(require_session)):
    """Edit an event (organizer or admin) — full replace of the editable fields."""
    ev = db.get_event(event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="unknown event")
    _require_event_owner(ev, user)
    fields = _validate_event(body)
    db.update_event(event_id, fields, datetime.now(timezone.utc).isoformat())
    return _event_view(db.get_event(event_id), user, detail=True)


@app.delete("/api/events/{event_id}")
async def cancel_event(event_id: int, user: dict = Depends(require_session)):
    """Cancel an event (organizer or admin). Soft — the row + roster survive."""
    ev = db.get_event(event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="unknown event")
    _require_event_owner(ev, user)
    db.cancel_event(event_id, datetime.now(timezone.utc).isoformat())
    return {"ok": True, "status": "cancelled"}


@app.post("/api/events/{event_id}/signup")
async def signup_event(event_id: int, body: SignupIn,
                       user: dict = Depends(require_session)):
    """Join (or update) the caller's signup with the role(s) they'll fill."""
    ev = db.get_event(event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="unknown event")
    phase = nav_core.derive_event_phase(ev, datetime.now(timezone.utc))
    if not phase["signups_open"]:
        detail = ("event is cancelled" if phase["phase"] == "cancelled"
                  else "signups have closed for this event")
        raise HTTPException(status_code=400, detail=detail)
    roles = _validate_signup_roles(body.roles)
    status = body.status if body.status in ("going", "maybe") else "going"
    db.upsert_signup(event_id, user["id"], roles, status, body.note,
                     datetime.now(timezone.utc).isoformat())
    return _event_view(db.get_event(event_id), user, detail=True)


@app.delete("/api/events/{event_id}/signup")
async def withdraw_signup(event_id: int, user: dict = Depends(require_session)):
    """Withdraw the caller from an event (kept as 'withdrawn' so re-joining is easy)."""
    ev = db.get_event(event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="unknown event")
    db.withdraw_signup(event_id, user["id"])
    return _event_view(db.get_event(event_id), user, detail=True)


@app.get("/api/biomes")
async def list_biomes():
    """Biome lookups (by_body / by_system / all) for the biome datalist; the
    UI narrows to the player's current body, falling back to system then all."""
    return biomes


@app.get("/api/custom_pois")
async def list_custom_pois(user: dict | None = Depends(current_user)):
    """Custom POIs visible to the caller — everyone's shared POIs plus the
    caller's own private ones."""
    allowed = viewer_owner_ids(user)
    return [
        d for d in db.list_custom_pois()
        if not d.get("private")
        or (d.get("owner_id") is not None and d["owner_id"] in allowed)
    ]


class PoiEditIn(BaseModel):
    note: str | None = Field(default=None, max_length=_NOTE_MAX)
    private: bool | None = None   # toggle owner-only visibility


@app.patch("/api/custom_pois/{poi_id}")
async def update_custom_poi(poi_id: int, body: PoiEditIn, user: dict = Depends(require_session)):
    """Edit a custom POI's note and/or private flag. Ownership-scoped like
    delete; only custom POIs are editable (upstream POIs carry a read-only
    Comment). Only the supplied fields change."""
    async with hub.lock:
        poi = nav.pois.get(poi_id)
        if poi is None or not getattr(poi, "custom", False):
            raise HTTPException(status_code=404, detail="unknown custom poi")
        ensure_owns(user, poi.owner_id)
        if body.note is not None:
            note = body.note.strip() or None
            db.update_custom_poi_note(poi_id, note)
            poi.note = note
        if body.private is not None and body.private != poi.private:
            db.update_custom_poi_private(poi_id, body.private)
            poi.private = body.private
            # A QT marker going private (or back) changes the shared jump index,
            # so rebuild it + reassign nearest_qt across the dataset.
            if poi.qt_marker:
                nav_core.assign_qt_markers(nav)
        await hub.broadcast_all()
    return {"ok": True, "note": poi.note, "private": poi.private}


@app.delete("/api/custom_pois/{poi_id}")
async def delete_custom_poi(poi_id: int, user: dict = Depends(require_session)):
    async with hub.lock:
        removed = nav.pois.get(poi_id)
        if removed is None or not getattr(removed, "custom", False):
            raise HTTPException(status_code=404, detail="unknown custom poi")
        ensure_owns(user, removed.owner_id)
        was_qt = removed.qt_marker
        db.delete_custom_poi(poi_id)
        nav.pois.pop(poi_id, None)
        # Removing a QT marker leaves other entities pointing at a marker that's
        # gone, so rebuild the index + reassign nearest_qt across the dataset.
        if was_qt:
            nav_core.assign_qt_markers(nav)
        hub.forget_entity(poi_id)
        await hub.broadcast_all()
    return {"ok": True}


@app.get("/api/observations")
async def get_observations(
    q: str = "", category: str | None = None, system: str | None = None,
    container: str | None = None, type: str | None = None,
    owner_id: int | None = None, limit: int = 100,
):
    return nav_core.search_observations(
        nav, query=q, category=category, system=system, container=container,
        type_value=type, owner_id=owner_id, limit=min(limit, 5000),
    )


@app.delete("/api/observations/{obs_id}")
async def delete_observation(obs_id: int, user: dict = Depends(require_session)):
    async with hub.lock:
        obs = nav.observations.get(obs_id)
        if obs is None:
            raise HTTPException(status_code=404, detail="unknown observation")
        ensure_owns(user, obs.owner_id)
        db.delete_observation(obs_id)
        nav.observations.pop(obs_id, None)
        hub.forget_entity(obs_id)
        await hub.broadcast_all()
        return {"ok": True}


@app.get("/api/leaderboard")
async def leaderboard(user: dict = Depends(require_session)):
    """Per-contributor tallies for the Leaderboard page: custom POIs and each
    observation category, counted per player. Players are keyed by their stable
    PlayerID (so a character rename stays one row) and labelled with their
    current handle; ownerless legacy records fold into a single 'Unknown' row."""
    cats = [
        {"key": "poi", "label": "POIs"},
        {"key": "resource", "label": "Resource Nodes"},
        {"key": "wildlife", "label": "Fauna"},
        {"key": "harvestable", "label": "Harvestables"},
    ]
    by_player: dict[str, dict] = {}
    mine_ids = handles.player_ids_for(user["id"])

    def bucket(owner_id, owner_handle):
        if owner_id is not None:
            key = f"id:{owner_id}"
            label = handles.handle_for(owner_id) or owner_handle or f"Player {owner_id}"
        elif owner_handle:
            key = f"h:{owner_handle}"
            label = owner_handle
        else:
            key, label = "unknown", "Unknown"
        row = by_player.get(key)
        if row is None:
            row = by_player[key] = {
                "handle": label,
                "counts": {c["key"]: 0 for c in cats},
                "total": 0,
                "mine": owner_id is not None and owner_id in mine_ids,
            }
        return row

    for poi in db.list_custom_pois():
        row = bucket(poi.get("owner_id"), poi.get("owner_handle"))
        row["counts"]["poi"] += 1
        row["total"] += 1

    for obs in db.list_observations():
        cat = obs.get("category")
        if cat not in ("resource", "wildlife", "harvestable"):
            continue
        row = bucket(obs.get("owner_id"), obs.get("owner_handle"))
        row["counts"][cat] += 1
        row["total"] += 1

    contributors = sorted(
        by_player.values(), key=lambda r: (-r["total"], r["handle"].lower())
    )
    totals = {c["key"]: sum(r["counts"][c["key"]] for r in contributors) for c in cats}
    return {
        "categories": cats,
        "contributors": contributors,
        "totals": totals,
        "grand_total": sum(totals.values()),
    }


# How many entries each "top N" breakdown returns to the Statistics page; the
# response also carries the distinct-count so the UI can say "+N more".
_STATS_TOP_N = 15
# How many trailing weeks the activity sparkline covers.
_STATS_WEEKS = 16


def _top_counter(counter: Counter, limit: int = _STATS_TOP_N) -> dict:
    """A Counter -> {"items": [{"name","count"}, ...top], "distinct": int} shape.
    Distinct is the full key count so the UI can note how many were truncated."""
    items = [{"name": name, "count": n} for name, n in counter.most_common(limit)]
    return {"items": items, "distinct": len(counter)}


def _iso_week_start(dt: datetime) -> datetime:
    """Monday 00:00 (UTC) of the ISO week containing `dt`."""
    d = dt.astimezone(timezone.utc)
    monday = (d - timedelta(days=d.weekday())).date()
    return datetime(monday.year, monday.month, monday.day, tzinfo=timezone.utc)


@app.get("/api/stats")
async def stats(user: dict = Depends(require_session)):
    """Aggregate dataset statistics for the Statistics page: overall totals, the
    spread across bodies / systems / biomes / shards, per-type breakdowns
    (ore, fauna species, harvestables, POI types), resource quality bands, and a
    weekly activity sparkline. Computed live — the dataset is small (org scale),
    so a full scan per page load is cheap and always current.

    POIs are read from the in-memory NavData (the only place the imported
    starmap catalog exists), split by `.custom` into guild-created vs imported.
    Imported POIs are folded into the dataset-wide breakdowns (bodies, systems,
    POI types) as their own dimension, and surfaced separately in the totals so
    the UI can annotate them; the guild-activity metrics (contributors, weekly
    activity, resource/fauna/harvestable observations) stay contribution-only."""
    # Normalize the Poi dataclasses to the same dict shape the observation rows
    # use (note: "container" not "container_name") so one aggregation path serves
    # both. `custom` distinguishes guild-created POIs from the imported catalog.
    # Private POIs are owner-only — keep them out of org-wide stats entirely so
    # neither their count nor their location leaks to the rest of the org.
    all_pois = [
        {"system": p.system, "container": p.container_name,
         "type": p.type or "Custom", "owner_id": p.owner_id,
         "owner_handle": p.owner_handle, "custom": p.custom}
        for p in nav.pois.values()
        if not getattr(p, "private", False)
    ]
    custom_pois = [p for p in all_pois if p["custom"]]
    imported_pois = [p for p in all_pois if not p["custom"]]
    obs = db.list_observations()

    cat_obs = {"resource": [], "wildlife": [], "harvestable": []}
    for o in obs:
        bucket = cat_obs.get(o.get("category"))
        if bucket is not None:
            bucket.append(o)

    def _body_label(r):
        return r.get("container") or "Deep Space"

    # --- coverage: distinct systems / bodies / shards / contributors ---------
    # Coverage spans the whole dataset (imported catalog included); contributors
    # is guild-only (imported POIs carry no owner).
    coverage = all_pois + obs
    systems = {r.get("system") for r in coverage if r.get("system")}
    bodies = {(r.get("system"), r.get("container")) for r in coverage if r.get("container")}
    shards = {o.get("shard_id") for o in obs if o.get("shard_id")}
    contributors = set()
    for r in custom_pois + obs:
        if r.get("owner_id") is not None:
            contributors.add(f"id:{r['owner_id']}")
        elif r.get("owner_handle"):
            contributors.add(f"h:{r['owner_handle']}")

    # --- where: records per body (stacked by category) -----------------------
    by_body: dict[tuple, dict] = {}
    by_system: Counter = Counter()
    by_biome: Counter = Counter()

    def _body_row(r):
        key = (r.get("system"), _body_label(r))
        row = by_body.get(key)
        if row is None:
            row = by_body[key] = {
                "body": key[1], "system": key[0] or "?", "imported": 0,
                "poi": 0, "resource": 0, "wildlife": 0, "harvestable": 0, "total": 0,
            }
        return row

    for p in all_pois:
        row = _body_row(p)
        if p["custom"]:
            row["poi"] += 1
            row["total"] += 1   # `total` ranks by guild activity, not catalog size
        else:
            row["imported"] += 1   # carried as scope context, not part of the rank
        if p.get("system"):
            by_system[p["system"]] += 1
    for o in obs:
        cat = o.get("category")
        if cat not in cat_obs:
            continue
        row = _body_row(o)
        row[cat] += 1
        row["total"] += 1
        if o.get("system"):
            by_system[o["system"]] += 1
        if o.get("biome"):
            by_biome[o["biome"]] += 1

    # Rank by guild contributions ("most-mapped by us"); bodies that only carry
    # imported catalog POIs aren't "mapped" by the guild, so they're left out
    # here (their scope still shows in the systems / coverage / type breakdowns).
    top_bodies = sorted(
        (r for r in by_body.values() if r["total"] > 0),
        key=lambda r: (-r["total"], r["body"].lower()),
    )[:_STATS_TOP_N]

    # --- per-type breakdowns -------------------------------------------------
    ores = Counter(o["data"].get("ore") or "Unknown" for o in cat_obs["resource"])
    species = Counter(o["data"].get("species") or "Unknown" for o in cat_obs["wildlife"])
    harvestables = Counter(o["data"].get("name") or "Unknown" for o in cat_obs["harvestable"])
    # POI types span both guild-created and imported POIs (the imported catalog is
    # where the rich type variety lives). Each item carries the guild subtotal
    # alongside the total so the UI can show how many of each type the org
    # contributed vs imported.
    poi_types_all = Counter(p["type"] for p in all_pois)
    poi_types_guild = Counter(p["type"] for p in custom_pois)
    poi_types = {
        "items": [
            {"name": name, "count": n, "guild": poi_types_guild.get(name, 0)}
            for name, n in poi_types_all.most_common(_STATS_TOP_N)
        ],
        "distinct": len(poi_types_all),
    }

    # --- resource quality bands (B1..B8 + Unknown) ---------------------------
    bands = Counter()
    for o in cat_obs["resource"]:
        b = o["data"].get("band")
        try:
            bands[str(max(1, min(8, int(b))))] += 1
        except (TypeError, ValueError):
            bands["Unk"] += 1
    band_series = [{"band": f"B{n}", "count": bands.get(str(n), 0)} for n in range(1, 9)]
    band_series.append({"band": "Unk", "count": bands.get("Unk", 0)})

    # --- weekly activity (observations carry a timestamp; POIs don't) --------
    weeks: Counter = Counter()
    for o in obs:
        ts = o.get("observed_at")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        weeks[_iso_week_start(dt)] += 1
    activity = []
    if weeks:
        end = _iso_week_start(datetime.now(timezone.utc))
        start = end - timedelta(weeks=_STATS_WEEKS - 1)
        wk = start
        while wk <= end:
            activity.append({
                "label": wk.strftime("%b %d"),
                "count": weeks.get(wk, 0),
            })
            wk += timedelta(weeks=1)

    return {
        "totals": {
            "poi": len(custom_pois),
            "poi_imported": len(imported_pois),
            "resource": len(cat_obs["resource"]),
            "wildlife": len(cat_obs["wildlife"]),
            "harvestable": len(cat_obs["harvestable"]),
            "observations": len(obs),
            # Guild contributions only (imported catalog excluded).
            "records": len(custom_pois) + len(obs),
            "contributors": len(contributors),
            "systems": len(systems),
            "bodies": len(bodies),
            "shards": len(shards),
        },
        # Whether the imported starmap POI catalog is loaded, so the UI knows to
        # show the imported annotations even when the count happens to be 0.
        "catalog_enabled": starmap_pois_enabled(),
        "top_bodies": top_bodies,
        "systems": _top_counter(by_system),
        "ores": _top_counter(ores),
        "species": _top_counter(species),
        "harvestables": _top_counter(harvestables),
        "biomes": _top_counter(by_biome),
        "poi_types": poi_types,
        "bands": band_series,
        "activity": activity,
    }


@app.post("/api/path/{action}")
async def path_control(action: str, user: dict = Depends(require_session)):
    if action not in ("start", "stop", "clear"):
        raise HTTPException(status_code=404, detail="unknown path action")
    async with hub.lock:
        sess = hub.get(user)
        if action == "start":
            sess.tracking = True
        elif action == "stop":
            sess.tracking = False
        else:  # clear
            sess.path.clear()
        if sess.nav_state is not None:
            sess._attach_breadcrumbs()
        await sess.broadcast()
    return {"ok": True, "tracking": sess.tracking, "crumbs": len(sess.path)}


async def _rebuild_nav() -> None:
    """Rebuild NavData (upstream catalog + DB customs/observations) and swap it
    in. Used by /api/refresh and when org settings change."""
    global nav
    fresh = await asyncio.to_thread(load_nav_data)
    nav_core.merge_custom_pois(fresh, db.list_custom_pois())
    merge_all_observations(fresh)
    nav_core.assign_qt_markers(fresh)
    async with hub.lock:
        nav = fresh
        for s in hub.sessions.values():
            if (s.destination_id is not None
                    and s.destination_id not in nav.pois
                    and s.destination_id not in nav.observations):
                s.destination_id = None
        await hub.broadcast_all()


@app.post("/api/refresh")
async def refresh_data(admin: dict = Depends(require_admin)):
    """Re-fetch the dataset (starmap) and the commodities list (uexcorp)
    without restarting. Admin only."""
    global raw_commodity_names, commodity_names, ships
    raw_commodity_names = await asyncio.to_thread(load_raw_commodity_names)
    commodity_names = await asyncio.to_thread(load_commodity_names)
    ships = await asyncio.to_thread(load_ships)
    await _rebuild_nav()
    return {
        "ok": True,
        "data": data_info,
        "containers": len(nav.containers),
        "pois": len(nav.pois),
        "observations": len(nav.observations),
        "raw_commodities": len(raw_commodity_names),
        "harvestables": len(harvestable_names),
        "ships": len(ships),
    }


@app.post("/api/admin/stats/resources/clear")
async def clear_resource_stats(admin: dict = Depends(require_admin)):
    """Wipe every resource/wildlife/harvestable sighting org-wide (admin only):
    zeroes the Statistics page's contribution metrics and the resource
    leaderboard. Custom POIs and QT markers are kept. The in-memory dataset is
    rebuilt afterwards so the change is live without a restart (and any session
    aimed at a now-deleted sighting is cleared)."""
    deleted = await asyncio.to_thread(db.clear_observations)
    await _rebuild_nav()
    return {"ok": True, "deleted": deleted, "observations": len(nav.observations)}


@app.post("/api/admin/stats/cargo/clear")
async def clear_cargo_stats(admin: dict = Depends(require_admin)):
    """Wipe finished hauling runs org-wide (admin only): zeroes the cargo
    leaderboard, hauling stats, and every member's run history. In-progress
    (active) runs are left running."""
    deleted = await asyncio.to_thread(db.clear_run_history)
    return {"ok": True, "deleted": deleted}


@app.get("/api/settings")
async def get_settings(user: dict = Depends(require_session)):
    """Org-wide settings (any member can read; admins change them)."""
    return {
        "starmap_pois_enabled": starmap_pois_enabled(),
        "member_role_id": member_role_id(),
        "obs_fresh_window_h": obs_fresh_window_h(),
        "extra_admin_ids": extra_admin_ids(),       # DB-backed, editable here
        "root_admin_ids": sorted(auth.ADMIN_IDS),   # env, read-only floor
        "org_logo": bool(db.get_setting("org_logo_ext")),
    }


class SettingsIn(BaseModel):
    starmap_pois_enabled: bool | None = None
    member_role_id: str | None = Field(default=None, max_length=_META_MAX)
    obs_fresh_window_h: int | None = Field(default=None, ge=1, le=8760)  # 1h .. 1yr
    # Discord snowflakes are ~17-20 digits; cap the list so the admin form can't
    # be used to stuff the meta table. Each id is validated (isdigit) below.
    extra_admin_ids: list[str] | None = Field(default=None, max_length=200)


@app.post("/api/settings")
async def update_settings(body: SettingsIn, admin: dict = Depends(require_admin)):
    """Update org settings (admin only). Only the fields present are changed.
    Toggling the POI catalog rebuilds the dataset; the member-role gate takes
    effect at the next login (existing sessions stand until they expire); the
    freshness window is display-only and applies on the clients' next refresh."""
    if body.member_role_id is not None:
        db.set_setting("member_role_id", body.member_role_id.strip())
    if body.extra_admin_ids is not None:
        cleaned, seen = [], set()
        for raw in body.extra_admin_ids:
            s = (raw or "").strip()
            if not s or s in seen:
                continue
            if not s.isdigit() or len(s) > 20:   # Discord ids are numeric snowflakes (<=20 digits)
                raise HTTPException(status_code=400,
                                    detail=f"invalid Discord id: {s!r}")
            seen.add(s)
            if s not in auth.ADMIN_IDS:   # root admins are implicit; don't store dupes
                cleaned.append(s)
        # The env root admins are the floor; only block a change that would
        # leave the whole org with no admin at all (possible only when ADMIN_IDS
        # is unset), which would be an unrecoverable lockout.
        if not (auth.ADMIN_IDS or cleaned):
            raise HTTPException(status_code=400, detail="can't remove the last admin")
        db.set_setting("extra_admin_ids", ",".join(cleaned))
    if body.obs_fresh_window_h is not None:
        db.set_setting("obs_fresh_window_h", str(max(1, body.obs_fresh_window_h)))
    if body.starmap_pois_enabled is not None:
        db.set_setting("starmap_pois_enabled", "1" if body.starmap_pois_enabled else "0")
        await _rebuild_nav()
    return {"ok": True, "starmap_pois_enabled": starmap_pois_enabled(),
            "member_role_id": member_role_id(),
            "obs_fresh_window_h": obs_fresh_window_h(),
            "extra_admin_ids": extra_admin_ids(),
            "root_admin_ids": sorted(auth.ADMIN_IDS), "pois": len(nav.pois)}


@app.get("/api/org-logo")
async def get_org_logo():
    """Serve the org's uploaded logo (shown alongside the built-in one in the
    header and on the login splash). Public so it can render pre-auth; the
    auth_gate middleware exempts this GET."""
    ext = db.get_setting("org_logo_ext")
    if ext:
        path = BRANDING_DIR / f"org_logo.{ext}"
        if path.is_file():
            return FileResponse(path)
    raise HTTPException(status_code=404, detail="no org logo")


@app.post("/api/org-logo")
async def upload_org_logo(file: UploadFile = File(...),
                          admin: dict = Depends(require_admin)):
    """Replace the org's custom logo (admin). Validates by Content-Type and caps
    size; writes to the /data volume and records the extension in `meta`."""
    ext = _LOGO_TYPES.get((file.content_type or "").lower())
    if not ext:
        raise HTTPException(status_code=400, detail="logo must be a PNG, JPG, or WebP image")
    data = await file.read(_LOGO_MAX_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="the file is empty")
    if len(data) > _LOGO_MAX_BYTES:
        raise HTTPException(status_code=400, detail="logo too large (max 2 MB)")
    # Verify the bytes actually match the claimed type — the Content-Type header
    # is client-supplied, so don't trust it to keep e.g. an HTML/script polyglot
    # off the /data volume.
    if not _sniff_image(data, ext):
        raise HTTPException(status_code=400,
                            detail="file contents don't match a PNG, JPG, or WebP image")
    BRANDING_DIR.mkdir(parents=True, exist_ok=True)
    # Drop any prior logo (possibly a different extension) so none is orphaned.
    for old in BRANDING_DIR.glob("org_logo.*"):
        old.unlink(missing_ok=True)
    (BRANDING_DIR / f"org_logo.{ext}").write_bytes(data)
    db.set_setting("org_logo_ext", ext)
    return {"ok": True, "org_logo": True}


@app.delete("/api/org-logo")
async def delete_org_logo(admin: dict = Depends(require_admin)):
    """Remove the org's custom logo (admin). The built-in logo always remains."""
    for old in BRANDING_DIR.glob("org_logo.*"):
        old.unlink(missing_ok=True)
    db.set_setting("org_logo_ext", "")
    return {"ok": True, "org_logo": False}


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "version": APP_VERSION,
        "containers": len(nav.containers),
        "pois": len(nav.pois),
        "observations": len(nav.observations),
        "handles": len(handles.by_handle),
        "raw_commodities": len(raw_commodity_names),
        "harvestables": len(harvestable_names),
        "ships": len(ships),
        "active_sessions": sum(1 for s in hub.sessions.values() if s.pos is not None),
        "data": data_info,
    }


def _ws_origin_ok(ws: WebSocket) -> bool:
    """Reject a cross-origin WebSocket handshake. SameSite=Lax already keeps a
    script-initiated cross-site handshake from carrying the session cookie, but
    an explicit Origin check is cheap defense-in-depth against socket hijacking.
    A same-origin browser sends Origin == its own scheme://host; non-browser
    clients (no Origin) are allowed since the cookie gate still applies."""
    origin = ws.headers.get("origin")
    if not origin:
        return True
    if PUBLIC_BASE_URL and origin.rstrip("/") == PUBLIC_BASE_URL:
        return True
    host = ws.headers.get("x-forwarded-host") or ws.headers.get("host")
    if host:
        try:
            return urllib.parse.urlparse(origin).netloc == host
        except ValueError:
            return False
    return False


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # Browsers only; require a logged-in org member (session loaded from cookie).
    if not _ws_origin_ok(ws):
        await ws.close(code=1008)   # policy violation (cross-origin)
        return
    user = ws.session.get("user")
    if not user:
        await ws.close(code=1008)   # policy violation
        return
    await ws.accept()
    sess = hub.get(user)
    was_offline = not sess.ws_clients   # first tab for this member?
    sess.ws_clients.add(ws)
    try:
        # Send this member's current state immediately so the UI isn't blank
        # until their next /showlocation.
        await ws.send_text(
            json.dumps(
                {
                    "type": "state",
                    "data": sess.nav_state,
                    "capture": sess.capture_status(),
                }
            )
        )
        # Initial teammate snapshot so the new tab's map/roster start populated
        # (later changes arrive as throttled presence deltas).
        async with hub.lock:
            roster = hub.roster()
        await ws.send_text(json.dumps({"type": "roster", "users": roster}))
        # Tell the new tab the current count immediately; tell everyone else only
        # when this member actually came online (a 2nd/3rd tab doesn't change it).
        if was_offline:
            await hub.broadcast_online()
        else:
            await ws.send_text(json.dumps({"type": "online", "count": hub.online_count()}))
        while True:
            await ws.receive_text()  # client pings; content ignored
    except WebSocketDisconnect:
        pass
    finally:
        sess.ws_clients.discard(ws)
        if not sess.ws_clients:   # member's last tab closed — they went offline
            await hub.broadcast_online()


# ---------------------------------------------------------------------------
# Discord OAuth gate (Phase 0)
# ---------------------------------------------------------------------------
# Login + org-membership check + signed session for browsers; bearer watcher
# tokens for the headless watcher. The auth_gate middleware enforces "any /api/*
# needs one of these" centrally; the dependencies below add the finer checks
# (session-only, admin-only).


# The OAuth CSRF token rides in its own short-lived cookie rather than the Lax
# session, because Discord's "Authorize" button is a cross-site POST that
# redirects to /auth/callback. Chrome's "Lax+POST" grace still sends a Lax
# cookie there, but Safari/WebKit (iPhone) does not — so a Lax session lost the
# state and every mobile login 400'd with "invalid OAuth state". SameSite=None
# (only valid alongside Secure, i.e. over HTTPS) is sent on that redirect. Kept
# separate from the session cookie so the rest of the app stays Lax.
OAUTH_STATE_COOKIE = "oauth_state"


def _set_oauth_state_cookie(resp: Response, state: str) -> None:
    resp.set_cookie(
        OAUTH_STATE_COOKIE, state,
        max_age=600, httponly=True, secure=COOKIE_SECURE,
        samesite="none" if COOKIE_SECURE else "lax", path="/auth",
    )


def _clear_oauth_state_cookie(resp: Response) -> None:
    resp.delete_cookie(OAUTH_STATE_COOKIE, path="/auth")


@app.get("/auth/login")
async def auth_login(request: Request):
    if not auth.configured():
        raise HTTPException(status_code=503, detail="Discord login is not configured")
    state = secrets.token_urlsafe(24)
    resp = RedirectResponse(auth.authorize_url(state))
    _set_oauth_state_cookie(resp, state)
    return resp


@app.get("/auth/callback")
async def auth_callback(request: Request, code: str = "", state: str = ""):
    expected = request.cookies.get(OAUTH_STATE_COOKIE)
    if not state or state != expected:
        raise HTTPException(status_code=400, detail="invalid OAuth state")
    if not code:
        raise HTTPException(status_code=400, detail="missing authorization code")
    try:
        token = await asyncio.to_thread(auth.exchange_code, code)
        profile, denied = await asyncio.to_thread(
            auth.fetch_member_profile, token, member_role_id(), admin_ids())
    except Exception as exc:
        # A urllib HTTPError carries Discord's JSON error body (e.g.
        # {"error":"invalid_client"}); read it and log to stdout so the real
        # reason shows up in `docker logs`, not just an opaque 502.
        body = ""
        if hasattr(exc, "read"):
            try:
                body = exc.read().decode("utf-8", "replace")[:500]
            except Exception:
                pass
        print(f"[sc-nav] auth callback failed: {exc!r} {body}", flush=True)
        traceback.print_exc()
        # Detail (which may carry Discord's raw error body) is logged above only;
        # the client gets a generic message so we don't disclose internals.
        raise HTTPException(status_code=502, detail="Discord authentication failed; please try again.")
    if profile is None:
        request.session.clear()
        html = auth.MISSING_ROLE_HTML if denied == "missing_role" else auth.NOT_IN_ORG_HTML
        resp = HTMLResponse(html, status_code=403)
        _clear_oauth_state_cookie(resp)
        return resp
    request.session["user"] = profile
    resp = RedirectResponse("/")
    _clear_oauth_state_cookie(resp)
    return resp


@app.post("/auth/logout")
async def auth_logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/me")
async def api_me(request: Request):
    """The signed-in org member (or 401). Drives the UI's account state. Carries
    the live presence-share flag so the UI's toggle reflects the current state."""
    user = require_session(request)
    return {**user, "share_presence": hub.get(user).share_presence,
            "org_logo": bool(db.get_setting("org_logo_ext")),
            "ships": db.list_user_ships(user["id"])}


class ProfileIn(BaseModel):
    share_presence: bool | None = None


class ShipPrefIn(BaseModel):
    """A member's learned usable-SCU for a ship (the cargo-planner override)."""
    name: str = Field(min_length=1, max_length=_NAME_MAX)
    usable_scu: float = Field(ge=0, le=100_000)


@app.put("/api/me")
async def update_me(body: ProfileIn, user: dict = Depends(require_session)):
    """Update the caller's profile. For now just the presence-share toggle:
    turning it off emits a `remove` and stops broadcasting the member (one-way —
    they keep receiving teammates); turning it on re-publishes their last fix."""
    async with hub.lock:
        sess = hub.get(user)
        if body.share_presence is not None:
            sess.share_presence = body.share_presence
            hub.touch_presence(sess)   # re-publish, or drop if now off / not on a body
        return {"ok": True, "share_presence": sess.share_presence}


@app.delete("/api/me")
async def delete_me(request: Request, user: dict = Depends(require_session)):
    """Self-service account deletion (Privacy Policy). Erases the caller's
    personal data — watcher tokens, saved ships, cargo runs, handle->Discord
    bindings, hauling-session marker — and de-identifies their contributed POIs/
    sightings (kept for the org, stripped of owner). The browser session is
    cleared so they're signed out; signing in again just creates a fresh, empty
    account (deletion erases data, it doesn't ban the Discord member)."""
    uid = user["id"]
    async with hub.lock:
        player_ids = handles.player_ids_for(uid)
        counts = db.delete_member(uid, player_ids)

        # Mirror the DB changes in the in-memory caches so nothing stale survives
        # until the next restart. 1) Drop this member's private POIs outright, then
        # de-identify the rest of their live contributions.
        for poi in [p for p in nav.pois.values()
                    if p.owner_id in player_ids and getattr(p, "private", False)]:
            nav.pois.pop(poi.id, None)
            hub.forget_entity(poi.id)
        for poi in nav.pois.values():
            if poi.owner_id in player_ids:
                poi.owner_id = poi.owner_handle = None
        for obs in nav.observations.values():
            if obs.owner_id in player_ids:
                obs.owner_id = obs.owner_handle = None
        # 2) Forget their handle bindings + watcher tokens.
        handles.by_handle = {h: e for h, e in handles.by_handle.items()
                             if e.get("discord_id") != uid}
        tokens.items = [t for t in tokens.items if t["discord_id"] != uid]
        # 3) Drop their live presence + session so teammates see them leave.
        hub.drop_presence(uid)
        hub.sessions.pop(uid, None)
        await hub.broadcast_all()
    await hub.broadcast_online()
    request.session.clear()
    return {"ok": True, "deleted": counts}


@app.put("/api/me/ship")
async def remember_ship(body: ShipPrefIn, user: dict = Depends(require_session)):
    """Save (or update) the caller's usable-SCU for a ship and mark it most
    recently used. Returns the caller's saved fleet, freshest first."""
    db.upsert_user_ship(user["id"], body.name.strip(), body.usable_scu,
                        datetime.now(timezone.utc).isoformat())
    return {"ok": True, "ships": db.list_user_ships(user["id"])}


@app.delete("/api/me/ship")
async def forget_ship(name: str, user: dict = Depends(require_session)):
    """Drop a saved ship from the caller's fleet."""
    if not db.delete_user_ship(user["id"], name.strip()):
        raise HTTPException(status_code=404, detail="no such saved ship")
    return {"ok": True, "ships": db.list_user_ships(user["id"])}


class TokenCreateIn(BaseModel):
    label: str = Field(default="watcher", max_length=_LABEL_MAX)


@app.post("/api/tokens")
async def create_token(request: Request, body: TokenCreateIn):
    """Mint a watcher token for the signed-in member. The raw token is returned
    once and never stored in the clear."""
    user = require_session(request)
    raw, public = tokens.mint(user["id"], user.get("display_name"), body.label)
    return {"token": raw, **public}


@app.get("/api/tokens")
async def list_tokens(request: Request):
    user = require_session(request)
    return tokens.list_for(user["id"])


@app.delete("/api/tokens/{token_id}")
async def delete_token(request: Request, token_id: str):
    user = require_session(request)
    if not tokens.revoke(token_id, user["id"], user.get("is_admin", False)):
        raise HTTPException(status_code=404, detail="unknown token")
    return {"ok": True}


def _server_base_url(request: Request) -> str:
    """Public base URL the watcher should POST to.

    Prefer the explicitly configured SC_NAV_PUBLIC_URL — the watcher zip bakes a
    freshly minted token into the bundled bat's SERVER=, so an attacker who could
    spoof X-Forwarded-Host (reachable only if the app is exposed off-tunnel)
    could otherwise redirect a victim's watcher — and its token — to their own
    server. Falling back to the forwarded headers keeps dev/no-config working."""
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = (request.headers.get("x-forwarded-host")
            or request.headers.get("host")
            or request.url.netloc)
    return f"{scheme}://{host}"


def _build_watcher_zip(base_url: str, token: str) -> bytes:
    """Zip the watcher up with this member's setup baked in: the bat is pointed
    at `base_url`, and `watcher_config.json` carries the token so the script
    authenticates with zero typing (the watcher reads token stickily from there).
    Returned under a top-level `watcher/` folder for a clean unzip."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in WATCHER_BUNDLE_FILES:
            src = WATCHER_DIR / name
            if not src.is_file():
                continue
            text = src.read_text(encoding="utf-8")
            if name == "run_watcher.bat":
                # Replace the SERVER= line (whatever address it was committed with)
                # with this deployment's URL so the user never edits the bat.
                text = re.sub(r"(?m)^set SERVER=.*$", f"set SERVER={base_url}", text)
            zf.writestr(f"watcher/{name}", text)
        # The token lives in watcher_config.json, the script's sticky-config file.
        zf.writestr("watcher/watcher_config.json", json.dumps({"token": token}))
    return buf.getvalue()


@app.post("/download/watcher")
async def download_watcher(request: Request):
    """Mint a token for the signed-in member and stream back a personalized,
    ready-to-run watcher bundle (Setup page, step 2). The token is baked into the
    zip rather than put in a URL, so it never lands in a log or browser history."""
    user = require_session(request)
    if WATCHER_DIR is None:
        raise HTTPException(status_code=503, detail="watcher bundle unavailable")
    raw, _ = tokens.mint(user["id"], user.get("display_name"), "watcher download")
    data = _build_watcher_zip(_server_base_url(request), raw)
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="watcher.zip"'},
    )


# The SPA shell is served through a route (not the StaticFiles mount) so the
# per-request CSP nonce can be stamped onto its single inline <script>. Routing
# is hash-based, so "/" is the only path that serves the shell; "/index.html" is
# handled too in case it's hit directly. Read per request so a dev edit shows up
# without a restart (matching StaticFiles); the file is small and only read on a
# full page load, not per API call. All other assets fall through to the mount.
INDEX_FILE = STATIC_DIR / "index.html"


def _index_response(request: Request) -> HTMLResponse:
    nonce = getattr(request.state, "csp_nonce", "")
    html = (
        INDEX_FILE.read_text(encoding="utf-8")
        .replace("<script>", f'<script nonce="{nonce}">', 1)
        # Stamp the running version into the footer (placeholder degrades to the
        # current version; the string is our own SemVer, so no escaping needed).
        .replace("{{APP_VERSION}}", APP_VERSION)
    )
    return HTMLResponse(html)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return _index_response(request)


@app.get("/index.html", response_class=HTMLResponse)
async def index_html(request: Request):
    return _index_response(request)


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
