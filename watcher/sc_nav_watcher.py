#!/usr/bin/env python3
"""SC Nav Watcher.

Polls the system clipboard for Star Citizen `/showlocation` output and
forwards parsed coordinates to the nav server as JSON over HTTP.

Designed to run on the Windows gaming PC. Uses only the Python standard
library so there is nothing to install beyond Python itself. Also runs on
macOS/Linux (via pbpaste/xclip/wl-paste) for development and testing.

Usage:
    python sc_nav_watcher.py --server http://192.168.1.50:8765
    python sc_nav_watcher.py --dry-run            # print instead of sending
    python sc_nav_watcher.py --once --dry-run     # single read, then exit
"""

import argparse
import collections
import importlib
import json
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

POSITION_ENDPOINT = "/api/position"
# Handle registration. Binding also rides every position post, but positions only
# happen once the player runs /showlocation in-game — pinging this at startup is
# what makes the web UI's IN-GAME IDENTITY panel fill in right away.
HANDLE_ENDPOINT = "/api/handle"

# ---------------------------------------------------------------------------
# Clipboard access
# ---------------------------------------------------------------------------


class WindowsClipboard:
    """Clipboard reader using the Win32 API via ctypes.

    GetClipboardSequenceNumber lets us detect *every* copy event cheaply,
    including re-copies of identical text (e.g. running /showlocation twice
    while stationary), without opening the clipboard on each poll.
    """

    CF_UNICODETEXT = 13

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.CloseClipboard.restype = wintypes.BOOL
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL

        self._user32 = user32
        self._kernel32 = kernel32

    def sequence_number(self):
        return self._user32.GetClipboardSequenceNumber()

    def read_text(self):
        # The game (or another app) may hold the clipboard; treat failure to
        # open as "nothing new" and let the next poll retry.
        if not self._user32.OpenClipboard(None):
            return None
        try:
            handle = self._user32.GetClipboardData(self.CF_UNICODETEXT)
            if not handle:
                return None
            ptr = self._kernel32.GlobalLock(handle)
            if not ptr:
                return None
            try:
                return self._ctypes.wstring_at(ptr)
            finally:
                self._kernel32.GlobalUnlock(handle)
        finally:
            self._user32.CloseClipboard()


