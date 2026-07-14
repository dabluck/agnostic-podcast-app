#!/usr/bin/env python3
"""Render app/index.html from library.sqlite — a self-contained static viewer.

Opens the warehouse READ-ONLY and emits ONE HTML file with the data inlined as
JSON. No network, no external assets, no build step. The browsing unit is the
podcast family (feed variants combined via the podcast_families view), and every
episode carries the cross-app merged status from merged_episode_state.

Views: a recent-listening mosaic with a monthly listening spark, a filterable
podcast grid (+ per-show detail), and a sortable episode table. Light/dark/auto
theme. Chart color validated with the dataviz palette checker: light #4a6bdc,
dark #6784ea.

Artwork comes from podcasts.image_url / episodes.image_url — enrichment hooks the
shipped ingesters leave unset, so out of the box every show renders as a lettered
tile. Fill them in and art appears. An http URL is hotlinked; a data: URI is
embedded. Art degrades down a chain — episode art, then the show's cover, then a
lettered tile — so embedding just the covers (there are far fewer of them) leaves
a page that still renders fully with no network, and sharpens when it has one.

Degrades cleanly on a sparse library: no dated plays just means no spark chart.

Regenerate any time with `python3 viewer.py`; serve it with `python3 serve.py`
or open the file directly. Exits non-zero if the page blows the size budget or
the library has no podcasts/episodes to show.
"""

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
DB = HERE / "library.sqlite"
OUT = HERE / "app" / "index.html"
SIZE_BUDGET = 8_000_000
STATUS_NUM = {"unplayed": 0, "partial": 1, "played": 2}


def day(iso):
    return iso[:10] if iso else None


def query_totals(db):
    """Headline numbers, plus per-app lifetime hours.

    An app that reports a lifetime counter (app_stats.time_listened_sec) is
    'measured'. Anything heard ONLY in apps that report no counter is estimated
    from episode lengths — never double-counted against a measured app.
    """
    def q(s):
        return db.execute(s).fetchone()[0]

    measured = [(app, int(float(sec))) for app, sec in db.execute(
        "SELECT app, value FROM app_stats s WHERE key = 'time_listened_sec'"
        " AND as_of = (SELECT max(as_of) FROM app_stats WHERE app = s.app"
        "              AND key = 'time_listened_sec') ORDER BY app")]
    counted = [a for a, _ in measured]
    marks = ",".join("?" * len(counted)) or "''"
    est = db.execute(f"""
        SELECT coalesce(sum(e.duration_sec), 0)
        FROM app_episode_state a JOIN episodes e ON e.id = a.episode_id
        WHERE a.status = 'played' AND a.app NOT IN ({marks})
          AND NOT EXISTS (SELECT 1 FROM app_episode_state b
                          WHERE b.episode_id = a.episode_id
                            AND b.app IN ({marks})
                            AND b.status IN ('played', 'partial'))""",
                     counted * 2).fetchone()[0]
    return {
        "played": q("SELECT count(*) FROM merged_episode_state WHERE status='played'"),
        "shows": q("""SELECT count(DISTINCT f.family_id) FROM merged_episode_state m
                      JOIN episodes e ON e.id = m.episode_id
                      JOIN podcast_families f ON f.podcast_id = e.podcast_id
                      WHERE m.status IN ('played','partial')"""),
        "measured": measured,
        "est_sec": int(est),
        "first_play": day(q("SELECT min(last_played_at) FROM merged_episode_state")),
    }


def query_podcasts(db):
    meta = {r[0]: r for r in db.execute("""
        SELECT f.family_id,
               max(CASE WHEN p.id = f.family_id THEN p.title  END),
               max(CASE WHEN p.id = f.family_id THEN p.author END),
               coalesce(max(CASE WHEN p.id = f.family_id THEN p.image_url END),
                        max(p.image_url)),
               max(p.is_private),
               count(*),
               coalesce(max(CASE WHEN p.id = f.family_id THEN p.category END),
                        max(p.category)),
               coalesce(max(CASE WHEN p.id = f.family_id THEN p.subcategory END),
                        max(p.subcategory))
        FROM podcast_families f JOIN podcasts p ON p.id = f.podcast_id
        GROUP BY f.family_id""")}
    rows = []
    for fid, known, played, last, est, measured in db.execute("""
        SELECT f.family_id, count(*), sum(m.status='played'), max(m.last_played_at),
               sum(CASE WHEN m.listened_sec IS NOT NULL THEN m.listened_sec
                        WHEN m.status='played'  THEN coalesce(e.duration_sec, 0)
                        WHEN m.status='partial' THEN coalesce(min(m.position_sec, e.duration_sec),
                                                              m.position_sec, 0)
                        ELSE 0 END),
               sum(m.listened_sec IS NOT NULL)
        FROM merged_episode_state m
        JOIN episodes e ON e.id = m.episode_id
        JOIN podcast_families f ON f.podcast_id = e.podcast_id
        GROUP BY f.family_id"""):
        m = meta[fid]
        rows.append([fid, m[1], m[2], m[3], m[4], known, played or 0,
                     int(est or 0), measured or 0, day(last), m[5], m[6], m[7]])
    return rows


def query_episodes(db):
    """Episode-level art is usually absent; the show's cover stands in (JS side)."""
    return [[fid, title, day(pub), int(dur) if dur else None,
             STATUS_NUM.get(status, 0), day(last),
             int(listened) if listened else None, img, eid, apps or ""]
            for fid, title, pub, dur, status, last, listened, img, eid, apps in db.execute("""
        SELECT f.family_id, m.episode, m.published_at, e.duration_sec,
               m.status, m.last_played_at, m.listened_sec, e.image_url, e.id, m.apps
        FROM merged_episode_state m
        JOIN episodes e ON e.id = m.episode_id
        JOIN podcast_families f ON f.podcast_id = e.podcast_id
        ORDER BY f.family_id, m.published_at DESC""")]


