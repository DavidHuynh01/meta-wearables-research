"""Detect a stream resolution switch in a screen recording of the viewer.

Usage:
    python tools/blackbox/resolution_switch.py rec.mp4 --crop 0.52,0.10,0.48,0.55

The sheet asks for viewer_resolution as "displayed video resolution; detect any
runtime switch", with the Gen 1 source given as screen-recording analysis. Reading
the number off a recording is not possible: the player upscales every stream to
fill the same window, so a recording shows the window size whatever the stream is.

The switch is still detectable, and for the same reason. Upscaling stretches
pixels without adding detail, so when the source resolution drops, neighbouring
pixels become interpolated from the same original sample and the finest detail in
the image collapses even though the window never changes size.

What is measured is the ratio

    mean |I[x] - I[x+1]|  /  mean |I[x] - I[x+2]|

the finest gradient against a coarser one. Content changes move both terms
together, so the ratio mostly cancels them out; upscaling suppresses only the
numerator. Around 0.5-0.6 means pixel-level detail is present. A sustained drop
means it is not.

This says a switch is *consistent with* the evidence. It cannot name the
resolutions, and blur, darkness and heavy motion push the ratio the same way, so
a drop that coincides with the scene going dark is not a finding.

Outputs <rec>_sharpness.csv (one row per sampled frame) and prints any steps found.
Needs ffmpeg and numpy.
"""

import argparse
import csv
import os
import subprocess
import sys

try:
    import numpy as np
except ImportError:
    raise SystemExit("numpy needed: python -m pip install numpy")

# Decoded at native resolution on purpose. Downscaling first resamples away the
# interpolation signature this looks for: a 1/3-scale upscale and a native frame
# both come out identical once ffmpeg has scaled them to a common smaller size,
# which is exactly how the first version of this script failed its own test.
# Frames are streamed one at a time rather than loaded together, since a few
# minutes of native-resolution grayscale does not fit in memory.
SAMPLE_FPS = 2
# a step has to hold this long to count, which rejects a single blurred frame
MIN_SEGMENT_S = 2.0
# Fraction the ratio must move to count. Calibrated against synthetic clips: a
# real 3x resolution change moves it about 30%, while ordinary content change
# between two segments of the same stream moves it under 10%.
STEP_FRACTION = 0.15


def probe_size(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", path],
        capture_output=True, text=True).stdout.strip()
    try:
        w, h = (int(v) for v in out.split("x")[:2])
        return w, h
    except ValueError:
        sys.exit("could not read the video size from %s" % path)


def iter_gray(path, crop):
    """Yield native-resolution grayscale frames one at a time."""
    w, h = probe_size(path)
    vf = "fps=%d" % SAMPLE_FPS
    if crop:
        cx, cy, cw, ch = crop
        vf = ("crop=iw*%f:ih*%f:iw*%f:ih*%f," % (cw, ch, cx, cy)) + vf
        w, h = int(w * cw) // 2 * 2, int(h * ch) // 2 * 2
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", path, "-vf", vf,
         "-pix_fmt", "gray", "-f", "rawvideo", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    size = w * h
    n = 0
    while True:
        buf = proc.stdout.read(size)
        if len(buf) < size:
            break
        n += 1
        yield np.frombuffer(buf, dtype=np.uint8).reshape(h, w)
    proc.stdout.close()
    proc.wait()
    if n == 0:
        sys.exit("no frames decoded - check the file and the crop\n%s"
                 % proc.stderr.read().decode()[:300])


def detail_ratio(frame):
    """Finest gradient over a coarser one. Low means the image was upscaled."""
    f = frame.astype(np.int16)
    fine = np.abs(f[:, 1:] - f[:, :-1]).mean() + np.abs(f[1:, :] - f[:-1, :]).mean()
    coarse = np.abs(f[:, 2:] - f[:, :-2]).mean() + np.abs(f[2:, :] - f[:-2, :]).mean()
    # a flat frame has no gradient either way and no detail to judge
    if coarse < 1e-6:
        return 0.0
    return float(fine / coarse)


