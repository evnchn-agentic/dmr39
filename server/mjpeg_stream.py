#!/usr/bin/env python3
"""HDMI capture streamer with synced A/V via HLS + IR remote relay.
Single ffmpeg: VAAPI H.264 + AAC -> HLS segments.
IR commands require a per-session nonce to prevent unauthorized use.
"""
import os
import re
import secrets
import subprocess
import threading
import time
import glob
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs

DEVICE = "/dev/video0"
AUDIO_DEV = "hw:1,0"
WIDTH, HEIGHT, FPS = 1280, 720, 10
PORT = 8889
HLS_DIR = "/tmp/hdmi-hls"
ESP32_URL = "http://192.168.50.214"
NONCE_TTL = 3600  # 1 hour

# VAAPI driver: iHD for Gen9+ (Jasper Lake, etc), i965 for Ivy Bridge
if "LIBVA_DRIVER_NAME" not in os.environ:
    import socket
    _hostname = socket.gethostname()
    if "N5095" in _hostname or "Jasper" in _hostname:
        os.environ["LIBVA_DRIVER_NAME"] = "iHD"
    else:
        os.environ["LIBVA_DRIVER_NAME"] = "i965"

# Nonce store: {token: expiry_time}
nonce_lock = threading.Lock()
valid_nonces = {}

def issue_nonce():
    token = secrets.token_urlsafe(24)
    with nonce_lock:
        now = time.time()
        expired = [k for k, v in valid_nonces.items() if v < now]
        for k in expired:
            del valid_nonces[k]
        valid_nonces[token] = now + NONCE_TTL
    return token

def check_nonce(token):
    if not token:
        return False
    with nonce_lock:
        exp = valid_nonces.get(token)
        if exp and exp > time.time():
            return True
        valid_nonces.pop(token, None)
        return False

def hls_loop():
    """Run ffmpeg to produce HLS segments with muxed video+audio. Restarts on failure."""
    while True:
        os.makedirs(HLS_DIR, exist_ok=True)
        for f in glob.glob(os.path.join(HLS_DIR, "*")):
            try:
                os.remove(f)
            except FileNotFoundError:
                pass
        proc = subprocess.Popen([
            "ffmpeg",
            "-init_hw_device", "vaapi=va:/dev/dri/renderD128",
            "-f", "v4l2", "-input_format", "mjpeg",
            "-video_size", f"{WIDTH}x{HEIGHT}", "-framerate", str(FPS),
            "-i", DEVICE,
            "-f", "alsa", "-i", AUDIO_DEV,
            "-vf", "format=nv12,hwupload",
            "-c:v", "h264_vaapi", "-g", "20",
            "-qp", "23",
            "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "48000",
            "-f", "hls",
            "-hls_time", "1",
            "-hls_list_size", "5",
            "-hls_flags", "delete_segments+independent_segments",
            "-hls_segment_filename", os.path.join(HLS_DIR, "seg%03d.ts"),
            os.path.join(HLS_DIR, "stream.m3u8"),
        ], stderr=subprocess.DEVNULL)
        proc.wait()
        print(f"ffmpeg exited with code {proc.returncode}, restarting in 3s...")
        time.sleep(3)

