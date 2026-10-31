#!/usr/bin/env python3
"""
reroute_controller.py  -  OpenFlow 1.3 controller app for Ryu / OS-Ken.

* Learns hosts, answers ARP itself (proxy ARP) and floods unknown ARP ONLY to
  host-facing ports, so loops in the topology cannot cause a broadcast storm.
* Installs shortest-path flows (match eth_src+eth_dst) for each host pair.
* On link DOWN / UP it recomputes paths and reroutes automatically.
* REROUTE=0 (environment variable) turns rerouting OFF = static baseline.
* Writes timestamped events to /tmp/sdn_events.log and the current path to /tmp/sdn_path.

Run (REST + topology discovery loaded together with this app):
  ryu-manager --observe-links --ofp-tcp-listen-port 6653 --wsapi-port 8080 \
      ryu.app.ofctl_rest ryu.app.rest_topology reroute_controller.py
  # OS-Ken: osken-manager ... os_ken.app.ofctl_rest os_ken.app.rest_topology reroute_controller.py
  # static baseline: prefix with  REROUTE=0
Wait ~10 s after starting Mininet (link discovery) before the first ping.
"""
import os
import time
from collections import deque

try:
    from ryu.base import app_manager
    from ryu.controller import ofp_event
    from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
    from ryu.ofproto import ofproto_v1_3
    from ryu.lib.packet import packet, ethernet, arp, ether_types
    from ryu.topology import event
    from ryu.topology.api import get_switch
except ImportError:  # OS-Ken fork has identical module layout
    from os_ken.base import app_manager
    from os_ken.controller import ofp_event
    from os_ken.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
    from os_ken.ofproto import ofproto_v1_3
    from os_ken.lib.packet import packet, ethernet, arp, ether_types
    from os_ken.topology import event
    from os_ken.topology.api import get_switch

REROUTE = os.environ.get("REROUTE", "1") != "0"
LOG_FILE = "/tmp/sdn_events.log"
PATH_FILE = "/tmp/sdn_path"
PRIORITY = 100
BCAST = "ff:ff:ff:ff:ff:ff"


def shortest_path(links, src, dst):
    """links = {(a, b): port_on_a}. Fewest hops, deterministic tie-break. None if unreachable."""
    if src == dst:
        return [src]
    adj = {}
    for (a, b) in links:
        adj.setdefault(a, []).append(b)
    prev, q = {src: None}, deque([src])
    while q:
        u = q.popleft()
        for v in sorted(adj.get(u, [])):
            if v not in prev:
                prev[v] = u
                if v == dst:
                    path = [v]
                    while prev[path[-1]] is not None:
                        path.append(prev[path[-1]])
                    return path[::-1]
                q.append(v)
    return None


_Base = getattr(app_manager, "RyuApp", None) or getattr(app_manager, "OSKenApp")


