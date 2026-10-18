#!/usr/bin/env python3
"""Live Game.log profiler: tail the log while you play, drop markers, and see
which log lines answer "what did the game write when I did X?".

`gamelog_spike.py` inventories logs AFTER a session. This is its live
companion, built for the paired test that spike can't run (docs/
watcher-modular-hud.md §9.2.4–9.2.5): do a thing in game, mark the moment, and
get back the event tags that cluster around the mark. That's how a parser
candidate gets found, and also how a negative result is proven ("I scanned four
rocks and nothing near any mark names an ore").

Usage (on the gaming PC, game running or about to be):
    python tools/gamelog_live.py [--game-log PATH] [--hotkey F9] [--out DIR]
                                 [--echo REGEX] [--from-end]
    python tools/gamelog_live.py --once [--game-log PATH] [--notes steps.txt]

While it runs:
    F9 (in game)          drop a numbered marker (beep). Works while the game has focus;
                          the key still reaches the game, so use one that's unbound there.
    <text> + Enter        new marker labelled <text>, at the moment you hit Enter
    @HH:MM[:SS] <text>    marker at that local time, after the fact
    <N> <text> + Enter    label marker #N (the F9 ones, after the fact); `#N <text>` too
    r + Enter             write the report now
    q + Enter / Ctrl+C    write the report and quit

`--once` processes a log as it stands (the live one, or a `logbackups/` file
via --game-log) and exits. Pair it with `--notes`, a file of `HH:MM[:SS] label`
lines in local time, to get marker windows for a session you only wrote down.

The console prints the nav timeline as it happens (QT, location, hangar, ship,
zones, purchases, HUD notifications minus tutorial hints) and every tag never
seen in an earlier session, as NEW.

Output (default `../game_logs/live/`, outside the repo like the rest of the
captured logs): `session-<utc>.md` (rewritten every minute and on exit) and
`seen_tags.json`, the cross-session baseline that decides what counts as NEW.
On the first run the baseline is seeded from the game's own `logbackups/`.

Reports scrub the player's handle/account id/geid and IPs (gamelog_spike's
scrubber), and chat lines are never sampled. Times are shown in local time.
Read-only: this opens a file the game writes and nothing else.
"""

from __future__ import annotations

import argparse
import bisect
import json
import re
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gamelog_spike import (CHAT_RE, IDENT_CHAR_RE, IDENT_HANDLE_RE,  # noqa: E402
                           build_scrubber, clip, parse_line)

DEFAULT_LOG = Path(r"C:\Program Files\Roberts Space Industries\StarCitizen\LIVE\Game.log")
DEFAULT_OUT = Path(__file__).resolve().parents[2] / "game_logs" / "live"

NOTIFICATION_RE = re.compile(r'<SHUDEvent_OnNotification> Added notification "([^"]*)')
BUILD_CL_RE = re.compile(r"^<[^>]+>\s+Changelist:\s+(\d+)\s*$")

# Tutorial hints share the notification stream with real events and outnumber
# them (~70% of the 2026-10-15 session). Titles only ever seen as hints; the
# title alone can't decide "Quantum Travel:" (also the obstruction warning), so
# that one is matched on its hint body. Extend as new hints show up.
HINT_RE = re.compile(
    r"^(?:Stamina|Hunger & Thirst|Eating & Drinking|Maps|Radar Ping Angle|Radar.s Ping|"
    r"Quantum Drive - Spooling|Quantum Travel - Calibration|Quantum Linking|Ship Startup|"
    r"Scan Mode|Mining - [\w ]+|Welcome to [\w ]+|Hints|Inventory|"
    r"Personal Inner Thought \(PIT\)|Monitored Space|Multi-Tool - [\w ]+|Salvage - [\w ]+|"
    r"Air Traffic Control \(ATC\)|Ship Movement|Multi-Function Displays|Flight Mode Select Menu|"
    r"Quantum Travel - Cancel|Force Reaction - [\w ]+|Chat|Shopping|Vehicle Retrieval|"
    r"Trading and Shipping Console|Exit Bed|PIT Menu - [\w ]+): "
    r"|^Quantum Travel: Once |^Exiting Armistice Zone: You have left")

