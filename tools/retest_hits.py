#!/usr/bin/env python3
"""Re-test discovered hits and nearby values with OCR + HDMI.

Focused test of 0x5F (MENU), 0x77 (display change), 0x7A (display mode),
plus nearby values that might be CH+/CH-/VOL+/VOL-/etc.

Waits for STB to be in a stable state before each injection.
Uses firmware 'x' command for injection... but we know firmware sweep works
better. So this script drives the ESP32-C3 firmware's own injection mechanism.

Run with: /home/evnchn/dmr39-c3/.venv/bin/python retest_hits.py
"""
import serial
import time
import subprocess
import os
import re
import cv2
import sys

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
OUTDIR = '/tmp/retest_hits'
RESULTS_FILE = '/tmp/retest_hits_results.txt'

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
    img = cv2.imread(path)
    return img

def snap_save(name, img=None):
    if img is None:
        img = snap_raw()
    if img is not None:
        path = os.path.join(OUTDIR, f'{name}.jpg')
        cv2.imwrite(path, img)
        return path
    return None

def snap_size():
    path = '/tmp/_snap_sz.jpg'
    try:
        subprocess.run(
            ['curl', '-s', '-o', path, SNAP_URL, '--max-time', '3'],
            capture_output=True, timeout=5
        )
    except:
        return 0
    return os.path.getsize(path) if os.path.exists(path) else 0

def run_ocr(img):
    if img is None:
        return []
    ocr = get_ocr()
    result, _ = ocr(img)
    if result is None:
        return []
    texts = []
    for item in result:
        box = item[0]
        text = item[1]
        y_center = sum(pt[1] for pt in box) / 4
        texts.append((text, y_center))
    return texts

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

def inject_key(s, val):
    """Inject a key using the 'x' command (raw hex)."""
    h1 = f'{(val >> 4):X}'
    h2 = f'{(val & 0xF):X}'
    s.write(b'x')
    time.sleep(0.05)
    s.write(h1.encode())
    time.sleep(0.05)
    s.write(h2.encode())
    s.flush()

def drain_serial(s, timeout=0.5):
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

