#!/usr/bin/env python3
"""Sweep monitor: OCR looks for OSD keywords, ignores TV content/subtitles.

Detection strategy:
1. Run OCR on each frame when sweep value changes
2. Look for OSD keywords (Volume, Channel, Menu, etc.) — NOT subtitles
3. Also track I2C WRITEs as a secondary signal
4. Boot-init WRITEs filtered (5s window after resume)

Run with: /home/evnchn/dmr39-c3/.venv/bin/python ocr_sweep_monitor.py
"""
import serial
import time
import subprocess
import os
import re
import cv2
import numpy as np

_ocr = None
def get_ocr():
    global _ocr
    if _ocr is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr = RapidOCR()
    return _ocr

PORT = '/dev/ttyACM0'
BAUD = 115200
SNAP_URL = 'http://localhost:8888/snapshot'
OUTDIR = '/tmp/ocr_sweep'
RESULTS_FILE = '/tmp/ocr_sweep_results.txt'

# OSD keywords (English). Any of these appearing = OSD detected.
OSD_KEYWORDS = [
    'volume', 'vol', 'channel', 'menu', 'mute',
    'programme', 'program', 'all channels', 'settings',
    'no signal', 'standby', 'power', 'input', 'source',
    'ok', 'exit', 'back', 'info', 'guide', 'epg',
    'audio', 'subtitle', 'language', 'timer', 'sleep',
    'favourite', 'favorite', 'list', 'search',
]

# Channel code format: CXXX (e.g. C001, C123)
# Also detect "CH 1", "1/32" patterns
CH_PATTERN = re.compile(r'(?:C\d{3}\b|ch\.?\s*\d+|\d{1,3}\s*/\s*\d+)', re.IGNORECASE)

def snap_raw():
    path = '/tmp/_snap_tmp.jpg'
    try:
        subprocess.run(
            ['curl', '-s', '-o', path, SNAP_URL, '--max-time', '3'],
            capture_output=True, timeout=5
        )
    except:
        return None
    if not os.path.exists(path):
        return None
    return cv2.imread(path)

def snap_save(name, img=None):
    if img is None:
        img = snap_raw()
    if img is not None:
        path = os.path.join(OUTDIR, f'{name}.jpg')
        cv2.imwrite(path, img)
        return path
    return None

def run_ocr(img):
    """Run OCR, return list of (text, y_center) tuples."""
    if img is None:
        return []
    ocr = get_ocr()
    result, _ = ocr(img)
    if result is None:
        return []
    texts = []
    for item in result:
        box = item[0]  # [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]
        text = item[1]
        # Calculate vertical center of text
        y_center = sum(pt[1] for pt in box) / 4
        texts.append((text, y_center))
    return texts

def find_osd_text(ocr_results, img_height=720):
    """Filter OCR results for OSD keywords. Ignore bottom 20% (subtitles area)."""
    subtitle_threshold = img_height * 0.80
    osd_found = []

    for text, y_center in ocr_results:
        # Skip text in subtitle area (bottom 20%)
        if y_center > subtitle_threshold:
            continue

        text_lower = text.lower().strip()

        # Check for OSD keywords
        for kw in OSD_KEYWORDS:
            if kw in text_lower:
                osd_found.append(text)
                break
        else:
            # Check for channel number pattern
            if CH_PATTERN.search(text):
                osd_found.append(text)

    return osd_found

