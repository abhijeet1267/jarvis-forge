#!/usr/bin/env python3
"""A controlled listener for backend_lifecycle.py's state machine.

The lifecycle tool decides who owns a port from two kinds of evidence: what
``/health/identity`` says, and -- when it says nothing -- what the OS knows
about the pid (uid, cwd, command line). This fixture lets a harness produce
each verdict deliberately, without touching the live backend:

* ``--identity-root`` set, ``--service vantage-backend`` -> answers identity.
  With ``--identity-root`` equal to this checkout it is ``ours``; with any
  other root it is ``other-vantage-checkout``.
* no identity at all (404): the pid is then judged by OS evidence. Started as
  ``run.py serve`` from inside the checkout it is ``legacy``; the same file
  started from another directory is ``foreign``.

It writes its chosen port to ``--port-file`` so the caller never guesses.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os


def build_handler(identity_root, service, instance, stale):
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == "/health/identity":
                if identity_root is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                body = json.dumps({
                    "identity": {
                        "service": service,
                        "instance_id": instance,
                        "pid": os.getpid(),
                        "app_version": "0.1.0",
                        "revision": "fixture",
                        "source_root": identity_root,
                    },
                    "code": {"stale": stale},
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/health":
                body = b'{"status":"ok"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

    return H


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--identity-root", default=None)
    parser.add_argument("--service", default="vantage-backend")
    parser.add_argument("--instance", default="BKT-fixture")
    parser.add_argument("--stale", action="store_true")
    parser.add_argument("--port-file", required=True)
    # Accepted so this file can masquerade as `run.py serve` in the script's
    # OS-evidence check (which looks for the launcher on the command line).
    parser.add_argument("entry", nargs="?", default=None)
    args = parser.parse_args()

    handler = build_handler(args.identity_root, args.service, args.instance, args.stale)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    port = srv.server_address[1]
    # The caller kills the server by this pid, and the pid is the fixture's
    # own -- not whatever a `cd ... && exec ... &` subshell reported.
    with open(args.port_file + ".pid", "w") as handle:
        handle.write(str(os.getpid()))
    tmp = args.port_file + ".tmp"
    with open(tmp, "w") as handle:
        handle.write(str(port))
    os.replace(tmp, args.port_file)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()