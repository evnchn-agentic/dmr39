"""Run: python3 server/test_nonce.py  (stdlib only, no capture hardware needed)"""
import http.client
import threading

import mjpeg_stream as m

m.ESP32_URL = "http://127.0.0.1:9"  # nothing listens: a nonce that passes gets 502, a rejected one 403

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

# after the flood a viewer still gets a working nonce
_, token = get("/nonce")
status, _ = get(f"/ir?code=A25D&t={token.decode()}")
assert status == 502, status
assert get("/ir?code=A25D&t=bogus")[0] == 403
print("ok")
