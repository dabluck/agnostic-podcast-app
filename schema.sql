-- library.sqlite: multi-app podcast listening warehouse.
--
-- Identity model: canonical rows are keyed by PUBLIC data (feed URL, RSS guid,
-- enclosure URL) so every app maps in the same way. App-specific IDs and play
-- state live in app_* tables keyed by (app, external_id). Ingesters upsert
-- canonical rows, link their app rows, and append an observation per sync.
--
-- Sparse by design: only interacted-with episodes get rows (played, in
-- progress, positioned, starred, or dated). Absence of a row = unplayed.
-- The full episode universe is always reconstructable from public feeds.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS podcasts (
    id                INTEGER PRIMARY KEY,
    feed_url          TEXT UNIQUE,         -- best current feed URL (see podcast_feed_urls for all)
    podcast_guid      TEXT,                -- <podcast:guid>; enrichment hook, no shipped ingester sets it yet
    title             TEXT NOT NULL,
    author            TEXT,
    website_url       TEXT,
    image_url         TEXT,                -- artwork; enrichment hook, unset by the shipped pipeline
    category          TEXT,                -- primary itunes category; enrichment hook, unset by default
    is_private        INTEGER NOT NULL DEFAULT 0,  -- tokenized/subscriber feed
    -- private/duplicate variants stay separate rows but point at their public
    -- counterpart here; NULL = this row is itself the public/primary feed
    public_podcast_id INTEGER REFERENCES podcasts(id)
);

