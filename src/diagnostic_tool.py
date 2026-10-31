#!/usr/bin/env python3
"""
diagnostic_tool.py  -  Person 2 (Integration + Report)

Combines:
  * UDP probe results (RTT, loss, jitter)           -> socket side
  * Ryu REST data (switches, links, hosts, ports)   -> SDN side
and produces a correlated diagnostic report:
  topology, candidate paths, inferred ACTIVE path, failed links/paths,
  and a comparison against the previous run (before/after a link failure).

Usage (Ryu must run with ryu.app.ofctl_rest + ryu.app.rest_topology, --observe-links):
  python3 diagnostic_tool.py --ryu 127.0.0.1:8080 --server 10.0.0.3 --src-ip 10.0.0.1 --label "normal"
  # then in Mininet:  link s1 s2 down
  python3 diagnostic_tool.py --ryu 127.0.0.1:8080 --server 10.0.0.3 --src-ip 10.0.0.1 --label "s1-s2 down"

NOTE: the Ryu REST port (8080 by default) must NOT clash with Partner C's mock
controller server (also 8080). Use --ryu host:port if you move one of them.
"""
import argparse
import json
import os
import socket
import statistics
import sys
import time
import urllib.request
from datetime import datetime

BASELINE_FILE = "topology_baseline.json"
HISTORY_FILE = "diagnostic_history.json"


# ---------------------------------------------------------------- REST helpers
def rest_get(base, path, timeout=3):
    with urllib.request.urlopen(f"http://{base}{path}", timeout=timeout) as r:
        return json.loads(r.read().decode())


def hex_int(v):
    """Ryu topology API returns dpid/port as hex strings, stats API as ints."""
    return int(v, 16) if isinstance(v, str) else int(v)


def discover_topology(base):
    switches = sorted(int(s) for s in rest_get(base, "/stats/switches"))
    raw_links = rest_get(base, "/v1.0/topology/links")
    try:
        hosts = rest_get(base, "/v1.0/topology/hosts")
    except Exception:
        hosts = []

    link_port = {}  # (src_dpid, dst_dpid) -> egress port on src_dpid
    for l in raw_links:
        a, b = hex_int(l["src"]["dpid"]), hex_int(l["dst"]["dpid"])
        link_port[(a, b)] = hex_int(l["src"]["port_no"])

    host_map = {}  # ip -> (dpid, port)
    for h in hosts:
        for ip in h.get("ipv4", []):
            host_map[ip] = (hex_int(h["port"]["dpid"]), hex_int(h["port"]["port_no"]))

    return {"switches": switches, "link_port": link_port, "hosts": host_map}


def port_tx_snapshot(base, switches):
    snap = {}
    for dpid in switches:
        try:
            data = rest_get(base, f"/stats/port/{dpid}")
        except Exception:
            continue
        for p in data.get(str(dpid), []):
            if isinstance(p.get("port_no"), int):
                snap[(dpid, p["port_no"])] = p.get("tx_packets", 0)
    return snap


# -------------------------------------------------------------- path analysis
def find_all_paths(link_port, src, dst):
    adj = {}
    for (a, b) in link_port:
        adj.setdefault(a, set()).add(b)
    paths = []

    def dfs(node, path):
        if node == dst:
            paths.append(list(path))
            return
        for nxt in sorted(adj.get(node, [])):
            if nxt not in path:
                path.append(nxt)
                dfs(nxt, path)
                path.pop()

    dfs(src, [src])
    return sorted(paths, key=len)


