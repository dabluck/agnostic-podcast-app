#!/usr/bin/env python3
"""Ingest a local Pocket Casts database export into library.sqlite (origin
app: 'pocketcasts'). Reads only local files — no network, no app API.

Idempotent: canonical rows are upserted by public identity (feed_url, guid,
enclosure_core), app rows by (app, external_id). Each run appends observations
only when an episode's state changed, so repeated runs build a dated history.

Sources (all local):
  - the `pocketcasts` SQLite export (play state, positions, sparse played
    dates). This is the app's own on-device database.
  - feed_urls.json (OPTIONAL): a {podcast_uuid: feed_url} mapping. The local
    export stores no RSS feed URL, so provide one if you have it — e.g. built
    from your own Pocket Casts OPML export (match by title). Without it,
    podcasts are created with feed_url=NULL and can be enriched later once you
    add their feed URLs to podcast_feed_urls; resolve_guids.py then fills guids.

RSS guids are NOT taken from any app-specific mapping here: run resolve_guids.py
after ingest to recover them from the public RSS feeds.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from identity import core_url, iso_from_epoch as iso

HERE = Path(__file__).parent
APP = "pocketcasts"
DB_PATH = HERE / "pocketcasts"
STATUS = {0: "unplayed", 1: "partial", 2: "played"}


def main():
    if not DB_PATH.exists():
        print(f"pocketcasts: no export at {DB_PATH.name}; skipping")
        return
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    feed_path = HERE / "feed_urls.json"
    if feed_path.exists():
        raw = json.load(open(feed_path))
        feed_urls = raw.get("result", raw)  # accept {result:{...}} or a flat map
    else:
        feed_urls = {}

    src = sqlite3.connect(DB_PATH)
    src.row_factory = sqlite3.Row
    out = sqlite3.connect(HERE / "library.sqlite")
    out.executescript(open(HERE / "schema.sql").read())

    # --- podcasts ---
    pod_canon = {}  # pocketcasts uuid -> canonical podcast_id
    for p in src.execute(
        "SELECT uuid, title, author, podcast_url, subscribed, is_private FROM podcasts"
    ):
        feed = feed_urls.get(p["uuid"])
        row = out.execute(
            "SELECT podcast_id FROM podcast_feed_urls WHERE url = ?", (feed,)
        ).fetchone() if feed else None
        if row:
            pid = row[0]
        else:
            pid = out.execute(
                "INSERT INTO podcasts (feed_url, title, author, website_url, is_private)"
                " VALUES (?,?,?,?,?)",
                (feed, p["title"], p["author"], p["podcast_url"], p["is_private"]),
            ).lastrowid
        if feed:
            out.execute(
                "INSERT OR IGNORE INTO podcast_feed_urls (url, podcast_id, role, source, added_at)"
                " VALUES (?,?,?,?,?)", (feed, pid, "current", APP, now))
        out.execute(
            "INSERT INTO app_podcasts (app, external_id, podcast_id, subscribed) VALUES (?,?,?,?) "
            "ON CONFLICT(app, external_id) DO UPDATE SET subscribed = excluded.subscribed",
            (APP, p["uuid"], pid, p["subscribed"]),
        )
        pod_canon[p["uuid"]] = pid

    # --- episodes + state ---
    n_eps = n_obs = 0
    q = src.execute(
        "SELECT uuid, podcast_id, title, published_date, download_url, duration,"
        "       season, number, playing_status, played_up_to, starred, archived,"
        "       last_playback_interaction_date"
        " FROM podcast_episodes"
    )
    for e in q:
        # No row = never interacted. Skip untouched episodes; archived alone is
        # not an interaction (Pocket Casts auto-archives untouched episodes).
        # NULL-tolerant: a future export with nullable columns must skip, not crash.
        if not ((e["playing_status"] or 0) > 0 or (e["played_up_to"] or 0) > 0
                or e["starred"] or (e["last_playback_interaction_date"] or 0) > 0):
            continue
        pid = pod_canon[e["podcast_id"]]
        core = core_url(e["download_url"])
        # The export doesn't carry the RSS guid, so match by enclosure core only
        # — and never on an EMPTY core, which would collapse distinct episodes
        # that both lack an enclosure. resolve_guids.py fills guids afterward
        # from the public RSS feed.
        found = out.execute(
            "SELECT id FROM episodes WHERE podcast_id=? AND enclosure_core=? AND guid IS NULL",
            (pid, core)).fetchone() if core else None
        if found:
            eid = found[0]
        else:
            eid = out.execute(
                "INSERT INTO episodes (podcast_id, guid, enclosure_url, enclosure_core,"
                " title, published_at, duration_sec, season, number) VALUES (?,?,?,?,?,?,?,?,?)",
                (pid, None, e["download_url"], core, e["title"], iso(e["published_date"]),
                 e["duration"], e["season"], e["number"]),
            ).lastrowid
            n_eps += 1

        # unknown/NULL status on an interacted row degrades to 'partial'
        status = STATUS.get(e["playing_status"], "partial")
        prev = out.execute(
            "SELECT status, position_sec FROM app_episode_state WHERE app=? AND external_id=?",
            (APP, e["uuid"]),
        ).fetchone()
        out.execute(
            "INSERT INTO app_episode_state (app, external_id, episode_id, status, raw_status,"
            " position_sec, last_played_at, starred, archived, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(app, external_id) DO UPDATE SET episode_id=excluded.episode_id,"
            "  status=excluded.status, raw_status=excluded.raw_status,"
            "  position_sec=excluded.position_sec, last_played_at=excluded.last_played_at,"
            "  starred=excluded.starred, archived=excluded.archived, updated_at=excluded.updated_at",
            (APP, e["uuid"], eid, status, str(e["playing_status"]), e["played_up_to"],
             iso(e["last_playback_interaction_date"]), e["starred"], e["archived"], now),
        )
        if prev is None or (prev[0], prev[1]) != (status, e["played_up_to"]):
            out.execute(
                "INSERT INTO observations (app, episode_id, status, position_sec, observed_at)"
                " VALUES (?,?,?,?,?)",
                (APP, eid, status, e["played_up_to"], now),
            )
            n_obs += 1

    out.commit()
    tot_p = out.execute("SELECT count(*) FROM podcasts").fetchone()[0]
    tot_e = out.execute("SELECT count(*) FROM episodes").fetchone()[0]
    with_guid = out.execute("SELECT count(*) FROM episodes WHERE guid IS NOT NULL").fetchone()[0]
    print(f"library.sqlite: {tot_p} podcasts, {tot_e} episodes ({with_guid} with RSS guid), "
          f"+{n_eps} new episodes, +{n_obs} observations this run")


if __name__ == "__main__":
    main()