# The vehicle a QT line is about: `| NOT AUTH | DRAK_Golem_868584930160[868584930160]|`.
# Every QT line reads NOT AUTH, including other players' ships (an ANVL_Asgard
# arrival showed up mid-session), so own-ship is decided by entity id.
QT_VEHICLE_RE = re.compile(r"\|\s*([A-Za-z0-9_]+?)_(\d{6,})\[\d+\]\|")


def _short(vehicle):
    return vehicle.rsplit("_", 1)[0] if re.search(r"_\d{6,}$", vehicle) else vehicle


# (kind, regex, formatter). The formatter gets the match; the first rule that
# matches a line wins. Kept to lines verified in captured logs (§9.2.1–9.2.4),
# plus the community-documented death lines we haven't captured yet.
TIMELINE_RULES = [
    ("session", re.compile(r"<Join PU>.*?\bshard\[([^\]]+)\]"),
     lambda m: f"joined shard {m.group(1)}"),
    ("location", re.compile(r"<RequestLocationInventory>.*?Location\[([^\]]+)\]"),
     lambda m: f"at {m.group(1)}"),
    ("location", re.compile(r"<Update Inventory Location>.*?Location \[\d+\] -> \[(\d+)\]"),
     lambda m: f"location id {m.group(1)}"),
    ("qt", re.compile(r"<Player Selected Quantum Target[^>]*>.*?selected point (\S+)"),
     lambda m: f"QT target selected: {m.group(1)}"),
    ("qt", re.compile(r"<Calculate Route>.*?Projected Start Location is (.+?) for route to destination (\S+)"),
     lambda m: f"QT route: {m.group(1)} → {m.group(2)}"),
    ("qt", re.compile(r"<Calculate Route>.*?Successfully calculated route to (\S+) fuel estimate ([\d.]+)"),
     lambda m: f"QT fuel estimate to {m.group(1)}: {float(m.group(2)):,.2f}"),
    ("qt", re.compile(r"<Quantum Drive Arrived[^>]*>"),
     lambda m: "QT arrived"),
    ("ship", re.compile(r"<CEntityComponentShipListProvider::SetVehicleSpawningInformations>.*?VehicleEntityId: \[(\d+)\]"),
     lambda m: f"ship retrieval requested (entity {m.group(1)})"),
    ("ship", re.compile(r"<Vehicle Control Flow>.*?(\w+) control token for '([^']+)'"),
     lambda m: f"{m.group(1)} control of {_short(m.group(2))}"),
    # Commodity kiosk: the GUID (no name; the server's commodity_guids maps it),
    # SCU (buy quotes centi-SCU, sell quotes SCU), boxes and the aUEC total.
    ("shop", re.compile(r"<CEntityComponentCommodityUIProvider::SendCommodity(Buy|Sell)Request>"
                        r".*?shopName\[([^\]]+)\].*?(?:price|amount)\[([\d.]+)\]"
                        r".*?resourceGUID\[([0-9a-f]{8})[^\]]*\].*?quantity\[([\d.]+)( cSCU)?\]"
                        r".*?boxSize\[([\d.]+)\]\s*\|\s*unitAmount\[(\d+)\]"),
     lambda m: (f"{m.group(1).lower()} {float(m.group(5)) / (100 if m.group(6) else 1):g} SCU "
                f"of commodity {m.group(4)}… ({m.group(8)} × {float(m.group(7)):g} SCU) "
                f"for {float(m.group(3)):,.0f} aUEC at {m.group(2)}")),
    ("shop", re.compile(r"<CEntityComponent\w*UIProvider::Send(?:Shop|Commodity)(Buy|Sell)Request>"
                        r".*?shopName\[([^\]]+)\](?:.*?itemName\[([^\]]+)\])?(?:.*?quantity\[([^\]]+)\])?"),
     lambda m: f"{m.group(1).lower()} at {m.group(2)}: {m.group(3) or 'commodity'} × {m.group(4) or '?'}"),
    ("shop", re.compile(r"<CEntityComponentShopUIProvider::RmShopFlowResponse>.*?result\[(\w+)\].*?type\[(\w+)\]"),
     lambda m: f"shop {m.group(2).lower()}: {m.group(1)}"),
    # Every objective's world position (2026-10-17 salvage capture): the zone it
    # is relative to, a local position in metres, and the contract name.
    ("mission", re.compile(r"<CLocalMissionPhaseMarker::CreateMarker>.*?contract \[([^\]]+)\]"
                           r".*?objectiveId \[([^\]_]+)_?.*?zoneHostId \[(\d+)\]"
                           r".*?position \[x: ([-\d.]+), y: ([-\d.]+), z: ([-\d.]+)\]"),
     lambda m: (f"objective marker: {m.group(1)}"
                + (" (drop-off)" if m.group(2) == "dropoff" else "")
                + f" in zone {m.group(3)} at ("
                + ", ".join(f"{float(m.group(i)):,.0f}" for i in (4, 5, 6)) + ") m")),
    ("mission", re.compile(r"<MissionEnded>.*?mission_state MISSION_STATE_(\w+)"),
     lambda m: f"mission ended: {m.group(1).lower()}"),
    ("death", re.compile(r"<(Actor Death|Vehicle Destruction)>\s*(.*)"),
     lambda m: f"{m.group(1)}: {m.group(2)}"),
    ("session", re.compile(r"<SystemQuit>"),
     lambda m: "game quit"),
]
OWN_VEHICLE_RES = (
    re.compile(r"SetVehicleSpawningInformations.*?VehicleEntityId: \[(\d+)\]"),
    re.compile(r"<Vehicle Control Flow>.*?control token for '[^']+' \[(\d+)\]"),
)

