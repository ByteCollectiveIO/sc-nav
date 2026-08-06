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

## What the watcher sends (and what it doesn't)

You are installing software on your gaming PC that talks to your org's server,
so here is the complete list. Everything below is verifiable in this folder —
it's plain Python, no build step, no dependencies.

| Sent to the org server | When |
|---|---|
| Your position (x/y/z, in meters) | Each time you run `/showlocation`, plus a re-send every 60 s while you're parked |
| Your in-game handle | You supply it yourself (`--handle`, or the prompt on first run) |
| Your shard id, e.g. `pub_use1b_…` | Read from `Game.log`, so teammates' maps can filter out other servers |
| Commodity kiosk buys/sells: shop name, commodity, total price, unit price, SCU, auto-load flag, box size/count | **On by default** — read from `Game.log` and used to keep the org's trade prices current. `--no-trade-capture` turns it off, and the choice sticks |

**Not sent, and not read at all:** the text on your clipboard (only the parsed
coordinates leave), chat, other players' names, your RSI account name, your IP
or the game server's, and anything outside `Game.log`. There is no keylogging,
no screenshots, and no reading of the game's memory.

**It does not touch the game.** The overlay is an ordinary always-on-top window
drawn beside Star Citizen — nothing is injected into the game process, no input
is simulated, and no graphics calls are hooked. Position data comes from CIG's
own `/showlocation` command and a read-only tail of `Game.log`.

**It never updates itself.** The watcher downloads no code and runs nothing it
didn't ship with. Updating means downloading a new bundle from the Setup page,
which is a deliberate act you take.

**Your access token is stored in plain text** in `watcher_config.json` next to
the script — that's what authenticates you to the org server. Keep the folder
out of OneDrive/Dropbox-synced locations, and if you think it leaked, revoke it
in the web UI under Settings → watcher tokens and download a fresh bundle.

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
| `--no-trade-capture` | capture on | Stop reporting your commodity kiosk buys/sells to the org's price data. Sticky — set it once and it's remembered |

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

**It ignores your mouse while you're flying.** Clicks pass straight through it
to the game, so a cursor that wanders across it — swinging a tractor-beamed box
to the far side of your screen, say — can't grab the overlay and drop what you
were doing. **Hold `F`** to make it clickable: that's the key that frees your
cursor for in-game menus anyway, and it's what the hint on the overlay has
always meant.

**Moving it:** hold `F` and drag it wherever you want; the position is
remembered.

**It gets out of the way.** Once you've been in game, alt-tabbing to Discord or
your browser hides the overlay instead of leaving it stuck on top of them. It
comes back the moment Star Citizen is in front again (and on its own a few
minutes after you quit the game).

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

**If it ever disappears behind the game:** it puts itself back on top whenever
it notices the flag was lost. It has no taskbar button by design (it's a bare
window with no title bar), so if it's genuinely gone, restarting the watcher
always brings it back.

It's an ordinary window that reads our own server — no game hooks, no memory
reads, no automated keystrokes. Making it click-through and noticing which
window is in front are ordinary Windows calls about *our own* window — nothing
is hooked, intercepted or injected anywhere near the game.

**Knobs** (optional, in `watcher_config.json` next to the script):

| Key | Default | What it does |
|---|---|---|
| `overlay_interact_key` | `"F"` | The hold-to-click key. Single letter or digit. |
| `overlay_clickthrough` | `true` | Set `false` for the old always-clickable behaviour. |
| `overlay_autohide` | `true` | Set `false` to keep it on screen over other apps. |
| `game_exe` | `"StarCitizen.exe"` | Only change this if CIG renames the binary. |

### Heavy overlay (beta)

Answer `H`, or pass `--overlay-mode heavy`. Instead of a HUD, the watcher opens
the **real web app** in a chrome-less browser window and keeps it pinned over
the game. You get every map and every app — navigator, Prospector, trade
planner — and unlike the light overlay they **update live**, because the
browser holds a real session.

Needs **Edge or Chrome** (Edge ships with Windows). If neither is found, the
watcher says so and carries on without it.

**First run asks you to sign in, once.** The overlay uses its own browser
profile (an `overlay-profile` folder next to the script) rather than your
everyday one. That's what lets it start the browser with the settings that keep
it from freezing behind a fullscreen game — handing a window to a browser
that's *already running* silently ignores them. Sign in once and it stays
signed in.

**Alt-tab to the window when you want to use it.** While Star Citizen is the
active window the overlay ignores your mouse entirely — clicks go to the game,
so it can't eat one — and it becomes a normal window again as soon as you
alt-tab to it. Click back into the game and it goes quiet again.

Note that Star Citizen keeps reading the mouse even when another window has
your cursor, so *moving* the mouse over the overlay can still turn your ship
while the game has focus. Alt-tabbing parks the controls. That part is the
game's input handling, not something the overlay can switch off from the
outside.

**If it ever locks up**, the watcher notices within a few seconds and reopens
it — up to three times, after which it says so and leaves it alone. Whatever
you had running (a trade route, a cargo run) lives on the server and is
unaffected either way.

**One thing to know:** on that page, *your own* marker is the stale one.
Teammates move in real time; you only move when you run `/showlocation`. It's
the reverse of the light overlay, where everything is equally old.

It's marked **beta** because the window-pinning is Windows-specific and hasn't
been through much real use yet. If it misbehaves, answer `L` or `N` next
launch — nothing else about the watcher changes.

**Knobs** (optional, in `watcher_config.json`):

| Key | Default | What it does |
|---|---|---|
| `heavy_clickthrough` | `"on"` | `"off"` keeps it always clickable; `"layered"` is a stronger form to try if clicks still land on the overlay while you fly. |
| `heavy_autorecover` | `true` | Set `false` to be told about a locked-up window instead of having it reopened. |
| `heavy_shared_profile` | `false` | `true` goes back to your everyday browser profile — no sign-in, but the anti-freeze settings only apply if no browser window is already open. |
| `heavy_x` / `heavy_y` / `heavy_w` / `heavy_h` | 60 / 60 / 720 / 520 | Where the window opens and how big. |

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
  "client_time": "2026-06-12T22:26:12.461474+00:00",
  "source": "sc_nav_watcher",
  "handle": "YourInGameName",
  "shard": "pub_use1b_12030094_130"
}
```

The parsed numbers are sent, never the clipboard text they came from. (A `raw`
field carrying up to 512 characters of clipboard text used to ride along; the
server never read it, so it was dropped. The server still accepts it so older
watchers keep working.)

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
