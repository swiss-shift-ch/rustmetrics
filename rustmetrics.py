#!/usr/bin/env python3
"""
RustMetrics — Public Multi-User Edition.

Public-Service-Version mit:
  • Steam OpenID 2.0 Login
  • Per-User Watchlist (eigene Watched-Server + Watched-Player)
  • Geteilte Snapshot-Daten (1× Polling pro Server, alle User profitieren)
  • Postgres-Backend
  • Rate-Limiting pro IP
  • Caddy-/systemd-ready

Konfiguration über Environment-Variablen (siehe .env.example).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import socket
import struct
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

import psycopg2
import psycopg2.extras
import psycopg2.pool


# ═══════════════════════════════════════════════════════════════════════════
#  KONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════

def _env(k, d=None, cast=str):
    v = os.environ.get(k, d)
    if v is None: return None
    try: return cast(v)
    except Exception: return d

HOST            = _env("RM_HOST", "127.0.0.1")
PORT            = _env("RM_PORT", 8765, int)
ORIGIN          = _env("RM_ORIGIN", "https://rustmetrics.eu").rstrip("/")

PG_DSN = (
    f"host={_env('RM_PGHOST', '127.0.0.1')} "
    f"port={_env('RM_PGPORT', 5432, int)} "
    f"user={_env('RM_PGUSER', 'rustmetrics')} "
    f"password={_env('RM_PGPASSWORD', '')} "
    f"dbname={_env('RM_PGDATABASE', 'rustmetrics')}"
)

ADMIN_STEAMIDS   = set(
    s.strip() for s in (_env("RM_ADMIN_STEAMIDS", "") or "").split(",") if s.strip()
)
STEAM_API_KEY    = _env("RM_STEAM_API_KEY", "") or None
SESSION_TTL      = _env("RM_SESSION_TTL", 30 * 86400, int)
POLL_INTERVAL    = _env("RM_POLL_INTERVAL_SEC", 20, int)
A2S_TIMEOUT      = _env("RM_A2S_TIMEOUT_SEC", 3.0, float)
SNAPSHOT_TTL_DAY = _env("RM_SNAPSHOTS_KEEP_DAYS", 14, int)

BROWSE_CACHE_TTL = _env("RM_BROWSE_CACHE_TTL", 180, int)
BROWSE_PARALLEL  = _env("RM_BROWSE_PARALLEL", 80, int)
BROWSE_TIMEOUT   = _env("RM_BROWSE_A2S_TIMEOUT", 2.5, float)

RL_BROWSE_PER_H  = _env("RM_RL_BROWSE_PER_HOUR", 12, int)
RL_REQ_PER_MIN   = _env("RM_RL_REQUESTS_PER_MIN", 120, int)

# Caddy-Access-Log Pfad (für /admin/stats Page). Auf nicht-existierende Datei
# fällt der Parser still zurück auf "log unavailable".
CADDY_LOG_PATH   = _env("RM_CADDY_LOG", "/var/log/caddy/rustmetrics.log")
CADDY_LOG_MAX_BYTES = _env("RM_CADDY_LOG_MAX_BYTES", 5 * 1024 * 1024, int)  # nur die letzten 5 MB lesen

ROOT             = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR       = os.path.join(ROOT, "static")
TEMPLATES_DIR    = os.path.join(ROOT, "templates")

RUST_APPID       = 252490
# Valve hat den Public-Master-Server Q4-2025 abgeschaltet (DNS NXDOMAIN, IPs tot).
# Wir benutzen jetzt einen Hybrid-Stack:
#   1. Steam Web API (IGameServersService/GetServerList) — wenn RM_STEAM_API_KEY gesetzt
#   2. BattleMetrics Public API als Fallback — ohne Key, rate-limited
STEAM_GETSERVERLIST_URL = "https://api.steampowered.com/IGameServersService/GetServerList/v1/"
STEAM_GETUSERSTATS_URL  = "https://api.steampowered.com/ISteamUserStats/GetUserStatsForGame/v2/"
BATTLEMETRICS_URL       = "https://api.battlemetrics.com/servers"
BATTLEMETRICS_PLAYER_URL = "https://api.battlemetrics.com/players"

# Cache-TTL für Rust-Stats (Steam-Counter ändern sich langsam, 1h reicht für Live-Page).
RUST_STATS_CACHE_TTL = _env("RM_RUST_STATS_CACHE_TTL", 3600, int)

# Region-Mapping für BM (BM filtert nach ISO-Ländercodes statt Steam-Region-Bytes)
REGION_TO_COUNTRIES = {
    "eu":   "DE,FR,NL,GB,CH,AT,SE,FI,NO,PL,IT,ES,IE,DK,BE,CZ,HU,PT,RO",
    "us-e": "US",  # BM differenziert nicht E/W
    "us-w": "US",
    "sa":   "BR,AR,CL,UY,CO,PE,MX",
    "asia": "JP,KR,HK,SG,TW,TH,VN,ID,MY",
    "au":   "AU,NZ",
    "me":   "AE,SA,IL,TR",
    "af":   "ZA",
}
BROWSE_MASTER_LIMIT = 800
REGION_BYTES     = {
    "all": 0xFF, "us-e": 0x00, "us-w": 0x01, "sa": 0x02,
    "eu":  0x03, "asia": 0x04, "au":  0x05, "me": 0x06, "af": 0x07,
}
COOKIE_NAME      = "rm_session"

A2S_HEADER             = b"\xFF\xFF\xFF\xFF"
A2S_INFO_REQUEST       = A2S_HEADER + b"TSource Engine Query\x00"
A2S_PLAYER_REQHDR      = b"\x55"
A2S_CHALLENGE_TYPE     = b"\x41"


# ═══════════════════════════════════════════════════════════════════════════
#  A2S — Steam Server Query Protocol  (unverändert ggü. lokaler Version)
# ═══════════════════════════════════════════════════════════════════════════

def _read_cstring(data: bytes, pos: int) -> tuple[str, int]:
    end = data.index(b"\x00", pos)
    return data[pos:end].decode("utf-8", errors="replace"), end + 1


def query_a2s(host: str, port: int, timeout: float = None) -> dict:
    if timeout is None: timeout = A2S_TIMEOUT
    result = {"host": host, "port": port, "online": False, "queried_at": int(time.time())}
    try:
        addr = (host, port)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            t0 = time.time()
            s.sendto(A2S_INFO_REQUEST, addr)
            data, _ = s.recvfrom(2048)
            if len(data) >= 5 and data[4:5] == A2S_CHALLENGE_TYPE:
                challenge = data[5:9]
                s.sendto(A2S_INFO_REQUEST + challenge, addr)
                data, _ = s.recvfrom(2048)
            result["ping_ms"] = int((time.time() - t0) * 1000)
            result.update(_parse_a2s_info(data))
            result["online"] = True
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(timeout)
                s.sendto(A2S_HEADER + A2S_PLAYER_REQHDR + b"\xFF\xFF\xFF\xFF", addr)
                data, _ = s.recvfrom(4096)
                if len(data) >= 5 and data[4:5] == A2S_CHALLENGE_TYPE:
                    challenge = data[5:9]
                    s.sendto(A2S_HEADER + A2S_PLAYER_REQHDR + challenge, addr)
                    data, _ = s.recvfrom(8192)
                if data[:4] == b"\xFE\xFF\xFF\xFF":
                    result["players_list"] = []
                else:
                    result["players_list"] = _parse_a2s_players(data)
        except Exception as e:
            result["players_list"] = []
            result["players_error"] = f"{type(e).__name__}: {e}"
    except socket.timeout:
        result["error"] = "timeout"
    except OSError as e:
        result["error"] = f"OSError: {e}"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    return result


def _parse_a2s_info(data: bytes) -> dict:
    if len(data) < 6 or data[4:5] != b"I":
        return {}
    pos = 5
    protocol = data[pos]; pos += 1
    name, pos    = _read_cstring(data, pos)
    mapname, pos = _read_cstring(data, pos)
    folder, pos  = _read_cstring(data, pos)
    game, pos    = _read_cstring(data, pos)
    app_id = struct.unpack_from("<H", data, pos)[0]; pos += 2
    players = data[pos]; pos += 1
    max_players = data[pos]; pos += 1
    bots = data[pos]; pos += 1
    server_type = chr(data[pos]); pos += 1
    environment = chr(data[pos]); pos += 1
    visibility = data[pos]; pos += 1
    vac = data[pos]; pos += 1
    version, pos = _read_cstring(data, pos)
    keywords, gameid = "", 0
    if pos < len(data):
        edf = data[pos]; pos += 1
        if edf & 0x80: pos += 2
        if edf & 0x10: pos += 8
        if edf & 0x40:
            pos += 2
            _, pos = _read_cstring(data, pos)
        if edf & 0x20: keywords, pos = _read_cstring(data, pos)
        if edf & 0x01:
            gameid = struct.unpack_from("<Q", data, pos)[0]; pos += 8
    return {
        "protocol": protocol, "name": name, "map": mapname,
        "folder": folder, "game": game, "app_id": app_id,
        "players_count": players, "max_players": max_players, "bots": bots,
        "server_type": server_type, "environment": environment,
        "visibility": visibility, "vac": vac, "version": version,
        "keywords": keywords, "gameid": gameid,
    }


def _parse_a2s_players(data: bytes) -> list[dict]:
    if len(data) < 6 or data[4:5] != b"D":
        return []
    count = data[5]; pos = 6
    out = []
    for _ in range(count):
        if pos >= len(data): break
        try:
            _idx = data[pos]; pos += 1
            name, pos = _read_cstring(data, pos)
            score    = struct.unpack_from("<i", data, pos)[0]; pos += 4
            duration = struct.unpack_from("<f", data, pos)[0]; pos += 4
            out.append({"name": name, "score": score, "duration_s": int(duration)})
        except Exception:
            break
    return out


# ═══════════════════════════════════════════════════════════════════════════
#  Server-List-Datenquellen (Steam-Web-API + BattleMetrics-Fallback)
#  Valve hat den Public-UDP-Master Q4-2025 abgeschaltet — diese zwei HTTP-APIs
#  ersetzen ihn.
# ═══════════════════════════════════════════════════════════════════════════

def query_steam_web_api(region: int = 0xFF, limit: int = BROWSE_MASTER_LIMIT) -> list[dict]:
    """
    IGameServersService/GetServerList — Steam Web API.
    Liefert komplette Server-Daten in einem HTTPS-Call, kein separates A2S nötig.
    Filter-Syntax identisch zum alten Master-Protokoll.
    Braucht STEAM_API_KEY in der .env.
    """
    if not STEAM_API_KEY:
        return []
    filter_parts = [rf"\appid\{RUST_APPID}", r"\dedicated\1"]
    if region != 0xFF:
        filter_parts.append(rf"\region\{region}")
    params = {"key": STEAM_API_KEY, "filter": "".join(filter_parts), "limit": str(limit)}
    url = STEAM_GETSERVERLIST_URL + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"[steam_web_api] error: {e}", file=sys.stderr, flush=True)
        return []
    out = []
    for s in payload.get("response", {}).get("servers", []):
        addr = s.get("addr", "")
        if ":" not in addr:
            continue
        host, port_str = addr.rsplit(":", 1)
        try:
            port = int(port_str)
        except ValueError:
            continue
        out.append({
            "host": host, "port": port,
            "name": s.get("name") or "",
            "map":  s.get("map"),
            "players_count": s.get("players") or 0,
            "max_players":   s.get("max_players") or 0,
            "ping_ms":       None,
            "keywords":      s.get("gametype") or "",   # Steam liefert Keywords als "gametype"
            "online":        True,
            "_source":       "steam_web_api",
        })
    return out


def query_battlemetrics(region: str = "all", limit: int = BROWSE_MASTER_LIMIT) -> list[dict]:
    """
    Public BattleMetrics-API als Fallback. Rate-limited (~60 req/min unauth),
    aber kein Key nötig. Attribution-Pflicht bei produktivem Einsatz.
    """
    fetched: list[dict] = []
    page_size = 100  # BM-Max pro Page
    base_params = {
        "filter[game]":   "rust",
        "filter[status]": "online",
        "page[size]":     str(page_size),
        "sort":           "-players",
    }
    if region != "all" and region in REGION_TO_COUNTRIES:
        base_params["filter[countries]"] = REGION_TO_COUNTRIES[region]
    url = BATTLEMETRICS_URL + "?" + urllib.parse.urlencode(base_params)
    pages = 0
    while url and len(fetched) < limit and pages < 8:
        pages += 1
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "RustMetrics/1.0 (+https://rustmetrics.eu)"})
            with urllib.request.urlopen(req, timeout=10) as r:
                payload = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            print(f"[battlemetrics] page {pages} error: {e}", file=sys.stderr, flush=True)
            break
        for s in payload.get("data", []):
            a = s.get("attributes", {}) or {}
            details = a.get("details", {}) or {}
            kw_tags = details.get("rust_modded_tags") or []
            tier_hint = details.get("rust_type") or ""   # "vanilla" / "modded"
            keywords = ",".join([t for t in (kw_tags + [tier_hint]) if t])
            if a.get("status") != "online":
                continue
            fetched.append({
                "host":          a.get("ip") or "",
                "port":          a.get("port") or 28015,
                "name":          a.get("name") or "",
                "map":           details.get("map"),
                "players_count": a.get("players") or 0,
                "max_players":   a.get("maxPlayers") or 0,
                "ping_ms":       None,
                "keywords":      keywords,
                "online":        True,
                "_source":       "battlemetrics",
                "_bm_rank":      a.get("rank"),
                "_bm_country":   a.get("country"),
            })
        url = (payload.get("links") or {}).get("next")
    return fetched[:limit]


def fetch_server_list(region: str = "all", limit: int = BROWSE_MASTER_LIMIT) -> tuple[list[dict], str]:
    """
    Hybrid:
      • region='all' und Steam-Key vorhanden → Steam Web API (global, schnell, kein Dritt-Anbieter)
      • region=eu/us-e/… → BattleMetrics (deren Country-Filter ist zuverlässig;
        Steams \\region\\X Filter ist unbrauchbar, weil die meisten Rust-Server
        \\region\\-1 reporten)
    Fallback in beide Richtungen falls die primäre Quelle leer kommt.
    """
    if region == "all" and STEAM_API_KEY:
        servers = query_steam_web_api(region=0xFF, limit=limit)
        if servers:
            return servers, "steam_web_api"
        print("[fetch] Steam Web API leer, falle zurück auf BattleMetrics", file=sys.stderr, flush=True)
    servers = query_battlemetrics(region=region, limit=limit)
    if servers:
        return servers, "battlemetrics"
    # Notfall: wenn BM auch leer und Steam noch nicht versucht
    if STEAM_API_KEY:
        servers = query_steam_web_api(region=0xFF, limit=limit)
        if servers:
            return servers, "steam_web_api"
    return [], "none"


# ── BattleMetrics per-Server Player-List Fallback ─────────────────────────
# Viele grosse Rust-Server (Atlas, Moose, Reborn, Helli's …) blocken A2S_PLAYER.
# BM hat die Daten trotzdem (whitelisted/Steamworks-Pfad). Wir nutzen sie als
# Fallback wenn unser eigener A2S-Call leer/garbage zurückgibt.

_bm_id_cache: dict = {}
_bm_id_lock = threading.Lock()
BM_ID_CACHE_TTL = 86400  # 24h — BM-Server-IDs sind stabil

def _bm_search_id(host: str, port: int) -> int | None:
    """
    BM-Search behandelt ':' im search-Term als Text-Token und matcht im Namen.
    Suchen daher nur per IP, dann clientseitig auf Port filtern.
    """
    try:
        url = (BATTLEMETRICS_URL + "?" + urllib.parse.urlencode({
            "filter[game]":    "rust",
            "filter[search]":  host,
            "page[size]":      "10",
        }))
        req = urllib.request.Request(url, headers={
            "User-Agent": "RustMetrics/1.0 (+https://rustmetrics.eu)"
        })
        with urllib.request.urlopen(req, timeout=8) as r:
            payload = json.loads(r.read().decode("utf-8"))
        # Erst exakter IP+Port-Match
        for s in payload.get("data", []):
            a = s.get("attributes") or {}
            if a.get("ip") == host and a.get("port") == port:
                try: return int(s.get("id"))
                except (TypeError, ValueError): pass
        # Sonst: erster Match auf gleicher IP (Port-Mismatch z.B. weil Game-Port ≠ Query-Port)
        for s in payload.get("data", []):
            a = s.get("attributes") or {}
            if a.get("ip") == host:
                try: return int(s.get("id"))
                except (TypeError, ValueError): pass
    except Exception as e:
        print(f"[bm_search] {host}:{port}: {e}", file=sys.stderr, flush=True)
    return None

def get_bm_id(host: str, port: int) -> int | None:
    """Cached BM-ID-Lookup für Host:Port."""
    key = (host, port)
    with _bm_id_lock:
        e = _bm_id_cache.get(key)
        if e and (time.time() - e["ts"]) < BM_ID_CACHE_TTL:
            return e["id"]
    bm_id = _bm_search_id(host, port)
    with _bm_id_lock:
        _bm_id_cache[key] = {"ts": time.time(), "id": bm_id}
    return bm_id

def query_bm_server(host: str, port: int) -> tuple[dict | None, list[tuple[int, str]]]:
    """
    Holt Server-Daten + Spielerliste von BM.
    Returns (server_attrs, [(bm_player_id, name), …]) — bm_player_id ist 0 wenn nicht parsbar.
    """
    bm_id = get_bm_id(host, port)
    if not bm_id:
        return None, []
    try:
        url = f"{BATTLEMETRICS_URL}/{bm_id}?include=player"
        req = urllib.request.Request(url, headers={
            "User-Agent": "RustMetrics/1.0 (+https://rustmetrics.eu)"
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"[bm_detail] id={bm_id}: {e}", file=sys.stderr, flush=True)
        return None, []
    attrs = ((payload.get("data") or {}).get("attributes")) or {}
    pairs: list[tuple[int, str]] = []
    for p in (payload.get("included") or []):
        if p.get("type") != "player": continue
        nm = (p.get("attributes") or {}).get("name")
        if not nm: continue
        try: pid = int(p.get("id"))
        except (TypeError, ValueError): pid = 0
        pairs.append((pid, nm))
    return attrs, pairs


# Behalten wir für individuelle Server-Polls (z.B. wenn der User direkt eine IP hinzufügt)
def query_a2s_batch(addrs, workers=BROWSE_PARALLEL, timeout=BROWSE_TIMEOUT) -> list[dict]:
    results: list[dict] = []
    if not addrs: return results
    with ThreadPoolExecutor(max_workers=workers) as ex:
        future_map = {ex.submit(query_a2s, ip, port, timeout): (ip, port)
                      for ip, port in addrs}
        for fut in as_completed(future_map):
            try:
                r = fut.result()
                if r.get("online"): results.append(r)
            except Exception:
                pass
    return results


# In-Memory Cache für Browse-Ergebnisse
_browse_cache: dict = {}
_browse_lock = threading.Lock()

def _cache_get(key, ttl=BROWSE_CACHE_TTL):
    with _browse_lock:
        e = _browse_cache.get(key)
        if e and (time.time() - e["ts"]) < ttl:
            return e["data"]
    return None

def _cache_put(key, data):
    with _browse_lock:
        _browse_cache[key] = {"ts": time.time(), "data": data}


# ═══════════════════════════════════════════════════════════════════════════
#  Rust-spezifische Helpers
# ═══════════════════════════════════════════════════════════════════════════

def parse_rust_keywords(kw: str) -> dict:
    out: dict = {"tags": []}
    if not kw: return out
    for part in [p.strip() for p in kw.split(",") if p.strip()]:
        if   part.startswith("mp"):
            try: out["max_players_kw"] = int(part[2:])
            except ValueError: pass
        elif part.startswith("cp"):
            try: out["current_players_kw"] = int(part[2:])
            except ValueError: pass
        elif part.startswith("qp"):
            try: out["queued_kw"] = int(part[2:])
            except ValueError: pass
        elif part.startswith("v") and part[1:].isdigit():
            out["version_kw"] = int(part[1:])
        elif part.startswith("born"):
            try: out["born_ts"] = int(part[4:])
            except ValueError: pass
        elif part.startswith("gm"): out["game_mode"] = part[2:]
        elif part.startswith("h") and part[1:].isdigit(): out["hash"] = part[1:]
        elif part == "stok": out["server_tokenized"] = True
        else: out["tags"].append(part)
    return out


def detect_rust_tier(name, tags) -> str:
    n = (name or "").lower() + " " + " ".join(tags).lower()
    if "pve" in n: return "pve"
    for mult in ("100x","50x","20x","10x","5x","3x","2x"):
        if mult in n: return mult
    if "modded" in n or "oxide" in tags: return "modded"
    if "vanilla" in n: return "vanilla"
    return "vanilla"


def detect_name_anonymization(player_names: list[str]) -> bool:
    """
    Erkennt Facepunch-style Name-Randomisierung. Heuristik fingerprintet:
      - Mehrere Duplikate (im echten Rust ungewöhnlich)
      - PLUS: hoher Anteil pure-lowercase-alpha-kurze-Namen (Facepunch zieht aus einem
        Pool von ~500 generischen kleinbuchstabigen Vornamen wie 'carmelo','gena')
    Beide Bedingungen zusammen sind nötig — Reddit/Stevious-style Server mit echten
    Mixed-Case-Namen + 1-2 Zufalls-Duplikaten dürfen nicht fälschlich anschlagen.
    """
    real_names = [n for n in player_names if (n or "").strip()]
    if len(real_names) < 15:
        return False
    seen = set()
    dupes = 0
    lower_alpha = 0
    for n in real_names:
        key = n.strip().lower()
        if key in seen: dupes += 1
        else: seen.add(key)
        # "Pure lowercase short alpha" = look like Facepunch's randomized pool entries
        if 3 <= len(n) <= 12 and n.islower() and n.isalpha():
            lower_alpha += 1
    lower_ratio = lower_alpha / len(real_names)
    # Strenger Filter: ≥3 Duplikate UND ≥70% pure-lowercase-alpha-short
    return dupes >= 3 and lower_ratio >= 0.70


def next_wipe_estimate(born_ts):
    if not born_ts: return None
    now = int(time.time())
    week = 7 * 86400
    if born_ts > now:
        return {"last_wipe_ts": born_ts, "next_wipe_estimate_ts": born_ts, "days_until_next": 0}
    weeks_since = (now - born_ts) // week
    next_wipe = born_ts + (weeks_since + 1) * week
    return {
        "last_wipe_ts": born_ts,
        "next_wipe_estimate_ts": next_wipe,
        "days_until_next": max(0, (next_wipe - now) // 86400),
    }


# ═══════════════════════════════════════════════════════════════════════════
#  POSTGRES — Connection-Pool + Helpers
# ═══════════════════════════════════════════════════════════════════════════

_pool: psycopg2.pool.ThreadedConnectionPool = None  # type: ignore

def init_pool():
    global _pool
    _pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=2, maxconn=20, dsn=PG_DSN
    )

class _Conn:
    """Context-Manager: holt Connection aus Pool, gibt sie sicher zurück."""
    def __init__(self, cursor=True):
        self._cursor = cursor
    def __enter__(self):
        self.conn = _pool.getconn()
        self.cur = self.conn.cursor(cursor_factory=psycopg2.extras.DictCursor) if self._cursor else None
        return (self.conn, self.cur) if self._cursor else self.conn
    def __exit__(self, exc_type, exc, tb):
        if self.cur:
            try: self.cur.close()
            except Exception: pass
        try:
            if exc_type is None: self.conn.commit()
            else: self.conn.rollback()
        finally:
            _pool.putconn(self.conn)


# ═══════════════════════════════════════════════════════════════════════════
#  STEAM OPENID 2.0
# ═══════════════════════════════════════════════════════════════════════════

STEAM_OPENID_URL = "https://steamcommunity.com/openid/login"
STEAM_ID_PATTERN = re.compile(r"^https?://steamcommunity\.com/openid/id/(\d+)$")

# OpenID-Nonce-Replay-Schutz (Sicherheits-Audit #6)
_openid_nonces: dict[str, float] = {}
_openid_nonce_lock = threading.Lock()
OPENID_NONCE_TTL = 600  # 10 Minuten — Steam-Nonces sind timestamp-basiert mit kurzer Gültigkeit

def _openid_nonce_seen(nonce: str) -> bool:
    """Returns True wenn nonce schon mal gesehen (= replay-Versuch). Idempotent verzeichnet sich selbst."""
    if not nonce: return False
    now = time.time()
    with _openid_nonce_lock:
        # Lazy-Cleanup wenn map zu gross
        if len(_openid_nonces) > 2000:
            cutoff = now - OPENID_NONCE_TTL
            for k in list(_openid_nonces.keys()):
                if _openid_nonces[k] < cutoff:
                    del _openid_nonces[k]
        if nonce in _openid_nonces:
            return True
        _openid_nonces[nonce] = now
        return False

def steam_login_url() -> str:
    params = {
        "openid.ns":         "http://specs.openid.net/auth/2.0",
        "openid.mode":       "checkid_setup",
        "openid.return_to":  f"{ORIGIN}/auth/steam/callback",
        "openid.realm":      f"{ORIGIN}/",
        "openid.identity":   "http://specs.openid.net/auth/2.0/identifier_select",
        "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
    }
    return f"{STEAM_OPENID_URL}?{urllib.parse.urlencode(params)}"


def steam_verify(query: dict) -> int | None:
    """
    Verifiziert eine OpenID-Antwort von Steam via check_authentication.
    Gibt SteamID64 (int) zurück bei Erfolg, sonst None.
    """
    if query.get("openid.mode") != "id_res": return None
    claimed = query.get("openid.claimed_id", "")
    m = STEAM_ID_PATTERN.match(claimed)
    if not m: return None
    steamid64 = int(m.group(1))
    # return_to muss zu unserer Domain passen
    expected_return = f"{ORIGIN}/auth/steam/callback"
    if not query.get("openid.return_to", "").startswith(expected_return):
        return None
    # Replay-Schutz: Steam-Nonce nur einmal akzeptieren
    nonce = query.get("openid.response_nonce", "")
    if nonce and _openid_nonce_seen(nonce):
        print(f"[openid] replay attempt rejected (nonce={nonce[:30]}…)", file=sys.stderr, flush=True)
        return None
    # check_authentication: alle Parameter zurück an Steam, mode überschreiben
    verify_params = dict(query)
    verify_params["openid.mode"] = "check_authentication"
    data = urllib.parse.urlencode(verify_params).encode("ascii")
    req = urllib.request.Request(
        STEAM_OPENID_URL, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            body = r.read().decode("utf-8", errors="replace")
    except Exception:
        return None
    is_valid = False
    for line in body.splitlines():
        k, _, v = line.partition(":")
        if k.strip() == "is_valid" and v.strip() == "true":
            is_valid = True
            break
    return steamid64 if is_valid else None


def fetch_steam_profile(steamid64: int) -> dict:
    """Holt Display-Name + Avatar via Steam Web API (optional, braucht API-Key)."""
    if not STEAM_API_KEY:
        return {}
    url = (
        "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/"
        f"?key={STEAM_API_KEY}&steamids={steamid64}"
    )
    try:
        with urllib.request.urlopen(url, timeout=6) as r:
            data = json.loads(r.read().decode("utf-8"))
        players = data.get("response", {}).get("players", [])
        if not players: return {}
        p = players[0]
        return {
            "display_name": p.get("personaname"),
            "avatar_url":   p.get("avatarfull"),
            "profile_url":  p.get("profileurl"),
        }
    except Exception:
        return {}


# ═══════════════════════════════════════════════════════════════════════════
#  RUST USER-STATS (Steam GetUserStatsForGame, appid 252490)
# ═══════════════════════════════════════════════════════════════════════════
#
# Rust selber lädt ~80 In-Game-Counter pro Spieler via Steam-Stats-API hoch
# (Kills, Deaths, gesammelte Materialien, Bullet-Hits per Tier-Tier, etc.).
# Wir fetchen die via Steam Web API und cachen in DB (1h TTL).
# Profile mit privatem "Game Details"-Setting liefern 403/leeres Result,
# das wird als is_private=TRUE markiert.

# Mapping von Steam-Stat-Namen → DB-Spalte. Nur die ~30 wichtigsten;
# raw_json speichert den kompletten Response für Future-Use.
RUST_STAT_COLUMNS = {
    "seconds_played":           "seconds_played",
    "deaths":                   "deaths",
    "kill_player":              "kill_player",
    "headshot":                 "headshot",
    "wounded":                  "wounded",
    "bullet_fired":             "bullet_fired",
    "bullet_hit_player":        "bullet_hit_player",
    "bullet_hit_building":      "bullet_hit_building",
    "bullet_hit_sign":          "bullet_hit_sign",
    "bullet_hit_wolf":          "bullet_hit_wolf",
    "bullet_hit_bear":          "bullet_hit_bear",
    "bullet_hit_boar":          "bullet_hit_boar",
    "bullet_hit_stag":          "bullet_hit_stag",
    "bullet_hit_horse":         "bullet_hit_horse",
    "bullet_hit_corpse":        "bullet_hit_corpse",
    "arrow_fired":              "arrow_fired",
    "arrow_hit_player":         "arrow_hit_player",
    "arrow_hit_entity":         "arrow_hit_entity",
    "harvested_wood":           "harvested_wood",
    "harvested_stones":         "harvested_stones",
    "harvested_cloth":          "harvested_cloth",
    "harvested_leather":        "harvested_leather",
    "harvested_sulfur.ore":     "harvested_sulfur_ore",
    "harvested_metal.ore":      "harvested_metal_ore",
    "harvested_hq.metal.ore":   "harvested_hq_metal_ore",
    "acquired_scrap":           "acquired_scrap",
    "acquired_lowgradefuel":    "acquired_lowgradefuel",
    "acquired_metal.fragments": "acquired_metalfrag",
    "acquired_sulfur":          "acquired_sulfur",
    "seconds_cold":             "seconds_cold",
    "seconds_hot":              "seconds_hot",
    "seconds_comfort":          "seconds_comfort",
    "melee_thrown":             "melee_thrown",
    "c4_thrown":                "c4_thrown",
    "rocket_fired":             "rocket_fired",
}


def query_rust_user_stats(steamid64: int) -> dict:
    """Roh-Fetch von Steam ISteamUserStats/GetUserStatsForGame.
    Returns: {'is_private': bool, 'stats': {steam_stat_name: int}, 'raw': dict, 'error': str|None}"""
    if not STEAM_API_KEY:
        return {"is_private": False, "stats": {}, "raw": None, "error": "no Steam API key configured"}
    url = (f"{STEAM_GETUSERSTATS_URL}?key={STEAM_API_KEY}"
           f"&appid={RUST_APPID}&steamid={int(steamid64)}")
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "RustMetrics/1.0 (+https://rustmetrics.eu)"
        })
        with urllib.request.urlopen(req, timeout=8) as r:
            raw = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 403 / 400 → profile private oder Game-Details private
        if e.code in (400, 403):
            return {"is_private": True, "stats": {}, "raw": None, "error": None}
        return {"is_private": False, "stats": {}, "raw": None, "error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e:
        return {"is_private": False, "stats": {}, "raw": None, "error": f"{type(e).__name__}: {e}"}

    playerstats = raw.get("playerstats", {})
    # Manche Antworten haben "error" Field — typischerweise wenn der User Rust nie gespielt hat
    if "error" in playerstats:
        return {"is_private": False, "stats": {}, "raw": raw, "error": playerstats["error"]}
    stats_arr = playerstats.get("stats") or []
    stats = {s["name"]: int(s.get("value", 0)) for s in stats_arr if "name" in s}
    return {"is_private": False, "stats": stats, "raw": raw, "error": None}


def upsert_rust_user_stats(steamid64: int, result: dict) -> None:
    """Speichert Rust-Stats für einen User in rust_player_stats."""
    now = int(time.time())
    cols: dict = {col: result["stats"].get(steam_name) for steam_name, col in RUST_STAT_COLUMNS.items()}
    cols.update({
        "steam_id":   int(steamid64),
        "fetched_at": now,
        "is_private": bool(result.get("is_private")),
        "error":      result.get("error"),
        "raw_json":   json.dumps(result.get("raw"), separators=(",", ":")) if result.get("raw") else None,
    })
    # Build dynamic UPSERT
    col_names = list(cols.keys())
    placeholders = ",".join("%s" for _ in col_names)
    cols_sql = ",".join(col_names)
    updates = ",".join(f"{c}=EXCLUDED.{c}" for c in col_names if c != "steam_id")
    sql = (f"INSERT INTO rust_player_stats ({cols_sql}) VALUES ({placeholders}) "
           f"ON CONFLICT (steam_id) DO UPDATE SET {updates}")
    values = [cols[c] for c in col_names]
    with _Conn() as (conn, cur):
        cur.execute(sql, values)


def get_rust_user_stats(steamid64: int, force_refresh: bool = False) -> dict | None:
    """Liefert Rust-Stats aus Cache (oder fetcht frisch wenn stale/missing).
    Returns dict mit allen Stats-Spalten + Meta, oder None bei totalem Fehler.
    """
    now = int(time.time())
    if not force_refresh:
        with _Conn() as (conn, cur):
            cur.execute(
                "SELECT * FROM rust_player_stats WHERE steam_id=%s", (int(steamid64),))
            r = cur.fetchone()
            if r and (now - r["fetched_at"]) < RUST_STATS_CACHE_TTL:
                return dict(r)
    # Cache-Miss oder Force: fresh fetch
    result = query_rust_user_stats(steamid64)
    upsert_rust_user_stats(steamid64, result)
    with _Conn() as (conn, cur):
        cur.execute("SELECT * FROM rust_player_stats WHERE steam_id=%s", (int(steamid64),))
        r = cur.fetchone()
        return dict(r) if r else None


def resolve_steam_id_from_bm(bm_player_id: int) -> int | None:
    """Versucht aus BattleMetrics-API die Steam-ID für einen BM-Player rauszuziehen.
    Funktioniert nur wenn der Player sein Steam öffentlich auf BM hat.
    Returns SteamID64 oder None.
    """
    try:
        url = f"{BATTLEMETRICS_PLAYER_URL}/{int(bm_player_id)}?include=identifier"
        req = urllib.request.Request(url, headers={
            "User-Agent": "RustMetrics/1.0 (+https://rustmetrics.eu)"
        })
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"[bm_steamid] {bm_player_id}: {e}", file=sys.stderr, flush=True)
        return None
    for inc in (data.get("included") or []):
        if inc.get("type") != "identifier":
            continue
        attrs = inc.get("attributes") or {}
        if attrs.get("type") == "steamID":
            v = attrs.get("identifier")
            try: return int(v)
            except (TypeError, ValueError): pass
    return None


def ensure_player_steam_id(bm_player_id: int) -> int | None:
    """Lazy resolve: liefert players.steam_id wenn vorhanden, sonst aus BM ziehen + cachen."""
    with _Conn() as (conn, cur):
        cur.execute("SELECT steam_id FROM players WHERE bm_id=%s", (int(bm_player_id),))
        r = cur.fetchone()
        if r and r["steam_id"]:
            return int(r["steam_id"])
    # Try BM
    sid = resolve_steam_id_from_bm(bm_player_id)
    if sid:
        try:
            with _Conn() as (conn, cur):
                cur.execute(
                    "UPDATE players SET steam_id=%s WHERE bm_id=%s",
                    (sid, int(bm_player_id)))
        except Exception as e:
            print(f"[steam_id_cache] {e}", file=sys.stderr, flush=True)
    return sid


# ═══════════════════════════════════════════════════════════════════════════
#  SESSIONS
# ═══════════════════════════════════════════════════════════════════════════

def create_session(user_id: int, ip: str = "", ua: str = "") -> str:
    token = secrets.token_urlsafe(32)
    now   = int(time.time())
    with _Conn() as (conn, cur):
        cur.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at, ip, user_agent) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (token, user_id, now, now + SESSION_TTL, ip[:64], ua[:255]),
        )
    return token


def lookup_session(token: str) -> dict | None:
    if not token: return None
    with _Conn() as (conn, cur):
        cur.execute(
            "SELECT s.user_id, s.expires_at, u.display_name, u.avatar_url, u.is_admin "
            "FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token = %s",
            (token,),
        )
        row = cur.fetchone()
        if not row: return None
        if row["expires_at"] < int(time.time()):
            cur.execute("DELETE FROM sessions WHERE token=%s", (token,))
            return None
        return dict(row)


def delete_session(token: str):
    if not token: return
    with _Conn() as (conn, cur):
        cur.execute("DELETE FROM sessions WHERE token=%s", (token,))


def upsert_user(steamid64: int, profile: dict) -> None:
    now = int(time.time())
    is_admin = str(steamid64) in ADMIN_STEAMIDS
    with _Conn() as (conn, cur):
        cur.execute(
            "INSERT INTO users (id, display_name, avatar_url, profile_url, is_admin, created_at, last_login_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (id) DO UPDATE SET "
            "  display_name  = COALESCE(EXCLUDED.display_name,  users.display_name), "
            "  avatar_url    = COALESCE(EXCLUDED.avatar_url,    users.avatar_url), "
            "  profile_url   = COALESCE(EXCLUDED.profile_url,   users.profile_url), "
            "  is_admin      = EXCLUDED.is_admin, "
            "  last_login_at = EXCLUDED.last_login_at",
            (steamid64, profile.get("display_name"), profile.get("avatar_url"),
             profile.get("profile_url"), is_admin, now, now),
        )


# ═══════════════════════════════════════════════════════════════════════════
#  CALENDAR-TOKEN (für ICS-Feed) — pro User ein opakes Token
# ═══════════════════════════════════════════════════════════════════════════

def get_or_create_calendar_token(user_id: int) -> str:
    """Holt das calendar_token des Users — generiert eins falls noch keins existiert."""
    with _Conn() as (conn, cur):
        cur.execute("SELECT calendar_token FROM users WHERE id=%s", (user_id,))
        r = cur.fetchone()
        if r and r["calendar_token"]:
            return r["calendar_token"]
        # Generate new token (urlsafe, 32 bytes = 43 chars)
        new_token = secrets.token_urlsafe(32)
        cur.execute(
            "UPDATE users SET calendar_token=%s WHERE id=%s",
            (new_token, user_id))
    return new_token


def reset_calendar_token(user_id: int) -> str:
    """Generiert ein NEUES Token (invalidiert alte Subscriber-URLs). Gibt das neue zurück."""
    new_token = secrets.token_urlsafe(32)
    with _Conn() as (conn, cur):
        cur.execute(
            "UPDATE users SET calendar_token=%s WHERE id=%s",
            (new_token, user_id))
    return new_token


def lookup_user_by_calendar_token(token: str) -> int | None:
    """Findet den User zu einem Calendar-Token. None wenn unbekannt."""
    if not token or len(token) < 16:
        return None
    with _Conn() as (conn, cur):
        cur.execute("SELECT id FROM users WHERE calendar_token=%s", (token,))
        r = cur.fetchone()
        return r["id"] if r else None


def _ics_escape(s: str) -> str:
    """Escape für ICS-TEXT-Werte (RFC 5545)."""
    if s is None: return ""
    return (str(s)
            .replace("\\", "\\\\")
            .replace(";", "\\;")
            .replace(",", "\\,")
            .replace("\n", "\\n")
            .replace("\r", ""))


def _ics_format_utc(ts: int) -> str:
    """Unix-TS → ICS UTC-Format YYYYMMDDTHHMMSSZ."""
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(int(ts)))


def render_wipes_ics(user_id: int) -> str:
    """
    Generiert einen ICS-Feed mit allen Wipes (vergangene + nächste 4) der Server,
    die der User gewatched hat. Format ist RFC 5545.
    """
    now = int(time.time())
    week = 7 * 86400
    out = ["BEGIN:VCALENDAR",
           "VERSION:2.0",
           "PRODID:-//RustMetrics//Wipe Calendar//EN",
           "CALSCALE:GREGORIAN",
           "METHOD:PUBLISH",
           "X-WR-CALNAME:RustMetrics Wipes",
           "X-WR-CALDESC:Upcoming and recent Rust server wipes for your watchlist",
           "X-PUBLISHED-TTL:PT1H",
           "REFRESH-INTERVAL;VALUE=DURATION:PT1H"]

    with _Conn() as (conn, cur):
        cur.execute(
            "SELECT s.id, s.host, s.port, s.name AS srv_name "
            "FROM servers s JOIN watched_servers ws ON ws.server_id=s.id "
            "WHERE ws.user_id=%s",
            (user_id,))
        servers = [dict(r) for r in cur.fetchall()]

        # Letzten Snapshot pro Server holen, daraus born_ts extrahieren
        for s in servers:
            cur.execute(
                "SELECT keywords, name FROM snapshots WHERE server_id=%s "
                "ORDER BY ts DESC LIMIT 1", (s["id"],))
            snap = cur.fetchone()
            if not snap: continue
            kw = (snap.get("keywords") or "")
            srv_label = snap.get("name") or s["srv_name"] or f"{s['host']}:{s['port']}"
            parsed = parse_rust_keywords(kw)
            born_ts = parsed.get("born_ts")
            if not born_ts:
                continue

            # Letzten + nächsten 4 Wipes generieren
            # Wir kennen die genaue Wipe-Stunde nicht, nehmen die Uhrzeit von born_ts
            # (das ist der reale letzte Wipe).
            wipe_ts = born_ts
            wipes: list[tuple[str, int, str]] = []  # (uid_suffix, ts, label)
            # Vergangener Wipe
            wipes.append(("past", wipe_ts, "(actual)"))
            # Nächste 4 — wöchentlich rolling
            for i in range(1, 5):
                t = wipe_ts + i * week
                # Wenn schon vorbei, überspringen (kann passieren wenn unsere
                # born_ts gerade zwischen zwei Wipes liegt — sollte aber selten sein)
                if t < now - 86400:
                    continue
                wipes.append((f"next{i}", t, f"(est. +{i}w)"))

            for suffix, ts, label in wipes:
                # 1h-Event ab Wipe-Zeit
                uid = f"wipe-{s['id']}-{suffix}-{ts}@rustmetrics.eu"
                title = f"Wipe: {srv_label} {label}"
                desc_lines = [
                    f"Server: {srv_label}",
                    f"Address: {s['host']}:{s['port']}",
                    f"Estimated wipe time (based on weekly cycle from last known wipe).",
                    f"",
                    f"View: https://rustmetrics.eu/server/{s['host']}:{s['port']}",
                ]
                out.extend([
                    "BEGIN:VEVENT",
                    f"UID:{uid}",
                    f"DTSTAMP:{_ics_format_utc(now)}",
                    f"DTSTART:{_ics_format_utc(ts)}",
                    f"DTEND:{_ics_format_utc(ts + 3600)}",
                    f"SUMMARY:{_ics_escape(title)}",
                    f"DESCRIPTION:{_ics_escape(chr(10).join(desc_lines))}",
                    f"URL:https://rustmetrics.eu/server/{s['host']}:{s['port']}",
                    f"LOCATION:{_ics_escape(s['host'] + ':' + str(s['port']))}",
                    "END:VEVENT",
                ])

    out.append("END:VCALENDAR")
    # RFC 5545: lines limited to 75 octets, CRLF line endings
    body = []
    for line in out:
        while len(line.encode("utf-8")) > 75:
            # Soft-wrap on character boundary (approximate — works for ASCII)
            body.append(line[:74])
            line = " " + line[74:]
        body.append(line)
    return "\r\n".join(body) + "\r\n"


# ═══════════════════════════════════════════════════════════════════════════
#  RATE LIMITING — Token Bucket pro IP
# ═══════════════════════════════════════════════════════════════════════════

class TokenBucket:
    """Simpler Token-Bucket. capacity tokens, refill_rate tokens/sec."""
    def __init__(self, capacity: float, refill_rate: float):
        self.capacity = capacity
        self.refill   = refill_rate
        self.tokens   = capacity
        self.last     = time.time()
        self._lock    = threading.Lock()

    def take(self, cost: float = 1.0) -> bool:
        with self._lock:
            now = time.time()
            elapsed = now - self.last
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill)
            self.last = now
            if self.tokens >= cost:
                self.tokens -= cost
                return True
            return False


_rl_general: dict[str, TokenBucket] = {}
_rl_browse:  dict[str, TokenBucket] = {}
_rl_lock = threading.Lock()

def _get_bucket(d, ip, capacity, rate) -> TokenBucket:
    with _rl_lock:
        b = d.get(ip)
        if b is None:
            b = TokenBucket(capacity, rate)
            d[ip] = b
            # Lazy-Cleanup: wenn Map zu gross, ältere weg
            if len(d) > 5000:
                old = sorted(d.items(), key=lambda kv: kv[1].last)[:1000]
                for k, _ in old: d.pop(k, None)
        return b

def rate_limit_general(ip: str) -> bool:
    b = _get_bucket(_rl_general, ip, RL_REQ_PER_MIN, RL_REQ_PER_MIN / 60.0)
    return b.take(1.0)

def rate_limit_browse_miss(ip: str) -> bool:
    b = _get_bucket(_rl_browse, ip, RL_BROWSE_PER_H, RL_BROWSE_PER_H / 3600.0)
    return b.take(1.0)


# ═══════════════════════════════════════════════════════════════════════════
#  POLLER — pollt nur Server, die mindestens 1 User in der Watchlist hat
# ═══════════════════════════════════════════════════════════════════════════

_poller_stop = threading.Event()


def _store_snapshot(cur, server_id: int, q: dict) -> None:
    players = q.get("players_list", []) or []
    cur.execute(
        "INSERT INTO snapshots "
        "(server_id, ts, online, name, map, players_count, max_players, "
        " ping_ms, keywords, players_json, error) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            server_id,
            q.get("queried_at", int(time.time())),
            bool(q.get("online")),
            q.get("name"), q.get("map"),
            q.get("players_count"), q.get("max_players"),
            q.get("ping_ms"), q.get("keywords"),
            json.dumps([p.get("name", "") for p in players]),
            q.get("error"),
        ),
    )
    if q.get("name"):
        cur.execute("UPDATE servers SET name=%s WHERE id=%s", (q["name"], server_id))


def _diff_player_sessions(cur, server_id: int, ts: int, names: list[str],
                          name_to_bmid: dict[str, int] | None = None) -> tuple[set, set]:
    """Updates player_sessions; returns (came_online_set, went_offline_set).

    name_to_bmid: optionales Mapping lowercase_name → bm_player_id, das beim
    Erzeugen neuer Sessions die stabile BM-ID mitspeichert. Alte (NULL-)Einträge
    werden nicht rückwirkend angefasst — die Player-Profil-Page matched per Name
    + players.aliases.
    """
    name_to_bmid = name_to_bmid or {}
    cur.execute(
        "SELECT id, player_name FROM player_sessions "
        "WHERE server_id=%s AND end_ts IS NULL", (server_id,))
    open_sess = {r["player_name"]: r["id"] for r in cur.fetchall()}
    now_set = set(names)
    came_online = now_set - set(open_sess.keys())
    went_offline = set(open_sess.keys()) - now_set
    for n in came_online:
        bid = name_to_bmid.get(n.lower())
        cur.execute(
            "INSERT INTO player_sessions (server_id, player_name, start_ts, bm_player_id) "
            "VALUES (%s,%s,%s,%s)",
            (server_id, n, ts, bid))
    for n in went_offline:
        cur.execute("UPDATE player_sessions SET end_ts=%s WHERE id=%s", (ts, open_sess[n]))
    return came_online, went_offline


def upsert_player_meta(cur, pairs: list[tuple[int, str]], ts: int | None = None) -> None:
    """Pflegt die players-Tabelle: bm_id → current_name + aliases.
    Wenn der Name sich geändert hat, wird der alte current_name in aliases verschoben.
    pairs: Liste von (bm_id, name) — bm_id muss > 0 sein, sonst übersprungen.
    """
    if not pairs:
        return
    if ts is None:
        ts = int(time.time())
    seen: set[int] = set()
    for bid, nm in pairs:
        if not bid or not nm or bid in seen:
            continue
        seen.add(bid)
        cur.execute("SELECT current_name, aliases FROM players WHERE bm_id=%s", (bid,))
        r = cur.fetchone()
        if not r:
            cur.execute(
                "INSERT INTO players (bm_id, current_name, aliases, first_seen, last_seen) "
                "VALUES (%s,%s,ARRAY[]::TEXT[],%s,%s)",
                (bid, nm, ts, ts))
        elif r["current_name"] != nm:
            # Name geändert: alten Namen zu aliases hinzufügen wenn neu
            new_aliases = list(r["aliases"] or [])
            if r["current_name"] and r["current_name"] not in new_aliases and r["current_name"] != nm:
                new_aliases.append(r["current_name"])
            # Limit aliases auf 20 Einträge (älteste fliegen raus)
            if len(new_aliases) > 20:
                new_aliases = new_aliases[-20:]
            cur.execute(
                "UPDATE players SET current_name=%s, aliases=%s, last_seen=%s WHERE bm_id=%s",
                (nm, new_aliases, ts, bid))
        else:
            cur.execute(
                "UPDATE players SET last_seen=%s WHERE bm_id=%s",
                (ts, bid))


# ─── Discord-Webhook-Dispatcher ─────────────────────────────────────────
DISCORD_WEBHOOK_RE = re.compile(
    r"^https://(?:ptb\.|canary\.)?(?:discord|discordapp)\.com/api/webhooks/\d+/[A-Za-z0-9_\-]+/?$"
)
_webhook_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="webhook")

def is_valid_discord_webhook(url: str) -> bool:
    return bool(url and DISCORD_WEBHOOK_RE.match(url))

def _post_discord_webhook(url: str, payload: dict) -> tuple[bool, str]:
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": "RustMetrics/1.0"})
        with urllib.request.urlopen(req, timeout=6) as r:
            return (200 <= r.status < 300), f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        body = ""
        try: body = e.read().decode("utf-8", errors="replace")[:200]
        except Exception: pass
        return False, f"HTTP {e.code}: {body}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

def fire_discord_webhook_async(url: str, payload: dict):
    """Fire-and-forget Discord-Webhook-POST."""
    if not is_valid_discord_webhook(url): return
    _webhook_executor.submit(_post_discord_webhook, url, payload)

def _make_player_embed(event: str, player: str, server_name: str,
                       players_count: int = None, max_players: int = None,
                       map_name: str = None) -> dict:
    online = (event == "online")
    color = 0x5FC26B if online else 0xD24A3E   # green / red
    fields = []
    if server_name:
        fields.append({"name": "Server", "value": server_name[:200], "inline": False})
    if map_name:
        fields.append({"name": "Map", "value": str(map_name)[:64], "inline": True})
    if players_count is not None and max_players is not None:
        fields.append({"name": "Players", "value": f"{players_count}/{max_players}", "inline": True})
    return {
        "username":  "RustMetrics",
        "avatar_url": "https://rustmetrics.eu/favicon.png",
        "embeds": [{
            "title":       f"🟢 Player online — {player}" if online else f"🔴 Player offline — {player}",
            "description": f"**{player}** {'appeared on' if online else 'left'} {server_name}",
            "color":       color,
            "fields":      fields,
            "footer":      {"text": "rustmetrics.eu"},
        }],
    }

def _notify_player_transitions(cur, server_id: int, server_name: str,
                               came_online: set, went_offline: set,
                               snapshot_pop: int = None, snapshot_max: int = None,
                               map_name: str = None) -> None:
    """Find watchers of these names and fire their Discord webhooks (async)."""
    if not came_online and not went_offline: return
    transitions = [(n, "online") for n in came_online] + [(n, "offline") for n in went_offline]
    for name, event in transitions:
        col = "notify_online" if event == "online" else "notify_offline"
        cur.execute(
            f"SELECT u.discord_webhook FROM watched_players wp "
            f"JOIN users u ON u.id = wp.user_id "
            f"WHERE wp.server_id=%s AND lower(wp.name)=lower(%s) "
            f"  AND u.discord_webhook IS NOT NULL AND u.{col}=TRUE",
            (server_id, name))
        for row in cur.fetchall():
            wh = row["discord_webhook"]
            embed = _make_player_embed(event, name, server_name,
                                        snapshot_pop, snapshot_max, map_name)
            fire_discord_webhook_async(wh, embed)


def poll_server_row(row: dict) -> dict:
    q = query_a2s(row["host"], row["port"])

    # Wenn A2S nicht alle Daten liefert (Server blockt A2S_INFO oder _PLAYER):
    # BattleMetrics-Fallback. Spart Resourcen indem wir nur dann BM anfragen,
    # wenn A2S nicht das volle Bild liefert.
    a2s_has_pop     = q.get("players_count") is not None and q.get("max_players") is not None
    a2s_has_players = bool(q.get("players_list"))
    bm_pairs: list[tuple[int, str]] = []
    if not a2s_has_pop or not a2s_has_players:
        try:
            bm_attrs, bm_pairs = query_bm_server(row["host"], row["port"])
        except Exception as e:
            print(f"[bm_fallback] {row['host']}:{row['port']} {e}", file=sys.stderr, flush=True)
            bm_attrs, bm_pairs = None, []
        if bm_attrs:
            details = bm_attrs.get("details") or {}
            if not q.get("name"):           q["name"]          = bm_attrs.get("name")
            if not q.get("map"):            q["map"]           = details.get("map")
            if q.get("players_count") is None: q["players_count"] = bm_attrs.get("players")
            if q.get("max_players") is None:   q["max_players"]   = bm_attrs.get("maxPlayers")
            if not q.get("keywords"):
                tags = list(details.get("rust_modded_tags") or [])
                if details.get("rust_type"): tags.append(details["rust_type"])
                kw_extra = []
                if details.get("rust_queued_players"): kw_extra.append(f"qp{details['rust_queued_players']}")
                q["keywords"] = ",".join(tags + kw_extra)
            if bm_attrs.get("status") == "online":
                q["online"] = True
                q.pop("error", None)
            if not a2s_has_players and bm_pairs:
                q["players_list"] = [
                    {"name": nm, "score": 0, "duration_s": 0, "_bm_id": bid}
                    for bid, nm in bm_pairs
                ]
            q["_data_source"] = "a2s+bm" if (q.get("ping_ms") is not None) else "bm-only"

    came_online: set = set()
    went_offline: set = set()
    server_display_name = q.get("name") or row.get("host") + ":" + str(row.get("port"))
    # name → bm_id mapping (case-insensitive), entweder direkt aus dem
    # A2S+BM-Mischpfad (players_list mit _bm_id), oder aus bm_pairs falls
    # nur BM Daten lieferte.
    name_to_bmid: dict[str, int] = {}
    for p in (q.get("players_list") or []):
        bid = p.get("_bm_id") if isinstance(p, dict) else None
        if bid and p.get("name"):
            name_to_bmid[p["name"].lower()] = bid
    for bid, nm in (bm_pairs or []):
        if bid and nm:
            name_to_bmid.setdefault(nm.lower(), bid)
    with _Conn() as (conn, cur):
        _store_snapshot(cur, row["id"], q)
        if q.get("online"):
            names = [p["name"] for p in q.get("players_list", []) or []]
            came_online, went_offline = _diff_player_sessions(
                cur, row["id"], q.get("queried_at", int(time.time())), names,
                name_to_bmid=name_to_bmid)
            # Player-Metadaten pflegen (current_name, aliases) für jeden bekannten BM-Player
            if name_to_bmid:
                meta_pairs = []
                for nm in names:
                    bid = name_to_bmid.get(nm.lower())
                    if bid:
                        meta_pairs.append((bid, nm))
                if meta_pairs:
                    upsert_player_meta(cur, meta_pairs,
                                       ts=q.get("queried_at", int(time.time())))
            _notify_player_transitions(
                cur, row["id"], server_display_name,
                came_online, went_offline,
                snapshot_pop=q.get("players_count"),
                snapshot_max=q.get("max_players"),
                map_name=q.get("map"),
            )
            # Persist anonymization detection — sobald wir's einmal sehen, bleibt's an
            if detect_name_anonymization(names):
                cur.execute(
                    "UPDATE servers SET is_anonymized=TRUE WHERE id=%s AND NOT is_anonymized",
                    (row["id"],))
            # Watched-players: BM-IDs auffüllen / Namen auto-aktualisieren
            if bm_pairs:
                bm_by_name = {nm.lower(): bid for bid, nm in bm_pairs if bid}
                bm_by_id   = {bid: nm for bid, nm in bm_pairs if bid}
                cur.execute(
                    "SELECT id, name, bm_player_id FROM watched_players WHERE server_id=%s",
                    (row["id"],))
                for w in cur.fetchall():
                    if w["bm_player_id"] and w["bm_player_id"] in bm_by_id:
                        # ID gematcht: ggf. neuen Namen übernehmen
                        cur_name = bm_by_id[w["bm_player_id"]]
                        if cur_name != w["name"]:
                            cur.execute(
                                "UPDATE watched_players SET name=%s WHERE id=%s",
                                (cur_name, w["id"]))
                    elif not w["bm_player_id"]:
                        # Noch keine ID: per Name backfillen
                        bid = bm_by_name.get(w["name"].lower())
                        if bid:
                            cur.execute(
                                "UPDATE watched_players SET bm_player_id=%s WHERE id=%s",
                                (bid, w["id"]))
    return q


def _cleanup_old(cur):
    cutoff = int(time.time()) - SNAPSHOT_TTL_DAY * 86400
    cur.execute("DELETE FROM snapshots WHERE ts < %s", (cutoff,))
    cur.execute("DELETE FROM player_sessions WHERE end_ts IS NOT NULL AND end_ts < %s", (cutoff,))
    cur.execute("DELETE FROM sessions WHERE expires_at < %s", (int(time.time()),))
    # Verwaiste Server löschen (die niemand mehr watcht und seit 7+ Tagen keine Snapshots)
    cur.execute(
        "DELETE FROM servers s WHERE NOT EXISTS (SELECT 1 FROM watched_servers w WHERE w.server_id = s.id) "
        "AND NOT EXISTS (SELECT 1 FROM snapshots sn WHERE sn.server_id = s.id AND sn.ts > %s)",
        (int(time.time()) - 7 * 86400,)
    )


def poller_loop():
    print("[poller] gestartet", flush=True)
    last_cleanup = 0.0
    while not _poller_stop.is_set():
        try:
            with _Conn() as (conn, cur):
                cur.execute("SELECT id, host, port FROM v_active_servers")
                rows = [dict(r) for r in cur.fetchall()]
            threads = []
            for r in rows:
                t = threading.Thread(target=poll_server_row, args=(r,), daemon=True)
                t.start()
                threads.append(t)
            for t in threads:
                t.join(timeout=A2S_TIMEOUT * 2 + 1)
            if time.time() - last_cleanup > 3600:
                with _Conn() as (conn, cur):
                    _cleanup_old(cur)
                last_cleanup = time.time()
        except Exception as e:
            print(f"[poller] error: {e}", file=sys.stderr, flush=True)
        for _ in range(POLL_INTERVAL):
            if _poller_stop.is_set(): return
            time.sleep(1)


# ═══════════════════════════════════════════════════════════════════════════
#  API-FUNKTIONEN
# ═══════════════════════════════════════════════════════════════════════════

def _latest_snapshot(cur, server_id: int):
    cur.execute(
        "SELECT * FROM snapshots WHERE server_id=%s ORDER BY ts DESC LIMIT 1",
        (server_id,))
    r = cur.fetchone()
    return dict(r) if r else None


def api_servers_list(user_id: int) -> list[dict]:
    """Liste der Server, die DIESER User beobachtet, mit aktuellem Snapshot
       und seinen beobachteten Spielernamen pro Server."""
    out = []
    with _Conn() as (conn, cur):
        cur.execute(
            "SELECT s.*, ws.added_at AS user_added_at FROM servers s "
            "JOIN watched_servers ws ON ws.server_id = s.id "
            "WHERE ws.user_id=%s ORDER BY ws.added_at ASC",
            (user_id,)
        )
        rows = cur.fetchall()
        for r in rows:
            snap = _latest_snapshot(cur, r["id"])
            players = []
            if snap and snap.get("players_json"):
                try: players = json.loads(snap["players_json"])
                except Exception: players = []
            cur.execute(
                "SELECT id, name, added_at, bm_player_id FROM watched_players "
                "WHERE user_id=%s AND server_id=%s ORDER BY name",
                (user_id, r["id"]))
            watched = [dict(w) for w in cur.fetchall()]
            online_set = {p.lower() for p in players}
            for w in watched: w["online"] = w["name"].lower() in online_set
            kw = (snap.get("keywords") if snap else "") or ""
            parsed = parse_rust_keywords(kw)
            tier = detect_rust_tier(snap.get("name") if snap else r["name"], parsed.get("tags", []))
            out.append({
                "id": r["id"], "host": r["host"], "port": r["port"],
                "name": (snap.get("name") if snap else None) or r["name"] or f"{r['host']}:{r['port']}",
                "added_at": r["user_added_at"],
                "snapshot": ({
                    "ts": snap["ts"], "online": bool(snap["online"]),
                    "map": snap.get("map"),
                    "players_count": snap.get("players_count"),
                    "max_players": snap.get("max_players"),
                    "ping_ms": snap.get("ping_ms"),
                    "players": players,
                    "error": snap.get("error"),
                } if snap else None),
                "tier": tier,
                "tags": parsed.get("tags", []),
                "queued": parsed.get("queued_kw"),
                "wipe": next_wipe_estimate(parsed.get("born_ts")),
                "watched_players": watched,
                "name_anonymized": bool(r.get("is_anonymized")) or detect_name_anonymization(players),
            })
    return out


def api_server_detail(user_id: int, server_id: int, hours: int = 24) -> dict | None:
    with _Conn() as (conn, cur):
        cur.execute(
            "SELECT s.* FROM servers s JOIN watched_servers ws "
            "ON ws.server_id=s.id WHERE s.id=%s AND ws.user_id=%s",
            (server_id, user_id))
        r = cur.fetchone()
        if not r: return None
        snap = _latest_snapshot(cur, server_id)
        cutoff = int(time.time()) - hours * 3600
        cur.execute(
            "SELECT ts, players_count, online, ping_ms FROM snapshots "
            "WHERE server_id=%s AND ts >= %s ORDER BY ts ASC", (server_id, cutoff))
        hist = [dict(x) for x in cur.fetchall()]
        cur.execute(
            "SELECT player_name, start_ts FROM player_sessions "
            "WHERE server_id=%s AND end_ts IS NULL ORDER BY start_ts ASC", (server_id,))
        active = [dict(x) for x in cur.fetchall()]
        cur.execute(
            "SELECT player_name, start_ts, end_ts FROM player_sessions "
            "WHERE server_id=%s AND end_ts IS NOT NULL ORDER BY end_ts DESC LIMIT 60",
            (server_id,))
        closed = [dict(x) for x in cur.fetchall()]
        kw_raw = (snap.get("keywords") if snap else "") or ""
        parsed = parse_rust_keywords(kw_raw)
        return {
            "id": r["id"], "host": r["host"], "port": r["port"],
            "name": (snap.get("name") if snap else None) or r["name"],
            "snapshot": snap, "history": hist,
            "active_sessions": active, "recent_sessions": closed,
            "keywords_raw": kw_raw, "keywords_parsed": parsed,
            "tier": detect_rust_tier(snap.get("name") if snap else r["name"], parsed.get("tags", [])),
            "wipe": next_wipe_estimate(parsed.get("born_ts")),
        }


def api_browse(user_id: int | None,
               region="all", q="", tier="all",
               min_pop=0, max_pop=9999,
               sort="pop_desc", limit=200,
               ip="0.0.0.0") -> dict:
    cache_key = f"browse:{region}"
    cache_age = None
    raw = _cache_get(cache_key)
    if raw is None:
        if not rate_limit_browse_miss(ip):
            raise PermissionError("rate-limit: too many browser refreshes — please wait")
        t0 = time.time()
        servers, source = fetch_server_list(region=region, limit=BROWSE_MASTER_LIMIT)
        raw = {
            "fetched_at":   int(time.time()),
            "addr_count":   len(servers),       # bei den neuen Quellen identisch
            "online_count": len(servers),
            "fetch_secs":   round(time.time() - t0, 2),
            "source":       source,
            "servers":      servers,
        }
        _cache_put(cache_key, raw)
    else:
        cache_age = int(time.time() - raw["fetched_at"])

    # Bereits beobachtete Server (für aktuellen User markieren)
    in_user_watchlist: set[tuple[str, int]] = set()
    if user_id:
        with _Conn() as (conn, cur):
            cur.execute(
                "SELECT s.host, s.port FROM servers s "
                "JOIN watched_servers ws ON ws.server_id=s.id WHERE ws.user_id=%s",
                (user_id,))
            in_user_watchlist = {(r["host"], r["port"]) for r in cur.fetchall()}

    ql = (q or "").strip().lower()
    out = []
    for s in raw["servers"]:
        pc = s.get("players_count") or 0
        if pc < min_pop or pc > max_pop: continue
        name = (s.get("name") or "").lower()
        if ql and ql not in name: continue
        kw = s.get("keywords") or ""
        parsed = parse_rust_keywords(kw)
        s_tier = detect_rust_tier(s.get("name"), parsed.get("tags", []))
        if tier != "all" and tier != s_tier: continue
        out.append({
            "host": s["host"], "port": s["port"],
            "name": s.get("name") or f"{s['host']}:{s['port']}",
            "map":  s.get("map"),
            "players_count": pc, "max_players": s.get("max_players") or 0,
            "ping_ms": s.get("ping_ms"),
            "tier": s_tier, "tags": parsed.get("tags", []),
            "queued": parsed.get("queued_kw") or 0,
            "wipe": next_wipe_estimate(parsed.get("born_ts")),
            "in_watchlist": (s["host"], s["port"]) in in_user_watchlist,
        })

    sort_keys = {
        "pop_desc": lambda x: -(x["players_count"] or 0),
        "pop_asc":  lambda x:  (x["players_count"] or 0),
        "name":     lambda x: (x["name"] or "").lower(),
        "ping":     lambda x: x["ping_ms"] if x["ping_ms"] is not None else 9999,
        "wipe":     lambda x: x["wipe"]["days_until_next"] if x.get("wipe") else 9999,
    }
    out.sort(key=sort_keys.get(sort, sort_keys["pop_desc"]))

    return {
        "servers": out[:limit],
        "source": raw.get("source", "unknown"),
        "total_online": raw["online_count"],
        "total_fetched": raw["addr_count"],
        "matched": len(out),
        "returned": min(limit, len(out)),
        "fetched_at": raw["fetched_at"],
        "cache_age_sec": cache_age,
        "fetch_secs": raw.get("fetch_secs"),
        "region": region,
        "authenticated": bool(user_id),
    }


def add_watch_server(user_id: int, host: str, port: int) -> int:
    with _Conn() as (conn, cur):
        cur.execute(
            "INSERT INTO servers (host, port, first_seen) VALUES (%s,%s,%s) "
            "ON CONFLICT (host, port) DO NOTHING", (host, port, int(time.time())))
        cur.execute("SELECT id FROM servers WHERE host=%s AND port=%s", (host, port))
        sid = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO watched_servers (user_id, server_id, added_at) VALUES (%s,%s,%s) "
            "ON CONFLICT (user_id, server_id) DO NOTHING",
            (user_id, sid, int(time.time())))
    # erstmaliger Sofort-Poll für direktes Feedback
    threading.Thread(
        target=poll_server_row,
        args=({"id": sid, "host": host, "port": port},),
        daemon=True
    ).start()
    return sid


def remove_watch_server(user_id: int, server_id: int):
    with _Conn() as (conn, cur):
        cur.execute("DELETE FROM watched_servers WHERE user_id=%s AND server_id=%s",
                    (user_id, server_id))
        cur.execute("DELETE FROM watched_players WHERE user_id=%s AND server_id=%s",
                    (user_id, server_id))


def add_watch_player(user_id: int, server_id: int, name: str):
    with _Conn() as (conn, cur):
        cur.execute("SELECT 1 FROM watched_servers WHERE user_id=%s AND server_id=%s",
                    (user_id, server_id))
        if not cur.fetchone():
            raise PermissionError("server not in your watchlist")
        cur.execute(
            "INSERT INTO watched_players (user_id, server_id, name, added_at) "
            "VALUES (%s,%s,%s,%s) ON CONFLICT (user_id, server_id, name) DO NOTHING",
            (user_id, server_id, name, int(time.time())))


def remove_watch_player(user_id: int, watch_id: int):
    with _Conn() as (conn, cur):
        cur.execute("DELETE FROM watched_players WHERE id=%s AND user_id=%s",
                    (watch_id, user_id))


def _esc(s) -> str:
    """HTML-Escape für Template-Substitution."""
    return (str(s) if s is not None else "")\
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")\
        .replace('"', "&quot;").replace("'", "&#39;")


def render_public_server_page(host: str, port: int) -> tuple[str, int]:
    """
    Server-side-rendered HTML für /server/<host>:<port>. Liefert (html, status).
    Daten: bevorzugt aktueller Snapshot aus DB, sonst Live-BM-Lookup, sonst 404.
    """
    name = map_name = None
    players = max_players = ping = None
    online = False
    keywords = ""
    players_list: list[str] = []
    # (name → bm_id) mapping — wenn vorhanden, werden Player-Namen als Links
    # zu /player/<bm_id> gerendert. Quelle: BM-Pairs (live oder cached).
    player_bmid: dict[str, int] = {}
    snap_ts = None
    source = "live"

    # 1. DB-Snapshot wenn vorhanden und frisch
    with _Conn() as (conn, cur):
        cur.execute("SELECT id FROM servers WHERE host=%s AND port=%s", (host, port))
        r = cur.fetchone()
        if r:
            cur.execute("SELECT * FROM snapshots WHERE server_id=%s ORDER BY ts DESC LIMIT 1", (r["id"],))
            s = cur.fetchone()
            if s and (int(time.time()) - s["ts"] < 600):
                name = s["name"]; map_name = s["map"]
                players = s["players_count"]; max_players = s["max_players"]
                ping = s["ping_ms"]
                online = bool(s["online"])
                keywords = s["keywords"] or ""
                try: players_list = json.loads(s["players_json"] or "[]")
                except Exception: players_list = []
                snap_ts = s["ts"]
                source = "rustmetrics-db"
                # BM-IDs für die aktuell sichtbaren Namen aus der players-Tabelle
                # nachziehen (falls dort schon bekannt durch frühere BM-Polls)
                if players_list:
                    lower_names = [n.lower() for n in players_list]
                    cur.execute(
                        "SELECT bm_id, current_name, aliases FROM players "
                        "WHERE LOWER(current_name) = ANY(%s) "
                        "   OR EXISTS (SELECT 1 FROM unnest(aliases) AS a "
                        "              WHERE LOWER(a) = ANY(%s))",
                        (lower_names, lower_names))
                    for pr in cur.fetchall():
                        # current_name + aliases einbeziehen
                        for n in [pr["current_name"]] + list(pr["aliases"] or []):
                            if n and n.lower() in lower_names:
                                player_bmid.setdefault(n.lower(), pr["bm_id"])

    # 2. Live-BM-Lookup falls kein frischer Snapshot
    if name is None:
        bm_attrs, bm_pairs = query_bm_server(host, port)
        if bm_attrs:
            details = bm_attrs.get("details") or {}
            name = bm_attrs.get("name")
            map_name = details.get("map")
            players = bm_attrs.get("players")
            max_players = bm_attrs.get("maxPlayers")
            online = bm_attrs.get("status") == "online"
            tags = list(details.get("rust_modded_tags") or [])
            if details.get("rust_type"): tags.append(details["rust_type"])
            if details.get("rust_queued_players"):
                tags.append(f"qp{details['rust_queued_players']}")
            keywords = ",".join(tags)
            players_list = [nm for _bid, nm in bm_pairs]
            for bid, nm in bm_pairs:
                if bid and nm:
                    player_bmid[nm.lower()] = bid
            # Bei dieser Live-Quelle direkt players-Metadaten pflegen
            if bm_pairs:
                try:
                    with _Conn() as (_c2, cur2):
                        upsert_player_meta(cur2, bm_pairs)
                except Exception as e:
                    print(f"[upsert_meta live] {e}", file=sys.stderr, flush=True)
            source = "battlemetrics-live"
        else:
            return _public_404_html(host, port), 404

    parsed = parse_rust_keywords(keywords)
    tier = detect_rust_tier(name, parsed.get("tags", []))
    wipe = next_wipe_estimate(parsed.get("born_ts"))
    queued = parsed.get("queued_kw") or 0

    title = f"{name} — RustMetrics" if name else f"{host}:{port} — RustMetrics"
    pop_str = (f"{players}/{max_players}" if players is not None and max_players is not None else "—")
    pop_pct = round(100 * (players or 0) / max_players) if (players and max_players) else 0
    desc_bits = []
    if online:    desc_bits.append("🟢 online")
    else:         desc_bits.append("🔴 offline")
    desc_bits.append(f"{pop_str} players")
    if map_name:  desc_bits.append(f"map: {map_name}")
    if tier:      desc_bits.append(f"tier: {tier}")
    if wipe and wipe.get("days_until_next") is not None:
        desc_bits.append(f"next wipe in {wipe['days_until_next']}d")
    og_description = " · ".join(desc_bits)

    # Player-Liste rendern (max ~80 anzeigen, Rest "…and N more").
    # Wenn wir eine BM-ID für den Namen kennen, wird er zur eigenen Player-Page verlinkt.
    pl_html = ""
    if players_list:
        shown = players_list[:80]
        rest = max(0, len(players_list) - 80)
        items = []
        for n in shown:
            bid = player_bmid.get((n or "").lower())
            if bid:
                items.append(
                    f"<div class='p'><a class='pname plink' "
                    f"href='/player/{int(bid)}'>{_esc(n)}</a></div>")
            else:
                items.append(f"<div class='p'><span class='pname'>{_esc(n)}</span></div>")
        pl_html = "<div class='player-list'>" + "".join(items) + "</div>"
        if rest:
            pl_html += f"<p class='hint'>… and {rest} more</p>"
    else:
        pl_html = "<p class='hint' style='text-align:center'>No players online or list not available.</p>"

    tag_chips = []
    if tier: tag_chips.append(f"<span class='tag tier-{_esc(tier)}'>{_esc(tier)}</span>")
    for t in (parsed.get("tags") or [])[:6]:
        if t == tier: continue
        tag_chips.append(f"<span class='tag'>{_esc(t)}</span>")
    tags_html = "".join(tag_chips)

    wipe_html = ""
    if wipe and wipe.get("days_until_next") is not None:
        wipe_html = f"<dt>Next wipe</dt><dd>~{wipe['days_until_next']} days</dd>"

    updated_html = ""
    if snap_ts:
        diff = max(0, int(time.time()) - snap_ts)
        if diff < 60:    upd = f"{diff}s ago"
        elif diff<3600:  upd = f"{diff//60}m ago"
        else:            upd = f"{diff//3600}h ago"
        updated_html = f"<span style='color:var(--text-dim); font-size:11px'>updated {upd}</span>"
    else:
        updated_html = "<span style='color:var(--text-dim); font-size:11px'>live BM lookup</span>"

    html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{_esc(title)}</title>
<meta name="description" content="{_esc(og_description)}" />
<meta property="og:type" content="website" />
<meta property="og:site_name" content="RustMetrics" />
<meta property="og:title" content="{_esc(name or host)}" />
<meta property="og:description" content="{_esc(og_description)}" />
<meta property="og:url" content="https://rustmetrics.eu/server/{_esc(host)}:{port}" />
<meta name="twitter:card" content="summary" />
<meta name="twitter:title" content="{_esc(name or host)}" />
<meta name="twitter:description" content="{_esc(og_description)}" />
<link rel="stylesheet" href="/style.css" />
</head><body>

<header class="topbar">
  <div class="brand">
    <a href="/" style="display:flex; align-items:center; gap:12px; text-decoration:none; color:inherit;">
      <span class="brand-mark" aria-hidden="true"></span>
      <span class="brand-name">RUST<span class="brand-name-accent">METRICS</span></span>
    </a>
  </div>
  <div class="topbar-actions" style="margin-left:auto;">
    <a class="btn-steam" href="/auth/steam/login" style="text-decoration:none;">
      <span class="steam-icon" aria-hidden="true">⌬</span> Sign in to watch
    </a>
  </div>
</header>

<article class="legal-page" style="max-width:880px;">
  <a href="/" class="legal-back">← Home</a>
  <h1 style="text-transform:none; font-size:22px;">{_esc(name or host)}</h1>
  <p style="color:var(--text-muted); font-family:'SF Mono',monospace; font-size:13px; margin:0 0 6px 0;">
    {_esc(host)}:{port}
  </p>
  <div style="margin:8px 0 16px 0;">{tags_html}</div>

  <section class="detail-section">
    <h3>Live Status</h3>
    <dl class="detail-meta">
      <dt>Status</dt><dd>{'<span style="color:var(--green)">● ONLINE</span>' if online else '<span style="color:var(--red)">● OFFLINE</span>'}</dd>
      <dt>Players</dt><dd>{_esc(pop_str)}{f' <span style="color:var(--hazmat); font-size:11px;">+{queued} queued</span>' if queued else ''}</dd>
      <dt>Map</dt><dd>{_esc(map_name or "—")}</dd>
      {('<dt>Ping</dt><dd>' + str(ping) + ' ms</dd>') if ping else ''}
      <dt>Tier</dt><dd>{_esc(tier)}</dd>
      {wipe_html}
    </dl>
    <div class="fill-bar" style="margin-top:14px;"><div class="fill-bar-inner" style="width:{pop_pct}%"></div></div>
  </section>

  <section class="detail-section">
    <h3>Currently online ({len(players_list)})</h3>
    {pl_html}
  </section>

  <section class="detail-section">
    <h3>Data source</h3>
    <p style="color:var(--text-muted); font-size:12px;">
      {_esc(source)} · {updated_html}
    </p>
  </section>

  <p style="text-align:center; margin-top:30px; color:var(--text-muted); font-size:13px;">
    Want a private watchlist with online/offline notifications for specific players?
    <a href="/auth/steam/login" style="color:var(--accent-bright); font-weight:600;">Sign in with Steam</a> — free, no email required.
  </p>
</article>

<footer class="footer">
  <a href="/">Home</a>
  <span class="footer-divider">·</span>
  <a href="/imprint">Imprint</a>
  <span class="footer-divider">·</span>
  <a href="/privacy">Privacy</a>
  <span class="footer-divider">·</span>
  <a href="https://github.com/swiss-shift-ch/rustmetrics" target="_blank" rel="noopener" title="Source on GitHub">GitHub ★</a>
</footer>

</body></html>"""
    return html, 200


