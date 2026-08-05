# Deploying SC Nav for your org

**Status:** ✅ current · written 2026-08-04

A step-by-step guide for an org that wants to run its own SC Nav instance on a
cheap VPS. Written for **volunteers, not sysadmins**: everything after the
initial install happens in a web browser, and there is no ongoing command-line
work except one nightly backup job you set up once.

**Time to first login: about 2 hours**, most of it waiting on Discord and
Cloudflare forms. You do not need to know Docker, Linux, or how to configure a
web server.

---

## 1. Who does what

Split the work so one person isn't a permanent bottleneck.

| Role | How many | What they do | Needs server access? |
|---|---|---|---|
| **Server owner** | 1 (a backup is wise) | Does the one-time install below; clicks "redeploy" when there's an update | Yes |
| **Org admins** | 2–4 | Everything day-to-day: org settings, POI quality, member roles, clearing bad data | **No** — all in the app's ADMIN panel |
| **Members** | everyone | Use the app; run the watcher on their gaming PC | No |

The important part: **once installed, ~95% of administration happens inside
the app itself**, not on the server. Org admins never need to log into the VPS.

---

## 2. What to buy

**Network Solutions NVMe 4 — 2 vCPU / 4 GB RAM / 100 GB NVMe (~$4.18/mo intro).**

