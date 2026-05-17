"""Quick master-server probe — to be run on the live server."""
import socket, time
IPS = ['208.64.200.39','208.64.200.65','208.64.200.52','162.254.196.84']
for ip in IPS:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(4)
    req = bytes([0x31, 0xFF]) + b'0.0.0.0:0\x00' + b'\\appid\\252490\\dedicated\\1\x00'
    t0 = time.time()
    try:
        s.sendto(req, (ip, 27011))
        data, _ = s.recvfrom(2048)
        dt = (time.time() - t0) * 1000
        if data[:6] == b'\xFF\xFF\xFF\xFF\x66\x0A':
            count = (len(data) - 6) // 6
            first_ip = '.'.join(str(b) for b in data[6:10]) if len(data) >= 10 else '?'
            print(f'{ip:18s} OK header  {dt:.0f}ms  ~{count} addrs  first={first_ip}  bytes={len(data)}')
        else:
            print(f'{ip:18s} BAD header  {dt:.0f}ms  first6={data[:6].hex()}  total={len(data)}')
    except Exception as e:
        print(f'{ip:18s} ERR  {type(e).__name__}: {e}')
    s.close()