class CommandClipboard:
    """Fallback clipboard reader for macOS/Linux dev machines.

    No sequence number available, so change detection falls back to
    comparing text (handled by the watcher loop).
    """

    def __init__(self):
        if platform.system() == "Darwin":
            self._cmd = ["pbpaste"]
        elif shutil.which("wl-paste"):
            self._cmd = ["wl-paste", "--no-newline"]
        elif shutil.which("xclip"):
            self._cmd = ["xclip", "-selection", "clipboard", "-o"]
        else:
            raise RuntimeError(
                "No clipboard tool found (need pbpaste, wl-paste, or xclip)"
            )

    def sequence_number(self):
        return None

    def read_text(self):
        try:
            result = subprocess.run(
                self._cmd, capture_output=True, text=True, timeout=2
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        return result.stdout if result.returncode == 0 else None


def make_clipboard():
    if platform.system() == "Windows":
        return WindowsClipboard()
    return CommandClipboard()


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

# /showlocation output looks like:
#   Coordinates: x:-18930539540.392 y:-2610158765.392 z:0.0
# The exact label and separators have shifted between patches, so match each
# axis independently and tolerate commas, '=' separators, and reordering.
_AXIS_PATTERNS = {
    axis: re.compile(
        rf"\b{axis}\s*[:=]\s*(-?[\d,]+(?:\.\d+)?)", re.IGNORECASE
    )
    for axis in ("x", "y", "z")
}


def parse_showlocation(text):
    """Extract x/y/z (meters) from clipboard text, or None if not present."""
    if not text or len(text) > 4096:
        return None
    coords = {}
    for axis, pattern in _AXIS_PATTERNS.items():
        match = pattern.search(text)
        if not match:
            return None
        try:
            coords[axis] = float(match.group(1).replace(",", ""))
        except ValueError:
            return None
    return coords


# ---------------------------------------------------------------------------
# Shard detection (Game.log)
# ---------------------------------------------------------------------------

# Star Citizen writes its current shard to Game.log. Two lines carry it:
#   <Join PU> address[..] port[..] shard[pub_use1b_12030094_130] locationId[..]
#   <Update Shard Id> New Shard Id: pub_use1b_12030094_130. Old Shard Id [..]
# The "Update Shard Id" line re-fires on every shard change (relog / mesh
# handoff), so tailing the log keeps `current_shard` correct across a session.
_SHARD_UPDATE_RE = re.compile(r"New Shard Id:\s*([^\s.]+)")
_SHARD_JOIN_RE = re.compile(r"<Join PU>.*?\bshard\[([^\]]+)\]")

# Common install locations, checked in order when --game-log isn't given. The
# live build is by far the most common; PTU/EPTU are there for power users.
_DEFAULT_LOG_CANDIDATES = (
    r"C:\Program Files\Roberts Space Industries\StarCitizen\LIVE\Game.log",
    r"C:\Program Files\Roberts Space Industries\StarCitizen\PTU\Game.log",
    r"C:\Program Files\Roberts Space Industries\StarCitizen\EPTU\Game.log",
)


def default_game_log():
    """First existing Game.log among the common install paths, or None."""
    for path in _DEFAULT_LOG_CANDIDATES:
        if os.path.isfile(path):
            return path
    return None


# Trade transaction capture (#41, docs/trade-transaction-capture.md §2).
# Commodity kiosks log both sides of every trade (verified in-game 2026-08-02):
#   <ts> [Notice] <CEntityComponentCommodityUIProvider::SendCommodityBuyRequest>
#     ... shopName[X] ... price[98840.000000] shopPricePerCentiSCU[34.082699]
#     ... resourceGUID[uuid] autoLoading[0] quantity[2900.000000 cSCU]
#     Cargo Box Data: boxSize[1.000000] | unitAmount[29] ...
#   <ts> [Notice] <...::SendCommoditySellRequest> ... shopName[X] ...
#     amount[161211.000000] ... resourceGUID[uuid] autoLoading[1] quantity[29]
#     ... Cargo Box Data:  [boxSize[1] | unitAmount[29]] ...
# Buy quantities are centi-SCU; sell quantities are plain SCU. Parsed
# defensively: a game patch that rewords these lines turns the feature off,
# never breaks the watcher.
_TXN_TS_RE = re.compile(r"^<(\d{4}-\d{2}-\d{2}T[0-9:.]+Z)>")
_TXN_BUY_RE = re.compile(
    r"<CEntityComponentCommodityUIProvider::SendCommodityBuyRequest>.*?"
    r"shopName\[([^\]]+)\].*?price\[([\d.]+)\].*?"
    r"shopPricePerCentiSCU\[([\d.]+)\].*?resourceGUID\[([0-9a-fA-F-]{8,})\].*?"
    r"quantity\[([\d.]+)\s*cSCU\]")
_TXN_SELL_RE = re.compile(
    r"<CEntityComponentCommodityUIProvider::SendCommoditySellRequest>.*?"
    r"shopName\[([^\]]+)\].*?amount\[([\d.]+)\].*?"
    r"resourceGUID\[([0-9a-fA-F-]{8,})\].*?quantity\[([\d.]+)\]")

# The same lines also say HOW the cargo moves, which is what a loading-time
# model needs: `autoLoading[0|1]` (0 = freight elevator / hand-load, 1 = the
# kiosk moves it to or from the ship) and the box breakdown `boxSize[n] |
# unitAmount[n]` (SCU per box × box count). Box count matters more than SCU:
# cargo moves a box at a time, so 32 × 1 SCU and 1 × 32 SCU are the same load
# and almost certainly not the same wait. Captured now, modelled later.
#
# Deliberately separate optional searches rather than groups on the two
# regexes above: the field set differs per side already (the sell line wraps
# the box data in its own brackets and drops the trailing zeros), so a patch
# that rewords one of them must cost us that field, not the transaction.
_TXN_AUTOLOAD_RE = re.compile(r"autoLoading\[([01])\]")
_TXN_BOX_SIZE_RE = re.compile(r"boxSize\[([\d.]+)\]")
_TXN_BOX_COUNT_RE = re.compile(r"unitAmount\[([\d.]+)\]")


def _txn_cargo_handling(line):
    """The optional loading-method fields off a transaction line, as a dict
    holding only the ones actually present (never raises, never blocks)."""
    out = {}
    m = _TXN_AUTOLOAD_RE.search(line)
    if m:
        out["auto_load"] = m.group(1) == "1"
    for key, rx, cast in (("box_size", _TXN_BOX_SIZE_RE, float),
                          ("box_count", _TXN_BOX_COUNT_RE, lambda s: int(float(s)))):
        m = rx.search(line)
        if m:
            try:
                out[key] = cast(m.group(1))
            except ValueError:
                pass
    return out


def parse_trade_txn(line):
    """One Game.log line -> a transaction dict shaped for
    POST /api/trade/transactions, or None. Pure — unit-tested in test_parse.py
    against the lines captured from a real, independently-verified trade."""
    ts = _TXN_TS_RE.match(line)
    if not ts:
        return None                    # every real transaction line is stamped
    t = ts.group(1)
    m = _TXN_BUY_RE.search(line)
    if m:
        shop, total, per_centi, guid, centi = m.groups()
        try:
            total, per_centi, centi = float(total), float(per_centi), float(centi)
        except ValueError:
            return None
        if centi <= 0:
            return None
        return {"side": "buy", "shop": shop, "guid": guid, "total": total,
                "unit_price": round(per_centi * 100.0, 2),
                "scu": centi / 100.0, "t": t, **_txn_cargo_handling(line)}
    m = _TXN_SELL_RE.search(line)
    if m:
        shop, total, guid, qty = m.groups()
        try:
            total, qty = float(total), float(qty)
        except ValueError:
            return None
        if qty <= 0:
            return None
        return {"side": "sell", "shop": shop, "guid": guid, "total": total,
                "unit_price": round(total / qty, 2), "scu": qty, "t": t,
                **_txn_cargo_handling(line)}
    return None


class GameLogShardReader:
    """Tails Star Citizen's Game.log and tracks the current shard id.

    Reads only the bytes appended since the last poll, so it's cheap to call
    every loop. The whole file is scanned once on the first poll so a session
    already in progress is picked up. The log is truncated when the game
    relaunches; a shrink in size re-seeks to the start.

    Also collects commodity transactions (#41) from the same line scan — one
    tail, two consumers; drain them with pop_transactions().
    """

    def __init__(self, path):
        self.path = path
        self._offset = 0
        self.shard = None
        self.transactions = []

    def pop_transactions(self):
        """Drain the transactions collected since the last call."""
        txns, self.transactions = self.transactions, []
        return txns

    def poll(self):
        """Scan new log lines; return the current shard id (or None)."""
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return self.shard
        if size < self._offset:
            self._offset = 0          # log rotated on game relaunch
        if size == self._offset:
            return self.shard
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
                fh.seek(self._offset)
                chunk = fh.read()
                self._offset = fh.tell()
        except OSError:
            return self.shard
        for line in chunk.splitlines():
            match = _SHARD_UPDATE_RE.search(line) or _SHARD_JOIN_RE.search(line)
            if match and match.group(1) != self.shard:
                self.shard = match.group(1)
                log(f"shard: {self.shard}")
            txn = parse_trade_txn(line)
            if txn is not None:
                self.transactions.append(txn)
        return self.shard


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect, so the org token can never follow one.

    urllib preserves the `Authorization` header across a redirect INCLUDING to a
    different host — `redirect_request` strips only content-length/content-type —
    and downgrades a 301/302/303 POST to a GET. So a single redirect from a
    compromised, proxied, or MITM'd server (trivially injectable on a plain-http
    server URL) would hand the member's watcher token to whatever host the
    redirect names. The watcher talks to exactly one configured server and has no
    legitimate reason to follow a hop, so refusing is free.

    Returning None makes urllib raise the 3xx as an HTTPError, which _post
    reports and drops without ever opening the second request."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Sender:
    """POSTs position payloads to the nav server.

    Failed sends are queued (bounded) and retried on subsequent events so a
    server restart doesn't lose the session.
    """

    def __init__(self, server_url, timeout=3.0, dry_run=False, token=None):
        self.url = server_url.rstrip("/") + POSITION_ENDPOINT if server_url else None
        self.txn_url = (server_url.rstrip("/") + "/api/trade/transactions"
                        if server_url else None)
        self.handle_url = (server_url.rstrip("/") + HANDLE_ENDPOINT
                           if server_url else None)
        self.timeout = timeout
        self.dry_run = dry_run
        self.token = token
        # Our own opener so the token never follows a redirect (see _NoRedirect).
        # Not urlopen's global default — this is per-Sender, so importing this
        # module can't change urllib behaviour for anything else in the process.
        self._opener = urllib.request.build_opener(_NoRedirect)
        if server_url and not server_url.lower().startswith("https://"):
            log("WARNING: server URL is not https — your watcher token and "
                "positions travel in the clear and can be read or altered by "
                "anything on the network path.")
        self.pending = collections.deque(maxlen=50)
        # Trade transactions awaiting delivery (#41). Its own queue: a jammed
        # transaction batch must never block a position post, or vice versa.
        self.txn_pending = collections.deque(maxlen=40)
        # Last `nav` object the server sent back on a successful post — the
        # overlay's whole data feed (#40). The server already computes this for
        # its own WS frame, so reading it here costs no extra request.
        self.last_nav = None
        # Last handle verdict the server reported, so _note_handle logs only on a
        # change (the verdict rides every position and every 60 s heartbeat).
        self.handle_state = None

    def send(self, payload):
        if self.dry_run:
            log(f"DRY-RUN {json.dumps(payload)}")
            return True
        self.pending.append(payload)
        return self.flush()

    def flush(self):
        ok = True
        while self.pending:
            payload = self.pending[0]
            if self._post(payload):
                self.pending.popleft()
            else:
                ok = False
                break
        return ok

    def register_handle(self, handle):
        """Announce the handle at startup so the org's web UI binds it to this
        member immediately. Without it the binding waits for the player's first
        in-game /showlocation, which made it look like nothing was binding.

        Best-effort by design: an older server 404s here and the position path
        still does the binding, so a failure is a note, never a stop."""
        if not handle or not self.handle_url:
            return
        if self.dry_run:
            log(f"DRY-RUN register handle {handle}")
            return
        if not self.token:
            return   # a guaranteed 401; the loop already warns about the missing token
        self._post({"handle": handle}, url=self.handle_url, what="handle registration")

    def send_transactions(self, txns):
        """Queue + deliver log-detected commodity transactions (#41). The
        server dedups on a natural key, so a batch that half-delivered and
        gets re-sent is harmless."""
        if self.dry_run:
            log(f"DRY-RUN txns {json.dumps(txns)}")
            return True
        self.txn_pending.extend(txns)
        return self.flush_txns()

    def flush_txns(self):
        if not self.txn_pending or not self.txn_url:
            return True
        batch = list(self.txn_pending)[:20]     # server-side batch cap
        if self._post({"txns": batch}, url=self.txn_url):
            for _ in batch:
                self.txn_pending.popleft()
            return not self.txn_pending or self.flush_txns()
        return False

    # A descriptive User-Agent — the default "Python-urllib/x.y" is flagged as a
    # bot by Cloudflare (in front of the tunnel) and gets a 403 before reaching
    # the app.
    USER_AGENT = "sc-nav-watcher/1.0"

    def _post(self, payload, url=None, what=None):
        """POST one payload. `what` names a one-shot side request (the handle
        registration) so its failures don't claim a retry that never comes —
        only the position/txn queues are retried."""
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": self.USER_AGENT}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url or self.url, data=data,
                                         headers=headers, method="POST")
        try:
            with self._opener.open(request, timeout=self.timeout) as resp:
                ok = 200 <= resp.status < 300
                if ok:
                    self._read_response(resp)
                return ok
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                # _NoRedirect refused this before a second request was opened, so
                # the token did NOT leave for the new host. Retrying can't help —
                # the configured URL is wrong (missing /api prefix, http->https
                # upgrade, a proxy in front) — so drop it and say what to fix.
                log(f"REDIRECT REFUSED (HTTP {exc.code} -> "
                    f"{exc.headers.get('Location', '?')}): refusing to send your "
                    "token to a redirect target. Point --server at the app's real "
                    "https URL.")
                return True
            if exc.code in (401, 403):
                # Retrying won't help, so drop it (don't jam the queue) and point
                # at the likely cause: the app 401s a bad token, Cloudflare 403s
                # a blocked bot request.
                body = ""
                try:
                    body = exc.read().decode("utf-8", "replace")[:200].lower()
                except Exception:
                    pass
                if "cloudflare" in body or "cf-ray" in body or exc.code == 403:
                    log(f"BLOCKED before the app (HTTP {exc.code}) — Cloudflare is "
                        "filtering this request. Add a WAF Skip rule for /api/* or "
                        "turn off Bot Fight Mode for the zone.")
                else:
                    log(f"AUTH FAILED (HTTP {exc.code}): set a valid --token "
                        "(generate one in the web UI under 'Watcher token')")
                return True
            if what:
                log(f"{what} failed (HTTP {exc.code}) — the binding will still "
                    "happen on your first /showlocation")
            else:
                log(f"send failed (HTTP {exc.code}); will retry "
                    f"({len(self.pending)} queued)")
            return False
        except (urllib.error.URLError, OSError) as exc:
            if what:
                log(f"{what} failed ({exc})")
            else:
                log(f"send failed ({exc}); will retry ({len(self.pending)} queued)")
            return False

    def _read_response(self, resp):
        """Pull the overlay slice (#40) and the handle verdict out of a successful
        response. Best-effort by design: an older server (or a future body shape)
        must never break position reporting, which is this program's actual job."""
        try:
            body = json.loads(resp.read(64_000).decode("utf-8", "replace"))
        except Exception:
            return
        if not isinstance(body, dict):
            return
        if isinstance(body.get("nav"), dict):
            self.last_nav = body["nav"]
        if isinstance(body.get("handle"), dict):
            self._note_handle(body["handle"])

    def _note_handle(self, status):
        """Log the server's verdict on the handle we're reporting — once per
        change, since it rides every position and every heartbeat. A handle that
        another account already owns is refused on purpose (it would otherwise
        let anyone claim a member's captures), but the refusal used to be
        completely silent, so it read as 'binding is broken'."""
        key = (status.get("handle"), bool(status.get("bound")), status.get("conflict"))
        if key == self.handle_state:
            return
        self.handle_state = key
        name = status.get("handle") or "?"
        if status.get("bound"):
            log(f'handle bound: "{name}" is verified as yours on the server')
        elif status.get("conflict") == "owned_by_other":
            log(f'HANDLE NOT BOUND: "{name}" is already claimed by another '
                "member's account, so your captures stay unattributed. Ask an org "
                "admin to clear the old binding, or pass the handle you play as.")
        elif status.get("conflict") == "rate_limited":
            log(f'handle not bound yet: "{name}" is new to the server and this '
                "account has registered several new handles recently. It'll bind "
                "on a later report — check the handle spelling meanwhile.")
        else:
            log(f'handle not bound: "{name}" ({status.get("conflict")})')


