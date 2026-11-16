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
FRAME_PATH = "/dev/shm/dmr39.jpg"
RESTART_EVERY = 24 * 3600

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
            "ffmpeg", "-y",
            "-init_hw_device", "vaapi=va:/dev/dri/renderD128",
            # wallclock on video too: otherwise ffmpeg drops the v4l2 timestamps,
            # counts frames, and drifts vs the wallclock-stamped ALSA audio (~3 s after 3 months)
            "-use_wallclock_as_timestamps", "1", "-thread_queue_size", "64",
            "-f", "v4l2", "-input_format", "mjpeg",
            "-video_size", f"{WIDTH}x{HEIGHT}", "-framerate", str(FPS),
            "-i", DEVICE,
            "-thread_queue_size", "1024", "-f", "alsa", "-i", AUDIO_DEV,
            # output 1: HLS with audio (watch mode)
            "-map", "0:v", "-map", "1:a",
            "-vf", "format=nv12,hwupload",
            "-af", "aresample=async=1000",  # smooth ALSA timestamp jitter (was +-3 ms steps -> clicks)
            "-c:v", "h264_vaapi", "-g", str(FPS),  # keyframe every 1 s = real 1 s segments
            "-qp", "23",
            "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "48000",
            "-f", "hls",
            "-hls_time", "1",
            "-hls_list_size", "5",
            "-hls_flags", "delete_segments+independent_segments",
            "-hls_start_number_source", "epoch",  # sequence numbers stay unique across restarts
            "-hls_segment_filename", os.path.join(HLS_DIR, "seg%03d.ts"),
            os.path.join(HLS_DIR, "stream.m3u8"),
            # output 2: latest camera JPEG, stream copy (remote mode, no audio, ~0 CPU)
            "-map", "0:v", "-c:v", "copy",
            "-f", "image2", "-update", "1", "-atomic_writing", "1", FRAME_PATH,
        ], stderr=subprocess.DEVNULL)
        try:
            # ponytail: daily restart bounds any residual A/V drift; drop once wallclock fix proves out
            proc.wait(timeout=RESTART_EVERY)
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait()
        print(f"ffmpeg exited with code {proc.returncode}, restarting in 3s...")
        time.sleep(3)

