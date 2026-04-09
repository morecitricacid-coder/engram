-- Migration 004: Prefer user-speaker snippets in recall trigger

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
            '  > ' || substr(sub.best_ts, 1, 10) || ': ' ||
            substr(sub.snippet, 1, 120),
            char(10)
        )
    FROM (
        SELECT
            m.session_id,
            MIN(m.ts) as best_ts,
            COALESCE(
                (SELECT m2.context_snippet
                 FROM mentions m2
                 WHERE m2.session_id = m.session_id
                   AND m2.entity = NEW.entity
                   AND m2.speaker = 'user'
                   AND m2.context_snippet IS NOT NULL
                 ORDER BY m2.ts ASC
                 LIMIT 1),
                (SELECT m2.context_snippet
                 FROM mentions m2
                 WHERE m2.session_id = m.session_id
                   AND m2.entity = NEW.entity
                 ORDER BY m2.ts ASC
                 LIMIT 1)
            ) as snippet
        FROM mentions m
        WHERE m.entity = NEW.entity
          AND m.session_id != NEW.session_id
        GROUP BY m.session_id
    ) sub
    HAVING COUNT(*) > 0;
END;

INSERT INTO schema_version VALUES (4, datetime('now'), 'Prefer user-speaker snippets in recall trigger');