# Browsing the starmap re-logs the same QT target/route/fuel triplet on every
# click (2026-10-17: ~70 lines in 15 s), so a QT line that repeats one from the
# last minute is dropped, not only an exact repeat of the line before.
QT_REPEAT_S = 60

MARKER_PRE_S = 5      # lines this long BEFORE a mark belong to it (you react late)
MARKER_POST_S = 20     # ...and this long after (the game logs some outcomes late)
WINDOW_TOP = 10        # tags listed per marker
# Below this share, every tag near a mark is just background: report it as a
# negative result, which is what "I scanned a rock and nothing logged" looks like.
DISTINCT_SHARE = 0.2
# Left out of marker windows: pure echoes of a line that is already counted
# (UpdateNotificationItem re-states each notification as it shows and fades).
WINDOW_IGNORE = {"UpdateNotificationItem"}
UNTAGGED = "(untagged)"
CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(.*)$")


def parse_ts(ts):
    """`2026-10-15T00:31:20.648Z` -> aware datetime, or None."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def hms(dt):
    """Local wall-clock time: what the player saw on their clock."""
    return dt.astimezone().strftime("%H:%M:%S")


def norm_tag(tag):
    """The baseline key for a tag. Folds ids/hex out so a tag that embeds a
    number isn't NEW every session (`StatObjLoad 0x800 Format`)."""
    return re.sub(r"0x[0-9a-fA-F]+|\d{3,}", "#", tag)


def resolve_clock(h, m, s, start, end):
    """Local HH:MM:SS -> the aware datetime on whichever day puts it in (or
    nearest to) [start, end]. A session that crosses midnight has two
    candidate days; the log span picks the right one."""
    tz = datetime.now().astimezone().tzinfo
    lo, hi = start.astimezone(tz), end.astimezone(tz)
    best = None
    day = lo.date() - timedelta(days=1)
    while day <= hi.date() + timedelta(days=1):
        cand = datetime(day.year, day.month, day.day, h, m, s, tzinfo=tz)
        dist = max((lo - cand).total_seconds(), (cand - hi).total_seconds(), 0)
        if best is None or dist < best[0]:
            best = (dist, cand)
        day += timedelta(days=1)
    return best[1].astimezone(timezone.utc)


def parse_clock(text):
    """`16:20 scanned taranite` -> (h, m, s, label), or None."""
    m = CLOCK_RE.match(text.strip())
    if not m:
        return None
    h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    if h > 23 or mi > 59 or s > 59:
        return None
    return h, mi, s, m.group(4).strip()


