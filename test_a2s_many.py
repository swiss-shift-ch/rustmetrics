"""Pollt die Top-15 EU-Server aus Browse und testet jeden mit echtem A2S_INFO+PLAYER."""
import json, socket, urllib.request

resp = urllib.request.urlopen("https://rustmetrics.eu/api/browse?region=eu&limit=15")
data = json.load(resp)
A2S_INFO = b"\xFF\xFF\xFF\xFFTSource Engine Query\x00"

print(f"{'Pop':>5}  {'Host:Port':<25}  Tier        A2S_INFO   A2S_PLAYER  Name")
print("-" * 110)
for s in data["servers"]:
    host, port, name = s["host"], s["port"], (s["name"] or "")[:40]
    pop, mx, tier = s["players_count"], s["max_players"], s["tier"]
    info_status, player_count = "??", "??"

    # A2S_INFO
    try:
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); sk.settimeout(3)
        sk.sendto(A2S_INFO, (host, port))
        d, _ = sk.recvfrom(2048)
        if len(d) >= 9 and d[4:5] == b"\x41":
            sk.sendto(A2S_INFO + d[5:9], (host, port))
            d, _ = sk.recvfrom(2048)
        if len(d) >= 5 and d[4:5] == b"I":
            info_status = f"OK({len(d)}b)"
        else:
            info_status = f"BAD len={len(d)}"
        sk.close()
    except Exception as e:
        info_status = type(e).__name__

    # A2S_PLAYER (challenge then real)
    try:
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); sk.settimeout(3)
        sk.sendto(b"\xFF\xFF\xFF\xFF\x55\xFF\xFF\xFF\xFF", (host, port))
        d, _ = sk.recvfrom(4096)
        if len(d) >= 9 and d[4:5] == b"\x41":
            sk.sendto(b"\xFF\xFF\xFF\xFF\x55" + d[5:9], (host, port))
            d, _ = sk.recvfrom(8192)
        if len(d) >= 6 and d[4:5] == b"D":
            count = d[5]
            player_count = f"OK n={count}"
        else:
            player_count = f"BAD len={len(d)}"
        sk.close()
    except Exception as e:
        player_count = type(e).__name__

    print(f"{pop or 0:>5}  {host+':'+str(port):<25}  {tier:<10}  {info_status:<10} {player_count:<11} {name}")