# ---------------------------------------------------------------------------
# Watcher loop
# ---------------------------------------------------------------------------


def log(message):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def build_payload(coords, raw_text, handle=None, shard=None):
    return {
        "x": coords["x"],
        "y": coords["y"],
        "z": coords["z"],
        "raw": raw_text.strip()[:512],
        "client_time": datetime.now(timezone.utc).isoformat(),
        "source": "sc_nav_watcher",
        "handle": handle,
        "shard": shard,
    }


def heartbeat_due(now, last_send, interval, shard, last_sent_shard):
    """Decide whether to re-send the last known position as a heartbeat.

    The game only copies coordinates when the player runs /showlocation, so a
    stationary player's position (and shard) would otherwise go stale on the
    server and they'd drop off teammates' maps. Returns a reason string:
      * "shard"    — the Game.log shard changed since we last transmitted (relog
                     or server mesh handoff); send immediately so the server
                     re-tags the player to the new server without waiting.
      * "interval" — `interval` seconds have elapsed since the last send.
      * ""         — nothing due.
    `interval <= 0` disables the timed heartbeat (shard changes still send)."""
    if shard != last_sent_shard:
        return "shard"
    if interval > 0 and (now - last_send) >= interval:
        return "interval"
    return ""


# Sticky config: --handle and --token are remembered here so future runs
# (e.g. the double-click .bat) don't need to re-specify them.
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watcher_config.json")