class Marker:
    def __init__(self, n, t, label=""):
        self.n, self.t, self.label = n, t, label


class Profiler:
    """All the bookkeeping, with no I/O: feed lines, add markers, get a report.

    Keeps every event (a session is ~3–15k lines), so a marker can land
    anywhere in the past and its window is cut at report time."""

    def __init__(self, baseline=None, echo=None, out=print):
        self.baseline = set(baseline or ())
        self.echo_re = re.compile(echo) if echo else None
        self.out = out
        self.started = datetime.now(timezone.utc)
        self.counts = Counter()
        self.first_seen = {}           # tag -> (dt, raw) for tags NEW vs baseline
        self.new_keys = set()
        self.events = []               # (dt, tag, raw), in log order
        self.dts = []                  # parallel to events, for bisect
        self.timeline = []             # (dt, kind, text, other_ship)
        self.hints = Counter()         # tutorial hint title -> count
        self.own_vehicles = set()
        self.markers = []
        self.last_dt = None
        self.build = None
        self.log_restarts = 0
        self._ident_text = ""
        self.scrub = build_scrubber("", False)
        self._lock = threading.Lock()

    # -- lines ---------------------------------------------------------------

    def feed(self, raw):
        raw = raw.rstrip("\r\n")
        if not raw.strip():
            return
        ts, _sev, tag, _tags, _msg = parse_line(raw)
        dt = parse_ts(ts) or self.last_dt
        if dt is None:
            dt = datetime.now(timezone.utc)
        # Keep the series sorted for bisect even if the clock steps back.
        if self.last_dt and dt < self.last_dt:
            dt = self.last_dt
        self.last_dt = dt
        key = tag or UNTAGGED
        with self._lock:
            self.events.append((dt, key, raw))
            self.dts.append(dt)
            self.counts[key] += 1
            self._learn_identity(raw)
            m = BUILD_CL_RE.match(raw)
            if m:
                self.build = m.group(1)
            for rx in OWN_VEHICLE_RES:
                m = rx.search(raw)
                if m:
                    self.own_vehicles.add(m.group(1))
            entry = self._timeline_entry(dt, raw)
            if tag and norm_tag(tag) not in self.baseline | self.new_keys:
                self.new_keys.add(norm_tag(tag))
                self.first_seen[key] = (dt, raw)
                self.out(f"NEW  {hms(dt)} <{tag}>  {self._sample(raw)}")
            if entry and not entry[3]:
                self.out(f"     {hms(dt)} {entry[1]:<8} {entry[2]}")
            elif self.echo_re and self.echo_re.search(raw) and not CHAT_RE.search(raw):
                self.out(f"     {hms(dt)} {self._sample(raw)}")

    def _timeline_entry(self, dt, raw):
        """Append and return this line's timeline entry, if it has one."""
        entry = None
        m = NOTIFICATION_RE.search(raw)
        if m:
            text = m.group(1).strip().rstrip(":").strip()
            if not text:
                return None
            if HINT_RE.search(text):
                self.hints[text.split(":", 1)[0]] += 1
                return None
            entry = (dt, "hud", self.scrub(text), False)
        else:
            for kind, rx, fmt in TIMELINE_RULES:
                m = rx.search(raw)
                if m:
                    text = fmt(m)
                    other = False
                    if kind == "qt":
                        v = QT_VEHICLE_RE.search(raw)
                        if v:
                            other = bool(self.own_vehicles) and v.group(2) not in self.own_vehicles
                            text += f" ({_short(v.group(1))})"
                    entry = (dt, kind, clip(self.scrub(text)), other)
                    break
        if entry is None:
            return None
        prev = self.timeline[-1] if self.timeline else None
        if prev and prev[1:] == entry[1:]:
            return None                        # the game repeats itself
        if entry[1] == "qt":
            for old in reversed(self.timeline):
                if (dt - old[0]).total_seconds() > QT_REPEAT_S:
                    break
                if old[1:] == entry[1:]:
                    return None
        self.timeline.append(entry)
        return entry

    def log_restarted(self):
        """The game relaunched (log truncated): mark it, it explains a gap."""
        with self._lock:
            self.log_restarts += 1
        self.add_marker(label="(game relaunched: log restarted)", quiet=True)

    def _learn_identity(self, raw):
        if IDENT_CHAR_RE.search(raw) or IDENT_HANDLE_RE.search(raw):
            self._ident_text += raw + "\n"
            self.scrub = build_scrubber(self._ident_text, False)

    def _sample(self, raw):
        return "(chat line, not shown)" if CHAT_RE.search(raw) else clip(self.scrub(raw.strip()))

    # -- markers -------------------------------------------------------------

    def add_marker(self, t=None, label="", quiet=False):
        t = t or datetime.now(timezone.utc)
        with self._lock:
            mk = Marker(len(self.markers) + 1, t, label)
            self.markers.append(mk)
        if not quiet:
            self.out(f"MARK #{mk.n} {hms(t)}" + (f"  {label}" if label else
                                                 f"  (label it: {mk.n} <text>)"))
        return mk

    def add_marker_at_clock(self, text, now=None):
        """`@HH:MM[:SS] label` from the console, or a `--notes` line. Resolved
        against the log span (or up to now, live)."""
        parsed = parse_clock(text)
        if parsed is None:
            return None
        h, m, s, label = parsed
        end = now or self.last_dt or datetime.now(timezone.utc)
        start = self.dts[0] if self.dts else end
        return self.add_marker(resolve_clock(h, m, s, start, end), label)

    def label(self, n, text):
        with self._lock:
            if 1 <= n <= len(self.markers):
                self.markers[n - 1].label = text
                return True
        return False

    def window(self, mk):
        lo = bisect.bisect_left(self.dts, mk.t - timedelta(seconds=MARKER_PRE_S))
        hi = bisect.bisect_right(self.dts, mk.t + timedelta(seconds=MARKER_POST_S))
        return self.events[lo:hi]

    # -- report --------------------------------------------------------------

    def marker_ranking(self, mk):
        """Tags in a marker's window, most concentrated first: a tag that
        appears ONLY near the mark (window share ~1) is the candidate; one that
        fires all session long is background, however often it hit the window."""
        win = Counter(tag for _dt, tag, _raw in self.window(mk) if tag not in WINDOW_IGNORE)
        ranked = sorted(win, key=lambda tag: (-win[tag] / max(self.counts[tag], 1),
                                              -win[tag], tag))
        return [(tag, win[tag], self.counts[tag]) for tag in ranked]

    def report(self, log_path=""):
        with self._lock:
            return self._report(log_path)

    def _report(self, log_path):
        out = []
        w = out.append
        tz = datetime.now().astimezone().strftime("%Z (UTC%z)")
        span = (f"{hms(self.dts[0])} → {hms(self.dts[-1])}" if self.dts else "empty")
        w(f"# Live Game.log session: {self.started.astimezone():%Y-%m-%d %H:%M}\n")
        w(f"Log: `{log_path}`  ")
        w(f"Build: `{self.build or '?'}` · span: {span} · lines: **{len(self.events):,}** · "
          f"distinct tags: {len(self.counts):,} · markers: {len(self.markers)}"
          + (f" · game relaunches: {self.log_restarts}" if self.log_restarts else "") + "  ")
        w(f"Times are local, {tz}. Scrubbed: handle/account/geid/IPs folded out; "
          "chat lines never sampled.\n")

        w("\n## Markers\n")
        if not self.markers:
            w("_None dropped. Next time press F9 in game, or add them after the fact: "
              "`@HH:MM note` in the console, or `--once --notes steps.txt`._\n")
        else:
            w(f"Window: {MARKER_PRE_S}s before → {MARKER_POST_S}s after each mark. "
              "Share = in-window ÷ session total; near 1.0 means the tag only fired here.\n")
        for mk in self.markers:
            w(f"### #{mk.n} · {hms(mk.t)} · {mk.label or '_(unlabelled)_'}\n")
            lines = self.window(mk)
            if not lines:
                w("_no log lines in the window_\n")
                continue
            ranked = self.marker_ranking(mk)
            if not ranked:
                w("**Nothing near this mark** but notification echoes: the action most "
                  "likely isn't logged.\n")
                continue
            if ranked[0][1] / max(ranked[0][2], 1) < DISTINCT_SHARE:
                w(f"**Nothing distinctive near this mark** (no tag above {DISTINCT_SHARE:.0%} "
                  "share): the action most likely isn't logged. Background in the window:\n")
            w("| share | in window | session | tag |")
            w("|---:|---:|---:|---|")
            for tag, n, total in ranked[:WINDOW_TOP]:
                w(f"| {n / max(total, 1):.2f} | {n} | {total:,} | `{tag}` |")
            w("")
            shown = {tag for tag, _n, _t in ranked[:WINDOW_TOP]}
            seen = set()
            for dt, tag, raw in lines:
                if tag in shown and tag not in seen:
                    seen.add(tag)
                    w(f"    {(dt - mk.t).total_seconds():+5.1f}s {self._sample(raw)}")
            w("")

        w("\n## Timeline\n")
        w("QT, location, hangar, ship, zones, purchases, missions and HUD notifications "
          "(tutorial hints excluded). Markers inline. QT lines about another player's "
          "ship are struck through.\n")
        items = [(e[0], 1, e) for e in self.timeline] + [(mk.t, 0, mk) for mk in self.markers]
        if not items:
            w("_nothing_\n")
        for _t, order, item in sorted(items, key=lambda x: (x[0], x[1])):
            if order == 0:
                w(f"- **`{hms(item.t)}` ▶ MARK #{item.n}** {item.label}")
            else:
                dt, kind, text, other = item
                text = f"~~{text}~~ (other ship)" if other else text
                w(f"- `{hms(dt)}` {kind}: {text}")
        if self.hints:
            w(f"\n_{sum(self.hints.values())} tutorial hints hidden: "
              + ", ".join(f"{t} ×{c}" for t, c in self.hints.most_common()) + "._")

        w("\n\n## Tags first seen this session\n")
        if not self.first_seen:
            w("_none: every tag was already in the baseline_\n")
        else:
            w("| count | first | tag | sample |")
            w("|---:|---|---|---|")
            for tag, (dt, raw) in sorted(self.first_seen.items(), key=lambda kv: kv[1][0]):
                sample = self._sample(raw).replace("|", "\\|")
                w(f"| {self.counts[tag]:,} | {hms(dt)} | `{tag}` | {sample} |")
            w("")

        w("\n## Tag inventory\n")
        w("| count | tag |")
        w("|---:|---|")
        for tag, c in self.counts.most_common():
            w(f"| {c:,} | `{tag}` |")
        return "\n".join(out) + "\n"

    def seen_keys(self):
        with self._lock:
            return self.baseline | self.new_keys


