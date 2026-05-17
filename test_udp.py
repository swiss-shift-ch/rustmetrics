"""Tests UDP-Egress vom Server. Master + NTP."""
import socket, struct

print('=== Steam Master (port 27011) ===')
MASTER_IPS = ['208.64.200.39', '208.64.200.65', '208.64.200.52', '162.254.196.84']
req = bytes([0x31, 0xFF]) + b'0.0.0.0:0\x00' + br'\appid\252490' + b'\x00'
print('Request hex:', req.hex())
for ip in MASTER_IPS:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(5)
    try:
        s.sendto(req, (ip, 27011))
        data, src = s.recvfrom(2048)
        print(f'  {ip:18s} OK {len(data)} bytes from {src}')
    except Exception as e:
        print(f'  {ip:18s} ERR {type(e).__name__}: {e}')
    s.close()

print()
print('=== NTP (port 123) — sanity check that any UDP egress works ===')
for ip in ['129.6.15.28', '132.163.97.1', '162.159.200.123']:  # NIST, NIST, Cloudflare
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(3)
    req = b'\x1b' + b'\x00' * 47
    try:
        s.sendto(req, (ip, 123))
        data, _ = s.recvfrom(48)
        print(f'  {ip:18s} NTP OK {len(data)} bytes')
    except Exception as e:
        print(f'  {ip:18s} NTP ERR {type(e).__name__}: {e}')
    s.close()

print()
print('=== A2S to a known Rust server (sanity check that A2S works at all) ===')
# Try one from BattleMetrics: Rusty In Places (or just random Rust IPs known to exist)
A2S_TARGETS = [('51.91.156.205', 28015), ('5.83.181.69', 28015)]
A2S_INFO = b'\xFF\xFF\xFF\xFFTSource Engine Query\x00'
for ip, port in A2S_TARGETS:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(3)
    try:
        s.sendto(A2S_INFO, (ip, port))
        data, _ = s.recvfrom(2048)
        print(f'  {ip}:{port}  A2S OK {len(data)} bytes  first6={data[:6].hex()}')
    except Exception as e:
        print(f'  {ip}:{port}  A2S ERR {type(e).__name__}: {e}')
    s.close()