def make_page(nonce):
    return f"""<!DOCTYPE html>
<html><head>
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>DMR-39 TV</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{height:100%}}
body{{background:#000;font-family:system-ui,sans-serif;overflow:hidden}}
#v,#mj{{position:fixed;inset:0;width:100%;height:100%;object-fit:contain;background:#000}}
#mj{{display:none}}
#fab{{position:fixed;top:12px;left:12px;width:48px;height:48px;border-radius:50%;background:rgba(40,40,40,.8);box-shadow:0 0 10px rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;cursor:pointer;z-index:10;touch-action:none;user-select:none;-webkit-tap-highlight-color:transparent}}
#fab.on{{background:rgba(30,90,160,.9)}}
#fsb{{position:fixed;top:68px;left:12px;width:48px;height:48px;border-radius:50%;background:rgba(40,40,40,.8);box-shadow:0 0 10px rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;cursor:pointer;z-index:10;user-select:none;-webkit-tap-highlight-color:transparent}}
#rc{{position:fixed;top:12px;right:12px;bottom:12px;width:236px;background:rgba(26,26,26,.92);border-radius:14px;overflow-y:auto;padding:8px;display:none;flex-direction:column;gap:3px;z-index:9}}
#rc.open{{display:flex}}
#hd{{display:flex;justify-content:space-between;align-items:center;color:#8ab;font-size:10px;padding:2px 2px 4px}}
#cl{{color:#aaa;font-size:16px;cursor:pointer;padding:0 6px}}
#mute{{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:rgba(0,0,0,.7);color:#ddd;font-size:12px;padding:6px 14px;border-radius:16px;cursor:pointer;z-index:8}}
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
@media(max-width:700px){{
  #rc{{top:auto;left:0;right:0;bottom:0;width:auto;max-height:62vh;max-height:62dvh;border-radius:14px 14px 0 0}}
  #rc>*{{width:100%;max-width:280px;margin-left:auto;margin-right:auto}}
  #mj{{bottom:auto;height:38vh;height:38dvh}}
}}
</style>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.20/dist/hls.min.js"></script>
</head><body>
<video id="v" autoplay muted playsinline></video>
<img id="mj" alt="">
<div id="fab" title="Remote"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#ddd" stroke-width="2"><rect x="7" y="2" width="10" height="20" rx="3"/><circle cx="12" cy="7" r="1.5"/><path d="M10 12h4M10 15h4M10 18h4"/></svg></div>
<div id="fsb" title="Full screen"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#ddd" stroke-width="2"><path d="M4 9V4h5M15 4h5v5M20 15v5h-5M9 20H4v-5"/></svg></div>
<div id="mute">Tap for sound</div>
<div id="rc">
<div id="hd"><span>FAST view &middot; no audio</span><span id="cl">&times;</span></div>
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
<script>
var T='{nonce}', SRC='/hls/stream.m3u8';
var v=document.getElementById('v'), mj=document.getElementById('mj'), fab=document.getElementById('fab'),
    rc=document.getElementById('rc'), L=document.getElementById('L'), mute=document.getElementById('mute'), hls=null;
function s(c,retried){{
 L.textContent='...';
 fetch('/ir?code='+c+'&t='+T).then(function(r){{
  if(r.status===403&&!retried)  // nonce gone (server restart or 1 h TTL): get a fresh one, retry once
   return fetch('/nonce').then(function(n){{return n.text()}}).then(function(n){{T=n;s(c,true)}});
  return r.text().then(function(t){{L.textContent=t}});
 }}).catch(function(){{L.textContent='ERR'}});
}}
function live(){{
 if(window.Hls&&Hls.isSupported()){{
  if(!hls){{
   hls=new Hls({{liveSyncDurationCount:2,liveMaxLatencyDurationCount:4}});
   hls.attachMedia(v);
   hls.on(Hls.Events.ERROR,function(e,d){{if(d.fatal)setTimeout(function(){{hls.loadSource(SRC)}},3000)}});
  }}
  hls.loadSource(SRC);
 }}else v.src=SRC;
 v.play().catch(function(){{}});
}}
// Fast view: read the multipart stream ourselves (Chromium's <img> multipart stalls),
// show the newest JPEG, reconnect if the stream ends while the remote is open.
var ac=null, prevUrl=null;
function mjStop(){{ if(ac){{ac.abort();ac=null}} }}
function mjStart(){{
 mjStop(); var my=ac=new AbortController();
 fetch('/mjpeg?raw',{{signal:my.signal}}).then(function(r){{
  var rd=r.body.getReader(), buf=new Uint8Array(0), dec=new TextDecoder('latin1');
  function pump(){{ return rd.read().then(function(x){{
   if(x.done)return;
   var b=new Uint8Array(buf.length+x.value.length); b.set(buf); b.set(x.value,buf.length); buf=b;
   var frame=null;
   for(;;){{
    var m=/Content-Length: (\\d+)\\r\\n\\r\\n/.exec(dec.decode(buf.subarray(0,Math.min(buf.length,200))));
    if(!m)break; var st=m.index+m[0].length, n=+m[1];
    if(buf.length<st+n)break;
    frame=buf.slice(st,st+n); buf=buf.slice(st+n);
   }}
   if(frame){{ var u=URL.createObjectURL(new Blob([frame],{{type:'image/jpeg'}})); mj.src=u; if(prevUrl)URL.revokeObjectURL(prevUrl); prevUrl=u; }}
   return pump();
  }}); }}
  return pump();
 }}).catch(function(){{}}).then(function(){{ setTimeout(function(){{ if(ac===my&&rc.classList.contains('open'))mjStart() }},500) }});
}}
function setOpen(open){{
 rc.classList.toggle('open',open); fab.classList.toggle('on',open);
 if(open){{  // remote mode: ~1 s button-to-picture; pause HLS so it stops downloading
  if(hls)hls.stopLoad(); v.pause(); v.style.display='none';
  mj.style.display='block'; mute.style.display='none'; mjStart();
 }}else{{    // watch mode: HLS with audio, rejoin at live edge
  mjStop(); mj.style.display='none'; v.style.display='block';
  live(); mute.style.display=v.muted?'block':'none';
 }}
}}
document.getElementById('cl').onclick=function(){{setOpen(false)}};
// full screen: whole page where the browser allows it (desktop, Android, iPad);
// iPhone only allows the native video player, so drop to watch mode and use that
var fsb=document.getElementById('fsb'), de=document.documentElement;
function placeFsb(){{ fsb.style.left=fab.offsetLeft+'px'; fsb.style.top=(fab.offsetTop+56)+'px'; }}
fsb.onclick=function(){{
 if(document.fullscreenElement||document.webkitFullscreenElement){{
  (document.exitFullscreen||document.webkitExitFullscreen).call(document);
 }}else if(document.fullscreenEnabled||document.webkitFullscreenEnabled){{
  (de.requestFullscreen||de.webkitRequestFullscreen).call(de);
 }}else if(v.webkitEnterFullscreen){{
  if(rc.classList.contains('open')){{ setOpen(false); return; }}  // video must be loaded first
  try{{ v.webkitEnterFullscreen(); }}catch(e){{}}
 }}
}};
mute.onclick=function(){{v.muted=false;v.play();mute.style.display='none'}};
// draggable FAB (position remembered), tap toggles the remote
(function(){{
 var sx,sy,ox,oy,moved=false,down=false;
 try{{var p=JSON.parse(localStorage.getItem('dmr39-fab'));if(p){{fab.style.left=p[0]+'px';fab.style.top=p[1]+'px'}}}}catch(e){{}}
 placeFsb();
 fab.addEventListener('pointerdown',function(e){{down=true;moved=false;sx=e.clientX;sy=e.clientY;ox=fab.offsetLeft;oy=fab.offsetTop;fab.setPointerCapture(e.pointerId)}});
 fab.addEventListener('pointermove',function(e){{
  if(!down)return; var dx=e.clientX-sx, dy=e.clientY-sy;
  if(!moved&&Math.abs(dx)+Math.abs(dy)<6)return; moved=true;
  fab.style.left=Math.max(0,Math.min(innerWidth-48,ox+dx))+'px';
  fab.style.top=Math.max(0,Math.min(innerHeight-104,oy+dy))+'px'; placeFsb();
 }});
 fab.addEventListener('pointerup',function(){{
  down=false;
  if(moved){{try{{localStorage.setItem('dmr39-fab',JSON.stringify([fab.offsetLeft,fab.offsetTop]))}}catch(e){{}}}}
  else setOpen(!rc.classList.contains('open'));
 }});
}})();
setOpen(false);
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

        if self.path.startswith("/mjpeg"):
            self.send_response(200)
            # the page reads ?raw with fetch(); WebKit never hands a multipart body to fetch readers
            raw = "raw" in self.path
            self.send_header("Content-Type", "application/octet-stream" if raw
                             else "multipart/x-mixed-replace; boundary=f")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            last = 0
            end = time.time() + 600  # cap: a leaked stream can't hold a thread forever
            self.connection.settimeout(10)  # a client that stops reading can't block write() forever
            try:
                while time.time() < end:
                    try:
                        m = os.stat(FRAME_PATH).st_mtime_ns
                        if m != last:
                            with open(FRAME_PATH, "rb") as f:
                                data = f.read()
                            last = m
                            self.wfile.write(b"--f\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                             + str(len(data)).encode() + b"\r\n\r\n" + data + b"\r\n")
                    except FileNotFoundError:
                        pass
                    time.sleep(0.03)
            except OSError:  # broken pipe, reset, write timeout
                pass
            return

        if self.path == "/nonce":
            # no CORS header, so other sites can't read it: same protection as the nonce in the page
            data = issue_nonce().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
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
