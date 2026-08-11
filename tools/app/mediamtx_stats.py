"""Poll a MediaMTX server once a second for the Server stage of the metrics sheet.

Usage, started before a trial and stopped after:
    python tools/app/mediamtx_stats.py --path mystream -o data/app/server_trial01.csv

Columns: epoch_window_start_ms, path, stream_ready, readiness_time_s,
         bytes_received, ingest_bitrate_kbps, bytes_sent, active_readers, tracks

Covers server_ingest_bitrate, bytes_received, active_readers and
stream_ready / readiness_time. Nothing here needs the phone: point ffmpeg at the
same server and every column fills in, which is the cheap way to prove the
server half works before writing anything that publishes from Android.

MediaMTX serves this on its HTTP API, port 9997 by default. If the connection is
refused, the API is off - set `api: yes` in mediamtx.yml and restart it.

Stdlib only, so it runs anywhere Python does.
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request


def fetch_paths(api):
    """Every path the server knows about, as a list of dicts."""
    url = api.rstrip("/") + "/v3/paths/list"
    with urllib.request.urlopen(url, timeout=3) as r:
        data = json.loads(r.read().decode())
    # the v3 API wraps the list in a paged object; older builds returned a bare
    # list, so accept either rather than failing on a version difference
    if isinstance(data, dict):
        return data.get("items", [])
    return data if isinstance(data, list) else []


def pick(paths, want):
    if want:
        return next((p for p in paths if p.get("name") == want), None)
    # with no --path given, follow whichever path is actually publishing
    ready = [p for p in paths if p.get("ready")]
    return (ready or paths or [None])[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://127.0.0.1:9997",
                    help="MediaMTX API base URL")
    ap.add_argument("--path", default=None,
                    help="stream path name, e.g. mystream (default: whichever is publishing)")
    ap.add_argument("-o", "--out", default=os.path.join("data", "app", "server_stats.csv"))
    ap.add_argument("--interval", type=float, default=1.0)
    args = ap.parse_args()

    outdir = os.path.dirname(args.out)
    if outdir and not os.path.isdir(outdir):
        os.makedirs(outdir)

    cols = ["epoch_window_start_ms", "path", "stream_ready", "readiness_time_s",
            "bytes_received", "ingest_bitrate_kbps", "bytes_sent", "active_readers",
            "tracks"]

    first_seen = None      # when a path first appeared at all
    ready_at = None        # when it first reported ready
    already_ready = False  # publishing before we started: readiness_time is not ours to claim
    prev_bytes = None
    prev_t = None
    rows = 0

    print("polling %s every %.1fs, writing %s" % (args.api, args.interval, args.out))
    print("ctrl-c to stop\n")
    try:
        with open(args.out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            while True:
                now = time.time()
                try:
                    paths = fetch_paths(args.api)
                except urllib.error.URLError as e:
                    sys.exit("cannot reach the MediaMTX API at %s (%s)\n"
                             "Is it running, and is `api: yes` set in mediamtx.yml?"
                             % (args.api, e))
                p = pick(paths, args.path)

                if p is None:
                    row = dict.fromkeys(cols, "")
                    row["epoch_window_start_ms"] = int(now * 1000)
                    row["path"] = args.path or ""
                    row["stream_ready"] = 0
                    row["active_readers"] = 0
                else:
                    ready = bool(p.get("ready"))
                    if first_seen is None:
                        first_seen = now
                        # already publishing when we attached, so the transition
                        # into ready happened before we were watching and cannot
                        # be timed. Say so rather than reporting a phantom 0.
                        if ready:
                            already_ready = True
                    if ready and ready_at is None:
                        ready_at = now
                    got = int(p.get("bytesReceived") or 0)
                    # cumulative counters, so the rate is the delta over the gap;
                    # a restart resets them, which would otherwise read as a huge
                    # negative spike
                    kbps = ""
                    if prev_bytes is not None and prev_t is not None:
                        dt = now - prev_t
                        db = got - prev_bytes
                        if dt > 0 and db >= 0:
                            kbps = round(db * 8 / dt / 1000.0, 1)
                    prev_bytes, prev_t = got, now

                    row = {
                        "epoch_window_start_ms": int(now * 1000),
                        "path": p.get("name", ""),
                        "stream_ready": 1 if ready else 0,
                        "readiness_time_s": ("" if already_ready or ready_at is None
                                             else round(ready_at - first_seen, 2)),
                        "bytes_received": got,
                        "ingest_bitrate_kbps": kbps,
                        "bytes_sent": int(p.get("bytesSent") or 0),
                        "active_readers": len(p.get("readers") or []),
                        "tracks": "|".join(p.get("tracks") or []),
                    }
                w.writerow(row)
                f.flush()
                rows += 1
                if rows % 5 == 1:
                    print("  %s  ready=%s  %s kbps  readers=%s  tracks=%s"
                          % (row["path"] or "(no path)", row["stream_ready"],
                             row["ingest_bitrate_kbps"] or "-",
                             row["active_readers"], row["tracks"] or "-"))
                time.sleep(args.interval)
    except KeyboardInterrupt:
        pass

    print("\nwrote %d rows to %s" % (rows, args.out))
    if already_ready:
        print("readiness_time: not measured - the stream was already publishing when "
              "polling started.\n  Start this before the publisher to capture it.")
    elif ready_at is not None and first_seen is not None:
        print("readiness_time: %.2f s from the path appearing to it going ready"
              % (ready_at - first_seen))
    else:
        print("the stream never went ready - nothing published to it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
