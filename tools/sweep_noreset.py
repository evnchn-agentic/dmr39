#!/usr/bin/env python3
"""Sweep monitor using stty+raw I/O to avoid ESP32-C3 reset.

Uses stty -hupcl to prevent USB_UART_CHIP_RESET when opening/closing the serial port.
Sends range sweep command via file I/O, monitors with OCR.

Usage:
    .venv/bin/python sweep_noreset.py 40 7F    # sweep 0x40-0x7F
    .venv/bin/python sweep_noreset.py 00 FF    # full sweep
    .venv/bin/python sweep_noreset.py           # full sweep (default)
"""
import time
import subprocess
import os
import re
import sys
import select
import cv2

_ocr = None
def get_ocr():
    global _ocr
    if _ocr is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr = RapidOCR()
    return _ocr

PORT = '/dev/ttyACM0'
SNAP_URL = 'http://localhost:8888/snapshot'
OUTDIR = '/tmp/sweep_noreset'
RESULTS_FILE = '/tmp/sweep_noreset_results.txt'

OSD_KEYWORDS = [
    'volume', 'vol', 'channel', 'menu', 'mute',
    'programme', 'program', 'all channels', 'settings',
    'no signal', 'standby', 'power', 'input', 'source',
    'ok', 'exit', 'back', 'info', 'guide', 'epg',
    'audio', 'subtitle', 'language', 'timer', 'sleep',
    'favourite', 'favorite', 'list', 'search',
]
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

def setup_serial():
    """Configure serial port with stty (no-reset mode)."""
    subprocess.run(['stty', '-F', PORT, '115200', 'raw', '-echo', '-hupcl'],
                   check=True, capture_output=True)

def send_serial(data):
    """Write to serial port via file I/O."""
    with open(PORT, 'wb', buffering=0) as f:
        f.write(data)

def main():
    os.makedirs(OUTDIR, exist_ok=True)
    log = open(RESULTS_FILE, 'w')

    def logprint(msg):
        print(msg, flush=True)
        log.write(msg + '\n')
        log.flush()

    # Parse range
    start_val = 0x00
    end_val = 0xFF
    if len(sys.argv) >= 3:
        start_val = int(sys.argv[1], 16)
        end_val = int(sys.argv[2], 16)

    logprint(f"=== No-Reset Sweep Monitor {time.strftime('%H:%M:%S')} ===")
    logprint(f"Range: 0x{start_val:02X} to 0x{end_val:02X}")
    logprint("Initializing OCR...")
    get_ocr()
    logprint("OCR ready.")

    # Setup serial without reset
    setup_serial()
    logprint("Serial port configured (stty -hupcl).")

    # Send range sweep command
    cmd = f'r{start_val:02X}{end_val:02X}'.encode()
    logprint(f"Sending range command: {cmd.decode()}")
    send_serial(cmd)
    time.sleep(1)

    # Open serial for reading (non-blocking)
    fd = os.open(PORT, os.O_RDONLY | os.O_NONBLOCK)

    current_val = -1
    resume_time = 0
    boot_init_done = False
    prev_osd = []
    writes_for_val = []
    hits = []
    line_buf = b''

    t0 = time.time()
    while time.time() - t0 < 1800:
        # Read available serial data
        try:
            data = os.read(fd, 4096)
            if data:
                line_buf += data
        except OSError:
            pass  # EAGAIN = no data available

        # Process complete lines
        while b'\n' in line_buf:
            line_raw, line_buf = line_buf.split(b'\n', 1)
            line = line_raw.decode('utf-8', errors='replace').strip()
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
                                for w in writes_for_val:
                                    logprint(f"  {w}")
                            if new_osd:
                                logprint(f"  New OSD: {new_osd}")
                        elif new_val % 16 == 0:
                            logprint(f"[0x{current_val:02X}] ok (osd={osd[:3]})")

                        if osd:
                            prev_osd = osd

                current_val = new_val
                writes_for_val = []

            if 'WRITE addr=' in line and boot_init_done:
                writes_for_val.append(line)

            if 'POWER ON' in line or 'POWER OFF' in line:
                logprint(line)
                boot_init_done = False

            if 'RANGE SWEEP' in line:
                logprint(line)

            if 'SWEEP' in line and 'END' in line:
                logprint(line)
                # Process last value
                if current_val >= 0 and boot_init_done:
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
                break

            if 'COMPLETE' in line:
                logprint(line)

        time.sleep(0.02)

    os.close(fd)

    # Summary
    logprint(f"\n{'='*60}")
    logprint(f"=== SWEEP RESULTS ===")
    logprint(f"Hits: {len(hits)}")
    for v, osd, nw in hits:
        logprint(f"  0x{v:02X}: osd={osd} writes={nw}")
    if not hits:
        logprint("  No hits found!")
    logprint(f"{'='*60}")

    log.close()

if __name__ == '__main__':
    main()
