#!/usr/bin/env python3
"""Render app/index.html from library.sqlite — a minimal, self-contained viewer.

Opens the warehouse READ-ONLY and emits one static HTML file (inline CSS, no
network, no external assets, theme-aware). The browsing unit is the podcast
family (feed variants combined via the podcast_families view); each family lists
its interacted-with episodes with the cross-app merged status.

This is intentionally small — a starting point. Regenerate any time with
`python3 viewer.py`; serve it with `python3 serve.py` or open the file directly.
"""

import html
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
DB = HERE / "library.sqlite"
OUT = HERE / "app" / "index.html"
STATUS_ORDER = {"played": 2, "partial": 1, "unplayed": 0}


def load(db):
    """family_id -> {title, author, episodes:[...]}, populated from the views."""
    fams = {}
    for fid, title, author in db.execute(
        "SELECT id, title, author FROM podcasts WHERE id IN"
        " (SELECT DISTINCT family_id FROM podcast_families)"
    ):
        fams[fid] = {"title": title or "(untitled)", "author": author, "episodes": []}
    for fid, ep, status, pub, last in db.execute("""
        SELECT f.family_id, m.episode, m.status, m.published_at, m.last_played_at
        FROM merged_episode_state m
        JOIN episodes e ON e.id = m.episode_id
        JOIN podcast_families f ON f.podcast_id = e.podcast_id
        ORDER BY m.published_at DESC"""):
        if fid in fams:
            fams[fid]["episodes"].append(
                {"title": ep, "status": status, "published": pub, "last": last})
    return fams


def esc(s):
    return html.escape(str(s)) if s is not None else ""


def render(fams):
    n_fam = sum(1 for f in fams.values() if f["episodes"])
    n_ep = sum(len(f["episodes"]) for f in fams.values())
    n_played = sum(1 for f in fams.values() for e in f["episodes"] if e["status"] == "played")

    # families with the most listening first
    ordered = sorted(
        (f for f in fams.values() if f["episodes"]),
        key=lambda f: (-sum(1 for e in f["episodes"] if e["status"] == "played"),
                       f["title"].lower()))

    rows = []
    for f in ordered:
        eps = sorted(f["episodes"],
                     key=lambda e: (-STATUS_ORDER.get(e["status"], 0), e["published"] or ""))
        played = sum(1 for e in f["episodes"] if e["status"] == "played")
        partial = sum(1 for e in f["episodes"] if e["status"] == "partial")
        ep_items = "\n".join(
            f'<li class="s-{esc(e["status"])}"><span class="dot"></span>'
            f'<span class="et">{esc(e["title"])}</span>'
            f'<span class="ed">{esc((e["published"] or "")[:10])}</span></li>'
            for e in eps)
        rows.append(f"""<details class="fam">
  <summary>
    <span class="ft">{esc(f["title"])}</span>
    <span class="fa">{esc(f["author"] or "")}</span>
    <span class="fc">{played} played · {partial} in progress · {len(eps)} tracked</span>
  </summary>
  <ul class="eps">{ep_items}</ul>
</details>""")

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Heard — Agnostic Podcast App</title>
<style>
  :root {{ color-scheme: light dark; --bg:#fff; --fg:#161616; --mut:#6b6b70;
    --card:#f6f6f7; --line:#e3e3e6; --played:#2f9e5a; --partial:#c98a1b; --unplayed:#b6b6bb; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --bg:#141416; --fg:#ececee;
    --mut:#9a9aa2; --card:#1e1e21; --line:#2c2c30; --played:#54c07d;
    --partial:#e0a63c; --unplayed:#55555c; }} }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    background:var(--bg); color:var(--fg); padding:2rem 1rem 4rem; }}
  .wrap {{ max-width:820px; margin:0 auto; }}
  h1 {{ font-size:1.4rem; margin:0 0 .25rem; }}
  .sub {{ color:var(--mut); margin:0 0 1.5rem; }}
  .stats {{ display:flex; gap:1.5rem; flex-wrap:wrap; margin-bottom:1.5rem; }}
  .stat b {{ display:block; font-size:1.6rem; }}
  .stat span {{ color:var(--mut); font-size:.85rem; }}
  .fam {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
    margin-bottom:.6rem; padding:.2rem .9rem; }}
  summary {{ cursor:pointer; padding:.7rem 0; display:grid;
    grid-template-columns:1fr auto; gap:.15rem .8rem; align-items:baseline; }}
  summary::-webkit-details-marker {{ display:none; }}
  .ft {{ font-weight:600; }}
  .fa {{ color:var(--mut); font-size:.85rem; grid-column:1; }}
  .fc {{ color:var(--mut); font-size:.8rem; grid-column:2; grid-row:1/3; white-space:nowrap; }}
  .eps {{ list-style:none; margin:0 0 .6rem; padding:.4rem 0 0; border-top:1px solid var(--line); }}
  .eps li {{ display:grid; grid-template-columns:auto 1fr auto; gap:.6rem;
    align-items:center; padding:.28rem 0; font-size:.9rem; }}
  .dot {{ width:9px; height:9px; border-radius:50%; background:var(--unplayed); }}
  .s-played .dot {{ background:var(--played); }}
  .s-partial .dot {{ background:var(--partial); }}
  .ed {{ color:var(--mut); font-variant-numeric:tabular-nums; font-size:.8rem; }}
</style></head>
<body><div class="wrap">
<h1>Heard</h1>
<p class="sub">Your cross-app podcast listening warehouse.</p>
<div class="stats">
  <div class="stat"><b>{n_fam}</b><span>shows</span></div>
  <div class="stat"><b>{n_ep}</b><span>episodes tracked</span></div>
  <div class="stat"><b>{n_played}</b><span>played</span></div>
</div>
{"".join(rows) or "<p class='sub'>No episodes yet — run the ingesters and rebuild.py first.</p>"}
</div></body></html>"""


def main():
    if not DB.exists():
        sys.exit(f"no {DB.name}; run rebuild.py first")
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(render(load(db)))
    print(f"wrote {OUT.relative_to(HERE)}")


if __name__ == "__main__":
    main()
