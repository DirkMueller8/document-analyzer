-- corpus.db - lives in data/ (gitignored, never leaves the machine)
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS entries (
    id             INTEGER PRIMARY KEY,
    date_start     TEXT,              -- ISO date, NULL if undated
    date_end       TEXT,
    date_precision TEXT CHECK (date_precision IN ('day', 'month', 'year')),
    raw_date       TEXT,
    text           TEXT NOT NULL,
    text_hash      TEXT NOT NULL,     -- keeps enrichment idempotent
    source_pos     INTEGER NOT NULL,
    format         TEXT NOT NULL CHECK (format IN ('plain', 'table'))
);
CREATE INDEX IF NOT EXISTS idx_entries_date ON entries(date_start);

-- Phase 2: chunks + FTS5 + sqlite-vec
CREATE TABLE IF NOT EXISTS chunks (
    id       INTEGER PRIMARY KEY,
    entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    seq      INTEGER NOT NULL,
    text     TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, content='chunks', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);

-- Phase 3: enrichment
CREATE TABLE IF NOT EXISTS entry_meta (
    entry_id   INTEGER PRIMARY KEY REFERENCES entries(id) ON DELETE CASCADE,
    text_hash  TEXT NOT NULL,
    mood_score INTEGER CHECK (mood_score BETWEEN -5 AND 5),
    emotions   TEXT,   -- JSON array
    energy     INTEGER,
    sleep      TEXT,
    themes     TEXT    -- JSON array
);
CREATE TABLE IF NOT EXISTS people (
    id             INTEGER PRIMARY KEY,
    canonical_name TEXT NOT NULL UNIQUE,
    aliases        TEXT NOT NULL DEFAULT '[]'  -- JSON array
);
CREATE TABLE IF NOT EXISTS mentions (
    person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    entry_id  INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    PRIMARY KEY (person_id, entry_id)
);
CREATE TABLE IF NOT EXISTS health_events (
    id        INTEGER PRIMARY KEY,
    entry_id  INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    condition TEXT NOT NULL,
    category  TEXT,
    type      TEXT CHECK (type IN ('symptom', 'diagnosis', 'treatment', 'recovery')),
    severity  INTEGER
);
CREATE TABLE IF NOT EXISTS activities (
    id           INTEGER PRIMARY KEY,
    entry_id     INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    domain       TEXT NOT NULL,     -- defined in taxonomy.yaml (data/, else config/ example)
    activity     TEXT NOT NULL,
    duration_min INTEGER,
    intensity    INTEGER,
    sentiment    INTEGER,
    note         TEXT
);
CREATE TABLE IF NOT EXISTS milestones (
    id       INTEGER PRIMARY KEY,
    entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    domain   TEXT NOT NULL,
    title    TEXT NOT NULL,
    kind     TEXT CHECK (kind IN ('first', 'achievement', 'setback', 'decision'))
);

-- Deterministic measurements (e.g. body weight), extracted by patterns from measurements.yaml
CREATE TABLE IF NOT EXISTS measurements (
    id       INTEGER PRIMARY KEY,
    entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    date     TEXT NOT NULL,          -- ISO date the value belongs to
    kind     TEXT NOT NULL,          -- e.g. 'weight'
    value    REAL NOT NULL,
    unit     TEXT,
    source   TEXT NOT NULL CHECK (source IN ('entry', 'ref'))  -- entry date or a date written before the value
);
CREATE INDEX IF NOT EXISTS idx_measurements ON measurements(kind, date);

-- Keep the full-text index in sync with chunks (the vector table gets its trigger in search/index.py)
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE INDEX IF NOT EXISTS idx_chunks_entry ON chunks(entry_id);
CREATE TABLE IF NOT EXISTS index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
