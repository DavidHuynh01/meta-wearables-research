"""Run one black-box trial start to finish and write its event log
(black-box guide, Part 6).

Usage:
    python tools/blackbox/trial_controller.py gen1_baseline_lowmotion_rep01
    python tools/blackbox/trial_controller.py gen1_static_uplink_050B_lowmotion_rep03 \
        --baseline 60 --stress 60 --recovery 60 --cap 1.4mbit

The script owns the trial's timeline so every trial runs identically:
it logs trial_start, waits for the operator's stream-start keypress, runs the
BASELINE / STRESS / RECOVERY phases on a timer, and logs trial_end. Times are
unix epoch milliseconds, the same clock the pcap parser, the viewer analyzer
and the phone app all stamp with, so every file from a trial lines up.

Outputs (into --outdir, default data/trials/<trial_id>/):
    <trial_id>_events.csv   trial_id,event,unix_time_ms,detail

Throttling: pass --cap to have the phases apply and remove a tc cap on the
router. On Windows the tc commands are printed rather than run, so the
schedule can be rehearsed without the lab rig; on Linux they execute.
Capture and screen recording are started separately, before this script -
it deliberately does not own them, so a controller crash cannot lose data.
"""

import argparse
import csv
import os
import platform
import subprocess
import sys
import threading
import time

PHASES = ("BASELINE", "STRESS", "RECOVERY")


def now_ms():
    return int(time.time() * 1000)


class TrialLog:
    def __init__(self, path, trial_id):
        self.path = path
        self.trial_id = trial_id
        self.rows = []
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow(["trial_id", "event", "unix_time_ms", "detail"])

    def log(self, event, detail=""):
        t = now_ms()
        self.rows.append((event, t, detail))
        # append immediately so a crash still leaves everything logged so far
        with open(self.path, "a", newline="") as f:
            csv.writer(f).writerow([self.trial_id, event, t, detail])
        stamp = time.strftime("%H:%M:%S", time.localtime(t / 1000))
        print("  [%s] %-22s %s" % (stamp, event, detail))
        return t


def run_tc(args_list, dry_run):
    cmd = " ".join(args_list)
    if dry_run:
        print("       (would run) %s" % cmd)
        return
    subprocess.run(args_list, check=False)


def apply_cap(cap, iface, phone_ip, dry_run):
    run_tc(["tc", "qdisc", "add", "dev", iface, "root", "handle", "1:", "htb",
            "default", "20"], dry_run)
    run_tc(["tc", "class", "add", "dev", iface, "parent", "1:", "classid", "1:10",
            "htb", "rate", cap, "ceil", cap], dry_run)
    run_tc(["tc", "filter", "add", "dev", iface, "protocol", "ip", "parent", "1:",
            "prio", "1", "u32", "match", "ip", "src", phone_ip + "/32",
            "flowid", "1:10"], dry_run)


def clear_cap(iface, dry_run):
    run_tc(["tc", "qdisc", "del", "dev", iface, "root"], dry_run)


def wait_for_key(prompt, log, event, detail=""):
    """Blocking prompt; the operator's keypress is the timestamp we log."""
    print("\n  >>> %s, then press Enter" % prompt)
    input()
    return log.log(event, detail)


def countdown(seconds, label):
    end = time.time() + seconds
    while True:
        left = end - time.time()
        if left <= 0:
            break
        sys.stdout.write("\r       %s: %4.0f s remaining " % (label, left))
        sys.stdout.flush()
        time.sleep(min(1.0, left))
    sys.stdout.write("\r" + " " * 46 + "\r")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("trial_id")
    ap.add_argument("--baseline", type=int, default=60)
    ap.add_argument("--stress", type=int, default=60)
    ap.add_argument("--recovery", type=int, default=60)
    ap.add_argument("--cap", default=None,
                    help="tc rate for the STRESS phase, e.g. 1.4mbit (omit for no throttling)")
    ap.add_argument("--iface", default="eth0", help="router interface facing the internet")
    ap.add_argument("--phone-ip", default="192.168.137.115")
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--notes", default="")
    args = ap.parse_args()

    outdir = args.outdir or os.path.join("data", "trials", args.trial_id)
    os.makedirs(outdir, exist_ok=True)
    log_path = os.path.join(outdir, args.trial_id + "_events.csv")
    dry_run = platform.system() == "Windows"

    total = args.baseline + args.stress + args.recovery
    print("trial: %s" % args.trial_id)
    print("plan:  BASELINE %ds -> STRESS %ds%s -> RECOVERY %ds  (%d s total)"
          % (args.baseline, args.stress,
             (" @ " + args.cap) if args.cap else " (no cap)", args.recovery, total))
    print("log:   %s" % log_path)
    if args.cap and dry_run:
        print("note:  Windows detected, tc commands will be printed not executed")
    print("\nBefore continuing: packet capture running, screen recording running,")
    print("glasses mounted and aimed at the source display, phone in position.")

    log = TrialLog(log_path, args.trial_id)
    log.log("trial_start", args.notes)

    wait_for_key("Start the stream now", log, "stream_request")
    wait_for_key("Press when Viewer A shows video", log, "viewer_A_first_frame")
    wait_for_key("Press when Viewer B shows video (Enter to skip)", log,
                 "viewer_B_first_frame")

    log.log("phase", "BASELINE")
    countdown(args.baseline, "BASELINE")

    if args.cap:
        apply_cap(args.cap, args.iface, args.phone_ip, dry_run)
    log.log("stress_start", args.cap or "no_cap")
    log.log("phase", "STRESS")
    countdown(args.stress, "STRESS")

    if args.cap:
        clear_cap(args.iface, dry_run)
    log.log("stress_end", "cap_removed" if args.cap else "no_cap")
    log.log("phase", "RECOVERY")
    countdown(args.recovery, "RECOVERY")

    wait_for_key("Stop the stream now", log, "stream_end")
    log.log("trial_end")

    print("\ndone. %d events written to %s" % (len(log.rows), log_path))
    print("now: stop the packet capture and the screen recording, and save both")
    print("into %s with the trial id in the filename." % outdir)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted - the events logged so far are already saved")
        sys.exit(1)