# ═══════════════════════════════════════════════════════════════════════════
#  ADMIN STATS
# ═══════════════════════════════════════════════════════════════════════════

def parse_caddy_log_tail(path: str = None, max_bytes: int = None) -> dict:
    """
    Liest die letzten max_bytes des Caddy-Access-Logs und extrahiert:
      - unique_ips (Set)
      - top_paths (Counter)
      - top_referers (Counter)
      - reddit_hits (int)
      - hits_total (int)
      - hits_by_hour (dict: "YYYY-MM-DD HH" → count, letzte 24h)
    Best-effort: Caddy log format kann JSON oder console sein, wir nutzen Regex.
    """
    if path is None: path = CADDY_LOG_PATH
    if max_bytes is None: max_bytes = CADDY_LOG_MAX_BYTES
    out = {
        "available":     False,
        "log_path":      path,
        "log_size":      0,
        "hits_total":    0,
        "unique_ips":    0,
        "reddit_hits":   0,
        "top_paths":     [],
        "top_referers":  [],
        "hits_by_hour":  [],
        "error":         None,
    }
    try:
        st = os.stat(path)
        out["log_size"] = st.st_size
    except FileNotFoundError:
        out["error"] = "log file not found"
        return out
    except PermissionError:
        out["error"] = "log file not readable (chown caddy:rustmetrics oder ACL setzen)"
        return out
    except Exception as e:
        out["error"] = f"stat: {type(e).__name__}: {e}"
        return out

    try:
        with open(path, "rb") as f:
            if out["log_size"] > max_bytes:
                f.seek(-max_bytes, 2)
                f.readline()  # erste Zeile ist möglicherweise abgeschnitten — wegwerfen
            data = f.read().decode("utf-8", errors="replace")
    except Exception as e:
        out["error"] = f"read: {type(e).__name__}: {e}"
        return out

    out["available"] = True

    # Regex-basierte Extraction. Caddy in beiden Formaten (console / json) hat
    # die Felder als "key":"value" oder "key":value im JSON-Block jeder Zeile.
    re_ip       = re.compile(r'"(?:client_ip|remote_ip|remote_addr)"\s*:\s*"([^"]+)"')
    re_uri      = re.compile(r'"uri"\s*:\s*"([^"]+)"')
    re_status   = re.compile(r'"status"\s*:\s*(\d+)')
    re_referer  = re.compile(r'"Referer"\s*:\s*\[\s*"([^"]+)"')
    re_ts       = re.compile(r'"ts"\s*:\s*([\d.]+)')

    ips: set[str] = set()
    path_counter: dict[str, int] = {}
    referer_counter: dict[str, int] = {}
    hour_counter: dict[str, int] = {}
    reddit_hits = 0
    total = 0
    now_ts = time.time()
    cutoff_24h = now_ts - 24 * 3600

    for line in data.splitlines():
        if not line.strip() or ('"request"' not in line and '"uri"' not in line):
            continue
        m_ip  = re_ip.search(line)
        m_uri = re_uri.search(line)
        m_st  = re_status.search(line)
        m_rf  = re_referer.search(line)
        m_ts  = re_ts.search(line)
        # Erfolg = mindestens uri da, sonst keine echte Request-Zeile
        if not m_uri:
            continue
        # Nur 2xx/3xx zählen
        if m_st and int(m_st.group(1)) >= 400:
            continue
        total += 1
        uri = m_uri.group(1)
        # API/static rausfiltern für Top-Paths
        if not (uri.startswith("/api/") or uri.startswith("/favicon")
                or uri.endswith(".css") or uri.endswith(".js")
                or uri.endswith(".png") or uri.endswith(".svg")
                or uri.endswith(".ico") or uri.endswith(".webmanifest")):
            # nur den "Pfad-Stamm" (ohne Query)
            stem = uri.split("?", 1)[0]
            path_counter[stem] = path_counter.get(stem, 0) + 1
        if m_ip:
            ips.add(m_ip.group(1))
        if m_rf:
            ref = m_rf.group(1)
            if ref:
                # auf Host-Anteil normalisieren
                try:
                    host = urllib.parse.urlparse(ref).netloc or ref
                    referer_counter[host] = referer_counter.get(host, 0) + 1
                    if "reddit" in host.lower():
                        reddit_hits += 1
                except Exception:
                    pass
        if m_ts:
            try:
                t = float(m_ts.group(1))
                if t >= cutoff_24h:
                    hkey = time.strftime("%Y-%m-%d %H", time.gmtime(t))
                    hour_counter[hkey] = hour_counter.get(hkey, 0) + 1
            except ValueError:
                pass

    out["hits_total"]   = total
    out["unique_ips"]   = len(ips)
    out["reddit_hits"]  = reddit_hits
    out["top_paths"]    = sorted(path_counter.items(), key=lambda x: -x[1])[:15]
    out["top_referers"] = sorted(referer_counter.items(), key=lambda x: -x[1])[:10]
    out["hits_by_hour"] = sorted(hour_counter.items())[-24:]
    return out