def wait_for_stb_ready(s, logprint, timeout=180):
    """Wait for STB to be on with stable 'no signal' or live TV screen."""
    logprint("Waiting for STB to be ready...")
    t0 = time.time()
    stable_count = 0
    while time.time() - t0 < timeout:
        sz = snap_size()
        # Drain serial
        if s.in_waiting:
            line = s.readline().decode('utf-8', errors='replace').strip()
            if line and ('POWER' in line or 'STB' in line or 'alive' in line):
                logprint(f"  serial: {line}")

        if sz < 7500:
            # Black frame — STB off
            stable_count = 0
            time.sleep(1)
            continue
        elif 20000 < sz < 25000:
            # Boot splash (SUPER)
            stable_count = 0
            time.sleep(1)
            continue
        elif sz > 7500:
            # Could be no-signal OSD or live TV
            stable_count += 1
            if stable_count >= 3:
                logprint(f"  STB ready (frame size {sz}, stable {stable_count}x)")
                return True
        time.sleep(1)
    return False


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    log = open(RESULTS_FILE, 'w')

    def logprint(msg):
        print(msg, flush=True)
        log.write(msg + '\n')
        log.flush()

    logprint(f"=== Re-test Hits Started {time.strftime('%H:%M:%S')} ===")
    logprint("Initializing OCR...")
    get_ocr()
    logprint("OCR ready.")

    # Test values: confirmed hits + nearby values
    # Focus on the range around 0x77 (most likely CH+/CH-) and full systematic nearby test
    test_values = []

    # Systematic: test all values with same high nibble as hits
    # 0x5F hit -> test 0x50-0x5F (but also 0x40-0x4F since TM1650 uses those)
    # 0x77 hit -> test 0x70-0x7F
    # 0x7A hit -> already in 0x70-0x7F range
    # Also test 0x00-0x0F and common remote control codes

    # Priority 1: Immediate neighbors of hits
    for base in [0x77, 0x7A, 0x5F]:
        for offset in range(-3, 4):
            v = base + offset
            if 0 <= v <= 255:
                test_values.append(v)

    # Priority 2: Full 0x70-0x7F range (most hits were here)
    for v in range(0x70, 0x80):
        test_values.append(v)

    # Priority 3: 0x50-0x5F range
    for v in range(0x50, 0x60):
        test_values.append(v)

    # Priority 4: 0x40-0x4F (TM1650 standard key codes)
    for v in range(0x40, 0x50):
        test_values.append(v)

    # Priority 5: 0x60-0x6F
    for v in range(0x60, 0x70):
        test_values.append(v)

    # Deduplicate preserving order
    seen = set()
    unique = []
    for v in test_values:
        if v not in seen:
            seen.add(v)
            unique.append(v)
    test_values = unique

    logprint(f"Testing {len(test_values)} values: {[f'0x{v:02X}' for v in test_values[:10]]}...")

    s = serial.Serial(PORT, BAUD, timeout=0.5, dsrdtr=False, rtscts=False)
    s.dtr = False
    s.rts = False
    time.sleep(0.5)
    drain_serial(s, timeout=2)

    # Wait for STB
    if not wait_for_stb_ready(s, logprint):
        logprint("TIMEOUT waiting for STB. Exiting.")
        s.close()
        log.close()
        return

    # Wait extra 5s for boot init to settle
    logprint("Waiting 5s for boot init to settle...")
    time.sleep(5)
    drain_serial(s, timeout=1)

    # Take baseline OCR
    img = snap_raw()
    baseline_osd = []
    if img is not None:
        baseline_osd = find_osd_text(run_ocr(img), img.shape[0])
        snap_save('baseline', img)
        logprint(f"Baseline OSD: {baseline_osd}")
        logprint(f"Baseline size: {os.path.getsize('/tmp/_snap_tmp.jpg')}")

    hits = []
    for i, val in enumerate(test_values):
        logprint(f"\n[{i+1}/{len(test_values)}] Testing 0x{val:02X}...")

        # Pre-snapshot + OCR
        pre_img = snap_raw()
        pre_size = os.path.getsize('/tmp/_snap_tmp.jpg') if os.path.exists('/tmp/_snap_tmp.jpg') else 0
        pre_osd = find_osd_text(run_ocr(pre_img), pre_img.shape[0]) if pre_img is not None else []

        # Drain serial before inject
        drain_serial(s, timeout=0.2)

        # Inject key
        inject_key(s, val)

        # Wait for STB to process (3 polls at 180ms + reaction time)
        time.sleep(1.8)

        # Post-snapshot + OCR
        post_img = snap_raw()
        post_size = os.path.getsize('/tmp/_snap_tmp.jpg') if os.path.exists('/tmp/_snap_tmp.jpg') else 0
        post_osd = find_osd_text(run_ocr(post_img), post_img.shape[0]) if post_img is not None else []

        # Check serial for WRITEs
        serial_lines = drain_serial(s, timeout=0.3)
        writes = [l for l in serial_lines if 'WRITE' in l]

        # Compare OSD
        new_osd = [t for t in post_osd if t not in pre_osd and t not in baseline_osd]

        # Detect changes
        size_delta = abs(post_size - pre_size)
        hdmi_changed = size_delta > 2000  # significant JPEG size change
        osd_changed = len(new_osd) > 0
        i2c_writes = len(writes) > 0

        if osd_changed or i2c_writes or hdmi_changed:
            markers = []
            if osd_changed:
                markers.append(f"OSD:{new_osd}")
            if i2c_writes:
                markers.append(f"I2C:{len(writes)}w")
            if hdmi_changed:
                markers.append(f"HDMI:Δ{size_delta}")
            logprint(f"[0x{val:02X}] *** HIT *** {' + '.join(markers)}")
            snap_save(f'HIT_{val:02X}', post_img)
            hits.append((val, new_osd, len(writes), size_delta))
            if writes:
                for w in writes:
                    logprint(f"  {w}")
            if new_osd:
                logprint(f"  New OSD text: {new_osd}")

            # If we opened a menu, wait for it to auto-dismiss
            if osd_changed:
                logprint(f"  Waiting 8s for OSD to dismiss...")
                time.sleep(8)
                drain_serial(s, timeout=0.5)
        else:
            # Brief status every 8 values
            if (i + 1) % 8 == 0:
                logprint(f"[0x{val:02X}] ok (pre_osd={pre_osd[:2]})")

        # Small delay between tests
        time.sleep(0.5)

    # Summary
    logprint(f"\n{'='*60}")
    logprint(f"=== RE-TEST RESULTS ===")
    logprint(f"Total hits: {len(hits)}")
    for v, osd, nw, delta in hits:
        logprint(f"  0x{v:02X}: osd={osd} writes={nw} hdmi_delta={delta}")
    if not hits:
        logprint("  No hits found!")
    logprint(f"{'='*60}")

    log.close()
    s.close()

if __name__ == '__main__':
    main()