class RerouteController(_Base):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dps = {}      # dpid -> datapath
        self.links = {}    # (src_dpid, dst_dpid) -> port on src
        self.hosts = {}    # mac -> (dpid, port)
        self.ip2mac = {}   # ip -> mac
        self.paths = {}    # (src_mac, dst_mac) -> [dpid, ...]
        self.log_event(f"CONTROLLER_START reroute={'ON' if REROUTE else 'OFF (static baseline)'}")

    # ------------------------------------------------------------ logging
    def log_event(self, msg):
        t = time.time()
        line = f"{time.strftime('%H:%M:%S', time.localtime(t))}.{int(t * 1000) % 1000:03d} {msg}"
        print(line, flush=True)
        try:
            with open(LOG_FILE, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass

    # ------------------------------------------------------- switch setup
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def features_handler(self, ev):
        dp = ev.msg.datapath
        ofp, p = dp.ofproto, dp.ofproto_parser
        self.dps[dp.id] = dp
        self.add_flow(dp, 0, p.OFPMatch(),
                      [p.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)])
        self.log_event(f"SWITCH_CONNECTED s{dp.id}")

    def add_flow(self, dp, priority, match, actions):
        ofp, p = dp.ofproto, dp.ofproto_parser
        inst = [p.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(p.OFPFlowMod(datapath=dp, priority=priority, match=match, instructions=inst))

    def del_flow(self, dpid, smac, dmac):
        dp = self.dps.get(dpid)
        if dp is None:
            return
        ofp, p = dp.ofproto, dp.ofproto_parser
        dp.send_msg(p.OFPFlowMod(datapath=dp, command=ofp.OFPFC_DELETE, out_port=ofp.OFPP_ANY,
                                 out_group=ofp.OFPG_ANY,
                                 match=p.OFPMatch(eth_src=smac, eth_dst=dmac)))

    def packet_out(self, dp, in_port, ports, data):
        ofp, p = dp.ofproto, dp.ofproto_parser
        dp.send_msg(p.OFPPacketOut(datapath=dp, buffer_id=ofp.OFP_NO_BUFFER, in_port=in_port,
                                   actions=[p.OFPActionOutput(x) for x in ports], data=data))

    # ------------------------------------------------- topology events
    @set_ev_cls(event.EventLinkAdd)
    def link_add(self, ev):
        l = ev.link
        self.links[(l.src.dpid, l.dst.dpid)] = l.src.port_no
        self.log_event(f"LINK_UP s{l.src.dpid}->s{l.dst.dpid} (port {l.src.port_no})")
        if REROUTE:
            self.recompute_all("link_up")

    @set_ev_cls(event.EventLinkDelete)
    def link_delete(self, ev):
        l = ev.link
        self.links.pop((l.src.dpid, l.dst.dpid), None)
        self.log_event(f"LINK_DOWN s{l.src.dpid}->s{l.dst.dpid} detected")
        if REROUTE:
            self.recompute_all("link_down", time.time())

    # ------------------------------------------------------- routing
    def hop_ports(self, path, dst_loc):
        out = []
        for i, dpid in enumerate(path):
            port = self.links[(dpid, path[i + 1])] if i < len(path) - 1 else dst_loc[1]
            out.append((dpid, port))
        return out

    def route(self, smac, dmac, reason="new", t0=None):
        s, d = self.hosts.get(smac), self.hosts.get(dmac)
        if not s or not d:
            return None
        path = shortest_path(self.links, s[0], d[0])
        old = self.paths.get((smac, dmac))
        if path is None:
            if old is not None:
                for dpid in old:
                    self.del_flow(dpid, smac, dmac)
                del self.paths[(smac, dmac)]
                self.log_event(f"NO_PATH {smac}->{dmac} (flows removed)")
            return None
        if path == old:
            return path
        for dpid, port in self.hop_ports(path, d):
            dp = self.dps.get(dpid)
            if dp is not None:
                self.add_flow(dp, PRIORITY,
                              dp.ofproto_parser.OFPMatch(eth_src=smac, eth_dst=dmac),
                              [dp.ofproto_parser.OFPActionOutput(port)])
        for dpid in (old or []):
            if dpid not in path:
                self.del_flow(dpid, smac, dmac)
        self.paths[(smac, dmac)] = path
        txt = "->".join(f"s{x}" for x in path)
        took = f" elapsed_ms={(time.time() - t0) * 1000:.1f}" if t0 else ""
        tag = "PATH_INSTALLED" if old is None else "REROUTE_INSTALLED"
        self.log_event(f"{tag} {smac}->{dmac} path={txt} reason={reason}{took}")
        try:
            with open(PATH_FILE, "w") as f:
                f.write(txt + "\n")
        except OSError:
            pass
        return path

    def recompute_all(self, reason, t0=None):
        for (smac, dmac) in list(self.paths):
            self.route(smac, dmac, reason, t0)

    # ------------------------------------------------------ packet-in
    def inter_ports(self):
        return {(a, port) for (a, _b), port in self.links.items()}

    def flood_edge(self, src_dp, in_port, data):
        """Send out of host-facing ports only (never inter-switch ports) -> loop-free."""
        inter = self.inter_ports()
        for sw in get_switch(self, None):
            dp = sw.dp
            ports = [pt.port_no for pt in sw.ports
                     if pt.port_no < dp.ofproto.OFPP_MAX and pt.is_live()
                     and (dp.id, pt.port_no) not in inter
                     and not (dp.id == src_dp.id and pt.port_no == in_port)]
            if ports:
                self.packet_out(dp, dp.ofproto.OFPP_CONTROLLER, ports, data)

    def arp_reply(self, dp, in_port, target_mac, target_ip, req_mac, req_ip):
        e = ethernet.ethernet(dst=req_mac, src=target_mac, ethertype=ether_types.ETH_TYPE_ARP)
        a = arp.arp(opcode=arp.ARP_REPLY, src_mac=target_mac, src_ip=target_ip,
                    dst_mac=req_mac, dst_ip=req_ip)
        pk = packet.Packet()
        pk.add_protocol(e)
        pk.add_protocol(a)
        pk.serialize()
        self.packet_out(dp, dp.ofproto.OFPP_CONTROLLER, [in_port], pk.data)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in(self, ev):
        msg = ev.msg
        dp = msg.datapath
        in_port = msg.match["in_port"]
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype in (ether_types.ETH_TYPE_LLDP, ether_types.ETH_TYPE_IPV6):
            return
        src, dst = eth.src, eth.dst
        if (dp.id, in_port) in self.inter_ports():
            return                                   # only learn / forward from host-facing ports
        if int(dst[:2], 16) & 1 and dst != BCAST:
            return                                   # ignore multicast noise
        if not self.links and len(self.dps) > 1:
            return                                   # topology not discovered yet: never flood blind

        self.hosts[src] = (dp.id, in_port)
        a = pkt.get_protocol(arp.arp)
        if a:
            self.ip2mac[a.src_ip] = src
            if a.opcode == arp.ARP_REQUEST and a.dst_ip in self.ip2mac:
                self.arp_reply(dp, in_port, self.ip2mac[a.dst_ip], a.dst_ip, src, a.src_ip)
                return

        if dst in self.hosts:
            path = self.route(src, dst)
            self.route(dst, src)
            if path:
                for dpid, port in self.hop_ports(path, self.hosts[dst]):
                    if dpid == dp.id:
                        self.packet_out(dp, in_port, [port], msg.data)
            return
        self.flood_edge(dp, in_port, msg.data)
