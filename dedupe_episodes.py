#!/usr/bin/env python3
"""Merge duplicate episode rows within a podcast (see DEDUP_RULES.md).

Two rows are the same episode when they share a non-empty enclosure_core, or
the same normalized title on the same publish date. Guid inequality does NOT
block a merge: feeds change guid schemes between app eras, so two guids can
name one episode; the enclosure/title+date evidence wins.

The keeper is the row with a guid (else the lowest id). All referencing rows
(app_episode_state, play_history, listen_sessions, observations) are
repointed; keeper fields are backfilled from the duplicate; the duplicate row
is deleted. Runs to a fixed point. Idempotent — safe in the rebuild chain
between the ingesters and link_feed_variants.py.
"""

import sqlite3
from pathlib import Path

from identity import norm_title as norm

db = sqlite3.connect(Path(__file__).parent / "library.sqlite")
db.execute("PRAGMA foreign_keys = ON")


def merge(keep, dupe):
    # play_history may collide on (app, episode_id, played_at): move, then drop leftovers
    db.execute("UPDATE OR IGNORE play_history SET episode_id=? WHERE episode_id=?", (keep, dupe))
    db.execute("DELETE FROM play_history WHERE episode_id=?", (dupe,))
    db.execute("UPDATE app_episode_state SET episode_id=? WHERE episode_id=?", (keep, dupe))
    db.execute("UPDATE listen_sessions SET episode_id=? WHERE episode_id=?", (keep, dupe))
    db.execute("UPDATE observations SET episode_id=? WHERE episode_id=?", (keep, dupe))
    db.execute("""
        UPDATE episodes SET
          guid           = coalesce(guid, (SELECT guid FROM episodes WHERE id=?)),
          enclosure_url  = coalesce(enclosure_url, (SELECT enclosure_url FROM episodes WHERE id=?)),
          enclosure_core = coalesce(nullif(enclosure_core,''), (SELECT enclosure_core FROM episodes WHERE id=?)),
          published_at   = coalesce(published_at, (SELECT published_at FROM episodes WHERE id=?)),
          duration_sec   = coalesce(duration_sec, (SELECT duration_sec FROM episodes WHERE id=?))
        WHERE id=?""", (dupe, dupe, dupe, dupe, dupe, keep))
    db.execute("DELETE FROM episodes WHERE id=?", (dupe,))


total = 0
for _pass in range(1000):  # to a fixed point; the bound only guards a logic bug
    pairs = []
    rows = db.execute("SELECT id, podcast_id, guid, title, published_at, enclosure_core"
                      " FROM episodes").fetchall()
    by_core, by_td = {}, {}
    for eid, pid, guid, title, pub, core in rows:
        if core:
            by_core.setdefault((pid, core), []).append((eid, guid))
        if title and pub:
            by_td.setdefault((pid, norm(title), pub[:10]), []).append((eid, guid))
    seen = set()
    for group in list(by_core.values()) + list(by_td.values()):
        if len(group) < 2:
            continue
        group.sort(key=lambda t: (t[1] is None, t[0]))  # guided rows first, then oldest
        keep = group[0][0]
        for eid, _ in group[1:]:
            if eid != keep and eid not in seen and keep not in seen:
                pairs.append((keep, eid))
                seen.add(eid)
    if not pairs:
        break
    for keep, dupe in pairs:
        merge(keep, dupe)
    total += len(pairs)
    print(f"pass {_pass + 1}: merged {len(pairs)} duplicate episode rows")
else:
    raise SystemExit("dedupe did not reach a fixed point — investigate before trusting the DB")

db.commit()
n = db.execute("SELECT count(*) FROM episodes").fetchone()[0]
print(f"done: {total} duplicates merged, {n} episodes remain")
