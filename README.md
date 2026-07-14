# Agnostic Podcast App

A tiny, dependency-free toolkit for building a **personal cross-app podcast
listening warehouse**. It ingests the play state you export from your podcast
apps into one canonical SQLite database, keyed by *public* podcast identity
(feed URL, RSS `<guid>`, enclosure URL), so the same episode heard in two
different apps resolves to one row. Then it deduplicates shows and episodes,
recovers missing RSS guids from public feeds, and renders a simple static
viewer.

Pure Python standard library. No installs, no services, no accounts.

## What this is NOT

This is the important part.

- **It does not talk to any podcast app's private or authenticated API.** There
  is no login, no token, no account scraping, no undocumented endpoint. The
  ingesters read **local export files that you produce yourself** (your own
  on-device database / backup).
- **The only network it does** is fetching **public RSS feeds** — the same
  feeds any podcast client fetches — to read episode `<guid>`s and metadata
  (`resolve_guids.py`). Responses are cached on disk in `feed_cache/`.
- **Bring your own data.** No databases, exports, tokens, feed URLs, or personal
  listening history are included in this repo. The `.gitignore` keeps all of
  that out; you point the ingesters at your own files locally.

## The identity model

The whole design follows from one rule: **canonical rows are keyed by public
data, never by an app's internal ids.**

- `podcasts` / `podcast_feed_urls` — a show and every feed URL ever seen for it
  (redirects, moved feeds, private/tokenized variants). Variants are
  *associated*, not merged: each stays its own row and points
  `public_podcast_id` at the family primary.
- `episodes` — keyed by `guid` and a normalized `enclosure_core` (the media
  URL with query strings and ad/tracking redirect chains stripped, so the same
  audio file matches across apps).
- `app_podcasts` / `app_episode_state` — each app's own ids and current play
  state, keyed by `(app, external_id)`.
- `observations` / `play_history` / `listen_sessions` — append-only history.
- The `merged_episode_state` view rolls all apps up into one "best known" state
  per episode.

Every identity-affecting rule lives once in `identity.py` (URL/title
normalization, private-feed detection, epoch-unit detection) and is covered by
`tests_identity.py`. See `DEDUP_RULES.md` for the full ruleset.

## Pipeline

`rebuild.py` runs the chain end to end (each ingester skips cleanly if its
input file isn't present):

```
ingest_pocketcasts.py   # your local Pocket Casts export (SQLite)   [reference adapter]
ingest_castro.py        # your local Castro .castrobackup           [reference adapter]
resolve_guids.py        # fetch PUBLIC RSS feeds, fill missing <guid>s
dedupe_episodes.py      # merge duplicate episode rows within a show
add_public_counterparts.py  # optional owner-curated feed knowledge
link_feed_variants.py   # group feed variants into show families
```

```sh
python3 rebuild.py --fresh   # build library.sqlite from scratch + verify invariants
python3 viewer.py            # render app/index.html
python3 serve.py             # view it at http://localhost:8574
python3 audit_dedup.py       # human-review report of dedup quality
python3 -m unittest tests_identity   # run the tests
```

## Adding another app

The ingesters are just reference adapters. A new one only has to, for each
interacted episode:

1. Upsert the show into `podcasts` / `podcast_feed_urls` (by feed URL if you
   have it).
2. Upsert the episode into `episodes` by public identity (`guid` and/or
   `enclosure_core` via `identity.core_url`).
3. Record the app's state in `app_episode_state` keyed by `(app, external_id)`,
   and append an `observations` row when state changed.

Then `resolve_guids.py` + `dedupe_episodes.py` + `link_feed_variants.py` handle
the cross-app reconciliation for free. `ingest_pocketcasts.py` is the smallest
worked example.

## Config files (optional)

Copy the templates and fill in your own; the real files are git-ignored:

- `feed_urls.json` — `{podcast_id: feed_url}` mapping for the Pocket Casts
  ingester (its export has no RSS URL; a good source is your own Pocket Casts
  OPML export, matched by title).
- `manual_links.example.json` → `manual_links.json` — owner rulings the linker
  can't derive.
- `public_counterparts.example.json` → `public_counterparts.json` — public feeds
  to anchor private variants to.
- `feed_replacements.example.json` → `feed_replacements.json` — successor URLs
  for feeds that moved.

## License

The Unlicense — this is released into the public domain. Do whatever you want
with it; no attribution required. See `LICENSE`.