def infer_active_path(paths, link_port, before, after, sent):
    """Active path = candidate whose egress ports all saw the most new TX packets."""
    best, best_score = None, 0
    for p in paths:
        deltas = []
        for a, b in zip(p, p[1:]):
            port = link_port.get((a, b))
            deltas.append(after.get((a, port), 0) - before.get((a, port), 0))
        score = min(deltas) if deltas else 0
        if score > best_score:
            best, best_score = p, score
    return best if best_score >= max(1, sent // 2) else None


def path_str(p):
    return " -> ".join(f"s{d}" for d in p) if p else "N/A"


# ------------------------------------------------------------------ UDP probes
def run_probes(server, port, n, timeout, interval):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    rtts, results = [], []
    for seq in range(1, n + 1):
        t0 = time.time()
        try:
            s.sendto(json.dumps({"seq": seq, "time": t0}).encode(), (server, port))
            data, _ = s.recvfrom(1024)
            if json.loads(data.decode()).get("seq") != seq:
                raise socket.timeout
            rtt = (time.time() - t0) * 1000
            rtts.append(rtt)
            results.append((seq, rtt))
            print(f"  probe {seq:2d}: RTT = {rtt:.2f} ms")
        except (socket.timeout, ValueError):
            results.append((seq, None))
            print(f"  probe {seq:2d}: TIMEOUT (lost)")
        time.sleep(interval)
    s.close()
    lost = n - len(rtts)
    jitter = (statistics.mean(abs(a - b) for a, b in zip(rtts, rtts[1:]))
              if len(rtts) > 1 else 0.0)
    return {
        "sent": n, "received": len(rtts), "lost": lost,
        "loss_pct": lost / n * 100,
        "avg_rtt": statistics.mean(rtts) if rtts else 0.0,
        "min_rtt": min(rtts) if rtts else 0.0,
        "max_rtt": max(rtts) if rtts else 0.0,
        "jitter": jitter,
    }


# ------------------------------------------- probe results read from a file/URL
KEYMAP = {
    "loss_pct": ["loss_pct", "packet_loss_pct", "packet_loss", "loss", "loss_percent"],
    "avg_rtt": ["avg_rtt", "avg_rtt_ms", "rtt_avg", "average_rtt", "mean_rtt", "rtt_ms", "rtt"],
    "min_rtt": ["min_rtt", "min_rtt_ms", "rtt_min"],
    "max_rtt": ["max_rtt", "max_rtt_ms", "rtt_max"],
    "jitter": ["jitter", "jitter_ms"],
    "sent": ["sent", "packets_sent", "probes_sent", "total"],
    "received": ["received", "packets_received", "recv"],
}


def _pick(d, names):
    for n in names:
        if n in d and d[n] is not None:
            return d[n]
    return None


def load_probe_file(src):
    """Read results written by the UDP client (run inside h1). src = path or http URL.
    Accepts a summary dict (several key spellings) or a per-probe list."""
    if src.startswith("http"):
        raw = urllib.request.urlopen(src, timeout=3).read().decode()
    else:
        raw = open(src).read()
        age = time.time() - os.path.getmtime(src)
        if age > 600:
            print(f"[!] WARNING: {src} is {age / 60:.0f} min old - are these results stale?")
    d = json.loads(raw)
    if isinstance(d, list):
        d = {"probes": d}
    items = next((d[k] for k in ("probes", "results", "samples") if isinstance(d.get(k), list)), None)
    if items:
        rtts = []
        for it in items:
            r = _pick(it, ["rtt_ms", "rtt"])
            if r is not None and not it.get("lost") and "time" not in str(it.get("status", "")).lower():
                rtts.append(float(r))
        n = len(items)
        d = dict(d, sent=n, received=len(rtts))
        if rtts:
            d.update(avg_rtt=statistics.mean(rtts), min_rtt=min(rtts), max_rtt=max(rtts),
                     jitter=(statistics.mean(abs(x - y) for x, y in zip(rtts, rtts[1:]))
                             if len(rtts) > 1 else 0.0))
    m = {k: _pick(d, v) for k, v in KEYMAP.items()}
    if m["sent"] is not None and m["received"] is not None and m["loss_pct"] is None:
        m["loss_pct"] = (m["sent"] - m["received"]) / m["sent"] * 100
    if m["loss_pct"] is None:
        raise ValueError(f"no loss field found; keys in file: {sorted(d.keys())}")
    if m["sent"] is not None and m["received"] is None:
        m["received"] = round(m["sent"] * (1 - m["loss_pct"] / 100))
    for k in ("avg_rtt", "min_rtt", "max_rtt", "jitter"):
        m[k] = float(m[k]) if m[k] is not None else 0.0
    m["loss_pct"] = float(m["loss_pct"])
    m["lost"] = (m["sent"] - m["received"]) if m["sent"] is not None and m["received"] is not None else None
    return m


# --------------------------------------------------------------------- helpers
def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def save_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)


