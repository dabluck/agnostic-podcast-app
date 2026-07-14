#!/usr/bin/env python3
"""Audit library.sqlite dedup quality: missed podcast dupes, over-aggressive
family links, and duplicate episode rows within a podcast."""
import re, sqlite3
from difflib import SequenceMatcher
from itertools import combinations

db = sqlite3.connect('library.sqlite')
db.row_factory = sqlite3.Row

STRIP = re.compile(r"\((?:private feed[^)]*|ad-?free[^)]*|the binge[^)]*)\)|"
                   r"\[[^\]]*\]|\(([^)]*)\)", re.I)
def norm(t):
    t = STRIP.sub(lambda m: m.group(1) or '', t or '')
    t = re.sub(r"[^a-z0-9 ]", " ", t.lower())
    t = re.sub(r"\b(podcast|the|a|an|with|show)\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()

pods = {r['id']: r for r in db.execute(
    "SELECT p.id, p.title, p.is_private, p.feed_url,"
    "       coalesce(p.public_podcast_id, p.id) AS family,"
    "       (SELECT count(*) FROM episodes e WHERE e.podcast_id = p.id) AS n_eps"
    " FROM podcasts p")}
sigs = {}   # podcast_id -> set of episode signatures (title|date) and cores
for pid in pods:
    s = set()
    for e in db.execute("SELECT title, published_at, enclosure_core FROM episodes"
                        " WHERE podcast_id=?", (pid,)):
        if e['title'] and e['published_at']:
            s.add(('td', norm(e['title']), e['published_at'][:10]))
        if e['enclosure_core']:
            s.add(('core', e['enclosure_core']))
    sigs[pid] = s

print("=== 1) possible MISSED dupes (different families, similar identity) ===")
by_norm = {}
for pid, p in pods.items():
    by_norm.setdefault(norm(p['title']), []).append(pid)
seen_pairs = set()
def report_pair(a, b, why):
    key = tuple(sorted((pods[a]['family'], pods[b]['family'])))
    if key in seen_pairs or key[0] == key[1]:
        return
    seen_pairs.add(key)
    ov = len(sigs[a] & sigs[b])
    print(f"  [{a}] '{pods[a]['title'][:48]}' ({pods[a]['n_eps']} eps) vs "
          f"[{b}] '{pods[b]['title'][:48]}' ({pods[b]['n_eps']} eps) — {why}, ep-overlap {ov}")
for nt, ids in by_norm.items():
    if nt and len({pods[i]['family'] for i in ids}) > 1:
        fams = {}
        for i in ids:
            fams.setdefault(pods[i]['family'], i)
        for a, b in combinations(fams.values(), 2):
            report_pair(a, b, "identical normalized title")
# fuzzy pass on distinct titles (only pods with episodes, keep it tractable)
names = [(pid, norm(p['title'])) for pid, p in pods.items() if p['n_eps'] > 0]
for (a, na), (b, nb) in combinations(names, 2):
    if na and nb and na != nb and pods[a]['family'] != pods[b]['family']:
        if abs(len(na) - len(nb)) <= 12 and na[:4] == nb[:4] \
           and SequenceMatcher(None, na, nb).ratio() >= 0.88:
            report_pair(a, b, "fuzzy title")
# shared content across families
for a, b in combinations([p for p in pods if sigs[p]], 2):
    if pods[a]['family'] != pods[b]['family']:
        ov = len(sigs[a] & sigs[b])
        if ov >= 3:
            report_pair(a, b, f"shared content")

print("\n=== 2) possibly OVER-AGGRESSIVE family links (zero shared content) ===")
for r in db.execute("SELECT id, title, public_podcast_id FROM podcasts"
                    " WHERE public_podcast_id IS NOT NULL"):
    a, b = r['id'], r['public_podcast_id']
    na, nb = pods[a]['n_eps'], pods[b]['n_eps']
    if min(na, nb) >= 4 and not (sigs[a] & sigs[b]):
        flag = " PRIVATE" if pods[a]['is_private'] else ""
        print(f"  [{a}] '{pods[a]['title'][:48]}' ({na} eps{flag}) -> "
              f"[{b}] '{pods[b]['title'][:48]}' ({nb} eps): no shared episodes at all")

print("\n=== 3) duplicate episode rows within one podcast ===")
n = 0
for r in db.execute("""
    SELECT e1.podcast_id, p.title AS pod, e1.id AS id1, e2.id AS id2,
           e1.title AS t1, e2.title AS t2, e1.guid AS g1, e2.guid AS g2,
           substr(e1.published_at,1,10) AS d
    FROM episodes e1 JOIN episodes e2
      ON e1.podcast_id = e2.podcast_id AND e1.id < e2.id
     AND ((e1.enclosure_core != '' AND e1.enclosure_core = e2.enclosure_core)
       OR (e1.title = e2.title AND substr(e1.published_at,1,10) = substr(e2.published_at,1,10)))
    JOIN podcasts p ON p.id = e1.podcast_id"""):
    n += 1
    if n <= 25:
        why = "same enclosure" if r['g1'] != r['g2'] else "same title+date"
        print(f"  '{r['pod'][:36]}': eps {r['id1']}/{r['id2']} '{(r['t1'] or '')[:44]}' "
              f"({r['d']}) guids: {str(r['g1'])[:28]!r} vs {str(r['g2'])[:28]!r}")
print(f"  total duplicate episode pairs: {n}")

print("\n=== 4) same-title pairs 1 day apart (timezone-boundary dupes dedupe would miss) ===")
from datetime import date
def _d(s):
    try: return date.fromisoformat((s or '')[:10])
    except ValueError: return None
n = 0
for pid in pods:
    by_t = {}
    for e in db.execute("SELECT id, title, published_at, enclosure_core FROM episodes"
                        " WHERE podcast_id=?", (pid,)):
        if e['title']:
            by_t.setdefault(norm(e['title']), []).append(e)
    for group in by_t.values():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                da, db_ = _d(a['published_at']), _d(b['published_at'])
                if da and db_ and abs((da - db_).days) == 1 \
                   and (not a['enclosure_core'] or a['enclosure_core'] != b['enclosure_core']):
                    n += 1
                    if n <= 10:
                        print(f"  [{pods[pid]['title'][:30]}] {a['title'][:45]!r} {da} vs {db_}")
print(f"  total: {n}")
