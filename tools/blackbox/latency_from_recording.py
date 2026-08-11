"""Read the source display's time bar out of a viewer recording and produce a
live_latency time series (metrics sheet: live_latency, Gen 1 method).

Usage, normally just:
    python tools/blackbox/latency_from_recording.py rec.mp4

It finds the time bar in the frame by itself and takes the recording's start
time from the file's own metadata, so no measuring or note-taking is needed.
Override either if the auto-detection is wrong:
    --bar-crop x,y,w,h --start-epoch 1786047238.5

The source display page draws 30 cells: a white sync cell, a black sync cell,
27 bits of milliseconds-since-midnight, and an even parity cell. This decodes
those cells frame by frame, so latency comes from the video itself rather than
from reading digits by eye.

    latency = (when this frame was recorded) - (time encoded inside the frame)

If the recording shows both the live page and the video playing it, both bars
decode; the tool keeps the one that is behind, because that is the one that
travelled through the pipeline.

Outputs:
    <rec>_latency_samples.csv   one row per decoded frame
    <rec>_latency_windows.csv   one row per second: median/min/max latency

Frames whose parity or sync cells fail are skipped rather than guessed, so a
blurry or half-drawn frame cannot poison the series.
"""

import argparse
import csv
import datetime
import os
import subprocess
import sys

try:
    import numpy as np
except ImportError:
    sys.exit("numpy needed: python -m pip install numpy")

BITS = 14
CELLS = BITS + 3
STEP_MS = 5                     # payload resolution
WRAP_MS = 60000                 # payload wraps every minute
MASK = 0b10101010101010         # keeps the bar busy; see the source display page
DEC_W, DEC_H = 480, 24          # decode strip size once the bar is located
SCAN_W, SCAN_H = 1920, 1080     # hunt at full size; the bar can be small on screen
MS_PER_DAY = 86400000
MAX_SANE_LATENCY_MS = 55000     # must stay inside one wrap of the payload


def ffprobe(path, entries):
    return subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", entries, "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True).stdout.strip()


def ffprobe_fps(path):
    out = ffprobe(path, "stream=r_frame_rate")
    if "/" in out:
        num, den = out.split("/")
        return float(num) / float(den) if float(den) else 30.0
    return float(out or 30.0)


