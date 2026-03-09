#!/usr/bin/env python3
"""Menu navigation probe: opens menu with 0x5F, then tries key codes to find
UP, DOWN, LEFT, RIGHT, OK, EXIT.

Uses stty -hupcl + persistent FDs. ALL mode, hold=10 (sweet spot).
Compares screenshots before/after each key to detect navigation changes.

Usage: .venv/bin/python menu_probe.py [start_hex end_hex]
"""
import time
import subprocess
import os
import sys
import cv2
import numpy as np

PORT = '/dev/ttyACM0'
SNAP_URL = 'http://localhost:8888/snapshot'
OUTDIR = '/tmp/menu_probe'
RESULTS_FILE = '/tmp/menu_probe_results.txt'
MENU_KEY = 0x5F
HOLD = 10  # sweet spot: opens menu reliably without key-repeat toggle

# Known keys to skip
SKIP_KEYS = {0x5F, 0x77}  # MENU (toggles), POWER (standby)

class SerialPort:
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

    def drain(self, timeout=0.5):
        buf = b''
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                data = os.read(self._read_fd, 4096)
                if data:
                    buf += data
            except OSError:
                pass
            time.sleep(0.02)
        return buf.decode('utf-8', errors='replace')

    def cmd(self, s, wait=0.3):
        self.write(s)
        time.sleep(wait)
        return self.drain(0.3)

    def close(self):
        if self._write_fd is not None:
            os.close(self._write_fd)
        if self._read_fd is not None:
            os.close(self._read_fd)

def snap():
    path = '/tmp/_snap_probe.jpg'
    try:
        subprocess.run(['curl', '-s', '-o', path, SNAP_URL, '--max-time', '3'],
                       capture_output=True, timeout=5)
    except:
        return None
    if not os.path.exists(path):
        return None
    return cv2.imread(path)

def get_menu_roi(img):
    """Extract the menu content area only (white box with text, excluding TV background).
    The menu occupies roughly the center of screen with a white background."""
    if img is None:
        return None
    h, w = img.shape[:2]
    # Menu box: approximately 9%-87% width, 12%-85% height
    # This covers the white content area (text items) but excludes
    # the transparent edges where TV bleeds through
    return img[int(h*0.13):int(h*0.82), int(w*0.10):int(w*0.86)]

def white_pct(img):
    """Percentage of bright white pixels in menu ROI — high = menu visible."""
    roi = get_menu_roi(img)
    if roi is None:
        return 0
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    return np.count_nonzero(gray > 200) / gray.size * 100

def menu_diff_score(img1, img2):
    """Compare ONLY the menu content area, masking out TV background.
    Uses the white menu area as a mask — only compares pixels where the
    menu is opaque (bright), ignoring transparent TV bleed-through."""
    if img1 is None or img2 is None:
        return 999
    if img1.shape != img2.shape:
        img2 = cv2.resize(img2, (img1.shape[1], img1.shape[0]))
    # Get menu ROI from both images
    roi1 = get_menu_roi(img1)
    roi2 = get_menu_roi(img2)
    if roi1 is None or roi2 is None:
        return 999
    g1 = cv2.cvtColor(roi1, cv2.COLOR_BGR2GRAY)
    g2 = cv2.cvtColor(roi2, cv2.COLOR_BGR2GRAY)
    # Create mask: pixels that are bright (part of menu, not TV) in EITHER image
    # Menu text/icons are dark on white, so we mask to white areas (>180)
    # and also include the teal/blue areas (like dialog boxes, highlights)
    mask1 = g1 > 140  # menu area is generally bright
    mask2 = g2 > 140
    menu_mask = mask1 | mask2
    if np.count_nonzero(menu_mask) < 100:
        return 0  # no menu area found
    # Compare only within the menu mask
    diff = cv2.absdiff(g1, g2)
    masked_diff = diff[menu_mask]
    # Count pixels with significant change (>25 intensity)
    changed = np.count_nonzero(masked_diff > 25)
    return changed / len(masked_diff) * 100

def is_menu_visible(img):
    return white_pct(img) > 15

