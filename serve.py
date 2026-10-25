"""Serve dist/ with the production security headers, for local and LAN testing.

    python serve.py [--port 8765] [--host 0.0.0.0]

Static files only: nothing here executes user input.
"""
import argparse
import functools
import http.server
import json
import os
import socket

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, "dist")


def load_rules():
    with open(os.path.join(DIST, "headers.json")) as f:
        return json.load(f)


class Handler(http.server.SimpleHTTPRequestHandler):
    rules = []
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript",
        ".svg": "image/svg+xml",
        ".whl": "application/zip",
        ".json": "application/json",
    }

    def end_headers(self):
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        for pattern, headers in self.rules:
            if pattern == "/*" or pattern == path:
                for k, v in headers.items():
                    self.send_header(k, v)
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def list_directory(self, path):
        self.send_error(404)

    def log_message(self, fmt, *args):
        pass


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()
    Handler.rules = load_rules()
    handler = functools.partial(Handler, directory=DIST)
    server = http.server.ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Serving {DIST}")
    print(f"  this machine: http://localhost:{args.port}/")
    ip = lan_ip()
    if ip and args.host in ("0.0.0.0", ""):
        print(f"  on your LAN:  http://{ip}:{args.port}/")
    server.serve_forever()


if __name__ == "__main__":
    main()
