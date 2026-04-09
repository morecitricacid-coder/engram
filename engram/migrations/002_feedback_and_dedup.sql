-- Migration 002: Recall feedback + trigger session dedup

CREATE TABLE IF NOT EXISTS recall_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    entity TEXT NOT NULL,
    score INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT 'explicit',
    user_note TEXT,
    reasoning TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_recall_feedback_entity ON recall_feedback(entity);

CREATE TABLE IF NOT EXISTS last_surfaced (
    session_id TEXT NOT NULL,
    entity TEXT NOT NULL,
    surfaced_at TEXT DEFAULT (datetime('now')),
    message_index INTEGER DEFAULT 0,
    PRIMARY KEY (session_id, entity)
);

DROP TRIGGER IF EXISTS trg_mention_connect;

CREATE TRIGGER trg_mention_connect
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
        COUNT(*) || ' prior session(s)' || char(10) ||
        GROUP_CONCAT(
            '  > ' || substr(sub.first_ts, 1, 10) || ': ' ||
            substr(sub.snippet, 1, 120),
            char(10)
        )
    FROM (
        SELECT
            m.session_id,
            MIN(m.ts) as first_ts,
            (SELECT m2.context_snippet
             FROM mentions m2
             WHERE m2.session_id = m.session_id
               AND m2.entity = NEW.entity
             ORDER BY m2.ts ASC
             LIMIT 1) as snippet
        FROM mentions m
        WHERE m.entity = NEW.entity
          AND m.session_id != NEW.session_id
        GROUP BY m.session_id
    ) sub
    HAVING COUNT(*) > 0;
END;

INSERT INTO schema_version (version, description)
VALUES (2, 'Recall feedback table + trigger session dedup');
