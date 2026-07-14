#!/usr/bin/env python3
"""Resolve RSS <guid>s for canonical episodes that were ingested without one.

This is the enclosure -> guid reverse lookup. Some app exports don't carry the
RSS guid (or the feed URL) for an episode; the public identity we can always
recover is the guid, by reading the show's PUBLIC RSS feed directly.

For every podcast owning guid-less episodes, fetch its feed(s) (cached forever
in feed_cache/feed_{sha16}.xml), then match each guid-less episode with the
standard cascade: enclosure core -> normalized title + publish date -> unique
normalized title. Matched episodes get their guid (and duration/published_at
when missing) from the feed item.

This only ever fetches the public RSS feed URLs already stored in
podcast_feed_urls; there is no app API involved.

Guid conflicts (another row of the podcast already holds that guid) are left
alone — that pair is a duplicate; dedupe_episodes.py merges it. Run order:
after the ingesters, before dedupe_episodes.py. Idempotent; offline once
feeds are cached.
"""

import hashlib
import re
import sqlite3
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

HERE = Path(__file__).parent
CACHE = HERE / "feed_cache"
UA = "agnostic-podcast-app/1.0 (+guid resolver)"

from identity import core_url, dur_to_sec, norm_title as norm


def fetch_feed(url):
    """Cached-forever feed fetch; returns parsed items or None on failure."""
    path = CACHE / ("feed_" + hashlib.sha256(url.encode()).hexdigest()[:16] + ".xml")
    if not (path.exists() and path.stat().st_size > 0):
        try:
            CACHE.mkdir(exist_ok=True)
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            path.write_bytes(urllib.request.urlopen(req, timeout=30).read())
        except Exception:
            return None
    try:
        txt = path.read_bytes().decode("utf-8", "replace")
        txt = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", txt)
        root = ET.fromstring(txt)
    except Exception:
        return None
    items = []
    ch = root.find("channel")
    if ch is None:
        return None
    for it in ch.iter("item"):
        enc = it.find("enclosure")
        dur = it.findtext("{http://www.itunes.com/dtds/podcast-1.0.dtd}duration")
        try:
            d = parsedate_to_datetime(it.findtext("pubDate") or "")
            date = d.astimezone(timezone.utc).strftime("%Y-%m-%d")
            iso = d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            date = iso = None
        items.append({
            "guid": (it.findtext("guid") or "").strip() or None,
            "core": core_url(enc.get("url") if enc is not None else None),
            "title": norm(it.findtext("title")),
            "date": date, "published": iso, "dur": dur,
        })
    return items


def main():
    db = sqlite3.connect(HERE / "library.sqlite")
    db.row_factory = sqlite3.Row
    targets = {}  # podcast_id -> [episode rows without guid]
    for e in db.execute("SELECT id, podcast_id, title, published_at, enclosure_core,"
                        " duration_sec FROM episodes WHERE guid IS NULL"):
        targets.setdefault(e["podcast_id"], []).append(dict(e))
    urls = {}  # podcast_id -> [feed urls]
    for r in db.execute("SELECT podcast_id, url FROM podcast_feed_urls"):
        if r["podcast_id"] in targets:
            urls.setdefault(r["podcast_id"], []).append(r["url"])
    print(f"{sum(len(v) for v in targets.values())} guid-less episodes "
          f"across {len(targets)} podcasts; fetching {len(urls)} feeds...")

    def job(pid):
        for u in urls.get(pid, []):
            items = fetch_feed(u)
            if items:
                return pid, items
        return pid, None

    with ThreadPoolExecutor(12) as ex:
        feed_items = dict(ex.map(job, targets))

    n_guid = n_conflict = n_nofeed = n_nomatch = n_meta = 0
    for pid, eps in targets.items():
        items = feed_items.get(pid)
        if not items:
            n_nofeed += len(eps)
            continue
        by_core, by_td, by_title = {}, {}, {}
        for it in items:
            if it["core"]:
                by_core.setdefault(it["core"], []).append(it)
            if it["title"] and it["date"]:
                by_td.setdefault((it["title"], it["date"]), []).append(it)
            if it["title"]:
                by_title.setdefault(it["title"], []).append(it)
        taken = {g for (g,) in db.execute(
            "SELECT guid FROM episodes WHERE podcast_id=? AND guid IS NOT NULL", (pid,))}
        for e in eps:
            cands = by_core.get(e["enclosure_core"] or "") \
                or by_td.get((norm(e["title"]), (e["published_at"] or "")[:10])) \
                or (by_title.get(norm(e["title"]))
                    if len(by_title.get(norm(e["title"]), [])) == 1 else None)
            if not cands:
                n_nomatch += 1
                continue
            it = cands[0]
            if it["guid"]:
                if it["guid"] in taken:
                    n_conflict += 1  # duplicate row; dedupe_episodes merges it
                else:
                    db.execute("UPDATE episodes SET guid=? WHERE id=?", (it["guid"], e["id"]))
                    taken.add(it["guid"])
                    n_guid += 1
            if e["duration_sec"] is None and dur_to_sec(it["dur"]):
                db.execute("UPDATE episodes SET duration_sec=? WHERE id=?",
                           (dur_to_sec(it["dur"]), e["id"]))
                n_meta += 1
    db.commit()
    print(f"guids backfilled: {n_guid}; conflicts left for dedupe: {n_conflict}; "
          f"no feed: {n_nofeed}; no match in feed: {n_nomatch}; durations added: {n_meta}")


if __name__ == "__main__":
    main()