# ---------------------------------------------------------------------------
# I/O: tailing, baseline, hotkey, console
# ---------------------------------------------------------------------------


class Tail:
    """Bytes appended to a file since the last poll, as whole lines. Holds a
    partial last line back until its newline lands; re-reads from the top when
    the file shrinks (the game truncates Game.log on every launch)."""

    def __init__(self, path, from_start=True):
        self.path = Path(path)
        self.offset = 0 if from_start else self._size()
        self.partial = b""

    def _size(self):
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def poll(self):
        """-> (lines, restarted)"""
        size = self._size()
        restarted = False
        if size < self.offset:
            self.offset, self.partial, restarted = 0, b"", True
        if size == self.offset:
            return [], restarted
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self.offset)
                data = fh.read()
                self.offset = fh.tell()
        except OSError:
            return [], restarted
        data = self.partial + data
        parts = data.split(b"\n")
        self.partial = parts.pop()
        return [p.decode("utf-8", errors="replace") for p in parts], restarted


def log_finished(path, tail_bytes=262144):
    """True when the log already holds a `<SystemQuit>`: the game isn't running
    and what's there is a past session (the 2026-10-17 capture opened with the
    previous day's log). The game writes one log per launch, so one quit ends it."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - tail_bytes))
            return b"<SystemQuit>" in fh.read()
    except OSError:
        return False


def parse_command(text):
    """A console line -> (action, arg). `N note` and `#N note` label marker N."""
    low = text.lower()
    if low in ("q", "quit", "exit"):
        return "quit", None
    if low == "r":
        return "report", None
    if text.startswith("@"):
        return "at", text[1:]
    m = re.fullmatch(r"#?(\d+)\s+(.+)", text)
    if m:
        return "label", (int(m.group(1)), m.group(2))
    return "mark", text


def load_baseline(path):
    try:
        return set(json.loads(path.read_text(encoding="utf-8")).get("tags", []))
    except (OSError, ValueError, AttributeError):
        return None


def save_baseline(path, keys):
    path.write_text(json.dumps({"tags": sorted(keys)}, indent=0), encoding="utf-8")


def seed_baseline(log_path):
    """Tags in the game's own previous-session logs (`logbackups/`)."""
    keys = set()
    backups = Path(log_path).parent / "logbackups"
    files = sorted(backups.glob("*.log")) if backups.is_dir() else []
    for f in files:
        for raw in f.read_text(encoding="utf-8", errors="replace").splitlines():
            tag = parse_line(raw)[2]
            if tag:
                keys.add(norm_tag(tag))
    return keys, len(files)