-- Every feed URL ever seen for a podcast (redirects, initial vs current,
-- per-app variants). Identity resolution at ingest goes through this table,
-- so a feed that moved or is known under several URLs maps to one podcast.
CREATE TABLE IF NOT EXISTS podcast_feed_urls (
    url        TEXT PRIMARY KEY,
    podcast_id INTEGER NOT NULL REFERENCES podcasts(id),
    role       TEXT,                       -- 'current' | 'initial' | 'redirect' | 'duplicate'
    source     TEXT,                       -- app or process that reported it
    added_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pfu_podcast ON podcast_feed_urls(podcast_id);

CREATE TABLE IF NOT EXISTS episodes (
    id             INTEGER PRIMARY KEY,
    podcast_id     INTEGER NOT NULL REFERENCES podcasts(id),
    guid           TEXT,                   -- RSS <guid>; null if never seen in a feed
    enclosure_url  TEXT,                   -- as last seen
    enclosure_core TEXT,                   -- normalized: no query, tracker prefixes stripped
    title          TEXT NOT NULL,
    published_at   TEXT,                   -- ISO8601 UTC
    duration_sec   REAL,
    season         INTEGER,
    number         INTEGER,
    image_url      TEXT,                   -- episode-level art (item itunes:image)
    UNIQUE (podcast_id, guid)
);
CREATE INDEX IF NOT EXISTS idx_episodes_core ON episodes(podcast_id, enclosure_core);

-- One row per app's view of a podcast.
CREATE TABLE IF NOT EXISTS app_podcasts (
    app          TEXT NOT NULL,            -- 'pocketcasts', 'apple_podcasts', ...
    external_id  TEXT NOT NULL,            -- the app's own podcast id
    podcast_id   INTEGER NOT NULL REFERENCES podcasts(id),
    subscribed   INTEGER,
    PRIMARY KEY (app, external_id)
);

-- One row per app's CURRENT state for an episode (upserted each sync).
CREATE TABLE IF NOT EXISTS app_episode_state (
    app            TEXT NOT NULL,
    external_id    TEXT NOT NULL,          -- the app's own episode id
    episode_id     INTEGER NOT NULL REFERENCES episodes(id),
    status         TEXT NOT NULL CHECK (status IN ('unplayed','partial','played')),
    raw_status     TEXT,                   -- app's untranslated value, for audit
    position_sec   REAL,
    last_played_at TEXT,                   -- ISO8601; null when the app doesn't know
    starred        INTEGER,
    archived       INTEGER,
    updated_at     TEXT NOT NULL,          -- when we last synced this row
    PRIMARY KEY (app, external_id)
);
CREATE INDEX IF NOT EXISTS idx_state_episode ON app_episode_state(episode_id);

-- Append-only: one row per (app, episode) per sync WHERE STATE CHANGED.
-- This is how we accumulate played dates that apps refuse to export:
-- first observation of status='played' bounds the real played date.
CREATE TABLE IF NOT EXISTS observations (
    id           INTEGER PRIMARY KEY,
    app          TEXT NOT NULL,
    episode_id   INTEGER NOT NULL REFERENCES episodes(id),
    status       TEXT NOT NULL,
    position_sec REAL,
    observed_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_obs_episode ON observations(episode_id, observed_at);

-- Dated listen events per app (a known "last listened" timestamp for an
-- episode, when a source exposes one). Append-only; (app, episode, played_at) unique.
CREATE TABLE IF NOT EXISTS play_history (
    id         INTEGER PRIMARY KEY,
    app        TEXT NOT NULL,
    episode_id INTEGER NOT NULL REFERENCES episodes(id),
    played_at  TEXT NOT NULL,                -- ISO8601 UTC
    source     TEXT,                         -- e.g. 'castro_backup'
    UNIQUE (app, episode_id, played_at)
);
CREATE INDEX IF NOT EXISTS idx_ph_episode ON play_history(episode_id);

-- App-level lifetime stats (e.g. total seconds listened), snapshotted per sync
-- so cross-app totals can be compared and tracked over time.
CREATE TABLE IF NOT EXISTS app_stats (
    app   TEXT NOT NULL,
    key   TEXT NOT NULL,       -- e.g. 'time_listened_sec', 'time_skipping_sec'
    value TEXT NOT NULL,
    as_of TEXT NOT NULL,       -- ISO8601 snapshot time
    PRIMARY KEY (app, key, as_of)
);

-- Individual listening sessions (e.g. Castro playSessions): wall-clock span plus
-- which slice of the episode audio was consumed. listened_sec is the audio
-- consumed (to - from), distinct from wall clock (ended - began).
CREATE TABLE IF NOT EXISTS listen_sessions (
    id           INTEGER PRIMARY KEY,
    app          TEXT NOT NULL,
    external_id  TEXT UNIQUE,             -- the app's session id
    episode_id   INTEGER NOT NULL REFERENCES episodes(id),
    began_at     TEXT NOT NULL,           -- ISO8601 UTC
    ended_at     TEXT,
    from_sec     REAL,                    -- position where playback started
    to_sec       REAL,                    -- position where playback stopped
    listened_sec REAL,                    -- audio consumed
    trimmed_sec  REAL,                    -- silence-trim savings
    source       TEXT
);
CREATE INDEX IF NOT EXISTS idx_ls_episode ON listen_sessions(episode_id, began_at);

-- Feed-family lens: group a show's variants (private feeds, duplicate URLs)
-- under one family_id for combined queries, without merging the rows.
DROP VIEW IF EXISTS podcast_families;
CREATE VIEW podcast_families AS
SELECT p.id AS podcast_id,
       coalesce(p.public_podcast_id, p.id) AS family_id,
       p.title, p.is_private, p.feed_url
FROM podcasts p;

-- Cross-app rollup: one row per episode with the "best" known state.
-- Merge rules (owner-confirmed): 'played' wins over 'partial' wins over
-- 'unplayed'; last_played_at is the most recent across app states, play
-- history, and sessions; sessions are combined across apps; position_sec
-- comes from the app with the most recent listen.
DROP VIEW IF EXISTS merged_episode_state;
CREATE VIEW merged_episode_state AS
SELECT
    e.id                    AS episode_id,
    p.title                 AS podcast,
    e.title                 AS episode,
    e.guid,
    e.published_at,
    CASE max(CASE s.status WHEN 'played' THEN 2 WHEN 'partial' THEN 1 ELSE 0 END)
         WHEN 2 THEN 'played' WHEN 1 THEN 'partial' ELSE 'unplayed' END AS status,
    coalesce(
      (SELECT s2.position_sec FROM app_episode_state s2
        WHERE s2.episode_id = e.id AND s2.position_sec IS NOT NULL
        ORDER BY s2.last_played_at IS NULL, s2.last_played_at DESC, s2.updated_at DESC
        LIMIT 1),
      (SELECT ls.to_sec FROM listen_sessions ls WHERE ls.episode_id = e.id
        ORDER BY ls.began_at DESC LIMIT 1)) AS position_sec,
    nullif(max(coalesce(max(s.last_played_at), ''),
               coalesce((SELECT max(ph.played_at) FROM play_history ph
                         WHERE ph.episode_id = e.id), ''),
               coalesce((SELECT max(ls.began_at) FROM listen_sessions ls
                         WHERE ls.episode_id = e.id), '')), '') AS last_played_at,
    max(s.starred)          AS starred,
    (SELECT count(*) FROM listen_sessions ls WHERE ls.episode_id = e.id)
                            AS session_count,
    (SELECT round(sum(ls.listened_sec)) FROM listen_sessions ls
      WHERE ls.episode_id = e.id) AS listened_sec,
    group_concat(DISTINCT s.app) AS apps
FROM episodes e
JOIN podcasts p ON p.id = e.podcast_id
LEFT JOIN app_episode_state s ON s.episode_id = e.id
GROUP BY e.id;