Buy it through the **[Docker/DevOps page](https://www.networksolutions.com/hosting/docker)**,
not the plain VPS page. They're the same product and the same price, but the
Docker page's checkout lets you preselect **Docker + Portainer**, which saves
you an install and gives you a web dashboard to manage everything. Choose
**Ubuntu** as the OS.

Why this tier, for an org of ~180 members with ~90 online at peak:

- **RAM:** the app uses ~160 MB at rest. Its one memory cost that scales with
  people is the breadcrumb trail (~2 MB per active member, worst case). Even
  with your *entire* org online at once with maxed trails, you'd use about
  930 MB — under a quarter of 4 GB.
- **CPU:** 2 cores is the sweet spot. The app is single-process by design and
  GIL-bound, so it can only use about one core no matter what you buy — the
  second core serves Docker, the tunnel, and the network stack. **NVMe 8 would
  be exactly as fast**; only buy it if you plan to run other services (a
  Discord bot, a wiki) on the same box.
- **Disk:** 100 GB is ~100× what this needs.

**Do not buy:** the cPanel add-on (unnecessary — Portainer is free and already
there), a dedicated IP (the Cloudflare Tunnel below needs no inbound
connection), or any DDoS/firewall add-on (Cloudflare covers it).

> ⚠️ **Budget warning.** That $4.18 is an introductory rate on a multi-year
> prepaid term. Renewal is meaningfully higher. Check the renewal price in the
> cart *before* committing, and put the year-2 number in the org's budget.

---

## 3. Before you start: gather these

Collect all of this first — the install goes smoothly if you're not hunting for
IDs halfway through.

- [ ] **A domain or subdomain**, e.g. `nav.yourorg.com`. If the org's site is at
      Network Solutions you can add a subdomain there, but the domain's DNS must
      be moved to Cloudflare (free) in step 5.
- [ ] **A Cloudflare account** (free tier is fine).
- [ ] **Discord Developer Mode on**: Discord → Settings → Advanced → Developer Mode.
- [ ] **Your Discord server ID**: right-click the server → Copy Server ID.
- [ ] **Admin user IDs**: right-click each admin's name → Copy User ID. Collect 2–4.
- [ ] *(Optional)* **A member role ID** if you want to restrict login to one role
      — right-click the role → Copy Role ID. Leave blank to allow any member of
      the server.
- [ ] **A session secret**: any random 64-character hex string. Generate one at
      a password generator, or run `openssl rand -hex 32` if you have a terminal.

---

## 4. Create the Discord sign-in app

The app has **no Discord bot** — it only verifies that whoever logs in is a
member of your server.

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
   → **New Application**. Name it after your org.
2. Left sidebar → **OAuth2**.
3. Under **Redirects**, click **Add Redirect** and enter exactly:
   `https://nav.yourorg.com/auth/callback`
   (substitute your real subdomain). **Save Changes.**
4. Copy the **Client ID** — you'll need it shortly.
5. Click **Reset Secret**, copy the **Client Secret**, and paste it somewhere
   safe. Discord shows it only once. **Treat it like a password.**

> The redirect URL must match your real public URL **character for character**,
> including `https://` and the `/auth/callback` path. A mismatch here is the
> single most common cause of "login just bounces back" — see the runbook.

---

## 5. Put it on the internet (Cloudflare Tunnel)

This is the step that removes the most long-term maintenance. A tunnel makes an
**outbound-only** connection from your server to Cloudflare, which means:

- No open ports, no firewall rules to manage
- No TLS certificate to install or renew — ever
- Your server's IP is never exposed

**Steps:**

1. Add your domain to Cloudflare (Add a Site → follow the nameserver
   instructions). This changes the domain's DNS to Cloudflare; existing website
   records are copied over automatically, but check them afterward.
2. Go to **Cloudflare Zero Trust** → **Networks** → **Tunnels** →
   **Create a tunnel** → choose **Cloudflared**. Name it `sc-nav`.
3. On the install screen, **ignore the install commands** — you don't need
   them. Just copy the long **token** string. That's your
   `CLOUDFLARE_TUNNEL_TOKEN`.
4. Under **Public Hostnames**, add:
   - **Subdomain:** `nav` · **Domain:** `yourorg.com`
   - **Service type:** `HTTP` · **URL:** `sc-nav:8765`

   That `sc-nav:8765` is the app's name inside Docker — not a typo, and not
   an IP address.
5. Save. The tunnel will show "inactive" until you finish step 6.

---

## 6. Deploy the app

Log into Portainer at the address Network Solutions gave you (usually
`https://your-server-ip:9443`) and set your admin password on first visit.

1. Left sidebar → **Stacks** → **Add stack**.
2. **Name:** `sc-nav`
3. **Build method:** choose **Repository**.
   - **Repository URL:** `https://github.com/ByteCollectiveIO/sc-nav`
   - **Reference:** `refs/heads/main`
   - **Compose path:** `docker-compose.yml`
4. Scroll to **Environment variables** → **Add an environment variable** for
   each row below. This is why we gathered everything in step 3.

| Name | Value |
|---|---|
| `DISCORD_CLIENT_ID` | from step 4 |
| `DISCORD_CLIENT_SECRET` | from step 4 — keep private |
| `OAUTH_REDIRECT_URI` | `https://nav.yourorg.com/auth/callback` |
| `ORG_GUILD_ID` | your Discord server ID |
| `ADMIN_IDS` | admin user IDs, comma-separated, no spaces |
| `SESSION_SECRET` | your random hex string |
| `SC_NAV_PUBLIC_URL` | `https://nav.yourorg.com` |
| `COOKIE_SECURE` | `true` |
| `ORG_MEMBER_ROLE_ID` | a role ID, or leave blank |
| `CLOUDFLARE_TUNNEL_TOKEN` | the token from step 5 |
| `STRATA_API_KEY` | leave blank (optional feature, add later) |

> ⚠️ `COOKIE_SECURE` must be the word **`true`**, not `1`. The code checks for
> the literal string `true`; `1` is read as *false* and quietly weakens your
> login cookies.

5. Click **Deploy the stack**. The first deploy builds the app from source and
   takes **3–6 minutes** on this tier. Later ones are faster.
6. Back in Cloudflare, the tunnel should flip to **Healthy** within a minute.
7. Visit `https://nav.yourorg.com`. You should see the login splash.

---

## 7. First login and day-one setup

Log in with Discord. Because your ID is in `ADMIN_IDS`, you'll have the ADMIN
panel.

**Do this first — the app looks broken without it:**

> 🚨 **Turn on the POI catalogs.** Go to **ORG SETTINGS** and enable both
> **starmap POIs** and **wiki POIs**. They ship **off** by default, and with
> them off the app knows about only **29 locations instead of 2,154**. New
> deployments regularly mistake this for a broken install. The wiki catalog is
> also *required* for the trade planner's ILLICIT cargo filter to work at all.

Then, while you're in ORG SETTINGS:

- **Branding** — set your org name (appears on the login splash and launcher).
- **Message of the day** — optional banner for members.
- **UEX price data** — leave the refresh interval at the default 6 hours. There
  is a hard 2-hour minimum; don't try to go below it.
- **Admins** — promote your other org admins here so you're not the only one.

Finally, **generate a watcher token** (Settings page) and walk one member
through the Setup page: they download a pre-configured watcher, run
`run_watcher.bat`, and type `/showlocation` in game. When their position shows
up on your map, the install is confirmed end to end.

---

## 8. Backups — the one thing that needs a terminal

Everything members create — custom POIs, survey marks, trade history,
inventory, goals — lives in a single database file. **It is irreplaceable.**
The repo ships a backup script; you set it up once and then forget it.

SSH into the server (Network Solutions provides the details) and run:

```bash
sudo apt update && sudo apt install -y sqlite3 git
sudo git clone https://github.com/ByteCollectiveIO/sc-nav /opt/sc-nav
docker volume ls | grep sc-nav        # note the exact volume name
```

Then `sudo crontab -e` and add one line (substituting the volume name you just
saw):

```
0 4 * * * SC_NAV_DATA=/var/lib/docker/volumes/sc-nav_sc-nav-data/_data SC_NAV_BACKUP_DIR=/var/backups/sc-nav /opt/sc-nav/server/deploy/backup_db.sh >> /var/log/sc-nav-backup.log 2>&1
```

That takes a safe copy every night at 4 AM without stopping the app, and keeps
the newest 14.

**Backups on the same server don't protect you from losing the server.** Once a
month, download a copy somewhere else — in Portainer, open the `sc-nav`
container → **Console**, or simply `scp` the newest file out of
`/var/backups/sc-nav`. Put a recurring reminder on the org calendar. Diarize it;
this is the step orgs skip and regret.

---

## 9. What maintenance actually looks like

| Task | How often | Who | Effort |
|---|---|---|---|
| Update the app | when a release is announced | server owner | Portainer → Stacks → `sc-nav` → **Pull and redeploy**. ~2 min |
| Game patch moved locations | per patch | org admin | ORG SETTINGS → **Refresh now**. No restart |
| Commodity prices | automatic | — | Refreshes every 6 h on its own |
| Nightly backup | automatic | — | The cron job from step 8 |
| Off-site backup copy | monthly | server owner | ~5 min |
| Ubuntu security updates | quarterly | server owner | `sudo apt update && sudo apt upgrade -y && sudo reboot` |
| Bad imported POI, spam post, wrong data | as needed | org admin | In-app ADMIN panel |

That's the whole ongoing commitment: **one click per release, five minutes a
month, and a quarterly reboot.**

---

## 10. Runbook — the things that will actually go wrong

**"Login bounces back to the splash / says invalid redirect."**
Your `OAUTH_REDIRECT_URI` doesn't exactly match the redirect registered on the
Discord app. Compare them character by character — `http` vs `https` and a
trailing slash both count. Fix whichever is wrong, then redeploy the stack.

**"Everyone got logged out after a restart."**
`SESSION_SECRET` is unset or was changed. Set it in Portainer's env vars and
redeploy. The container log says this explicitly on startup.

**"The site is down."**
Portainer → **Containers**. You should see `sc-nav` and `sc-nav-tunnel` both
green/running. If `sc-nav` is restarting, click it → **Logs** and read the last
20 lines — misconfiguration says so plainly. If both are running but the site
won't load, check the tunnel is **Healthy** in Cloudflare Zero Trust.

**"The map only knows a handful of places."**
The POI catalogs are off. See the 🚨 box in step 7.

**"We need to undo a bad data import / restore a backup."**
Stop the stack in Portainer, then on the server:
```bash
sudo gunzip -c /var/backups/sc-nav/sc_nav-YYYYMMDD-HHMMSS.db.gz > /tmp/restore.db
sudo cp /tmp/restore.db /var/lib/docker/volumes/sc-nav_sc-nav-data/_data/sc_nav.db
```
Start the stack again. (Ask the maintainer first if you're unsure — this
overwrites current data.)

---

## 11. Never do these

- **Never run `docker compose down -v`.** The `-v` deletes the volume — that is
  your entire database, every survey mark and custom POI the org has ever
  recorded. Removing the *stack* in Portainer can do the same thing; use
  **Pull and redeploy** to update, and don't delete the stack.
- **Never add `--workers` to the app.** It keeps live position and WebSocket
  state in memory in one process; a second worker would see half the picture.
- **Never commit or paste the Discord client secret, session secret, or tunnel
  token** into Discord, a ticket, or the repo. They live only in Portainer's
  environment variables.
- **Don't set `COOKIE_SECURE=1`** — see step 6.
- **Don't drop the UEX refresh below 2 hours.** It's rate-limited upstream and
  the server enforces the floor anyway.

---

## 12. If they outgrow it

The numbers in section 2 say NVMe 4 comfortably covers ~180 members. If the org
doubles, the first thing to feel it is memory from breadcrumb trails, and the
fix is a one-line constant change (`PATH_MAX` in `server/app.py`) rather than a
bigger server. Resizing the VPS one tier is also available and non-destructive.
Talk to the maintainer before either.
