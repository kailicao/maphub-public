#!/usr/bin/env python3
"""Serve the viewer locally the way the published site will be laid out.

    python viewer/serve.py            # then open http://localhost:8000/

/            -> viewer/ (index.html, viewer.js, viewer.css)
/tables/...  -> tables/ at the repo root
/YYYY-MM-DD  -> viewer/index.html (the page reads the date from its URL)
"""

import argparse
import http.server
import re
import urllib.parse
from pathlib import Path

VIEWER = Path(__file__).resolve().parent
ROOT = VIEWER.parent


class Handler(http.server.SimpleHTTPRequestHandler):
    def translate_path(self, path):
        path = urllib.parse.unquote(path.split("?", 1)[0].split("#", 1)[0])
        if re.fullmatch(r"/\d{4}-\d{2}-\d{2}/?", path):
            return str(VIEWER / "index.html")
        base = ROOT / "tables" if path.startswith("/tables/") else VIEWER
        target = (ROOT / "tables" / path[len("/tables/"):] if base != VIEWER
                  else VIEWER / path.lstrip("/")).resolve()
        # Serve nothing outside viewer/ and tables/ (the repo root holds .env).
        return str(target) if target.is_relative_to(base) else str(VIEWER / "missing")

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    server = http.server.ThreadingHTTPServer(("localhost", args.port), Handler)
    print(f"MapHub viewer at http://localhost:{args.port}/  (Ctrl+C to stop)", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
