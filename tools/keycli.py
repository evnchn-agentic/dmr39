#!/usr/bin/env python3
"""Interactive key injection CLI with HDMI screenshot feedback.

Firmware uses press+release automatically (bit 6 = pressed flag).
All key commands send press code; firmware handles release.

Commands:
  +            CH+ (0x4F)
  -            CH- (0x47)
  m            MENU (0x5F)
  o            OK (0x57)
  p            POWER (0x77)
  x<HH>       Inject raw key code (e.g. x5F for MENU)
  n            Inject next value in scan sequence
  scan <S> <E> Set scan range (e.g. scan 40 7F), resets cursor to start
  h<HH>       Set hold count (e.g. h0A for 10)
  snap         Take screenshot and save to /tmp/keycli/
  mark <text>  Save current screenshot with a label
  note <text>  Log a text observation
  log          Show observation log
  s            ESP status
  q            Quit

Usage: .venv/bin/python keycli.py
"""
import os
import sys
import time
import subprocess
import readline

PORT = '/dev/ttyACM0'
SNAP_URL = 'http://localhost:8888/snapshot'
OUTDIR = '/tmp/keycli'
LOGFILE = '/tmp/keycli/observations.log'

class SerialPort:
    def __init__(self, port):
        self.port = port
        self._wfd = None
        self._rfd = None

    def open(self):
        subprocess.run(['stty', '-F', self.port, '115200', 'raw', '-echo', '-hupcl'],
                       check=True, capture_output=True)
        self._wfd = os.open(self.port, os.O_WRONLY)
        self._rfd = os.open(self.port, os.O_RDONLY | os.O_NONBLOCK)

    def cmd(self, s, wait=0.3):
        os.write(self._wfd, s.encode() if isinstance(s, str) else s)
        time.sleep(wait)
        return self.drain(0.3)

    def drain(self, timeout=0.5):
        buf = b''
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                d = os.read(self._rfd, 4096)
                if d: buf += d
            except OSError:
                pass
            time.sleep(0.02)
        return buf.decode('utf-8', errors='replace')

    def close(self):
        if self._wfd is not None:
            os.close(self._wfd)
        if self._rfd is not None:
            os.close(self._rfd)

snap_counter = [0]
scan_cursor = [0x40]  # current scan position
scan_end = [0x7F]     # scan range end (inclusive)
SKIP_KEYS = {0x4F, 0x47, 0x57, 0x5F, 0x77}  # known keys to skip during scan

def take_snap(label=None):
    snap_counter[0] += 1
    name = f'{snap_counter[0]:03d}'
    if label:
        safe = label.replace(' ', '_').replace('/', '-')[:40]
        name += f'_{safe}'
    path = os.path.join(OUTDIR, f'{name}.jpg')
    try:
        subprocess.run(['curl', '-s', '-o', path, SNAP_URL, '--max-time', '3'],
                       capture_output=True, timeout=5)
        sz = os.path.getsize(path) if os.path.exists(path) else 0
        print(f'  Saved: {path} ({sz} bytes)')
        return path
    except Exception as e:
        print(f'  Snap failed: {e}')
        return None

def log_observation(text):
    ts = time.strftime('%H:%M:%S')
    line = f'[{ts}] {text}'
    print(f'  Logged: {line}')
    with open(LOGFILE, 'a') as f:
        f.write(line + '\n')

def show_log():
    if os.path.exists(LOGFILE):
        with open(LOGFILE, 'r') as f:
            print(f.read())
    else:
        print('  No observations yet.')

def show_serial_display(ser):
    """Drain serial and print any DISP: lines."""
    output = ser.drain(0.3)
    for line in output.split('\n'):
        line = line.strip()
        if line.startswith('DISP:') or line.startswith('[') and 'DISP:' in line:
            print(f'  {line}')

