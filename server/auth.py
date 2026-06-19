"""Discord OAuth gate for SC Nav (multi-user Phase 0).

Locks the app to a single Discord guild: a user can sign in only if they're a
member of `ORG_GUILD_ID`. Identity is the **Discord user id** (permanent); the
RSI handle stays cosmetic elsewhere. `ADMIN_IDS` are the immutable root admins;
more can be granted from the UI (app.py `admin_ids()`, DB-backed).

This module is just the login + membership check + config. App state is still
global at this phase; per-user sessions come later. The signed session cookie
itself is handled by Starlette's SessionMiddleware in app.py.

Config comes from the environment (see .env):
  DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET, OAUTH_REDIRECT_URI, ORG_GUILD_ID,
  ADMIN_IDS (comma-separated discord ids).
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

DISCORD_API = "https://discord.com/api"
AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
TOKEN_URL = f"{DISCORD_API}/oauth2/token"
# identify -> who they are; guilds -> membership; guilds.members.read -> the
# per-guild member object (incl. the user's role ids) without needing a bot.
OAUTH_SCOPES = "identify guilds guilds.members.read"

CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "")
GUILD_ID = os.environ.get("ORG_GUILD_ID", "")
ADMIN_IDS = {x.strip() for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
# Seed default for the required member role; the live value is DB-backed and
# admin-editable (app.py meta `member_role_id`). Empty = any guild member.
MEMBER_ROLE_ID = os.environ.get("ORG_MEMBER_ROLE_ID", "")


def configured() -> bool:
    """True when enough is set to run the OAuth flow."""
    return all([CLIENT_ID, CLIENT_SECRET, REDIRECT_URI, GUILD_ID])


def authorize_url(state: str) -> str:
    """Discord consent URL to redirect the browser to. `state` is the CSRF
    token we stash in the session and re-check on callback."""
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": OAUTH_SCOPES,
        "state": state,
    })


def _post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "sc-nav/1.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def _get(url: str, access_token: str):
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {access_token}",
                      "User-Agent": "sc-nav/1.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def exchange_code(code: str) -> str:
    """Trade the OAuth code for a user access token. Blocking — call via a
    thread from async handlers."""
    tok = _post_form(TOKEN_URL, {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    })
    return tok["access_token"]


def fetch_member_profile(access_token: str, required_role_id: str = "",
                         admin_ids: set[str] | None = None) -> tuple[dict | None, str | None]:
    """Resolve the signed-in user to an org-member profile.

    Returns (profile, None) on success, or (None, reason) when denied:
      "not_member"   -> not in the guild
      "missing_role" -> in the guild but lacks the required role

    Roles come from the per-guild member object (needs the guilds.members.read
    scope; no bot). Admins bypass the role requirement so a mis-set role can
    never lock out the people who fix it. `admin_ids` is the effective admin set
    (env root admins + the DB-backed list); it falls back to the env ADMIN_IDS
    when not supplied. Blocking — call via a thread."""
    admins = ADMIN_IDS if admin_ids is None else admin_ids
    me = _get(f"{DISCORD_API}/users/@me", access_token)
    try:
        member = _get(f"{DISCORD_API}/users/@me/guilds/{GUILD_ID}/member", access_token)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None, "not_member"   # not a guild member
        raise
    uid = str(me["id"])
    is_admin = uid in admins
    roles = {str(r) for r in (member.get("roles") or [])}
    if required_role_id and required_role_id not in roles and not is_admin:
        return None, "missing_role"
    return {
        "id": uid,
        "username": me.get("username"),
        "display_name": me.get("global_name") or me.get("username"),
        "avatar": me.get("avatar"),
        "is_admin": is_admin,
    }, None


_DENIED_PAGE = """<!doctype html><meta charset="utf-8">
<title>SC Nav — access denied</title>
<body style="background:#0b0e13;color:#d8e1ee;font-family:system-ui;
  display:grid;place-items:center;height:100vh;margin:0;text-align:center">
<div><h1 style="color:#ef5350">{title}</h1>
<p>{body}</p>
<p><a style="color:#4fc3f7" href="/auth/login">Try a different account</a></p>
</div></body>"""

NOT_IN_ORG_HTML = _DENIED_PAGE.format(
    title="Not in the org",
    body="Your Discord account isn't a member of this organization's server, "
         "so you can't access SC Nav.")

MISSING_ROLE_HTML = _DENIED_PAGE.format(
    title="Missing the required role",
    body="You're in the server but don't have the role required to use SC Nav. "
         "Ask an admin to grant it, then sign in again.")