def link_key(a, b):
    return f"{min(a, b)}-{max(a, b)}"


# ------------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ryu", default="127.0.0.1:8080", help="Ryu REST host:port")
    ap.add_argument("--server", default="10.0.0.2", help="UDP echo server IP (h2)")
    ap.add_argument("--udp-port", type=int, default=5005)
    ap.add_argument("--src-ip", default="10.0.0.1", help="probe client host IP")
    ap.add_argument("--src-dpid", type=int, help="override: ingress switch id")
    ap.add_argument("--dst-dpid", type=int, help="override: egress switch id")
    ap.add_argument("--probes", type=int, default=10)
    ap.add_argument("--timeout", type=float, default=2.0)
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument("--loss-threshold", type=float, default=20.0)
    ap.add_argument("--label", default="run")
    ap.add_argument("--reset-baseline", action="store_true")
    ap.add_argument("--baseline-file", default=BASELINE_FILE,
                    help="use a different file per topology (single vs multipath)")
    ap.add_argument("--from-file", help="read probe results written by the client in h1 "
                    "(path or http URL) instead of probing from here")
    ap.add_argument("--wait", type=float, default=0,
                    help="with --from-file: seconds to wait (while probes run in h1) between "
                         "the two port-counter snapshots; 0 = prompt for Enter")
    ap.add_argument("--json-out", help="also write the final report as JSON")
    a = ap.parse_args()

    print("=" * 62)
    print(" NETWORK DIAGNOSTIC & PATH ANALYSIS REPORT")
    print(f" {datetime.now():%Y-%m-%d %H:%M:%S}   label: {a.label}")
    print("=" * 62)

    # 1. SDN topology discovery
    try:
        topo = discover_topology(a.ryu)
    except Exception as e:
        print(f"[!] Cannot reach Ryu REST API at {a.ryu}: {e}")
        print("    Is Ryu running with ofctl_rest + rest_topology --observe-links?")
        return
    switches, link_port = topo["switches"], topo["link_port"]
    live_links = {link_key(x, y) for (x, y) in link_port}

    print(f"\n[1] SDN TOPOLOGY (from Ryu)")
    print(f"    Switches : {', '.join('s%d' % s for s in switches)}")
    for k in sorted(live_links):
        x, y = map(int, k.split("-"))
        print(f"    Link     : s{x} <-> s{y}  (ports {link_port.get((x, y))}/{link_port.get((y, x))})")
    print(f"    Hosts    : {topo['hosts'] or 'none learned yet (run pingall first)'}")

    # 2. Baseline (known-good) topology -> detect failed links
    base = load_json(a.baseline_file)
    if base is None or a.reset_baseline:
        save_json(a.baseline_file, sorted(live_links))
        base = sorted(live_links)
        print(f"    (baseline topology saved to {a.baseline_file})")
    failed_links = sorted(set(base) - live_links)

    # 3. Candidate paths
    src_dpid = a.src_dpid or topo["hosts"].get(a.src_ip, (None,))[0]
    dst_dpid = a.dst_dpid or topo["hosts"].get(a.server, (None,))[0]
    if src_dpid is None or dst_dpid is None:
        print("\n[!] Host->switch mapping unknown. Run 'pingall' in Mininet or pass "
              "--src-dpid / --dst-dpid.")
        return

    # paths over the BASELINE graph so failed paths can be identified
    full_ports = dict(link_port)
    for k in failed_links:
        x, y = map(int, k.split("-"))
        full_ports.setdefault((x, y), 0)
        full_ports.setdefault((y, x), 0)
    all_paths = find_all_paths(full_ports, src_dpid, dst_dpid)

    print(f"\n[2] PATH ANALYSIS  (s{src_dpid} -> s{dst_dpid})")
    path_status = {}
    for p in all_paths:
        broken = [link_key(x, y) for x, y in zip(p, p[1:]) if link_key(x, y) in failed_links]
        path_status[tuple(p)] = "FAILED" if broken else "UP"
        print(f"    {path_str(p):<28} hops={len(p) - 1}  [{path_status[tuple(p)]}]")

    # 4. UDP probes with port-counter snapshots around them
    if a.from_file:
        print(f"\n[3] PROBE RESULTS from {a.from_file}")
        before = port_tx_snapshot(a.ryu, switches)
        if a.wait > 0:
            print(f"    Run the probes in h1 now ({a.wait:.0f}s window)...")
            time.sleep(a.wait)
        elif sys.stdin.isatty():
            input("    Run the probes in h1 NOW, then press Enter here... ")
        after = port_tx_snapshot(a.ryu, switches)
        try:
            m = load_probe_file(a.from_file)
        except Exception as e:
            print(f"[!] Could not read probe results: {e}")
            return
    else:
        print(f"\n[3] UDP PROBES -> {a.server}:{a.udp_port}")
        before = port_tx_snapshot(a.ryu, switches)
        m = run_probes(a.server, a.udp_port, a.probes, a.timeout, a.interval)
        after = port_tx_snapshot(a.ryu, switches)

    live_paths = [list(p) for p, st in path_status.items() if st == "UP"]
    active = infer_active_path(live_paths, link_port, before, after, m["received"] or 10)

    # 5. Correlation + verdict
    status = "DEGRADED" if m["loss_pct"] > a.loss_threshold else "HEALTHY"
    print("\n[4] MEASUREMENTS")
    na = lambda v: "n/a" if v is None else v
    print(f"    Sent/Recv/Lost : {na(m['sent'])}/{na(m['received'])}/{na(m['lost'])}")
    print(f"    Packet loss    : {m['loss_pct']:.1f}%")
    print(f"    RTT min/avg/max: {m['min_rtt']:.2f} / {m['avg_rtt']:.2f} / {m['max_rtt']:.2f} ms")
    print(f"    Jitter         : {m['jitter']:.2f} ms")
    print(f"    Status         : {status}")

    print("\n[5] CORRELATION (application <-> network)")
    print(f"    Active path (from port counters): {path_str(active)}")
    if failed_links:
        print("    FAILED LINKS   : " + ", ".join(
            f"s{k.split('-')[0]}<->s{k.split('-')[1]}" for k in failed_links))
    else:
        print("    FAILED LINKS   : none")
    failed_paths = [path_str(list(p)) for p, st in path_status.items() if st == "FAILED"]
    print(f"    FAILED PATHS   : {', '.join(failed_paths) if failed_paths else 'none'}")
    if status == "DEGRADED" and failed_links:
        print("    >> Loss is explained by a missing link in the SDN topology.")
    elif status == "DEGRADED":
        print("    >> Loss but all links up: suspect congestion / flow rules / "
              "netem loss on the active path.")
    if live_paths and (status == "DEGRADED" or failed_links):
        alt = min(live_paths, key=len)
        print(f"    Suggested reroute: {path_str(alt)}")

    # 6. Compare with previous run
    hist = load_json(HISTORY_FILE) or []
    print("\n[6] COMPARISON WITH PREVIOUS RUN")
    if hist:
        p = hist[-1]
        print(f"    previous '{p['label']}': loss {p['loss_pct']:.1f}%  avg RTT {p['avg_rtt']:.2f} ms  "
              f"path {p['active_path']}")
        print(f"    current  '{a.label}': loss {m['loss_pct']:.1f}%  avg RTT {m['avg_rtt']:.2f} ms  "
              f"path {path_str(active)}")
        print(f"    delta: loss {m['loss_pct'] - p['loss_pct']:+.1f}%  "
              f"RTT {m['avg_rtt'] - p['avg_rtt']:+.2f} ms")
    else:
        print("    (first run - nothing to compare)")
    hist.append({"time": datetime.now().isoformat(), "label": a.label,
                 "loss_pct": m["loss_pct"], "avg_rtt": m["avg_rtt"],
                 "active_path": path_str(active), "failed_links": failed_links,
                 "status": status})
    save_json(HISTORY_FILE, hist)
    if a.json_out:
        save_json(a.json_out, {"label": a.label, "measurements": m, "active_path": path_str(active),
                               "failed_links": failed_links, "failed_paths": failed_paths,
                               "status": status})
    print("\n" + "=" * 62)


if __name__ == "__main__":
    main()
