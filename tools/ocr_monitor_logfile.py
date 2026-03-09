#!/usr/bin/env python3
"""OCR sweep monitor that reads from a serial log file (tail -f style).

This avoids opening the serial port (which resets ESP32-C3).
The serial output is captured separately via: timeout 600 cat /dev/ttyACM0 > logfile

Usage: .venv/bin/python ocr_monitor_logfile.py /tmp/sweep_full2.serial.log
"""
import time
import subprocess
import os
import re
import sys
import cv2

_ocr = None
def get_ocr():
    global _ocr
    if _ocr is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr = RapidOCR()
    return _ocr

SNAP_URL = 'http://localhost:8888/snapshot'
OUTDIR = '/tmp/sweep_full2'
RESULTS_FILE = '/tmp/sweep_full2_results.txt'

OSD_KEYWORDS = [
    'volume', 'vol', 'channel', 'menu', 'mute',
    'programme', 'program', 'all channels', 'settings',
    'no signal', 'standby', 'power', 'input', 'source',
    'ok', 'exit', 'back', 'info', 'guide', 'epg',
    'audio', 'subtitle', 'language', 'timer', 'sleep',
    'favourite', 'favorite', 'list', 'search',
    'picture', 'time', 'lock', 'parental',
]
CH_PATTERN = re.compile(r'(?:C\d{3}\b|ch\.?\s*\d+|\d{1,3}\s*/\s*\d+)', re.IGNORECASE)

def snap_raw():
    path = '/tmp/_snap_tmp.jpg'
    try:
        subprocess.run(['curl', '-s', '-o', path, SNAP_URL, '--max-time', '3'],
                       capture_output=True, timeout=5)
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
    if img is None:
        return []
    ocr = get_ocr()
    result, _ = ocr(img)
    if result is None:
        return []
    return [(item[1], sum(pt[1] for pt in item[0]) / 4) for item in result]

def find_osd_text(ocr_results, img_height=720):
    subtitle_threshold = img_height * 0.80
    osd_found = []
    for text, y_center in ocr_results:
        if y_center > subtitle_threshold:
            continue
        text_lower = text.lower().strip()
        for kw in OSD_KEYWORDS:
            if kw in text_lower:
                osd_found.append(text)
                break
        else:
            if CH_PATTERN.search(text):
                osd_found.append(text)
    return osd_found

def main():
    if len(sys.argv) < 2:
        print("Usage: ocr_monitor_logfile.py <serial_log_file>")
        sys.exit(1)

    log_path = sys.argv[1]
    os.makedirs(OUTDIR, exist_ok=True)
    log = open(RESULTS_FILE, 'w')

    def logprint(msg):
        print(msg, flush=True)
        log.write(msg + '\n')
        log.flush()

    logprint(f"=== OCR Monitor (logfile) Started {time.strftime('%H:%M:%S')} ===")
    logprint(f"Reading from: {log_path}")
    logprint(f"Key scan addr: 0x4F (TM1650 standard)")
    logprint("Initializing OCR...")
    get_ocr()
    logprint("OCR ready.")

    current_val = -1
    boot_init_done = True  # assume already booted since sweep is running
    prev_osd = []
    writes_for_val = []
    hits = []
    file_pos = 0

    # Start from current end of file (ignore old data)
    if os.path.exists(log_path):
        file_pos = os.path.getsize(log_path)

    # Take initial baseline
    img = snap_raw()
    if img is not None:
        prev_osd = find_osd_text(run_ocr(img), img.shape[0])
        logprint(f"Baseline OSD: {prev_osd}")

    t0 = time.time()
    while time.time() - t0 < 1800:
        # Read new lines from log file
        try:
            with open(log_path, 'r') as f:
                f.seek(file_pos)
                new_data = f.read()
                file_pos = f.tell()
        except:
            time.sleep(0.1)
            continue

        if not new_data:
            time.sleep(0.1)
            continue

        for line in new_data.strip().split('\n'):
            line = line.strip()
            if not line:
                continue

            if 'STB ready' in line:
                boot_init_done = False
                logprint(line)

            if not boot_init_done and 'boot init' not in line.lower():
                # Wait 5s worth of lines after STB ready
                pass  # simplified: just set boot_init_done after seeing first SWEEP line

            # Sweep value change
            m = re.search(r'\[SWEEP (\d+)/\d+\] val=0x([0-9A-Fa-f]{2})', line)
            if m:
                boot_init_done = True  # if we see sweep lines, boot is done
                new_val = int(m.group(2), 16)

                if current_val >= 0:
                    # Capture + OCR for previous value
                    img = snap_raw()
                    if img is not None:
                        osd = find_osd_text(run_ocr(img), img.shape[0])
                        new_osd = [t for t in osd if t not in prev_osd]
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
                                for w in writes_for_val[:5]:
                                    logprint(f"  {w}")
                            if new_osd:
                                logprint(f"  New OSD: {new_osd}")
                        elif new_val % 32 == 0:
                            logprint(f"[0x{current_val:02X}] ok (osd={osd[:2]})")

                        if osd:
                            prev_osd = osd

                current_val = new_val
                writes_for_val = []

            if 'WRITE addr=' in line and boot_init_done:
                writes_for_val.append(line)

            if 'POWER ON' in line or 'POWER OFF' in line:
                logprint(line)
                boot_init_done = False

            if 'COMPLETE' in line or ('SWEEP' in line and 'END' in line):
                logprint(line)
                # Process last value
                if current_val >= 0:
                    img = snap_raw()
                    if img is not None:
                        osd = find_osd_text(run_ocr(img), img.shape[0])
                        new_osd = [t for t in osd if t not in prev_osd]
                        if new_osd or writes_for_val:
                            markers = []
                            if new_osd:
                                markers.append(f"OSD:{new_osd}")
                            if writes_for_val:
                                markers.append(f"I2C:{len(writes_for_val)}w")
                            logprint(f"[0x{current_val:02X}] *** HIT *** {' + '.join(markers)}")
                            hits.append((current_val, new_osd, len(writes_for_val)))
                if 'END' in line:
                    break

        # Check if sweep ended
        if 'END' in new_data:
            break

    # Summary
    logprint(f"\n{'='*60}")
    logprint(f"=== SWEEP RESULTS (keyScanAddr=0x4F) ===")
    logprint(f"Hits: {len(hits)}")
    for v, osd, nw in hits:
        logprint(f"  0x{v:02X}: osd={osd} writes={nw}")
    if not hits:
        logprint("  No hits found!")
    logprint(f"{'='*60}")

    log.close()

if __name__ == '__main__':
    main()
