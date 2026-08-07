"""Turn a packet capture into the per-second Network/Router table from the
metrics sheet (black-box guide, Part 17.1).

Usage:
    python tools/pcap_windows.py capture.pcapng 192.168.137.42
    python tools/pcap_windows.py capture.pcapng 192.168.137.42 -o data/router

Inputs: a pcap/pcapng file and the phone's IP address on the capture network.
Outputs (next to the capture unless -o gives a prefix):
    <prefix>_windows.csv  one row per wall-clock second:
        uplink/downlink Mbps and packet rates, packet-size p50/p95,
        uplink inter-arrival p50/p95/max, active_flows, new_connections,
        conn_resets, tcp_retx
    <prefix>_flows.csv    one row per 5-tuple flow: first/last seen, packets,
        bytes each way (feeds the connection-replacement event type)

Windows are absolute epoch seconds, and epoch_window_start_ms is emitted so
these rows line up with the app CSVs, the controller event log, and the viewer
recordings on one shared timeline.

Needs tshark (installs with Wireshark). The traffic is encrypted, so this reads
sizes, timing, and flow behavior only. Per the guide: call the result the
phone-uplink traffic rate, never the encoder bitrate, and tcp_retx counts only
TCP - QUIC's recovery is invisible and must not be claimed.
"""

import argparse
import csv
import os
import statistics
import subprocess
import sys

TSHARK_CANDIDATES = [
    "tshark",
    r"C:\Program Files\Wireshark\tshark.exe",
    r"C:\Program Files (x86)\Wireshark\tshark.exe",
]

FIELDS = [
    "frame.time_epoch", "ip.src", "ip.dst", "frame.len", "ip.proto",
    "tcp.srcport", "tcp.dstport", "udp.srcport", "udp.dstport",
    "tcp.flags.syn", "tcp.flags.ack", "tcp.flags.reset",
    "tcp.analysis.retransmission",
]


def find_tshark():
    for cand in TSHARK_CANDIDATES:
        try:
            subprocess.run([cand, "--version"], capture_output=True, check=True)
            return cand
        except (OSError, subprocess.CalledProcessError):
            continue
    sys.exit("tshark not found. Install Wireshark (wireshark.org) and rerun.")


def run_tshark(tshark, pcap, phone_ip):
    cmd = [tshark, "-r", pcap, "-Y", "ip.addr==%s" % phone_ip,
           "-T", "fields", "-E", "separator=/t"]
    for f in FIELDS:
        cmd += ["-e", f]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit("tshark failed: %s" % proc.stderr.strip()[:400])
    return proc.stdout.splitlines()


