#!/usr/bin/env python3
"""HDMI snapshot ring buffer — saves last 60 seconds of frames (1fps)"""
import time, os, subprocess, sys

OUTDIR = "/tmp/hdmi_ring"
INTERVAL = 1.0  # seconds between captures
KEEP = 60       # seconds of history

os.makedirs(OUTDIR, exist_ok=True)

print(f"Ring buffer: {OUTDIR}, {INTERVAL}s interval, {KEEP}s history")
while True:
    ts = time.time()
    fname = f"{OUTDIR}/{ts:.2f}.jpg"
    try:
        r = subprocess.run(
            ["curl", "-s", "-o", fname, "http://localhost:8888/snapshot", "--max-time", "3"],
            capture_output=True, timeout=5
        )
        if r.returncode != 0:
            print(f"[{time.strftime('%H:%M:%S')}] capture failed (rc={r.returncode})")
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] error: {e}")

    # Clean old frames
    cutoff = ts - KEEP
    for f in os.listdir(OUTDIR):
        try:
            fts = float(f.replace('.jpg', ''))
            if fts < cutoff:
                os.remove(os.path.join(OUTDIR, f))
        except:
            pass

    elapsed = time.time() - ts
    if elapsed < INTERVAL:
        time.sleep(INTERVAL - elapsed)
