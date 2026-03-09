#!/usr/bin/env python3
"""Synchronous key sweep: inject key, wait, snapshot, compare, repeat.
Only injects when HDMI shows the "no signal" baseline (~8879 bytes).
"""
import serial
import time
import subprocess
import os
import sys

PORT = '/dev/ttyACM0'
BAUD = 115200
SNAP_URL = 'http://localhost:8888/snapshot'
OUTDIR = '/tmp/sync_sweep'
RESULTS_FILE = '/tmp/sync_sweep_results.txt'

# "No signal" baseline is ~8879 bytes. 7402 = black (STB off).
# Accept 8000-10000 to exclude black frame but catch "no signal" OSD
BASELINE_TARGET = 8879
BASELINE_LOW = 8000
BASELINE_HIGH = 10000

def snap(name=None):
    """Capture one HDMI frame, return (path, size)."""
    if name:
        path = os.path.join(OUTDIR, f'{name}.jpg')
    else:
        path = '/tmp/snap_tmp.jpg'
    try:
        subprocess.run(
            ['curl', '-s', '-o', path, SNAP_URL, '--max-time', '3'],
            capture_output=True, timeout=5
        )
    except:
        pass
    size = os.path.getsize(path) if os.path.exists(path) else 0
    return path, size

def is_baseline(size):
    """Check if frame size matches the 'no signal' baseline."""
    return BASELINE_LOW <= size <= BASELINE_HIGH

def wait_for_baseline(timeout=150):
    """Wait until HDMI shows the baseline 'no signal' screen."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        _, size = snap()
        if is_baseline(size):
            return True, size
        elapsed = time.time() - t0
        if int(elapsed) % 10 == 0:
            print(f"  waiting for baseline... {size} bytes ({elapsed:.0f}s)", flush=True)
        time.sleep(1)
    return False, 0

def inject_key(s, val):
    """Inject a key code via serial 'x' command."""
    h1 = f'{(val >> 4):X}'
    h2 = f'{(val & 0xF):X}'
    s.write(b'x')
    time.sleep(0.05)
    s.write(h1.encode())
    time.sleep(0.05)
    s.write(h2.encode())
    s.flush()

def drain_serial(s, timeout=0.5):
    """Read all available serial data."""
    lines = []
    t0 = time.time()
    while time.time() - t0 < timeout:
        if s.in_waiting:
            line = s.readline().decode('utf-8', errors='replace').strip()
            if line:
                lines.append(line)
        else:
            time.sleep(0.02)
    return lines

def main():
    os.makedirs(OUTDIR, exist_ok=True)
    log = open(RESULTS_FILE, 'w')

    def logprint(msg):
        print(msg, flush=True)
        log.write(msg + '\n')
        log.flush()

    # Parse optional start value and direction
    # Usage: sync_sweep.py [start] [end]
    # Default: 0 255 (forward). Use "255 0" for reverse.
    start_val = 0
    end_val = 255
    if len(sys.argv) > 1:
        start_val = int(sys.argv[1], 0)
    if len(sys.argv) > 2:
        end_val = int(sys.argv[2], 0)
    reverse = start_val > end_val

    logprint(f"=== Sync Sweep Started {time.strftime('%H:%M:%S')} ===")
    logprint(f"Baseline target: {BASELINE_TARGET} bytes ({BASELINE_LOW}-{BASELINE_HIGH})")
    logprint(f"Range: 0x{start_val:02X} -> 0x{end_val:02X} ({'reverse' if reverse else 'forward'})")

    s = serial.Serial(PORT, BAUD, timeout=0.5, dsrdtr=False, rtscts=False)
    s.dtr = False
    s.rts = False
    time.sleep(0.5)
    drain_serial(s, timeout=2)

    hits = []
    val = start_val
    step = -1 if reverse else 1

    def in_range(v):
        if reverse:
            return v >= end_val
        return v <= end_val

    while in_range(val):
        # 1. Wait for baseline before injecting
        logprint(f"\n[0x{val:02X}] Waiting for baseline...")
        ok, pre_size = wait_for_baseline(timeout=150)
        if not ok:
            logprint(f"[0x{val:02X}] TIMEOUT waiting for baseline. Retrying...")
            continue

        # Double-check baseline is stable (wait 1s, check again)
        time.sleep(1)
        _, check_size = snap()
        if not is_baseline(check_size):
            logprint(f"[0x{val:02X}] Baseline unstable ({check_size}), retrying...")
            time.sleep(2)
            continue

        # 2. Record pre-state
        _, pre_size = snap(f'pre_{val:02X}')

        # 3. Inject key
        inject_key(s, val)

        # 4. Wait for STB to process (3 polls at 180ms + reaction)
        time.sleep(1.5)

        # 5. Take post-snapshot
        _, post_size = snap(f'post_{val:02X}')

        # 6. Check serial for WRITEs
        serial_lines = drain_serial(s, timeout=0.3)
        writes = [l for l in serial_lines if 'WRITE' in l]

        # 7. Compare
        delta = abs(post_size - pre_size)
        pct = (delta / max(pre_size, 1)) * 100

        hdmi_hit = not is_baseline(post_size)  # post frame is NOT baseline = something changed
        i2c_hit = len(writes) > 0

        status = ""
        if hdmi_hit or i2c_hit:
            markers = []
            if hdmi_hit:
                markers.append(f"HDMI({post_size}b)")
            if i2c_hit:
                markers.append(f"I2C({len(writes)}w)")
            status = f" *** {' + '.join(markers)} ***"
            hits.append((val, pre_size, post_size, pct, len(writes), hdmi_hit, i2c_hit))
            # Save the changed frame with a clear name
            snap(f'HIT_{val:02X}')

        logprint(f"[0x{val:02X}] pre={pre_size:6d} post={post_size:6d}{status}")

        if writes:
            for w in writes:
                logprint(f"  WRITE: {w}")

        # 8. If something changed, wait for it to auto-dismiss (max 2 min)
        if hdmi_hit:
            logprint(f"[0x{val:02X}] OSD detected, waiting for auto-dismiss...")
            # No need to actively wait — the next iteration's wait_for_baseline handles it

        val += step

    # Summary
    logprint(f"\n{'='*60}")
    logprint(f"=== SYNC SWEEP COMPLETE ===")
    logprint(f"Total hits: {len(hits)}")
    for v, pre, post, pct, nw, hdmi, i2c in hits:
        flags = []
        if hdmi: flags.append("HDMI")
        if i2c: flags.append("I2C")
        logprint(f"  0x{v:02X}: {pre}->{post} ({pct:.0f}%) writes={nw} [{'+'.join(flags)}]")
    logprint(f"{'='*60}")

    log.close()
    s.close()

if __name__ == '__main__':
    main()
