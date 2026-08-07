"""Fold every session's CSVs in a folder into single master tables.

Usage:
    python tools/merge_sessions.py data/

Reads every trial_/frames_/events_ CSV (grouped by the timestamp in the
filename) and writes:

    trials_all.csv   one row per session: the conditions + session stats
    windows_all.csv  one row per second across ALL sessions, with the trial
                     conditions stamped onto every row (her per-window shape)

The per-session files on the phone stay raw and untouched; these master files
can be regenerated any time. Old sessions from before trial files existed get
blank condition columns.
"""

import csv
import glob
import os
import statistics
import sys

WINDOW_MS = 1000
LARGE_GAP_MS = 500

TRIAL_COLS = [
    "trial_id", "device", "local_transport", "quality", "frame_rate",
    "phone_position", "motion_condition", "network_limit", "epoch_start_ms",
]


def load_trials_index(folder):
    """The phone appends every session to one trials.csv, keyed by session_stamp."""
    path = os.path.join(folder, "trials.csv")
    index = {}
    if os.path.exists(path):
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                index[r.get("session_stamp", "")] = {c: r.get(c, "") for c in TRIAL_COLS}
    return index


def read_trial(folder, stamp, index):
    if stamp in index:
        return index[stamp]
    # fall back to the old one-file-per-session format
    path = os.path.join(folder, "trial_%s.csv" % stamp)
    if not os.path.exists(path):
        return {c: "" for c in TRIAL_COLS}
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return {c: "" for c in TRIAL_COLS}
    return {c: rows[0].get(c, "") for c in TRIAL_COLS}


def read_frames(path):
    with open(path, newline="") as f:
        return [
            {
                "t": int(r["timestamp_ms"]),
                "w": int(r["width"]),
                "h": int(r["height"]),
                "gap": int(r["gap_ms"]) if r["gap_ms"] else None,
            }
            for r in csv.DictReader(f)
        ]


def read_display(path):
    """Frames that actually reached the screen, written by the presentation queue."""
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return [
            {"t": int(r["timestamp_ms"]), "gap": int(r["gap_ms"]) if r["gap_ms"] else None}
            for r in csv.DictReader(f)
        ]


def parse_detail(detail):
    out = {}
    for part in detail.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k] = v
    return out


def read_events(path):
    """Fold the recovery and battery events into per-session summary columns."""
    summary = {
        "startup_ms": "", "startup_attempts": "", "retries": 0, "failures": 0,
        "recoveries": 0, "aborted": 0, "battery_start": "", "battery_end": "",
    }
    if not os.path.exists(path):
        return summary
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            kind = r.get("type", "")
            d = parse_detail(r.get("detail", ""))
            if kind == "startup":
                summary["startup_ms"] = d.get("startup_ms", "")
                summary["startup_attempts"] = d.get("attempts", "")
            elif kind == "retry":
                summary["retries"] += 1
            elif kind == "failure":
                summary["failures"] += 1
            elif kind == "recovery":
                summary["recoveries"] += 1
            elif kind == "abort":
                summary["aborted"] = 1
            elif kind == "battery":
                p = d.get("percent", "")
                if summary["battery_start"] == "":
                    summary["battery_start"] = p
                summary["battery_end"] = p
    return summary


VIEWER_BLANK = {
    "viewer_fps": "", "viewer_gap_p95": "", "viewer_gap_max": "",
    "viewer_freezes": "", "viewer_freeze_ms": "",
}


def display_windows(display, last_t):
    """Per-second viewer-side stats; a display gap over 500 ms is her freeze definition."""
    per = {}
    for start in range(0, last_t + 1, WINDOW_MS):
        inside = [f for f in display if start <= f["t"] < start + WINDOW_MS]
        gaps = [f["gap"] for f in inside if f["gap"] is not None]
        freezes = [g for g in gaps if g > LARGE_GAP_MS]
        per[start] = {
            "viewer_fps": len(inside),
            "viewer_gap_p95": pct(gaps, 95),
            "viewer_gap_max": max(gaps) if gaps else "",
            "viewer_freezes": len(freezes),
            "viewer_freeze_ms": sum(freezes) if freezes else 0,
        }
    return per