def admin_stats_data() -> dict:
    """SQL-Aggregate für /admin/stats. Liefert Dict mit allen Metriken."""
    now = int(time.time())
    data: dict = {"generated_at": now}
    with _Conn() as (conn, cur):
        cur.execute("SELECT COUNT(*) AS c FROM users")
        data["users_total"] = cur.fetchone()["c"]

        cur.execute(
            "SELECT COUNT(*) AS c FROM sessions WHERE expires_at > %s", (now,))
        data["sessions_active"] = cur.fetchone()["c"]

        for k, secs in [("active_1h", 3600), ("active_24h", 86400),
                        ("active_7d", 7 * 86400), ("active_30d", 30 * 86400)]:
            cur.execute(
                "SELECT COUNT(*) AS c FROM users WHERE last_login_at > %s",
                (now - secs,))
            data[k] = cur.fetchone()["c"]

        for k, secs in [("new_24h", 86400), ("new_7d", 7 * 86400),
                        ("new_30d", 30 * 86400)]:
            cur.execute(
                "SELECT COUNT(*) AS c FROM users WHERE created_at > %s",
                (now - secs,))
            data[k] = cur.fetchone()["c"]

        cur.execute("SELECT COUNT(*) AS c FROM watched_servers")
        data["watched_servers_total"] = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM watched_players")
        data["watched_players_total"] = cur.fetchone()["c"]

        cur.execute("SELECT COUNT(*) AS c FROM servers")
        data["servers_total"] = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM v_active_servers")
        data["servers_active"] = cur.fetchone()["c"]

        cur.execute("SELECT COUNT(*) AS c FROM players")
        data["players_tracked"] = cur.fetchone()["c"]
        cur.execute(
            "SELECT COUNT(*) AS c FROM players WHERE last_seen > %s",
            (now - 86400,))
        data["players_seen_24h"] = cur.fetchone()["c"]
        cur.execute(
            "SELECT COUNT(*) AS c FROM players WHERE steam_id IS NOT NULL")
        data["players_with_steam_id"] = cur.fetchone()["c"]

        cur.execute("SELECT COUNT(*) AS c FROM snapshots")
        data["snapshots_total"] = cur.fetchone()["c"]
        cur.execute(
            "SELECT COUNT(*) AS c FROM player_sessions WHERE end_ts IS NULL")
        data["open_player_sessions"] = cur.fetchone()["c"]

        # Top-10 meist-gewatchte Server (über alle User hinweg)
        cur.execute(
            "SELECT s.id, s.host, s.port, COALESCE(s.name, s.host || ':' || s.port) AS name, "
            "       COUNT(ws.user_id) AS watchers "
            "FROM servers s JOIN watched_servers ws ON ws.server_id=s.id "
            "GROUP BY s.id ORDER BY watchers DESC, s.id LIMIT 10")
        data["top_watched_servers"] = [dict(r) for r in cur.fetchall()]

        # Top-10 meist-gewatchte Player-Namen
        cur.execute(
            "SELECT name, COUNT(*) AS watchers FROM watched_players "
            "GROUP BY name ORDER BY watchers DESC, name LIMIT 10")
        data["top_watched_players"] = [dict(r) for r in cur.fetchall()]

        # Letzte 10 Signups
        cur.execute(
            "SELECT id, display_name, created_at, last_login_at "
            "FROM users ORDER BY created_at DESC LIMIT 10")
        data["recent_signups"] = [dict(r) for r in cur.fetchall()]

        # Signups pro Tag, letzte 14 Tage (für Sparkline)
        cur.execute(
            "SELECT DATE(to_timestamp(created_at)) AS day, COUNT(*) AS c "
            "FROM users WHERE created_at > %s "
            "GROUP BY day ORDER BY day",
            (now - 14 * 86400,))
        data["signups_by_day"] = [(str(r["day"]), r["c"]) for r in cur.fetchall()]

    # Caddy-Log Stats (best effort, kann fehlen)
    data["caddy"] = parse_caddy_log_tail()
    return data


