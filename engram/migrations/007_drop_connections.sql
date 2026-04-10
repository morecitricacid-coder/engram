-- Migration 007: Drop unused connections table, keep surface_queue trigger
--
-- The connections table (85K+ rows) is never read by any code path.
-- Co-occurrence scoring joins mentions directly. Surface queue is the
-- only trigger output that matters. Strip the dead weight.

DROP TRIGGER IF EXISTS trg_mention_connect;

CREATE TRIGGER trg_mention_surface
AFTER INSERT ON mentions
BEGIN
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

DROP INDEX IF EXISTS idx_connections_entity;
DROP INDEX IF EXISTS idx_connections_unique;
DROP TABLE IF EXISTS connections;

INSERT INTO schema_version (version, description)
VALUES (7, 'Drop unused connections table, surface-only trigger');
