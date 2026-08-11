"""Merge one trial's files into a single per-second table, and apply the
attribution rules (black-box guide, Parts 17 and 18).

Usage:
    python tools/blackbox/join_trial.py data/blackbox/gen1_rep01 \
        --pcap-windows data/blackbox/gen1_rep01_windows.csv \
        --viewer-a data/blackbox/gen1_rep01_viewerA_viewer_windows.csv \
        --events data/trials/gen1_rep01/gen1_rep01_events.csv

Everything joins on wall-clock epoch milliseconds, which every tool already
stamps: the pcap parser from packet timestamps, the viewer analyzer from
--start-epoch, the controller from the event log. That shared clock is the only
reason these files can be lined up at all.

Outputs <prefix>_joined.csv, one row per second of the trial:
    trial_phase, uplink/downlink rates, packet stats, flow behaviour,
    viewer fps/gaps/freezes for each viewer, and an attribution verdict.

The attribution column applies the guide's Part 18 rules mechanically. It is a
heuristic, not proof of Meta's internals - the wording is deliberately
"consistent with", and every verdict names what was observed.
"""

import argparse
import csv
import os
import statistics
import sys

FREEZE_MS = 500
# a viewer second counts as degraded if it froze or its fps fell well below the
# trial's own healthy baseline, rather than against an assumed nominal rate
DEGRADED_FPS_FRACTION = 0.6
# uplink is "down" when it drops below this fraction of the baseline median
UPLINK_DROP_FRACTION = 0.7


def read_csv(path):
    if not path or not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def num(row, key):
    v = (row or {}).get(key, "")
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def phases_from_events(events):
    """Build [(start_ms, end_ms, phase)] from the controller's phase events."""
    marks = []
    for r in events:
        if r.get("event") == "phase":
            marks.append((int(r["unix_time_ms"]), r.get("detail", "")))
        elif r.get("event") == "trial_end":
            marks.append((int(r["unix_time_ms"]), None))
    spans = []
    for i, (t, name) in enumerate(marks):
        if name is None:
            continue
        end = marks[i + 1][0] if i + 1 < len(marks) else None
        spans.append((t, end, name))
    return spans


def phase_at(spans, epoch_ms):
    for start, end, name in spans:
        if epoch_ms >= start and (end is None or epoch_ms < end):
            return name
    return ""


