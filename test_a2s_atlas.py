"""Debug-Probe gegen einen einzelnen Server: was kommt zurück?"""
import socket

HOST, PORT = "205.178.168.205", 28010
A2S_INFO = b"\xFF\xFF\xFF\xFFTSource Engine Query\x00"

print(f"=== Anfrage 1: A2S_INFO an {HOST}:{PORT} ===")
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(4)
s.sendto(A2S_INFO, (HOST, PORT))
try:
    data, _ = s.recvfrom(2048)
    print(f"  Length: {len(data)}")
    print(f"  Hex:    {data.hex()}")
    print(f"  ASCII:  {data!r}")
except Exception as e:
    print(f"  err: {e}")
s.close()

print()
print("=== Anfrage 2: mit Challenge falls 0x41 zurück ===")
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(4)
s.sendto(A2S_INFO, (HOST, PORT))
data, _ = s.recvfrom(2048)
if len(data) >= 9 and data[4:5] == b"\x41":
    challenge = data[5:9]
    print(f"  Got 0x41 challenge: {challenge.hex()}")
    s.sendto(A2S_INFO + challenge, (HOST, PORT))
    data2, _ = s.recvfrom(4096)
    print(f"  Reply length: {len(data2)}")
    print(f"  Hex(80): {data2[:80].hex()}")
    print(f"  ASCII(80): {data2[:80]!r}")
elif len(data) >= 5 and data[4:5] == b"I":
    print("  → Direkt 'I' (sollte normal parsen)")
else:
    print(f"  → unbekannter Reply, len={len(data)} type={data[4:5] if len(data)>=5 else 'N/A'}")
s.close()
