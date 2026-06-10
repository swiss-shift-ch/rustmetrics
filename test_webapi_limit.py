"""GetServerList-Probe mit hohem Limit — auf dem Live-Server laufen lassen.

Prueft, ob mit limit=20000 die komplette Rust-Serverliste kommt und ob die
grossen Netzwerke (Moose, Rustafied, Facepunch, ...) jetzt drin sind.

    python3 test_webapi_limit.py            # liest Key aus env oder /opt/rustmetrics/.env
    RM_STEAM_API_KEY=xyz python3 test_webapi_limit.py
"""
import json, os, sys, time, urllib.parse, urllib.request

def get_key():
    key = os.environ.get("RM_STEAM_API_KEY", "").strip()
    if key:
        return key
    for envpath in (".env", "/opt/rustmetrics/.env"):
        try:
            for line in open(envpath, encoding="utf-8"):
                line = line.strip()
                if line.startswith("RM_STEAM_API_KEY=") :
                    key = line.split("=", 1)[1].strip()
                    if key:
                        return key
        except OSError:
            continue
    return ""

KEY = get_key()
if not KEY:
    sys.exit("FEHLER: kein RM_STEAM_API_KEY gefunden (env oder .env)")

LIMIT = int(os.environ.get("RM_BROWSE_MASTER_LIMIT", "20000"))
params = {"key": KEY, "filter": r"\appid\252490\dedicated\1", "limit": str(LIMIT)}
url = ("https://api.steampowered.com/IGameServersService/GetServerList/v1/?"
       + urllib.parse.urlencode(params))

t0 = time.time()
with urllib.request.urlopen(url, timeout=60) as r:
    body = r.read()
dt = time.time() - t0
servers = json.loads(body).get("response", {}).get("servers", [])
names = [(s.get("name") or "").lower() for s in servers]

print(f"limit={LIMIT}  ->  {len(servers)} Server  in {dt:.1f}s  ({len(body)/1e6:.1f} MB)")
print()
for needle in ("moose", "rustafied", "facepunch", "rusticated", "rustoria"):
    hits = [n for n in names if needle in n]
    status = "OK " if hits else "FEHLT"
    print(f"  [{status}] {needle:<12} {len(hits):>4} Treffer" + (f"   z.B. {hits[0][:60]!r}" if hits else ""))
print()
top = sorted(servers, key=lambda s: -(s.get("players") or 0))[:5]
print("Top 5 nach Population:")
for s in top:
    print(f"  {s.get('players',0):>4}/{s.get('max_players',0):<4} {(s.get('name') or '')[:70]}")
