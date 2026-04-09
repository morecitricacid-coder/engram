-- S2 Episodic Memory v1 — Initial Schema

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT DEFAULT (datetime('now')),
    description TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    model TEXT,
    topic_summary TEXT,
    transcript_path TEXT,
    message_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS mentions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    speaker TEXT NOT NULL CHECK(speaker IN ('user', 'assistant')),
    entity TEXT NOT NULL,
    raw_text TEXT,
    context_snippet TEXT,
    source TEXT DEFAULT 'hook'
);

CREATE TABLE IF NOT EXISTS connections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mention_id INTEGER NOT NULL REFERENCES mentions(id),
    related_mention_id INTEGER NOT NULL REFERENCES mentions(id),
    entity TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS surface_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    entity TEXT NOT NULL,
    recall_text TEXT NOT NULL,
    surfaced INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS entity_aliases (
    alias TEXT PRIMARY KEY,
    canonical TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS trg_mention_connect
AFTER INSERT ON mentions
BEGIN
    INSERT OR IGNORE INTO connections (mention_id, related_mention_id, entity)
    SELECT NEW.id, m.id, NEW.entity
    FROM mentions m
    WHERE m.entity = NEW.entity
      AND m.session_id != NEW.session_id;

    INSERT INTO surface_queue (session_id, entity, recall_text)
    SELECT
        NEW.session_id,
        NEW.entity,
        '- "' || NEW.entity || '" -- ' ||
        COUNT(DISTINCT m.session_id) || ' prior session(s)' || char(10) ||
        GROUP_CONCAT(
            '  > ' || substr(m.ts, 1, 10) || ': ' ||
            substr(COALESCE(m.context_snippet, m.raw_text, '(no snippet)'), 1, 120),
            char(10)
        )
    FROM mentions m
    WHERE m.entity = NEW.entity
      AND m.session_id != NEW.session_id
    GROUP BY NEW.entity
    HAVING COUNT(*) > 0;
END;

CREATE INDEX IF NOT EXISTS idx_mentions_entity ON mentions(entity);
CREATE INDEX IF NOT EXISTS idx_mentions_session ON mentions(session_id);
CREATE INDEX IF NOT EXISTS idx_mentions_ts ON mentions(ts);
CREATE INDEX IF NOT EXISTS idx_connections_entity ON connections(entity);
CREATE INDEX IF NOT EXISTS idx_surface_session ON surface_queue(session_id, surfaced);

INSERT INTO schema_version VALUES (1, datetime('now'), 'Initial episodic memory schema');