def make_page(nonce):
    return f"""<!DOCTYPE html>
<html><head>
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<title>DMR-39 TV</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#111;font-family:system-ui,sans-serif;display:flex;height:100vh;height:100dvh;overflow:hidden}}
#tv{{flex:1;display:flex;align-items:center;justify-content:center;background:#000;min-width:0}}
video{{max-width:100%;max-height:100%;object-fit:contain}}
#rc{{width:260px;min-width:260px;background:#1a1a1a;overflow-y:auto;padding:8px;display:flex;flex-direction:column;gap:3px}}
.g3{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:4px}}
.g4{{display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:4px}}
.b{{background:#333;border:1px solid #555;border-radius:6px;color:#ccc;font-size:10px;padding:8px 2px;cursor:pointer;text-align:center;user-select:none;-webkit-tap-highlight-color:transparent;min-height:36px;display:flex;align-items:center;justify-content:center}}
.b:active{{background:#666!important}}
.pw{{background:#511;color:#f99}}.mt{{color:#f99}}
.ok{{background:#555;border-radius:50%;font-weight:bold;font-size:13px;min-height:44px}}
.dp{{font-size:16px}}.n{{border-radius:50%;font-size:14px;font-weight:bold;min-height:40px}}
.rd{{background:#922;color:#fff}}.gn{{background:#292;color:#fff}}
.yl{{background:#992;color:#fff}}.bl{{background:#229;color:#fff}}
.x{{visibility:hidden}}
.sep{{border-top:1px solid #333;margin:4px 0}}
#L{{color:#0a0;font-size:9px;text-align:center;padding:3px;min-height:14px}}
#bar{{position:fixed;bottom:0;left:0;right:260px;background:rgba(0,0,0,0.7);color:#aaa;font:11px monospace;padding:3px 8px;text-align:center;cursor:pointer}}
@media(max-width:700px){{
  body{{flex-direction:column}}
  #tv{{height:40vh;height:40dvh;min-height:200px}}
  #rc{{width:100%;max-width:280px;min-width:0;flex:1;margin:0 auto;overflow-y:auto}}
  #bar{{right:0;bottom:auto;top:0}}
}}
</style>
<script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
</head><body>
<div id="tv">
<video id="v" autoplay muted playsinline></video>
</div>
<div id="rc">
<div class="g3">
 <div class="b pw" onclick="s('9A65')">POWER</div>
 <div class="b x"></div>
 <div class="b mt" onclick="s('9867')">MUTE</div>
</div>
<div class="sep"></div>
<div class="g3">
 <div class="b" onclick="s('B24D')">EPG</div>
 <div class="b" onclick="s('708F')">INFO</div>
 <div class="b" onclick="s('B04F')">TTX</div>
 <div class="b" onclick="s('8A75')">AUDIO</div>
 <div class="b" onclick="s('48B7')">PVR</div>
 <div class="b" onclick="s('8877')">SUB</div>
 <div class="b" onclick="s('A25D')">MENU</div>
 <div class="b dp" onclick="s('609F')">&#9650;</div>
 <div class="b" onclick="s('A05F')">EXIT</div>
 <div class="b dp" onclick="s('5AA5')">&#9664;</div>
 <div class="b ok" onclick="s('58A7')">OK</div>
 <div class="b dp" onclick="s('D827')">&#9654;</div>
 <div class="b" onclick="s('AA55')">FAV</div>
 <div class="b dp" onclick="s('6897')">&#9660;</div>
 <div class="b" onclick="s('A857')">TV</div>
</div>
<div class="sep"></div>
<div class="g4">
 <div class="b n" onclick="s('4AB5')">1</div>
 <div class="b n" onclick="s('0AF5')">2</div>
 <div class="b n" onclick="s('08F7')">3</div>
 <div class="b" onclick="s('C837')">BACK</div>
 <div class="b n" onclick="s('6A95')">4</div>
 <div class="b n" onclick="s('2AD5')">5</div>
 <div class="b n" onclick="s('28D7')">6</div>
 <div class="b" onclick="s('E817')">SEEK</div>
 <div class="b n" onclick="s('728D')">7</div>
 <div class="b n" onclick="s('32CD')">8</div>
 <div class="b n" onclick="s('30CF')">9</div>
 <div class="b n" onclick="s('F00F')">0</div>
</div>
<div class="sep"></div>
<div class="g4">
 <div class="b" onclick="s('52AD')">&lt;&lt;REW</div>
 <div class="b" onclick="s('12ED')">FF&gt;&gt;</div>
 <div class="b" onclick="s('10EF')">|&#9664;</div>
 <div class="b" onclick="s('D02F')">&#9654;|</div>
 <div class="b" onclick="s('629D')">&#9654;PLAY</div>
 <div class="b" onclick="s('22DD')">&#10074;&#10074;</div>
 <div class="b" onclick="s('20DF')">&#9632;STOP</div>
 <div class="b" onclick="s('E01F')">&#8634;RPT</div>
</div>
<div class="sep"></div>
<div class="g4">
 <div class="b rd" onclick="s('42BD')">RED</div>
 <div class="b gn" onclick="s('02FD')">GREEN</div>
 <div class="b yl" onclick="s('00FF')">YELLOW</div>
 <div class="b bl" onclick="s('C03F')">BLUE</div>
</div>
<div id="L"></div>
</div>
<div id="bar">Click to unmute</div>
<script>
var T='{nonce}';
function s(c){{
 document.getElementById('L').textContent='...';
 fetch('/ir?code='+c+'&t='+T).then(function(r){{return r.text()}}).then(function(t){{document.getElementById('L').textContent=t}}).catch(function(e){{document.getElementById('L').textContent='ERR'}});
}}
var v=document.getElementById('v'), bar=document.getElementById('bar');
if(Hls.isSupported()){{
 var hls=new Hls({{liveSyncDurationCount:2,liveMaxLatencyDurationCount:4,lowLatencyMode:true}});
 hls.loadSource('/hls/stream.m3u8');
 hls.attachMedia(v);
 hls.on(Hls.Events.MANIFEST_PARSED,function(){{v.play();bar.textContent='Muted - click to unmute'}});
 hls.on(Hls.Events.ERROR,function(e,d){{bar.textContent='HLS: '+d.details;if(d.fatal)setTimeout(function(){{hls.loadSource('/hls/stream.m3u8');hls.attachMedia(v)}},3000)}});
}}else if(v.canPlayType('application/vnd.apple.mpegurl')){{v.src='/hls/stream.m3u8';v.play()}}
bar.addEventListener('click',function(){{v.muted=false;bar.textContent='Audio on'}});
</script>
</body></html>""".encode()

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/hls/"):
            fname = self.path[5:]
            fpath = os.path.realpath(os.path.join(HLS_DIR, fname))
            hls_real = os.path.realpath(HLS_DIR)
            if not fpath.startswith(hls_real + "/"):
                self.send_response(403)
                self.end_headers()
                return
            try:
                with open(fpath, "rb") as f:
                    data = f.read()
            except FileNotFoundError:
                self.send_response(404)
                self.end_headers()
                return
            if fname.endswith(".m3u8"):
                ctype = "application/vnd.apple.mpegurl"
            elif fname.endswith(".ts"):
                ctype = "video/mp2t"
            else:
                ctype = "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache" if fname.endswith(".m3u8") else "max-age=10")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
            return

        if self.path.startswith("/ir"):
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            token = qs.get("t", [None])[0]
            if not check_nonce(token):
                msg = b"403 invalid nonce"
                self.send_response(403)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
                return
            code = qs.get("code", [None])[0]
            if not code or not re.fullmatch(r'[0-9A-Fa-f]{4}', code):
                msg = b"400 invalid code"
                self.send_response(400)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
                return
            try:
                with urllib.request.urlopen(
                    f"{ESP32_URL}/ir?code={code}", timeout=5) as r:
                    data = r.read()
                    status = r.status
                    ctype = r.headers.get("Content-Type", "text/plain")
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception:
                msg = b"502 relay error"
                self.send_response(502)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
            return

        if self.path != "/":
            self.send_response(404)
            self.end_headers()
            return

        # Root: generate page with fresh nonce
        nonce = issue_nonce()
        page = make_page(nonce)
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(page)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(page)

    def log_message(self, *args):
        pass

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

if __name__ == "__main__":
    threading.Thread(target=hls_loop, daemon=True).start()
    import socket
    _ip = socket.gethostbyname(socket.gethostname()) or "0.0.0.0"
    print(f"HDMI capture: http://{_ip}:{PORT}/")
    print(f"HLS stream:   http://{_ip}:{PORT}/hls/stream.m3u8")
    ThreadedHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
