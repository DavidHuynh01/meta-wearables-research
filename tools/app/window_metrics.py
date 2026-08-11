"""Turn a session's frames/events CSVs into the per-second window table the lab's
metrics sheet asks for, plus a summary of the metrics we can already derive.

Usage:
    python tools/app/window_metrics.py data/app/frames_20260715_150134.csv

The events and encoded CSVs are found automatically by swapping "frames_" for
"events_" and "encoded_". Writes windows_<stamp>.csv next to the input and prints
a summary.

Encoder columns describe the phone-side encoder this project added, not the one on
the glasses, which is sealed. Sessions logged before that encoder existed simply
have no encoded_ file and those columns come out blank.

Still not covered: anything needing a media server or a remote viewer.
"""

import csv
import os
import statistics
import sys

WINDOW_MS = 1000
LARGE_GAP_MS = 500  # the lab sheet's threshold for flagging a gap


def read_frames(path):
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows.append(
                {
                    "index": int(r["frame_index"]),
                    "t": int(r["timestamp_ms"]),
                    "w": int(r["width"]),
                    "h": int(r["height"]),
                    # the first frame has no previous frame, so no gap
                    "gap": int(r["gap_ms"]) if r["gap_ms"] else None,
                }
            )
    return rows


def read_events(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return [
            {"t": int(r["timestamp_ms"]), "type": r["type"], "detail": r["detail"]}
            for r in csv.DictReader(f)
        ]


def read_encoded(path):
    """One row per frame our own encoder produced. Absent for older sessions."""
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows.append(
                {
                    "t": int(r["timestamp_ms"]),
                    "size": int(r["size_bytes"]),
                    "key": r["keyframe"] == "1",
                }
            )
    return rows


def pct(values, p):
    """Nearest-rank percentile, so it works on small windows without interpolation."""
    if not values:
        return ""
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(p / 100.0 * len(s) + 0.5)) - 1))
    return s[k]


def windows(frames, encoded):
    if not frames:
        return []
    last = frames[-1]["t"]
    if encoded:
        last = max(last, encoded[-1]["t"])
    out = []
    for start in range(0, last + 1, WINDOW_MS):
        end = start + WINDOW_MS
        inside = [f for f in frames if start <= f["t"] < end]
        gaps = [f["gap"] for f in inside if f["gap"] is not None]
        enc = [e for e in encoded if start <= e["t"] < end]
        enc_bytes = sum(e["size"] for e in enc)
        out.append(
            {
                "window_start_ms": start,
                "input_fps": len(inside),
                "input_gap_p95": pct(gaps, 95),
                "input_gap_max": max(gaps) if gaps else "",
                "input_width": inside[0]["w"] if inside else "",
                "input_height": inside[0]["h"] if inside else "",
                "large_gaps": sum(1 for g in gaps if g > LARGE_GAP_MS),
                # blank rather than zero when there is no encoder log at all, so a
                # session that predates the encoder is not read as one that
                # encoded nothing
                "encoded_fps": len(enc) if encoded else "",
                # a one-second window means bytes*8 is already bits per second
                "encoded_bitrate_kbps": round(enc_bytes * 8 / 1000.0, 1) if encoded else "",
                "encoded_frame_size_mean": round(enc_bytes / len(enc)) if enc else "",
                "encoded_frame_size_max": max((e["size"] for e in enc), default="") if encoded else "",
                "keyframes": sum(1 for e in enc if e["key"]) if encoded else "",
            }
        )
    return out


