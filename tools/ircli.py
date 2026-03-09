#!/usr/bin/env python3
"""Interactive IR capture & replay CLI for DMR-39.

ESP32-C3 firmware captures IR waveforms on GPIO3 and replays on GPIO0.
This CLI provides interactive control for mapping remote buttons.

Commands:
  cap / c          Show last captured signal
  save <name>      Save last capture with name
  play <name>      Replay named code once
  play3 <name>     Replay named code 3x (with repeat gap)
  list / l         List all stored codes
  del <name>       Delete named code
  del              Delete all codes
  wave / w         Show ASCII waveform of last capture
  raw <csv>        Replay raw interval CSV
  auto             Toggle auto-capture
  status / st      Show ESP pin states
  snap             Take HDMI screenshot
  mark <text>      Screenshot + log
  note <text>      Log observation
  log              Show observation log
  q                Quit

Usage: .venv/bin/python ircli.py
"""
import os
import sys
import time
import subprocess
import readline

PORT = '/dev/ttyACM1'
SNAP_URL = 'http://localhost:8888/hls/snapshot'  # Note: snapshot endpoint removed; use HDMI capture directly if needed
OUTDIR = '/tmp/ircli'
LOGFILE = '/tmp/ircli/observations.log'

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
        line = s if s.endswith('\n') else s + '\n'
        os.write(self._wfd, line.encode())
        time.sleep(wait)
        return self.drain(0.5)

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

def print_response(resp, prefix='  '):
    """Print non-empty lines from ESP response."""
    for line in resp.strip().split('\n'):
        line = line.strip()
        if line:
            print(f'{prefix}{line}')

def wait_for_capture(ser, timeout=10):
    """Wait for a [CAP] line in serial output."""
    t0 = time.time()
    buf = ''
    while time.time() - t0 < timeout:
        try:
            d = os.read(ser._rfd, 4096)
            if d:
                buf += d.decode('utf-8', errors='replace')
                if '[CAP' in buf:
                    # Read a bit more for complete output
                    time.sleep(0.3)
                    try:
                        d = os.read(ser._rfd, 4096)
                        if d: buf += d.decode('utf-8', errors='replace')
                    except OSError:
                        pass
                    return buf
        except OSError:
            pass
        time.sleep(0.02)
    return buf

def main():
    os.makedirs(OUTDIR, exist_ok=True)

    ser = SerialPort(PORT)
    print('Opening serial...', end=' ', flush=True)
    ser.open()
    print('OK')

    # Drain startup messages
    startup = ser.drain(2.0)
    if startup.strip():
        print('--- ESP startup ---')
        print_response(startup)
        print('---')

    print(f'Screenshots: {OUTDIR}')
    print(f'Log: {LOGFILE}')
    print()
    print('Press remote buttons — captures appear automatically.')
    print('Commands: c=capture s=save p=play l=list w=wave st=status snap mark note log q=quit')
    print()

    while True:
        # Check for incoming captures while idle
        try:
            d = os.read(ser._rfd, 4096)
            if d:
                text = d.decode('utf-8', errors='replace')
                for line in text.strip().split('\n'):
                    line = line.strip()
                    if line:
                        print(f'  << {line}')
        except OSError:
            pass

        try:
            # Non-blocking input check
            import select
            if not select.select([sys.stdin], [], [], 0.1)[0]:
                continue
            line = input('> ').strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not line:
            continue

        parts = line.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ''

        if cmd in ('q', 'quit', 'exit'):
            break

        elif cmd in ('c', 'cap', 'capture'):
            resp = ser.cmd('c', 0.5)
            print_response(resp)

        elif cmd in ('s', 'save'):
            if not arg:
                print('  Usage: save <name>')
            else:
                resp = ser.cmd(f's {arg}', 0.5)
                print_response(resp)
                log_observation(f'SAVE: {arg}')

        elif cmd in ('p', 'play'):
            if not arg:
                print('  Usage: play <name>')
            else:
                resp = ser.cmd(f'p {arg}', 1.0)
                print_response(resp)
                log_observation(f'PLAY: {arg}')

        elif cmd == 'play3':
            if not arg:
                print('  Usage: play3 <name>')
            else:
                resp = ser.cmd(f'pp {arg}', 1.5)
                print_response(resp)
                log_observation(f'PLAY3: {arg}')

        elif cmd in ('l', 'list'):
            resp = ser.cmd('l', 0.5)
            print_response(resp)

        elif cmd in ('d', 'del', 'delete'):
            if arg:
                resp = ser.cmd(f'd {arg}', 0.3)
            else:
                resp = ser.cmd('d', 0.3)
            print_response(resp)

        elif cmd in ('w', 'wave'):
            resp = ser.cmd('w', 0.5)
            print_response(resp)

        elif cmd == 'raw':
            if not arg:
                print('  Usage: raw <interval1,interval2,...>')
            else:
                resp = ser.cmd(f'raw {arg}', 1.0)
                print_response(resp)

        elif cmd in ('r', 'auto'):
            resp = ser.cmd('r', 0.3)
            print_response(resp)

        elif cmd in ('st', 'status'):
            resp = ser.cmd('status', 0.5)
            print_response(resp)

        elif cmd == 'snap':
            take_snap(arg if arg else None)

        elif cmd == 'mark':
            if not arg:
                print('  Usage: mark <description>')
            else:
                take_snap(arg)
                log_observation(f'MARK: {arg}')

        elif cmd == 'note':
            if not arg:
                print('  Usage: note <observation text>')
            else:
                log_observation(f'NOTE: {arg}')

        elif cmd == 'log':
            show_log()

        elif cmd in ('?', 'help'):
            print('Commands:')
            print('  c / cap         - show last capture')
            print('  s / save <name> - save last capture')
            print('  p / play <name> - replay code')
            print('  play3 <name>    - replay 3x')
            print('  l / list        - list stored codes')
            print('  d / del [name]  - delete code(s)')
            print('  w / wave        - ASCII waveform')
            print('  raw <csv>       - replay raw intervals')
            print('  r / auto        - toggle auto-capture')
            print('  st / status     - ESP pin states')
            print('  snap            - HDMI screenshot')
            print('  mark <text>     - screenshot + log')
            print('  note <text>     - log observation')
            print('  log             - show log')
            print('  q               - quit')

        else:
            print(f'  Unknown: {cmd}. Type ? for help.')

    ser.close()
    print('Bye.')

if __name__ == '__main__':
    main()
