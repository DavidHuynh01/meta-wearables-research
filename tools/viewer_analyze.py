"""Turn a screen recording of the viewer into the per-second Viewer table from
the metrics sheet (black-box guide, Part 17.2).

Usage:
    python tools/viewer_analyze.py rec.mp4 --crop 0.52,0.10,0.48,0.55
    python tools/viewer_analyze.py rec.mp4 --crop 0.52,0.10,0.48,0.55 --start-epoch 1786047238

--crop x,y,w,h are fractions of the frame (0-1) selecting just the video player
inside the recording, so the desktop around it never counts as motion. Run once
without --crop and check the preview PNG the script writes, then adjust.

Outputs:
    <rec>_viewer_frames.csv   one row per recorded frame: advanced? change score
    <rec>_viewer_windows.csv  one row per second: viewer_fps, gaps p50/p95/max,
                              freeze_count, freeze_ms, first_frame_ms

How it works: the recording has a fixed frame rate, but the STREAM inside it may
advance more slowly or stall. Comparing consecutive frames of the cropped region
tells us when the displayed video actually changed. A run of no-change longer
than the freeze threshold is a freeze (guide: 500 ms, with 300 ms and 1 s
reported as a sensitivity check).

Needs ffmpeg (already used elsewhere in this project) and numpy.
Latency is NOT computed here - that comes from reading the clock digits in the
video, which the screenshot method does directly.
"""

import argparse
import csv
import os
import subprocess
import sys

try:
    import numpy as np
except ImportError:
    sys.exit("numpy needed: python -m pip install numpy")

FREEZE_MS = 500
SENSITIVITY_MS = (300, 1000)
# a frame counts as advanced if its mean absolute pixel change exceeds this;
# tuned for compressed screen recordings where identical frames still wobble
CHANGE_THRESHOLD = 0.8
GRAY_W, GRAY_H = 160, 90


def ffprobe_fps(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", path],
        capture_output=True, text=True).stdout.strip()
    if "/" in out:
        num, den = out.split("/")
        return float(num) / float(den) if float(den) else 30.0
    return float(out or 30.0)


def read_gray_frames(path, crop):
    """Decode to a small grayscale stream so whole-recording diffing is cheap."""
    vf = "scale=%d:%d" % (GRAY_W, GRAY_H)
    if crop:
        x, y, w, h = crop
        vf = ("crop=iw*%f:ih*%f:iw*%f:ih*%f," % (w, h, x, y)) + vf
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-vf", vf,
         "-pix_fmt", "gray", "-f", "rawvideo", "-"],
        capture_output=True)
    if proc.returncode != 0:
        sys.exit("ffmpeg failed: %s" % proc.stderr.decode()[:300])
    buf = np.frombuffer(proc.stdout, dtype=np.uint8)
    n = len(buf) // (GRAY_W * GRAY_H)
    if n == 0:
        sys.exit("no frames decoded - check the file and the crop")
    return buf[: n * GRAY_W * GRAY_H].reshape(n, GRAY_H, GRAY_W)


