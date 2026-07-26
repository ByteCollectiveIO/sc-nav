# SC Nav Watcher (Windows gaming PC)

Watches the clipboard for Star Citizen `/showlocation` output and forwards
the coordinates to the nav server on your LAN. Single file, standard library
only — no pip installs.

> **Downloaded this from the web UI's Setup page?** It's already configured for
> you — the server address is set and your access token is in
> `watcher_config.json`. Just install Python (step 1 below) and double-click
> `run_watcher.bat`; it'll ask once for your in-game handle. The manual steps
> below are only for setting it up by hand from a fresh copy.

## Setup on the gaming PC

1. Install Python 3.10+ from https://www.python.org/downloads/windows/
   (checking **"Add python.exe to PATH"** is nice but not required —
   `run_watcher.bat` finds Python via the `py` launcher either way).
   Leave **"tcl/tk and IDLE"** ticked — it's on by default, and it's what the
   optional in-game overlay draws with. Nothing else to install; the watcher
   uses only the standard library.
2. Copy this `watcher/` folder to the PC.
3. Edit `run_watcher.bat` and set your Linux server's address.
4. Double-click `run_watcher.bat` (or run from a terminal):

   ```
   python sc_nav_watcher.py --server http://192.168.1.50:8765
   ```

## In-game flow

1. Open chat (`F12` by default), type `/showlocation`, press Enter.
   The game copies your current position to the clipboard.
2. The watcher notices within ~250 ms, parses it, and POSTs it to the server.
3. Glance at the nav UI on your laptop/tablet.

Tip: chat history (up-arrow in the chat box) makes re-sending `/showlocation`
a three-keystroke action. A programmable keyboard/mouse macro can make it one.

## Options

| Flag | Default | Purpose |
|---|---|---|
| `--server URL` | — | Nav server base URL (required unless `--dry-run`) |
| `--interval N` | 0.25 | Clipboard poll interval, seconds |
| `--heartbeat N` | 60 | Re-send your last position every N seconds so you stay live on teammates' maps even while parked (a shard change re-sends instantly). `0` disables the timed re-send |
| `--timeout N` | 3.0 | HTTP timeout, seconds |
| `--dry-run` | off | Print payloads instead of sending |
| `--once` | off | Read clipboard once, send if valid, exit (connectivity test) |
| `--verbose` | off | Log non-location clipboard changes |
| `--handle NAME` | — | Your in-game handle, attached to captures for attribution |
| `--overlay-mode MODE` | off | `off` · `light` (the HUD) · `heavy` (beta browser window) — see below |
| `--overlay` / `--no-overlay` | off | Older aliases for `--overlay-mode light` / `off` |
| `--game-log PATH` | autodetect | Path to SC's `Game.log`; tags captures with your current shard so nodes from other servers can be filtered out |

`--handle`, `--token`, `--game-log`, and your overlay answer are saved to
`watcher_config.json` on first use and remembered after that, so you only need
to pass each once (or set `HANDLE` / `OVERLAY` in `run_watcher.bat`).

## In-game overlay (optional)

`run_watcher.bat` asks once and remembers your answer. Three choices:

| | What you get |
|---|---|
| **N** — none | No overlay. The watcher just reports your position. |
| **L** — light | A small always-on-top window with your **target, distance, ETA** and how old the reading is. Tiny, no browser, no login. |
| **H** — heavy *(beta)* | The **whole web app** in a browser window pinned over the game — every map, updating live. See below. |

### Light overlay

Shows your **current target, distance, ETA** and how old the reading is — so
you don't have to alt-tab to the browser for one line. Answer `L`, or pass
`--overlay-mode light`.