def render_admin_stats_page(user_session: dict) -> tuple[str, int]:
    """Rendert /admin/stats — nur für is_admin Users."""
    d = admin_stats_data()
    now = d["generated_at"]
    user_name = user_session.get("display_name") or "admin"

    # Helper für hübsche Zahlen
    def fnum(n): return f"{n:,}".replace(",", " ")

    # Top-Server-Tabelle
    server_rows = []
    for s in d["top_watched_servers"]:
        server_rows.append(
            f"<tr><td><a href='/server/{_esc(s['host'])}:{s['port']}' "
            f"style='color:var(--text); text-decoration:none;'>{_esc(s['name'])}</a>"
            f"<div style='font-size:11px; color:var(--text-dim); font-family:\"SF Mono\",monospace;'>"
            f"{_esc(s['host'])}:{s['port']}</div></td>"
            f"<td style='text-align:right; font-variant-numeric:tabular-nums;'>{s['watchers']}</td></tr>")
    server_table = ("<table style='width:100%; border-collapse:collapse;'>"
                    "<thead><tr style='border-bottom:1px solid var(--border); color:var(--text-dim);"
                    " font-size:11px; text-transform:uppercase; letter-spacing:1px;'>"
                    "<th style='text-align:left; padding:6px 8px;'>Server</th>"
                    "<th style='text-align:right; padding:6px 8px;'>Watchers</th>"
                    "</tr></thead><tbody>"
                    + ("".join(server_rows) or "<tr><td colspan=2 style='padding:10px; color:var(--text-dim)'>none yet</td></tr>")
                    + "</tbody></table>")

    # Top-Players-Tabelle
    player_rows = []
    for p in d["top_watched_players"]:
        player_rows.append(
            f"<tr><td>{_esc(p['name'])}</td>"
            f"<td style='text-align:right; font-variant-numeric:tabular-nums;'>{p['watchers']}</td></tr>")
    player_table = ("<table style='width:100%; border-collapse:collapse;'>"
                    "<thead><tr style='border-bottom:1px solid var(--border); color:var(--text-dim);"
                    " font-size:11px; text-transform:uppercase; letter-spacing:1px;'>"
                    "<th style='text-align:left; padding:6px 8px;'>Player</th>"
                    "<th style='text-align:right; padding:6px 8px;'>Watchers</th>"
                    "</tr></thead><tbody>"
                    + ("".join(player_rows) or "<tr><td colspan=2 style='padding:10px; color:var(--text-dim)'>none yet</td></tr>")
                    + "</tbody></table>")

    # Recent Signups
    signup_rows = []
    for u in d["recent_signups"]:
        ago = max(0, now - u["created_at"])
        if   ago < 3600:   ago_str = f"{ago // 60}m ago"
        elif ago < 86400:  ago_str = f"{ago // 3600}h ago"
        else:              ago_str = f"{ago // 86400}d ago"
        signup_rows.append(
            f"<tr><td>{_esc(u['display_name'] or str(u['id']))}</td>"
            f"<td style='font-family:\"SF Mono\",monospace; font-size:11px; color:var(--text-dim);'>{u['id']}</td>"
            f"<td style='text-align:right; color:var(--text-dim); font-size:12px;'>{ago_str}</td></tr>")
    signups_table = ("<table style='width:100%; border-collapse:collapse;'>"
                     "<thead><tr style='border-bottom:1px solid var(--border); color:var(--text-dim);"
                     " font-size:11px; text-transform:uppercase; letter-spacing:1px;'>"
                     "<th style='text-align:left; padding:6px 8px;'>Display name</th>"
                     "<th style='text-align:left; padding:6px 8px;'>SteamID64</th>"
                     "<th style='text-align:right; padding:6px 8px;'>Signed up</th>"
                     "</tr></thead><tbody>"
                     + ("".join(signup_rows) or "<tr><td colspan=3 style='padding:10px; color:var(--text-dim)'>none yet</td></tr>")
                     + "</tbody></table>")

    # Signup-Sparkline (letzte 14 Tage)
    sparkline_html = ""
    if d["signups_by_day"]:
        vals = [v for _, v in d["signups_by_day"]]
        max_v = max(vals) if vals else 1
        bars = []
        for day, v in d["signups_by_day"]:
            h = int(40 * v / max_v) if max_v else 0
            bars.append(
                f"<div title='{day}: {v} signups' style='flex:1; min-width:14px; "
                f"display:flex; flex-direction:column; align-items:center; gap:4px;'>"
                f"<div style='width:100%; height:42px; display:flex; align-items:flex-end;'>"
                f"<div style='width:100%; height:{max(2,h)}px; background:var(--accent); border-radius:2px;'></div>"
                f"</div>"
                f"<div style='font-size:10px; color:var(--text-dim); font-variant-numeric:tabular-nums;'>{v}</div>"
                f"</div>")
        sparkline_html = (
            f"<div style='display:flex; gap:3px; align-items:flex-end; padding:8px 0;'>"
            + "".join(bars) + "</div>"
            f"<p class='hint' style='text-align:right; font-size:11px;'>last 14 days · max {max_v}/day</p>")

    # Caddy-Log Section
    caddy = d["caddy"]
    if caddy["available"]:
        log_size_mb = caddy["log_size"] / (1024 * 1024)
        path_rows = "".join(
            f"<tr><td style='font-family:\"SF Mono\",monospace; font-size:12px;'>{_esc(p)}</td>"
            f"<td style='text-align:right; font-variant-numeric:tabular-nums;'>{c}</td></tr>"
            for p, c in caddy["top_paths"]) or \
            "<tr><td colspan=2 style='padding:10px; color:var(--text-dim)'>none</td></tr>"
        ref_rows = "".join(
            f"<tr><td style='font-family:\"SF Mono\",monospace; font-size:12px;'>{_esc(r)}</td>"
            f"<td style='text-align:right; font-variant-numeric:tabular-nums;'>{c}</td></tr>"
            for r, c in caddy["top_referers"]) or \
            "<tr><td colspan=2 style='padding:10px; color:var(--text-dim)'>none</td></tr>"
        # Hits-by-Hour Mini-Chart
        hits_bars = ""
        if caddy["hits_by_hour"]:
            hv = [v for _, v in caddy["hits_by_hour"]]
            hmax = max(hv) if hv else 1
            bars = []
            for hkey, v in caddy["hits_by_hour"]:
                h = int(40 * v / hmax) if hmax else 0
                bars.append(
                    f"<div title='{hkey}: {v} hits' style='flex:1; min-width:8px; "
                    f"display:flex; flex-direction:column; align-items:center;'>"
                    f"<div style='width:100%; height:42px; display:flex; align-items:flex-end;'>"
                    f"<div style='width:100%; height:{max(2,h)}px; background:var(--hazmat); border-radius:2px;'></div>"
                    f"</div></div>")
            hits_bars = (f"<div style='display:flex; gap:2px; align-items:flex-end; padding:8px 0;'>"
                         + "".join(bars) + "</div>"
                         f"<p class='hint' style='text-align:right; font-size:11px;'>last 24h · max {hmax}/h</p>")
        caddy_html = f"""
        <section class="detail-section">
          <h3>Traffic · Caddy access log ({log_size_mb:.2f} MB tail)</h3>
          <dl class="detail-meta">
            <dt>Total requests (parsed)</dt><dd>{fnum(caddy['hits_total'])}</dd>
            <dt>Unique IPs</dt><dd>{fnum(caddy['unique_ips'])}</dd>
            <dt>Reddit referrals</dt><dd>{fnum(caddy['reddit_hits'])}</dd>
          </dl>
          {hits_bars}
          <div style="display:grid; grid-template-columns:1fr 1fr; gap:18px; margin-top:14px;">
            <div><h4 style="margin:0 0 6px 0; font-size:12px; color:var(--text-dim); text-transform:uppercase; letter-spacing:1px;">Top paths</h4>
              <table style='width:100%; border-collapse:collapse;'><tbody>{path_rows}</tbody></table>
            </div>
            <div><h4 style="margin:0 0 6px 0; font-size:12px; color:var(--text-dim); text-transform:uppercase; letter-spacing:1px;">Top referrers</h4>
              <table style='width:100%; border-collapse:collapse;'><tbody>{ref_rows}</tbody></table>
            </div>
          </div>
        </section>"""
    else:
        caddy_html = f"""
        <section class="detail-section">
          <h3>Traffic · Caddy access log</h3>
          <p class="hint">Log unavailable: <code>{_esc(caddy.get('error') or 'unknown')}</code><br>
             Pfad: <code>{_esc(caddy['log_path'])}</code> — falls die Datei existiert, lies-Rechte für den
             rustmetrics-Service-User per <code>chmod 644</code> oder ACL setzen.</p>
        </section>"""

    html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta name="robots" content="noindex, nofollow" />