def pct(values, p):
    if not values:
        return ""
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(p / 100.0 * len(s) + 0.5)) - 1))
    return round(s[k], 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("recording")
    ap.add_argument("--crop", default=None,
                    help="x,y,w,h as fractions of the frame, e.g. 0.52,0.10,0.48,0.55")
    ap.add_argument("--start-epoch", type=float, default=None,
                    help="wall-clock epoch seconds when the recording started, "
                         "so windows line up with the pcap and app logs")
    ap.add_argument("--threshold", type=float, default=CHANGE_THRESHOLD)
    args = ap.parse_args()

    crop = None
    if args.crop:
        try:
            crop = [float(v) for v in args.crop.split(",")]
            assert len(crop) == 4
        except (ValueError, AssertionError):
            sys.exit("--crop needs four numbers: x,y,w,h")

    fps = ffprobe_fps(args.recording)
    frames = read_gray_frames(args.recording, crop)
    frame_ms = 1000.0 / fps
    print("recording: %d frames at %.2f fps (%.1f s)"
          % (len(frames), fps, len(frames) / fps))

    # save a preview so the crop can be eyeballed rather than guessed
    prefix = os.path.splitext(args.recording)[0]
    try:
        from PIL import Image
        mid = frames[len(frames) // 2]
        Image.fromarray(mid).resize((GRAY_W * 4, GRAY_H * 4)).save(prefix + "_crop_preview.png")
        print("crop preview: %s_crop_preview.png" % os.path.basename(prefix))
    except ImportError:
        pass

    diffs = np.abs(frames[1:].astype(np.int16) - frames[:-1].astype(np.int16))
    scores = diffs.mean(axis=(1, 2))
    advanced = scores > args.threshold

    rows = []
    first_advance_ms = ""
    last_advance_ms = None
    gaps = []
    for i, adv in enumerate(advanced):
        t_ms = (i + 1) * frame_ms
        if adv:
            if first_advance_ms == "":
                first_advance_ms = round(t_ms, 1)
            if last_advance_ms is not None:
                gaps.append((t_ms - last_advance_ms, last_advance_ms))
            last_advance_ms = t_ms
        rows.append({"frame_index": i + 1, "timestamp_ms": round(t_ms, 1),
                     "change_score": round(float(scores[i]), 3),
                     "advanced": int(bool(adv))})

    with open(prefix + "_viewer_frames.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["frame_index", "timestamp_ms",
                                          "change_score", "advanced"])
        w.writeheader()
        w.writerows(rows)

    total_ms = len(frames) * frame_ms
    per_sec = {}
    for gap_ms, at_ms in gaps:
        sec = int(at_ms // 1000)
        per_sec.setdefault(sec, {"gaps": [], "advances": 0})
        per_sec[sec]["gaps"].append(gap_ms)
    for i, adv in enumerate(advanced):
        if adv:
            sec = int(((i + 1) * frame_ms) // 1000)
            per_sec.setdefault(sec, {"gaps": [], "advances": 0})
            per_sec[sec]["advances"] += 1

    with open(prefix + "_viewer_windows.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["window_s", "epoch_window_start_ms", "viewer_fps",
                    "gap_p50_ms", "gap_p95_ms", "gap_max_ms",
                    "freeze_count", "freeze_ms", "first_frame_ms"])
        for sec in range(int(total_ms // 1000) + 1):
            d = per_sec.get(sec, {"gaps": [], "advances": 0})
            fz = [g for g in d["gaps"] if g > FREEZE_MS]
            epoch_ms = (int(args.start_epoch * 1000) + sec * 1000
                        if args.start_epoch else "")
            w.writerow([sec, epoch_ms, d["advances"],
                        pct(d["gaps"], 50), pct(d["gaps"], 95),
                        round(max(d["gaps"]), 1) if d["gaps"] else "",
                        len(fz), round(sum(fz), 1) if fz else 0,
                        first_advance_ms if sec == 0 else ""])

    all_gaps = [g for g, _ in gaps]
    freezes = [g for g in all_gaps if g > FREEZE_MS]
    print("viewer fps (mean): %.1f" % (int(advanced.sum()) / (total_ms / 1000.0)))
    print("first frame advance: %s ms" % first_advance_ms)
    if all_gaps:
        print("gaps ms: p50 %s  p95 %s  max %s"
              % (pct(all_gaps, 50), pct(all_gaps, 95), pct(all_gaps, 100)))
    print("freezes over %d ms: %d (total %.0f ms)"
          % (FREEZE_MS, len(freezes), sum(freezes)))
    for alt in SENSITIVITY_MS:
        alt_f = [g for g in all_gaps if g > alt]
        print("  sensitivity at %d ms: %d freezes" % (alt, len(alt_f)))
    print("wrote %s_viewer_frames.csv and %s_viewer_windows.csv"
          % (os.path.basename(prefix), os.path.basename(prefix)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
