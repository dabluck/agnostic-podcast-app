# Podcast & Episode Deduplication Rules

How `library.sqlite` decides that two feeds are the same show and two rows are
the same episode. Every identity rule is implemented once in `identity.py`
(tested by `tests_identity.py`) and enforced by `dedupe_episodes.py`
(episodes) and `link_feed_variants.py` (podcast families).
Full rebuild: `python3 rebuild.py --fresh`, which runs
`ingest_pocketcasts.py → ingest_castro.py → resolve_guids.py →
dedupe_episodes.py → add_public_counterparts.py → link_feed_variants.py`
and the verification queries below. (Add more ingesters by following the
same contract — see the README.)

## Identity model

- **Canonical rows are keyed by public data** (feed URL, RSS guid, enclosure
  URL) — never by an app's internal ids.
- **Variants are associated, not merged.** A show's private feed, moved feed,
  or duplicate URL stays its own `podcasts` row; `public_podcast_id` points at
  the family primary. Query the family via the `podcast_families` view.
- **Sparse warehouse caveat:** episode rows exist only for interacted
  episodes, so two feeds of one show can share *zero* stored episodes (each
  app touched a different era). Absence of overlap is therefore **never**
  evidence against linking; presence of overlap is strong evidence for it.

## Podcast (feed) rules — `link_feed_variants.py`

0. **Private flag backfill.** A feed is private if any of its URLs matches:
   `/private/`, `?auth=`, `?access_token=`, `supportingcast.fm/content/`,
   `passport.online`, `/members/`, `patreon.com/rss/`.
   (Apps don't reliably flag this themselves; Pocket Casts marked a tokenized
   Patreon feed as public.)
1. **Same normalized title ⇒ same family.** Normalization: drop
   "(private feed …)", "(premium feed)", "(ad-free)", "(the binge)"
   parentheticals, then strip all non-alphanumerics, lowercase.
   Applies across **all** private/public combinations — including
   private↔private (re-subscribing to a Substack/Supporting Cast show mints a
   new token URL each time; we found 3 generations of "Politix").
2. **Variant-suffix fold.** A title that equals an existing family's title
   after removing a trailing membership marker — `Club`, `Patrons-Only
   (Episodes Feed)`, `Bonus Content/Episodes`, `Members (Only)`, `Archives`,
   `Ad-Free`, `Premium`, `Plus` — joins that family **if** it is private
   (membership feeds are) **or** shares ≥ 2 content signatures with it.
   Example: "The Rest Is History Club" → "The Rest Is History".
3. **`<podcast:guid>` equality always links.**
4. **Primary selection:** the public row with the most canonical episodes;
   if the whole family is private, the private row with the most episodes.
   Chains are flattened (every member points directly at the root).
5. **What we deliberately do NOT do: fuzzy title matching.** Near-titles are
   usually different shows ("Founders" ≠ "The Founder", "Hysterical" ≠
   "Hysteria", "Bookclub" (BBC) ≠ "The Book Club"). Anything that only a
   fuzzy match would catch is surfaced by `audit_dedup.py` for human review
   instead of being linked automatically.
6. **Manual overrides** (`manual_links.json`): owner rulings the rules can't
   derive (e.g. "The Stratechery Podcast" member feed = "Stratechery",
   "No Agenda" old feed = "No Agenda Show"). Keyed by feed URL so they
   survive full rebuilds; applied by the linker after the rule passes.
   Prefer `"match": "exact"` — a `"prefix"` once swallowed a *different show*
   on the same host whose URL differed only by access token (the Stratechery
   Plus bundle serves multiple shows from one member-feed path).

**Content signature** of a feed = set of (normalized episode title,
publish date) pairs plus normalized enclosure cores of its stored episodes.
Used to confirm rule-2 folds; superseded guid-overlap checks (guids differ
across app eras even for one feed, so guid overlap under-counts).

## Episode rules — matching at ingest + `dedupe_episodes.py`

Within one podcast, rows are matched in this order:
1. **guid equality** (exact).
2. **enclosure core equality** — URL minus query string, truncated to the
   media path from the LAST embedded hostname (strips redirector chains like
   podtrac → swap.fm → pscrb.fm → real host). Implemented once in
   `identity.core_url`; guarded by tests — an earlier leftmost-match
   version silently kept the whole chain and cost ~50 cross-app merges.
3. **normalized title + publish date (day precision).**

Merge policy (`dedupe_episodes.py`): rows matching on (2) or (3) merge even
when their guids differ — feeds change guid schemes between app eras, so two
guids can name one episode. Keeper = the row with a guid, else the oldest row;
missing fields are backfilled from the duplicate; all references
(`app_episode_state`, `play_history`, `listen_sessions`, `observations`)
are repointed. Runs to a fixed point.

## Epoch/date rules (bugs that broke dedup once)

- **Never assume a timestamp unit — detect it.** Epoch value > 1e11 ⇒
  milliseconds, else seconds. (Aurelian `pubDate` is seconds; assuming ms put
  5,651 episodes in January 1970 and silently disabled title+date matching.)
- Castro backups mix epochs by section: `exportDate`/queue dates are Apple
  reference dates (+978307200 to Unix); sessions and `lastPlayed` are Unix
  seconds.
- All stored dates are ISO8601 UTC (`YYYY-MM-DDTHH:MM:SSZ`); comparisons use
  day precision for matching.

## Cross-app state merge rules (`merged_episode_state` view)

Owner-confirmed 2026-07-10:

- **Status:** `played` > `partial` > `unplayed`. Any app saying played wins,
  even if a later interaction elsewhere is only partial (a re-listen never
  demotes a finished episode).
- **last_played_at:** most recent timestamp across app states, `play_history`
  rows, and `listen_sessions` begins.
- **Sessions:** combined across apps (union) — `session_count` and
  `listened_sec` aggregate everything.
- **position_sec:** from the app with the most recent listen that actually
  reports a position; falls back to the newest session's `to_sec` when no app
  state carries one.
- **starred:** any app starring it marks it starred.

## Verification queries (run after any rebuild)

```sql
-- must be 0: date-unit regressions
SELECT count(*) FROM episodes WHERE published_at < '2000';
-- must be 0 pairs: within-podcast dupes (see audit_dedup.py section 3)
-- families should not regress upward without new data
SELECT count(DISTINCT coalesce(public_podcast_id, id)) FROM podcasts;
```

These invariants are enforced automatically: `rebuild.py` fails if any is
non-zero (including plays/sessions dated before 2015, which catches an
Apple-reference epoch misfiled as Unix seconds — a case magnitude detection
cannot see). `audit_dedup.py` runs the full four-part audit (missed dupes,
over-aggressive links, episode dupes, off-by-one-day title pairs) for human
review.

Known accepted behavior: running ingesters incrementally against an existing
DB *converges* (dedupe re-merges churned rows each run) but is not a strict
no-op — episode ids can change. For a canonical state, use
`rebuild.py --fresh`, which is fully deterministic.
