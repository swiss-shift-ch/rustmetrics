# RustMetrics

A free, ad-free server and player tracker for Facepunch's Rust.
Self-hosted, no third-party trackers, sign in with Steam (no email needed).

Live: **<https://rustmetrics.eu>**

## What it does

- **Personal watchlist** of Rust servers — live population, map, ping, wipe-day countdown.
- **Watched players** — add specific players to your watchlist, get pinged in Discord when they come online.
- **Live server browser** — Steam Web API for global, BattleMetrics fallback for regional / hardened servers.
- **Public per-server pages** — `/server/<ip>:<port>` with live status (indexable by Google for SEO).
- **Public per-player pages** — `/player/<bm_id>` with recent activity across all tracked servers, stable across name changes.
- **Steam OpenID 2.0 login** — never enters your Steam password. Reads only your public SteamID64, display name, avatar.
- **Detection of Facepunch "streamer-mode" anonymization** — flags servers where everyone shows as fake names so you know watchlisting won't work there.
- **Discord webhook notifications** — paste a webhook URL, get DMs/channel-pings when watched players come online/offline.
- **No ads, no premium tier, no analytics, no third-party scripts.** Only cookie is the session cookie.

## Why

BattleMetrics is great but locks player-tracking behind paid tiers and runs ads. I wanted a free tool that:

1. Lets me see if my friends are on without opening 4 tabs.
2. Doesn't email-spam me, doesn't track my browsing, doesn't try to upsell.
3. Is self-hostable on a single box without Docker / Kubernetes / a framework du jour.

So I built it over a few weekends.

## Stack

