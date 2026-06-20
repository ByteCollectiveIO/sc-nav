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
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

POSITION_ENDPOINT = "/api/position"

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


class GameLogShardReader:
    """Tails Star Citizen's Game.log and tracks the current shard id.

    Reads only the bytes appended since the last poll, so it's cheap to call
    every loop. The whole file is scanned once on the first poll so a session
    already in progress is picked up. The log is truncated when the game
    relaunches; a shrink in size re-seeks to the start.
    """

    def __init__(self, path):
        self.path = path
        self._offset = 0
        self.shard = None

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
        return self.shard


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


class Sender:
    """POSTs position payloads to the nav server.

    Failed sends are queued (bounded) and retried on subsequent events so a
    server restart doesn't lose the session.
    """

    def __init__(self, server_url, timeout=3.0, dry_run=False, token=None):
        self.url = server_url.rstrip("/") + POSITION_ENDPOINT if server_url else None
        self.timeout = timeout
        self.dry_run = dry_run
        self.token = token
        self.pending = collections.deque(maxlen=50)

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

    # A descriptive User-Agent — the default "Python-urllib/x.y" is flagged as a
    # bot by Cloudflare (in front of the tunnel) and gets a 403 before reaching
    # the app.
    USER_AGENT = "sc-nav-watcher/1.0"

    def _post(self, payload):
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": self.USER_AGENT}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                return 200 <= resp.status < 300
        except urllib.error.HTTPError as exc:
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
            log(f"send failed (HTTP {exc.code}); will retry ({len(self.pending)} queued)")
            return False
        except (urllib.error.URLError, OSError) as exc:
            log(f"send failed ({exc}); will retry ({len(self.pending)} queued)")
            return False


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


def resolve_handle(args):
    return _resolve_sticky(args.handle, "handle")


def resolve_token(args):
    return _resolve_sticky(args.token, "token")


def resolve_game_log(args):
    """The Game.log path: --game-log (sticky), else the saved one, else a
    common-install autodetect. None disables shard detection."""
    chosen = _resolve_sticky(args.game_log, "game_log")
    return chosen or default_game_log()


def run(args):
    clipboard = make_clipboard()
    token = resolve_token(args)
    sender = Sender(args.server, timeout=args.timeout, dry_run=args.dry_run, token=token)
    handle = resolve_handle(args)
    if handle:
        log(f"reporting as handle: {handle}")
    else:
        log("no handle set (captures will be unattributed) — pass --handle \"YourName\"")

    game_log = resolve_game_log(args)
    shard_reader = GameLogShardReader(game_log) if game_log else None
    if shard_reader:
        log(f"watching shard from: {game_log}")
    else:
        log("no Game.log found — shard tagging off (pass --game-log to enable; "
            "nodes won't be filtered by server)")
    if not token and not args.dry_run:
        log("WARNING: no auth token set — the server will reject positions. "
            "Generate one in the web UI ('Watcher token') and pass --token \"...\"")

    last_seq = clipboard.sequence_number()
    last_text = None
    sent_count = 0

    log(
        f"watching clipboard every {args.interval}s -> "
        + ("dry-run" if args.dry_run else sender.url)
    )

    while True:
        shard = shard_reader.poll() if shard_reader else None
        changed = args.once  # single-shot mode always reads
        seq = clipboard.sequence_number()
        if seq is not None:
            if seq != last_seq:
                last_seq = seq
                changed = True
        else:
            # No sequence numbers on this platform; detect by text diff below.
            changed = True

        if changed:
            text = clipboard.read_text()
            if text is not None and text != last_text:
                last_text = text
                coords = parse_showlocation(text)
                if coords:
                    payload = build_payload(coords, text, handle, shard)
                    sender.send(payload)
                    sent_count += 1
                    log(
                        f"position #{sent_count}: "
                        f"x={coords['x']:.1f} y={coords['y']:.1f} z={coords['z']:.1f}"
                    )
                elif args.verbose:
                    log(f"clipboard changed, not a location ({len(text)} chars)")
            elif seq is not None:
                # New copy event with identical text (e.g. /showlocation while
                # stationary) — forward it as a heartbeat so an armed capture
                # still fires and late-joining UIs get the current position.
                if last_text and (coords := parse_showlocation(last_text)):
                    sender.send(build_payload(coords, last_text, handle, shard))
                    if args.verbose:
                        log("re-copy of same position forwarded")

        if args.once:
            return 0 if sent_count else 1
        if sender.pending:
            sender.flush()
        time.sleep(args.interval)


def main():
    parser = argparse.ArgumentParser(
        description="Forward Star Citizen /showlocation clipboard output to the nav server."
    )
    parser.add_argument(
        "--server",
        help="Nav server base URL, e.g. http://192.168.1.68:8765",
    )
    parser.add_argument(
        "--interval", type=float, default=0.25, help="Poll interval seconds (default 0.25)"
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
        "--game-log",
        help="Path to Star Citizen's Game.log, used to tag captures with your "
        "current shard so nodes from other servers can be filtered out. "
        "Autodetected from the default install if omitted. Saved to "
        "watcher_config.json.",
    )
    args = parser.parse_args()

    if not args.server and not args.dry_run:
        parser.error("--server is required unless --dry-run is set")

    try:
        sys.exit(run(args))
    except KeyboardInterrupt:
        log("stopped")
        sys.exit(0)


if __name__ == "__main__":
    main()
