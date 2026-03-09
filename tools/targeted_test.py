#!/usr/bin/env python3
"""Targeted key test — inject specific values and capture HDMI snapshots."""
import serial
import time
import subprocess
import os

PORT = '/dev/ttyACM0'
BAUD = 115200
RING_DIR = '/tmp/hdmi_ring'

def snap(label):
    path = f'/tmp/key_test_{label}.jpg'
    subprocess.run(['curl', '-s', '-o', path, 'http://localhost:8888/snapshot'], timeout=5)
    size = os.path.getsize(path) if os.path.exists(path) else 0
    return path, size

def inject_key(s, val):
    """Inject a key using the 'x' command (raw hex)."""
    h1 = f'{(val >> 4):X}'
    h2 = f'{(val & 0xF):X}'
    s.write(b'x')
    time.sleep(0.1)
    s.write(h1.encode())
    time.sleep(0.1)
    s.write(h2.encode())
    s.flush()
    time.sleep(0.3)
    # Read response
    while s.in_waiting:
        line = s.readline().decode('utf-8', errors='replace').strip()
        if line:
            print(f"  serial: {line}")

def main():
    s = serial.Serial(PORT, BAUD, timeout=0.5, dsrdtr=False, rtscts=False)
    s.dtr = False
    s.rts = False
    time.sleep(0.3)
    s.reset_input_buffer()

    # Take baseline
    _, baseline_size = snap('baseline')
    print(f"Baseline frame size: {baseline_size}")

    # Test values: near 0xE0 (confirmed volume), and candidates for CH+/CH-/Power
    # Also test some values from the first half that we couldn't test with stable HDMI
    test_values = [
        # Near 0xE0 (volume was here)
        0xE0, 0xE2, 0xE4, 0xE8,
        0xF0, 0xF2, 0xF4, 0xF8,
        0xD0, 0xD2, 0xD4, 0xD8,
        0xC0, 0xC2, 0xC4, 0xC8,
        # 0xA8 was interesting
        0xA8, 0xA0, 0xA4,
        # High nibble variants of 0xE0
        0xE0, 0xD0, 0xC0, 0xB0, 0xA0, 0x90, 0x80,
        # Low nibble variants
        0xE1, 0xE2, 0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8,
        # Standard TM1650 candidates
        0x44, 0x4C, 0x54, 0x5C, 0x64, 0x6C, 0x74, 0x7C,
        # First-half interesting values (from earlier sweep with TV)
        0x34, 0x38, 0x3C,
        0x14, 0x18, 0x1C,
        0x24, 0x28, 0x2C,
    ]

    # Deduplicate while preserving order
    seen = set()
    unique_values = []
    for v in test_values:
        if v not in seen:
            seen.add(v)
            unique_values.append(v)

    print(f"\nTesting {len(unique_values)} values with HDMI snapshot comparison")
    print("=" * 60)

    results = []
    for val in unique_values:
        # Pre-snapshot
        _, pre_size = snap(f'pre_0x{val:02X}')

        # Inject key
        print(f"\n[0x{val:02X}] Injecting...")
        inject_key(s, val)

        # Wait for STB to respond
        time.sleep(2.0)

        # Post-snapshot
        _, post_size = snap(f'post_0x{val:02X}')

        delta = abs(post_size - pre_size)
        pct = (delta / max(pre_size, 1)) * 100

        status = ""
        if pct > 10:
            status = " *** CHANGE ***"
            results.append((val, pre_size, post_size, pct))

        print(f"[0x{val:02X}] pre={pre_size} post={post_size} delta={pct:.0f}%{status}")

        # Wait for any OSD to dismiss
        time.sleep(2.0)

    print("\n" + "=" * 60)
    print("RESULTS — Values that triggered HDMI changes:")
    if results:
        for val, pre, post, pct in results:
            print(f"  0x{val:02X}: {pre} -> {post} ({pct:.0f}%)")
    else:
        print("  None found!")

    s.close()

if __name__ == '__main__':
    main()
