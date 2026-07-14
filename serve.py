#!/usr/bin/env python3
"""Serve the viewer locally: python3 serve.py  ->  http://localhost:8574

Static file server for app/, plus an on-disk cache for artwork the page hotlinks
(/artcache?u=...). Embedded data: URIs never come through here; remote art is
fetched once into art_cache/ and thereafter served from localhost.

Two things keep the artwork from popping in as you scroll:

  * On startup the page's own art URLs are read out of app/index.html and
    prefetched in the background, so by the time you scroll, the cache is warm
    and nothing waits on a podcast CDN.
  * Cached art is downscaled (when `sips` is available) to ART_MAX. Feeds serve
    3000px covers, and decoding one of those into a 190px tile is what actually
    makes scrolling stutter — the bytes on disk are beside the point.

Delete art_cache/ to refetch. The page also works from file:// — it just goes
straight to the remote URLs, uncached.
"""

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent
PORT = 8574
ART = HERE / "art_cache"
ART_MAX = 640          # ample for the viewer's largest tile on a retina screen
UA = "agnostic-podcast-app/1.0 (+local viewer)"
YEAR = "public, max-age=31536000, immutable"


def shrink(body, ctype):
    """Downscale to ART_MAX so the browser decodes a tile, not a poster.
    Needs `sips` (macOS); anywhere else the original is cached untouched."""
    if not shutil.which("sips"):
        return body, ctype
    try:
        with tempfile.TemporaryDirectory() as td:
            src, out = Path(td) / "in", Path(td) / "out.jpg"
            src.write_bytes(body)
            r = subprocess.run(
                ["sips", "-Z", str(ART_MAX), "-s", "format", "jpeg",
                 "-s", "formatOptions", "78", str(src), "--out", str(out)],
                capture_output=True)
            if r.returncode == 0 and out.exists() and out.stat().st_size:
                return out.read_bytes(), "image/jpeg"
    except Exception:
        pass
    return body, ctype


def cache_art(url):
    """Fetch + shrink + store once. Returns (bytes, content-type)."""
    key = ART / hashlib.sha256(url.encode()).hexdigest()[:20]
    meta = key.with_suffix(".type")
    if key.exists() and key.stat().st_size:
        return key.read_bytes(), (meta.read_text() if meta.exists() else "image/jpeg")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        body, ctype = r.read(), r.headers.get("Content-Type", "image/jpeg")
    body, ctype = shrink(body, ctype)
    ART.mkdir(exist_ok=True)
    key.write_bytes(body)
    meta.write_text(ctype)
    return body, ctype


def prewarm(index_html):
    """Read the page's own art URLs out of its inlined JSON and fetch them all,
    so scrolling never waits on the network. Idempotent; runs in the background."""
    try:
        m = re.search(r'<script type="application/json" id="d">(.*?)</script>',
                      index_html.read_text(encoding="utf-8"), re.S)
        if not m:
            return
        d = json.loads(m.group(1).replace("<\\/", "</"))
        urls = {p[3] for p in d.get("podcasts", [])} | {e[7] for e in d.get("episodes", [])}
        urls = sorted(u for u in urls if u and u.startswith("http"))
    except Exception:
        return
    if not urls:
        return
    done = 0
    print(f"warming art cache: {len(urls)} images...", flush=True)
    with ThreadPoolExecutor(8) as ex:
        for _ in ex.map(lambda u: _quiet(cache_art, u), urls):
            done += 1
    print(f"art cache warm ({done} images)", flush=True)


def _quiet(fn, *a):
    try:
        return fn(*a)
    except Exception:
        return None


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass  # quiet

    def do_GET(self):
        if self.path.startswith("/artcache?"):
            return self.artcache()
        return super().do_GET()

    def artcache(self):
        url = urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query).get("u", [""])[0]
        if not url.startswith(("http://", "https://")):
            return self.send_error(400, "artcache takes an http(s) url")
        try:
            body, ctype = cache_art(url)
        except Exception as exc:   # the page falls back to the show's cover
            return self.send_error(502, f"artcache fetch failed: {exc}")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", YEAR)
        self.end_headers()
        self.wfile.write(body)


def main():
    app_dir = HERE / "app"
    if not (app_dir / "index.html").exists():
        raise SystemExit("app/index.html missing — run: python3 viewer.py")
    threading.Thread(target=prewarm, args=(app_dir / "index.html",),
                     daemon=True).start()
    server = ThreadingHTTPServer(
        ("127.0.0.1", PORT), partial(Handler, directory=str(app_dir)))
    url = f"http://localhost:{PORT}/"
    print(f"serving {url}  (Ctrl-C to stop)")
    webbrowser.open(url)
    server.serve_forever()


if __name__ == "__main__":
    main()
