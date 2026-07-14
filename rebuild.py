#!/usr/bin/env python3
"""Rebuild library.sqlite from scratch out of your local exports.

Runs the ingesters, then the generic pipeline. resolve_guids.py fetches only
public RSS feeds, and caches them, so a re-run needs no network once cached.
Chain and rules: DEDUP_RULES.md.

Ingesters are optional: any whose input export is absent should print a short
skip line and exit 0 (the shipped ones do), so this chain runs as-is even if
you only have one app's data.
"""
import sqlite3
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
CHAIN = ["ingest_pocketcasts.py", "ingest_castro.py", "resolve_guids.py",
         "dedupe_episodes.py", "add_public_counterparts.py",
         "link_feed_variants.py"]

if __name__ == "__main__":
    if "--fresh" in sys.argv:
        (HERE / "library.sqlite").unlink(missing_ok=True)
        print("removed library.sqlite (fresh rebuild)")
    for script in CHAIN:
        print(f"=== {script} ===", flush=True)
        r = subprocess.run([sys.executable, str(HERE / script)],
                           capture_output=True, text=True)
        print(r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "(no output)")
        if r.returncode != 0:
            sys.exit(f"{script} FAILED:\n{r.stderr}")

    db = sqlite3.connect(HERE / "library.sqlite")
    def q(s): return db.execute(s).fetchone()[0]

    # Invariants from DEDUP_RULES.md — any non-zero fails the rebuild.
    from identity import norm_title
    dupes = 0
    seen = set()
    for pid, title, pub, core in db.execute(
            "SELECT podcast_id, title, published_at, enclosure_core FROM episodes"):
        keys = []
        if core:
            keys.append(("c", pid, core))
        if title and pub:
            keys.append(("t", pid, norm_title(title), pub[:10]))
        for key in keys:
            dupes += key in seen
            seen.add(key)
    checks = {
        "episodes dated pre-2000": q("SELECT count(*) FROM episodes WHERE published_at < '2000'"),
        "within-podcast duplicate episode keys": dupes,
        # listening data starts 2015; earlier dates mean a misfiled epoch
        # (Apple-reference seconds land in 1994 and would pass a pre-2000
        # check on episodes only)
        "plays before 2015": q("SELECT count(*) FROM play_history WHERE played_at < '2015'"),
        "sessions before 2015": q("SELECT count(*) FROM listen_sessions WHERE began_at < '2015'"),
    }
    failed = {k: v for k, v in checks.items() if v}
    print("\nverify:", "; ".join(f"{k}={v}" for k, v in checks.items()))
    print(f"podcasts {q('SELECT count(*) FROM podcasts')} in "
          f"{q('SELECT count(DISTINCT coalesce(public_podcast_id, id)) FROM podcasts')} families | "
          f"episodes {q('SELECT count(*) FROM episodes')} "
          f"({q('SELECT count(*) FROM episodes WHERE guid IS NOT NULL')} with guid) | "
          f"states {q('SELECT count(*) FROM app_episode_state')} | "
          f"plays {q('SELECT count(*) FROM play_history')} | "
          f"sessions {q('SELECT count(*) FROM listen_sessions')}")
    if failed:
        sys.exit(f"INVARIANT FAILURES: {failed}")