def main():
    import sys
    os.makedirs(OUTDIR, exist_ok=True)
    log = open(RESULTS_FILE, 'w')

    def logprint(msg):
        print(msg, flush=True)
        log.write(msg + '\n')
        log.flush()

    # Parse optional range: ocr_sweep_monitor.py [start_hex end_hex]
    sweep_start = None
    sweep_end = None
    if len(sys.argv) >= 3:
        sweep_start = int(sys.argv[1], 16)
        sweep_end = int(sys.argv[2], 16)

    logprint(f"=== OCR Sweep Monitor v2 Started {time.strftime('%H:%M:%S')} ===")
    if sweep_start is not None:
        logprint(f"Range sweep: 0x{sweep_start:02X} to 0x{sweep_end:02X}")
    logprint("Looking for OSD keywords, ignoring TV content + subtitles")
    logprint("Initializing OCR...")
    get_ocr()
    logprint("OCR ready.")

    s = serial.Serial(PORT, BAUD, timeout=0.2, dsrdtr=False, rtscts=False)
    s.dtr = False
    s.rts = False
    time.sleep(0.3)
    s.reset_input_buffer()

    # Send range sweep command if specified (wait for ESP to finish booting first)
    if sweep_start is not None:
        logprint("Waiting 3s for ESP32-C3 to boot...")
        time.sleep(3)
        s.reset_input_buffer()
        cmd = f'r{sweep_start:02X}{sweep_end:02X}'
        logprint(f"Sending range sweep command: {cmd}")
        for c in cmd:
            s.write(c.encode())
            time.sleep(0.05)
        s.flush()
        time.sleep(0.5)
        # Drain confirmation
        while s.in_waiting:
            line = s.readline().decode('utf-8', errors='replace').strip()
            if line:
                logprint(f"  ESP: {line}")

    current_val = -1
    resume_time = 0
    boot_init_done = False
    prev_osd = []
    writes_for_val = []
    hits = []

    t0 = time.time()
    while time.time() - t0 < 1800:
        if s.in_waiting:
            line = s.readline().decode('utf-8', errors='replace').strip()
            if not line:
                continue

            if 'STB ready' in line:
                resume_time = time.time()
                boot_init_done = False
                logprint(line)

            if not boot_init_done and resume_time > 0 and time.time() - resume_time > 5:
                boot_init_done = True
                logprint("  (boot init done)")
                img = snap_raw()
                if img is not None:
                    prev_osd = find_osd_text(run_ocr(img), img.shape[0])
                    logprint(f"  Baseline OSD: {prev_osd}")

            # Sweep value change
            m = re.search(r'\[SWEEP (\d+)/\d+\] val=0x([0-9A-Fa-f]{2})', line)
            if m and boot_init_done:
                new_val = int(m.group(2), 16)

                if current_val >= 0:
                    # Capture + OCR for previous value
                    img = snap_raw()
                    if img is not None:
                        osd = find_osd_text(run_ocr(img), img.shape[0])

                        # Check for new OSD text
                        new_osd = [t for t in osd if t not in prev_osd]
                        # Also check I2C WRITEs
                        has_writes = len(writes_for_val) > 0

                        if new_osd or has_writes:
                            markers = []
                            if new_osd:
                                markers.append(f"OSD:{new_osd}")
                            if has_writes:
                                markers.append(f"I2C:{len(writes_for_val)}w")
                            logprint(f"[0x{current_val:02X}] *** HIT *** {' + '.join(markers)}")
                            snap_save(f'HIT_{current_val:02X}', img)
                            hits.append((current_val, new_osd, len(writes_for_val)))
                            if writes_for_val:
                                for w in writes_for_val:
                                    logprint(f"  {w}")

                        elif new_val % 32 == 0:
                            logprint(f"[0x{current_val:02X}] ok (osd={osd})")

                        if osd:
                            prev_osd = osd

                current_val = new_val
                writes_for_val = []

            # Track I2C WRITEs
            if 'WRITE addr=' in line and boot_init_done:
                writes_for_val.append(line)

            # Power events
            if 'POWER ON' in line or 'POWER OFF' in line:
                logprint(line)
                boot_init_done = False

            if 'ALL 256 VALUES TESTED' in line:
                logprint(line)
            if 'SWEEP' in line and 'END' in line:
                logprint(line)
                break
        else:
            time.sleep(0.02)

    # Summary
    logprint(f"\n{'='*60}")
    logprint(f"=== OCR SWEEP RESULTS ===")
    logprint(f"Hits: {len(hits)}")
    for v, osd, nw in hits:
        logprint(f"  0x{v:02X}: osd={osd} writes={nw}")
    logprint(f"{'='*60}")

    log.close()
    s.close()

if __name__ == '__main__':
    main()