def _load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_config(config):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(config, fh)
        # The config holds the bearer token — keep it owner-only where the OS
        # honors it (POSIX; a no-op on Windows, the watcher's usual home).
        try:
            os.chmod(CONFIG_PATH, 0o600)
        except OSError:
            pass
    except OSError as exc:
        log(f"could not save watcher config: {exc}")


def _resolve_sticky(args_value, key):
    """Return args_value (saving it to config if new), else the saved value."""
    config = _load_config()
    if args_value:
        value = args_value.strip()
        if value and value != config.get(key):
            config[key] = value
            _save_config(config)
        return value
    return (config.get(key) or "").strip() or None


def _resolve_sticky_flag(args_value, key, default=False):
    """Sticky resolution for a boolean, which `_resolve_sticky` can't do: a
    False is a real answer, not "unset". argparse hands us None for "didn't
    say", which must stay distinguishable from an explicit --no-overlay —
    otherwise a saved `true` could never be turned back off."""
    config = _load_config()
    if args_value is not None:
        value = bool(args_value)
        if value != config.get(key):
            config[key] = value
            _save_config(config)
        return value
    saved = config.get(key)
    return default if saved is None else bool(saved)


def resolve_handle(args):
    return _resolve_sticky(args.handle, "handle")


OVERLAY_MODES = ("off", "light", "heavy")


