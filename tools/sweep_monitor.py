#!/usr/bin/env python3
"""Monitor sweep via serial + HDMI ring buffer comparison.

Reads serial output to track sweep progress.
Compares HDMI snapshots (file size delta) to detect visual changes.
Logs everything to /tmp/sweep_results.txt
"""
import serial
import time
import os
import glob

PORT = '/dev/ttyACM0'
BAUD = 115200
RING_DIR = '/tmp/hdmi_ring'
LOG_FILE = '/tmp/sweep_results.txt'

def get_latest_frame():
    """Get the most recent frame from ring buffer."""
    files = glob.glob(os.path.join(RING_DIR, '*.jpg'))
    if not files:
        return None, 0
    latest = max(files, key=os.path.getmtime)
    return latest, os.path.getsize(latest)

def main():
    log = open(LOG_FILE, 'w')
    def logprint(msg):
        print(msg, flush=True)
        log.write(msg + '\n')
        log.flush()

    logprint(f"=== Sweep Monitor Started {time.strftime('%H:%M:%S')} ===")

    s = serial.Serial(PORT, BAUD, timeout=0.5, dsrdtr=False, rtscts=False)
    s.dtr = False
    s.rts = False
    time.sleep(0.3)
    s.reset_input_buffer()

    # Reset sweep
    s.write(b'w')
    s.flush()
    logprint("Sent 'w' to reset sweep")

    current_val = -1
    prev_frame_size = 0
    hdmi_hits = []
    i2c_hits = []

    t0 = time.time()
    while time.time() - t0 < 900:  # 15 min max
        if s.in_waiting:
            line = s.readline().decode('utf-8', errors='replace').strip()
            if not line:
                continue

            # Track sweep value
            if '[SWEEP ' in line and 'val=0x' in line:
                try:
                    hex_str = line.split('val=0x')[1][:2]
                    current_val = int(hex_str, 16)
                except:
                    pass

            # Log important lines
            if 'HIT' in line:
                logprint(f"*** I2C HIT: {line}")
                i2c_hits.append(line)
            elif 'SWEEP' in line and 'END' in line:
                logprint(line)
                break
            elif 'WRITE' in line:
                logprint(f"  WRITE during val=0x{current_val:02X}: {line}")
            elif 'POWER ON' in line or 'POWER OFF' in line:
                logprint(line)
            elif 'STB ready' in line:
                logprint(line)
            elif 'COMPLETE' in line or 'SWEEP' in line:
                # Print sweep progress every 16 values
                if 'val=0x' in line:
                    try:
                        v = int(line.split('val=0x')[1][:2], 16)
                        if v % 16 == 0:
                            logprint(line)
                    except:
                        logprint(line)
                else:
                    logprint(line)

            # HDMI comparison after each sweep value change
            if '[SWEEP ' in line and 'val=0x' in line and current_val >= 0:
                _, new_size = get_latest_frame()
                if prev_frame_size > 0 and new_size > 0:
                    delta = abs(new_size - prev_frame_size)
                    pct = (delta / max(prev_frame_size, 1)) * 100
                    if pct > 15:  # >15% size change = likely visual change
                        logprint(f"*** HDMI CHANGE at val=0x{current_val:02X}: "
                                f"size {prev_frame_size}->{new_size} ({pct:.0f}% delta)")
                        hdmi_hits.append(current_val)
                prev_frame_size = new_size
        else:
            time.sleep(0.05)

    # Summary
    logprint(f"\n=== SWEEP MONITOR RESULTS ===")
    logprint(f"I2C hits (display writes): {len(i2c_hits)}")
    for h in i2c_hits:
        logprint(f"  {h}")
    logprint(f"HDMI hits (visual changes): {len(hdmi_hits)}")
    for v in hdmi_hits:
        logprint(f"  0x{v:02X}")
    logprint(f"=== END ===")

    log.close()
    s.close()

if __name__ == '__main__':
    main()
