#!/usr/bin/env python3
"""Serve the viewer locally the way the published site will be laid out.

    python viewer/serve.py            # then open http://localhost:8000/
    python viewer/serve.py --tables .work/demo-tables   # serve other tables

/            -> viewer/ (index.html, viewer.js, viewer.css)
/tables/...  -> tables/ at the repo root (or the folder given by --tables)
/YYYY-MM-DD  -> viewer/index.html (the page reads the date from its URL)
"""

import argparse
import http.server
import re
import urllib.parse
from pathlib import Path

VIEWER = Path(__file__).resolve().parent
ROOT = VIEWER.parent
TABLES = ROOT / "tables"


class Handler(http.server.SimpleHTTPRequestHandler):
    def translate_path(self, path):
        path = urllib.parse.unquote(path.split("?", 1)[0].split("#", 1)[0])
        if re.fullmatch(r"/\d{4}-\d{2}-\d{2}/?", path):
            return str(VIEWER / "index.html")
        base = TABLES if path.startswith("/tables/") else VIEWER
        target = (TABLES / path[len("/tables/"):] if base != VIEWER
                  else VIEWER / path.lstrip("/")).resolve()
        # Serve nothing outside viewer/ and the tables (the repo root holds .env).
        return str(target) if target.is_relative_to(base) else str(VIEWER / "missing")

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()


def main():
    global TABLES
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--tables", type=Path, default=TABLES, help="folder served as /tables/")
    args = ap.parse_args()
    TABLES = args.tables.resolve()
    server = http.server.ThreadingHTTPServer(("localhost", args.port), Handler)
    print(f"MapHub viewer at http://localhost:{args.port}/  (Ctrl+C to stop)", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
