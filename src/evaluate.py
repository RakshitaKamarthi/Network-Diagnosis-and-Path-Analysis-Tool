#!/usr/bin/env python3
"""
evaluate.py - automated failover experiment (static vs dynamic) -> results.csv (+ charts).

Controller must already be running:
   dynamic:  (default)             ryu-manager ... reroute_controller.py
   static :  REROUTE=0 ryu-manager ... reroute_controller.py
Then:
   sudo python3 evaluate.py --topo multi --mode dynamic --trials 5
   (restart controller with REROUTE=0)  sudo python3 evaluate.py --topo multi --mode static --trials 5
   python3 evaluate.py --plot results.csv

Each trial: probe h1->h2 every 0.2 s, take a link down at --fail-at s, bring it up at --up-at s.
Metrics: loss %, RTT before/after, failover time (link-down -> first successful probe, while the
link is still down). 'recovered=False' means traffic never came back before the link was restored.
"""
import argparse
import csv
import json
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def analyze(probes, t_fail, t_up):
    """probes: list of {t_sent, rtt_ms|None}. Returns metrics dict."""
    sent = len(probes)
    ok = [p for p in probes if p.get("rtt_ms") is not None]
    before = [p["rtt_ms"] for p in ok if p["t_sent"] < t_fail]
    after_ok = [p for p in ok if t_fail <= p["t_sent"] < t_up]
    first = min(after_ok, key=lambda p: p["t_sent"]) if after_ok else None
    return {
        "sent": sent, "lost": sent - len(ok),
        "loss_pct": round((sent - len(ok)) / sent * 100, 2) if sent else 0,
        "rtt_before_ms": round(statistics.mean(before), 2) if before else "",
        "rtt_after_ms": round(statistics.mean(p["rtt_ms"] for p in after_ok), 2) if after_ok else "",
        "failover_s": round(first["t_sent"] - t_fail, 2) if first else "",
        "recovered": bool(first),
    }


def run(a):
    from mininet.log import setLogLevel
    from sdn_topo import TOPOS, build_net
    setLogLevel("warning")
    if os.geteuid() != 0:
        sys.exit("run with sudo")
    net, h1, h2 = build_net(a.topo, a.ctrl_ip, a.ctrl_port)
    fa, fb = a.fail.split(",") if a.fail else TOPOS[a.topo]["fail"]
    net.start()
    time.sleep(10)
    srv = h2.popen(["python3", os.path.join(HERE, "udp_server.py")])
    time.sleep(1)
    if net.ping([h1, h2], timeout="2") > 0:
        net.stop()
        sys.exit("h1 cannot reach h2: is the controller running and links discovered?")
    new = not os.path.exists(a.csv)
    with open(a.csv, "a", newline="") as f:
        w = csv.DictWriter(f, ["topo", "mode", "trial", "link", "sent", "lost", "loss_pct",
                               "rtt_before_ms", "rtt_after_ms", "failover_s", "recovered"])
        if new:
            w.writeheader()
        for trial in range(1, a.trials + 1):
            net.configLinkStatus(fa, fb, "up")
            time.sleep(4)
            out = f"/tmp/eval_{trial}.json"
            count = int((a.up_at + 6) / (a.interval + 0.1))
            proc = h1.popen(["python3", os.path.join(HERE, "udp_client.py"), "10.0.0.2",
                             "--count", str(count), "--interval", str(a.interval),
                             "--timeout", "0.3", "--out", out])
            time.sleep(a.fail_at)
            t_fail = time.time()
            net.configLinkStatus(fa, fb, "down")
            time.sleep(a.up_at - a.fail_at)
            t_up = time.time()
            net.configLinkStatus(fa, fb, "up")
            proc.wait()
            m = analyze(json.load(open(out))["probes"], t_fail, t_up)
            row = {"topo": a.topo, "mode": a.mode, "trial": trial, "link": f"{fa}-{fb}", **m}
            w.writerow(row)
            f.flush()
            print(row)
    srv.terminate()
    net.stop()
    print(f"[*] results appended to {a.csv}")


def plot(csv_path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    rows = list(csv.DictReader(open(csv_path)))
    modes = sorted({r["mode"] for r in rows})
    def avg(mode, key, only_rec=False):
        v = [float(r[key]) for r in rows if r["mode"] == mode and r[key] != "" and (r["recovered"] == "True" or not only_rec)]
        return statistics.mean(v) if v else 0
    fig, ax = plt.subplots(1, 3, figsize=(12, 3.8))
    for i, (key, title, only) in enumerate([("loss_pct", "Packet loss during failure test (%)", False),
                                            ("failover_s", "Failover time (s), recovered trials", True),
                                            ("rtt_after_ms", "RTT after failure (ms)", True)]):
        ax[i].bar(modes, [avg(m, key, only) for m in modes], color=["#4472C4", "#ED7D31"][:len(modes)])
        ax[i].set_title(title, fontsize=10)
    for m in modes:
        n = sum(1 for r in rows if r["mode"] == m)
        rec = sum(1 for r in rows if r["mode"] == m and r["recovered"] == "True")
        print(f"{m}: {rec}/{n} trials recovered before link restore")
    fig.tight_layout()
    fig.savefig("results.png", dpi=150)
    print("[*] chart saved to results.png")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--topo", default="multi")
    ap.add_argument("--mode", choices=["dynamic", "static"], default="dynamic")
    ap.add_argument("--trials", type=int, default=5)
    ap.add_argument("--fail", help="link to fail, e.g. s1,s2 (default: the topology's main-path link)")
    ap.add_argument("--fail-at", type=float, default=4.0)
    ap.add_argument("--up-at", type=float, default=14.0)
    ap.add_argument("--interval", type=float, default=0.2)
    ap.add_argument("--ctrl-ip", default="127.0.0.1")
    ap.add_argument("--ctrl-port", type=int, default=6653)
    ap.add_argument("--csv", default="results.csv")
    ap.add_argument("--plot", metavar="CSV")
    a = ap.parse_args()
    plot(a.plot) if a.plot else run(a)