<meta http-equiv="refresh" content="60" />
<title>Admin Stats — RustMetrics</title>
<link rel="stylesheet" href="/style.css?v=20260517g" />
<link rel="icon" type="image/svg+xml" href="/favicon.svg" />
</head><body>

<header class="topbar">
  <div class="brand">
    <a href="/" style="display:flex; align-items:center; gap:12px; text-decoration:none; color:inherit;">
      <span class="brand-mark" aria-hidden="true"></span>
      <span class="brand-name">RUST<span class="brand-name-accent">METRICS</span></span>
    </a>
  </div>
  <div class="topbar-actions" style="margin-left:auto;">
    <span style="color:var(--text-dim); font-size:12px;">admin · {_esc(user_name)}</span>
    <a class="btn-ghost" href="/" style="text-decoration:none;">← App</a>
  </div>
</header>

<article class="legal-page" style="max-width:1100px;">
  <h1 style="text-transform:none; font-size:22px; margin-bottom:4px;">Admin Stats</h1>
  <p style="color:var(--text-muted); font-size:12px; margin:0 0 18px 0;">
    auto-refresh every 60s · generated {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(now))}
  </p>

  <section class="detail-section">
    <h3>Users</h3>
    <dl class="detail-meta">
      <dt>Total signups (lifetime)</dt><dd>{fnum(d['users_total'])}</dd>
      <dt>Active sessions now</dt><dd>{fnum(d['sessions_active'])}</dd>
      <dt>Active last 1h / 24h / 7d / 30d</dt>
      <dd>{fnum(d['active_1h'])} · {fnum(d['active_24h'])} · {fnum(d['active_7d'])} · {fnum(d['active_30d'])}</dd>
      <dt>New signups 24h / 7d / 30d</dt>
      <dd>{fnum(d['new_24h'])} · {fnum(d['new_7d'])} · {fnum(d['new_30d'])}</dd>
    </dl>
    {sparkline_html}
  </section>

  <section class="detail-section">
    <h3>Engagement</h3>
    <dl class="detail-meta">
      <dt>Watched servers (per-user rows)</dt><dd>{fnum(d['watched_servers_total'])}</dd>
      <dt>Watched players (per-user rows)</dt><dd>{fnum(d['watched_players_total'])}</dd>
      <dt>Distinct servers tracked</dt><dd>{fnum(d['servers_total'])} ({fnum(d['servers_active'])} with active watchers)</dd>
      <dt>Player profiles built</dt><dd>{fnum(d['players_tracked'])} ({fnum(d['players_seen_24h'])} seen last 24h)</dd>
      <dt>SteamID resolved</dt><dd>{fnum(d['players_with_steam_id'])}</dd>
      <dt>Open player sessions (currently online somewhere we track)</dt><dd>{fnum(d['open_player_sessions'])}</dd>
      <dt>Snapshots in DB</dt><dd>{fnum(d['snapshots_total'])}</dd>
    </dl>
  </section>

  <section class="detail-section">
    <div style="display:grid; grid-template-columns:1fr 1fr; gap:18px;">
      <div><h3 style="margin-top:0;">Top watched servers</h3>{server_table}</div>
      <div><h3 style="margin-top:0;">Top watched players</h3>{player_table}</div>
    </div>
  </section>

  <section class="detail-section">
    <h3>Recent signups</h3>
    {signups_table}
  </section>

  {caddy_html}

  <p style="color:var(--text-muted); font-size:11px; margin-top:30px; text-align:center;">
    SQL aggregates from Postgres · Caddy log parsing from {_esc(caddy['log_path'])}<br>
    This page is admin-only and excluded from search engines (robots noindex).
  </p>