def main():
    os.makedirs(OUTDIR, exist_ok=True)

    ser = SerialPort(PORT)
    print('Opening serial...', end=' ', flush=True)
    ser.open()
    print('OK')

    # Drain + setup
    ser.drain(1.0)
    ser.cmd('k4F', 0.5)  # 0x4F-only mode (no register pollution)
    ser.cmd('n02', 0.5)  # hold=2 (0x4F read ~once per 2.4s cycle, so 2 reads ≈ 5s press)
    print('Config: 0x4F-only mode, hold=2, press+release auto')
    print(f'Screenshots: {OUTDIR}')
    print(f'Log: {LOGFILE}')
    print()
    print('Commands: +=CH+ -=CH- m=menu o=ok p=power x<HH>=raw n=next scan h<HH>=hold snap mark note log s q')
    print(f'Scan range: 0x{scan_cursor[0]:02X}-0x{scan_end[0]:02X} (skips: {", ".join(f"0x{k:02X}" for k in sorted(SKIP_KEYS))})')
    print()

    while True:
        try:
            line = input('> ').strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not line:
            continue

        parts = line.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ''

        if cmd == 'q' or cmd == 'quit':
            break

        elif cmd == '+' or cmd == 'ch+':
            ser.cmd('+', 0.2)
            print(f'  CH+ (0x4F→0x0F)')
            log_observation('CH+ (0x4F)')
            time.sleep(1)
            show_serial_display(ser)
            take_snap('ch_plus')

        elif cmd == '-' or cmd == 'ch-':
            ser.cmd('-', 0.2)
            print(f'  CH- (0x47→0x07)')
            log_observation('CH- (0x47)')
            time.sleep(1)
            show_serial_display(ser)
            take_snap('ch_minus')

        elif cmd == 'm' or cmd == 'menu':
            ser.cmd('m', 0.2)
            print(f'  MENU (0x5F→0x1F)')
            log_observation('MENU (0x5F)')
            time.sleep(1)
            take_snap('menu')

        elif cmd == 'o' or cmd == 'ok':
            ser.cmd('o', 0.2)
            print(f'  OK (0x57→0x17)')
            log_observation('OK (0x57)')
            time.sleep(1)
            take_snap('ok')

        elif cmd == 'p' or cmd == 'power':
            ser.cmd('p', 0.2)
            print(f'  POWER (0x77→0x37)')
            log_observation('POWER (0x77)')
            time.sleep(1)
            take_snap('power')

        elif cmd == 'n' or cmd == 'next':
            # Skip known keys
            while scan_cursor[0] in SKIP_KEYS and scan_cursor[0] <= scan_end[0]:
                print(f'  Skip 0x{scan_cursor[0]:02X} (known)')
                scan_cursor[0] += 1
            if scan_cursor[0] > scan_end[0]:
                print(f'  Scan complete! Use "scan <S> <E>" to reset.')
            else:
                val = scan_cursor[0]
                ser.cmd(f'x{val:02X}', 0.2)
                release = val & ~0x40
                print(f'  Inject 0x{val:02X}→0x{release:02X}  next=0x{min(val+1, 0xFF):02X}')
                log_observation(f'SCAN 0x{val:02X}')
                time.sleep(1)
                take_snap(f'scan_{val:02X}')
                scan_cursor[0] += 1

        elif cmd == 'scan':
            try:
                parts2 = arg.split()
                if len(parts2) >= 2:
                    s = int(parts2[0], 16)
                    e = int(parts2[1], 16)
                    scan_cursor[0] = s
                    scan_end[0] = e
                    print(f'  Scan range set: 0x{s:02X}-0x{e:02X} ({e - s + 1} values)')
                else:
                    print(f'  Current: 0x{scan_cursor[0]:02X}-0x{scan_end[0]:02X}, cursor at 0x{scan_cursor[0]:02X}')
                    print(f'  Usage: scan <start_hex> <end_hex>')
            except ValueError:
                print(f'  Bad hex values')

        elif cmd.startswith('x') and len(cmd) == 3:
            hex_part = cmd[1:3]
            try:
                val = int(hex_part, 16)
                ser.cmd(f'x{val:02X}', 0.2)
                release = val & ~0x40
                print(f'  Inject 0x{val:02X}→0x{release:02X}')
                log_observation(f'Inject 0x{val:02X}')
                time.sleep(1)
                take_snap(f'key_{val:02X}')
            except ValueError:
                print(f'  Bad hex: {hex_part}')

        elif cmd.startswith('h') and len(cmd) == 3:
            try:
                val = int(cmd[1:], 16)
                ser.cmd(f'n{val:02X}', 0.3)
                print(f'  Hold set to {val} (0x{val:02X})')
            except ValueError:
                print(f'  Bad hex: {cmd[1:]}')

        elif cmd == 'd' or cmd == 'disp' or cmd == 'display':
            resp = ser.cmd('d', 0.5)
            for l in resp.strip().split('\n'):
                l = l.strip()
                if l:
                    print(f'  {l}')

        elif cmd == 'snap':
            take_snap(arg if arg else None)

        elif cmd == 'mark':
            if not arg:
                print('  Usage: mark <description>')
            else:
                path = take_snap(arg)
                log_observation(f'MARK: {arg}')

        elif cmd == 'note':
            if not arg:
                print('  Usage: note <observation text>')
            else:
                log_observation(f'NOTE: {arg}')

        elif cmd == 'log':
            show_log()

        elif cmd == 's' or cmd == 'status':
            resp = ser.cmd('s', 0.5)
            for l in resp.strip().split('\n'):
                l = l.strip()
                if l:
                    print(f'  {l}')

        elif cmd == 'help' or cmd == '?':
            print('Commands: +=CH+ -=CH- m=menu o=ok p=power x<HH>=raw d=display n=next scan h<HH>=hold snap mark note log s q')

        else:
            print(f'  Unknown: {cmd}. Type ? for help.')

    ser.close()
    print('Bye.')

if __name__ == '__main__':
    main()