def query_recent(db):
    """Every dated listen as episode ids, newest first (full-timestamp order —
    the day-precision field in the episodes array can't give intra-day order).
    The mosaic dereferences ids against the episodes payload, so this costs a
    few KB instead of duplicating titles and art per row."""
    return [eid for (eid,) in db.execute("""
        SELECT episode_id FROM merged_episode_state
        WHERE last_played_at IS NOT NULL
        ORDER BY last_played_at DESC""")]


def query_months(db):
    """[["YYYY-MM", hours], ...] gap-filled. Sessions are measured audio; dated
    plays from apps without sessions are estimated from episode length."""
    sec = {}
    for ym, s in db.execute(
            "SELECT substr(began_at,1,7), sum(listened_sec) FROM listen_sessions GROUP BY 1"):
        sec[ym] = sec.get(ym, 0) + (s or 0)
    session_apps = [a for (a,) in db.execute("SELECT DISTINCT app FROM listen_sessions")]
    marks = ",".join("?" * len(session_apps)) or "''"
    for ym, s in db.execute(f"""
        SELECT substr(s.last_played_at,1,7), sum(coalesce(e.duration_sec, 0))
        FROM app_episode_state s JOIN episodes e ON e.id = s.episode_id
        WHERE s.app NOT IN ({marks}) AND s.status = 'played'
          AND s.last_played_at IS NOT NULL GROUP BY 1""", session_apps):
        sec[ym] = sec.get(ym, 0) + (s or 0)
    sec = {ym: v for ym, v in sec.items() if ym and ym >= "2015"}
    if not sec:
        return []
    lo, hi = min(sec), max(sec)
    out, y, m = [], int(lo[:4]), int(lo[5:])
    while f"{y:04d}-{m:02d}" <= hi:
        ym = f"{y:04d}-{m:02d}"
        out.append([ym, round(sec.get(ym, 0) / 3600, 1)])
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Heard</title>
<style>
:root, :root[data-theme=light] {
  --bg: #faf9f7; --card: #ffffff; --fg: #1a1a1d; --muted: #71717a;
  --border: #e6e4e0; --accent: #4a6bdc; --chart: #4a6bdc;
  --pill: #eef0f4; --shadow: rgba(20,20,25,.08);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme=light]) {
    --bg: #131316; --card: #1d1d21; --fg: #ececf0; --muted: #94949e;
    --border: #2a2a30; --accent: #7d97f0; --chart: #6784ea;
    --pill: #26262c; --shadow: rgba(0,0,0,.4);
  }
}
:root[data-theme=dark] {
  --bg: #131316; --card: #1d1d21; --fg: #ececf0; --muted: #94949e;
  --border: #2a2a30; --accent: #7d97f0; --chart: #6784ea;
  --pill: #26262c; --shadow: rgba(0,0,0,.4);
}
* { box-sizing: border-box; margin: 0; }
body { background: var(--bg); color: var(--fg);
  font: 15px/1.5 "Avenir Next", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  -webkit-font-smoothing: antialiased; }
main { max-width: 1240px; margin: 0 auto; padding: 38px 28px 90px; position: relative; }
a { color: inherit; text-decoration: none; }

#themebtn { position: absolute; top: 52px; right: 28px; z-index: 10;
  padding: 6px 13px; font: 12.5px -apple-system, BlinkMacSystemFont, sans-serif;
  color: var(--muted); background: transparent; border: 1px solid var(--border);
  border-radius: 999px; cursor: pointer; }
#themebtn:hover { color: var(--fg); border-color: var(--accent); }

.tophead { display: flex; align-items: baseline; gap: 26px; margin-bottom: 8px; }
h1.mast { font: 700 44px/1 "Avenir Next", -apple-system, sans-serif; letter-spacing: -.03em; }
.prose { margin: 14px 0 2px; font-size: 16px; color: var(--muted); max-width: 40em; }
.prose b { color: var(--fg); font-weight: 650; }
.prosesub { font-size: 12.5px; color: var(--muted); opacity: .85; }
svg.spark { width: 100%; max-width: 520px; height: 48px; margin: 12px 0 2px; display: block;
  overflow: visible; }
svg.spark text { font: 10px -apple-system, sans-serif; fill: var(--muted);
  font-variant-numeric: tabular-nums; }
svg.spark .bar { fill: var(--chart); }
svg.spark .bar.hl { opacity: .7; }
.sparkwrap { position: relative; }
.tip { position: absolute; pointer-events: none; background: var(--fg); color: var(--bg);
  font-size: 12px; padding: 4px 9px; border-radius: 7px; white-space: nowrap;
  transform: translate(-50%, -130%); font-variant-numeric: tabular-nums; z-index: 5; }

nav.links a { font-size: 18px; color: var(--muted); margin-right: 22px; padding-bottom: 2px; }
nav.links a.on { color: var(--fg); border-bottom: 2px solid var(--accent); font-weight: 600; }
nav.links a:hover { color: var(--fg); }
.homeblock { margin-bottom: 18px; }

/* Cover art is square, so the lead tile spans an equal 3x3 — big, and with the
   whole cover intact. An unequal span (3x2) crops the top and bottom off every
   cover, which eats the title on text-heavy art. */
