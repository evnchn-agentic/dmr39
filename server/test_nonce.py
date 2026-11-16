"""Run: python3 server/test_nonce.py  (stdlib only, no capture hardware needed)"""
import http.client
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import mjpeg_stream as m

esp_paths = []


class FakeEsp(BaseHTTPRequestHandler):
    def do_GET(self):
        esp_paths.append(self.path)
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass


esp = HTTPServer(("127.0.0.1", 0), FakeEsp)
threading.Thread(target=esp.serve_forever, daemon=True).start()
m.ESP32_URL = f"http://127.0.0.1:{esp.server_address[1]}"

srv = m.ThreadedHTTPServer(("127.0.0.1", 0), m.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()


def get(path):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    conn.request("GET", path)
    r = conn.getresponse()
    return r.status, r.read()


for _ in range(m.NONCE_MAX * 3):
    get("/nonce")
assert len(m.valid_nonces) <= m.NONCE_MAX, f"nonce store grew to {len(m.valid_nonces)}"

# after the flood a viewer still gets a working nonce, and the press reaches the ESP32
_, token = get("/nonce")
assert get(f"/ir?code=A25D&t={token.decode()}") == (200, b"OK")
assert esp_paths == ["/ir?code=A25D"], esp_paths
assert get("/ir?code=A25D&t=bogus")[0] == 403
print("ok")