</article>

<footer class="footer">
  <a href="/">Home</a>
  <span class="footer-divider">·</span>
  <span style="color:var(--text-dim); font-size:11px;">/admin/stats</span>
</footer>

</body></html>"""
    return html, 200


def _fmt_int(n) -> str:
    """1234567 → '1 234 567' (thin-space). None/0 → '—'."""
    if n is None: return "—"
    if not isinstance(n, (int, float)): return str(n)
    s = f"{int(n):,}"
    return s.replace(",", " ")


def _fmt_duration_secs(s) -> str:
    """120 → '2m'. 7200 → '2h'. 86400 → '1d'. None → '—'."""
    if not s: return "—"
    s = int(s)
    if s < 60: return f"{s}s"
    if s < 3600: return f"{s // 60}m"
    if s < 86400:
        h = s // 3600
        return f"{h}h" if (s % 3600) < 60 else f"{h}h {(s % 3600) // 60:02d}m"
    d = s // 86400
    h = (s % 86400) // 3600
    return f"{d}d {h}h" if h else f"{d}d"


def _render_rust_stats_block(row: dict) -> str:
    """Generiert das HTML für den Lifetime-Stats-Block einer Rust-Player-Page."""
    # K/D Ratio berechnen
    kills  = row.get("kill_player") or 0
    deaths = row.get("deaths") or 0
    kd     = f"{kills / max(1, deaths):.2f}" if deaths else "—"
    hs     = row.get("headshot") or 0
    hs_pct = f"{100 * hs / max(1, kills):.1f}%" if kills else "—"
    bullets_fired   = row.get("bullet_fired") or 0
    bullets_hit_pl  = row.get("bullet_hit_player") or 0
    accuracy        = f"{100 * bullets_hit_pl / max(1, bullets_fired):.1f}%" if bullets_fired else "—"

    # Layout: Stat-Cards-Grid (numbers + labels)
    def card(label, value, sub=""):
        sub_html = f"<div class='stat-sub'>{_esc(sub)}</div>" if sub else ""
        return (f"<div class='stat-card'>"
                f"<div class='stat-num'>{_esc(value)}</div>"
                f"<div class='stat-label'>{_esc(label)}</div>"
                f"{sub_html}</div>")

    # Top-Row: Combat
    combat_cards = "".join([
        card("Kills",     _fmt_int(kills), sub=f"K/D {kd}"),
        card("Deaths",    _fmt_int(deaths)),
        card("Headshots", _fmt_int(hs),    sub=f"{hs_pct} of kills"),
        card("Hit rate",  accuracy,        sub=f"{_fmt_int(bullets_fired)} fired"),
    ])

    # Mid-Row: Bullets by target
    bullet_hits = [
        ("Players",       row.get("bullet_hit_player")),
        ("Buildings",     row.get("bullet_hit_building")),
        ("Signs",         row.get("bullet_hit_sign")),
        ("Wolves",        row.get("bullet_hit_wolf")),
        ("Bears",         row.get("bullet_hit_bear")),
        ("Boars",         row.get("bullet_hit_boar")),
        ("Stags",         row.get("bullet_hit_stag")),
        ("Horses",        row.get("bullet_hit_horse")),
        ("Corpses",       row.get("bullet_hit_corpse")),
    ]
    bullet_rows = "".join(
        f"<div class='stat-row'><span>{_esc(lbl)}</span>"
        f"<span class='stat-row-v'>{_fmt_int(v)}</span></div>"
        for lbl, v in bullet_hits if v
    )
    bullet_html = (
        "<div class='stat-list'><div class='stat-list-title'>Bullets hit, by target</div>"
        + bullet_rows + "</div>" if bullet_rows else ""
    )

    # Bottom-Row: Harvested
    harvest = [
        ("Wood",       row.get("harvested_wood")),
        ("Stones",     row.get("harvested_stones")),
        ("Cloth",      row.get("harvested_cloth")),
        ("Leather",    row.get("harvested_leather")),
        ("Sulfur ore", row.get("harvested_sulfur_ore")),
        ("Metal ore",  row.get("harvested_metal_ore")),
        ("HQ metal",   row.get("harvested_hq_metal_ore")),
        ("Scrap",      row.get("acquired_scrap")),
        ("Sulfur (gathered)", row.get("acquired_sulfur")),
        ("Metal frags",row.get("acquired_metalfrag")),
        ("Low-grade fuel", row.get("acquired_lowgradefuel")),
    ]
    harvest_rows = "".join(
        f"<div class='stat-row'><span>{_esc(lbl)}</span>"
        f"<span class='stat-row-v'>{_fmt_int(v)}</span></div>"
        for lbl, v in harvest if v
    )
    harvest_html = (
        "<div class='stat-list'><div class='stat-list-title'>Harvested / acquired</div>"
        + harvest_rows + "</div>" if harvest_rows else ""
    )

    # Misc
    misc = [
        ("Playtime (tracked by Rust)", _fmt_duration_secs(row.get("seconds_played"))),
        ("Wounded",        _fmt_int(row.get("wounded"))),
        ("C4 thrown",      _fmt_int(row.get("c4_thrown"))),
        ("Rockets fired",  _fmt_int(row.get("rocket_fired"))),
        ("Melee thrown",   _fmt_int(row.get("melee_thrown"))),
        ("Arrows fired",   _fmt_int(row.get("arrow_fired"))),
        ("Arrows hit",     _fmt_int(row.get("arrow_hit_player"))),
        ("Time cold",      _fmt_duration_secs(row.get("seconds_cold"))),
        ("Time hot",       _fmt_duration_secs(row.get("seconds_hot"))),
        ("Time comfy",     _fmt_duration_secs(row.get("seconds_comfort"))),
    ]
    misc_rows = "".join(
        f"<div class='stat-row'><span>{_esc(lbl)}</span>"
        f"<span class='stat-row-v'>{_esc(v)}</span></div>"
        for lbl, v in misc if v not in ("—", None, 0, "0")
    )
    misc_html = (
        "<div class='stat-list'><div class='stat-list-title'>Other</div>"
        + misc_rows + "</div>" if misc_rows else ""
    )

    fetched = row.get("fetched_at") or 0
    age = max(0, int(time.time()) - fetched) if fetched else None
    if   age is None:        age_str = ""
    elif age < 60:           age_str = "just now"
    elif age < 3600:         age_str = f"{age // 60}m ago"
    else:                    age_str = f"{age // 3600}h ago"
    footer = (f"<p class='hint' style='text-align:right; font-size:11px; margin-top:8px;'>"
              f"counters from Steam · cached {age_str}</p>" if age_str else "")

    return (
        f"<div class='stat-cards'>{combat_cards}</div>"
        f"<div class='stat-lists'>{bullet_html}{harvest_html}{misc_html}</div>"
        f"{footer}"
    )


def render_player_profile_page(bm_id: int) -> tuple[str, int]:
    """
    Server-side-rendered HTML für /player/<bm_id>. Liefert (html, status).
    Zeigt: aktueller Name, Aliases, Recent Activity (letzte 14 Tage über
    alle Server hinweg), Session-History pro Server, Currently-online-Status.
    Vollständig SSR — keine User-Auth nötig, public + indexable.
    """
    now = int(time.time())
    cutoff_14d = now - 14 * 86400
    with _Conn() as (conn, cur):
        # 1. Player-Metadaten
        cur.execute(
            "SELECT bm_id, current_name, aliases, steam_id, first_seen, last_seen "
            "FROM players WHERE bm_id=%s", (bm_id,))
        p = cur.fetchone()

        # Fallback: bm_id ist noch nicht in players-Tabelle, aber existiert in
        # watched_players (User hat ihn manuell hinzugefügt). Wir nehmen den
        # neuesten Namen von dort.
        if not p:
            cur.execute(
                "SELECT name, added_at FROM watched_players "
                "WHERE bm_player_id=%s ORDER BY added_at DESC LIMIT 1", (bm_id,))
            w = cur.fetchone()
            if not w:
                return _public_player_404_html(bm_id), 404
            p = {
                "bm_id":        bm_id,
                "current_name": w["name"],
                "aliases":      [],
                "steam_id":     None,
                "first_seen":   w["added_at"],
                "last_seen":    w["added_at"],
            }

        all_names = [p["current_name"]] + list(p["aliases"] or [])
        # Lowercase-Vergleich + Dedup
        seen_lower = set()
        all_names_dedup = []
        for n in all_names:
            if n and n.lower() not in seen_lower:
                seen_lower.add(n.lower())
                all_names_dedup.append(n)

        # 2. Sessions: Match per bm_player_id ODER per Name-Liste (für Legacy
        # ohne bm_player_id). Letzte 14 Tage.
        cur.execute(
            "SELECT ps.player_name, ps.start_ts, ps.end_ts, ps.bm_player_id, "
            "       s.id AS server_id, s.host, s.port, s.name AS server_name "
            "FROM player_sessions ps "
            "JOIN servers s ON s.id = ps.server_id "
            "WHERE ps.start_ts >= %s AND ("
            "      ps.bm_player_id = %s "
            "   OR (ps.bm_player_id IS NULL AND ps.player_name = ANY(%s))"
            ") "
            "ORDER BY ps.start_ts DESC LIMIT 500",
            (cutoff_14d, bm_id, all_names_dedup))
        sessions = [dict(x) for x in cur.fetchall()]

        # 3. Currently online? — irgendeine offene Session
        currently_on = None
        for s in sessions:
            if s["end_ts"] is None:
                currently_on = s
                break

        # 4. Aggregat pro Server (letzte 14 Tage): total minutes, session-count
        agg: dict[int, dict] = {}
        for s in sessions:
            sid = s["server_id"]
            end = s["end_ts"] or now
            dur = max(0, end - s["start_ts"])
            a = agg.setdefault(sid, {
                "server_id":   sid,
                "host":        s["host"],
                "port":        s["port"],
                "server_name": s["server_name"] or f"{s['host']}:{s['port']}",
                "total_secs":  0,
                "session_count": 0,
                "last_seen":   0,
            })
            a["total_secs"]    += dur
            a["session_count"] += 1
            a["last_seen"]      = max(a["last_seen"], end)
        agg_list = sorted(agg.values(), key=lambda x: -x["total_secs"])[:10]

    # 5. HTML-Render
    name        = p["current_name"]
    aliases     = [a for a in (p["aliases"] or []) if a and a.lower() != name.lower()][-5:]
    first_seen  = p["first_seen"]
    last_seen   = p["last_seen"]
    steam_id    = p.get("steam_id")
    # Lazy steam-id resolve via BM falls noch nicht bekannt — fire-and-forget,
    # damit der erste Page-Render nicht warten muss. Beim 2. Visit ist's da.
    if not steam_id and bm_id:
        try:
            steam_id = ensure_player_steam_id(bm_id)
        except Exception as e:
            print(f"[steam_id_resolve] bm={bm_id}: {e}", file=sys.stderr, flush=True)
    days_known  = max(0, (now - first_seen) // 86400)

    title = f"{name} — RustMetrics player profile"
    desc_bits = []
    if currently_on:
        sname = currently_on["server_name"] or f"{currently_on['host']}:{currently_on['port']}"
        desc_bits.append(f"🟢 online now on {sname}")
    else:
        if last_seen:
            ago = max(0, now - last_seen)
            if   ago < 3600:   desc_bits.append(f"last seen {ago // 60}m ago")
            elif ago < 86400:  desc_bits.append(f"last seen {ago // 3600}h ago")
            else:              desc_bits.append(f"last seen {ago // 86400}d ago")
    desc_bits.append(f"tracked for {days_known}d")
    if agg_list:
        desc_bits.append(f"played on {len(agg_list)} server(s) in last 14 days")
    og_description = " · ".join(desc_bits)

    # Aliases-HTML
    aliases_html = ""
    if aliases:
        chips = "".join(f"<span class='tag'>{_esc(a)}</span>" for a in aliases)
        aliases_html = f"<p class='hint' style='margin:4px 0 0 0;'>also known as: {chips}</p>"

    # Status-HTML
    status_html = ""
    if currently_on:
        sname = currently_on["server_name"] or f"{currently_on['host']}:{currently_on['port']}"
        shost, sport = currently_on["host"], currently_on["port"]
        dur = max(0, now - currently_on["start_ts"])
        if   dur < 3600:  dur_str = f"{dur // 60}m"
        elif dur < 86400: dur_str = f"{dur // 3600}h {(dur % 3600) // 60}m"
        else:             dur_str = f"{dur // 86400}d {(dur % 86400) // 3600}h"
        status_html = (
            f"<div style='padding:16px; background:rgba(80,160,80,0.08); "
            f"border:1px solid rgba(80,160,80,0.3); border-radius:8px;'>"
            f"<span style='color:var(--green); font-weight:700;'>● ONLINE NOW</span> "
            f"on <a href='/server/{_esc(shost)}:{sport}' style='color:var(--accent-bright)'>"
            f"{_esc(sname)}</a> "
            f"<span style='color:var(--text-dim); font-size:13px;'>· {dur_str}</span>"
            f"</div>")
    else:
        if last_seen:
            ago = max(0, now - last_seen)
            if   ago < 3600:   ago_str = f"{ago // 60}m ago"
            elif ago < 86400:  ago_str = f"{ago // 3600}h ago"
            else:              ago_str = f"{ago // 86400}d ago"
        else:
            ago_str = "never"
        status_html = (
            f"<div style='padding:16px; background:rgba(120,120,120,0.05); "
            f"border:1px solid var(--border); border-radius:8px;'>"
            f"<span style='color:var(--text-dim); font-weight:700;'>● OFFLINE</span> "
            f"<span style='color:var(--text-dim); font-size:13px;'>· last seen {ago_str}</span>"
            f"</div>")

    # Activity-Tabelle (Top-Server der letzten 14 Tage)
    activity_html = ""
    if agg_list:
        rows_html = []
        for a in agg_list:
            total_min = a["total_secs"] // 60
            if total_min >= 60:
                total_str = f"{total_min // 60}h {total_min % 60:02d}m"
            else:
                total_str = f"{total_min}m"
            rows_html.append(
                f"<tr>"
                f"<td><a href='/server/{_esc(a['host'])}:{a['port']}' "
                f"style='color:var(--text); text-decoration:none;'>"
                f"{_esc(a['server_name'])}</a>"
                f"<div style='font-size:11px; color:var(--text-dim); font-family:\"SF Mono\",monospace;'>"
                f"{_esc(a['host'])}:{a['port']}</div></td>"
                f"<td style='text-align:right; font-variant-numeric:tabular-nums;'>{total_str}</td>"
                f"<td style='text-align:right; font-variant-numeric:tabular-nums; color:var(--text-dim);'>"
                f"{a['session_count']}</td>"
                f"</tr>")
        activity_html = (
            "<table style='width:100%; border-collapse:collapse; margin-top:8px;'>"
            "<thead><tr style='border-bottom:1px solid var(--border); color:var(--text-dim);"
            " font-size:11px; text-transform:uppercase; letter-spacing:1px;'>"
            "<th style='text-align:left; padding:6px 8px;'>Server</th>"
            "<th style='text-align:right; padding:6px 8px;'>Playtime</th>"
            "<th style='text-align:right; padding:6px 8px;'>Sessions</th>"
            "</tr></thead><tbody>"
            + "".join(rows_html)
            + "</tbody></table>")
    else:
        activity_html = (
            "<p class='hint' style='text-align:center; padding:20px;'>"
            "No tracked activity in the last 14 days.</p>")

    # Steam-Profil-Button (nur wenn wir die SteamID haben)
    steam_button_html = ""
    if steam_id:
        steam_button_html = (
            f"<a href='https://steamcommunity.com/profiles/{int(steam_id)}/' "
            f"target='_blank' rel='noopener nofollow' "
            f"class='btn-steam' style='text-decoration:none; display:inline-flex; "
            f"align-items:center; gap:8px;'>"
            f"<span class='steam-icon' aria-hidden='true'>⌬</span> Steam Profile</a>")

    # Rust Lifetime Stats (Steam GetUserStatsForGame), server-side gerendert wenn vorhanden.
    # Falls steam_id noch nicht resolved, versucht das Client-JS später es nachzuziehen.
    stats_html = ""
    if steam_id:
        try:
            stats_row = get_rust_user_stats(steam_id, force_refresh=False)
        except Exception as e:
            print(f"[player_stats_render] {e}", file=sys.stderr, flush=True)
            stats_row = None
        if stats_row and not stats_row.get("is_private") and (stats_row.get("kill_player") is not None
                                                              or stats_row.get("seconds_played") is not None):
            stats_html = _render_rust_stats_block(stats_row)
        elif stats_row and stats_row.get("is_private"):
            stats_html = ("<p class='hint' style='padding:16px; text-align:center;'>"
                          "🔒 This player's Steam game-details are private — lifetime stats unavailable.</p>")

    # Recent-Sessions-Liste (letzte 8 closed sessions)
    recent_closed = [s for s in sessions if s["end_ts"] is not None][:8]
    recent_html = ""
    if recent_closed:
        items = []
        for s in recent_closed:
            sname = s["server_name"] or f"{s['host']}:{s['port']}"
            dur = max(0, s["end_ts"] - s["start_ts"])
            if   dur < 60:    dur_str = f"{dur}s"
            elif dur < 3600:  dur_str = f"{dur // 60}m"
            elif dur < 86400: dur_str = f"{dur // 3600}h {(dur % 3600) // 60:02d}m"
            else:             dur_str = f"{dur // 86400}d {(dur % 86400) // 3600}h"
            ago = max(0, now - s["end_ts"])
            if   ago < 3600:   ago_str = f"{ago // 60}m ago"
            elif ago < 86400:  ago_str = f"{ago // 3600}h ago"
            else:              ago_str = f"{ago // 86400}d ago"
            items.append(
                f"<div class='p' style='display:flex; justify-content:space-between; "
                f"align-items:center; padding:8px 10px; border-bottom:1px solid var(--border);'>"
                f"<a href='/server/{_esc(s['host'])}:{s['port']}' "
                f"style='color:var(--text); text-decoration:none; flex:1;'>"
                f"{_esc(sname)}</a>"
                f"<span style='color:var(--text-dim); font-size:12px; font-variant-numeric:tabular-nums;'>"
                f"{dur_str} · {ago_str}</span></div>")
        recent_html = "<div class='player-list'>" + "".join(items) + "</div>"

    html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{_esc(title)}</title>
<meta name="description" content="{_esc(og_description)}" />
<meta name="robots" content="{'index, follow' if agg_list else 'noindex, follow'}" />
<link rel="canonical" href="https://rustmetrics.eu/player/{int(bm_id)}" />
<meta property="og:type" content="profile" />
<meta property="og:site_name" content="RustMetrics" />
<meta property="og:title" content="{_esc(name)}" />
<meta property="og:description" content="{_esc(og_description)}" />
<meta property="og:url" content="https://rustmetrics.eu/player/{int(bm_id)}" />
<meta name="twitter:card" content="summary" />
<meta name="twitter:title" content="{_esc(name)}" />
<meta name="twitter:description" content="{_esc(og_description)}" />
<link rel="stylesheet" href="/style.css?v=20260517d" />
</head><body>

<header class="topbar">
  <div class="brand">
    <a href="/" style="display:flex; align-items:center; gap:12px; text-decoration:none; color:inherit;">
      <span class="brand-mark" aria-hidden="true"></span>
      <span class="brand-name">RUST<span class="brand-name-accent">METRICS</span></span>
    </a>
  </div>
  <div class="topbar-actions" style="margin-left:auto;">
    <a class="btn-steam" href="/auth/steam/login" data-anon-only style="text-decoration:none;">
      <span class="steam-icon" aria-hidden="true">⌬</span> Sign in to watch
    </a>
    <a class="btn-ghost" href="/" data-auth-only style="text-decoration:none; display:none;">
      ← My watchlist
    </a>
  </div>
</header>

<article class="legal-page" style="max-width:880px;">
  <a href="/" class="legal-back">← Home</a>
  <h1 style="text-transform:none; font-size:22px; margin-bottom:4px;">{_esc(name)}</h1>
  <p style="color:var(--text-muted); font-family:'SF Mono',monospace; font-size:13px; margin:0;">
    BM #{int(bm_id)} · tracked for {days_known}d
  </p>
  {aliases_html}

  <section class="detail-section" style="margin-top:18px;">
    {status_html}
    {('<div style="margin-top:12px;">' + steam_button_html + '</div>') if steam_button_html else ''}
  </section>

  {('<section class="detail-section"><h3>Lifetime stats · from Steam</h3>' + stats_html + '</section>') if stats_html else ''}

  <section class="detail-section">
    <h3>Recent activity · last 14 days</h3>
    {activity_html}
  </section>

  {('<section class="detail-section"><h3>Recent sessions</h3>' + recent_html + '</section>') if recent_html else ''}

  <p data-anon-only style="text-align:center; margin-top:30px; color:var(--text-muted); font-size:13px;">
    Want to get notified when {_esc(name)} comes online?
    <a href="/auth/steam/login" style="color:var(--accent-bright); font-weight:600;">Sign in with Steam</a>
    and add them to your watchlist — free, no email required.
  </p>
  <p data-auth-only style="text-align:center; margin-top:30px; color:var(--text-muted); font-size:13px; display:none;">
    To watch <strong>{_esc(name)}</strong>, open the server they play on (above) and
    use the <strong>+</strong> button next to their name in <em>Currently online</em>.
  </p>
</article>

<footer class="footer">
  <a href="/">Home</a>
  <span class="footer-divider">·</span>
  <a href="/imprint">Imprint</a>
  <span class="footer-divider">·</span>
  <a href="/privacy">Privacy</a>
  <span class="footer-divider">·</span>
  <a href="https://github.com/swiss-shift-ch/rustmetrics" target="_blank" rel="noopener" title="Source on GitHub">GitHub ★</a>
</footer>

<script src="/player-page.js?v=20260517f"></script>

</body></html>"""
    return html, 200


