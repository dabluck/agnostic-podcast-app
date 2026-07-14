#!/usr/bin/env python3
"""Ingest a Castro .castrobackup into library.sqlite (origin app: 'castro').

Second reference adapter (after ingest_pocketcasts.py), showing how another
app maps onto the same public-identity schema. Reads only local files.

Sources (all local — no network):
  - castro.castrobackup: the app's own exported UserData graph of Castro public
    ids. Epochs differ by section: playSessions.beganAt/finishedAt and
    episodes.lastPlayed are Unix seconds; queue.created and exportDate are
    Apple reference dates (unused here).
  - castro_cache/ (OPTIONAL metadata mapping Castro public ids to public RSS
    identity — supply your own if you have it):
      podcasts.jsonl: podcast public id -> feed_url (or private flag)
      episodes.jsonl: episode public id -> feed_guid, media_url, title,
        published_at, duration, podcast_public_id
      pod_titles.json + feed_titles.json: names for new podcasts
    Without this cache the backup alone has no public feed URLs, so nothing can
    be resolved and the run is a no-op — provide the mapping to use it.

State mapping: an episode is 'played' when progress or session-consumed audio
reaches 92% of its duration, 'partial' when there is any listening evidence
(history entry, progress, session), and 'unplayed' when the only interaction
is queueing/starring. Private-feed episodes are skipped and counted — they
are pending the user's manual feed mapping.

Idempotent. Order among ingesters doesn't matter (they upsert by public identity).
"""

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
CACHE = HERE / "castro_cache"
APP = "castro"
PLAYED_FRACTION = 0.92

from identity import core_url, iso_any, iso_from_epoch as iso_s


def dur_sec(d):
    """Some metadata caches store duration as {'seconds': N}; unwrap it."""
    if isinstance(d, dict):
        return d.get("seconds")
    return d


def _load_json(path, default):
    return json.load(open(path)) if path.exists() else default