def _migrate_overlay_mode(saved):
    """`overlay` used to be a bool (W1). Map the old values onto the modes so
    an existing watcher_config.json keeps working untouched (#40 §13.5)."""
    if saved is True:
        return "light"
    if saved is False:
        return "off"
    if isinstance(saved, str) and saved.lower() in OVERLAY_MODES:
        return saved.lower()
    return None


def resolve_overlay(args):
    """Which overlay to run: off | light | heavy.

    Off unless asked for — a window appearing over someone's cockpit uninvited
    is the thing to avoid (#40 §7). The launcher asks once and the answer
    sticks, exactly like --handle. `--overlay-mode` wins; the older
    `--overlay`/`--no-overlay` remain aliases for light/off so launchers and
    muscle memory from W1 keep working."""
    chosen = None
    if getattr(args, "overlay_mode", None):
        chosen = args.overlay_mode.lower()
    elif args.overlay is not None:
        chosen = "light" if args.overlay else "off"

    config = _load_config()
    if chosen in OVERLAY_MODES:
        if chosen != _migrate_overlay_mode(config.get("overlay")):
            config["overlay"] = chosen
            _save_config(config)
        return chosen
    return _migrate_overlay_mode(config.get("overlay")) or "off"


def resolve_token(args):
    return _resolve_sticky(args.token, "token")