def _public_player_404_html(bm_id: int) -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8" />
<meta name="robots" content="noindex" />
<title>Player not found — RustMetrics</title>
<link rel="stylesheet" href="/style.css" />
</head><body>
<header class="topbar">
  <div class="brand">
    <a href="/" style="display:flex; align-items:center; gap:12px; text-decoration:none; color:inherit;">
      <span class="brand-mark"></span>
      <span class="brand-name">RUST<span class="brand-name-accent">METRICS</span></span>
    </a>
  </div>
</header>
<article class="legal-page">
  <a href="/" class="legal-back">← Home</a>
  <h1>Player not found</h1>
  <p>We don't track BattleMetrics player <code>#{int(bm_id)}</code> yet.
     Players appear on RustMetrics once they've been seen on a server in our watchlist database.</p>
  <p>Try the <a href="/">Server Browser</a> to find live servers.</p>
</article>
</body></html>"""


def _public_404_html(host: str, port: int) -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8" />
<title>Server not found — RustMetrics</title>
<link rel="stylesheet" href="/style.css" />
</head><body>
<header class="topbar">
  <div class="brand">
    <a href="/" style="display:flex; align-items:center; gap:12px; text-decoration:none; color:inherit;">
      <span class="brand-mark"></span>
      <span class="brand-name">RUST<span class="brand-name-accent">METRICS</span></span>
    </a>
  </div>
</header>
<article class="legal-page">
  <a href="/" class="legal-back">← Home</a>
  <h1>Server not found</h1>
  <p>BattleMetrics doesn't know <code>{_esc(host)}:{port}</code>, and we don't have it
     in our database either. Either the IP/port is wrong, or the server is offline and
     not tracked anywhere.</p>
  <p>Try the <a href="/">Server Browser</a> to find live servers.</p>
</article>
</body></html>"""


# ═══════════════════════════════════════════════════════════════════════════
#  HTTP-HANDLER
# ═══════════════════════════════════════════════════════════════════════════

