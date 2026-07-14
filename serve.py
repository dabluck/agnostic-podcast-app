#!/usr/bin/env python3
"""Serve the viewer locally: python3 serve.py  ->  http://localhost:8574

Static file server for app/. The page is fully self-contained, so opening
app/index.html directly with file:// works too; this is just for convenience.
"""

import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent
PORT = 8574


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass  # quiet


def main():
    app_dir = HERE / "app"
    if not (app_dir / "index.html").exists():
        raise SystemExit("app/index.html missing — run: python3 viewer.py")
    server = ThreadingHTTPServer(
        ("127.0.0.1", PORT), partial(Handler, directory=str(app_dir)))
    url = f"http://localhost:{PORT}/"
    print(f"serving {url}  (Ctrl-C to stop)")
    webbrowser.open(url)
    server.serve_forever()


if __name__ == "__main__":
    main()