def resolve_trade_capture(args):
    """#41: report commodity buys/sells read from Game.log. Default ON — a
    member already streaming live position isn't surprised by trade capture,
    and the data only ever feeds their own org's tools. Sticky like the other
    knobs; --no-trade-capture turns a saved 'on' back off."""
    config = _load_config()
    val = getattr(args, "trade_capture", None)
    if val is not None:
        if config.get("trade_capture") != val:
            config["trade_capture"] = val
            _save_config(config)
        return val
    saved = config.get("trade_capture")
    return True if saved is None else bool(saved)


def resolve_game_log(args):
    """The Game.log path: --game-log (sticky), else the saved one, else a
    common-install autodetect. None disables shard detection."""
    chosen = _resolve_sticky(args.game_log, "game_log")
    return chosen or default_game_log()


def run(args, sink=None, stop=None):
    """The watch loop. `sink(nav, fix_t)` — when given — receives the server's
    overlay slice plus the monotonic stamp of the fix it describes; `stop` is a
    threading.Event that ends the loop. Both are None in the default
    console-only mode, where this runs on the main thread exactly as before."""
    clipboard = make_clipboard()
    token = resolve_token(args)
    sender = Sender(args.server, timeout=args.timeout, dry_run=args.dry_run, token=token)
    handle = resolve_handle(args)
    if handle:
        log(f"reporting as handle: {handle}")
        # Bind now rather than at the first /showlocation, so the web UI's
        # IN-GAME IDENTITY panel fills in as soon as the watcher is up. Logs the
        # server's verdict via _note_handle.
        sender.register_handle(handle)
    else:
        log("no handle set (captures will be unattributed) — pass --handle \"YourName\"")

    game_log = resolve_game_log(args)
    shard_reader = GameLogShardReader(game_log) if game_log else None
    if shard_reader:
        log(f"watching shard from: {game_log}")
    else:
        log("no Game.log found — shard tagging off (pass --game-log to enable; "
            "nodes won't be filtered by server)")
    trade_capture = resolve_trade_capture(args) and shard_reader is not None
    if shard_reader:
        log("trade capture: on — commodity buys/sells from Game.log feed the "
            "org's price + run tools (--no-trade-capture to turn off)"
            if trade_capture else "trade capture: off")
    if not token and not args.dry_run:
        log("WARNING: no auth token set — the server will reject positions. "
            "Generate one in the web UI ('Watcher token') and pass --token \"...\"")

    last_seq = clipboard.sequence_number()
    last_text = None
    last_coords = None            # most recent successfully-parsed location
    last_raw = ""                 # its raw clipboard text (for the payload)
    last_sent_shard = None        # shard value last transmitted to the server
    last_send_t = time.monotonic()  # drives the heartbeat cadence
    sent_count = 0
    # When the CURRENT position was actually observed. Only a clipboard read
    # moves this: a heartbeat re-sends the same coordinates, so it must not
    # reset the age the overlay shows, or a stale fix would look live (#40 §3.3).
    fix_t = None

    log(
        f"watching clipboard every {args.interval}s -> "
        + ("dry-run" if args.dry_run else sender.url)
    )
    if not args.once:
        log(
            f"heartbeat: re-sending last position every {args.heartbeat:g}s"
            if args.heartbeat > 0 else "heartbeat: disabled (timed re-send off)"
        )

    while stop is None or not stop.is_set():
        shard = shard_reader.poll() if shard_reader else None
        if trade_capture:
            txns = shard_reader.pop_transactions()
            for t in txns:
                log(f"trade: {t['side'].upper()} {t['scu']:g} SCU @ "
                    f"{t['unit_price']:,.0f}/SCU at {t['shop']} — reporting")
            if txns:
                sender.send_transactions(txns)
            elif sender.txn_pending:
                sender.flush_txns()      # retry an earlier failed batch
        changed = args.once  # single-shot mode always reads
        seq = clipboard.sequence_number()
        if seq is not None:
            if seq != last_seq:
                last_seq = seq
                changed = True
        else:
            # No sequence numbers on this platform; detect by text diff below.
            changed = True

        sent_this_loop = False
        if changed:
            text = clipboard.read_text()
            if text is not None and text != last_text:
                last_text = text
                coords = parse_showlocation(text)
                if coords:
                    last_coords, last_raw = coords, text
                    fix_t = time.monotonic()
                    sender.send(build_payload(coords, text, handle, shard))
                    sent_count += 1
                    sent_this_loop = True
                    log(
                        f"position #{sent_count}: "
                        f"x={coords['x']:.1f} y={coords['y']:.1f} z={coords['z']:.1f}"
                    )
                elif args.verbose:
                    log(f"clipboard changed, not a location ({len(text)} chars)")
            elif seq is not None:
                # New copy event with identical text (e.g. /showlocation while
                # stationary) — forward it so an armed capture still fires and
                # late-joining UIs get the current position.
                if last_text and (coords := parse_showlocation(last_text)):
                    last_coords, last_raw = coords, last_text
                    # A re-copy IS a fresh observation (the player just ran
                    # /showlocation again) even though the numbers match.
                    fix_t = time.monotonic()
                    sender.send(build_payload(coords, last_text, handle, shard))
                    sent_this_loop = True
                    if args.verbose:
                        log("re-copy of same position forwarded")

        # Heartbeat: keep a stationary player fresh on the server (they only copy
        # coords on /showlocation) and re-tag them the instant their shard changes.
        if last_coords and not args.once and not sent_this_loop:
            reason = heartbeat_due(time.monotonic(), last_send_t, args.heartbeat,
                                   shard, last_sent_shard)
            if reason:
                sender.send(build_payload(last_coords, last_raw, handle, shard))
                sent_this_loop = True
                if reason == "shard":
                    log(f"heartbeat: shard changed -> {shard}")
                elif args.verbose:
                    log("heartbeat: re-sent last position")

        if sent_this_loop:
            last_send_t = time.monotonic()
            last_sent_shard = shard
            if sink is not None:
                # Every send path funnels through here, so the overlay sees a
                # /showlocation, a re-copy and a heartbeat alike — the heartbeat
                # is how a destination changed in the BROWSER reaches the HUD.
                sink(sender.last_nav, fix_t)

        if args.once:
            return 0 if sent_count else 1
        if sender.pending:
            sender.flush()
        time.sleep(args.interval)
    return 0