VK_NAMES = {"PAUSE": 0x13, "SCROLL": 0x91, "SCROLLLOCK": 0x91, "INSERT": 0x2D,
            "HOME": 0x24, "END": 0x23, "PGUP": 0x21, "PGDN": 0x22,
            # Main-row punctuation (US layout positions; VK_OEM_*).
            "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, ";": 0xBA, "'": 0xDE,
            ",": 0xBC, ".": 0xBE, "/": 0xBF, "\\": 0xDC, "`": 0xC0,
            # Numpad.
            "NUM*": 0x6A, "NUM+": 0x6B, "NUM-": 0x6D, "NUM.": 0x6E, "NUM/": 0x6F}


def vk_for(name):
    """`F9` / `Pause` / `K` / `-` / `Num5` -> virtual-key code, or None."""
    key = (name or "").strip().upper()
    if re.fullmatch(r"F([1-9]|1\d|2[0-4])", key):
        return 0x6F + int(key[1:])
    if re.fullmatch(r"NUM\d", key):
        return 0x60 + int(key[3])
    if key in VK_NAMES:
        return VK_NAMES[key]
    if len(key) == 1 and key.isalnum():
        return ord(key)
    return None


class EdgeDetector:
    """Key-state samples -> one press per down-stroke (holding it doesn't repeat)."""

    def __init__(self):
        self.was_down = False

    def update(self, down):
        pressed = down and not self.was_down
        self.was_down = down
        return pressed