def attribute(up_mbps, up_baseline, congestion, viewers_degraded, viewers_total):
    """Guide Part 18, stated as observations rather than causes."""
    if viewers_total == 0 or viewers_degraded == 0:
        return "ok"
    if viewers_total > 1 and viewers_degraded < viewers_total:
        return "one viewer only, consistent with a viewer-side or downstream event"
    # every viewer we have degraded at once
    if up_mbps is None or up_baseline is None:
        return "viewers degraded, no uplink reference"
    dropped = up_mbps < up_baseline * UPLINK_DROP_FRACTION
    if dropped and congestion:
        return "uplink down with congestion signs, consistent with an uplink bottleneck"
    if dropped and not congestion:
        return "uplink down cleanly, consistent with upstream sending less"
    return "uplink steady, consistent with a problem before the phone"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out_prefix")
    ap.add_argument("--pcap-windows", required=True)
    ap.add_argument("--viewer-a")
    ap.add_argument("--viewer-b")
    ap.add_argument("--events")
    args = ap.parse_args()

    pcap = read_csv(args.pcap_windows)
    if not pcap:
        sys.exit("no pcap windows found at %s" % args.pcap_windows)
    va = {int(float(r["epoch_window_start_ms"])): r
          for r in read_csv(args.viewer_a) if r.get("epoch_window_start_ms")}
    vb = {int(float(r["epoch_window_start_ms"])): r
          for r in read_csv(args.viewer_b) if r.get("epoch_window_start_ms")}
    events = read_csv(args.events)
    spans = phases_from_events(events)

    if (args.viewer_a and not va) or (args.viewer_b and not vb):
        print("note: a viewer file had no epoch column - rerun viewer_analyze.py"
              " with --start-epoch so it can be joined")

    # baselines from the BASELINE phase where we have one, else the whole trial
    def baseline_median(values):
        vals = [v for v in values if v is not None]
        return statistics.median(vals) if vals else None

    base_rows = [r for r in pcap
                 if not spans or phase_at(spans, int(float(r["epoch_window_start_ms"]))) == "BASELINE"]
    up_baseline = baseline_median([num(r, "uplink_mbps") for r in base_rows]) \
        or baseline_median([num(r, "uplink_mbps") for r in pcap])

    viewer_baselines = {}
    for tag, table in (("A", va), ("B", vb)):
        if not table:
            continue
        rows = [r for ms, r in table.items()
                if not spans or phase_at(spans, ms) == "BASELINE"] or list(table.values())
        viewer_baselines[tag] = baseline_median([num(r, "viewer_fps") for r in rows])

    out_path = args.out_prefix + "_joined.csv"
    cols = ["epoch_ms", "trial_phase", "uplink_mbps", "downlink_mbps",
            "uplink_pkt_rate", "pkt_size_p50", "inter_arrival_p95_ms",
            "active_flows", "new_connections", "conn_resets", "tcp_retx",
            "viewerA_fps", "viewerA_freezes", "viewerA_gap_max_ms",
            "viewerB_fps", "viewerB_freezes", "viewerB_gap_max_ms",
            "attribution"]

    n_degraded = 0
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in pcap:
            ms = int(float(r["epoch_window_start_ms"]))
            a, b = va.get(ms), vb.get(ms)
            up = num(r, "uplink_mbps")
            # congestion signs: retransmits, resets, or a stretched packet tail
            ia95 = num(r, "inter_arrival_p95_ms") or 0
            congestion = ((num(r, "tcp_retx") or 0) > 0
                          or (num(r, "conn_resets") or 0) > 0
                          or ia95 > 200)

            degraded = 0
            total = 0
            for tag, row in (("A", a), ("B", b)):
                if row is None:
                    continue
                total += 1
                fps = num(row, "viewer_fps")
                base = viewer_baselines.get(tag)
                froze = (num(row, "freeze_count") or 0) > 0
                slow = (fps is not None and base and fps < base * DEGRADED_FPS_FRACTION)
                if froze or slow:
                    degraded += 1
            verdict = attribute(up, up_baseline, congestion, degraded, total)
            if degraded:
                n_degraded += 1

            w.writerow({
                "epoch_ms": ms,
                "trial_phase": phase_at(spans, ms),
                "uplink_mbps": r.get("uplink_mbps", ""),
                "downlink_mbps": r.get("downlink_mbps", ""),
                "uplink_pkt_rate": r.get("uplink_pkt_rate", ""),
                "pkt_size_p50": r.get("pkt_size_p50", ""),
                "inter_arrival_p95_ms": r.get("inter_arrival_p95_ms", ""),
                "active_flows": r.get("active_flows", ""),
                "new_connections": r.get("new_connections", ""),
                "conn_resets": r.get("conn_resets", ""),
                "tcp_retx": r.get("tcp_retx", ""),
                "viewerA_fps": (a or {}).get("viewer_fps", ""),
                "viewerA_freezes": (a or {}).get("freeze_count", ""),
                "viewerA_gap_max_ms": (a or {}).get("gap_max_ms", ""),
                "viewerB_fps": (b or {}).get("viewer_fps", ""),
                "viewerB_freezes": (b or {}).get("freeze_count", ""),
                "viewerB_gap_max_ms": (b or {}).get("gap_max_ms", ""),
                "attribution": verdict,
            })

    print("joined %d seconds" % len(pcap))
    print("phases: %s" % (", ".join(n for _, _, n in spans) or "none (no event log)"))
    print("uplink baseline: %s Mbps"
          % (round(up_baseline, 2) if up_baseline else "n/a"))
    for tag, base in viewer_baselines.items():
        print("viewer %s baseline fps: %s" % (tag, round(base, 1) if base else "n/a"))
    print("seconds with a degraded viewer: %d" % n_degraded)
    print("wrote %s" % out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
