#!/usr/bin/env python3
"""Sync sweep v2: per-value inject via shell serial, HDMI image diff detection.

Uses stty -hupcl + raw file I/O to avoid ESP32-C3 reset.
Image difference (structural) to detect OSD changes over live TV.
OCR only runs on significant changes.

Usage:
    .venv/bin/python sync_sweep_v2.py            # full 0x00-0xFF
    .venv/bin/python sync_sweep_v2.py 40 7F      # range 0x40-0x7F
"""
import time
import subprocess
import os
import re
import sys
import struct
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
SNAP_URL = 'http://localhost:8888/snapshot'
OUTDIR = '/tmp/sync_sweep_v2'
RESULTS_FILE = '/tmp/sync_sweep_v2_results.txt'

OSD_KEYWORDS = [
    'volume', 'vol', 'channel', 'menu', 'mute',
    'programme', 'program', 'all channels', 'settings',
    'no signal', 'standby', 'power', 'input', 'source',
    'ok', 'exit', 'back', 'info', 'guide', 'epg',
    'audio', 'subtitle', 'language', 'timer', 'sleep',
    'favourite', 'favorite', 'list', 'search',
    'picture', 'lock', 'parental',
]
# Channel pattern: CXXX only (not dates)
CH_PATTERN = re.compile(r'\bC\d{3}\b', re.IGNORECASE)

def snap():
    """Capture HDMI frame, return (image, size)."""
    path = '/tmp/_snap_sync.jpg'
    try:
        subprocess.run(['curl', '-s', '-o', path, SNAP_URL, '--max-time', '3'],
                       capture_output=True, timeout=5)
    except:
        return None, 0
    if not os.path.exists(path):
        return None, 0
    img = cv2.imread(path)
    size = os.path.getsize(path)
    return img, size

def image_diff(img1, img2):
    """Compute structural difference between two images.
    Returns a score: 0 = identical, higher = more different.
    Also returns a mask of changed pixels."""
    if img1 is None or img2 is None:
        return 999, None
    # Resize to same size if needed
    if img1.shape != img2.shape:
        img2 = cv2.resize(img2, (img1.shape[1], img1.shape[0]))
    # Convert to grayscale
    g1 = cv2.cvtColor(img1, cv2.COLOR_BGR2GRAY)
    g2 = cv2.cvtColor(img2, cv2.COLOR_BGR2GRAY)
    # Compute absolute difference
    diff = cv2.absdiff(g1, g2)
    # Threshold to get changed pixels (>30 intensity change)
    _, mask = cv2.threshold(diff, 30, 255, cv2.THRESH_BINARY)
    # Score = percentage of changed pixels
    changed_pct = np.count_nonzero(mask) / mask.size * 100
    return changed_pct, mask

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

class SerialPort:
    """Persistent serial port using raw file descriptors (no pyserial, no reset)."""
    def __init__(self, port):
        self.port = port
        self._write_fd = None
        self._read_fd = None

    def open(self):
        subprocess.run(['stty', '-F', self.port, '115200', 'raw', '-echo', '-hupcl'],
                       check=True, capture_output=True)
        self._write_fd = os.open(self.port, os.O_WRONLY)
        self._read_fd = os.open(self.port, os.O_RDONLY | os.O_NONBLOCK)

    def write(self, data):
        if isinstance(data, str):
            data = data.encode()
        os.write(self._write_fd, data)

    def read_lines(self, timeout=1.0):
        buf = b''
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                data = os.read(self._read_fd, 4096)
                if data:
                    buf += data
            except OSError:
                pass
            time.sleep(0.03)
        lines = []
        for line in buf.split(b'\n'):
            s = line.decode('utf-8', errors='replace').strip()
            if s:
                lines.append(s)
        return lines

    def close(self):
        if self._write_fd is not None:
            os.close(self._write_fd)
        if self._read_fd is not None:
            os.close(self._read_fd)

_serial = None

def get_serial():
    global _serial
    if _serial is None:
        _serial = SerialPort(PORT)
        _serial.open()
    return _serial

def inject_key(val, verify=False):
    """Inject a raw key value via 'x' command."""
    s = get_serial()
    s.write(f'x{val:02X}')
    if verify:
        time.sleep(0.3)
        lines = s.read_lines(timeout=0.5)
        for l in lines:
            if 'INJECT' in l:
                return True
        return False
    return True