def duration_s(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def auto_start_epoch(path):
    """Recorders stamp a creation time; fall back to file mtime minus duration."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format_tags=creation_time",
         "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True).stdout.strip()
    if out:
        try:
            txt = out.replace("Z", "+00:00")
            dt = datetime.datetime.fromisoformat(txt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt.timestamp(), "file metadata"
        except ValueError:
            pass
    mtime = os.path.getmtime(path)
    return mtime - duration_s(path), "file mtime minus duration"


def grab_frame(path, at_s, w, h):
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", "%.3f" % at_s, "-i", path,
         "-frames:v", "1", "-vf", "scale=%d:%d" % (w, h),
         "-pix_fmt", "gray", "-f", "rawvideo", "-"],
        capture_output=True)
    buf = np.frombuffer(proc.stdout, dtype=np.uint8)
    if buf.size < w * h:
        return None
    return buf[: w * h].reshape(h, w)


def read_bar_frames(path, crop, rotated=False):
    x, y, w, h = crop
    # transpose=0 is ffmpeg's equivalent of a numpy .T, so a crop measured on a
    # transposed frame lines up after this filter
    pre = "transpose=0," if rotated else ""
    vf = (pre + "crop=iw*%f:ih*%f:iw*%f:ih*%f,scale=%d:%d"
          % (w, h, x, y, DEC_W, DEC_H))
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-vf", vf,
         "-pix_fmt", "gray", "-f", "rawvideo", "-"],
        capture_output=True)
    if proc.returncode != 0:
        sys.exit("ffmpeg failed: %s" % proc.stderr.decode()[:300])
    buf = np.frombuffer(proc.stdout, dtype=np.uint8)
    n = len(buf) // (DEC_W * DEC_H)
    if n == 0:
        sys.exit("no frames decoded - check the file and --bar-crop")
    return buf[: n * DEC_W * DEC_H].reshape(n, DEC_H, DEC_W)


def decode_vals(vals):
    """Turn 30 cell brightnesses into a timestamp, or None if they fail checks."""
    white_ref, black_ref = vals[0], vals[1]
    if white_ref - black_ref < 25:
        return None
    mid = (white_ref + black_ref) / 2.0
    bits = (vals > mid).astype(int)
    if bits[0] != 1 or bits[1] != 0:
        return None
    payload = bits[2:2 + BITS]
    if payload.sum() % 2 != bits[CELLS - 1]:
        return None
    value = 0
    for b in payload:
        value = (value << 1) | int(b)
    ms = (value ^ MASK) * STEP_MS      # undo the run-breaking scramble
    return ms if 0 <= ms < WRAP_MS else None


def sample_cells(band, left, cell):
    """Average the middle 60% of each cell, using sub-pixel boundaries."""
    vals = np.empty(CELLS)
    for i in range(CELLS):
        a = left + i * cell
        lo = int(np.ceil(a + 0.2 * cell))
        hi = int(np.floor(a + 0.8 * cell))
        if hi <= lo:
            lo, hi = int(a), int(a + cell)
        if lo < 0 or hi > len(band) or hi <= lo:
            return None
        vals[i] = band[lo:hi].mean()
    return vals


def decode_band(band, left, cell):
    vals = sample_cells(band, left, cell)
    return None if vals is None else decode_vals(vals)


def decode_profile(col):
    """Decode a profile that spans exactly the bar and nothing else."""
    if len(col) < CELLS * 2:
        return None
    return decode_band(col, 0.0, len(col) / float(CELLS))


def fit_grid(edges, left0, cell0):
    """Least-squares fit of left and cell width to the observed edges.

    Every edge sits on a cell boundary, so estimating the width from a single
    cell and multiplying by 30 amplifies one rounding error thirtyfold. Fitting
    all the edges at once gives sub-pixel geometry instead.
    """
    ks, xs = [], []
    for e in edges:
        k = (e - left0) / cell0
        if -0.5 <= k <= CELLS + 0.5 and abs(k - round(k)) < 0.25:
            ks.append(round(k))
            xs.append(e)
    if len(ks) < 4:
        return left0, cell0
    ks = np.array(ks, dtype=float)
    xs = np.array(xs, dtype=float)
    cell, left = np.polyfit(ks, xs, 1)
    if cell <= 1:
        return left0, cell0
    return float(left), float(cell)


def decode_frame(frame):
    return decode_profile(frame.mean(axis=0))


def band_from_crop(frame, crop):
    h, w = frame.shape
    x, y, bw, bh = crop
    x0, y0 = int(x * w), int(y * h)
    x1, y1 = x0 + max(1, int(bw * w)), y0 + max(1, int(bh * h))
    if x1 > w or y1 > h:
        return None
    return frame[y0:y1].mean(axis=0)[x0:x1]


def scan_candidates(frame):
    """Every (crop, decoded_ms) in this frame that passes sync and parity.

    The bar advertises its own geometry: cell 0 is always white and cell 1 is
    always black, so the first white run is exactly one cell wide. That gives
    the left edge and the cell size directly, instead of brute-forcing a grid
    that never lands on the cell boundaries when the bar is small on screen.
    """
    h, w = frame.shape
    strip_h = max(6, h // 90)
    found = []
    seen = set()
    for y in range(0, h - strip_h, max(3, strip_h // 2)):
        band = frame[y:y + strip_h].mean(axis=0)
        lo, hi = float(band.min()), float(band.max())
        if hi - lo < 50:
            continue
        bits = (band > (lo + hi) / 2.0).astype(np.int8)
        edges = np.flatnonzero(np.diff(bits)) + 1
        if len(edges) < 6:
            continue
        # try the first few rising edges as "start of cell 0"
        for i in range(min(10, len(edges) - 1)):
            left0 = float(edges[i])
            cell0 = float(edges[i + 1]) - left0
            if cell0 < 3:
                continue
            left, cell = fit_grid(edges, left0, cell0)
            bw = CELLS * cell
            if bw < 90 or left < 0 or left + bw > w:
                continue
            key = (y // strip_h, int(left) // 2, int(bw) // 2)
            if key in seen:
                continue
            seen.add(key)
            ms = decode_band(band, left, cell)
            if ms is None:
                continue
            # grow the strip to the bar's real height; a strip that only clips
            # the bar's edge decodes here but misses entirely when re-cropped
            top, bot = y, y + strip_h
            while top - 2 >= 0 and decode_band(
                    frame[top - 2:bot].mean(axis=0), left, cell) == ms:
                top -= 2
            while bot + 2 <= h and decode_band(
                    frame[top:bot + 2].mean(axis=0), left, cell) == ms:
                bot += 2
            found.append(((left / w, top / h, bw / w, (bot - top) / float(h)), ms))
    return found


def autodetect_bar(path, start_epoch, dur, probes=6):
    """Find the bar without being fooled by a lucky misread.

    Sync and parity alone let the odd garbage decode through, so a candidate has
    to prove its time advances in step with wall time. Verification is done
    against neighbouring frames rather than distant ones, because a stream may
    only be playing during part of a recording. Of the survivors we keep the
    most delayed, because that is the copy that came through the pipeline rather
    than the live page sitting next to it.
    """
    span = max(2.0, dur)
    times = [span * (i + 1) / (probes + 1.0) for i in range(probes)]
    best = None
    for t in times:
        raw = grab_frame(path, t, SCAN_W, SCAN_H)
        if raw is None:
            continue
        # a phone filmed in the other orientation arrives rotated, so the bar is
        # a vertical strip; transposing turns it back into a row
        for rotated in (False, True):
            frame = np.ascontiguousarray(raw.T) if rotated else raw
            cands = scan_candidates(frame)
            if not cands:
                continue
            probe_ds = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0)
            checks = []
            for d in probe_ds:
                fr = grab_frame(path, t + d, SCAN_W, SCAN_H)
                checks.append(None if fr is None else
                              (np.ascontiguousarray(fr.T) if rotated else fr))
            for crop, ms0 in cands:
                # a real bar decodes on most probes and advances with wall time;
                # noise decodes rarely and erratically, so score reliability
                agree = 0
                for d, fr in zip(probe_ds, checks):
                    if fr is None:
                        continue
                    band = band_from_crop(fr, crop)
                    if band is None:
                        continue
                    ms = decode_band(band, 0.0, len(band) / float(CELLS))
                    if ms is None:
                        continue
                    drift = ((ms - ms0 - d * 1000.0 + WRAP_MS / 2) % WRAP_MS) - WRAP_MS / 2
                    if abs(drift) <= 300:
                        agree += 1
                # two independent probes advancing correctly is already very
                # unlikely by chance; demanding more loses bars that only decode
                # on about half the frames, which is normal for a filmed screen
                if agree < 2:
                    continue
                latency = (ms_since_midnight(start_epoch + t) - ms0) % WRAP_MS
                if latency > MAX_SANE_LATENCY_MS:
                    continue
                cand = (latency, crop, rotated)
                if best is None or latency > best[0]:
                    best = cand
        if best is not None and best[0] > 500:
            break                              # found a genuinely delayed bar
    return best


def ms_since_midnight(epoch_s):
    lt = datetime.datetime.fromtimestamp(epoch_s)
    return ((lt.hour * 60 + lt.minute) * 60 + lt.second) * 1000 + lt.microsecond // 1000


def pct(values, p):
    if not values:
        return ""
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(p / 100.0 * len(s) + 0.5)) - 1))
    return round(s[k], 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("recording")
    ap.add_argument("--bar-crop", default=None,
                    help="x,y,w,h fractions around the time bar (default: auto)")
    ap.add_argument("--start-epoch", type=float, default=None,
                    help="unix epoch seconds when recording started (default: from file)")
    args = ap.parse_args()

    prefix = os.path.splitext(args.recording)[0]
    fps = ffprobe_fps(args.recording)

    if args.start_epoch is not None:
        start_epoch, epoch_src = args.start_epoch, "given"
    else:
        start_epoch, epoch_src = auto_start_epoch(args.recording)
    print("recording starts %s  (%s)"
          % (datetime.datetime.fromtimestamp(start_epoch).strftime("%H:%M:%S.%f")[:-3],
             epoch_src))

    rotated = False
    if args.bar_crop:
        crop = [float(v) for v in args.bar_crop.split(",")]
        if len(crop) != 4:
            sys.exit("--bar-crop needs four numbers: x,y,w,h")
        print("time bar: using given crop")
    else:
        found = autodetect_bar(args.recording, start_epoch, duration_s(args.recording))
        if not found:
            sys.exit("could not find the time bar automatically.\n"
                     "Check it was visible in the recording, or pass --bar-crop x,y,w,h.")
        latency0, crop, rotated = found
        print("time bar: found at %.2f,%.2f,%.2f,%.2f%s (about %.1f s behind)"
              % (crop[0], crop[1], crop[2], crop[3],
                 " in rotated video" if rotated else "", latency0 / 1000.0))

    frames = read_bar_frames(args.recording, crop, rotated)
    start_ms_midnight = ms_since_midnight(start_epoch)

    samples = []
    decoded = 0
    for i, f in enumerate(frames):
        src_ms = decode_frame(f)
        if src_ms is None:
            continue
        decoded += 1
        frame_ms_midnight = start_ms_midnight + (i / fps) * 1000.0
        latency = (frame_ms_midnight - src_ms) % WRAP_MS
        if latency > MAX_SANE_LATENCY_MS:
            continue
        samples.append({
            "frame_index": i,
            "recorded_ms_since_midnight": round(frame_ms_midnight, 1),
            "source_ms_since_midnight": src_ms,
            "latency_ms": round(latency, 1),
            "epoch_ms": int((start_epoch + i / fps) * 1000),
        })

    if not samples:
        sys.exit("no frames decoded cleanly - was the time bar visible, and is "
                 "the start time right? Try --start-epoch.")

    with open(prefix + "_latency_samples.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(samples[0].keys()))
        w.writeheader()
        w.writerows(samples)

    per_sec = {}
    for s in samples:
        per_sec.setdefault(s["epoch_ms"] // 1000, []).append(s["latency_ms"])
    with open(prefix + "_latency_windows.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["epoch_window_start_ms", "samples", "latency_median_ms",
                    "latency_min_ms", "latency_max_ms"])
        for sec in sorted(per_sec):
            v = per_sec[sec]
            w.writerow([sec * 1000, len(v), pct(v, 50), round(min(v), 1),
                        round(max(v), 1)])

    lat = [s["latency_ms"] for s in samples]
    print("frames: %d, decoded cleanly: %d (%.0f%%)"
          % (len(frames), decoded, 100.0 * decoded / len(frames)))
    print("latency ms: median %s  p05 %s  p95 %s  min %.0f  max %.0f"
          % (pct(lat, 50), pct(lat, 5), pct(lat, 95), min(lat), max(lat)))
    print("wrote %s_latency_samples.csv and %s_latency_windows.csv"
          % (os.path.basename(prefix), os.path.basename(prefix)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