# ---------------------------------------------------------------------------
# Overlay mode (#40)
# ---------------------------------------------------------------------------


def _load_overlay_module(name="sc_nav_overlay"):
    """Import a sibling overlay module, or None. The launcher cd's into this
    directory, but someone running the script by absolute path from elsewhere
    shouldn't lose the overlay to sys.path."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        return importlib.import_module(name)
    except Exception as exc:
        log(f"{name} unavailable ({exc}) — continuing without it")
        return None


def _persist_overlay_config(cfg):
    """Save just the overlay's own keys, re-reading the file first: the watch
    thread writes the same config (sticky handle/token/game_log), so writing
    back our whole startup snapshot would clobber anything it saved since."""
    saved = _load_config()
    changed = False
    for key in ("overlay_x", "overlay_y"):
        if key in cfg and cfg[key] != saved.get(key):
            saved[key] = cfg[key]
            changed = True
    if changed:
        _save_config(saved)


def run_with_heavy(args):
    """Heavy mode (BETA, #40 §13): the watch loop runs on the main thread as
    normal, and the browser window is launched + held on top from a daemon
    thread. Opposite arrangement to light mode, where tk demanded main.

    The browser is a sibling process, not ours to babysit: if it can't start we
    log one line and keep reporting position, because that is the actual job."""
    if args.dry_run:
        log("heavy overlay: skipped in --dry-run (nothing to point it at)")
        return run(args)

    heavy = _load_overlay_module("sc_nav_heavy")
    url = heavy.heavy_url(args.server) if heavy else ""
    if not heavy or not url:
        log("heavy overlay unavailable — continuing without it")
        try:
            return run(args)
        except KeyboardInterrupt:
            log("stopped")
            return 0

    stop = threading.Event()
    thread = threading.Thread(
        target=heavy.run,
        kwargs={"url": url, "config": _load_config(), "log": log, "stop": stop},
        name="sc-nav-heavy", daemon=True,
    )
    thread.start()
    try:
        return run(args, stop=stop)
    except KeyboardInterrupt:
        log("stopped")
        return 0
    finally:
        # Close the window we opened, and give it a moment to actually go.
        stop.set()
        thread.join(timeout=3.0)


def run_with_overlay(args):
    """tkinter must own the main thread, so the watch loop moves to a daemon
    thread and hands updates over a Queue. If the window can't be built (a
    Python with no tcl/tk, a headless session), fall back to console-only in
    this same process — losing position reporting to a cosmetic feature would
    be a bad trade (#40 §7.3)."""
    overlay = _load_overlay_module()
    if overlay is None or not overlay.available():
        if overlay is not None:
            log("overlay unavailable on this Python (no tkinter) — "
                "continuing without it")
        try:
            return run(args)
        except KeyboardInterrupt:
            log("stopped")
            return 0

    updates = queue.Queue()
    stop = threading.Event()

    def worker():
        try:
            run(args, sink=lambda nav, fix_t: updates.put((nav, fix_t)), stop=stop)
        except Exception as exc:
            log(f"watcher loop stopped: {exc}")
        finally:
            # Whatever happened, don't leave a window on screen showing a
            # distance that will never update again.
            stop.set()

    thread = threading.Thread(target=worker, name="sc-nav-watcher", daemon=True)
    thread.start()
    log("overlay: on (hold F to move it or click it while you're in game; "
        "borderless/windowed mode only)")

    try:
        started = overlay.start(
            updates, config=_load_config(), on_close=_persist_overlay_config,
            log=log, stop=stop,
        )
        if not started:
            # Window construction failed but the watch loop is already running
            # and doing its job — stay out of its way until Ctrl-C.
            while thread.is_alive():
                thread.join(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
    log("stopped")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Forward Star Citizen /showlocation clipboard output to the nav server."
    )
    parser.add_argument(
        "--server",
        help="Nav server base URL, e.g. http://192.168.1.50:8765",
    )
    parser.add_argument(
        "--interval", type=float, default=0.25, help="Poll interval seconds (default 0.25)"
    )
    parser.add_argument(
        "--heartbeat", type=float, default=60.0,
        help="Re-send the last known position every N seconds so a stationary "
        "player stays live on teammates' maps (a shard change always re-sends "
        "immediately). 0 disables the timed re-send. Default 60.",
    )
    parser.add_argument(
        "--timeout", type=float, default=3.0, help="HTTP timeout seconds (default 3)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print payloads instead of sending"
    )
    parser.add_argument(
        "--once", action="store_true", help="Read clipboard once, send if valid, exit"
    )
    parser.add_argument("--verbose", action="store_true", help="Log non-location clipboard changes")
    parser.add_argument(
        "--handle",
        help="Your in-game player handle, attached to captures for attribution. "
        "Saved to watcher_config.json so it's remembered on later runs.",
    )
    parser.add_argument(
        "--token",
        help="Watcher auth token (generate one in the web UI under 'Watcher token'). "
        "Required by an authenticated server. Saved to watcher_config.json.",
    )
    parser.add_argument(
        "--overlay-mode",
        choices=OVERLAY_MODES,
        default=None,
        help="off = no overlay · light = the small always-on-top HUD "
        "(target/distance/ETA) · heavy = BETA, opens the full web app in a "
        "pinned browser window over the game (every map, live updates; needs "
        "Edge or Chrome and a signed-in browser). Saved to watcher_config.json.",
    )
    parser.add_argument(
        "--overlay",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Show a small always-on-top window over the game with your current "
        "target, distance and ETA. Off unless asked for; the launcher asks once "
        "and the answer is saved to watcher_config.json. Use --no-overlay to "
        "turn a saved 'on' back off. Only visible in borderless/windowed mode.",
    )
    parser.add_argument(
        "--game-log",
        help="Path to Star Citizen's Game.log, used to tag captures with your "
        "current shard so nodes from other servers can be filtered out. "
        "Autodetected from the default install if omitted. Saved to "
        "watcher_config.json.",
    )
    parser.add_argument(
        "--trade-capture",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Report commodity buys/sells read from Game.log to the org's "
        "trade tools: run-mode confirms get the real price/SCU pre-filled and "
        "the org price board learns actual prices. On by default when a "
        "Game.log is being watched; --no-trade-capture opts out (sticky).",
    )
    args = parser.parse_args()

    if not args.server and not args.dry_run:
        parser.error("--server is required unless --dry-run is set")

    mode = resolve_overlay(args)
    if mode == "light":
        sys.exit(run_with_overlay(args))
    if mode == "heavy":
        sys.exit(run_with_heavy(args))
    log("overlay: off (answer L or H at the launcher prompt to enable)")
    try:
        sys.exit(run(args))
    except KeyboardInterrupt:
        log("stopped")
        sys.exit(0)


if __name__ == "__main__":
    main()