| Layer | Choice |
|---|---|
| Backend | Python 3.12, standard library only + `psycopg2-binary` |
| Web server / TLS | [Caddy](https://caddyserver.com/) (auto Let's Encrypt) |
| Database | PostgreSQL 15 |
| Frontend | Vanilla HTML / CSS / JavaScript (no framework, no build step) |
| Auth | Steam OpenID 2.0 |
| Data sources | A2S (Steam Server Query Protocol via UDP) → Steam Web API → BattleMetrics public API as fallback |
| Hosting | Single Hetzner CX22 box (Germany), systemd-managed |

No build process. Edit a file, deploy with `rsync`, `systemctl restart`. That's it.

## Architecture (zoomed in)

```
┌───────────────────────────────────────────────────────────────┐
│  Browser                                                      │
└──────┬────────────────────────────────────────────────────────┘
       │ HTTPS
┌──────▼─────────────────────────────────────────────────┐
│  Caddy  (auto-HTTPS, security headers, rate limit)     │
└──────┬─────────────────────────────────────────────────┘
       │ reverse_proxy 127.0.0.1:8765
┌──────▼─────────────────────────────────────────────────┐
│  rustmetrics.py  (Python stdlib http.server)           │
│                                                        │
│   ┌──────────────────┐  ┌──────────────────────────┐  │
│   │ Request handler  │  │  Poller (background)     │  │
│   │ - HTML SSR pages │  │  - 20s loop              │  │
│   │ - JSON API       │  │  - A2S UDP query         │  │
│   │ - Steam OpenID   │  │  - BattleMetrics fallback│  │
│   │ - Discord WH     │  │  - upsert players + diff │  │
│   └────────┬─────────┘  └─────────┬────────────────┘  │
│            └────────┬─────────────┘                    │
│              ┌──────▼──────┐                           │
│              │  Postgres   │                           │
│              └─────────────┘                           │
└────────────────────────────────────────────────────────┘
```

Single Python file (`rustmetrics.py`), no class hierarchies, no DI framework, no ORM.

## Setup

### Prerequisites

- Linux server (tested on Debian 12)
- Postgres 15+, Caddy 2+, Python 3.12+
- Domain pointed at the server

### Quick install

```bash
# 1. System packages
sudo apt-get update
sudo apt-get install -y python3 python3-psycopg2 postgresql caddy acl

# 2. Application user + directory
sudo useradd -m -d /opt/rustmetrics -r -s /bin/bash rustmetrics
sudo mkdir -p /opt/rustmetrics
sudo chown rustmetrics:rustmetrics /opt/rustmetrics

# 3. Code
sudo -u rustmetrics git clone https://github.com/<your-user>/rustmetrics-public.git /opt/rustmetrics
cd /opt/rustmetrics

# 4. Postgres
sudo -u postgres psql <<SQL
CREATE USER rustmetrics WITH PASSWORD 'CHANGE-ME-RANDOM-PASSWORD';
CREATE DATABASE rustmetrics OWNER rustmetrics;
SQL
sudo -u rustmetrics psql -d rustmetrics -f /opt/rustmetrics/init.sql

# 5. Config
sudo -u rustmetrics cp /opt/rustmetrics/.env.example /opt/rustmetrics/.env
sudo -u rustmetrics chmod 600 /opt/rustmetrics/.env
# Edit /opt/rustmetrics/.env — set RM_PGPASSWORD, RM_ORIGIN, RM_ADMIN_STEAMIDS, RM_STEAM_API_KEY

# 6. systemd unit
sudo cp /opt/rustmetrics/rustmetrics.service /etc/systemd/system/rustmetrics.service
sudo systemctl daemon-reload
sudo systemctl enable --now rustmetrics

# 7. Caddy
sudo bash -c 'cat /opt/rustmetrics/Caddyfile.snippet >> /etc/caddy/Caddyfile'
# Edit /etc/caddy/Caddyfile — replace rustmetrics.eu with your domain
sudo systemctl reload caddy

# 8. Verify
curl -sI https://your-domain.example/
```

### Steam Web API key (optional)

Without a key, Steam OpenID still works (logs you in), but display names and avatars stay blank.
Get a free key at <https://steamcommunity.com/dev/apikey> and set `RM_STEAM_API_KEY` in `.env`.

### Becoming admin

Add your SteamID64 to `RM_ADMIN_STEAMIDS` in `.env` (comma-separated for multiple admins).
After first login, you can also flip the flag directly in the DB:

```sql
UPDATE users SET is_admin = TRUE WHERE id = <your_steamid64>;
```

Admins get access to `/admin/stats` (users, sessions, engagement, traffic).

## Configuration

All settings via environment variables; see [`.env.example`](.env.example) for the full list with defaults.

## Privacy / Data stored

Per signed-in user:

- SteamID64 (public number, not the password)
- Display name and avatar URL (public, from Steam Web API)
- Session token (opaque, per-device, 30-day TTL)
- Watched servers (IP:port)
- Watched players (display name string)
- Optional Discord webhook URL (only used to send notifications, never read elsewhere)

Per server (global, shared across users):

- Server name, map, population snapshots (no PII)
- Player session-start/end (just the in-game display name, no SteamIDs unless surfaced by BattleMetrics)

No analytics, no third-party trackers, no email collection.
Sole cookie set: `rm_session` (HttpOnly, Secure, SameSite=Lax).

## Security

- TLS via Caddy + Let's Encrypt (auto-renew)
- Strong CSP (`script-src 'self'`, no `unsafe-inline`)
- Origin-header check on every state-changing endpoint (CSRF defense in depth)
- OpenID nonce cache (replay-attack defense)
- Steam-URL scheme whitelist on the client side
- Rate-limited (per-IP token-bucket: 120 req/min general, 12/h for browser cache misses)
- Discord webhook URL validated against `discord.com` regex (no SSRF)
- systemd hardening: `NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`, etc.

## Contributing

PRs welcome. Issues welcome.

Hard rules:

- Do not introduce build steps. Plain Python, plain HTML/CSS/JS.
- Do not add analytics, trackers, ads, or any third-party scripts.
- Do not add new dependencies unless they're already in Debian stable's repos.
- All inline scripts are CSP-blocked — load JS from `/static/*.js` files.

Soft rules:

- One feature per PR.
- Commits in English, conventional-commits style preferred (`feat:`, `fix:`, `refactor:`).
- If you add a route, add it to the README too.

## License

[MIT](LICENSE) — do what you want, just don't blame me.

## Credits

- Built solo by [Swiss-Shift](https://swiss-shift.ch/), Switzerland.
- Inspired (positively) by [BattleMetrics](https://www.battlemetrics.com/) — kudos for solving the original problem.
- Server data via [Valve A2S](https://developer.valvesoftware.com/wiki/Server_queries),
  [Steam Web API](https://partner.steamgames.com/doc/webapi),
  and the [BattleMetrics public API](https://www.battlemetrics.com/developers/documentation).
- Facepunch for making Rust.
