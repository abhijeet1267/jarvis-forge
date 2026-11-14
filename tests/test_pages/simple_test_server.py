#!/usr/bin/env python3
"""Simple HTTP server for browser automation tests."""
import http.server
import socketserver
import os
import threading
import time

PORT = 8765
TEST_DIR = os.path.dirname(os.path.abspath(__file__))


def run_server():
    """Start simple HTTP server serving from test_pages directory."""
    os.chdir(TEST_DIR)
    with socketserver.TCPServer(("", PORT), http.server.SimpleHTTPRequestHandler) as httpd:
        print(f"Serving on port {PORT}")
        httpd.serve_forever()


if __name__ == "__main__":
    # Run in background thread if imported
    if __name__ != "__main__":
        t = threading.Thread(target=run_server, daemon=True)
        t.start()
        time.sleep(0.5)  # Give server time to start
    else:
        run_server()