.mosaic { display: grid; grid-template-columns: repeat(6, 1fr); grid-auto-rows: 190px; gap: 14px; }
.mosaic .hero:first-child { grid-column: span 3; grid-row: span 3; }
.mosaic .hero:first-child .et { font-size: 24px; }
.mosaic .hero:first-child .es { font-size: 14px; }
.hero { position: relative; border-radius: 14px; overflow: hidden; background: var(--pill);
  box-shadow: 0 2px 10px var(--shadow); cursor: pointer; transition: transform .18s ease; }
.hero:hover { transform: scale(1.015); }
.hero img { width: 100%; height: 100%; object-fit: cover; display: block;
  position: absolute; inset: 0; }
.hero .letter { position: absolute; inset: 0; font-size: 54px; }
.hero .ov { position: absolute; inset: 0; display: flex; flex-direction: column;
  justify-content: flex-end; padding: 12px 14px;
  background: linear-gradient(transparent 48%, rgba(8,8,12,.78)); }
.hero .et { color: #fff; font-size: 13.5px; font-weight: 650; line-height: 1.25;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.hero .es { color: rgba(255,255,255,.72); font-size: 11.5px; margin-top: 3px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

/* Art eases in on load instead of snapping. The tile keeps its surface colour
   underneath, so what you see is a fill resolving into a cover, not a flash. */
img.ph { opacity: 0; transition: opacity .3s ease; }
img.ph.on { opacity: 1; }
@media (prefers-reduced-motion: reduce) { img.ph { transition: none; opacity: 1; } }

.letter { width: 100%; height: 100%; display: flex; align-items: center;
  justify-content: center; font-size: 44px; font-weight: 700; }
.lock { position: absolute; top: 8px; right: 8px; background: rgba(0,0,0,.55);
  border-radius: 999px; padding: 4px 6px; line-height: 0; z-index: 2; }
.lock svg { width: 10px; height: 10px; fill: #fff; }

.controls { display: flex; gap: 10px; margin: 4px 0 26px; flex-wrap: wrap; }
.controls input[type=search] { flex: 1; min-width: 200px; padding: 8px 0; font: inherit;
  color: var(--fg); background: transparent; border: 0;
  border-bottom: 1px solid var(--border); outline: none; }
.controls input[type=search]:focus { border-color: var(--accent); }
.controls select { padding: 8px 10px; font: inherit; color: var(--muted);
  background: transparent; border: 0; border-bottom: 1px solid var(--border);
  outline: none; cursor: pointer; }
.controls select.active { color: var(--accent); border-color: var(--accent); }

.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 22px 18px; }
.cardp { cursor: pointer; }
.art { position: relative; aspect-ratio: 1; border-radius: 12px; overflow: hidden;
  background: var(--pill); box-shadow: 0 2px 8px var(--shadow); }
.art img { width: 100%; height: 100%; object-fit: cover; display: block; }
.cardp .t { margin-top: 9px; font-size: 13.5px; font-weight: 600; line-height: 1.3;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.cardp .m { font-size: 12px; color: var(--muted); margin-top: 2px; }
.empty { color: var(--muted); text-align: center; padding: 60px 0; font-size: 17px; }

.back { display: inline-block; color: var(--muted); font-size: 14px; margin-bottom: 24px; }
.back:hover { color: var(--fg); }
.dhead { display: flex; gap: 26px; align-items: flex-start; margin-bottom: 30px; }
.dhead .art { width: 168px; flex: none; }
.dhead .letter { font-size: 60px; }
.dhead h2 { font: 600 34px/1.15 "Avenir Next", -apple-system, sans-serif; letter-spacing: -.02em; }
.dhead .a { color: var(--muted); margin-top: 4px; }
.dhead .s { margin-top: 14px; font-size: 13.5px; color: var(--muted); line-height: 1.9; }
.dhead .s span + span::before { content: " \\00B7  "; padding: 0 4px; color: var(--border); }
td.th { width: 46px; padding-right: 0; }
.epthumb { width: 40px; height: 40px; border-radius: 8px; object-fit: cover;
  display: block; background: var(--pill); }
span.epthumb { visibility: hidden; }

table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
th { text-align: left; color: var(--muted); font-size: 11px; font-weight: 500;
  text-transform: uppercase; letter-spacing: .08em; padding: 6px 10px 8px;
  border-bottom: 1px solid var(--border); }
td { padding: 9px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }
td.num { font-variant-numeric: tabular-nums; color: var(--muted); white-space: nowrap; }
td.pod { color: var(--muted); max-width: 220px; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
tr td:first-child { max-width: 420px; }
.pill { background: var(--pill); border-radius: 999px; padding: 2px 10px; font-size: 12px;
  white-space: nowrap; display: inline-block; }
.pill.play { color: var(--accent); font-weight: 600; }
/* which apps knew this episode — the whole point of the warehouse */
.app { background: var(--pill); border-radius: 5px; padding: 1px 5px; font-size: 10.5px;
  font-weight: 600; color: var(--muted); margin-right: 3px; letter-spacing: .02em; }
.app.multi { color: var(--accent); }
.more { margin: 22px auto 0; display: block; padding: 8px 20px; font: inherit;
  color: var(--muted); background: transparent; border: 1px solid var(--border);
  border-radius: 999px; cursor: pointer; }
.more:hover { color: var(--fg); border-color: var(--accent); }
footer { margin-top: 56px; color: var(--muted); font-size: 12px; }
</style>
</head>
<body>
<script type="application/json" id="d">{{DATA}}</script>
<main id="main"></main>
<script>
"use strict";
const D = JSON.parse(document.getElementById("d").textContent);
const [P, E, T] = [D.podcasts, D.episodes, D.totals];
// podcasts: [fid,title,author,img,priv,known,played,est_sec,measured,last,variants,cat,subcat]
// episodes: [fid,title,pub,dur,status,last,listened,img,eid,apps]
const epsByFam = new Map();
for (const e of E) { (epsByFam.get(e[0]) || epsByFam.set(e[0], []).get(e[0])).push(e); }
const famTitle = new Map(P.map(p => [p[0], p[1]]));
const famArt = new Map(P.map(p => [p[0], p[3]]));

const HUES = [212, 340, 25, 152, 262, 45, 190, 310];
const hue = s => HUES[[...s].reduce((a, c) => (a * 31 + c.charCodeAt(0)) >>> 0, 0) % HUES.length];
const esc = s => (s || "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const dark = () => {
  const t = document.documentElement.dataset.theme;
  return t ? t === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
};
const hours = s => { const h = s / 3600;
  return h >= 1000 ? (h/1000).toFixed(1).replace(/\\.0$/, "") + "K h"
       : h >= 10 ? Math.round(h) + " h" : h >= 1 ? h.toFixed(1) + " h"
       : Math.round(s/60) + " min"; };
const compact = n => n >= 10000 ? (n/1000).toFixed(1).replace(/\\.0$/, "") + "K" : n.toLocaleString();
const dur = s => { if (!s) return "\\u2014"; const h = Math.floor(s/3600), m = Math.round(s%3600/60);
  return h ? h + ":" + String(m).padStart(2, "0") + " h" : m + " min"; };
const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
const ymLabel = ym => MONTHS[+ym.slice(5) - 1] + " " + ym.slice(0, 4);
const FULLMONTHS = ["January","February","March","April","May","June","July",
  "August","September","October","November","December"];
const fullDate = d => { const [y, m, dd] = d.split("-");
  return FULLMONTHS[+m - 1] + " " + (+dd) + ", " + y; };
const withTip = (d, text) => '<span title="' + fullDate(d) + '">' + text + "</span>";
function fmtDate(d) {
  if (!d) return "\\u2014";
  const [y, m, dd] = d.split("-");
  const s = MONTHS[+m - 1] + " " + (+dd);
  return withTip(d, +y === new Date().getFullYear() ? s : s + ", " + y);
}
function fmtAgo(d) {
  if (!d) return "\\u2014";
  const days = Math.floor((Date.now() - new Date(d + "T12:00:00Z")) / 864e5);
  if (days <= 0) return withTip(d, "today");
  if (days === 1) return withTip(d, "yesterday");
  if (days < 7) return withTip(d, days + " days ago");
  if (days < 11) return withTip(d, "a week ago");
  if (days < 45) return withTip(d, Math.round(days / 7) + " weeks ago");
  return fmtDate(d);
}
const LOCK = '<span class="lock" title="includes a private feed"><svg viewBox="0 0 12 12"><path d="M6 1a3 3 0 0 0-3 3v1H2v6h8V5H9V4a3 3 0 0 0-3-3zm0 1.4A1.6 1.6 0 0 1 7.6 4v1H4.4V4A1.6 1.6 0 0 1 6 2.4z"/></svg></span>';
const APPNAME = { pocketcasts: "Pocket Casts", castro: "Castro", apple_podcasts: "Apple", spotify: "Spotify" };
const APPTAG  = { pocketcasts: "PC", castro: "Castro", apple_podcasts: "Apple", spotify: "Spotify" };
function appBadges(csv) {
  const apps = (csv || "").split(",").filter(Boolean);
  if (!apps.length) return "";
  const cls = apps.length > 1 ? "app multi" : "app";
  const title = apps.length > 1
    ? "heard in " + apps.map(a => APPNAME[a] || a).join(" + ") + " \\u2014 merged to one row"
    : "heard in " + (APPNAME[apps[0]] || apps[0]);
  return apps.map(a => '<span class="' + cls + '" title="' + esc(title) + '">' +
    esc(APPTAG[a] || a) + "</span>").join("");
}

/* --- theme --- */
const THEMES = ["auto", "light", "dark"];
function applyTheme(t) {
  if (t === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = t;
  const b = document.getElementById("themebtn");
  if (b) b.textContent = t === "auto" ? "\\u25D0 Auto" : t === "light" ? "\\u2600 Light" : "\\u263D Dark";
}
const store = { get: k => { try { return localStorage.getItem(k); } catch (e) { return null; } },
                set: (k, v) => { try { localStorage.setItem(k, v); } catch (e) {} } };
let theme = THEMES.includes(store.get("heard-theme")) ? store.get("heard-theme") : "auto";

function letterTile(title) {
  const h = hue(title || "?");
  const bg = "hsl(" + h + " 42% " + (dark() ? "26%" : "84%") + ")";
  const fg = "hsl(" + h + " 48% " + (dark() ? "82%" : "26%") + ")";
  return '<div class="letter" style="background:' + bg + ";color:" + fg + '">'
       + esc((title || "?").replace(/^the /i, "").trim()[0] || "?").toUpperCase() + "</div>";
}
// Art degrades down a chain: episode art -> the show's cover -> a lettered tile.
// Episode art is typically a remote URL while a cover may be embedded, so this is
// also what keeps the page usable with no network.
function imgFail(img) {
  if (img.dataset.fb) { img.src = img.dataset.fb; img.removeAttribute("data-fb"); }
  else if (img.classList.contains("epthumb")) { img.style.visibility = "hidden"; }
  else { img.outerHTML = letterTile(img.dataset.t || "?"); }
}
// Remote art goes through serve.py's on-disk cache, so scrolling doesn't re-hit
// podcast CDNs. From file:// there's no server, so use the raw URL.
// Ask for each image at the size it is drawn at (2x for retina). Serving one big
// size for every slot is what still stutters on a fast scroll: a 640px image in a
// 190px tile costs ~4x the bytes and decode it needs, times 150 tiles.
const THUMB = 96, TILE = 384, HERO = 768;
const proxied = location.protocol.startsWith("http");
const mediaSrc = (u, w) => (u && proxied && u.startsWith("http"))
  ? "/artcache?u=" + encodeURIComponent(u) + "&w=" + (w || TILE) : u;
// Images fade in on load rather than snapping. An image that is already decoded
// (cached, or re-rendered from another view) fires no load event, so mark those
// ready up front — otherwise they'd sit invisible forever.
function imgTag(src, fb, title, cls, eager, w) {
  return '<img ' + (eager ? 'loading="eager"' : 'loading="lazy"') + ' decoding="async"' +
    ' class="ph' + (cls ? " " + cls : "") + '"' +
    ' src="' + esc(mediaSrc(src, w)) + '" alt=""' +
    (fb ? ' data-fb="' + esc(mediaSrc(fb, w)) + '"' : "") +
    ' data-t="' + esc(title).replace(/"/g, "&quot;") +
    '" onload="this.classList.add(\\'on\\')" onerror="imgFail(this)">';
}
function settleImages(root) {
  for (const img of (root || document).querySelectorAll("img.ph")) {
    if (img.complete && img.naturalWidth) img.classList.add("on");
  }
}
function epArt(e) {
  const cover = famArt.get(e[0]);
  return e[7] ? [e[7], cover] : [cover, null];   // [src, fallback]
}
function epThumb(e) {
  const [src, fb] = epArt(e);
  return src ? imgTag(src, fb, "", "epthumb", false, THUMB)
             : '<span class="epthumb"></span>';
}
function art(p) {
  // covers are usually embedded, so there is nothing to defer: load them now
  const inner = p[3] ? imgTag(p[3], null, p[1], "", true, TILE) : letterTile(p[1]);
  return '<div class="art">' + inner + (p[4] ? LOCK : "") + "</div>";
}

/* --- monthly listening spark (hoverable) --- */
const SW = 1000, SH = 48, SB = 12;
function spark() {
  const M = D.months;
  if (!M.length) return "";
  const maxH = Math.max(...M.map(m => m[1])) || 1;
  const bw = Math.max(2, SW / M.length - 2);
  let s = "";
  M.forEach((m, i) => {
    if (m[1] > 0) {
      const h = Math.max(1.5, (SH - SB) * m[1] / maxH);
      s += '<rect class="bar" data-i="' + i + '" x="' + (i * SW / M.length).toFixed(1) +
           '" y="' + (SH - SB - h).toFixed(1) + '" width="' + bw.toFixed(1) +
           '" height="' + h.toFixed(1) + '" rx="1.2"/>';
    }
    if (m[0].endsWith("-01") && +m[0].slice(0, 4) % 2 === 0) {
      s += '<text x="' + (i * SW / M.length).toFixed(1) + '" y="' + (SH - 2) + '">' +
           m[0].slice(0, 4) + "</text>";
    }
  });
  return '<div class="sparkwrap" id="sparkwrap"><svg class="spark" id="sparksvg" viewBox="0 0 ' +
    SW + " " + SH + '" aria-label="Monthly listening hours">' + s + "</svg></div>";
}
function wireSpark() {
  const wrap = document.getElementById("sparkwrap");
  const svg = document.getElementById("sparksvg");
  if (!svg) return;
  let tip = null, hl = null;
  svg.addEventListener("mousemove", ev => {
    const r = svg.getBoundingClientRect();
    const M = D.months;
    const i = Math.floor((ev.clientX - r.left) / r.width * M.length);
    if (i < 0 || i >= M.length) return;
    if (!tip) { tip = document.createElement("div"); tip.className = "tip"; wrap.appendChild(tip); }
    tip.textContent = ymLabel(M[i][0]) + " \\u00B7 " +
      (M[i][1] >= 10 ? Math.round(M[i][1]) : M[i][1]) + " h";
    tip.style.left = (ev.clientX - wrap.getBoundingClientRect().left) + "px";
    tip.style.top = (ev.clientY - wrap.getBoundingClientRect().top) + "px";
    if (hl) hl.classList.remove("hl");
    hl = svg.querySelector('rect[data-i="' + i + '"]');
    if (hl) hl.classList.add("hl");
  });
  svg.addEventListener("mouseleave", () => {
    if (tip) { tip.remove(); tip = null; }
    if (hl) { hl.classList.remove("hl"); hl = null; }
  });
}

const SORTS = {
  played: { label: "Most played",     cmp: (a, b) => b[6] - a[6],
            metric: p => compact(p[6]) + " played" },
  hours:  { label: "Most hours",      cmp: (a, b) => b[7] - a[7],
            metric: p => "~" + hours(p[7]).replace("~", "") },
  recent: { label: "Recently played", cmp: (a, b) => (b[9] || "").localeCompare(a[9] || ""),
            metric: p => p[9] ? fmtAgo(p[9]) : "\\u2014" },
  az:     { label: "A\\u2013Z",       cmp: (a, b) => a[1].localeCompare(b[1]),
            metric: p => compact(p[6]) + " played" },
};
const ESORTS = {
  recent:   { label: "Recently played", cmp: (a, b) => (b[5] || "").localeCompare(a[5] || "") },
  listened: { label: "Most listened (measured)", cmp: (a, b) => (b[6] || 0) - (a[6] || 0) },
  longest:  { label: "Longest",          cmp: (a, b) => (b[3] || 0) - (a[3] || 0) },
};
let state = { sort: "played", filter: "", cat: "", subcat: "", scroll: 0,
              esort: "recent", efilter: "", eall: false, rshown: 0 };

const NAV = [["#/", "Recent", "recent"], ["#/podcasts", "Podcasts", "pods"],
             ["#/episodes", "Episodes", "eps"]];
function navLinks(tab) {
  return '<nav class="links">' +
    NAV.map(([h, l, k]) => '<a href="' + h + '"' + (k === tab ? ' class="on"' : "") +
      ">" + l + "</a>").join("") + "</nav>";
}
// Identical header on EVERY view: title + nav share one baseline row, so the
// links never move as you switch tabs.
function header(tab) {
  return '<button id="themebtn"></button><div class="tophead">' +
    '<a href="#/"><h1 class="mast">Heard</h1></a>' + navLinks(tab) + "</div>";
}
function homeBlock() {
  const measured = T.measured.reduce((a, m) => a + m[1], 0);
  const totalSec = measured + T.est_sec;
  let sub = T.measured.map(m => Math.round(m[1] / 3600).toLocaleString() + " h " +
    (APPNAME[m[0]] || m[0])).join(" \\u00B7 ");
  if (T.est_sec) sub += " \\u00B7 ~" + Math.round(T.est_sec / 3600).toLocaleString() +
    " h estimated from episode lengths";
  const since = T.first_play ? " since " + T.first_play.slice(0, 4) : "";
  return '<div class="homeblock">' +
  '<p class="prose"><b>' + T.played.toLocaleString() + " episodes</b> across <b>" +
    T.shows + " shows</b>" + (totalSec ? " \\u2014 \\u2248<b>" +
      Math.round(totalSec / 3600).toLocaleString() + " hours</b> of listening" : "") +
    since + ".</p>" +
  (sub ? '<p class="prosesub">' + sub + "</p>" : "") + spark() + "</div>";
}

const byEid = new Map(E.map(e => [e[8], e]));
// The mosaic loads eagerly. Lazy-loading only pays when each image is a slow
// remote fetch; served warm and downscaled from serve.py's cache they arrive
// faster than you can scroll, and deferring them is precisely what makes tiles
// pop in. The episode TABLE keeps lazy thumbs — that list runs to thousands.
function heroTile(eid, i) {
  const e = byEid.get(eid);
  if (!e) return "";
  const show = famTitle.get(e[0]) || "";
  const [src, fb] = epArt(e);
  const inner = src ? imgTag(src, fb, show, "", true, i ? TILE : HERO)
                    : letterTile(show);
  return '<a class="hero" href="#/p/' + e[0] + '">' + inner +
    '<div class="ov"><div class="et">' + esc(e[1]) + "</div>" +
    '<div class="es">' + esc(show) + " \\u00B7 " + fmtAgo(e[5]) + "</div></div></a>";
}
// The mosaic is a fixed 6-column grid and the lead tile takes 9 cells (3x3), so
// it displaces 8 ordinary tiles: a count renders even rows when (n + 8) % 6 == 0.
// Round the initial batch up and the final total down so the wall never ends ragged.
const MCOLS = 6, HERO_EXTRA = 8;
const evenUp = n => { while ((n + HERO_EXTRA) % MCOLS) n++; return n; };
const evenDown = n => { while (n > 1 && (n + HERO_EXTRA) % MCOLS) n--; return n; };
const RECENT_INIT = evenUp(150);
const RECENT_PAGE = 300;
function viewRecent() {
  document.title = "Heard";
  const total = evenDown(D.recent.length);
  if (!state.rshown) state.rshown = RECENT_INIT;
  const list = D.recent.slice(0, Math.min(state.rshown, total));
  return header("recent") + homeBlock() +
    '<div class="mosaic" id="mosaic">' + list.map(heroTile).join("") + "</div>" +
    (total > state.rshown ?
      '<button class="more" id="rmore">Show more \\u00B7 ' +
        (total - state.rshown).toLocaleString() + " older</button>" : "") +
    '<footer>Heard \\u00B7 generated ' + D.generated + "</footer>";
}

function aggBy(list, keyFn) {
  const agg = new Map();
  for (const p of list) {
    const c = keyFn(p);
    if (c === null) continue;
    const a = agg.get(c) || { n: 0, sec: 0 };
    a.n += 1; a.sec += p[7];
    agg.set(c, a);
  }
  return [...agg.entries()].sort((a, b) => b[1].sec - a[1].sec);
}
// Category filtering lives in the controls row as quiet selects: a category
// dropdown (with show counts + hours), and a subcategory dropdown that only
// exists while a category with subcategories is chosen.
function catSelects() {
  const opt = (val, label, sel) =>
    '<option value="' + esc(val) + '"' + (sel ? " selected" : "") + ">" + esc(label) + "</option>";
  let html = '<select id="cat"' + (state.cat ? ' class="active"' : "") + ">" +
    opt("", "All categories", !state.cat) +
    aggBy(P, p => p[11] || "Uncategorized").map(([c, a]) =>
      opt(c, c + " \\u00B7 " + a.n + " \\u00B7 " + hours(a.sec).replace("~", ""),
          state.cat === c)).join("") + "</select>";
  if (state.cat) {
    const subs = aggBy(P.filter(p => (p[11] || "Uncategorized") === state.cat),
                       p => p[12] || null);
    if (subs.length > 1) {
      html += '<select id="subcat"' + (state.subcat ? ' class="active"' : "") + ">" +
        opt("", "All " + state.cat, !state.subcat) +
        subs.map(([c, a]) => opt(c, c + " \\u00B7 " + a.n, state.subcat === c)).join("") +
        "</select>";
    }
  }
  return html;
}
function grid() {
  const f = state.filter.toLowerCase();
  let list = P.filter(p => !f || (p[1] + " " + (p[2] || "")).toLowerCase().includes(f));
  if (state.cat) list = list.filter(p => (p[11] || "Uncategorized") === state.cat);
  if (state.cat && state.subcat) list = list.filter(p => p[12] === state.subcat);
  list = list.slice().sort(SORTS[state.sort].cmp);
  const cards = list.map(p =>
    '<a class="cardp" href="#/p/' + p[0] + '">' + art(p) +
    '<div class="t">' + esc(p[1]) + '</div><div class="m">' +
    SORTS[state.sort].metric(p) + "</div></a>").join("");
  return cards ? '<div class="grid">' + cards + "</div>"
               : '<div class="empty">No podcasts match \\u201C' + esc(state.filter) + '\\u201D</div>';
}
function viewPodcasts() {
  document.title = "Podcasts \\u00B7 Heard";
  return header("pods") +
  '<div class="controls"><input type="search" id="q" placeholder="Filter podcasts\\u2026" value="' +
  esc(state.filter) + '">' + catSelects() + '<select id="sort">' +
  Object.entries(SORTS).map(([k, s]) => '<option value="' + k + '"' +
    (k === state.sort ? " selected" : "") + ">" + s.label + "</option>").join("") +
  '</select></div><div id="gridwrap">' + grid() + "</div>";
}

const STATUS_PILL = ['<span class="pill">\\u2014</span>',
  '<span class="pill">In progress</span>', '<span class="pill play">Played</span>'];
const CAP = 200;
const EP_HEAD = "<thead><tr><th></th><th>Episode</th><th>Podcast</th><th>Published</th>" +
                "<th>Length</th><th>Status</th><th>Apps</th><th>Last played</th></tr></thead>";
function epRows(list) {
  return list.map(e =>
    '<tr><td class="th">' + epThumb(e) + "</td><td>" + esc(e[1]) +
    '</td><td class="pod"><a href="#/p/' + e[0] + '">' +
    esc(famTitle.get(e[0]) || "") + '</a></td><td class="num">' + fmtDate(e[2]) +
    '</td><td class="num">' + dur(e[3]) + "</td><td>" + STATUS_PILL[e[4]] +
    "</td><td>" + appBadges(e[9]) +
    '</td><td class="num">' + fmtDate(e[5]) + "</td></tr>").join("");
}
function epTable() {
  const f = state.efilter.toLowerCase();
  let list = E.filter(e => e[5] || e[4] || e[6]);
  if (f) list = list.filter(e =>
    (e[1] + " " + (famTitle.get(e[0]) || "")).toLowerCase().includes(f));
  if (state.esort === "listened") list = list.filter(e => e[6]);
  list = list.slice().sort(ESORTS[state.esort].cmp);
  const total = list.length;
  if (!state.eall) list = list.slice(0, CAP);
  if (!total) return '<div class="empty">No episodes match \\u201C' + esc(state.efilter) + '\\u201D</div>';
  return "<table>" + EP_HEAD + "<tbody>" + epRows(list) + "</tbody></table>" +
    (total > CAP && !state.eall ?
      '<button class="more" id="emore">Show all ' + total.toLocaleString() + " episodes</button>" : "");
}
function viewEpisodes() {
  document.title = "Episodes \\u00B7 Heard";
  return header("eps") +
  '<div class="controls"><input type="search" id="eq" placeholder="Filter episodes\\u2026" value="' +
  esc(state.efilter) + '"><select id="esort">' +
  Object.entries(ESORTS).map(([k, s]) => '<option value="' + k + '"' +
    (k === state.esort ? " selected" : "") + ">" + s.label + "</option>").join("") +
  '</select></div><div id="epwrap">' + epTable() + "</div>";
}

function viewDetail(fid, showAll) {
  const p = P.find(x => x[0] === fid);
  if (!p) { location.hash = "#/"; return ""; }
  document.title = p[1] + " \\u00B7 Heard";
  const eps = epsByFam.get(fid) || [];
  const shown = showAll ? eps : eps.slice(0, CAP);
  const rows = shown.map(e =>
    '<tr><td class="th">' + epThumb(e) + "</td><td>" + esc(e[1]) +
    '</td><td class="num">' + fmtDate(e[2]) +
    '</td><td class="num">' + dur(e[3]) + "</td><td>" + STATUS_PILL[e[4]] +
    "</td><td>" + appBadges(e[9]) +
    '</td><td class="num">' + fmtDate(e[5]) + "</td></tr>").join("");
  const stats = [compact(p[6]) + " played",
                 p[7] ? "~" + hours(p[7]).replace("~", "") : null,
                 p[9] ? "last played " + fmtAgo(p[9]) : null,
                 p[11] ? p[11] + (p[12] ? " \\u00B7 " + p[12] : "") : null,
                 p[10] > 1 ? p[10] + " feeds" : null,
                 p[4] ? "private feed" : null].filter(Boolean);
  return header(null) +
  '<a class="back" href="#/podcasts" onclick="if(history.length>1){history.back();return false}">\\u2190 Back</a>' +
  '<div class="dhead">' + art(p) + "<div><h2>" + esc(p[1]) + "</h2>" +
  (p[2] ? '<div class="a">' + esc(p[2]) + "</div>" : "") +
  '<div class="s">' + stats.map(s => "<span>" + s + "</span>").join("") + "</div></div></div>" +
  "<table><thead><tr><th></th><th>Episode</th><th>Published</th><th>Length</th><th>Status</th>" +
  "<th>Apps</th><th>Last played</th></tr></thead><tbody>" + rows + "</tbody></table>" +
  (eps.length > CAP && !showAll ?
    '<button class="more" id="more">Show all ' + eps.length.toLocaleString() + " episodes</button>" : "");
}

function wirePodcastControls(main) {
  const q = document.getElementById("q");
  let t;
  q.oninput = () => { clearTimeout(t); t = setTimeout(() => {
    state.filter = q.value;
    document.getElementById("gridwrap").innerHTML = grid();
  }, 100); };
  document.getElementById("sort").onchange = ev => {
    state.sort = ev.target.value;
    document.getElementById("gridwrap").innerHTML = grid();
  };
  const rerenderPods = () => {  // the subcategory select appears/disappears
    main.innerHTML = viewPodcasts();
    postRender("pods", main);
  };
  document.getElementById("cat").onchange = ev => {
    state.cat = ev.target.value;
    state.subcat = "";
    rerenderPods();
  };
  const sub = document.getElementById("subcat");
  if (sub) sub.onchange = ev => { state.subcat = ev.target.value; rerenderPods(); };
}
function wireEpisodeControls() {
  const q = document.getElementById("eq");
  let t;
  const rewrap = () => {
    document.getElementById("epwrap").innerHTML = epTable();
    const btn = document.getElementById("emore");
    if (btn) btn.onclick = () => { state.eall = true; rewrap(); };
  };
  q.oninput = () => { clearTimeout(t); t = setTimeout(() => {
    state.efilter = q.value; state.eall = false; rewrap(); }, 100); };
  document.getElementById("esort").onchange = ev => {
    state.esort = ev.target.value; state.eall = false; rewrap(); };
  const btn = document.getElementById("emore");
  if (btn) btn.onclick = () => { state.eall = true; rewrap(); };
}

function postRender(view, main) {
  applyTheme(theme);
  settleImages(main);
  const b = document.getElementById("themebtn");
  if (b) b.onclick = () => {
    theme = THEMES[(THEMES.indexOf(theme) + 1) % 3];
    store.set("heard-theme", theme);
    applyTheme(theme);
    render();
  };
  if (view === "recent") {
    wireSpark();
    const btn = document.getElementById("rmore");
    if (btn) btn.onclick = () => {   // append a page in place; no scroll jump
      const total = evenDown(D.recent.length);
      const next = Math.min(state.rshown + RECENT_PAGE, total);
      const from = state.rshown;   // keep the real index: appended tiles are never eager
      document.getElementById("mosaic").insertAdjacentHTML(
        "beforeend", D.recent.slice(from, next).map((id, k) => heroTile(id, from + k)).join(""));
      settleImages();
      state.rshown = next;
      if (next >= total) btn.remove();
      else btn.textContent = "Show more \\u00B7 " + (total - next).toLocaleString() + " older";
    };
  } else if (view === "pods") {
    wirePodcastControls(main);
  } else if (view === "eps") {
    wireEpisodeControls();
  }
}

function render() {
  const main = document.getElementById("main");
  const m = location.hash.match(/^#\\/p\\/(\\d+)/);
  if (m) {
    main.innerHTML = viewDetail(+m[1], false);
    scrollTo(0, 0);
    postRender("detail", main);
    const btn = document.getElementById("more");
    if (btn) btn.onclick = () => {
      main.innerHTML = viewDetail(+m[1], true);
      postRender("detail", main);
    };
  } else if (location.hash.startsWith("#/episodes")) {
    main.innerHTML = viewEpisodes();
    scrollTo(0, 0);
    postRender("eps", main);
  } else if (location.hash.startsWith("#/podcasts")) {
    main.innerHTML = viewPodcasts();
    scrollTo(0, state.scroll);
    postRender("pods", main);
  } else {
    main.innerHTML = viewRecent();
    scrollTo(0, 0);
    postRender("recent", main);
  }
}
addEventListener("scroll", () => {
  if (location.hash.startsWith("#/podcasts")) state.scroll = scrollY;
}, { passive: true });
addEventListener("hashchange", render);
render();
</script>
</body>
</html>
"""


def main():
    if not DB.exists():
        sys.exit(f"no {DB.name}; run rebuild.py first")
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)

    data = {"totals": query_totals(db), "podcasts": query_podcasts(db),
            "episodes": query_episodes(db), "months": query_months(db),
            "recent": query_recent(db),
            "generated": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
    blob = json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    json.loads(blob.replace("<\\/", "</"))  # round-trip integrity check

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(TEMPLATE.replace("{{DATA}}", blob), encoding="utf-8")

    size = OUT.stat().st_size
    t, pods, eps = data["totals"], data["podcasts"], data["episodes"]
    with_art = sum(1 for p in pods if p[3])
    total_h = (sum(m[1] for m in t["measured"]) + t["est_sec"]) // 3600
    print(f"app/index.html: {size:,} bytes | {len(pods)} families "
          f"({with_art} with artwork) | {len(eps):,} episodes | "
          f"{len(data['months'])} chart months | {t['played']:,} played, "
          f"{t['shows']} shows, ~{total_h:,} h")
    if size > SIZE_BUDGET:
        sys.exit(f"FAIL: {size:,} bytes exceeds the {SIZE_BUDGET:,} budget")
    # A library with no dated plays is legitimate (some app exports carry none) —
    # it just renders without the spark. Having nothing to show at all is not.
    if not (pods and eps):
        sys.exit("FAIL: no podcasts or episodes in the library")


if __name__ == "__main__":
    main()