def find_steps(ratios, min_frames, step_fraction):
    """Points where the median before and after differ by more than step_fraction.

    Comparing medians of whole segments rather than adjacent frames is what makes
    this survive motion: a single fast pan moves one frame, not a segment.
    """
    n = len(ratios)
    m = min_frames
    # Windows sit immediately either side of i. A gap between them would locate
    # the switch worse, not better: with windows touching, the change peaks exactly
    # where the switch is and tapers away from it, which is what makes the peak
    # meaningful.
    change = np.zeros(n)
    for i in range(m, n - m):
        before = float(np.median(ratios[i - m:i]))
        after = float(np.median(ratios[i:i + m]))
        if before > 1e-6:
            change[i] = (after - before) / before

    # Take the largest change first and suppress everything near it, rather than
    # walking left to right. Any window overlapping a switch reads as a partial
    # step, so a first-match scan reports the same switch two or three times.
    steps = []
    remaining = np.abs(change).copy()
    while remaining.max() >= step_fraction:
        i = int(np.argmax(remaining))
        before = float(np.median(ratios[i - m:i]))
        after = float(np.median(ratios[i:i + m]))
        steps.append((i, before, after))
        # the change tapers for m either side of a switch, so suppress twice that
        # before looking for the next one
        remaining[max(0, i - 2 * m):i + 2 * m] = 0.0
    return sorted(steps)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("recording")
    ap.add_argument("--crop", default=None,
                    help="x,y,w,h as fractions, the same crop used for viewer_analyze")
    ap.add_argument("--step", type=float, default=STEP_FRACTION,
                    help="fraction the ratio must move to count as a switch")
    args = ap.parse_args()

    crop = None
    if args.crop:
        crop = [float(v) for v in args.crop.split(",")]
        if len(crop) != 4:
            sys.exit("--crop needs four numbers: x,y,w,h")

    rs, bs = [], []
    for frame in iter_gray(args.recording, crop):
        rs.append(detail_ratio(frame))
        bs.append(float(frame.mean()))
    ratios = np.array(rs)
    brightness = np.array(bs)

    prefix = os.path.splitext(args.recording)[0]
    out = prefix + "_sharpness.csv"
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["sample_index", "time_s", "detail_ratio", "brightness"])
        for i, (r, b) in enumerate(zip(ratios, brightness)):
            w.writerow([i, round(i / float(SAMPLE_FPS), 2), round(r, 4), round(b, 1)])

    print("%s: %d samples over %.1f s" % (args.recording, len(ratios), len(ratios) / float(SAMPLE_FPS)))
    print("detail_ratio  mean %.3f  min %.3f  max %.3f  stdev %.3f"
          % (ratios.mean(), ratios.min(), ratios.max(), ratios.std()))

    min_frames = int(MIN_SEGMENT_S * SAMPLE_FPS)
    steps = find_steps(ratios, min_frames, args.step)
    if not steps:
        print("\nno sustained step over %.0f%% - no evidence of a resolution switch"
              % (args.step * 100))
    else:
        print("\n%d step%s found:" % (len(steps), "" if len(steps) == 1 else "s"))
        for i, before, after in steps:
            # a step that coincides with the picture going dark is more likely a
            # scene change than a resolution change, so show both
            b0 = brightness[max(0, i - min_frames):i].mean()
            b1 = brightness[i:i + min_frames].mean()
            print("  t=%6.1fs  ratio %.3f -> %.3f (%+.0f%%)   brightness %.0f -> %.0f"
                  % (i / float(SAMPLE_FPS), before, after,
                     100.0 * (after - before) / before, b0, b1))
        print("\nreport as consistent with a resolution switch, not as one: this "
              "measures detail,\nnot pixel dimensions, and blur and motion move it too.")
    print("\nwrote %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