def read_serial_lines(timeout=1.0):
    return get_serial().read_lines(timeout)

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

    logprint(f"=== Sync Sweep v2 Started {time.strftime('%H:%M:%S')} ===")
    logprint(f"Range: 0x{start_val:02X} to 0x{end_val:02X} ({end_val - start_val + 1} values)")
    logprint(f"Key scan addr: 0x4F (TM1650 standard)")
    logprint("Initializing OCR...")
    get_ocr()
    logprint("OCR ready.")

    # Open persistent serial connection
    get_serial()
    logprint("Serial opened (persistent FD, stty -hupcl).")

    # Drain old serial data
    read_serial_lines(timeout=0.5)

    hits = []

    # Pre-check: make sure no OSD menu is showing
    logprint("Pre-check: clearing any open menus...")
    for dismiss_try in range(3):
        img_check, _ = snap()
        osd_check = find_osd_text(run_ocr(img_check), img_check.shape[0]) if img_check is not None else []
        if osd_check:
            logprint(f"  OSD detected: {osd_check}. Sending 0x5F to dismiss...")
            inject_key(0x5F)
            time.sleep(3)
        else:
            logprint(f"  Screen is clean.")
            break

    # Wait a moment for TV to stabilize
    time.sleep(2)

    # Establish baseline OSD text (what's normally on screen)
    logprint("Taking baseline OCR...")
    img_base, _ = snap()
    baseline_osd = set()
    if img_base is not None:
        for text, y in run_ocr(img_base):
            baseline_osd.add(text.lower().strip())
    logprint(f"Baseline has {len(baseline_osd)} text items")

    for val in range(start_val, end_val + 1):
        # 1. Inject key
        inject_key(val)

        # 2. Wait for STB to process (3 polls at 180ms + reaction)
        time.sleep(2.0)

        # 3. Take post-snapshot + OCR
        img_post, post_size = snap()
        osd = find_osd_text(run_ocr(img_post), img_post.shape[0]) if img_post is not None else []

        # 4. Check serial for WRITEs
        serial_lines = read_serial_lines(timeout=0.3)
        writes = [l for l in serial_lines if 'WRITE' in l]

        # 5. Filter: only NEW OSD text (not in baseline)
        new_osd = [t for t in osd if t.lower().strip() not in baseline_osd]

        # Log every value
        if osd or writes:
            logprint(f"[0x{val:02X}] osd={osd} new={new_osd} writes={len(writes)}")

        # 6. Detect hit
        is_hit = len(new_osd) > 0 or len(writes) > 0

        if is_hit:
            markers = []
            if new_osd:
                markers.append(f"OSD:{new_osd}")
            if writes:
                markers.append(f"I2C:{len(writes)}w")

            logprint(f"[0x{val:02X}] *** HIT *** {' + '.join(markers)}")
            hits.append((val, new_osd, len(writes)))

            # Save screenshot
            if img_post is not None:
                cv2.imwrite(os.path.join(OUTDIR, f'HIT_{val:02X}.jpg'), img_post)

            # Wait for OSD to dismiss
            logprint(f"  Clearing OSD...")
            # Try toggling with same key
            inject_key(val)
            time.sleep(3)

            # Check if OSD cleared
            img_check, _ = snap()
            check_osd = find_osd_text(run_ocr(img_check), img_check.shape[0]) if img_check is not None else []
            check_new = [t for t in check_osd if t.lower().strip() not in baseline_osd]
            if check_new:
                # Still has OSD. Try 0x5F to dismiss menu.
                logprint(f"  Still showing OSD: {check_new}. Trying 0x5F...")
                inject_key(0x5F)
                time.sleep(3)
                # If still showing, wait longer
                img_check2, _ = snap()
                check_osd2 = find_osd_text(run_ocr(img_check2), img_check2.shape[0]) if img_check2 is not None else []
                check_new2 = [t for t in check_osd2 if t.lower().strip() not in baseline_osd]
                if check_new2:
                    logprint(f"  Persistent OSD: {check_new2}. Waiting 30s...")
                    time.sleep(30)
            else:
                logprint(f"  Cleared.")

            # Refresh baseline after OSD dismissal
            img_new_base, _ = snap()
            if img_new_base is not None:
                for text, y in run_ocr(img_new_base):
                    baseline_osd.add(text.lower().strip())

        elif val % 32 == 0:
            logprint(f"[0x{val:02X}] ok (osd={osd[:2]})")

        # Brief pause between values
        time.sleep(0.3)

    # Summary
    logprint(f"\n{'='*60}")
    logprint(f"=== SYNC SWEEP v2 RESULTS ===")
    logprint(f"Range: 0x{start_val:02X}-0x{end_val:02X}")
    logprint(f"Total hits: {len(hits)}")
    for v, osd, nw in hits:
        logprint(f"  0x{v:02X}: osd={osd} writes={nw}")
    if not hits:
        logprint("  No hits found!")
    logprint(f"{'='*60}")

    log.close()

if __name__ == '__main__':
    main()