**Moving it:** drag it wherever you want and the position is remembered. In
game, **hold `F`** first — that's the key that frees your mouse cursor for
in-game menus, and it works for grabbing the overlay too. (Without it the game
keeps the cursor captive and the drag won't take.)

Three things to know before you turn it on:

- **Borderless/Windowed only.** In exclusive Fullscreen the game owns the
  screen and no overlay — ours, Discord's, or anyone's — is drawn over it. If
  you see nothing, check your graphics settings first.
- **It shows a bearing only on a planet/moon surface.** `/showlocation` reports
  where you are, not which way you're pointing, so in space there's no heading
  to draw an arrow against. You get distance and ETA there instead.
- **It's only as fresh as your last `/showlocation`.** The `fix` age in the
  corner is how old the reading is — it goes amber, then red. The number is
  frozen between chat commands; the overlay tells you so rather than pretending
  otherwise.

**If it ever disappears behind the game:** it re-asserts itself on top every
couple of seconds, so it should come back on its own. It has no taskbar button
by design (it's a bare window with no title bar), so if it's genuinely gone,
restarting the watcher always brings it back.

It's an ordinary window that reads our own server — no game hooks, no memory
reads, no automated keystrokes.

### Heavy overlay (beta)

Answer `H`, or pass `--overlay-mode heavy`. Instead of a HUD, the watcher opens
the **real web app** in a chrome-less browser window and keeps it pinned over
the game. You get every map and every app — navigator, Prospector, trade
planner — and unlike the light overlay they **update live**, because the
browser holds a real session.

Needs **Edge or Chrome** (Edge ships with Windows) and a browser you're already
signed into — it uses your normal profile, so the window opens logged in. If
neither browser is found, the watcher says so and carries on without it.

**Alt-tab to the window before you click around.** Star Citizen keeps reading
the mouse even when another window has your cursor, so moving the mouse over
the overlay can still turn your ship. Alt-tabbing to the browser and back is
the reliable way to park the game's controls while you use the app — holding
`F` isn't enough here (that frees the cursor, it doesn't stop the game reading
it). This is the game's input handling, not something the overlay can switch
off from the outside.

**One thing to know:** on that page, *your own* marker is the stale one.
Teammates move in real time; you only move when you run `/showlocation`. It's
the reverse of the light overlay, where everything is equally old.

It's marked **beta** because the window-pinning is Windows-specific and hasn't
been through much real use yet. If it misbehaves, answer `L` or `N` next
launch — nothing else about the watcher changes.

**"The overlay needs Python's tcl/tk and IDLE component"?** The overlay draws
with `tkinter`, which is part of Python itself — there's nothing to `pip
install`. It comes from the **"tcl/tk and IDLE"** option in the Python
installer, ticked by default, so you only see this if it was unticked (or on a
trimmed/embedded Python). To add it: **Settings → Apps → Python → Modify → tick
"tcl/tk and IDLE" → Install**, or reinstall from
https://www.python.org/downloads/windows/ leaving that box ticked. Either way
the watcher keeps reporting your position — you just don't get the overlay
until it's there.

The watcher tails `Game.log` for your **shard** (e.g. `pub_use1b_12030094_130`),
read from the `<Join PU>` and `<Update Shard Id>` lines, and includes it in each
position. SC's ephemeral nodes (resources/fauna) only exist on the shard they
were seen on, so the web UI uses this to hide nodes that aren't on your server
and to flag which teammates share your shard. If no `Game.log` is found (and
`--game-log` isn't given) the watcher still runs — captures just go out untagged
and aren't shard-filtered.

Because the game only copies coordinates when you run `/showlocation`, the watcher
also sends a **heartbeat** (`--heartbeat`, default 60s) that re-sends your last
known position so you don't age off teammates' maps while parked or AFK. The shard
is re-read from `Game.log` on every heartbeat, and any change (a relog or server
mesh handoff) re-sends immediately so the server re-tags you to the new server
without waiting for the interval.

Failed sends are queued (last 50) and retried automatically, so a nav-server
restart mid-session loses nothing.

## API contract (for the server side)

The watcher POSTs to `{server}/api/position` with `Content-Type: application/json`:

```json
{
  "x": -18930539540.392,
  "y": -2610158765.392,
  "z": 0.0,
  "raw": "Coordinates: x:-18930539540.392 y:-2610158765.392 z:0.0",
  "client_time": "2026-06-12T22:26:12.461474+00:00",
  "source": "sc_nav_watcher",
  "handle": "YourInGameName",
  "shard": "pub_use1b_12030094_130"
}
```

`shard` is `null` when no `Game.log` is available.

`x`/`y`/`z` are meters in the current star system's global frame (origin =
system center). Any 2xx response counts as delivered; anything else (or a
connection error) re-queues the payload.

## Notes

- Coordinate parsing is deliberately tolerant (axis order, `:` or `=`,
  thousands separators, surrounding text) because the exact `/showlocation`
  format has shifted between game patches. If a patch changes it beyond
  recognition, run with `--verbose` to see what the clipboard actually
  contains and update `_AXIS_PATTERNS` in `sc_nav_watcher.py`.
- On Windows the watcher uses the clipboard *sequence number*, so running
  `/showlocation` twice without moving still registers (heartbeat).
- The script also runs on macOS/Linux (pbpaste/xclip/wl-paste) for development.
- Tests: `python3 test_parse.py`
