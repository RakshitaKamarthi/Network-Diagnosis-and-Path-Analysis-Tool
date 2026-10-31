#!/usr/bin/env python3
"""UDP probe client. Run inside h1:
     h1 python3 udp_client.py 10.0.0.2 --count 10 --interval 0.5
Measures RTT and packet loss, prints per-probe lines and a summary, and writes
the results as JSON (default /tmp/diag_result.json) for diagnostic_tool.py."""
import argparse
import json
import socket
import statistics
import time

ap = argparse.ArgumentParser()
ap.add_argument("server", nargs="?", default="10.0.0.2")
ap.add_argument("--port", type=int, default=5005)
ap.add_argument("--count", type=int, default=10)
ap.add_argument("--interval", type=float, default=0.5)
ap.add_argument("--timeout", type=float, default=2.0)
ap.add_argument("--out", default="/tmp/diag_result.json")
a = ap.parse_args()

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(a.timeout)
probes, rtts = [], []
print(f"[*] Probing {a.server}:{a.port}  x{a.count}  interval={a.interval}s  timeout={a.timeout}s")

for seq in range(1, a.count + 1):
    t0 = time.time()
    rtt = None
    try:
        s.sendto(json.dumps({"seq": seq, "time": t0}).encode(), (a.server, a.port))
        while True:                                  # ignore late echoes of older probes
            data, _ = s.recvfrom(2048)
            if json.loads(data.decode()).get("seq") == seq:
                rtt = (time.time() - t0) * 1000
                break
        rtts.append(rtt)
        print(f"Probe {seq:3d}: OK   RTT = {rtt:.2f} ms")
    except socket.timeout:
        print(f"Probe {seq:3d}: LOST (timeout)")
    except (OSError, ValueError) as e:
        print(f"Probe {seq:3d}: ERROR {e}")
    probes.append({"seq": seq, "t_sent": t0, "rtt_ms": rtt, "lost": rtt is None})
    time.sleep(a.interval)
s.close()

sent, recv = a.count, len(rtts)
result = {
    "client": "h1", "server": a.server, "timestamp": time.time(),
    "sent": sent, "received": recv, "lost": sent - recv,
    "loss_pct": (sent - recv) / sent * 100,
    "avg_rtt": statistics.mean(rtts) if rtts else 0.0,
    "min_rtt": min(rtts) if rtts else 0.0,
    "max_rtt": max(rtts) if rtts else 0.0,
    "jitter": (statistics.mean(abs(x - y) for x, y in zip(rtts, rtts[1:])) if len(rtts) > 1 else 0.0),
    "probes": probes,
}
with open(a.out, "w") as f:
    json.dump(result, f, indent=2)
print(f"\n--- RESULT --- loss {result['loss_pct']:.1f}%  avg RTT {result['avg_rtt']:.2f} ms  "
      f"jitter {result['jitter']:.2f} ms\n[*] written to {a.out}")