def main():
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    backup = HERE / "castro.castrobackup"
    if not backup.exists():
        print(f"castro: no {backup.name}; skipping")
        return
    b = json.load(open(backup))

    feed_by_pod = {}
    pod_titles = _load_json(CACHE / "pod_titles.json", {})
    if (CACHE / "podcasts.jsonl").exists():
        for line in open(CACHE / "podcasts.jsonl"):
            p = json.loads(line)
            if not p.get("private") and p.get("feed_url"):
                feed_by_pod[p["public_id"]] = p["feed_url"]
                if p.get("resolved_public_id"):
                    feed_by_pod[p["resolved_public_id"]] = p["feed_url"]
                if p.get("title"):
                    pod_titles.setdefault(p["public_id"], p["title"])
    feed_titles = {pid: t for pid, t in _load_json(CACHE / "feed_titles.json", {}).items() if t}

    # user-supplied private feeds (optional dump) — same identity rules
    private_feed_pods = set()
    pp_path = CACHE / "private_podcasts.json"
    if pp_path.exists():
        for p in json.load(open(pp_path)):
            feed_by_pod[p["public_id"]] = p["feed_url"]
            pod_titles.setdefault(p["public_id"], p["title"])
            private_feed_pods.add(p["public_id"])

    ep_meta = {}  # castro episode id -> resolved identity
    if (CACHE / "episodes.jsonl").exists():
        for line in open(CACHE / "episodes.jsonl"):
            e = json.loads(line)
            if not e.get("null"):
                ep_meta[e["requested_id"]] = e
    pe_path = CACHE / "castro_private_episodes.jsonl"
    if pe_path.exists():
        for line in open(pe_path):
            if not line.strip():
                continue
            e = json.loads(line)
            guid = e.get("feed_guid")
            if isinstance(guid, dict):  # some dumps wrap guids as {"str": ...}
                guid = guid.get("str")
            ep_meta.setdefault(e["public_id"], {
                "requested_id": e["public_id"], "public_id": e["public_id"],
                "podcast_public_id": e["podcast_public_id"], "feed_guid": guid,
                "media_url": e.get("enclosure_url"), "title": e.get("title"),
                "published_at": e.get("published_at"), "duration": None})

    # backup state per episode
    ep_state = {e["publicId"]: e for e in b["episodes"]}
    history = {h["publicId"] for h in b["history"]}
    queued = {q["publicId"] for q in b["queue"]}
    subscribed = {p["publicId"] for p in b.get("podcasts", [])}
    sessions_by_ep = {}
    for s in b["playSessions"]:
        sessions_by_ep.setdefault(s["episodePublicId"], []).append(s)

    out = sqlite3.connect(HERE / "library.sqlite")
    out.executescript(open(HERE / "schema.sql").read())

    # --- canonical podcasts for every resolved episode's show ---
    def podcast_row(cpod):
        feed = feed_by_pod.get(cpod)
        if not feed:
            return None
        row = out.execute(
            "SELECT podcast_id FROM podcast_feed_urls WHERE url = ?", (feed,)).fetchone()
        if row:
            pid = row[0]
        else:
            title = pod_titles.get(cpod) or feed_titles.get(cpod) or f"(castro {cpod[:8]})"
            pid = out.execute(
                "INSERT INTO podcasts (feed_url, title, is_private) VALUES (?,?,?)",
                (feed, title, 1 if cpod in private_feed_pods else 0)).lastrowid
            out.execute(
                "INSERT OR IGNORE INTO podcast_feed_urls (url, podcast_id, role, source, added_at)"
                " VALUES (?,?,?,?,?)", (feed, pid, "current", APP, now))
        out.execute(
            "INSERT INTO app_podcasts (app, external_id, podcast_id, subscribed) VALUES (?,?,?,?)"
            " ON CONFLICT(app, external_id) DO UPDATE SET subscribed = excluded.subscribed",
            (APP, cpod, pid, 1 if cpod in subscribed else 0))
        return pid

    pod_canon = {}
    for cpod in {m["podcast_public_id"] for m in ep_meta.values()}:
        pid = podcast_row(cpod)
        if pid:
            pod_canon[cpod] = pid

    # --- episodes: state, history, sessions ---
    touched = set(ep_state) | history | queued | set(sessions_by_ep)
    n_eps = n_obs = n_hist = n_sess = 0
    skipped_private = 0
    for ceid in sorted(touched):
        meta = ep_meta.get(ceid)
        if meta is None or meta["podcast_public_id"] not in pod_canon:
            skipped_private += 1
            continue
        pid = pod_canon[meta["podcast_public_id"]]
        guid, url = meta.get("feed_guid"), meta.get("media_url")
        core = core_url(url)

        found = None
        if guid:
            found = out.execute(
                "SELECT id FROM episodes WHERE podcast_id=? AND guid=?", (pid, guid)).fetchone()
        if not found and core:
            found = out.execute(
                "SELECT id FROM episodes WHERE podcast_id=? AND enclosure_core=? AND guid IS NULL",
                (pid, core)).fetchone()
            if found and guid:
                out.execute("UPDATE episodes SET guid=? WHERE id=?", (guid, found[0]))
        if found:
            eid = found[0]
        else:
            eid = out.execute(
                "INSERT INTO episodes (podcast_id, guid, enclosure_url, enclosure_core,"
                " title, published_at, duration_sec) VALUES (?,?,?,?,?,?,?)",
                (pid, guid, url, core, meta.get("title") or "(untitled)",
                 iso_any(meta.get("published_at")), dur_sec(meta.get("duration")))).lastrowid
            n_eps += 1

        st = ep_state.get(ceid, {})
        sess = sessions_by_ep.get(ceid, [])
        progress = st.get("playProgress") or 0
        consumed = sum((s["playedTo"] or 0) - (s["playedFrom"] or 0) for s in sess)
        duration = dur_sec(meta.get("duration")) or 0
        if not duration:  # private dump has no duration; use the canonical row's
            row = out.execute("SELECT duration_sec FROM episodes WHERE id=?", (eid,)).fetchone()
            duration = (row and row[0]) or 0
        if duration and max(progress, consumed) >= PLAYED_FRACTION * duration:
            status = "played"
        elif ceid in history or progress > 0 or consumed > 0:
            status = "partial"
        else:
            status = "unplayed"  # queued/starred only
        raw = ",".join(k for k, v in (
            ("history", ceid in history), ("queued", ceid in queued),
            ("starred", st.get("starred")), ("progress", progress > 0),
            ("sessions", bool(sess))) if v) or "backup"
        last_played = iso_s(st.get("lastPlayed"))
        if sess:
            # default= matters: an episode whose every session is unfinished
            # (in-progress play) yields an empty generator, and max() without
            # a default would abort the whole ingest.
            last_sess = iso_s(max((s["finishedAt"] for s in sess if s.get("finishedAt")),
                                  default=0))
            last_played = max(filter(None, [last_played, last_sess]), default=None)

        prev = out.execute(
            "SELECT status, position_sec FROM app_episode_state WHERE app=? AND external_id=?",
            (APP, ceid)).fetchone()
        out.execute(
            "INSERT INTO app_episode_state (app, external_id, episode_id, status, raw_status,"
            " position_sec, last_played_at, starred, updated_at) VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(app, external_id) DO UPDATE SET episode_id=excluded.episode_id,"
            "  status=excluded.status, raw_status=excluded.raw_status,"
            "  position_sec=excluded.position_sec, last_played_at=excluded.last_played_at,"
            "  starred=excluded.starred, updated_at=excluded.updated_at",
            (APP, ceid, eid, status, raw, progress or None, last_played,
             1 if st.get("starred") else None, now))
        if prev is None or (prev[0], prev[1]) != (status, progress or None):
            out.execute(
                "INSERT INTO observations (app, episode_id, status, position_sec, observed_at)"
                " VALUES (?,?,?,?,?)", (APP, eid, status, progress or None, now))
            n_obs += 1
        if st.get("lastPlayed"):
            n_hist += out.execute(
                "INSERT OR IGNORE INTO play_history (app, episode_id, played_at, source)"
                " VALUES (?,?,?,?)", (APP, eid, iso_s(st["lastPlayed"]), "castro_backup")).rowcount
        for s in sess:
            if not s.get("beganAt"):  # malformed session: no start = no identity
                continue
            # older sessions carry no id; synthesize a stable one for idempotency
            sid = s.get("sessionPublicId") or f"{ceid}:{s['beganAt']}:{s.get('playedFrom')}"
            n_sess += out.execute(
                "INSERT OR IGNORE INTO listen_sessions (app, external_id, episode_id, began_at,"
                " ended_at, from_sec, to_sec, listened_sec, trimmed_sec, source)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (APP, sid, eid, iso_s(s["beganAt"]), iso_s(s.get("finishedAt")),
                 s.get("playedFrom"), s.get("playedTo"),
                 (s.get("playedTo") or 0) - (s.get("playedFrom") or 0),
                 s.get("trimmed"), "castro_backup")).rowcount

    # --- lifetime stats across ALL sessions (private feeds included) ---
    all_sess = [s for s in b["playSessions"] if s.get("beganAt")]
    listened = sum((s["playedTo"] or 0) - (s["playedFrom"] or 0) for s in all_sess)
    wall = sum((s["finishedAt"] or s["beganAt"]) - s["beganAt"] for s in all_sess)
    trimmed = sum(s.get("trimmed") or 0 for s in all_sess)
    for key, val in (("time_listened_sec", listened), ("wall_clock_sec", wall),
                     ("time_silence_removal_sec", trimmed),
                     ("sessions_count", len(all_sess))):
        out.execute("INSERT OR IGNORE INTO app_stats (app, key, value, as_of) VALUES (?,?,?,?)",
                    (APP, key, str(round(val)), now))

    out.commit()
    tot = out.execute("SELECT count(*) FROM app_episode_state WHERE app=?", (APP,)).fetchone()[0]
    print(f"castro: {len(pod_canon)} podcasts, {tot} episode states "
          f"(+{n_eps} new canonical eps, +{n_obs} observations, +{n_hist} play_history, "
          f"+{n_sess} sessions); {skipped_private} private/unresolved episodes pending user mapping; "
          f"lifetime listened {listened/3600:.0f}h (wall {wall/3600:.0f}h, trimmed {trimmed/3600:.0f}h)")


if __name__ == "__main__":
    main()
