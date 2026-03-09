#!/usr/bin/env python3
"""Test which I2C read address is the key scan register.

Sets keyScanAddr to each known read address, injects 0x5F (MENU),
and checks HDMI + OCR for menu appearance.

Uses stty -hupcl to avoid ESP reset.

Run with: .venv/bin/python test_key_addr.py
"""
import time
import subprocess
import os
import re
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
OUTDIR = '/tmp/test_key_addr'

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
        subprocess.run(['curl', '-s', '-o', path, SNAP_URL, '--max-time', '3'],
                       capture_output=True, timeout=5)
    except:
        return None
    if not os.path.exists(path):
        return None
    return cv2.imread(path)

def snap_size():
    path = '/tmp/_snap_sz.jpg'
    try:
        subprocess.run(['curl', '-s', '-o', path, SNAP_URL, '--max-time', '3'],
                       capture_output=True, timeout=5)
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

def send_serial(data):
    with open(PORT, 'wb', buffering=0) as f:
        f.write(data)

def read_serial(timeout=2):
    """Read serial output using non-blocking I/O."""
    fd = os.open(PORT, os.O_RDONLY | os.O_NONBLOCK)
    lines = []
    buf = b''
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            data = os.read(fd, 4096)
            if data:
                buf += data
        except OSError:
            pass
        time.sleep(0.05)
    os.close(fd)
    for line_raw in buf.split(b'\n'):
        line = line_raw.decode('utf-8', errors='replace').strip()
        if line:
            lines.append(line)
    return lines

def set_key_addr(addr):
    """Set the key scan address (0x00 = ALL)."""
    cmd = f'k{addr:02X}'.encode()
    send_serial(cmd)
    time.sleep(0.5)
    lines = read_serial(timeout=1)
    for l in lines:
        if 'Key scan' in l or 'addr' in l:
            print(f"  ESP: {l}")

def inject_key(val):
    """Inject a key via 'x' command."""
    cmd = f'x{val:02X}'.encode()
    send_serial(cmd)

def main():
    os.makedirs(OUTDIR, exist_ok=True)

    print("=== Key Scan Address Test ===")
    print("Initializing OCR...")
    get_ocr()
    print("OCR ready.")

    # Configure serial
    subprocess.run(['stty', '-F', PORT, '115200', 'raw', '-echo', '-hupcl'],
                   check=True, capture_output=True)

    # Wait for ESP to settle
    print("Waiting for ESP to settle...")
    time.sleep(3)

    # Check STB state
    sz = snap_size()
    print(f"HDMI frame: {sz} bytes")
    if sz < 7500:
        print("STB is off (black frame). Wait for it to turn on.")
        return

    # Read addresses seen by the STB
    # Known from observation: 0x3F, 0x4F, 0x7F, 0x9F, 0xFF
    test_addrs = [0x00, 0x3F, 0x4F, 0x7F, 0x9F, 0xFF]
    # 0x00 = ALL mode (baseline — should work if anything works)

    test_key = 0x5F  # MENU — confirmed from first sweep

    results = []
    for addr in test_addrs:
        label = "ALL" if addr == 0x00 else f"0x{addr:02X}"
        print(f"\n--- Testing key scan addr: {label} ---")

        # Set key scan address
        set_key_addr(addr)
        time.sleep(0.5)

        # Take baseline
        pre_img = snap_raw()
        pre_size = snap_size()
        pre_osd = find_osd_text(run_ocr(pre_img), pre_img.shape[0]) if pre_img is not None else []

        # Inject MENU key
        print(f"  Injecting 0x{test_key:02X}...")
        inject_key(test_key)

        # Wait for STB response
        time.sleep(2.5)

        # Check result
        post_img = snap_raw()
        post_size = snap_size()
        post_osd = find_osd_text(run_ocr(post_img), post_img.shape[0]) if post_img is not None else []

        # Read serial for WRITEs
        serial_lines = read_serial(timeout=0.5)
        writes = [l for l in serial_lines if 'WRITE' in l]

        new_osd = [t for t in post_osd if t not in pre_osd]
        size_delta = abs(post_size - pre_size)

        hit = bool(new_osd) or len(writes) > 0
        status = "HIT!" if hit else "no change"
        print(f"  Result: {status}")
        print(f"    Size: {pre_size} -> {post_size} (Δ{size_delta})")
        print(f"    Pre OSD: {pre_osd[:3]}")
        print(f"    Post OSD: {post_osd[:3]}")
        if new_osd:
            print(f"    NEW OSD: {new_osd}")
        if writes:
            for w in writes:
                print(f"    WRITE: {w}")

        results.append((addr, label, hit, new_osd, len(writes), size_delta))

        if hit:
            # Save screenshot
            if post_img is not None:
                cv2.imwrite(os.path.join(OUTDIR, f'HIT_addr{label}.jpg'), post_img)

        # Wait for any OSD to dismiss
        if hit:
            print("  Waiting 10s for OSD to dismiss...")
            time.sleep(10)
        else:
            time.sleep(2)

    # Summary
    print(f"\n{'='*60}")
    print("=== RESULTS: Key Scan Address Test ===")
    for addr, label, hit, osd, nw, delta in results:
        marker = " *** HIT ***" if hit else ""
        print(f"  {label}: osd={osd} writes={nw} delta={delta}{marker}")
    print(f"{'='*60}")

    # Reset to ALL mode
    set_key_addr(0x00)

if __name__ == '__main__':
    main()