def start_hotkey(name, on_press, out=print, poll_s=0.025):
    """Marker key, read by polling GetAsyncKeyState, the way the watcher reads
    its interact key (watcher/sc_nav_win32.py `key_down`). RegisterHotKey can't
    be used: SC takes the keyboard through raw input and a registered hotkey
    never fires in game (#40.1 §2.4). Polling is passive and the keystroke still
    reaches the game, so pick a key that's unbound in game. No-op (with a note)
    off Windows."""
    vk = vk_for(name)
    if vk is None or sys.platform != "win32":
        out(f"hotkey: DISABLED ({'not Windows' if vk else f'unknown key {name!r}'}); "
            "type markers in this window instead")
        return
    import ctypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short

    def loop():
        edge = EdgeDetector()
        while True:
            if edge.update(bool(user32.GetAsyncKeyState(vk) & 0x8000)):
                on_press()
            time.sleep(poll_s)

    threading.Thread(target=loop, daemon=True, name="hotkey").start()
    out(f"hotkey: {name} drops a marker (works in game; you'll hear a beep). "
        "It still reaches the game, so keep it unbound there.")


def beep():
    try:
        import winsound
        winsound.Beep(1400, 90)
    except Exception:
        pass


def load_notes(prof, path):
    """`HH:MM[:SS] label` lines (local time) -> markers. '#' starts a comment."""
    added = 0
    for i, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        text = line.split("#", 1)[0].strip()
        if not text:
            continue
        if prof.add_marker_at_clock(text) is None:
            print(f"notes line {i}: expected 'HH:MM[:SS] label', skipped: {line!r}")
        else:
            added += 1
    return added


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--game-log", type=Path, default=DEFAULT_LOG)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help=f"report + baseline directory (default {DEFAULT_OUT})")
    ap.add_argument("--hotkey", default="F9", help="marker key: F1-F24, a letter/digit, - = [ ] ; ' , . / \\ `, Num0-Num9, "
                         "Pause, Insert...; 'none' to disable. Write '--hotkey=-' for punctuation.")
    ap.add_argument("--echo", default="",
                    help="extra regex of raw lines to print live (the timeline always prints)")
    ap.add_argument("--from-end", action="store_true",
                    help="skip what's already in the log (default: read the session so far)")
    ap.add_argument("--once", action="store_true",
                    help="process the log as it stands, write the report, exit")
    ap.add_argument("--notes", type=Path,
                    help="with --once: 'HH:MM[:SS] label' lines (local time) to use as markers")
    args = ap.parse_args(argv)
    if args.notes and not args.once:
        ap.error("--notes needs --once (live, type '@HH:MM label' instead)")

    args.out.mkdir(parents=True, exist_ok=True)
    seen_path = args.out / "seen_tags.json"
    baseline = load_baseline(seen_path)
    if baseline is None:
        baseline, n = seed_baseline(args.game_log)
        print(f"baseline: seeded {len(baseline)} tags from {n} file(s) in logbackups/")
    else:
        print(f"baseline: {len(baseline)} tags from {seen_path}")

    prof = Profiler(baseline, echo=args.echo or None)
    report_path = args.out / f"session-{prof.started:%Y%m%dT%H%M%SZ}.md"

    def flush():
        report_path.write_text(prof.report(str(args.game_log)), encoding="utf-8")
        save_baseline(seen_path, prof.seen_keys())

    from_start = not args.from_end
    if from_start and not args.once and log_finished(args.game_log):
        print("Game.log is from a finished session (it ends with a quit): skipping it, "
              "waiting for the game to start")
        from_start = False
    tail = Tail(args.game_log, from_start=from_start)
    if args.once:
        lines, _ = tail.poll()
        for line in lines + ([tail.partial.decode("utf-8", "replace")] if tail.partial else []):
            prof.feed(line)
        if args.notes:
            print(f"notes: {load_notes(prof, args.notes)} marker(s) from {args.notes}")
        flush()
        print(f"wrote {report_path}")
        return 0

    if not args.game_log.exists():
        print(f"waiting for {args.game_log} ...")
    quit_evt = threading.Event()

    def on_hotkey():
        prof.add_marker()
        beep()

    if args.hotkey.lower() != "none":
        start_hotkey(args.hotkey, on_hotkey)

    def console():
        for line in sys.stdin:
            text = line.strip()
            if not text:
                continue
            action, arg = parse_command(text)
            if action == "quit":
                break
            if action == "report":
                flush()
                print(f"wrote {report_path}")
            elif action == "at":
                if prof.add_marker_at_clock(arg, now=datetime.now(timezone.utc)) is None:
                    print("usage: @HH:MM[:SS] label   (local time)")
            elif action == "label":
                ok = prof.label(*arg)
                print(f"labelled #{arg[0]}" if ok else f"no marker #{arg[0]}")
            else:
                prof.add_marker(label=arg)
        quit_evt.set()

    threading.Thread(target=console, daemon=True, name="console").start()
    print(f"tailing {args.game_log}\nreport: {report_path}\n"
          "MARKERS: F9 in game · note + Enter = mark now · @HH:MM note = mark earlier · "
          "'#N note' = label #N · r = report · q = quit")

    last_flush = time.monotonic()
    try:
        while not quit_evt.is_set():
            lines, restarted = tail.poll()
            if restarted:
                prof.log_restarted()
                print("game relaunched: log restarted")
            for line in lines:
                prof.feed(line)
            if time.monotonic() - last_flush > 60:
                flush()
                last_flush = time.monotonic()
            quit_evt.wait(0.5)
    except KeyboardInterrupt:
        pass
    flush()
    print(f"wrote {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
