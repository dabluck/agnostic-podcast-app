#!/usr/bin/env python3
"""Group podcast feed variants into families (see DEDUP_RULES.md).

A "family" is one show known under several feeds: app-specific URLs, moved
feeds, and private (tokenized) variants — including several generations of
private tokens for the same show. Rows are never merged; secondaries point
public_podcast_id at the family primary.

Rules applied here, in order:
  0. is_private backfill: any podcast whose URL matches PRIVATE_URL is private
     (tokenized feeds arrive from apps that don't flag them).
  1. Same normalized title => same family. Normalization strips "(private
     feed …)"-style parentheticals and punctuation. Safe because a wrong
     same-title link has never been observed, while sparse episode data makes
     overlap-based confirmation unreliable (different apps touch different
     eras of a show, so zero overlap is expected).
  2. Variant-suffix match: a title that reduces to an existing family's title
     after dropping a trailing membership marker ("Club", "Patrons-Only …",
     "Bonus …", "Members", "Archives", "Ad-Free", "Premium", "Plus") joins
     that family IF it is private OR shares >= 2 content signatures
     (normalized title+date, or enclosure core) with the family.
  3. <podcast:guid> equality always links. (Inert until something populates
     podcasts.podcast_guid — no shipped ingester does yet; it's an enrichment hook.)
Primary selection: public row with the most canonical episodes; if the whole
family is private, the private row with the most episodes.
"""

import sqlite3
from pathlib import Path

from identity import PRIVATE_URL, norm_title as norm, strip_suffix

db = sqlite3.connect(Path(__file__).parent / "library.sqlite")
db.row_factory = sqlite3.Row

# Idempotency = rebuild links from scratch, never layer on a previous run:
# stale links from rules that no longer fire would otherwise persist, and a
# flipped primary would create a 2-cycle the flattener resolves destructively.
db.execute("UPDATE podcasts SET public_podcast_id = NULL")

# --- rule 0: is_private backfill from URL patterns (any alias) ---
n_flagged = 0
for r in db.execute(
    "SELECT p.id, p.title, group_concat(u.url, ' ') urls FROM podcasts p"
    " JOIN podcast_feed_urls u ON u.podcast_id = p.id WHERE p.is_private = 0 GROUP BY p.id"):
    if PRIVATE_URL.search(r["urls"] or ""):
        db.execute("UPDATE podcasts SET is_private = 1 WHERE id = ?", (r["id"],))
        print(f"  flagged private by URL: [{r['id']}] {r['title']!r}")
        n_flagged += 1
print(f"{n_flagged} podcasts newly flagged private\n")

pods = {r["id"]: dict(r) for r in db.execute(
    "SELECT id, title, is_private, podcast_guid,"
    "       (SELECT count(*) FROM episodes e WHERE e.podcast_id = podcasts.id) n_eps"
    " FROM podcasts")}


def sigs(pid):
    s = set()
    for e in db.execute("SELECT title, published_at, enclosure_core FROM episodes"
                        " WHERE podcast_id=?", (pid,)):
        if e["title"] and e["published_at"]:
            s.add((norm(e["title"]), e["published_at"][:10]))
        if e["enclosure_core"]:
            s.add(e["enclosure_core"])
    return s


families = {}  # norm title -> set of podcast ids
for pid, p in pods.items():
    families.setdefault(norm(p["title"]), set()).add(pid)
guid_owner = {}
for pid, p in pods.items():
    if p["podcast_guid"]:
        families.setdefault(("guid", p["podcast_guid"]), set()).add(pid)

# --- rule 2: fold variant-suffix titles into their base family ---
for pid, p in pods.items():
    base = strip_suffix(p["title"])
    me = norm(p["title"])
    if base and base != me and base in families and families[base] - {pid}:
        target_ids = families[base]
        if p["is_private"]:
            ok, why = True, "private variant"
        else:
            fam_sigs = set().union(*(sigs(t) for t in target_ids))
            ov = len(sigs(pid) & fam_sigs)
            ok, why = ov >= 2, f"content overlap {ov}"
        if ok:
            families[base].add(pid)
            families[me].discard(pid)
            print(f"  suffix fold: [{pid}] {p['title']!r} -> family {base!r} ({why})")

# --- link every multi-member family to its primary ---
n_link = 0
assigned = {}
for key, ids in families.items():
    ids = {i for i in ids if i in pods}
    if len(ids) < 2:
        continue
    public = [i for i in ids if not pods[i]["is_private"]]
    primary = max(public or ids, key=lambda i: pods[i]["n_eps"])
    for other in sorted(ids):
        if other == primary or assigned.get(other) == primary:
            continue
        assigned[other] = primary
        kind = "private->public" if pods[other]["is_private"] and not pods[primary]["is_private"] \
            else ("private variants" if pods[other]["is_private"] else "public duplicate")
        db.execute("UPDATE podcasts SET public_podcast_id=? WHERE id=?", (primary, other))
        print(f"  linked [{other}] {pods[other]['title'][:45]!r} -> "
              f"[{primary}] {pods[primary]['title'][:45]!r} ({kind})")
        n_link += 1
# --- manual overrides: owner-ruled links the rules can't derive ---
# manual_links.json entries: variant_url -> primary_url ("match": exact|prefix
# against podcast_feed_urls). Keyed by URL so rulings survive full rebuilds.
import json
ml_path = Path(__file__).parent / "manual_links.json"
if ml_path.exists():
    url2pid = dict(db.execute("SELECT url, podcast_id FROM podcast_feed_urls"))
    for m in json.load(open(ml_path)):
        if m.get("match") == "prefix":
            vids = {p for u, p in url2pid.items() if u.startswith(m["variant_url"])}
        else:
            vids = {url2pid[m["variant_url"]]} if m["variant_url"] in url2pid else set()
        tgt = url2pid.get(m["primary_url"])
        if tgt is None or not vids:
            print(f"  manual link SKIPPED (url not found): {m['note']}")
            continue
        for v in vids - {tgt}:
            db.execute("UPDATE podcasts SET public_podcast_id=? WHERE id=?", (tgt, v))
            assigned[v] = tgt
            print(f"  manual link: [{v}] -> [{tgt}] ({m['note']})")
            n_link += 1

# flatten chains (A -> B -> C becomes A -> C) and keep roots clean
for _ in range(10):  # bounded: a cross-family cycle must not hang the run
    db.execute("UPDATE podcasts SET public_podcast_id = NULL WHERE public_podcast_id = id")
    if not db.execute(
    "UPDATE podcasts SET public_podcast_id ="
    " (SELECT p2.public_podcast_id FROM podcasts p2 WHERE p2.id = podcasts.public_podcast_id)"
    " WHERE public_podcast_id IN"
    " (SELECT id FROM podcasts WHERE public_podcast_id IS NOT NULL)").rowcount:
        break
db.execute("UPDATE podcasts SET public_podcast_id = NULL WHERE public_podcast_id = id")

db.commit()
fams = db.execute("SELECT count(DISTINCT coalesce(public_podcast_id, id)) FROM podcasts").fetchone()[0]
tot = db.execute("SELECT count(*) FROM podcasts").fetchone()[0]
print(f"\n{n_link} links set; {tot} podcasts in {fams} families")