def pct(values, p):
    if not values:
        return ""
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(p / 100.0 * len(s) + 0.5)) - 1))
    return s[k]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pcap")
    ap.add_argument("phone_ip")
    ap.add_argument("-o", "--out-prefix", default=None,
                    help="output prefix (default: capture path without extension)")
    args = ap.parse_args()

    prefix = args.out_prefix or os.path.splitext(args.pcap)[0]
    tshark = find_tshark()
    lines = run_tshark(tshark, args.pcap, args.phone_ip)
    if not lines:
        sys.exit("no packets matched %s - wrong phone IP?" % args.phone_ip)

    windows = {}   # epoch second -> accumulators
    flows = {}     # 5-tuple -> stats
    seen_flows = set()
    last_up_time = None

    def wslot(sec):
        if sec not in windows:
            windows[sec] = {
                "up_bytes": 0, "down_bytes": 0, "up_pkts": 0, "down_pkts": 0,
                "up_sizes": [], "inter_arrival_ms": [], "flows": set(),
                "new_connections": 0, "conn_resets": 0, "tcp_retx": 0,
            }
        return windows[sec]

    for line in lines:
        parts = line.split("\t")
        if len(parts) < len(FIELDS) or not parts[0] or not parts[1]:
            continue
        (t_epoch, src, dst, flen, proto, tsp, tdp, usp, udp_p,
         syn, ack, rst, retx) = parts[:13]
        try:
            t = float(t_epoch)
            size = int(flen)
        except ValueError:
            continue
        sec = int(t)
        w = wslot(sec)
        up = src == args.phone_ip

        sport = tsp or usp
        dport = tdp or udp_p
        # one key per conversation regardless of direction
        a = (src, sport)
        b = (dst, dport)
        key = (proto,) + (a + b if a <= b else b + a)

        if key not in flows:
            flows[key] = {"first": t, "last": t, "pkts": 0,
                          "up_bytes": 0, "down_bytes": 0}
        fl = flows[key]
        fl["last"] = t
        fl["pkts"] += 1

        w["flows"].add(key)
        if key not in seen_flows:
            seen_flows.add(key)
            w["new_connections"] += 1
        # a SYN without ACK is an explicit fresh TCP handshake
        elif syn == "1" and ack != "1":
            w["new_connections"] += 1

        if rst == "1":
            w["conn_resets"] += 1
        if retx and retx != "0":
            w["tcp_retx"] += 1

        if up:
            w["up_bytes"] += size
            w["up_pkts"] += 1
            w["up_sizes"].append(size)
            fl["up_bytes"] += size
            if last_up_time is not None:
                w["inter_arrival_ms"].append((t - last_up_time) * 1000.0)
            last_up_time = t
        else:
            w["down_bytes"] += size
            w["down_pkts"] += 1
            fl["down_bytes"] += size

    win_path = prefix + "_windows.csv"
    with open(win_path, "w", newline="") as f:
        wcsv = csv.writer(f)
        wcsv.writerow([
            "window_epoch_s", "epoch_window_start_ms",
            "uplink_mbps", "downlink_mbps", "uplink_pkt_rate", "downlink_pkt_rate",
            "pkt_size_p50", "pkt_size_p95",
            "inter_arrival_p50_ms", "inter_arrival_p95_ms", "inter_arrival_max_ms",
            "active_flows", "new_connections", "conn_resets", "tcp_retx",
        ])
        for sec in sorted(windows):
            w = windows[sec]
            ia = w["inter_arrival_ms"]
            wcsv.writerow([
                sec, sec * 1000,
                round(w["up_bytes"] * 8 / 1e6, 4), round(w["down_bytes"] * 8 / 1e6, 4),
                w["up_pkts"], w["down_pkts"],
                pct(w["up_sizes"], 50), pct(w["up_sizes"], 95),
                round(pct(ia, 50), 1) if ia else "",
                round(pct(ia, 95), 1) if ia else "",
                round(max(ia), 1) if ia else "",
                len(w["flows"]), w["new_connections"], w["conn_resets"], w["tcp_retx"],
            ])

    flow_path = prefix + "_flows.csv"
    with open(flow_path, "w", newline="") as f:
        wcsv = csv.writer(f)
        wcsv.writerow(["proto", "endpoint_a", "port_a", "endpoint_b", "port_b",
                       "first_epoch_s", "last_epoch_s", "duration_s",
                       "packets", "up_bytes", "down_bytes"])
        for key, fl in sorted(flows.items(), key=lambda kv: -kv[1]["pkts"]):
            proto, a_ip, a_port, b_ip, b_port = key
            wcsv.writerow([proto, a_ip, a_port, b_ip, b_port,
                           round(fl["first"], 3), round(fl["last"], 3),
                           round(fl["last"] - fl["first"], 1),
                           fl["pkts"], fl["up_bytes"], fl["down_bytes"]])

    secs = sorted(windows)
    up_rates = [windows[s]["up_bytes"] * 8 / 1e6 for s in secs]
    print("windows: %d seconds, flows: %d" % (len(secs), len(flows)))
    if up_rates:
        print("uplink Mbps: median %.2f  p95 %.2f  max %.2f"
              % (statistics.median(up_rates), pct(up_rates, 95), max(up_rates)))
    print("wrote %s" % win_path)
    print("wrote %s" % flow_path)
    print("reminder: this is phone-uplink traffic rate, not encoder bitrate")
    return 0


if __name__ == "__main__":
    sys.exit(main())
