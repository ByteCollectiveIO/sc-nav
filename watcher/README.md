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
   (check **"Add python.exe to PATH"** in the installer).
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
| `--timeout N` | 3.0 | HTTP timeout, seconds |
| `--dry-run` | off | Print payloads instead of sending |
| `--once` | off | Read clipboard once, send if valid, exit (connectivity test) |
| `--verbose` | off | Log non-location clipboard changes |
| `--handle NAME` | — | Your in-game handle, attached to captures for attribution |

`--handle` is saved to `watcher_config.json` on first use and remembered after
that, so you only need to pass it once (or set `HANDLE` in `run_watcher.bat`).

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
  "handle": "YourInGameName"
}
```

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
