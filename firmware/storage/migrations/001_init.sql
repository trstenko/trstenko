-- The first schema. Migrations only ever add: new tables, new columns with
-- defaults, new indexes. A release that is rolled back has to be able to open
-- a database a newer release has already migrated, so nothing here may be
-- removed, renamed or change meaning later (plan 06).

CREATE TABLE schema_version (
  version INTEGER NOT NULL
);

CREATE TABLE memories (
  id           INTEGER PRIMARY KEY,
  text         TEXT NOT NULL,
  category     TEXT NOT NULL DEFAULT 'general',
  created_at   REAL NOT NULL,
  last_used_at REAL,
  pinned       INTEGER NOT NULL DEFAULT 0,
  source       TEXT NOT NULL DEFAULT 'voice'
);

-- Reminders and timers (plan 02).
CREATE TABLE jobs (
  id          INTEGER PRIMARY KEY,
  kind        TEXT NOT NULL,          -- 'timer' | 'reminder' | 'repeat'
  text        TEXT NOT NULL,          -- what to say ('' for a bare timer)
  local_time  TEXT,                   -- '2026-10-02T18:00', reminders only
  tz          TEXT,                   -- 'Europe/Ljubljana'
  repeat      TEXT,                   -- NULL | 'daily' | 'weekdays' | 'weekly:MO,WE'
  due_wall    REAL,                   -- next firing, epoch seconds, computed
  due_mono    REAL,                   -- timers, this boot only
  state       TEXT NOT NULL,          -- 'pending' | 'ringing' | 'done' | 'missed' | 'cancelled' | 'interrupted'
  created_at  REAL NOT NULL,
  fired_at    REAL,
  acked_at    REAL,
  source      TEXT NOT NULL           -- 'voice' | 'web' | 'local_intent'
);
CREATE INDEX jobs_pending ON jobs(state, due_wall);

-- Plan 05. Money is in millionths of a dollar so sums do not drift.
CREATE TABLE usage (
  id               INTEGER PRIMARY KEY,
  at               REAL NOT NULL,
  day              TEXT NOT NULL,
  turn_id          TEXT NOT NULL,
  provider         TEXT NOT NULL,
  model            TEXT NOT NULL,
  kind             TEXT NOT NULL,     -- 'chat' | 'stt' | 'tts'
  input_tokens     INTEGER DEFAULT 0,
  cached_tokens    INTEGER DEFAULT 0,
  output_tokens    INTEGER DEFAULT 0,
  reasoning_tokens INTEGER DEFAULT 0,
  audio_seconds    REAL DEFAULT 0,
  estimated        INTEGER DEFAULT 0,
  cost_usd_micro   INTEGER DEFAULT 0,
  latency_ms       INTEGER,
  status           TEXT NOT NULL,     -- 'ok' | 'rate_limited' | 'quota' | 'auth' | 'error' | 'timeout' | 'cancelled'
  free_tier        INTEGER DEFAULT 0
);
CREATE INDEX usage_day ON usage(day);

CREATE TABLE usage_daily (
  day            TEXT,
  provider       TEXT,
  model          TEXT,
  requests       INTEGER,
  input_tokens   INTEGER,
  output_tokens  INTEGER,
  audio_seconds  REAL,
  cost_usd_micro INTEGER,
  PRIMARY KEY (day, provider, model)
);

-- Only written when the owner has turned the conversation log on; the last 50
-- are kept.
CREATE TABLE turn_log (
  id         INTEGER PRIMARY KEY,
  at         REAL NOT NULL,
  turn_id    TEXT NOT NULL,
  transcript TEXT,
  reply      TEXT,
  tools_json TEXT
);

-- What the owner sees on the page; the last 500 are kept.
CREATE TABLE events_log (
  id     INTEGER PRIMARY KEY,
  at     REAL NOT NULL,
  kind   TEXT NOT NULL,
  detail TEXT
);
