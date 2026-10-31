#!/usr/bin/env python3
"""
sdn_topo.py - Mininet topologies for the diagnostics project (OpenFlow 1.3, remote controller).

  sudo python3 sdn_topo.py --topo triangle      # 3 switches, 2 paths (s1-s3 direct, s1-s2-s3)
  sudo python3 sdn_topo.py --topo multi         # 5 switches: s1-s2-s5 (short) and s1-s3-s4-s5 (long)
  sudo python3 sdn_topo.py --topo single        # 3 switches in a line, one path only (no alternative)

h1 = 10.0.0.1 (client, on s1)    h2 = 10.0.0.2 (server, on the last switch)
Start the controller FIRST. Clean leftovers with:  sudo mn -c
"""
import argparse
import os
import time

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import info, setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController

HERE = os.path.dirname(os.path.abspath(__file__))

TOPOS = {
    "single":   {"n": 3, "links": [(1, 2), (2, 3)], "h2_on": 3, "fail": ("s1", "s2")},
    "triangle": {"n": 3, "links": [(1, 2), (2, 3), (1, 3)], "h2_on": 3, "fail": ("s1", "s3")},
    "multi":    {"n": 5, "links": [(1, 2), (2, 5), (1, 3), (3, 4), (4, 5)], "h2_on": 5, "fail": ("s1", "s2")},
}


def build_net(topo, ctrl_ip="127.0.0.1", ctrl_port=6653, delay=None, loss=None):
    t = TOPOS[topo]
    net = Mininet(controller=None, switch=OVSSwitch, link=TCLink, autoSetMacs=True)
    net.addController("c0", controller=RemoteController, ip=ctrl_ip, port=ctrl_port)
    sw = {i: net.addSwitch(f"s{i}", dpid=f"{i:016x}", protocols="OpenFlow13")
          for i in range(1, t["n"] + 1)}
    h1 = net.addHost("h1", ip="10.0.0.1/24")
    h2 = net.addHost("h2", ip="10.0.0.2/24")
    opts = {}
    if delay:
        opts["delay"] = delay
    if loss:
        opts["loss"] = loss
    for a, b in t["links"]:
        net.addLink(sw[a], sw[b], **opts)
    net.addLink(h1, sw[1])
    net.addLink(h2, sw[t["h2_on"]])
    return net, h1, h2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topo", choices=TOPOS, default="triangle")
    ap.add_argument("--ctrl-ip", default="127.0.0.1")
    ap.add_argument("--ctrl-port", type=int, default=6653)
    ap.add_argument("--delay", help='delay on switch-switch links, e.g. "5ms"')
    ap.add_argument("--loss", type=float, help="percent loss on switch-switch links")
    ap.add_argument("--no-server", action="store_true", help="do not auto-start the UDP echo server in h2")
    a = ap.parse_args()

    setLogLevel("info")
    net, h1, h2 = build_net(a.topo, a.ctrl_ip, a.ctrl_port, a.delay, a.loss)
    net.start()
    info(f"*** waiting 10 s for the controller to discover links\n")
    time.sleep(10)
    if not a.no_server:
        h2.cmd(f"python3 {os.path.join(HERE, 'udp_server.py')} > /tmp/udp_server.log 2>&1 &")
        info("*** UDP echo server started on h2 (port 5005)\n")
    fa, fb = TOPOS[a.topo]["fail"]
    info("*** Try:  pingall   |   h1 python3 udp_client.py 10.0.0.2 --count 10\n")
    info(f"*** Failure test:  link {fa} {fb} down   /   link {fa} {fb} up\n")
    CLI(net)
    net.stop()


if __name__ == "__main__":
    main()