def summarize(frames, events, encoded):
    gaps = [f["gap"] for f in frames if f["gap"] is not None]
    duration = frames[-1]["t"] if frames else 0

    # startup_time is session_start to the first STREAMING state
    streaming = next(
        (e["t"] for e in events if e["type"] == "stream_state" and e["detail"] == "STREAMING"),
        None,
    )
    first_frame = frames[0]["t"] if frames else None
    resolutions = [e["detail"] for e in events if e["type"] == "resolution"]

    print("session")
    print("  duration: %d ms" % duration)
    print("  frames: %d" % len(frames))
    if duration:
        print("  input_fps (session avg): %.2f" % (len(frames) * 1000.0 / duration))
    print("  resolutions seen: %s" % (", ".join(resolutions) or "none logged"))
    print("  resolution changes: %d" % max(0, len(resolutions) - 1))

    print("recovery")
    print("  startup_time: %s" % ("%d ms" % streaming if streaming is not None else "no STREAMING event"))
    print("  first_frame_latency: %s" % ("%d ms" % first_frame if first_frame is not None else "no frames"))

    if encoded:
        sizes = [e["size"] for e in encoded]
        keys = [e for e in encoded if e["key"]]
        total = sum(sizes)
        print("encoder (phone side, not the glasses)")
        print("  encoded_fps (session avg): %.2f" % (len(encoded) * 1000.0 / duration) if duration else "")
        print("  encoded_bitrate: %.0f kbps mean" % (total * 8.0 / duration) if duration else "")
        print("  encoded_frame_size: mean %d, p95 %s, max %d bytes"
              % (total / len(sizes), pct(sizes, 95), max(sizes)))
        print("  keyframes: %d of %d frames" % (len(keys), len(encoded)))
        # keyframes are ~10x an inter frame, so their share of the bytes says how
        # much of the bitrate is being spent on recovery points rather than motion
        if keys:
            key_bytes = sum(e["size"] for e in keys)
            print("  keyframe share of bytes: %.0f%%" % (100.0 * key_bytes / total))
        # what we asked the codec for vs what it granted; they routinely differ
        fmt = next((e["detail"] for e in events if e["type"] == "encoder_format"), None)
        if fmt:
            print("  target_bitrate: %s" % fmt)
        drop = next((e["detail"] for e in events if e["type"] == "encoder_summary"), None)
        if drop:
            print("  encoder summary: %s" % drop)
    else:
        print("encoder")
        print("  no encoded_ file: this session predates the phone-side encoder")

    if gaps:
        print("gaps (input side)")
        print("  mean: %.1f ms" % statistics.mean(gaps))
        print("  median: %d ms" % statistics.median(gaps))
        print("  p95: %s ms" % pct(gaps, 95))
        print("  p99: %s ms" % pct(gaps, 99))
        print("  max: %d ms" % max(gaps))
        print("  over %d ms: %d of %d frames" % (LARGE_GAP_MS, sum(1 for g in gaps if g > LARGE_GAP_MS), len(gaps)))
        print("jitter")
        print("  stdev of gaps: %.1f ms" % statistics.pstdev(gaps))
        # mean absolute change between consecutive gaps, the RFC 3550 idea of jitter
        deltas = [abs(b - a) for a, b in zip(gaps, gaps[1:])]
        if deltas:
            print("  mean |gap change|: %.1f ms" % statistics.mean(deltas))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    frames_path = sys.argv[1]
    events_path = frames_path.replace("frames_", "events_")
    encoded_path = frames_path.replace("frames_", "encoded_")

    frames = read_frames(frames_path)
    events = read_events(events_path)
    encoded = read_encoded(encoded_path)
    if not frames:
        print("no frames in %s" % frames_path)
        return 1

    summarize(frames, events, encoded)

    rows = windows(frames, encoded)
    out_path = frames_path.replace("frames_", "windows_")
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    print("windows")
    print("  wrote %d rows to %s" % (len(rows), out_path))
    print("  %-16s %-10s %-14s %-14s %s" % ("window_start_ms", "input_fps", "input_gap_p95", "input_gap_max", "resolution"))
    for r in rows[:10]:
        print("  %-16d %-10d %-14s %-14s %sx%s" % (
            r["window_start_ms"], r["input_fps"], r["input_gap_p95"], r["input_gap_max"],
            r["input_width"], r["input_height"]))
    if len(rows) > 10:
        print("  ... %d more" % (len(rows) - 10))
    return 0


if __name__ == "__main__":
    sys.exit(main())
