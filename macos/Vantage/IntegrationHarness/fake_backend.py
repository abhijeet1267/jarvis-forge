import http.server
import json
import os

BASE = "/tmp/bm_harness"
MODE = os.path.join(BASE, "mode")
PORTF = os.path.join(BASE, "port")
HITSF = os.path.join(BASE, "hits")
#: The source_root the fake backend claims in /health/identity. The Swift
#: driver writes it, so the "same checkout" and "different checkout" cases
#: are driven from one place.
SOURCE_ROOTF = os.path.join(BASE, "source_root")

def cur_mode():
    try:
        with open(MODE) as f:
            return f.read().strip()
    except Exception:
        return "up"

def source_root():
    try:
        with open(SOURCE_ROOTF) as f:
            return f.read().strip()
    except Exception:
        return ""

HEALTH = b'{"status":"ok","tools":154,"ai_provider":"harness"}'

def identity_payload(service="vantage-backend", stale=False):
    root = source_root()
    return json.dumps({
        "identity": {
            "service": service,
            "instance_id": "BKT-harness0001",
            "pid": os.getpid(),
            "started_at": 1793097234.5,
            "app_version": "0.1.0",
            "revision": "harnessrev+dirty",
            "branch": "main",
            "source_root": root,
            "python": "3.11.16",
        },
        "code": {
            "stale": stale,
            "started_at": 1793097234.5,
            "newest_mtime": 1793097285.4,
            "note": "harness",
        },
        "fingerprint": "0.1.0@harnessrev+dirty",
    }).encode()

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _count(self):
        try:
            with open(HITSF) as f:
                n = int(f.read().strip() or 0)
        except Exception:
            n = 0
        with open(HITSF, "w") as f:
            f.write(str(n + 1))

    def _json(self, body, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        m = cur_mode()
        # /health/identity is unauthenticated on the real backend: it answers
        # even while /health would 401, which is exactly why the client uses
        # it to identify a backend whose token is not (yet) accepted.
        if self.path == "/health/identity":
            if m in ("stale", "up"):
                self._json(identity_payload(stale=(m == "stale")))
            elif m == "foreign-folder":
                self._json(identity_payload())
            elif m == "foreign-service":
                self._json(identity_payload(service="some-other-service"))
            elif m in ("old", "404"):
                self._json(b'{"detail":"Not Found"}', status=404)
            elif m == "down":
                self.connection.close()
            else:
                # 401 / auth: identity still answers.
                self._json(identity_payload())
            return
        if self.path != "/health":
            self.send_response(404); self.end_headers(); return
        if m == "down":
            # Simulate backend gone: close without a response.
            self.connection.close()
            return
        if m == "401":
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        # Any other mode: health is healthy; what is on the port is decided by
        # /health/identity.
        self._count()
        self._json(HEALTH)

def main():
    os.makedirs(BASE, exist_ok=True)
    with open(MODE, "w") as f:
        f.write("up")
    with open(HITSF, "w") as f:
        f.write("0")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    with open(PORTF, "w") as f:
        f.write(str(port))
    print(f"backend-on-port {port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()