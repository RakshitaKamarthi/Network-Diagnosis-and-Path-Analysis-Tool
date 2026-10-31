#!/usr/bin/env python3
"""UDP echo server. Run inside h2:  h2 python3 udp_server.py &"""
import argparse
import socket

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=5005)
a = ap.parse_args()

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("0.0.0.0", a.port))
print(f"[*] UDP echo server listening on 0.0.0.0:{a.port}", flush=True)
n = 0
while True:
    data, addr = s.recvfrom(2048)
    s.sendto(data, addr)          # echo back immediately
    n += 1
    if n % 50 == 0:
        print(f"[*] echoed {n} packets", flush=True)