class Handler(SimpleHTTPRequestHandler):
    server_version = "RustMetrics/1.0"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=STATIC_DIR, **kwargs)

    def log_message(self, fmt, *args):
        try:
            if args and "/api/" in str(args[0]): return
        except Exception: pass
        super().log_message(fmt, *args)

    # ── Helpers ────────────────────────────────────────────────────────
    def _client_ip(self) -> str:
        return (self.headers.get("X-Real-IP")
                or self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                or self.client_address[0])

    def _origin_ok(self) -> bool:
        """Defense-in-Depth gegen CSRF: Wenn Origin gesetzt (Browser), muss er ORIGIN matchen.
           Wenn nicht gesetzt (curl, Mobile App), erlauben — Session-Auth schützt dann allein."""
        origin = (self.headers.get("Origin") or "").rstrip("/")
        if not origin:
            return True
        return origin == ORIGIN.rstrip("/")

    def _json(self, data, status=200):
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length == 0: return {}
        try: return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception: return {}

    def _read_session(self) -> dict | None:
        cookie_hdr = self.headers.get("Cookie", "")
        if not cookie_hdr: return None
        c = SimpleCookie()
        try: c.load(cookie_hdr)
        except Exception: return None
        if COOKIE_NAME not in c: return None
        return lookup_session(c[COOKIE_NAME].value)

    def _send_html(self, path: str, status: int = 200):
        try:
            with open(path, "rb") as f: body = f.read()
        except FileNotFoundError:
            return self._json({"error": "not found"}, 404)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str, status: int = 302,
                  set_cookie: str | None = None,
                  clear_cookie: bool = False):
        self.send_response(status)
        self.send_header("Location", location)
        if set_cookie:
            cookie = SimpleCookie()
            cookie[COOKIE_NAME] = set_cookie
            cookie[COOKIE_NAME]["path"] = "/"
            cookie[COOKIE_NAME]["httponly"] = True
            cookie[COOKIE_NAME]["secure"] = True
            cookie[COOKIE_NAME]["samesite"] = "Lax"
            cookie[COOKIE_NAME]["max-age"] = str(SESSION_TTL)
            self.send_header("Set-Cookie", cookie.output(header="").strip())
        if clear_cookie:
            self.send_header("Set-Cookie",
                f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax")
        self.end_headers()

    # ── Routing ───────────────────────────────────────────────────────
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        # Rate-Limit (generell, sehr grosszügig)
        if not rate_limit_general(self._client_ip()):
            return self._json({"error": "rate limited"}, 429)

        # Auth-Endpoints
        if u.path == "/auth/steam/login":
            return self._redirect(steam_login_url())
        if u.path == "/auth/steam/callback":
            return self._auth_steam_callback(u.query)
        if u.path == "/auth/logout":
            sess = self._read_session()
            cookie_hdr = self.headers.get("Cookie", "")
            if cookie_hdr:
                c = SimpleCookie()
                try:
                    c.load(cookie_hdr)
                    if COOKIE_NAME in c:
                        delete_session(c[COOKIE_NAME].value)
                except Exception: pass
            return self._redirect("/", clear_cookie=True)

        # API
        if u.path == "/api/ping": return self._json({"ok": True, "ts": int(time.time())})
        if u.path == "/api/me":   return self._api_me()
        if u.path == "/api/servers":
            sess = self._read_session()
            if not sess: return self._json({"error": "auth required"}, 401)
            return self._json(api_servers_list(sess["user_id"]))
        if u.path.startswith("/api/server/"):
            parts = u.path.strip("/").split("/")
            if len(parts) == 3:
                sess = self._read_session()
                if not sess: return self._json({"error": "auth required"}, 401)
                try: sid = int(parts[2])
                except ValueError: return self._json({"error": "bad id"}, 400)
                qs = urllib.parse.parse_qs(u.query)
                hours = int(qs.get("hours", ["24"])[0])
                d = api_server_detail(sess["user_id"], sid, hours)
                if d is None: return self._json({"error": "not found"}, 404)
                return self._json(d)
        # Logged-in user's own Rust stats (cached, fetched from Steam API)
        if u.path == "/api/me/stats":
            sess = self._read_session()
            if not sess: return self._json({"error": "auth required"}, 401)
            try:
                stats = get_rust_user_stats(sess["user_id"], force_refresh=False)
                return self._json(stats or {"error": "stats unavailable"})
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        # Stats für einen einzelnen Player (Steam-ID wird per BM lazy resolved)
        # /api/player/<bm_id>/stats
        if u.path.startswith("/api/player/") and u.path.endswith("/stats"):
            spec = u.path[len("/api/player/"):-len("/stats")]
            try: bm_id = int(spec)
            except ValueError: return self._json({"error": "bad bm_id"}, 400)
            sid = ensure_player_steam_id(bm_id)
            if not sid:
                return self._json({
                    "available": False,
                    "reason": "no Steam ID known for this player (BattleMetrics didn't expose it)"
                })
            try:
                stats = get_rust_user_stats(sid, force_refresh=False)
                if not stats:
                    return self._json({"available": False, "reason": "fetch failed"})
                stats["available"] = True
                return self._json(stats)
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if u.path == "/api/browse":
            sess = self._read_session()
            uid = sess["user_id"] if sess else None
            qs = urllib.parse.parse_qs(u.query)
            def g(k, d=""):
                v = qs.get(k, [d]); return v[0] if v else d
            try:
                data = api_browse(
                    user_id=uid,
                    region=g("region", "all"), q=g("q", ""), tier=g("tier", "all"),
                    min_pop=int(g("min_pop", "0") or 0),
                    max_pop=int(g("max_pop", "9999") or 9999),
                    sort=g("sort", "pop_desc"),
                    limit=min(500, int(g("limit", "200") or 200)),
                    ip=self._client_ip(),
                )
                return self._json(data)
            except PermissionError as e:
                return self._json({"error": str(e)}, 429)
            except Exception as e:
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

        # Legal pages (English now; old DE paths kept as alias)
        if u.path in ("/imprint", "/impressum"):
            return self._send_html(os.path.join(TEMPLATES_DIR, "imprint.html"))
        if u.path in ("/privacy", "/datenschutz"):
            return self._send_html(os.path.join(TEMPLATES_DIR, "privacy.html"))

        # SEO: robots.txt + sitemap
        if u.path == "/robots.txt":
            body = (
                "User-agent: *\n"
                "Allow: /\n"
                "Disallow: /api/\n"
                "Disallow: /auth/\n"
                "\n"
                f"Sitemap: {ORIGIN}/sitemap.xml\n"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            self.wfile.write(body)
            return

        if u.path == "/sitemap.xml":
            # Dynamisch: Statische Seiten + Top-100 öffentliche Server aus Browse-Cache
            urls = [
                (f"{ORIGIN}/",         "daily",   "1.0"),
                (f"{ORIGIN}/imprint",  "yearly",  "0.3"),
                (f"{ORIGIN}/privacy",  "yearly",  "0.3"),
            ]
            # Top-Server aus dem ersten gefundenen Browse-Cache (Region-Keys "all", "eu", ...)
            with _browse_lock:
                cache_entry = (_browse_cache.get("browse:all")
                               or _browse_cache.get("browse:eu")
                               or next((v for k,v in _browse_cache.items() if k.startswith("browse:")), None))
            if cache_entry:
                # _browse_cache stores raw dict (mit 'servers' key); kein zusätzliches 'data'-wrapping
                raw = cache_entry.get("data", cache_entry)
                servers = (raw.get("servers") or [])[:100]
                for s in servers:
                    host = s.get("host"); port = s.get("port")
                    if host and port:
                        urls.append((f"{ORIGIN}/server/{host}:{port}", "hourly", "0.6"))
            # Top-Players nach letzter Aktivität (max 200) — bringt Long-Tail Traffic
            try:
                with _Conn() as (_c, _cur):
                    _cur.execute(
                        "SELECT bm_id FROM players "
                        "WHERE last_seen > %s ORDER BY last_seen DESC LIMIT 200",
                        (int(time.time()) - 7 * 86400,))
                    for r in _cur.fetchall():
                        urls.append((f"{ORIGIN}/player/{int(r['bm_id'])}", "daily", "0.4"))
            except Exception as e:
                print(f"[sitemap players] {e}", file=sys.stderr, flush=True)
            xml = ['<?xml version="1.0" encoding="UTF-8"?>',
                   '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
            for url, freq, prio in urls:
                xml.append(f"  <url><loc>{_esc(url)}</loc>"
                          f"<changefreq>{freq}</changefreq>"
                          f"<priority>{prio}</priority></url>")
            xml.append("</urlset>")
            body = "\n".join(xml).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/xml; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            self.wfile.write(body)
            return

        # Public server detail page  /server/<host>:<port>
        if u.path.startswith("/server/"):
            spec = u.path[len("/server/"):].rstrip("/")
            if ":" in spec:
                host, port_str = spec.rsplit(":", 1)
                try: port = int(port_str)
                except ValueError: port = 0
                if host and 0 < port < 65536:
                    try:
                        html, status = render_public_server_page(host, port)
                    except Exception as e:
                        print(f"[public_server] {host}:{port}: {e}", file=sys.stderr, flush=True)
                        return self._json({"error": str(e)}, 500)
                    body = html.encode("utf-8")
                    self.send_response(status)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "public, max-age=60")
                    self.end_headers()
                    self.wfile.write(body)
                    return
            return self._json({"error": "use /server/<host>:<port>"}, 400)

        # Calendar-ICS-Feed  /calendar/wipes.ics?token=<calendar_token>
        # Auth via Token (Cookie geht nicht — Calendar-Apps senden keine)
        if u.path == "/calendar/wipes.ics":
            qs = urllib.parse.parse_qs(u.query)
            token = (qs.get("token", [""])[0] or "").strip()
            user_id = lookup_user_by_calendar_token(token)
            if not user_id:
                return self._json({"error": "invalid token"}, 401)
            try:
                ics = render_wipes_ics(user_id)
            except Exception as e:
                print(f"[ics_feed] user={user_id}: {e}", file=sys.stderr, flush=True)
                return self._json({"error": str(e)}, 500)
            body = ics.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/calendar; charset=utf-8")
            self.send_header("Content-Disposition",
                "inline; filename=\"rustmetrics-wipes.ics\"")
            self.send_header("Content-Length", str(len(body)))
            # Cache hour-level. Most calendar clients re-fetch hourly anyway.
            self.send_header("Cache-Control", "private, max-age=3600")
            self.end_headers()
            self.wfile.write(body)
            return

        # Admin-Stats-Page  /admin/stats — auth + is_admin required
        if u.path == "/admin/stats":
            sess = self._read_session()
            if not sess:
                return self._redirect("/auth/steam/login")
            if not sess.get("is_admin"):
                return self._json({"error": "admin only"}, 403)
            try:
                html, status = render_admin_stats_page(sess)
            except Exception as e:
                print(f"[admin_stats] {e}", file=sys.stderr, flush=True)
                return self._json({"error": str(e)}, 500)
            body = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, private")
            self.end_headers()
            self.wfile.write(body)
            return

        # Public player profile page  /player/<bm_id>
        if u.path.startswith("/player/"):
            spec = u.path[len("/player/"):].rstrip("/")
            try: bm_id = int(spec)
            except ValueError: bm_id = 0
            if bm_id <= 0:
                return self._json({"error": "use /player/<bm_player_id>"}, 400)
            try:
                html, status = render_player_profile_page(bm_id)
            except Exception as e:
                print(f"[player_profile] bm_id={bm_id}: {e}", file=sys.stderr, flush=True)
                return self._json({"error": str(e)}, 500)
            body = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=60")
            self.end_headers()
            self.wfile.write(body)
            return

        # Default: static files
        return super().do_GET()

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if not rate_limit_general(self._client_ip()):
            return self._json({"error": "rate limited"}, 429)
        if not self._origin_ok():
            return self._json({"error": "bad origin"}, 403)
        parts = u.path.strip("/").split("/")
        sess = self._read_session()
        if not sess: return self._json({"error": "auth required"}, 401)

        if u.path == "/api/servers":
            body = self._read_json()
            host = (body.get("host") or "").strip()
            try: port = int(body.get("port") or 0)
            except (TypeError, ValueError): port = 0
            if not host or not (0 < port < 65536):
                return self._json({"error": "host and port required"}, 400)
            try:
                sid = add_watch_server(sess["user_id"], host, port)
                return self._json({"ok": True, "id": sid})
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if (len(parts) == 4 and parts[0] == "api" and parts[1] == "server"
                and parts[3] == "watch"):
            try: sid = int(parts[2])
            except ValueError: return self._json({"error": "bad id"}, 400)
            body = self._read_json()
            name = (body.get("name") or "").strip()
            if not name: return self._json({"error": "name required"}, 400)
            try:
                add_watch_player(sess["user_id"], sid, name)
                return self._json({"ok": True})
            except PermissionError as e:
                return self._json({"error": str(e)}, 403)
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if u.path == "/api/me/settings":
            return self._api_save_settings(sess["user_id"])
        if u.path == "/api/me/stats/refresh":
            try:
                stats = get_rust_user_stats(sess["user_id"], force_refresh=True)
                return self._json(stats or {"error": "stats unavailable"})
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        if u.path.startswith("/api/player/") and u.path.endswith("/stats/refresh"):
            spec = u.path[len("/api/player/"):-len("/stats/refresh")]
            try: bm_id = int(spec)
            except ValueError: return self._json({"error": "bad bm_id"}, 400)
            sid = ensure_player_steam_id(bm_id)
            if not sid:
                return self._json({"available": False, "reason": "no Steam ID known"})
            try:
                stats = get_rust_user_stats(sid, force_refresh=True)
                if not stats:
                    return self._json({"available": False, "reason": "fetch failed"})
                stats["available"] = True
                return self._json(stats)
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        if u.path == "/api/me/calendar/reset":
            try:
                new_token = reset_calendar_token(sess["user_id"])
                cal_url    = f"{ORIGIN}/calendar/wipes.ics?token={new_token}"
                cal_webcal = cal_url.replace("https://", "webcal://").replace("http://", "webcal://")
                return self._json({"ok": True, "url": cal_url, "webcal_url": cal_webcal})
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        if u.path == "/api/me/test-webhook":
            return self._api_test_webhook(sess["user_id"])

        return self._json({"error": "not found"}, 404)

    def do_PUT(self):
        # PUT alias for /api/me/settings, falls Frontend REST-style PUT will
        if not rate_limit_general(self._client_ip()):
            return self._json({"error": "rate limited"}, 429)
        if not self._origin_ok():
            return self._json({"error": "bad origin"}, 403)
        sess = self._read_session()
        if not sess: return self._json({"error": "auth required"}, 401)
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/me/settings":
            return self._api_save_settings(sess["user_id"])
        return self._json({"error": "not found"}, 404)

    def do_DELETE(self):
        u = urllib.parse.urlparse(self.path)
        if not rate_limit_general(self._client_ip()):
            return self._json({"error": "rate limited"}, 429)
        if not self._origin_ok():
            return self._json({"error": "bad origin"}, 403)
        sess = self._read_session()
        if not sess: return self._json({"error": "auth required"}, 401)
        parts = u.path.strip("/").split("/")
        # /api/server/:id
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "server":
            try: sid = int(parts[2])
            except ValueError: return self._json({"error": "bad id"}, 400)
            remove_watch_server(sess["user_id"], sid)
            return self._json({"ok": True})
        # /api/server/:id/watch/:wid
        if (len(parts) == 5 and parts[0] == "api" and parts[1] == "server"
                and parts[3] == "watch"):
            try: wid = int(parts[4])
            except ValueError: return self._json({"error": "bad id"}, 400)
            remove_watch_player(sess["user_id"], wid)
            return self._json({"ok": True})
        return self._json({"error": "not found"}, 404)

    # ── Auth-Endpoint-Impl ────────────────────────────────────────────
    def _auth_steam_callback(self, query_string: str):
        qs = urllib.parse.parse_qs(query_string)
        # parse_qs gibt Listen, OpenID braucht einzelne Values
        params = {k: v[0] if isinstance(v, list) and v else "" for k, v in qs.items()}
        steamid64 = steam_verify(params)
        if not steamid64:
            return self._json({"error": "Steam login failed"}, 400)
        profile = fetch_steam_profile(steamid64)
        upsert_user(steamid64, profile)
        token = create_session(
            steamid64, ip=self._client_ip(),
            ua=self.headers.get("User-Agent", "")[:255]
        )
        return self._redirect("/", set_cookie=token)

    def _api_me(self):
        sess = self._read_session()
        if not sess:
            return self._json({"authenticated": False})
        # Settings dazuladen
        settings = {"discord_webhook": None, "notify_online": True, "notify_offline": False}
        try:
            with _Conn() as (conn, cur):
                cur.execute(
                    "SELECT discord_webhook, notify_online, notify_offline FROM users WHERE id=%s",
                    (sess["user_id"],))
                r = cur.fetchone()
                if r:
                    settings = {
                        "discord_webhook": r["discord_webhook"],
                        "notify_online":   bool(r["notify_online"]),
                        "notify_offline":  bool(r["notify_offline"]),
                    }
        except Exception: pass
        # Calendar-URL (lazy generiert beim ersten Mal)
        try:
            cal_token = get_or_create_calendar_token(sess["user_id"])
            cal_url   = f"{ORIGIN}/calendar/wipes.ics?token={cal_token}"
            # webcal:// für 1-tap subscribe in Apple/Google Calendar (kein https)
            cal_webcal = cal_url.replace("https://", "webcal://").replace("http://", "webcal://")
        except Exception:
            cal_url = cal_webcal = None
        return self._json({
            "authenticated": True,
            "user_id":       str(sess["user_id"]),
            "display_name":  sess.get("display_name") or str(sess["user_id"]),
            "avatar_url":    sess.get("avatar_url"),
            "is_admin":      bool(sess.get("is_admin")),
            "settings":      settings,
            "calendar": {
                "url":        cal_url,
                "webcal_url": cal_webcal,
            },
        })

    def _api_save_settings(self, user_id: int):
        body = self._read_json()
        webhook = (body.get("discord_webhook") or "").strip() or None
        notify_online  = bool(body.get("notify_online",  True))
        notify_offline = bool(body.get("notify_offline", False))
        # Validate Discord webhook
        if webhook and not is_valid_discord_webhook(webhook):
            return self._json({"error": "invalid Discord webhook URL"}, 400)
        try:
            with _Conn() as (conn, cur):
                cur.execute(
                    "UPDATE users SET discord_webhook=%s, notify_online=%s, notify_offline=%s WHERE id=%s",
                    (webhook, notify_online, notify_offline, user_id))
            return self._json({"ok": True})
        except Exception as e:
            return self._json({"error": str(e)}, 500)

    def _api_test_webhook(self, user_id: int):
        with _Conn() as (conn, cur):
            cur.execute("SELECT discord_webhook FROM users WHERE id=%s", (user_id,))
            r = cur.fetchone()
        wh = (r["discord_webhook"] if r else None)
        if not wh:
            return self._json({"error": "no webhook configured"}, 400)
        if not is_valid_discord_webhook(wh):
            return self._json({"error": "invalid webhook URL"}, 400)
        embed = {
            "username":   "RustMetrics",
            "avatar_url": "https://rustmetrics.eu/favicon.png",
            "embeds": [{
                "title": "✓ RustMetrics test webhook",
                "description": "If you can read this, your Discord webhook is wired up correctly.",
                "color":  0xCD412A,
                "footer": {"text": "rustmetrics.eu"},
            }],
        }
        ok, msg = _post_discord_webhook(wh, embed)
        return self._json({"ok": ok, "message": msg}, 200 if ok else 502)


# ═══════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════

def _banner():
    print("")
    print("  ██████  ██    ██ ███████ ████████   ███    ███ ███████ ████████ ██████  ██  ██████ ███████")
    print("  ██   ██ ██    ██ ██         ██      ████  ████ ██         ██    ██   ██ ██ ██      ██     ")
    print("  ██████  ██    ██ ███████    ██      ██ ████ ██ █████      ██    ██████  ██ ██      ███████")
    print("  ██   ██ ██    ██      ██    ██      ██  ██  ██ ██         ██    ██   ██ ██ ██           ██")
    print("  ██   ██  ██████  ███████    ██      ██      ██ ███████    ██    ██   ██ ██  ██████ ███████")
    print("                                                                            PUBLIC EDITION   ")
    print("")
    print(f"  Listen:           {HOST}:{PORT}")
    print(f"  Origin:           {ORIGIN}")
    print(f"  PG:               {_env('RM_PGHOST', '127.0.0.1')}:{_env('RM_PGPORT', 5432, int)}/{_env('RM_PGDATABASE', 'rustmetrics')}")
    print(f"  Poll-Intervall:   {POLL_INTERVAL}s")
    print(f"  Browse-Cache:     {BROWSE_CACHE_TTL}s")
    print(f"  Steam-API-Key:    {'gesetzt' if STEAM_API_KEY else 'NICHT gesetzt (Avatar/Name fehlen)'}")
    print(f"  Admin-SteamIDs:   {ADMIN_STEAMIDS or '—'}")
    print(f"  Rate-Limit:       {RL_REQ_PER_MIN} req/min/IP, browse-miss {RL_BROWSE_PER_H}/h/IP")
    print("")


def main():
    init_pool()
    # quick DB-Check
    try:
        with _Conn() as (conn, cur):
            cur.execute("SELECT 1")
            cur.fetchone()
    except Exception as e:
        print(f"FATAL: Postgres-Verbindung fehlgeschlagen: {e}", file=sys.stderr)
        sys.exit(2)

    t = threading.Thread(target=poller_loop, daemon=True)
    t.start()

    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    httpd.daemon_threads = True
    _banner()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutdown — bitte einen Moment …")
        _poller_stop.set()
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    main()