def main():
    os.makedirs(OUTDIR, exist_ok=True)
    log = open(RESULTS_FILE, 'w')

    def logprint(msg):
        print(msg, flush=True)
        log.write(msg + '\n')
        log.flush()

    # Parse range
    start_val = 0x40
    end_val = 0x7F
    if len(sys.argv) >= 3:
        start_val = int(sys.argv[1], 16)
        end_val = int(sys.argv[2], 16)

    logprint(f"=== Menu Navigation Probe {time.strftime('%H:%M:%S')} ===")
    logprint(f"Range: 0x{start_val:02X} to 0x{end_val:02X}")
    logprint(f"Menu key: 0x{MENU_KEY:02X}, Hold: {HOLD}")

    ser = SerialPort(PORT)
    ser.open()
    logprint("Serial opened.")

    # Drain old data
    ser.drain(1.0)

    # Set ALL mode and hold=10
    ser.cmd('k00', 0.5)
    ser.cmd(f'n{HOLD:02X}', 0.5)
    logprint(f"ALL mode, hold={HOLD}")

    hits = []
    consecutive_no_menu = 0

    for val in range(start_val, end_val + 1):
        if val in SKIP_KEYS:
            logprint(f"[0x{val:02X}] SKIP (known key)")
            continue

        # Step 1: Ensure menu is open
        # Check if menu is already showing
        pre_check = snap()
        if pre_check is not None and is_menu_visible(pre_check):
            logprint(f"[0x{val:02X}] Menu already open")
            menu_img = pre_check
        else:
            # Open menu
            ser.cmd('x5F', 0.2)
            time.sleep(1.5)
            menu_img = snap()
            if menu_img is None or not is_menu_visible(menu_img):
                # Retry
                time.sleep(1)
                ser.cmd('x5F', 0.2)
                time.sleep(1.5)
                menu_img = snap()

            if menu_img is None or not is_menu_visible(menu_img):
                consecutive_no_menu += 1
                wp = white_pct(menu_img) if menu_img is not None else 0
                logprint(f"[0x{val:02X}] FAIL open menu (white={wp:.1f}%, consecutive={consecutive_no_menu})")
                if consecutive_no_menu >= 5:
                    logprint("  5 consecutive failures, aborting.")
                    break
                continue

        consecutive_no_menu = 0

        # Step 2: Inject test key
        ser.cmd(f'x{val:02X}', 0.2)
        time.sleep(1.5)

        # Step 3: Take post-key screenshot
        post_img = snap()
        if post_img is None:
            logprint(f"[0x{val:02X}] No post screenshot")
            continue

        # Step 4: Compare menu content area only (ignores TV background)
        diff = menu_diff_score(menu_img, post_img)
        menu_still = is_menu_visible(post_img)
        wp_post = white_pct(post_img)

        if diff > 5.0:  # significant menu content change (>5%)
            if menu_still:
                kind = "NAV"  # navigated within menu
            else:
                kind = "EXIT"  # left menu
            logprint(f"[0x{val:02X}] *** HIT *** diff={diff:.1f}% type={kind} white={wp_post:.1f}%")
            cv2.imwrite(os.path.join(OUTDIR, f'HIT_{val:02X}_pre.jpg'), menu_img)
            cv2.imwrite(os.path.join(OUTDIR, f'HIT_{val:02X}_post.jpg'), post_img)
            hits.append((val, diff, kind, wp_post))

            if not menu_still:
                time.sleep(2)  # let TV settle before reopening
        else:
            if val % 8 == 0:
                logprint(f"[0x{val:02X}] ok diff={diff:.1f}%")
            # Menu might have timed out
            if not menu_still:
                logprint(f"[0x{val:02X}] menu timed out (diff={diff:.1f}%)")
                time.sleep(1)

        time.sleep(0.3)

    # Summary
    logprint(f"\n{'='*60}")
    logprint(f"=== MENU PROBE RESULTS ===")
    logprint(f"Range: 0x{start_val:02X}-0x{end_val:02X}")
    logprint(f"Total hits: {len(hits)}")
    for v, d, k, wp in hits:
        logprint(f"  0x{v:02X}: diff={d:.1f}% type={k} white={wp:.1f}%")
    if not hits:
        logprint("  No hits found!")
    logprint(f"{'='*60}")

    log.close()
    ser.close()

if __name__ == '__main__':
    main()
