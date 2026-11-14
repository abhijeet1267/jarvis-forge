import http.server
import os
import time

BASE = "/tmp/cc_harness"
MODE = os.path.join(BASE, "mode")
PORTF = os.path.join(BASE, "port")

def cur_mode():
    try:
        with open(MODE) as f:
            return f.read().strip()
    except Exception:
        return "success"

OK_CMD = b'{"reply":"The sun is a star.","model_used":"harness-model","intent":"general","decision":"EXECUTE","session_id":"SESS-harness"}'
PROVIDER_FAIL = b'{"detail":"No active credentials for provider: openai"}'
AUTH_FAIL = b''
MALFORMED = b'{"reply": "truncated, not valid json'

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        if self.path != "/command":
            self.send_response(404); self.end_headers(); return
        length = int(self.headers.get("Content-Length") or 0)
        _ = self.rfile.read(length)  # drain body
        m = cur_mode()
        if m == "slow9":
            # A model turn slower than APIClient's 8s request timeout. The old
            # config killed it in transport before the turn's own budget ran.
            time.sleep(9)
            body = OK_CMD
            self.send_response(200)
        elif m == "provider_fail":
            body = PROVIDER_FAIL
            self.send_response(503)
        elif m == "auth_fail":
            body = AUTH_FAIL
            self.send_response(401)
        elif m == "malformed":
            body = MALFORMED
            self.send_response(200)
        else:  # success
            body = OK_CMD
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

def do_GET(self):
    self.send_response(404); self.end_headers()

def main():
    os.makedirs(BASE, exist_ok=True)
    with open(MODE, "w") as f:
        f.write("success")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    with open(PORTF, "w") as f:
        f.write(str(port))
    print(f"cc-backend-on-port {port}", flush=True)
    srv.serve_forever()

if __name__ == "__main__":
    main()
