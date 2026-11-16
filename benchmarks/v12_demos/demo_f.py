"""Demo F -- Privacy Egress: the firewall refuses undeclared destinations, and
the redactor strips credentials from a payload before it could be sent."""
from __future__ import annotations

import json, sys, urllib.error, urllib.request

BASE = "http://127.0.0.1:8798"
TOK = open("/tmp/vantage_probe2/api_token").read().strip()
HDR = {"X-JARVIS-Token": TOK, "Host": "127.0.0.1", "Content-Type": "application/json"}


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=HDR, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


print("== 1. what the firewall covers, and what it says it does not ==")
st, posture = call("GET", "/egress")
print("privacy mode:", posture.get("privacy_mode"))
covers = posture.get("covers") or posture.get("note") or posture.get("cannot")
print("note:", json.dumps(covers, indent=1)[:900] if covers else "(see full)")
print("declared destinations:", len(posture.get("destinations") or []))
for d in (posture.get("destinations") or [])[:6]:
    print("   ", d.get("kind"), d.get("host"))

print("\n== 2. a destination nobody declared is refused ==")
for url in ("https://api.example.com/v1/status",
            "https://hooks.slack.com/services/T/B/X",
            "http://127.0.0.1:8798/health"):
    st, v = call("POST", "/egress/check", {"url": url})
    print(f"  {url}")
    print(f"    allowed={v.get('allowed')}  {str(v.get('reason'))[:140]}")

print("\n== 3. the redactor strips credentials from a payload ==")
# This is the code path a caller uses before handing text to a transport. It is
# the same function the log and the audit chain use, so what it says about a
# payload is what would actually leave.
sys.path.insert(0, ".")
import os
os.environ.setdefault("JARVIS_HOME", "/tmp/vantage_probe2")
from jarvis.security.privacy import contains_secret, redact, redact_value

samples = {
    "a bare API key": "here is my key sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "an AWS key id": "AKIAIOSFODNN7EXAMPLE is the id",
    "a key=value pair": "api_key=abc123def456&other=1",
    "an email address": "contact alice@example.com about it",
    "a harmless sentence": "the build finished in 12 seconds",
}
for label, text in samples.items():
    out = redact(text)
    print(f"  {label}:")
    print(f"    in : {text}")
    print(f"    out: {out}")
    print(f"    contains_secret(in)={contains_secret(text)} changed={out != text}")

print("\n== 4. structural redaction: a whole request body ==")
payload = {
    "model": "some-model",
    "messages": [{"role": "user", "content": "my token is sk-live-AAAABBBBCCCCDDDDEEEE"}],
    "api_key": "should-never-leave",
    "nested": {"password": "hunter2", "note": "keep this"},
}
safe = redact_value(payload)
print(json.dumps(safe, indent=1))

print("\n== 5. the redaction is not cosmetic: the raw form really carried the key ==")
raw = json.dumps(payload)
print("raw contains the key:", "sk-live-AAAABBBBCCCCDDDDEEEE" in raw)
print("redacted contains it:", "sk-live-AAAABBBBCCCCDDDDEEEE" in json.dumps(safe))
print("redacted contains hunter2:", "hunter2" in json.dumps(safe))
print("\nDEMO_F_OK")