def pct(values, p):
    if not values:
        return ""
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(p / 100.0 * len(s) + 0.5)) - 1))
    return s[k]


def windows(frames):
    if not frames:
        return []
    out = []
    for start in range(0, frames[-1]["t"] + 1, WINDOW_MS):
        inside = [f for f in frames if start <= f["t"] < start + WINDOW_MS]
        gaps = [f["gap"] for f in inside if f["gap"] is not None]
        out.append(
            {
                "window_start_ms": start,
                "input_fps": len(inside),
                "input_width": inside[0]["w"] if inside else "",
                "input_height": inside[0]["h"] if inside else "",
                "input_gap_p95": pct(gaps, 95),
                "input_gap_max": max(gaps) if gaps else "",
                "large_gaps": sum(1 for g in gaps if g > LARGE_GAP_MS),
            }
        )
    return out


def main():
    folder = sys.argv[1] if len(sys.argv) > 1 else "data"
    frame_files = sorted(glob.glob(os.path.join(folder, "frames_*.csv")))
    if not frame_files:
        print("no frames_*.csv found in %s" % folder)
        return 1

    trials_index = load_trials_index(folder)
    trial_rows = []
    window_rows = []
    for frames_path in frame_files:
        stamp = os.path.basename(frames_path)[len("frames_"):-len(".csv")]
        trial = read_trial(folder, stamp, trials_index)
        frames = read_frames(frames_path)
        display = read_display(os.path.join(folder, "display_%s.csv" % stamp))
        recovery = read_events(os.path.join(folder, "events_%s.csv" % stamp))

        gaps = [f["gap"] for f in frames if f["gap"] is not None]
        duration_ms = frames[-1]["t"] if frames else 0
        trial_rows.append(
            dict(
                trial,
                session_stamp=stamp,
                frames=len(frames),
                duration_ms=duration_ms,
                avg_fps=round(len(frames) * 1000.0 / duration_ms, 2) if duration_ms else "",
                gap_mean_ms=round(statistics.mean(gaps), 1) if gaps else "",
                gap_max_ms=max(gaps) if gaps else "",
                display_frames=len(display) if display else "",
                **recovery,
            )
        )

        epoch = trial["epoch_start_ms"]
        dwin = display_windows(display, duration_ms) if display else {}
        for w in windows(frames):
            row = dict(trial)
            row["session_stamp"] = stamp
            row.update(w)
            # wall-clock time of each window, so rows line up with other logs
            row["epoch_window_start_ms"] = (
                int(epoch) + w["window_start_ms"] if epoch else ""
            )
            row.update(dwin.get(w["window_start_ms"], VIEWER_BLANK))
            window_rows.append(row)

    trials_out = os.path.join(folder, "trials_all.csv")
    with open(trials_out, "w", newline="") as f:
        cols = TRIAL_COLS + [
            "session_stamp", "frames", "duration_ms", "avg_fps", "gap_mean_ms", "gap_max_ms",
            "display_frames", "startup_ms", "startup_attempts", "retries", "failures",
            "recoveries", "aborted", "battery_start", "battery_end",
        ]
        writer = csv.DictWriter(f, fieldnames=cols)
        writer.writeheader()
        writer.writerows(trial_rows)

    windows_out = os.path.join(folder, "windows_all.csv")
    with open(windows_out, "w", newline="") as f:
        cols = TRIAL_COLS + [
            "session_stamp", "window_start_ms", "epoch_window_start_ms",
            "input_fps", "input_width", "input_height",
            "input_gap_p95", "input_gap_max", "large_gaps",
            "viewer_fps", "viewer_gap_p95", "viewer_gap_max",
            "viewer_freezes", "viewer_freeze_ms",
        ]
        writer = csv.DictWriter(f, fieldnames=cols)
        writer.writeheader()
        writer.writerows(window_rows)

    print("sessions merged: %d" % len(trial_rows))
    print("wrote %s (%d rows)" % (trials_out, len(trial_rows)))
    print("wrote %s (%d rows)" % (windows_out, len(window_rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
