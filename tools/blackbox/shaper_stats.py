"""Sample the traffic shaper's queue once per second during a trial
(black-box guide, Part 2 Step 8; metrics sheet row shaper_queue_stats).

Usage, on the Linux router, started before the trial and stopped after:
    python3 tools/blackbox/shaper_stats.py --iface eth0 \
        -o data/blackbox/gen1_rep01_shaper.csv

Writes one row per second:
    epoch_window_start_ms, backlog_bytes, backlog_pkts, drops, overlimits,
    requeues, sent_bytes, sent_pkts, rate_bps

Why it matters: the pcap says how much traffic left the phone, but not whether
the shaper was holding traffic back. Backlog and drops rising is the difference
between "the sender sent less" and "the cap would not let it through" - which is
exactly the ambiguity the attribution rules have to resolve.

Runs `tc -s class show dev <iface>` each second and parses the counters. On any
non-Linux machine it prints what it would run and exits, so the command can be
rehearsed without the lab rig.
"""

import argparse
import csv
import os
import platform
import re
import subprocess
import sys
import time

# tc -s class show output looks like:
#   class htb 1:10 root prio 0 rate 1400Kbit ceil 1400Kbit burst 1600b cburst 1600b
#    Sent 1234567 bytes 890 pkt (dropped 12, overlimits 34 requeues 0)
#    rate 1350Kbit 120pps backlog 4321b 3p requeues 0
SENT_RE = re.compile(r"Sent (\d+) bytes (\d+) pkt \(dropped (\d+), overlimits (\d+) requeues (\d+)\)")
BACKLOG_RE = re.compile(r"backlog (\d+)([bKMG]?)b? (\d+)p")
RATE_RE = re.compile(r"\brate (\d+)([KMG]?)bit")

MULT = {"": 1, "K": 1000, "M": 1000000, "G": 1000000000}


def scale(value, suffix):
    return int(value) * MULT.get(suffix.upper(), 1)


def sample(iface, classid):
    out = subprocess.run(["tc", "-s", "class", "show", "dev", iface],
                         capture_output=True, text=True)
    if out.returncode != 0:
        return None
    text = out.stdout
    if classid:
        # keep only the block for the shaped class, up to the next class line
        blocks = re.split(r"\nclass ", text)
        text = ""
        for b in blocks:
            if b.startswith("htb %s " % classid) or (" %s " % classid) in b.split("\n")[0]:
                text = b
                break
        if not text:
            return None

    row = {"backlog_bytes": "", "backlog_pkts": "", "drops": "", "overlimits": "",
           "requeues": "", "sent_bytes": "", "sent_pkts": "", "rate_bps": ""}
    m = SENT_RE.search(text)
    if m:
        row["sent_bytes"], row["sent_pkts"] = m.group(1), m.group(2)
        row["drops"], row["overlimits"], row["requeues"] = m.group(3), m.group(4), m.group(5)
    m = BACKLOG_RE.search(text)
    if m:
        row["backlog_bytes"] = scale(m.group(1), m.group(2))
        row["backlog_pkts"] = m.group(3)
    m = RATE_RE.search(text)
    if m:
        row["rate_bps"] = scale(m.group(1), m.group(2))
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iface", default="eth0")
    ap.add_argument("--classid", default="1:10",
                    help="htb class carrying the phone's traffic (blank for all)")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--interval", type=float, default=1.0)
    args = ap.parse_args()

    if platform.system() != "Linux":
        print("not Linux - would run: tc -s class show dev %s" % args.iface)
        print("start this on the router before the trial, Ctrl+C after")
        return 0

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    cols = ["epoch_window_start_ms", "backlog_bytes", "backlog_pkts", "drops",
            "overlimits", "requeues", "sent_bytes", "sent_pkts", "rate_bps"]
    n = 0
    print("sampling %s class %s every %.1fs -> %s (Ctrl+C to stop)"
          % (args.iface, args.classid or "all", args.interval, args.out))
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        try:
            while True:
                tick = time.time()
                row = sample(args.iface, args.classid)
                if row is None:
                    print("no shaper found on %s - is tc configured?" % args.iface)
                    break
                row["epoch_window_start_ms"] = int(tick * 1000)
                w.writerow(row)
                f.flush()   # survive a Ctrl+C or a crash mid-trial
                n += 1
                # drift-free: sleep to the next whole interval boundary
                time.sleep(max(0.0, args.interval - (time.time() - tick)))
        except KeyboardInterrupt:
            pass
    print("wrote %d samples to %s" % (n, args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
