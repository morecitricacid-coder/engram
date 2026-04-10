-- Migration 006: Compressed session archives for deep recall

CREATE TABLE IF NOT EXISTS session_archives (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id),
    compressed_text TEXT NOT NULL,
    original_chars INTEGER,
    compressed_chars INTEGER,
    message_count INTEGER,
    archived_at TEXT DEFAULT (datetime('now'))
);

INSERT INTO schema_version VALUES (6, datetime('now'), 'Session archives for compressed conversation recall');
