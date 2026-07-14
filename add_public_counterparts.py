#!/usr/bin/env python3
"""Seed owner-approved feed knowledge the ingesters can't derive:
  - public_counterparts.json: public podcast rows for shows the apps only
    know via private feeds, so linking can anchor variants to them.
  - feed_replacements.json: successor feed URLs for shows whose original
    feed died and moved (verified by episode overlap; see
    dead_feeds_review.md). Registered as role='replacement' aliases.
Run before link_feed_variants.py. Idempotent."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
db = sqlite3.connect(HERE / "library.sqlite")
n = 0
pc_path = HERE / "public_counterparts.json"
counterparts = json.load(open(pc_path)) if pc_path.exists() else []
for p in counterparts:
    if db.execute("SELECT 1 FROM podcast_feed_urls WHERE url=?", (p["feed_url"],)).fetchone():
        continue
    pid = db.execute("INSERT INTO podcasts (feed_url, title, author) VALUES (?,?,?)",
                     (p["feed_url"], p["title"], p.get("author"))).lastrowid
    db.execute("INSERT OR IGNORE INTO podcast_feed_urls (url, podcast_id, role, source, added_at)"
               " VALUES (?,?,?,?,?)", (p["feed_url"], pid, "current", "public_counterpart", now))
    n += 1

n_rep = 0
rep_path = HERE / "feed_replacements.json"
if rep_path.exists():
    for m in json.load(open(rep_path)):
        row = db.execute("SELECT podcast_id FROM podcast_feed_urls WHERE url=?",
                         (m["existing_url"],)).fetchone()
        if row is None:
            print(f"  replacement SKIPPED (unknown existing_url): {m['note']}")
            continue
        n_rep += db.execute(
            "INSERT OR IGNORE INTO podcast_feed_urls (url, podcast_id, role, source, added_at)"
            " VALUES (?,?,?,?,?)",
            (m["replacement_url"], row[0], "replacement", "owner_ruling", now)).rowcount
db.commit()
print(f"public counterparts: +{n} rows; feed replacements: +{n_rep} aliases")
