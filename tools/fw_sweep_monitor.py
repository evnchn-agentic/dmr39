#!/usr/bin/env python3
"""Monitor firmware-driven sweep with I2C WRITE detection + HDMI snapshots.
Antenna must be connected. Primary detection: I2C WRITEs (display changes).
Secondary: HDMI snapshots saved on any I2C hit for visual confirmation.

Boot-init WRITEs are filtered: we ignore WRITEs in the first 3s after sweep resumes.
"""
import serial
import time
import subprocess
import os
import re

PORT = '/dev/ttyACM0'
BAUD = 115200
SNAP_URL = 'http://localhost:8888/snapshot'
OUTDIR = '/tmp/fw_sweep'
RESULTS_FILE = '/tmp/fw_sweep_results.txt'

def snap(name):
    path = os.path.join(OUTDIR, f'{name}.jpg')
    try:
        subprocess.run(
            ['curl', '-s', '-o', path, SNAP_URL, '--max-time', '3'],
            capture_output=True, timeout=5
        )
    except:
        pass
    return os.path.getsize(path) if os.path.exists(path) else 0

def main():
    os.makedirs(OUTDIR, exist_ok=True)
    log = open(RESULTS_FILE, 'w')

    def logprint(msg):
        print(msg, flush=True)
        log.write(msg + '\n')
        log.flush()

    logprint(f"=== FW Sweep Monitor Started {time.strftime('%H:%M:%S')} ===")
    logprint("Using I2C WRITE detection (ignoring boot-init WRITEs)")
    logprint("HDMI snapshots saved on any real hit for visual confirmation")

    s = serial.Serial(PORT, BAUD, timeout=0.2, dsrdtr=False, rtscts=False)
    s.dtr = False
    s.rts = False
    time.sleep(0.3)
    s.reset_input_buffer()

    current_val = -1
    resume_time = 0       # when sweep resumed after boot
    boot_init_done = False # true after 3s post-resume (boot WRITEs finished)
    writes_for_val = []    # WRITEs collected for current value
    hits = []

    t0 = time.time()
    while time.time() - t0 < 1800:  # 30 min max
        if s.in_waiting:
            line = s.readline().decode('utf-8', errors='replace').strip()
            if not line:
                continue

            # Track sweep resume (after STB boot)
            if 'STB ready' in line:
                resume_time = time.time()
                boot_init_done = False
                logprint(line)

            # After 5s post-resume, consider boot init done
            if not boot_init_done and resume_time > 0 and time.time() - resume_time > 5:
                boot_init_done = True
                logprint(f"  (boot init window closed, tracking real WRITEs now)")

            # Detect sweep value change
            m = re.search(r'\[SWEEP (\d+)/256\] val=0x([0-9A-Fa-f]{2})', line)
            if m:
                sweep_num = int(m.group(1))
                new_val = int(m.group(2), 16)

                # Process previous value's WRITEs
                if current_val >= 0 and boot_init_done and writes_for_val:
                    logprint(f"[0x{current_val:02X}] *** REAL I2C HIT — {len(writes_for_val)} WRITEs! ***")
                    for w in writes_for_val:
                        logprint(f"  {w}")
                    sz = snap(f'HIT_{current_val:02X}')
                    logprint(f"  HDMI snapshot: {sz} bytes")
                    hits.append((current_val, len(writes_for_val)))
                elif current_val >= 0 and sweep_num % 16 == 0:
                    logprint(f"[0x{current_val:02X}] ok (no WRITEs)")

                current_val = new_val
                writes_for_val = []

            # Collect WRITEs (only after boot init window)
            if 'WRITE addr=' in line and boot_init_done:
                writes_for_val.append(line)

            # Log power events
            if 'POWER ON' in line or 'POWER OFF' in line:
                logprint(line)

            # Detect completion
            if 'ALL 256 VALUES TESTED' in line:
                logprint(line)
            if 'SWEEP' in line and 'END' in line:
                logprint(line)
                break
        else:
            time.sleep(0.02)

    # Summary
    logprint(f"\n{'='*60}")
    logprint(f"=== RESULTS ===")
    logprint(f"Real I2C hits (post-boot WRITEs): {len(hits)}")
    for v, nw in hits:
        logprint(f"  0x{v:02X}: {nw} WRITEs — see /tmp/fw_sweep/HIT_{v:02X}.jpg")
    if not hits:
        logprint("  None found.")
    logprint(f"{'='*60}")

    log.close()
    s.close()

if __name__ == '__main__':
    main()
